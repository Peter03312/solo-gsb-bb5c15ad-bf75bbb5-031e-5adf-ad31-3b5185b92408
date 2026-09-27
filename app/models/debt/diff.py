"""债务对比域（/debt-diff）：两情景差异对比模型。"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.errors import RequestValidationErrors
from app.models.debt.simulate import (
    DebtBucketFinalState,
    DebtSimulateRequest,
)

# ---------- 两情景差异试算 (/debt-diff) ----------


class DebtDiffRequest(BaseModel):
    """POST /debt-diff 请求体：两组格式与 /debt-simulate 相同的情景。

    baseline 与 candidate 的桶集合（按 ID 比较，顺序可不同）以及请求序列
    的长度、逐索引请求 ID 与时间戳必须完全一致；cost / chain / 桶参数
    （capacity / refill_rate / initial / debt_limit）可以不同——它们正是
    要比较的情景差异。任何不一致都以 422 返回定位错误，不做回放。
    """

    model_config = ConfigDict(extra="forbid")

    baseline: Annotated[DebtSimulateRequest, Field(description="基线情景（/debt-simulate 格式）")]
    candidate: Annotated[DebtSimulateRequest, Field(description="候选情景（/debt-simulate 格式）")]

    @model_validator(mode="after")
    def _cross_validate(self):
        errors: list[dict] = []

        baseline_requests = self.baseline.requests
        candidate_requests = self.candidate.requests

        # 请求数量不一致：无法按原始索引对齐
        if len(baseline_requests) != len(candidate_requests):
            errors.append(
                {
                    "type": "scenario_request_count_mismatch",
                    "loc": ("candidate", "requests"),
                    "msg": (
                        "baseline 与 candidate 的请求数量不一致："
                        f"{len(baseline_requests)} != {len(candidate_requests)}"
                    ),
                    "input": {
                        "baseline": len(baseline_requests),
                        "candidate": len(candidate_requests),
                    },
                }
            )

        # 桶集合必须一致（按 ID 比较，允许两边桶定义顺序不同）：
        # 否则逐桶余额/债务差值无法对齐
        baseline_bucket_ids = [b.id for b in self.baseline.buckets]
        candidate_bucket_ids = [b.id for b in self.candidate.buckets]
        baseline_bucket_set = set(baseline_bucket_ids)
        candidate_bucket_set = set(candidate_bucket_ids)
        if baseline_bucket_set != candidate_bucket_set:
            errors.append(
                {
                    "type": "scenario_bucket_set_mismatch",
                    "loc": ("candidate", "buckets"),
                    "msg": "baseline 与 candidate 的桶 ID 集合不一致",
                    "input": {
                        "missing_in_candidate": sorted(
                            baseline_bucket_set - candidate_bucket_set
                        ),
                        "extra_in_candidate": sorted(
                            candidate_bucket_set - baseline_bucket_set
                        ),
                    },
                }
            )

        # 逐原始索引对齐：请求 ID 与时间戳必须一致
        for index in range(min(len(baseline_requests), len(candidate_requests))):
            base_request = baseline_requests[index]
            cand_request = candidate_requests[index]
            if base_request.id != cand_request.id:
                errors.append(
                    {
                        "type": "scenario_request_id_mismatch",
                        "loc": ("candidate", "requests", index, "id"),
                        "msg": (
                            f"索引 {index} 处请求 ID 不一致："
                            f"{base_request.id!r} != {cand_request.id!r}"
                        ),
                        "input": {
                            "index": index,
                            "baseline": base_request.id,
                            "candidate": cand_request.id,
                        },
                    }
                )
            if float(base_request.timestamp) != float(cand_request.timestamp):
                errors.append(
                    {
                        "type": "scenario_timestamp_mismatch",
                        "loc": ("candidate", "requests", index, "timestamp"),
                        "msg": (
                            f"索引 {index}（请求 {base_request.id!r}）时间戳不一致："
                            f"{float(base_request.timestamp)} != "
                            f"{float(cand_request.timestamp)}"
                        ),
                        "input": {
                            "index": index,
                            "request_id": base_request.id,
                            "baseline": float(base_request.timestamp),
                            "candidate": float(cand_request.timestamp),
                        },
                    }
                )

        if errors:
            raise RequestValidationErrors(errors)
        return self


class DebtDiffOutcome(BaseModel):
    """单个情景在某个请求上的放行结论（用于呈现首个分歧）。"""

    allowed: Annotated[bool, Field(description="该情景下该请求是否放行")]
    rejected_by: Annotated[
        str | None, Field(description="该情景下首个无法满足成本的桶 ID；放行时为 null")
    ] = None
    reason: Annotated[
        str | None,
        Field(
            description=(
                "该情景下的拒绝原因：cost_exceeds_limit / "
                "debt_limit_exceeded；放行时为 null"
            )
        ),
    ] = None


class DebtDiffFirstDivergence(BaseModel):
    """按原始索引顺序首个 allowed/拒绝原因分歧的定位与双方结论。"""

    request_id: Annotated[str, Field(description="首个分歧请求的 ID")]
    index: Annotated[int, Field(description="首个分歧请求在原始 requests 数组中的下标")]
    timestamp: Annotated[float, Field(description="首个分歧请求的时间戳")]
    baseline: Annotated[DebtDiffOutcome, Field(description="基线情景的放行结论")]
    candidate: Annotated[DebtDiffOutcome, Field(description="候选情景的放行结论")]


class DebtDiffBucketDelta(BaseModel):
    """单个请求时刻某个桶在两情景间的状态差值。

    基线/候选各给出该时刻（连续补充/偿债之后）桶状态与本请求之后的
    余额/债务；某情景的该请求链未引用此桶时，delta 字段为 null
    （before/after 数值相同，两侧时间推进仍可比较）。
    """

    baseline_balance: Annotated[float, Field(description="基线情景该请求之后的可用余额")]
    candidate_balance: Annotated[float, Field(description="候选情景该请求之后的可用余额")]
    balance_delta: Annotated[
        float | None,
        Field(
            description=(
                "余额差值 = candidate - baseline（两情景链上均引用此桶时）；"
                "任一情景未引用时为 null"
            )
        ),
    ]
    baseline_debt: Annotated[float, Field(description="基线情景该请求之后的累计债务")]
    candidate_debt: Annotated[float, Field(description="候选情景该请求之后的累计债务")]
    debt_delta: Annotated[
        float | None,
        Field(description="债务差值 = candidate - baseline（同上，未引用时为 null）"),
    ]


class DebtDiffResult(BaseModel):
    """逐原始索引对齐的两情景单请求差异。"""

    request_id: str
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: float
    diverged: Annotated[
        bool,
        Field(
            description=(
                "该请求的放行结论是否分歧：allowed 不同，"
                "或双方均拒绝但 rejected_by/reason 不同"
            )
        ),
    ]
    baseline: Annotated[DebtDiffOutcome, Field(description="基线情景的放行结论")]
    candidate: Annotated[DebtDiffOutcome, Field(description="候选情景的放行结论")]
    buckets: Annotated[
        dict[str, DebtDiffBucketDelta],
        Field(
            description=(
                "该请求时刻两情景请求链引用桶的并集上的余额/债务差值，"
                "按两情景链上桶首次出现的顺序排列"
            )
        ),
    ]


class DebtDiffFinalSnapshot(BaseModel):
    """单个桶在两情景终态（最后请求时刻）的快照差值。"""

    id: str
    baseline: Annotated[DebtBucketFinalState, Field(description="基线情景终态快照")]
    candidate: Annotated[DebtBucketFinalState, Field(description="候选情景终态快照")]
    balance_delta: Annotated[
        float, Field(description="终态可用余额差值 = candidate - baseline")
    ]
    debt_delta: Annotated[
        float, Field(description="终态累计债务差值 = candidate - baseline")
    ]
    available_delta: Annotated[
        float,
        Field(description="终态可立即支付总额差值 = candidate - baseline"),
    ]


class DebtDiffResponse(BaseModel):
    request_count: Annotated[int, Field(description="对齐回放的请求数量")]
    divergence_count: Annotated[int, Field(description="放行结论发生分歧的请求总数")]
    first_divergence: Annotated[
        DebtDiffFirstDivergence | None,
        Field(description="按原始索引顺序的首个分歧；全程无分歧时为 null"),
    ] = None
    results: Annotated[
        list[DebtDiffResult],
        Field(description="逐原始索引对齐的单请求差异"),
    ]
    final_buckets: Annotated[
        list[DebtDiffFinalSnapshot],
        Field(description="各桶两情景终态快照差值，按 baseline 桶输入顺序"),
    ]
