"""异常归因领域服务：从告警出发，按告警记录中的样本区间关联实际采样做归因。

真实数据流：值班员选择一条告警记录 → 用该告警的 ``window_start`` /
``window_end`` / ``calibration_version`` / ``sample_ids`` 锁定采样 →
pipeline.detect_anomaly 在锁定的采样上做异常检测与维度归因 → 结论、
样本区间、超限证据一起落库。

已完成的归因会保存证据快照与哈希：后来补报改写同一采样时，再次运行
不会悄悄改写原结论，而是返回冲突并要求显式重算。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.alerting import load_samples, get_alert_log
from app.database import db
from app.pipeline import detect_anomaly
from app.services.base import BaseModuleService


def _evidence_hash(samples: list[dict[str, Any]]) -> str:
    payload = [
        {"id": sample["id"], "sample_time": sample["sample_time"],
         "value": sample["value"], "calibration_version": sample["calibration_version"]}
        for sample in samples
    ]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AnomalyAnalysisService(BaseModuleService):
    MODULE = "anomaly_analysis"
    ENTITY = "归因分析器"
    OPERATION_TERM = "分析"
    LIST_FIELDS = ["分析对象", "异常时间点", "归因维度", "贡献度排序", "分析结论"]
    REQUIRED_FIELDS = ["分析对象", "异常时间点", "归因维度"]
    STATUS_ORDER = ["待分析", "分析中", "已完成", "分析失败"]
    ACTION_RULES = {"启动分析": "已完成", "取消分析": "分析失败"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 从告警发起归因
    # ------------------------------------------------------------------

    def create_from_alert(
        self,
        alert_log_id: int,
        *,
        attribution_dimensions: str | None = None,
    ) -> dict[str, Any]:
        """按一条告警记录登记归因任务，带上该次判定用的样本区间与证据。"""
        log = get_alert_log(alert_log_id)
        if log is None:
            return {"ok": False, "message": f"告警记录 {alert_log_id} 不存在"}

        alert = db.fetchone("SELECT * FROM alerts WHERE id=?", [log["alert_id"]])
        if alert is None:
            return {"ok": False, "message": f"告警规则 {log['alert_id']} 不存在"}

        dims = attribution_dimensions or json.dumps(["dimension_key"], ensure_ascii=False)
        evidence_samples = log.get("evidence") or []
        evidence_ids = [sample.get("id") for sample in evidence_samples if sample.get("id")]
        entry_id = db.insert("anomaly_analyses", {
            "target_name": alert["monitor_metric"],
            "anomaly_time": log.get("window_end") or log.get("sample_time") or log["triggered_at"],
            "attribution_dimensions": dims,
            "status": "待分析",
            "alert_log_id": alert_log_id,
            "metric_id": log.get("metric_id"),
            "calibration_version": log.get("calibration_version"),
            "sample_window_start": log.get("window_start"),
            "sample_window_end": log.get("window_end"),
            "evidence_sample_ids": json.dumps(evidence_ids),
            "evidence_json": json.dumps(evidence_samples, ensure_ascii=False),
            "evidence_hash": _evidence_hash(evidence_samples),
        })
        entry = db.fetchone("SELECT * FROM anomaly_analyses WHERE id=?", [entry_id])
        return {
            "ok": True,
            "message": "已按告警记录登记归因任务",
            "entry": entry,
            "sample_window_start": log.get("window_start"),
            "sample_window_end": log.get("window_end"),
            "calibration_version": log.get("calibration_version"),
        }

    # ------------------------------------------------------------------
    # 执行归因
    # ------------------------------------------------------------------

    def run_analysis(self, entry_id: int, *, recompute: bool = False) -> dict[str, Any]:
        """执行异常归因分析。先加载记录，再调用管线。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return {"ok": False, "message": f"分析记录 {entry_id} 不存在"}

        if entry.get("status") == "已完成" and entry.get("evidence_hash") and not recompute:
            drift = self._evidence_drift(entry)
            if drift["changed"]:
                return {
                    "ok": False,
                    "conflict": True,
                    "message": (
                        "归因结论所依据的采样已被后续补报改写；"
                        "原结论保留不变，如需按新采样重算请显式选择重算"
                    ),
                    "original_conclusion": entry.get("conclusion"),
                    "original_window": [
                        entry.get("sample_window_start"), entry.get("sample_window_end"),
                    ],
                    "changed_samples": drift["changed_samples"],
                }

        db.update("anomaly_analyses", {"status": "分析中"}, "id=?", [entry_id])

        evidence_ids: list[int] = []
        try:
            evidence_ids = json.loads(entry.get("evidence_sample_ids") or "[]")
        except (TypeError, json.JSONDecodeError):
            evidence_ids = []

        analysis_data = {
            "分析对象": entry.get("target_name"),
            "异常时间点": entry.get("anomaly_time"),
            "归因维度": entry.get("attribution_dimensions"),
            "metric_id": entry.get("metric_id"),
            "calibration_version": entry.get("calibration_version"),
            "sample_window_start": entry.get("sample_window_start"),
            "sample_window_end": entry.get("sample_window_end"),
            "evidence_sample_ids": evidence_ids,
        }
        result = detect_anomaly(analysis_data)

        if result.get("status") == "分析失败":
            db.update("anomaly_analyses", {"status": "分析失败"}, "id=?", [entry_id])
            return {"ok": False, "message": result.get("reason", "分析失败")}

        used_samples = result.get("evidence_samples", [])
        conclusion = result.get("conclusion", "分析完成")
        contributions = result.get("contributions", {})
        ranking_json = json.dumps(
            sorted(contributions.items(), key=lambda x: -x[1])[:5],
            ensure_ascii=False,
        )
        window_start = result.get("sample_window_start") or entry.get("sample_window_start")
        window_end = result.get("sample_window_end") or entry.get("sample_window_end")
        calibration = result.get("calibration_version") or entry.get("calibration_version")
        sample_ids = [sample["id"] for sample in used_samples]
        snapshot = [{
            "id": sample["id"],
            "sample_time": sample["sample_time"],
            "value": sample["value"],
            "calibration_version": sample["calibration_version"],
            "source": sample["source"],
        } for sample in used_samples]

        db.update("anomaly_analyses", {
            "status": "已完成",
            "contribution_ranking": ranking_json,
            "conclusion": conclusion,
            "metric_id": result.get("metric_id"),
            "calibration_version": calibration,
            "sample_window_start": window_start,
            "sample_window_end": window_end,
            "evidence_sample_ids": json.dumps(sample_ids),
            "evidence_json": json.dumps(snapshot, ensure_ascii=False),
            "evidence_hash": _evidence_hash(snapshot),
            "analysis_inputs": json.dumps({
                "alert_log_id": entry.get("alert_log_id"),
                "z_score": result.get("z_score"),
                "mean": result.get("mean"),
                "std": result.get("std"),
                "window_value": result.get("window_value"),
                "baseline_sample_count": result.get("baseline_sample_count"),
            }, ensure_ascii=False),
        }, "id=?", [entry_id])

        return {
            "ok": True,
            "target": analysis_data["分析对象"],
            "alert_log_id": entry.get("alert_log_id"),
            "is_anomaly": result.get("is_anomaly", False),
            "z_score": result.get("z_score", 0),
            "conclusion": conclusion,
            "contributions": contributions,
            "sample_window_start": window_start,
            "sample_window_end": window_end,
            "calibration_version": calibration,
            "evidence": snapshot,
        }

    # ------------------------------------------------------------------
    # 证据回查
    # ------------------------------------------------------------------

    def evidence(self, entry_id: int) -> dict[str, Any]:
        """回查归因结论当时依据的采样证据。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return {"ok": False, "message": f"分析记录 {entry_id} 不存在"}

        try:
            frozen = json.loads(entry.get("evidence_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            frozen = []

        current: list[dict[str, Any]] = []
        if entry.get("metric_id"):
            as_of = entry.get("sample_window_end") or entry.get("anomaly_time")
            samples, _ = load_samples(
                int(entry["metric_id"]),
                as_of=as_of,
                calibration_version=entry.get("calibration_version"),
            )
            window_start = entry.get("sample_window_start")
            if window_start:
                samples = [s for s in samples if s["sample_time"] >= window_start]
            if entry.get("sample_window_end"):
                samples = [s for s in samples if s["sample_time"] <= entry["sample_window_end"]]
            current = [{
                "id": s["id"], "sample_time": s["sample_time"], "value": s["value"],
                "calibration_version": s["calibration_version"], "source": s["source"],
            } for s in samples]

        drift = self._evidence_drift(entry)
        alert_log = get_alert_log(int(entry["alert_log_id"])) if entry.get("alert_log_id") else None

        return {
            "ok": True,
            "entry_id": entry_id,
            "status": entry.get("status"),
            "conclusion": entry.get("conclusion"),
            "contribution_ranking": entry.get("contribution_ranking"),
            "alert_log_id": entry.get("alert_log_id"),
            "alert_evidence": (alert_log or {}).get("evidence", []),
            "sample_window_start": entry.get("sample_window_start"),
            "sample_window_end": entry.get("sample_window_end"),
            "calibration_version": entry.get("calibration_version"),
            "frozen_evidence": frozen,
            "evidence_hash": entry.get("evidence_hash"),
            "current_evidence": current,
            "evidence_changed": drift["changed"],
            "changed_samples": drift["changed_samples"],
        }

    def _evidence_drift(self, entry: dict[str, Any]) -> dict[str, Any]:
        """比较冻结证据与当前库内同一采样的值。"""
        try:
            frozen = json.loads(entry.get("evidence_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            frozen = []
        changed: list[dict[str, Any]] = []
        for item in frozen:
            row = db.fetchone("SELECT * FROM metric_values WHERE id=?", [item.get("id")])
            if row is None:
                changed.append({"sample_id": item.get("id"), "issue": "采样已被删除",
                                "frozen_value": item.get("value")})
                continue
            current_value = float(row.get("snapshot_value") or 0.0)
            if abs(current_value - float(item.get("value") or 0.0)) > 1e-9:
                changed.append({
                    "sample_id": item.get("id"),
                    "sample_time": item.get("sample_time"),
                    "frozen_value": item.get("value"),
                    "current_value": current_value,
                })
        return {"changed": bool(changed), "changed_samples": changed}

    # ------------------------------------------------------------------
    # 列表与统计
    # ------------------------------------------------------------------

    def recent_anomalies(self, limit: int = 20) -> list[dict[str, Any]]:
        """获取最近的分析结果。"""
        return db.fetchall(
            "SELECT * FROM anomaly_analyses WHERE status='已完成' ORDER BY id DESC LIMIT ?",
            [limit],
        )

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["total_analyses"] = db.count("anomaly_analyses")
        snapshot["completed_analyses"] = db.count("anomaly_analyses", "status='已完成'")
        snapshot["linked_analyses"] = db.count("anomaly_analyses", "alert_log_id IS NOT NULL")
        return snapshot
