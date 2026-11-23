"""分析告警策略领域服务：定义指标阈值告警，并驱动稳定的连续超限判定。

判定规则（详见 app.alerting）：
* 连续次数按指标采样记录计算，同一采样反复检查只算一次；
* 迟到补报按采样时刻归位，缺口未补齐前不能跨缺口接续；
* 不同口径（calibration_version）的采样各自成段，不拼成一段连续超限；
* 每次判定留审计记录，触发时告警记录带判定采样时刻与异常区间。
"""
from __future__ import annotations

from typing import Any

from app.alerting import (
    alert_checks,
    alert_history,
    calibration_groups,
    evaluate_alert,
    get_alert_log,
)
from app.database import db
from app.services.base import BaseModuleService


class AlertPolicyService(BaseModuleService):
    MODULE = "alert_policy"
    ENTITY = "告警规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则名称", "监控指标", "告警条件", "通知方式", "连续触发次数"]
    REQUIRED_FIELDS = ["规则名称", "监控指标", "告警条件"]
    STATUS_ORDER = ["待生效", "已生效", "已关闭"]
    ACTION_RULES = {"启用告警": "已生效", "关闭告警": "已关闭"}
    NEGATIVE_ACTIONS = ["关闭告警"]

    def _active_alerts(self) -> list[dict[str, Any]]:
        return db.fetchall("SELECT * FROM alerts WHERE status='已生效' ORDER BY id")

    def check_all(self, *, as_of: str | None = None) -> dict[str, Any]:
        """检查所有已生效告警规则。"""
        alerts = self._active_alerts()
        results = []
        triggered_count = 0

        for alert in alerts:
            result = evaluate_alert(dict(alert), as_of=as_of)
            results.append({
                "alert_id": alert["id"],
                "rule_name": alert["rule_name"],
                "triggered": bool(result.get("triggered")),
                "status": result.get("status"),
                "message": result.get("reason", ""),
                "check_id": result.get("check_id"),
                "log_id": result.get("log_id"),
            })
            if result.get("triggered") and not result.get("already_triggered"):
                triggered_count += 1

        return {
            "checked": len(alerts),
            "triggered": triggered_count,
            "as_of": as_of or db.now(),
            "details": results,
        }

    def check_single(self, alert_id: int, *, as_of: str | None = None) -> dict[str, Any]:
        """检查单个告警规则，返回完整判定依据（采样、连续段、缺口、是否可等）。"""
        alert = db.fetchone("SELECT * FROM alerts WHERE id=?", [alert_id])
        if alert is None:
            return {"ok": False, "message": f"告警规则 {alert_id} 不存在"}

        result = evaluate_alert(dict(alert), as_of=as_of)
        return {
            "ok": True,
            "alert_id": alert_id,
            "rule_name": alert["rule_name"],
            "check_result": result,
        }

    def alert_history(self, alert_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """告警触发记录，带判定采样时刻、异常区间与超限证据。"""
        return alert_history(alert_id, limit)

    def alert_checks(self, alert_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """告警判定审计：每次检查用了哪些采样、为什么没触发、还能不能等。"""
        return alert_checks(alert_id, limit)

    def calibration_groups(self, alert_id: int) -> dict[str, Any]:
        """按口径分组回看采样与连续段，用于对照口径切换前后的告警证据。"""
        return calibration_groups(alert_id)

    def get_log(self, log_id: int) -> dict[str, Any] | None:
        return get_alert_log(log_id)

    def resolve_alert(self, log_id: int) -> dict[str, Any]:
        """标记告警为已解决。"""
        updated = db.update("alert_log", {"resolved": 1}, "id=?", [log_id])
        if not updated:
            return {"ok": False, "message": f"告警记录 {log_id} 不存在"}
        return {"ok": True, "message": "告警已标记为已解决"}

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["total_rules"] = db.count("alerts")
        snapshot["active_rules"] = db.count("alerts", "status='已生效'")
        snapshot["today_triggers"] = db.count("alert_log")
        snapshot["unresolved_triggers"] = db.count("alert_log", "resolved=0")
        snapshot["checks"] = db.count("alert_checks")
        return snapshot
