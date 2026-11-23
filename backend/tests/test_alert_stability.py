"""告警判定稳定性回归测试。

覆盖：
1. 连续三次超限按采样记录计算，同一采样反复检查只算一次；
2. 迟到补报：缺口未补齐前不跨缺口接续，可回查当时为何没触发、以及补报超限后命中哪一段；
3. 告警记录带判定采样时刻与异常区间；
4. 从告警发起归因，按告警样本区间关联采样，保存结论与超限证据；
5. 不同口径的采样不接成同一段连续超限；
6. 已确认的归因结论可回查当时证据，后续补报不能悄悄改写。

直接运行：``python -m tests.test_alert_stability``（在 backend 目录下）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="alert_stability_")
os.environ["CC10045_DB_DIR"] = _TMP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import db  # noqa: E402
from app.services.alert_policy import AlertPolicyService  # noqa: E402
from app.services.anomaly_analysis import AnomalyAnalysisService  # noqa: E402
from app.services.metric_store import MetricStoreService  # noqa: E402

aps = AlertPolicyService()
aas = AnomalyAnalysisService()
ms = MetricStoreService()

METRIC = "订单总量-稳定性测试"
ALERT = "订单超限稳定性测试"


def at(minute: int, second: int = 0) -> str:
    return f"2026-11-23T10:{minute:02d}:{second:02d}"


def setup() -> tuple[int, int]:
    metric_id = db.insert("metrics", {
        "metric_name": METRIC,
        "metric_type": "counter",
        "source_module": "event_tracker",
        "calibration_version": 1,
        "unit": "单",
    })
    alert_id = db.insert("alerts", {
        "rule_name": ALERT,
        "monitor_metric": METRIC,
        "alert_condition": "> 100",
        "notify_channel": "sms",
        "consecutive_threshold": 3,
        "sample_interval_seconds": 60,
        "backfill_grace_seconds": 300,
        "status": "已生效",
    })
    # 有历史基线，归因才能给出 z-score
    for minute in range(0, 8):
        ms.record_sample(metric_id, 80 + minute, sample_time=at(minute), source="history")
    return metric_id, alert_id


def test_missing_sample_cannot_join() -> None:
    metric_id, alert_id = setup()
    # 清掉历史基线中的 10:00~10:03，改用超限采样重放场景
    db.delete("metric_values", "metric_id=? AND sample_time >= ?", [metric_id, at(0)])

    ms.record_sample(metric_id, 150, sample_time=at(0))
    ms.record_sample(metric_id, 160, sample_time=at(1))
    ms.record_sample(metric_id, 170, sample_time=at(3))

    first = aps.check_single(alert_id, as_of=at(3, 30))["check_result"]
    assert first["triggered"] is False, "10:02 缺失时不能触发"
    assert first["status"] == "采样缺口待补报"
    assert first["pending"]["sample_time"] == at(2)
    assert first["pending"]["can_wait"] is True, "补报窗口内应说明还能继续等"
    assert first["pending"]["grace_deadline"] == at(7)
    assert first["if_backfilled"]["would_trigger"] is True, "并应说明补报超限后会命中哪一段"
    assert first["if_backfilled"]["window_start"] == at(0)
    assert first["if_backfilled"]["window_end"] == at(2)

    # 缺口的后一段只有 10:03 一个采样，10:00/10:01 与 10:03 不能拼成三段
    runs = {(run["start_time"], run["sample_count"]) for run in first["runs"]}
    assert (at(0), 2) in runs and (at(3), 1) in runs, runs

    # 同一采样反复检查只算一次
    second = aps.check_single(alert_id, as_of=at(3, 31))["check_result"]
    third = aps.check_single(alert_id, as_of=at(3, 32))["check_result"]
    assert second["status"] == third["status"] == "采样缺口待补报"
    assert db.count("alert_log", "alert_id=?", [alert_id]) == 0, "三次检查不能凑成三次超限"

    # 审计记录可回查“为什么当时没有触发”
    checks = aps.alert_checks(alert_id)
    assert len(checks) == 3
    for check in checks:
        assert check["status"] == "采样缺口待补报"
        assert [s["sample_time"] for s in check["evaluated_samples"]] == [at(0), at(1), at(3)]
        assert check["pending"]["sample_time"] == at(2)
        assert check["can_wait"] == 1
    # 10:00 同时被检查了三次，但采样口径里只有一条
    assert all(len({s["id"] for s in check["evaluated_samples"]}) == 3 for check in checks)

    # 迟到补报为超限，补齐后触发；异常区间为 10:00~10:02
    ms.backfill_sample(metric_id, 155, at(2))
    triggered = aps.check_single(alert_id, as_of=at(4, 10))["check_result"]
    assert triggered["triggered"] is True
    assert triggered["window_start"] == at(0)
    assert triggered["window_end"] == at(2)
    assert [sample["sample_time"] for sample in triggered["evidence"]] == [at(0), at(1), at(2)]
    assert [sample["value"] for sample in triggered["evidence"]] == [150.0, 160.0, 155.0]

    logs = aps.alert_history(alert_id)
    assert len(logs) == 1
    log = logs[0]
    assert log["sample_time"] == at(2), "告警记录要带判定用到的采样时刻"
    assert (log["window_start"], log["window_end"]) == (at(0), at(2))
    assert log["consecutive_count"] == 3
    assert {row["sample_time"] for row in db.fetchall(
        "SELECT sample_time FROM alert_evidence WHERE alert_log_id=?", [log["id"]]
    )} == {at(0), at(1), at(2)}

    # 重复检查不再新增告警
    again = aps.check_single(alert_id, as_of=at(4, 20))["check_result"]
    assert again["triggered"] is True and again["already_triggered"] is True
    assert db.count("alert_log", "alert_id=?", [alert_id]) == 1

    # 归因按告警记录的样本区间关联采样
    created = aas.create_from_alert(log["id"], attribution_dimensions='["dimension_key"]')
    assert created["ok"] is True
    entry_id = created["entry"]["id"]
    analysis = aas.run_analysis(entry_id)
    assert analysis["ok"] is True
    assert (analysis["sample_window_start"], analysis["sample_window_end"]) == (at(0), at(2))
    assert analysis["calibration_version"] == 1
    assert [sample["id"] for sample in analysis["evidence"]] == log["sample_ids"]

    evidence = aas.evidence(entry_id)
    assert evidence["evidence_changed"] is False
    assert [s["sample_time"] for s in evidence["frozen_evidence"]] == [at(0), at(1), at(2)]
    assert evidence["alert_evidence"] == log["evidence"]

    # 后来补报改写同一采样：结论不能被悄悄改写
    ms.backfill_sample(metric_id, 999, at(1))
    rerun = aas.run_analysis(entry_id)
    assert rerun["ok"] is False and rerun.get("conflict") is True
    stored = aas.get_entry(entry_id)
    assert stored["conclusion"] == analysis["conclusion"], "原结论必须保留"
    evidence_after = aas.evidence(entry_id)
    assert evidence_after["evidence_changed"] is True
    assert evidence_after["changed_samples"][0]["sample_time"] == at(1)
    assert evidence_after["frozen_evidence"][1]["value"] == 160.0

    # 显式重算才允许按新采样刷新结论
    recomputed = aas.run_analysis(entry_id, recompute=True)
    assert recomputed["ok"] is True
    assert [s["value"] for s in recomputed["evidence"]] == [150.0, 999.0, 155.0]


def test_calibration_switch_not_joined() -> None:
    db.delete("metrics", "metric_name=?", [METRIC])
    db.delete("alerts", "rule_name=?", [ALERT])
    metric_id, alert_id = setup()
    db.delete("metric_values", "metric_id=? AND sample_time >= ?", [metric_id, at(0)])

    # 口径 v1：10:00、10:01、10:02 超限
    for minute, value in ((0, 150), (1, 160), (2, 155)):
        ms.record_sample(metric_id, value, sample_time=at(minute), calibration_version=1)
    # 口径 v2：10:03、10:04 超限（只有两个）
    for minute, value in ((3, 300), (4, 310)):
        ms.record_sample(metric_id, value, sample_time=at(minute), calibration_version=2)

    check = aps.check_single(alert_id, as_of=at(5, 10))["check_result"]
    logs = aps.alert_history(alert_id)
    assert len(logs) == 1, "v1 的三段应触发一次"
    assert logs[0]["calibration_version"] == 1
    assert (logs[0]["window_start"], logs[0]["window_end"]) == (at(0), at(2))

    # v2 不能借 v1 的采样凑成三段
    v2_runs = [run for run in check["runs"] if run["calibration_version"] == 2]
    assert len(v2_runs) == 1 and v2_runs[0]["sample_count"] == 2
    assert v2_runs[0]["break_reason"].startswith("口径切换后重新计数")

    groups = aps.calibration_groups(alert_id)
    assert [g["calibration_version"] for g in groups["groups"]] == [1, 2]
    v1_group, v2_group = groups["groups"]
    assert v1_group["sample_count"] == 3 and len(v1_group["alert_logs"]) == 1
    assert v2_group["sample_count"] == 2 and v2_group["alert_logs"] == []

    # 口径切换后的第二组采样补齐到三个，独立触发，不复用 v1 证据
    ms.record_sample(metric_id, 320, sample_time=at(5), calibration_version=2)
    triggered = aps.check_single(alert_id, as_of=at(6, 10))["check_result"]
    assert triggered["triggered"] is True
    logs = aps.alert_history(alert_id)
    assert len(logs) == 2
    v2_log = next(log for log in logs if log["calibration_version"] == 2)
    assert (v2_log["window_start"], v2_log["window_end"]) == (at(3), at(5))
    assert [sample["id"] for sample in v2_log["evidence"]] == [
        sample["id"] for sample in triggered["evidence"]
    ]
    assert all(sample["calibration_version"] == 2 for sample in v2_log["evidence"])

    # 两组采样分别归因，区间与证据互不混用
    v1_log = next(log for log in logs if log["calibration_version"] == 1)
    for log, expected_window in ((v1_log, (at(0), at(2))), (v2_log, (at(3), at(5)))):
        entry = aas.create_from_alert(log["id"], attribution_dimensions='["dimension_key"]')
        result = aas.run_analysis(entry["entry"]["id"])
        assert result["ok"] is True
        assert (result["sample_window_start"], result["sample_window_end"]) == expected_window
        assert all(
            sample["calibration_version"] == log["calibration_version"]
            for sample in result["evidence"]
        )
        assert [sample["id"] for sample in result["evidence"]] == log["sample_ids"]

    groups = aps.calibration_groups(alert_id)
    for group in groups["groups"]:
        for log in group["alert_logs"]:
            assert {s["calibration_version"] for s in log["evidence"]} == {group["calibration_version"]}


def test_same_sample_check_is_idempotent_and_recorded_once() -> None:
    db.delete("metrics", "metric_name=?", [METRIC])
    db.delete("alerts", "rule_name=?", [ALERT])
    metric_id, alert_id = setup()
    db.delete("metric_values", "metric_id=? AND sample_time >= ?", [metric_id, at(0)])

    ms.record_sample(metric_id, 150, sample_time=at(0))
    # 同采样同值重复登记：幂等，不新增采样
    a = ms.record_sample(metric_id, 150, sample_time=at(0))
    assert a["changed"] is False
    assert db.count("metric_values", "metric_id=? AND sample_time=?", [metric_id, at(0)]) == 1

    # 同一时刻的不同写法也算同一采样，不会因为格式差异变成两个采样
    a2 = ms.record_sample(metric_id, 150, sample_time="2026-11-23T10:00:00+00:00")
    assert a2["changed"] is False
    assert db.count("metric_values", "metric_id=?", [metric_id]) == 1

    # 同采样重复登记不同值：覆盖同一条，而不是新增一条
    b = ms.record_sample(metric_id, 151, sample_time=at(0))
    assert b["changed"] is True and b.get("replaced") is True
    assert db.count("metric_values", "metric_id=? AND sample_time=?", [metric_id, at(0)]) == 1
    assert db.fetchone(
        "SELECT snapshot_value FROM metric_values WHERE metric_id=? AND sample_time=?",
        [metric_id, at(0)],
    )["snapshot_value"] == 151.0

    # 三次检查只产生一条审计采样集合，不产生告警
    for offset in (10, 20, 30):
        aps.check_single(alert_id, as_of=at(0, offset))
    assert db.count("alert_log", "alert_id=?", [alert_id]) == 0
    checks = aps.alert_checks(alert_id)
    assert len(checks) == 3
    assert all(check["sample_count"] == 1 for check in checks)

    # 三次不同的采样才构成三段
    ms.record_sample(metric_id, 160, sample_time=at(1))
    ms.record_sample(metric_id, 170, sample_time=at(2))
    result = aps.check_single(alert_id, as_of=at(3, 5))["check_result"]
    assert result["triggered"] is True
    assert result["window_start"] == at(0) and result["window_end"] == at(2)
    assert db.count("alert_log", "alert_id=?", [alert_id]) == 1


def test_check_audit_survives_later_backfill() -> None:
    """回查“当时为什么没触发”靠审计快照，不靠当前采样。"""
    db.delete("metrics", "metric_name=?", [METRIC])
    db.delete("alerts", "rule_name=?", [ALERT])
    metric_id, alert_id = setup()
    db.delete("metric_values", "metric_id=? AND sample_time >= ?", [metric_id, at(0)])

    ms.record_sample(metric_id, 150, sample_time=at(0))
    ms.record_sample(metric_id, 160, sample_time=at(1))
    ms.record_sample(metric_id, 170, sample_time=at(3))
    aps.check_single(alert_id, as_of=at(3, 30))
    first_check = aps.alert_checks(alert_id)[0]
    assert first_check["sample_count"] == 3
    assert first_check["pending"]["sample_time"] == at(2)
    snapshot_ids = {sample["id"] for sample in first_check["evaluated_samples"]}

    # 10:02 补报后，库里有了新的采样；当时那次检查的快照仍然只有三条，
    # 且缺口与等待结论不会被后续数据抹掉。
    ms.backfill_sample(metric_id, 155, at(2))
    aps.check_single(alert_id, as_of=at(4, 10))
    old_check = next(item for item in aps.alert_checks(alert_id) if item["id"] == first_check["id"])
    assert {sample["id"] for sample in old_check["evaluated_samples"]} == snapshot_ids
    assert old_check["sample_count"] == 3
    assert old_check["pending"]["sample_time"] == at(2)
    assert old_check["status"] == "采样缺口待补报"
    assert old_check["triggered_log_id"] is None

    # 触发发生在补报之后的那次检查里，记录里看得到是哪次判定写的
    logs = aps.alert_history(alert_id)
    assert len(logs) == 1
    assert logs[0]["check_id"] is not None
    assert logs[0]["check_id"] != first_check["id"]


def main() -> None:
    tests = [
        test_missing_sample_cannot_join,
        test_calibration_switch_not_joined,
        test_same_sample_check_is_idempotent_and_recorded_once,
        test_check_audit_survives_later_backfill,
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print("all alert stability tests passed")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
