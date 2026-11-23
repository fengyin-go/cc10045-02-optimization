"""指标存储领域服务：管理指标定义、执行快照计算并与基准对比。

真实数据流：接收指标定义 → 调用 pipeline.compute_metric_snapshot 计算当前值
→ 写入 metric_values 表 → 对比上一期基准值和历史序列 → 检测 z-score 异常
→ 下游告警策略消费者。
"""
from __future__ import annotations

from typing import Any

from app.database import db
from app.pipeline import compute_metric_snapshot
from app.services.base import BaseModuleService


class MetricStoreService(BaseModuleService):
    MODULE = "metric_store"
    ENTITY = "指标快照"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["指标名称", "快照值", "快照时间", "口径版本", "对比基准"]
    REQUIRED_FIELDS = ["指标名称", "快照值", "快照时间"]
    STATUS_ORDER = ["已保存", "对比成功", "有异常"]
    ACTION_RULES = {"保存快照": "已保存", "标记异常": "有异常"}
    NEGATIVE_ACTIONS = []

    def list_metrics(self, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        """列出所有指标定义。"""
        total = db.count("metrics")
        rows = db.fetchall(
            "SELECT * FROM metrics ORDER BY id DESC LIMIT ? OFFSET ?",
            [size, (page - 1) * size],
        )
        return rows, total

    def compute_snapshot(self, metric_id: int) -> dict[str, Any]:
        """对指定指标执行一次快照计算并存储。"""
        metric = db.fetchone("SELECT * FROM metrics WHERE id=?", [metric_id])
        if metric is None:
            return {"ok": False, "message": f"指标 {metric_id} 不存在"}
        result = compute_metric_snapshot(dict(metric))
        if result is None:
            return {"ok": False, "message": f"指标 {metric['metric_name']} 计算失败"}
        return {"ok": True, "result": result}

    def record_sample(
        self,
        metric_id: int,
        value: float,
        *,
        sample_time: str | None = None,
        calibration_version: int | None = None,
        source: str = "live",
    ) -> dict[str, Any]:
        """登记一个指标采样。

        同一 ``(指标, 采样时刻, 口径版本)`` 视为同一采样：
        * 值未变：幂等，不新增记录；
        * 值变化且是迟到补报：覆盖该采样值并标记 ``source='backfill'``，
          只保留一条，避免同一采样被重复计数。
        """
        metric = db.fetchone("SELECT * FROM metrics WHERE id=?", [metric_id])
        if metric is None:
            return {"ok": False, "message": f"指标 {metric_id} 不存在"}

        from app.alerting import format_time, parse_time  # 延迟导入避免循环

        ingested_at = db.now()
        time_text = str(sample_time or ingested_at)
        parsed = parse_time(time_text)
        if parsed is None:
            return {"ok": False, "message": f"采样时刻格式无法解析: {sample_time}"}
        # 统一成 ISO 秒级格式，避免同一时刻的不同写法被当成两个采样
        time_text = format_time(parsed)
        calibration = int(calibration_version or metric.get("calibration_version") or 1)

        existing = db.fetchone(
            "SELECT * FROM metric_values WHERE metric_id=? AND COALESCE(sample_time, snapshot_time)=? "
            "AND calibration_version=? ORDER BY id DESC LIMIT 1",
            [metric_id, time_text, calibration],
        )
        if existing is not None:
            if abs(float(existing["snapshot_value"]) - float(value)) < 1e-9:
                return {
                    "ok": True,
                    "changed": False,
                    "sample_id": existing["id"],
                    "message": "同一采样时刻的值未变化，按幂等处理",
                }
            db.update("metric_values", {
                "snapshot_value": round(float(value), 4),
                "ingested_at": ingested_at,
                "source": source,
                "snapshot_time": time_text,
                "sample_time": time_text,
            }, "id=?", [existing["id"]])
            return {
                "ok": True,
                "changed": True,
                "replaced": True,
                "sample_id": existing["id"],
                "message": f"采样 {time_text} 已由补报覆盖（口径 v{calibration}）",
            }

        sample_id = db.insert("metric_values", {
            "metric_id": metric_id,
            "snapshot_value": round(float(value), 4),
            "snapshot_time": time_text,
            "sample_time": time_text,
            "ingested_at": ingested_at,
            "source": source,
            "calibration_version": calibration,
        })
        return {
            "ok": True,
            "changed": True,
            "created": True,
            "sample_id": sample_id,
            "message": f"已登记采样 {time_text}（口径 v{calibration}）",
        }

    def backfill_sample(
        self,
        metric_id: int,
        value: float,
        sample_time: str,
        calibration_version: int | None = None,
    ) -> dict[str, Any]:
        """迟到补报入口：显式标记 source='backfill'。"""
        return self.record_sample(
            metric_id, value,
            sample_time=sample_time,
            calibration_version=calibration_version,
            source="backfill",
        )

    def compute_all(self) -> dict[str, Any]:
        """对所有指标执行快照计算。"""
        metrics = db.fetchall("SELECT * FROM metrics")
        results = []
        for m in metrics:
            r = self.compute_snapshot(m["id"])
            results.append({
                "metric_name": m["metric_name"],
                "ok": r["ok"],
                "value": r["result"]["snapshot_value"] if r["ok"] else None,
                "status": r["result"]["comparison_status"] if r["ok"] else "failed",
            })
        return {"processed": len(metrics), "results": results}

    def metric_trend(self, metric_id: int, limit: int = 30) -> dict[str, Any]:
        """获取指标的历史趋势数据。"""
        metric = db.fetchone("SELECT * FROM metrics WHERE id=?", [metric_id])
        if metric is None:
            return {"trend": "no_data", "points": []}

        rows = db.fetchall(
            "SELECT * FROM metric_values WHERE metric_id=? "
            "ORDER BY COALESCE(sample_time, snapshot_time) DESC, id DESC LIMIT ?",
            [metric_id, limit],
        )
        values = [r["snapshot_value"] for r in rows]
        if not values:
            return {"trend": "no_data", "points": [], "metric_name": metric["metric_name"]}

        import statistics
        mean_v = statistics.mean(values)

        trend = "stable"
        if len(values) >= 2:
            if values[0] > mean_v * 1.1:
                trend = "upward"
            elif values[0] < mean_v * 0.9:
                trend = "downward"

        return {
            "metric_name": metric["metric_name"],
            "metric_type": metric["metric_type"],
            "unit": metric["unit"],
            "trend": trend,
            "latest": values[0],
            "mean": round(mean_v, 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "points": [{"time": r.get("sample_time") or r["snapshot_time"],
                        "value": r["snapshot_value"],
                        "source": r.get("source") or "live",
                        "status": r["comparison_status"]} for r in rows],
        }

    def compare_calibrations(self, metric_id: int, v1: int, v2: int) -> dict[str, Any]:
        """对比同一指标在两个口径版本下的差异。"""
        snapshots_v1 = db.fetchall(
            "SELECT * FROM metric_values WHERE metric_id=? AND calibration_version=? "
            "ORDER BY COALESCE(sample_time, snapshot_time) DESC LIMIT 10",
            [metric_id, v1],
        )
        snapshots_v2 = db.fetchall(
            "SELECT * FROM metric_values WHERE metric_id=? AND calibration_version=? "
            "ORDER BY COALESCE(sample_time, snapshot_time) DESC LIMIT 10",
            [metric_id, v2],
        )

        import statistics
        mean_v1 = statistics.mean([s["snapshot_value"] for s in snapshots_v1]) if snapshots_v1 else 0
        mean_v2 = statistics.mean([s["snapshot_value"] for s in snapshots_v2]) if snapshots_v2 else 0
        diff_pct = round((mean_v1 - mean_v2) / max(abs(mean_v2), 0.0001) * 100, 2)

        return {
            "metric_id": metric_id,
            "version_a": v1,
            "version_b": v2,
            "mean_a": round(mean_v1, 4),
            "mean_b": round(mean_v2, 4),
            "diff_pct": diff_pct,
            "significant": abs(diff_pct) > 5,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["total_metrics"] = db.count("metrics")
        snapshot["total_snapshots"] = db.count("metric_values")
        abnormal = db.count("metric_values", "comparison_status='有异常'")
        snapshot["abnormal_snapshots"] = abnormal
        return snapshot