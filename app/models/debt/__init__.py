"""债务对比域：/debt-simulate 与 /debt-diff 的模型。"""
from __future__ import annotations

from app.models.debt.diff import (
    DebtDiffBucketDelta,
    DebtDiffFinalSnapshot,
    DebtDiffFirstDivergence,
    DebtDiffOutcome,
    DebtDiffRequest,
    DebtDiffResponse,
    DebtDiffResult,
)
from app.models.debt.simulate import (
    DebtBucketBalance,
    DebtBucketFinalState,
    DebtBucketSpec,
    DebtRejection,
    DebtSimResult,
    DebtSimulateRequest,
    DebtSimulateResponse,
)

__all__ = [
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
