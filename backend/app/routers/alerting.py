"""告警判定与异常归因的模块路由。

路径挂在模块名下（``/api/alert_policy/...``、``/api/anomaly_analysis/...``、
``/api/metric_store/...``），与通用资源路由并存：通用路由负责列表登记动作，
这里负责判定审计、证据回查和从告警发起归因。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.services import service_for

router = APIRouter(prefix="/api", tags=["告警与归因"])


class CheckPayload(BaseModel):
    as_of: str | None = None


class AttributionPayload(BaseModel):
    attribution_dimensions: str | None = None


class RunPayload(BaseModel):
    recompute: bool = False


def _alert_service():
    return service_for("alert_policy")


def _analysis_service():
    return service_for("anomaly_analysis")


# ---------------------------------------------------------------------------
# 告警规则判定
# ---------------------------------------------------------------------------

@router.post("/alert_policy/{alert_id}/check")
def check_alert_rule(alert_id: int, payload: CheckPayload | None = None) -> dict:
    """对单条规则做一次判定。

    返回当时可见的采样、连续段、缺口、是否还能等补报，以及若补报超限
    会命中哪一段。每次判定都会落一条审计记录。
    """
    svc = _alert_service()
    result = svc.check_single(alert_id, as_of=(payload.as_of if payload else None))
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.get("/alert_policy/{alert_id}/history")
def alert_history(alert_id: int, limit: int = Query(default=50, ge=1, le=200)) -> dict:
    """触发记录：判定采样时刻、异常区间、超限证据。"""
    return {"ok": True, "alert_id": alert_id,
            "items": _alert_service().alert_history(alert_id, limit)}


@router.get("/alert_policy/{alert_id}/checks")
def alert_checks(alert_id: int, limit: int = Query(default=50, ge=1, le=200)) -> dict:
    """判定审计：每次检查看到的采样与未触发原因，用于事后回查。"""
    return {"ok": True, "alert_id": alert_id,
            "items": _alert_service().alert_checks(alert_id, limit)}


@router.get("/alert_policy/{alert_id}/calibrations")
def alert_calibrations(alert_id: int) -> dict:
    """按指标口径分别回看采样与连续段。"""
    result = _alert_service().calibration_groups(alert_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return result


@router.post("/alert_policy/{alert_id}/resolve/{log_id}")
def resolve_alert_log(alert_id: int, log_id: int) -> dict:
    """把某条触发记录标记为已解决。"""
    return _alert_service().resolve_alert(log_id)


# ---------------------------------------------------------------------------
# 归因
# ---------------------------------------------------------------------------

@router.post("/anomaly_analysis/from-alert/{log_id}")
def start_attribution(log_id: int, payload: AttributionPayload | None = None) -> dict:
    """从告警记录发起归因：锁定该次判定用的样本区间、口径和超限证据。"""
    dimensions = payload.attribution_dimensions if payload else None
    return _analysis_service().create_from_alert(log_id, attribution_dimensions=dimensions)


@router.post("/anomaly_analysis/{entry_id}/run")
def run_analysis(entry_id: int, payload: RunPayload | None = None) -> dict:
    """执行归因。结论所依据的采样被后续补报改写时默认拒绝，需显式重算。"""
    return _analysis_service().run_analysis(
        entry_id, recompute=bool(payload and payload.recompute)
    )


@router.get("/anomaly_analysis/{entry_id}/evidence")
def analysis_evidence(entry_id: int) -> dict:
    """回查归因结论当时的采样证据，以及后续补报是否改动了这些采样。"""
    return _analysis_service().evidence(entry_id)


# ---------------------------------------------------------------------------
# 指标采样登记（含迟到补报）
# ---------------------------------------------------------------------------

@router.get("/metric_store/{metric_id}/samples")
def list_metric_samples(
    metric_id: int,
    calibration_version: int | None = Query(default=None),
) -> dict:
    """按采样时刻列出采样；同一采样时刻同一口径只返回一条。"""
    from app.alerting import load_samples, public_sample

    samples, duplicates = load_samples(metric_id, calibration_version=calibration_version)
    return {
        "ok": True,
        "metric_id": metric_id,
        "duplicate_samples": duplicates,
        "samples": [public_sample(sample) for sample in samples],
    }


@router.post("/metric_store/{metric_id}/samples")
def record_metric_sample(metric_id: int, payload: dict) -> dict:
    """登记采样：同一 (采样时刻, 口径) 幂等；值变化按补报覆盖。"""
    return _record_sample(metric_id, payload, force_backfill=False)


@router.post("/metric_store/{metric_id}/backfill")
def backfill_metric_sample(metric_id: int, payload: dict) -> dict:
    """迟到补报：只占它原本的采样时刻，不新增采样点。"""
    return _record_sample(metric_id, payload, force_backfill=True)


def _record_sample(metric_id: int, payload: dict, *, force_backfill: bool) -> dict:
    if "value" not in payload or payload.get("sample_time") in (None, ""):
        raise HTTPException(
            status_code=422,
            detail="采样登记需要 value 和 sample_time：补报必须落到它原本的采样时刻",
        )
    svc = service_for("metric_store")
    try:
        value = float(payload["value"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="value 必须是数值")

    source = "backfill" if force_backfill else str(payload.get("source") or "live")
    return svc.record_sample(
        metric_id,
        value,
        sample_time=str(payload["sample_time"]),
        calibration_version=payload.get("calibration_version"),
        source=source,
    )
