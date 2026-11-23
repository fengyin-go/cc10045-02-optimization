"""业务路由汇总：资源路由 + 管道路由 + 告警/归因路由。"""
from __future__ import annotations

from app.routers import resource
from app.routers import pipeline
from app.routers import alerting

ROUTERS = [resource]
PIPELINE_ROUTER = pipeline.router
ALERTING_ROUTER = alerting.router