"""窗口/结转域：/window-simulate 与 /rollover-simulate 的模型。"""
from __future__ import annotations

from app.models.windows.rollover import (
    PeriodRange,
    RolloverBalances,
    RolloverPolicyFinalState,
    RolloverPolicySpec,
    RolloverPolicyView,
    RolloverRejection,
    RolloverSimRequest,
    RolloverSimResult,
    RolloverSimulateRequest,
    RolloverSimulateResponse,
)
from app.models.windows.sliding import (
    SlidingWindowRange,
    WindowPolicyFinalState,
    WindowPolicySpec,
    WindowPolicyView,
    WindowPrincipalOccupancy,
    WindowRejection,
    WindowSimRequest,
    WindowSimResult,
    WindowSimulateRequest,
    WindowSimulateResponse,
)

__all__ = [
    "PeriodRange",
    "RolloverBalances",
    "RolloverPolicyFinalState",
    "RolloverPolicySpec",
    "RolloverPolicyView",
    "RolloverRejection",
    "RolloverSimRequest",
    "RolloverSimResult",
    "RolloverSimulateRequest",
    "RolloverSimulateResponse",
    "SlidingWindowRange",
    "WindowPolicyFinalState",
    "WindowPolicySpec",
    "WindowPolicyView",
    "WindowPrincipalOccupancy",
    "WindowRejection",
    "WindowSimRequest",
    "WindowSimResult",
    "WindowSimulateRequest",
    "WindowSimulateResponse",
]
