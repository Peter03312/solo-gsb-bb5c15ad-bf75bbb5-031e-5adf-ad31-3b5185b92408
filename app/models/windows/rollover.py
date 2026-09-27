"""窗口/结转域（/rollover-simulate）：周期结转额度模型。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import RequestValidationErrors
from app.models.common import BucketBalance, _is_finite_number

# ---------- 周期结转额度试算 (/rollover-simulate) ----------


class RolloverPolicySpec(BaseModel):
    """周期结转额度策略。

    anchor: 周期对齐锚点时刻（秒，任意一致的时间原点）
    period_seconds: 周期长度（秒）
    allowance: 每个周期发放的基础额度
    carry_cap: 上一周期未用基础额度结转到下一周期的上限（旧结转额跨期一律失效）
    """

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="结转策略唯一标识")]
    anchor: Annotated[float, Field(description="周期对齐锚点时刻（秒），可为任意有限数值（含负数）")]
    period_seconds: Annotated[float, Field(description="周期长度（秒），必须为正数")]
    allowance: Annotated[float, Field(description="每个周期的基础额度，必须为正数")]
    carry_cap: Annotated[
        float, Field(description="上期未用基础额度结转到下期的上限，必须为非负数；0 表示不结转")
    ]

    @field_validator("anchor", "period_seconds", "allowance", "carry_cap", mode="before")
    @classmethod
    def _validate_numeric_fields(cls, value, info):
        if not _is_finite_number(value):
            raise ValueError(f"{info.field_name} 必须是有限数值")
        if info.field_name in ("period_seconds", "allowance") and value <= 0:
            raise ValueError(f"{info.field_name} 必须为正数")
        if info.field_name == "carry_cap" and value < 0:
            raise ValueError("carry_cap 必须为非负数")
        return value


class RolloverSimRequest(BaseModel):
    """单次周期结转试算请求。"""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="请求唯一标识")]
    timestamp: Annotated[float, Field(description="请求时间（秒，任意一致的时间原点）")]
    cost: Annotated[float, Field(gt=0, description="消耗量（对每个引用策略各计此数量）")]
    policy_refs: Annotated[
        list[str], Field(min_length=1, description="引用的结转策略 ID 列表，按顺序判定")
    ]

    @field_validator("timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value):
        if not _is_finite_number(value):
            raise ValueError("timestamp 必须是有限数值")
        return value

    @field_validator("cost", mode="before")
    @classmethod
    def _validate_cost(cls, value):
        if not _is_finite_number(value):
            raise ValueError("cost 必须是有限数值")
        if value <= 0:
            raise ValueError("cost 必须为正数")
        return value

    @field_validator("policy_refs")
    @classmethod
    def _validate_refs_shape(cls, value: list[str]):
        if not value:
            raise ValueError("policy_refs 至少需要引用一个结转策略")
        seen: set[str] = set()
        for policy_id in value:
            if not isinstance(policy_id, str) or not policy_id:
                raise ValueError("policy_refs 中的策略 ID 必须是非空字符串")
            if policy_id in seen:
                raise ValueError(f"policy_refs 中存在重复策略引用: {policy_id}")
            seen.add(policy_id)
        return value


class RolloverSimulateRequest(BaseModel):
    """POST /rollover-simulate 请求体。"""

    model_config = ConfigDict(extra="forbid")

    policies: Annotated[
        list[RolloverPolicySpec], Field(min_length=1, description="周期结转策略集合")
    ]
    requests: Annotated[
        list[RolloverSimRequest],
        Field(min_length=1, description="按时间戳试算的请求序列（可乱序输入）"),
    ]

    @model_validator(mode="after")
    def _cross_validate(self):
        errors: list[dict] = []

        # 重复策略 ID
        seen: set[str] = set()
        duplicates = False
        for index, policy in enumerate(self.policies):
            if policy.id in seen:
                duplicates = True
                errors.append(
                    {
                        "type": "duplicate_policy",
                        "loc": ("policies", index, "id"),
                        "msg": f"重复的结转策略 ID: {policy.id}",
                        "input": policy.id,
                    }
                )
            seen.add(policy.id)

        if duplicates:
            # 已存在重复定义，引用校验意义不大，直接返回
            raise RequestValidationErrors(errors)

        # 引用中未知策略
        for r_index, request in enumerate(self.requests):
            for p_index, policy_id in enumerate(request.policy_refs):
                if policy_id not in seen:
                    errors.append(
                        {
                            "type": "unknown_policy_ref",
                            "loc": ("requests", r_index, "policy_refs", p_index),
                            "msg": f"请求 {request.id!r} 引用了未定义的结转策略: {policy_id}",
                            "input": policy_id,
                        }
                    )

        # 重复请求 ID（便于调用方定位模拟结果）
        seen_req: set[str] = set()
        for r_index, request in enumerate(self.requests):
            if request.id in seen_req:
                errors.append(
                    {
                        "type": "duplicate_request",
                        "loc": ("requests", r_index, "id"),
                        "msg": f"重复的请求 ID: {request.id}",
                        "input": request.id,
                    }
                )
            seen_req.add(request.id)

        if errors:
            raise RequestValidationErrors(errors)
        return self


class PeriodRange(BaseModel):
    """周期区间：编号 index 与半开时间区间 [start, end)。"""

    index: Annotated[int, Field(description="周期编号 = floor((timestamp - anchor) / period_seconds)）")]
    start: Annotated[float, Field(description="周期起点（含）= anchor + index · period_seconds")]
    end: Annotated[float, Field(description="周期终点（不含）= start + period_seconds")]


class RolloverBalances(BaseModel):
    """某策略在一次请求判定时两类余额的扣减前后视图。"""

    base: Annotated[
        BucketBalance, Field(description="本期基础额度余额（每周期初重置为 allowance）")
    ]
    carry: Annotated[
        BucketBalance, Field(description="结转余额（上期未用基础额度结转而来，跨期即失效）")
    ]


class RolloverPolicyView(BaseModel):
    """单个请求对某个引用策略的判定视图。"""

    id: str
    period: Annotated[int, Field(description="判定时刻所在周期的编号")]
    period_window: Annotated[PeriodRange, Field(description="所在周期的编号与起止时间")]
    allowance: Annotated[float, Field(description="该策略每周期的基础额度")]
    carry_cap: Annotated[float, Field(description="该策略的结转上限")]
    balances: Annotated[RolloverBalances, Field(description="两类余额扣减前后快照")]


class RolloverRejection(BaseModel):
    """拒绝原因：两类余额合计不足的首个引用策略。"""

    policy_id: Annotated[str, Field(description="首个余额不足的策略 ID（按 policy_refs 顺序）")]
    reason: Annotated[
        Literal["insufficient_balance"],
        Field(description="拒绝原因：本期基础额度与结转余额合计不足以支付 cost"),
    ]
    period: Annotated[int, Field(description="判定时刻所在周期的编号")]
    period_window: Annotated[PeriodRange, Field(description="所在周期的编号与起止时间")]
    allowance: Annotated[float, Field(description="该策略每周期的基础额度")]
    carry_cap: Annotated[float, Field(description="该策略的结转上限")]
    cost: Annotated[float, Field(description="本次请求的消耗量")]
    base_available: Annotated[float, Field(description="判定时本期基础额度余额")]
    carry_available: Annotated[float, Field(description="判定时结转余额")]
    available: Annotated[float, Field(description="判定时两类余额合计 = base + carry")]
    shortage: Annotated[float, Field(description="缺口 = cost - available")]


class RolloverSimResult(BaseModel):
    """单项请求的周期结转试算结果。"""

    request_id: str
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: float
    cost: float
    allowed: bool
    policies: Annotated[
        dict[str, RolloverPolicyView],
        Field(description="各引用策略的判定视图，按 policy_refs 顺序"),
    ]
    rejected_by: Annotated[
        str | None, Field(default=None, description="首个余额不足的策略 ID；放行时为 null")
    ] = None
    rejection: Annotated[
        RolloverRejection | None, Field(default=None, description="拒绝原因明细；放行时为 null")
    ] = None


class RolloverPolicyFinalState(BaseModel):
    """单个策略在模拟结束时刻（最后一个请求时间戳）的终态。"""

    id: str
    anchor: Annotated[float, Field(description="周期对齐锚点时刻（秒）")]
    period_seconds: Annotated[float, Field(description="周期长度（秒）")]
    allowance: Annotated[float, Field(description="每周期基础额度")]
    carry_cap: Annotated[float, Field(description="结转上限")]
    period: Annotated[int, Field(description="终态观察时刻所在周期的编号")]
    period_window: Annotated[PeriodRange, Field(description="所在周期的编号与起止时间")]
    base: Annotated[float, Field(description="本期基础额度终态余额")]
    carry: Annotated[float, Field(description="结转余额终态")]
    available: Annotated[float, Field(description="两类余额终态合计 = base + carry")]


class RolloverSimulateResponse(BaseModel):
    allowed_count: int
    rejected_count: int
    results: Annotated[
        list[RolloverSimResult], Field(description="逐项请求结果，按原始输入索引升序")
    ]
    policies: Annotated[
        list[RolloverPolicyFinalState],
        Field(description="各策略在最后请求时刻的终态，按 policies 输入顺序"),
    ]


