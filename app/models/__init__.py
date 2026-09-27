"""Pydantic 模型的稳定导出门面。

模型实现已按领域拆分到 ``app.models`` 包下的子包中：

- ``app.models.common``：跨领域共享的基础常量、数值工具与通用余额视图；
- ``app.models.simulation``：基础模拟域（/simulate、/plan-initial-balances）；
- ``app.models.analysis``：分析域（/forecast、/usage-report、/fairness-report）；
- ``app.models.scheduling``：预留/路由/排队域（/reservation-simulate、
  /route-simulate、/queue-simulate）；
- ``app.models.windows``：窗口/结转域（/window-simulate、/rollover-simulate）；
- ``app.models.debt``：债务对比域（/debt-simulate、/debt-diff）。

业务代码应从对应领域子模块导入；``app.models`` 仅作为稳定门面，按旧
``app/models.py`` 的公开名字原样再导出，历史导入路径（``from app.models
import X``）保持可用，请求/响应 JSON 与 OpenAPI schema 名称均不随之变化。
"""
from __future__ import annotations

# 公共基础
from app.models.common import EPS, BucketBalance, _is_finite_number

# 基础模拟域
from app.models.simulation import (
    BucketSpec,
    PlanBucketResult,
    PlanFailure,
    PlanInitialBalancesResponse,
    SimRequest,
    SimResult,
    SimulateRequest,
    SimulateResponse,
    _simulate_cross_validation_errors,
)

# 分析域
from app.models.analysis import (
    BusiestWindow,
    FairnessPrincipalRow,
    FairnessReportRequest,
    FairnessReportResponse,
    FairnessScopeReport,
    FairnessSimRequest,
    FairnessWindowReport,
    FirstShortfall,
    ForecastBucket,
    ForecastBucketResult,
    ForecastRequest,
    ForecastResponse,
    UsageBucketReport,
    UsageReportRequest,
    UsageReportResponse,
    UsageWindowStats,
    WindowRange,
)

# 预留/路由/排队域
from app.models.scheduling import (
    OccupancyInterval,
    QueueSimRequest,
    QueueSimResult,
    QueueSimulateRequest,
    QueueSimulateResponse,
    ReservationBucket,
    ReservationBucketOccupancy,
    ReservationRequest,
    ReservationResult,
    ReservationSimulateRequest,
    ReservationSimulateResponse,
    RouteCandidateRejection,
    RouteSimRequest,
    RouteSimResult,
    RouteSimulateRequest,
    RouteSimulateResponse,
)

# 窗口/结转域
from app.models.windows import (
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

# 债务对比域
from app.models.debt import (
    DebtBucketBalance,
    DebtBucketFinalState,
    DebtBucketSpec,
    DebtDiffBucketDelta,
    DebtDiffFinalSnapshot,
    DebtDiffFirstDivergence,
    DebtDiffOutcome,
    DebtDiffRequest,
    DebtDiffResponse,
    DebtDiffResult,
    DebtRejection,
    DebtSimResult,
    DebtSimulateRequest,
    DebtSimulateResponse,
)

__all__ = [
    # 公共基础
    "EPS",
    "BucketBalance",
    # 基础模拟域
    "BucketSpec",
    "PlanBucketResult",
    "PlanFailure",
    "PlanInitialBalancesResponse",
    "SimRequest",
    "SimResult",
    "SimulateRequest",
    "SimulateResponse",
    # 分析域
    "BusiestWindow",
    "FairnessPrincipalRow",
    "FairnessReportRequest",
    "FairnessReportResponse",
    "FairnessScopeReport",
    "FairnessSimRequest",
    "FairnessWindowReport",
    "FirstShortfall",
    "ForecastBucket",
    "ForecastBucketResult",
    "ForecastRequest",
    "ForecastResponse",
    "UsageBucketReport",
    "UsageReportRequest",
    "UsageReportResponse",
    "UsageWindowStats",
    "WindowRange",
    # 预留/路由/排队域
    "OccupancyInterval",
    "QueueSimRequest",
    "QueueSimResult",
    "QueueSimulateRequest",
    "QueueSimulateResponse",
    "ReservationBucket",
    "ReservationBucketOccupancy",
    "ReservationRequest",
    "ReservationResult",
    "ReservationSimulateRequest",
    "ReservationSimulateResponse",
    "RouteCandidateRejection",
    "RouteSimRequest",
    "RouteSimResult",
    "RouteSimulateRequest",
    "RouteSimulateResponse",
    # 窗口/结转域
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
    # 债务对比域
    "DebtBucketBalance",
    "DebtBucketFinalState",
    "DebtBucketSpec",
    "DebtDiffBucketDelta",
    "DebtDiffFinalSnapshot",
    "DebtDiffFirstDivergence",
    "DebtDiffOutcome",
    "DebtDiffRequest",
    "DebtDiffResponse",
    "DebtDiffResult",
    "DebtRejection",
    "DebtSimResult",
    "DebtSimulateRequest",
    "DebtSimulateResponse",
]
