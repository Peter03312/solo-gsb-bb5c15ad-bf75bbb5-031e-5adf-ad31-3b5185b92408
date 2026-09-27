"""基础模拟域（/simulate）：令牌桶定义、请求与跨字段校验。

/simulate、/plan-initial-balances、/usage-report 与 /fairness-report
共用这里的桶/请求模型与 _simulate_cross_validation_errors。
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import RequestValidationErrors
from app.models.common import (
    EPS,
    BucketBalance,
    _coerce_finite_number,
    _is_finite_number,
)

class BucketSpec(BaseModel):
    """令牌桶定义。

    capacity: 桶容量（令牌上限）
    refill_rate: 每秒连续补充的令牌数
    initial: 初始余额，缺省为满桶
    """

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="桶唯一标识")]
    capacity: Annotated[float, Field(description="桶容量，必须为正数")]
    refill_rate: Annotated[float, Field(description="令牌补充速率（令牌/秒），必须非负")]
    initial: Annotated[float | None, Field(default=None, description="初始余额，默认等于容量")] = None

    @field_validator("capacity", "refill_rate", mode="before")
    @classmethod
    def _validate_rate_fields(cls, value, info):
        # 必须在 pydantic 的 float 强制转换之前拦截：NaN 与任何数比较均为
        # False，mode="after" 的正/非负检查无法拦住 NaN/Infinity
        # （含 "NaN"/"1e999" 等字符串转换后的非有限值），会进入模拟
        # 引擎产生 NaN 余额或在响应渲染时触发 500。
        numeric = _coerce_finite_number(value)
        if numeric is None:
            raise ValueError(f"{info.field_name} 必须是有限数值")
        if info.field_name == "capacity" and numeric <= 0:
            raise ValueError("capacity 必须为正数")
        if info.field_name == "refill_rate" and numeric < 0:
            raise ValueError("refill_rate 必须为非负数")
        return numeric

    @field_validator("initial", mode="before")
    @classmethod
    def _validate_initial(cls, value):
        if value is not None and (not _is_finite_number(value) or value < 0):
            raise ValueError("initial 必须是非负有限数值")
        return value

    @model_validator(mode="after")
    def _check_initial_within_capacity(self):
        if self.initial is not None and self.initial > self.capacity + EPS:
            raise ValueError("initial 不能大于 capacity")
        return self


class SimRequest(BaseModel):
    """单次试算请求。"""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="请求唯一标识")]
    timestamp: Annotated[float, Field(description="请求时间（秒，任意一致的时间原点）")]
    cost: Annotated[float, Field(gt=0, description="消耗量（从链上每个桶扣减的令牌数）")]
    chain: Annotated[list[str], Field(min_length=1, description="桶链：按顺序引用的桶 ID")]

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

    @field_validator("chain")
    @classmethod
    def _validate_chain_shape(cls, value: list[str]):
        if not value:
            raise ValueError("chain 至少需要引用一个桶")
        seen: set[str] = set()
        for bucket_id in value:
            if not isinstance(bucket_id, str) or not bucket_id:
                raise ValueError("chain 中的桶 ID 必须是非空字符串")
            if bucket_id in seen:
                raise ValueError(f"chain 中存在重复桶引用: {bucket_id}")
            seen.add(bucket_id)
        return value


def _simulate_cross_validation_errors(
    buckets: list[BucketSpec], requests: list[SimRequest]
) -> list[dict]:
    """/simulate 系请求体的跨字段校验：重复桶、未知桶引用、重复请求 ID。

    /simulate、/plan-initial-balances、/usage-report 与 /fairness-report 共用。
    """
    errors: list[dict] = []

    # 重复桶 ID
    seen: set[str] = set()
    duplicates: set[str] = set()
    for index, bucket in enumerate(buckets):
        if bucket.id in seen:
            duplicates.add(bucket.id)
            errors.append(
                {
                    "type": "duplicate_bucket",
                    "loc": ("buckets", index, "id"),
                    "msg": f"重复的桶 ID: {bucket.id}",
                    "input": bucket.id,
                }
            )
        seen.add(bucket.id)

    if duplicates:
        # 已存在重复定义，引用校验意义不大，直接返回
        return errors

    # 链中未知引用
    for r_index, request in enumerate(requests):
        for c_index, bucket_id in enumerate(request.chain):
            if bucket_id not in seen:
                errors.append(
                    {
                        "type": "unknown_bucket_ref",
                        "loc": ("requests", r_index, "chain", c_index),
                        "msg": f"请求 {request.id!r} 引用了未定义的桶: {bucket_id}",
                        "input": bucket_id,
                    }
                )

    # 重复请求 ID（便于调用方定位模拟结果）
    seen_req: set[str] = set()
    for r_index, request in enumerate(requests):
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

    return errors


class SimulateRequest(BaseModel):
    """POST /simulate 请求体。"""

    model_config = ConfigDict(extra="forbid")

    buckets: Annotated[list[BucketSpec], Field(min_length=1, description="令牌桶集合")]
    requests: Annotated[list[SimRequest], Field(min_length=1, description="按时间戳模拟的请求序列")]

    @model_validator(mode="after")
    def _cross_validate(self):
        errors = _simulate_cross_validation_errors(self.buckets, self.requests)
        if errors:
            raise RequestValidationErrors(errors)
        return self


class SimResult(BaseModel):
    """单项请求的模拟结果。"""

    request_id: str
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: float
    allowed: bool
    rejected_by: Annotated[str | None, Field(default=None, description="导致拒绝的第一个余额不足的桶 ID")]
    balances: Annotated[dict[str, BucketBalance], Field(description="链上各桶扣减前后余额")]


class SimulateResponse(BaseModel):
    allowed_count: int
    rejected_count: int
    results: list[SimResult]
    final_balances: Annotated[dict[str, float], Field(description="模拟结束时所有桶的余额")]


