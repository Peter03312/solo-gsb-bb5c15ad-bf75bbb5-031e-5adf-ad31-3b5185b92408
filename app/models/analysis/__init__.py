"""分析域：/forecast、/usage-report 与 /fairness-report 的模型。"""
from __future__ import annotations

from app.models.analysis.fairness import (
    FairnessPrincipalRow,
    FairnessReportRequest,
    FairnessReportResponse,
    FairnessScopeReport,
    FairnessSimRequest,
    FairnessWindowReport,
    FirstShortfall,
)
from app.models.analysis.forecast import (
    ForecastBucket,
    ForecastBucketResult,
    ForecastRequest,
    ForecastResponse,
)
from app.models.analysis.usage import (
    BusiestWindow,
    UsageBucketReport,
    UsageReportRequest,
    UsageReportResponse,
    UsageWindowStats,
    WindowRange,
)

__all__ = [
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
]
