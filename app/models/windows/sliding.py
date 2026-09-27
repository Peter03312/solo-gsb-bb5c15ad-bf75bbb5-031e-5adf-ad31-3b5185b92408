"""窗口/结转域（/window-simulate）：半开滑动窗口配额模型。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import RequestValidationErrors
from app.models.common import _is_finite_number

class WindowPolicySpec(BaseModel):
    """半开滑动窗口配额策略。

    window_seconds: 滑动窗口宽度（秒）
    limit: 窗口内按归属主体（principal）统计的累计消耗上限
    """

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="窗口策略唯一标识")]
    window_seconds: Annotated[float, Field(description="滑动窗口宽度（秒），必须为正数")]
    limit: Annotated[float, Field(description="窗口内累计消耗上限，必须为正数")]

    @field_validator("window_seconds", "limit", mode="before")
    @classmethod
    def _validate_positive_fields(cls, value, info):
        if not _is_finite_number(value):
            raise ValueError(f"{info.field_name} 必须是有限数值")
        if value <= 0:
            raise ValueError(f"{info.field_name} 必须为正数")
        return value


class WindowSimRequest(BaseModel):
    """单次滑动窗口试算请求。"""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="请求唯一标识")]
    timestamp: Annotated[float, Field(description="请求时间（秒，任意一致的时间原点）")]
    principal: Annotated[
        str, Field(min_length=1, description="请求归属主体（租户/用户/调用方 ID）")
    ]
    cost: Annotated[float, Field(gt=0, description="消耗量（对每个引用策略各计此数量）")]
    policy_refs: Annotated[
        list[str], Field(min_length=1, description="引用的窗口策略 ID 列表，按顺序判定")
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

    @field_validator("principal", mode="before")
    @classmethod
    def _validate_principal(cls, value):
        if not isinstance(value, str) or not value:
            raise ValueError("principal 必须是非空字符串")
        return value

    @field_validator("policy_refs")
    @classmethod
    def _validate_refs_shape(cls, value: list[str]):
        if not value:
            raise ValueError("policy_refs 至少需要引用一个窗口策略")
        seen: set[str] = set()
        for policy_id in value:
            if not isinstance(policy_id, str) or not policy_id:
                raise ValueError("policy_refs 中的策略 ID 必须是非空字符串")
            if policy_id in seen:
                raise ValueError(f"policy_refs 中存在重复策略引用: {policy_id}")
            seen.add(policy_id)
        return value


class WindowSimulateRequest(BaseModel):
    """POST /window-simulate 请求体。"""

    model_config = ConfigDict(extra="forbid")

    policies: Annotated[
        list[WindowPolicySpec], Field(min_length=1, description="滑动窗口策略集合")
    ]
    requests: Annotated[
        list[WindowSimRequest],
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
                        "msg": f"重复的窗口策略 ID: {policy.id}",
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
                            "msg": f"请求 {request.id!r} 引用了未定义的窗口策略: {policy_id}",
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


class SlidingWindowRange(BaseModel):
    """请求判定时刻的半开滑动窗口 (start, end]：左端不含、右端含。"""

    start: Annotated[float, Field(description="窗口左端（不含）= end - window_seconds")]
    end: Annotated[float, Field(description="窗口右端（含）= 请求时间戳")]


class WindowPolicyView(BaseModel):
    """单个请求对某个引用策略的判定视图。"""

    id: str
    window_seconds: Annotated[float, Field(description="该策略的窗口宽度（秒）")]
    limit: Annotated[float, Field(description="该策略的窗口累计消耗上限")]
    window: Annotated[SlidingWindowRange, Field(description="判定时刻的半开窗口边界")]
    used_before: Annotated[
        float, Field(description="移除左端过期事件后、本请求记账前的窗口内累计消耗")
    ]
    used_after: Annotated[
        float, Field(description="本请求记账后的窗口内累计消耗；被拒绝时与 used_before 相同")
    ]
    remaining_before: Annotated[
        float, Field(description="记账前剩余额度 = limit - used_before")
    ]
    remaining_after: Annotated[
        float,
        Field(description="记账后剩余额度 = limit - used_after；被拒绝时与 remaining_before 相同"),
    ]


class WindowRejection(BaseModel):
    """拒绝原因：首个累计消耗超过上限的引用策略。"""

    policy_id: Annotated[str, Field(description="首个超限的窗口策略 ID（按 policy_refs 顺序）")]
    reason: Annotated[
        Literal["limit_exceeded"],
        Field(description="拒绝原因：窗口内累计消耗加本次 cost 超过 limit"),
    ]
    window_seconds: Annotated[float, Field(description="该策略的窗口宽度（秒）")]
    limit: Annotated[float, Field(description="该策略的窗口累计消耗上限")]
    used: Annotated[float, Field(description="判定时窗口内已占用的累计消耗")]
    cost: Annotated[float, Field(description="本次请求的消耗量")]
    required: Annotated[float, Field(description="放行所需额度 = used + cost")]
    available: Annotated[float, Field(description="判定时剩余额度 = limit - used")]
    window: Annotated[SlidingWindowRange, Field(description="判定时刻的半开窗口边界")]


class WindowSimResult(BaseModel):
    """单项请求的滑动窗口试算结果。"""

    request_id: str
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: float
    principal: str
    cost: float
    allowed: bool
    policies: Annotated[
        dict[str, WindowPolicyView],
        Field(description="各引用策略的判定视图，按 policy_refs 顺序"),
    ]
    rejected_by: Annotated[
        str | None, Field(default=None, description="首个超限的策略 ID；放行时为 null")
    ] = None
    rejection: Annotated[
        WindowRejection | None, Field(default=None, description="拒绝原因明细；放行时为 null")
    ] = None


class WindowPrincipalOccupancy(BaseModel):
    """终态时刻某个主体在单个策略窗口内的占用。"""

    principal: Annotated[str, Field(description="主体标识")]
    event_count: Annotated[int, Field(description="终态窗口内仍有效（未过期）的记账事件数")]
    used: Annotated[float, Field(description="终态窗口内该主体的累计消耗")]
    remaining: Annotated[float, Field(description="终态剩余额度 = limit - used")]
    window: Annotated[SlidingWindowRange, Field(description="终态时刻的半开窗口边界")]


class WindowPolicyFinalState(BaseModel):
    """单个策略在模拟结束时刻（最后一个请求时间戳）的终态占用。"""

    id: str
    window_seconds: Annotated[float, Field(description="该策略的窗口宽度（秒）")]
    limit: Annotated[float, Field(description="该策略的窗口累计消耗上限")]
    total_used: Annotated[
        float, Field(description="终态窗口内全部主体的累计消耗合计")
    ]
    window: Annotated[SlidingWindowRange, Field(description="终态时刻的半开窗口边界")]
    principals: Annotated[
        list[WindowPrincipalOccupancy],
        Field(description="各主体终态占用，按该策略上首次成功记账的顺序"),
    ]


class WindowSimulateResponse(BaseModel):
    allowed_count: int
    rejected_count: int
    results: Annotated[
        list[WindowSimResult], Field(description="逐项请求结果，按原始输入索引升序")
    ]
    policies: Annotated[
        list[WindowPolicyFinalState],
        Field(description="各策略的终态占用，按 policies 输入顺序"),
    ]


