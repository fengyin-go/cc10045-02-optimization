"""告警判定引擎：按指标采样记录计算连续超限。

设计要点（与告警判定稳定性直接相关）：

* 连续次数只数“采样”，不数“检查次数”。同一 ``(采样时刻, 指标口径)``
  无论被检查多少次都只算一条。
* 连续段必须在同一指标口径内、且采样时刻连续。缺一个采样槽位
  （迟到补报尚未到）就断开，后到的更晚采样不能跨过缺口接续。
* 每次判定都会落一条 ``alert_checks`` 审计记录，写明当时用了哪些采样、
  缺口在哪、还能不能继续等；真正达到阈值时再写 ``alert_log`` 与
  ``alert_evidence``，告警记录带上判定采样时刻和异常区间。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from app.database import db

TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_GRACE_SECONDS = 300


# ---------------------------------------------------------------------------
# 时间与条件解析
# ---------------------------------------------------------------------------

def parse_time(value: str | None) -> datetime | None:
    """解析 ISO 风格时间，兼容末尾 Z 和毫秒。"""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in (TIME_FORMAT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(TIME_FORMAT)


def _shift(value: str, seconds: int) -> str:
    parsed = parse_time(value)
    if parsed is None:
        return ""
    return format_time(parsed + timedelta(seconds=seconds))


def _seconds_between(start: str, end: str) -> float | None:
    left, right = parse_time(start), parse_time(end)
    if left is None or right is None:
        return None
    return (right - left).total_seconds()


def parse_conditions(condition_text: str | None) -> list[tuple[str, float]]:
    """把``> 100`` / ``< 50,>= 10`` 之类的条件解析成 (op, threshold) 列表。

    多个条件之间与原实现保持一致，按“任一满足”处理。
    """
    text = str(condition_text or "").strip().replace("＞", ">").replace("＜", "<")
    if not text:
        return []

    conditions: list[tuple[str, float]] = []
    for part in text.replace("；", ",").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        for op in (">=", "<=", ">", "<"):
            if part.startswith(op):
                raw = part[len(op):].strip()
                try:
                    conditions.append((op, float(raw)))
                except ValueError:
                    pass
                break
    return conditions


def condition_matches(value: float, conditions: list[tuple[str, float]]) -> bool:
    for op, threshold in conditions:
        if op == ">=" and value >= threshold:
            return True
        if op == "<=" and value <= threshold:
            return True
        if op == ">" and value > threshold:
            return True
        if op == "<" and value < threshold:
            return True
    return False


def primary_threshold(conditions: list[tuple[str, float]]) -> float | None:
    return conditions[0][1] if conditions else None


# ---------------------------------------------------------------------------
# 采样读取：按 (采样时刻, 口径版本) 去重，保证同一采样只算一次
# ---------------------------------------------------------------------------

def resolve_metric(monitor_metric: str | None) -> dict[str, Any] | None:
    name = str(monitor_metric or "").strip()
    if not name:
        return None
    exact = db.fetchone("SELECT * FROM metrics WHERE metric_name=?", [name])
    if exact:
        return exact
    return db.fetchone(
        "SELECT * FROM metrics WHERE metric_name LIKE ? ORDER BY LENGTH(metric_name), id LIMIT 1",
        [f"%{name}%"],
    )


def _row_sample_time(row: dict[str, Any]) -> str:
    return str(row.get("sample_time") or row.get("snapshot_time") or "")


def _row_calibration(row: dict[str, Any]) -> int:
    try:
        return int(row.get("calibration_version") or 1)
    except (TypeError, ValueError):
        return 1


def load_samples(
    metric_id: int,
    *,
    as_of: str | None = None,
    calibration_version: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """加载并去重采样。

    * 采样时刻统一成 ISO 格式，避免 ``10:00:00`` 与 ``10:00:00+00:00`` 之类
      的写法被当成两个采样；
    * 同一 ``(采样时刻, 口径版本)`` 若存在多条（例如补报覆盖），以最后写入的
      一条为准；返回去重后的采样列表与重复条数。

    ``as_of`` 是采样时刻截止点：只看该时刻及之前的采样。
    """
    cutoff = format_time(parse_time(as_of)) if as_of and parse_time(as_of) else as_of
    rows = db.fetchall(
        "SELECT * FROM metric_values WHERE metric_id=? ORDER BY id ASC",
        [metric_id],
    )
    deduped: dict[tuple[str, int], tuple[str, dict[str, Any]]] = {}
    duplicates = 0
    for row in rows:
        raw_time = _row_sample_time(row)
        parsed_time = parse_time(raw_time)
        if parsed_time is None:
            continue
        sample_time = format_time(parsed_time)
        if cutoff and sample_time > cutoff:
            continue
        calibration = _row_calibration(row)
        if calibration_version is not None and calibration != calibration_version:
            continue
        key = (sample_time, calibration)
        if key in deduped:
            duplicates += 1
        deduped[key] = (raw_time, row)

    samples: list[dict[str, Any]] = []
    for (sample_time, calibration), (raw_time, row) in deduped.items():
        samples.append({
            "id": int(row["id"]),
            "sample_time": sample_time,
            "raw_sample_time": raw_time,
            "value": float(row.get("snapshot_value") or 0.0),
            "calibration_version": calibration,
            "source": str(row.get("source") or "live"),
            "ingested_at": row.get("ingested_at") or row.get("created_at"),
        })
    samples.sort(key=lambda item: (item["sample_time"], item["calibration_version"]))
    return samples, duplicates


def public_sample(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": sample["id"],
        "sample_time": sample["sample_time"],
        "value": sample["value"],
        "calibration_version": sample["calibration_version"],
        "source": sample["source"],
        "ingested_at": sample["ingested_at"],
    }


# ---------------------------------------------------------------------------
# 连续段计算
# ---------------------------------------------------------------------------

def resolve_interval(alert: dict[str, Any], samples: list[dict[str, Any]]) -> int:
    try:
        configured = int(alert.get("sample_interval_seconds") or 0)
    except (TypeError, ValueError):
        configured = 0
    if configured > 0:
        return configured

    gaps: list[float] = []
    by_calibration: dict[int, list[str]] = {}
    for sample in samples:
        bucket = by_calibration.setdefault(sample["calibration_version"], [])
        if not bucket or bucket[-1] != sample["sample_time"]:
            bucket.append(sample["sample_time"])
    for times in by_calibration.values():
        for previous, current in zip(times, times[1:]):
            gap = _seconds_between(previous, current)
            if gap and gap > 0:
                gaps.append(gap)
    return int(min(gaps)) if gaps else DEFAULT_INTERVAL_SECONDS


def group_by_calibration(samples: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    groups: dict[int, list[dict[str, Any]]] = {}
    for sample in samples:
        groups.setdefault(sample["calibration_version"], []).append(sample)
    for group in groups.values():
        group.sort(key=lambda item: item["sample_time"])
    return groups


def _tolerance(interval: int) -> float:
    # 允许采样时刻有少量抖动；缺失一个完整采样槽位必然超过该容差。
    return interval + max(1.0, interval * 0.5)


def build_runs(
    samples: list[dict[str, Any]],
    *,
    conditions: list[tuple[str, float]],
    interval: int,
) -> list[dict[str, Any]]:
    """把超限采样切成连续段。

    跨口径、跨缺口、遇到未超限采样都会断开。
    """
    runs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    tolerance = _tolerance(interval)

    for sample in samples:
        exceeded = condition_matches(sample["value"], conditions)
        if not exceeded:
            if current is not None:
                current["closed_by"] = "non_exceeded"
                current["closed_at"] = sample["sample_time"]
                current = None
            continue

        if current is None:
            current = {
                "samples": [sample],
                "calibration_version": sample["calibration_version"],
                "start_time": sample["sample_time"],
                "last_time": sample["sample_time"],
                "closed_by": None,
                "closed_at": None,
                "break_reason": None,
                "missing_sample_time": None,
            }
            runs.append(current)
            continue

        gap = _seconds_between(current["last_time"], sample["sample_time"]) or 0.0
        calibration_changed = sample["calibration_version"] != current["calibration_version"]
        gap_too_large = gap > tolerance

        if calibration_changed or gap_too_large:
            current["closed_by"] = "calibration_switch" if calibration_changed else "gap"
            current["closed_at"] = sample["sample_time"]
            if gap_too_large and not calibration_changed:
                expected = _shift(current["last_time"], interval)
                current["missing_sample_time"] = expected
                current["break_reason"] = (
                    f"缺少采样 {expected}，不能跨缺口接续连续段"
                )
            elif calibration_changed:
                current["break_reason"] = (
                    f"指标口径从 v{current['calibration_version']} "
                    f"切换到 v{sample['calibration_version']}，不能接到同一连续段"
                )
            current = {
                "samples": [sample],
                "calibration_version": sample["calibration_version"],
                "start_time": sample["sample_time"],
                "last_time": sample["sample_time"],
                "closed_by": None,
                "closed_at": None,
                "break_reason": (
                    f"口径切换后重新计数（v{sample['calibration_version']}）"
                    if calibration_changed else None
                ),
                "missing_sample_time": None,
            }
            runs.append(current)
            continue

        current["samples"].append(sample)
        current["last_time"] = sample["sample_time"]

    return runs


def _run_summary(run: dict[str, Any], threshold: int) -> dict[str, Any]:
    return {
        "calibration_version": run["calibration_version"],
        "start_time": run["start_time"],
        "last_time": run["last_time"],
        "sample_count": len(run["samples"]),
        "exceeded": len(run["samples"]) >= threshold,
        "closed_by": run.get("closed_by"),
        "closed_at": run.get("closed_at"),
        "missing_sample_time": run.get("missing_sample_time"),
        "break_reason": run.get("break_reason"),
        "sample_times": [sample["sample_time"] for sample in run["samples"]],
    }


def _run_key(alert_id: int, run: dict[str, Any]) -> str:
    return f"{alert_id}:v{run['calibration_version']}:{run['start_time']}"


# ---------------------------------------------------------------------------
# 告警记录
# ---------------------------------------------------------------------------

def _find_log(alert_id: int, run_key: str) -> dict[str, Any] | None:
    return db.fetchone(
        "SELECT * FROM alert_log WHERE alert_id=? AND run_key=?",
        [alert_id, run_key],
    )


def _find_overlapping_log(alert_id: int, sample_ids: list[int]) -> dict[str, Any] | None:
    """同一段证据里只要有一个采样已经触发过，就不重复告警。"""
    existing_ids = set(sample_ids)
    if not existing_ids:
        return None
    rows = db.fetchall(
        "SELECT * FROM alert_log WHERE alert_id=? ORDER BY id DESC LIMIT 50",
        [alert_id],
    )
    for row in rows:
        try:
            logged_ids = set(json.loads(row.get("sample_ids") or "[]"))
        except (TypeError, json.JSONDecodeError):
            continue
        if logged_ids & existing_ids:
            return row
    return None


def _write_trigger(
    alert: dict[str, Any],
    run: dict[str, Any],
    *,
    threshold: int,
    interval: int,
    conditions: list[tuple[str, float]],
    checked_at: str,
) -> tuple[dict[str, Any], bool]:
    """写告警日志与超限证据；同一连续段只写一次。"""
    evidence = run["samples"][:threshold]
    sample_ids = [sample["id"] for sample in evidence]

    run_key = _run_key(int(alert["id"]), run)
    existing = _find_log(int(alert["id"]), run_key)
    if existing:
        return existing, False
    # 同一批采样若已作为另一段（例如向后补到的新段）的证据触发过，视为已触发。
    existing = _find_overlapping_log(int(alert["id"]), sample_ids)
    if existing:
        return existing, False

    window_start = evidence[0]["sample_time"]
    window_end = evidence[-1]["sample_time"]
    threshold_value = primary_threshold(conditions)
    rule_snapshot = {
        "rule_name": alert.get("rule_name"),
        "monitor_metric": alert.get("monitor_metric"),
        "alert_condition": alert.get("alert_condition"),
        "consecutive_threshold": threshold,
        "sample_interval_seconds": interval,
    }

    log_id = db.insert("alert_log", {
        "alert_id": int(alert["id"]),
        "triggered_at": checked_at,
        "metric_id": run.get("metric_id"),
        "calibration_version": run["calibration_version"],
        "sample_time": window_end,
        "window_start": window_start,
        "window_end": window_end,
        "consecutive_count": len(evidence),
        "metric_value": evidence[-1]["value"],
        "threshold_value": threshold_value,
        "sample_ids": json.dumps(sample_ids),
        "evidence_json": json.dumps(
            [public_sample(sample) for sample in evidence], ensure_ascii=False
        ),
        "rule_snapshot": json.dumps(rule_snapshot, ensure_ascii=False),
        "run_key": run_key,
    })

    for position, sample in enumerate(evidence, start=1):
        db.insert("alert_evidence", {
            "alert_log_id": log_id,
            "metric_value_id": sample["id"],
            "sample_time": sample["sample_time"],
            "sample_value": sample["value"],
            "calibration_version": sample["calibration_version"],
            "position": position,
        })

    return db.fetchone("SELECT * FROM alert_log WHERE id=?", [log_id]) or {}, True


# ---------------------------------------------------------------------------
# 判定主流程
# ---------------------------------------------------------------------------

def _pending_for_run(
    run: dict[str, Any],
    *,
    alert: dict[str, Any],
    interval: int,
    threshold: int,
    as_of: str,
    sample_slots: set[tuple[str, int]],
) -> dict[str, Any] | None:
    if len(run["samples"]) >= threshold:
        return None
    closed_by = run.get("closed_by")
    if closed_by == "non_exceeded":
        return None

    expected = _shift(run["last_time"], interval)
    if not expected:
        return None
    # 期望槽位已经有采样（包括口径切换后的采样），该段已被现实数据截断。
    if any(slot_time == expected for slot_time, _ in sample_slots):
        return None

    grace_seconds = int(alert.get("backfill_grace_seconds") or DEFAULT_GRACE_SECONDS)
    deadline = _shift(expected, grace_seconds)
    can_wait = bool(as_of and deadline and as_of <= deadline)

    remaining = threshold - len(run["samples"])
    if closed_by == "gap":
        reason = (
            f"{expected} 的采样尚未到报，{run['last_time']} 之后的更晚采样"
            f"不能跨缺口凑成连续超限；还差 {remaining} 个连续样本"
        )
    else:
        reason = f"连续超限 {len(run['samples'])} 个，还差 {remaining} 个，等待 {expected} 的采样"

    would_trigger = len(run["samples"]) + 1 >= threshold
    return {
        "sample_time": expected,
        "reason": reason,
        "remaining_samples": remaining,
        "grace_deadline": deadline,
        "grace_seconds": grace_seconds,
        "can_wait": can_wait,
        "wait_state": "可以继续等待补报" if can_wait else "补报窗口已过",
        "run": _run_summary(run, threshold),
        "if_backfilled_exceeded": {
            "would_trigger": would_trigger,
            "window_start": run["start_time"] if would_trigger else None,
            "window_end": expected if would_trigger else None,
            "evidence_sample_times": [sample["sample_time"] for sample in run["samples"]] + [expected],
        },
    }


def evaluate_alert(alert: dict[str, Any], *, as_of: str | None = None) -> dict[str, Any]:
    """对单条告警规则做一次判定，并写入审计记录。

    判定看到什么就记什么：每次检查都把当时的采样集合、连续段、未被补齐的
    缺口和等待结论写进 ``alert_checks``。事后要回答“当时为什么没触发”，
    回查审计记录即可，不依赖之后可能被补报改写的当前采样。
    """
    checked_at = as_of or db.now()
    alert_id = int(alert["id"])
    try:
        threshold = max(1, int(alert.get("consecutive_threshold") or 1))
    except (TypeError, ValueError):
        threshold = 1

    metric = resolve_metric(alert.get("monitor_metric"))
    if metric is None:
        summary = {
            "alert_id": alert_id,
            "rule_name": alert.get("rule_name"),
            "checked_at": checked_at,
            "triggered": False,
            "status": "监控指标不存在",
            "reason": f"未找到监控指标: {alert.get('monitor_metric')}",
            "threshold": threshold,
            "samples_considered": 0,
            "evaluated_samples": [],
            "runs": [],
            "evidence": [],
        }
        _persist_check(alert_id, summary, metric_id=None)
        return summary

    conditions = parse_conditions(alert.get("alert_condition"))
    samples, duplicates = load_samples(int(metric["id"]), as_of=checked_at)
    interval = resolve_interval(alert, samples)
    runs: list[dict[str, Any]] = []
    calibration_groups = sorted(group_by_calibration(samples).items())
    for index, (calibration, group_samples) in enumerate(calibration_groups):
        group_runs = build_runs(group_samples, conditions=conditions, interval=interval)
        for run in group_runs:
            run["metric_id"] = int(metric["id"])
        if group_runs and index > 0:
            previous_calibration = calibration_groups[index - 1][0]
            group_runs[0]["break_reason"] = (
                f"口径切换后重新计数（v{calibration}），不接 v{previous_calibration} 的采样"
            )
            group_runs[0]["calibration_switch_from"] = previous_calibration
        runs.extend(group_runs)
    runs.sort(key=lambda run: (run["start_time"], run["calibration_version"]))

    latest = samples[-1] if samples else None
    summary: dict[str, Any] = {
        "alert_id": alert_id,
        "rule_name": alert.get("rule_name"),
        "metric_id": int(metric["id"]),
        "metric_name": metric["metric_name"],
        "threshold": threshold,
        "alert_condition": alert.get("alert_condition"),
        "sample_interval_seconds": interval,
        "checked_at": checked_at,
        "samples_considered": len(samples),
        "duplicate_samples": duplicates,
        "evaluated_samples": [public_sample(sample) for sample in samples],
        "latest_sample": public_sample(latest) if latest else None,
        "calibration_version": latest["calibration_version"] if latest else None,
        "runs": [_run_summary(run, threshold) for run in runs],
        "triggered": False,
        "already_triggered": False,
        "log_id": None,
        "evidence": [],
        "window_start": None,
        "window_end": None,
        "pending": None,
        "waiting_runs": [],
        "if_backfilled": None,
    }

    if not samples:
        summary["status"] = "无采样"
        summary["reason"] = "该指标还没有任何采样记录"
        _persist_check(alert_id, summary, metric_id=int(metric["id"]))
        return summary

    if not conditions:
        summary["status"] = "告警条件无效"
        summary["reason"] = f"无法解析告警条件: {alert.get('alert_condition')}"
        _persist_check(alert_id, summary, metric_id=int(metric["id"]))
        return summary

    # 达到阈值的连续段：每段只触发一次
    triggered_runs = [run for run in runs if len(run["samples"]) >= threshold]
    triggered_logs: list[dict[str, Any]] = []
    newly_triggered = 0
    for run in triggered_runs:
        run["metric_id"] = int(metric["id"])
        log, created = _write_trigger(
            alert, run,
            threshold=threshold,
            interval=interval,
            conditions=conditions,
            checked_at=checked_at,
        )
        triggered_logs.append({**log, "created": created})
        if created:
            newly_triggered += 1

    if triggered_logs:
        summary["triggered"] = True
        summary["already_triggered"] = newly_triggered == 0
        summary["triggered_count"] = len(triggered_logs)
        summary["newly_triggered_count"] = newly_triggered
        primary = next((log for log in triggered_logs if log["created"]), triggered_logs[-1])
        summary["log_id"] = primary.get("id")
        summary["window_start"] = primary.get("window_start")
        summary["window_end"] = primary.get("window_end")
        summary["evidence"] = json.loads(primary.get("evidence_json") or "[]")
        summary["sample_time"] = primary.get("sample_time")
        summary["metric_value"] = primary.get("metric_value")
        summary["run_key"] = primary.get("run_key")
        summary["triggered_logs"] = [
            {
                "log_id": log.get("id"),
                "window_start": log.get("window_start"),
                "window_end": log.get("window_end"),
                "calibration_version": log.get("calibration_version"),
                "created": log["created"],
            }
            for log in triggered_logs
        ]

        summary["status"] = "已触发"
        if newly_triggered == 0:
            summary["reason"] = "该连续段此前已触发过告警，本次检查复用原记录，不重复计数"
        else:
            summary["reason"] = (
                f"连续 {threshold} 个采样（口径 v{primary.get('calibration_version')}）"
                f"均超限，异常区间 {summary['window_start']} ~ {summary['window_end']}"
            )

    # 未形成有效连续段的超限采样，给出可回查的原因、缺口与等待结论。
    # 已触发时也保留这些信息：口径切换后的另一组采样可能在继续等待，
    # 值班员需要同时看到“已触发”和“还在等什么”。
    sample_slots = {(sample["sample_time"], sample["calibration_version"]) for sample in samples}
    waiting: list[dict[str, Any]] = []
    for run in runs:
        pending = _pending_for_run(
            run,
            alert=alert,
            interval=interval,
            threshold=threshold,
            as_of=checked_at,
            sample_slots=sample_slots,
        )
        if pending:
            pending["calibration_version"] = run["calibration_version"]
            waiting.append(pending)

    # 缺口型且只差一个采样的段最值得值班员关注，排在最前
    waiting.sort(
        key=lambda item: (
            0 if item["remaining_samples"] <= 1 else 1,
            item["sample_time"],
        )
    )
    if waiting:
        primary = waiting[0]
        summary["pending"] = primary
        summary["waiting_runs"] = waiting
        summary["if_backfilled"] = primary["if_backfilled_exceeded"]

    if not triggered_logs:
        if waiting:
            primary = waiting[0]
            if primary["run"]["closed_by"] == "gap":
                summary["status"] = "采样缺口待补报"
            else:
                summary["status"] = "等待连续样本"
            summary["reason"] = primary["reason"]
        elif not condition_matches(latest["value"], conditions):
            summary["status"] = "未超限"
            summary["reason"] = (
                f"最新采样 {latest['sample_time']} 值 {latest['value']} 未满足告警条件"
            )
        else:
            summary["status"] = "未形成连续超限"
            summary["reason"] = "超限采样未在同一口径内形成连续段"

    # 说明最新采样所在连续段的口径情况
    trailing = runs[-1] if runs else None
    if trailing and trailing.get("break_reason") and trailing["start_time"] == latest["sample_time"]:
        summary["calibration_note"] = trailing["break_reason"]

    _persist_check(alert_id, summary, metric_id=int(metric["id"]))
    return summary


def _persist_check(alert_id: int, summary: dict[str, Any], *, metric_id: int | None) -> None:
    check_id = db.insert("alert_checks", {
        "alert_id": alert_id,
        "checked_at": summary.get("checked_at") or db.now(),
        "metric_id": metric_id,
        "calibration_version": summary.get("calibration_version"),
        "latest_sample_time": (summary.get("latest_sample") or {}).get("sample_time"),
        "status": summary.get("status"),
        "reason": summary.get("reason"),
        "sample_count": int(summary.get("samples_considered") or 0),
        "evaluated_samples": json.dumps(
            summary.get("evaluated_samples") or [], ensure_ascii=False
        ),
        "runs_json": json.dumps(summary.get("runs") or [], ensure_ascii=False),
        "pending_json": json.dumps(summary.get("pending") or {}, ensure_ascii=False),
        "waiting_json": json.dumps(summary.get("waiting_runs") or [], ensure_ascii=False),
        "pending_sample_time": (summary.get("pending") or {}).get("sample_time"),
        "grace_deadline": (summary.get("pending") or {}).get("grace_deadline"),
        "can_wait": 1 if (summary.get("pending") or {}).get("can_wait") else 0,
        "triggered_log_id": summary.get("log_id"),
    })
    summary["check_id"] = check_id
    if summary.get("log_id"):
        db.update("alert_log", {"check_id": check_id}, "id=?", [summary["log_id"]])
    summary["evaluated_sample_ids"] = [
        sample.get("id") for sample in summary.get("evaluated_samples") or []
    ]


# ---------------------------------------------------------------------------
# 告警记录读取
# ---------------------------------------------------------------------------

def _decode_log(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for field, default in (("evidence_json", []), ("sample_ids", []), ("rule_snapshot", {})):
        try:
            decoded[field.replace("_json", "")] = json.loads(row.get(field) or json.dumps(default))
        except (TypeError, json.JSONDecodeError):
            decoded[field.replace("_json", "")] = default
    return decoded


def alert_history(alert_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.fetchall(
        "SELECT * FROM alert_log WHERE alert_id=? ORDER BY triggered_at DESC, id DESC LIMIT ?",
        [alert_id, limit],
    )
    return [_decode_log(row) for row in rows]


def alert_checks(alert_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.fetchall(
        "SELECT * FROM alert_checks WHERE alert_id=? ORDER BY checked_at DESC, id DESC LIMIT ?",
        [alert_id, limit],
    )
    for row in rows:
        for field, default in (
            ("evaluated_samples", []),
            ("runs", []),
            ("pending", {}),
            ("waiting_runs", []),
        ):
            source = f"{field}_json" if f"{field}_json" in row else field
            try:
                row[field] = json.loads(row.get(source) or json.dumps(default))
            except (TypeError, json.JSONDecodeError):
                row[field] = default
    return rows


def get_alert_log(log_id: int) -> dict[str, Any] | None:
    row = db.fetchone("SELECT * FROM alert_log WHERE id=?", [log_id])
    return _decode_log(row) if row else None


def calibration_groups(alert_id: int) -> dict[str, Any]:
    """按指标口径分组展示采样与连续段，便于对照切换前后的证据。"""
    alert = db.fetchone("SELECT * FROM alerts WHERE id=?", [alert_id])
    if alert is None:
        return {"ok": False, "message": f"告警规则 {alert_id} 不存在"}

    metric = resolve_metric(alert.get("monitor_metric"))
    if metric is None:
        return {"ok": False, "message": f"未找到监控指标: {alert.get('monitor_metric')}"}

    conditions = parse_conditions(alert.get("alert_condition"))
    try:
        threshold = max(1, int(alert.get("consecutive_threshold") or 1))
    except (TypeError, ValueError):
        threshold = 1
    samples, duplicates = load_samples(int(metric["id"]))
    interval = resolve_interval(alert, samples)

    groups: list[dict[str, Any]] = []
    by_calibration: dict[int, list[dict[str, Any]]] = {}
    for sample in samples:
        by_calibration.setdefault(sample["calibration_version"], []).append(sample)

    for calibration in sorted(by_calibration):
        group_samples = by_calibration[calibration]
        runs = build_runs(group_samples, conditions=conditions, interval=interval)
        logs = []
        for run in runs:
            log = _find_log(int(alert["id"]), _run_key(int(alert["id"]), run))
            if log:
                logs.append(_decode_log(log))
        groups.append({
            "calibration_version": calibration,
            "sample_count": len(group_samples),
            "exceeded_count": sum(
                1 for sample in group_samples if condition_matches(sample["value"], conditions)
            ),
            "sample_start": group_samples[0]["sample_time"],
            "sample_end": group_samples[-1]["sample_time"],
            "samples": [public_sample(sample) for sample in group_samples],
            "runs": [_run_summary(run, threshold) for run in runs],
            "alert_logs": logs,
        })

    return {
        "ok": True,
        "alert_id": alert_id,
        "rule_name": alert["rule_name"],
        "metric_id": int(metric["id"]),
        "metric_name": metric["metric_name"],
        "threshold": threshold,
        "sample_interval_seconds": interval,
        "duplicate_samples": duplicates,
        "groups": groups,
        "note": "不同口径的采样各自独立成段，不参与同一段连续超限计数",
    }
