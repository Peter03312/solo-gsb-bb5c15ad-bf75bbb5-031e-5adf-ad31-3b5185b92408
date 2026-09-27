"""分析域（/forecast）：等待时间预测请求/响应模型。"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import RequestValidationErrors
from app.models.common import EPS, _coerce_finite_number, _is_finite_number

class ForecastBucket(BaseModel):
    """预测时刻的桶状态快照。

    capacity: 桶容量（令牌上限）
    refill_rate: 每秒连续补充的令牌数
    balance: 当前余额（now 时刻持有）
    """

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="桶唯一标识")]
    capacity: Annotated[float, Field(description="桶容量，必须为正数")]
    refill_rate: Annotated[float, Field(description="令牌补充速率（令牌/秒），必须非负")]
    balance: Annotated[float, Field(description="当前余额，要求 0 ≤ balance ≤ capacity")]

    @field_validator("capacity", "refill_rate", mode="before")
    @classmethod
    def _validate_rate_fields(cls, value, info):
        # 与 BucketSpec 相同：在 pydantic float 转换前拒绝 NaN/Infinity
        # 及数字字符串转换后的非有限值。
        numeric = _coerce_finite_number(value)
        if numeric is None:
            raise ValueError(f"{info.field_name} 必须是有限数值")
        if info.field_name == "capacity" and numeric <= 0:
            raise ValueError("capacity 必须为正数")
        if info.field_name == "refill_rate" and numeric < 0:
            raise ValueError("refill_rate 必须为非负数")
        return numeric

    @field_validator("balance", mode="before")
    @classmethod
    def _validate_balance(cls, value):
        if not _is_finite_number(value) or value < 0:
            raise ValueError("balance 必须是非负有限数值")
        return value

    @model_validator(mode="after")
    def _check_balance_within_capacity(self):
        if self.balance > self.capacity + EPS:
            raise ValueError("balance 不能大于 capacity")
        return self


class ForecastRequest(BaseModel):
    """POST /forecast 请求体。"""

    model_config = ConfigDict(extra="forbid")

    now: Annotated[float, Field(description="当前时刻（秒，任意一致的时间原点）")]
    cost: Annotated[float, Field(gt=0, description="请求成本（链上每个桶都需持有的令牌数）")]
    chain: Annotated[list[str], Field(min_length=1, description="桶链：按顺序引用的桶 ID")]
    buckets: Annotated[list[ForecastBucket], Field(min_length=1, description="桶状态快照集合")]

    @field_validator("now", mode="before")
    @classmethod
    def _validate_now(cls, value):
        if not _is_finite_number(value):
            raise ValueError("now 必须是有限数值")
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

    @model_validator(mode="after")
    def _cross_validate(self):
        errors: list[dict] = []

        # 重复桶 ID
        seen: set[str] = set()
        duplicates = False
        for index, bucket in enumerate(self.buckets):
            if bucket.id in seen:
                duplicates = True
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
            raise RequestValidationErrors(errors)

        # 链中未知引用
        for c_index, bucket_id in enumerate(self.chain):
            if bucket_id not in seen:
                errors.append(
                    {
                        "type": "unknown_bucket_ref",
                        "loc": ("chain", c_index),
                        "msg": f"桶链引用了未定义的桶: {bucket_id}",
                        "input": bucket_id,
                    }
                )

        if errors:
            raise RequestValidationErrors(errors)
        return self


class ForecastBucketResult(BaseModel):
    """链上单个桶的可达性预测。"""

    id: str
    balance_now: Annotated[float, Field(description="now 时刻的余额")]
    reachable: Annotated[bool, Field(description="该桶单独是否可达成本")]
    reason: Annotated[
        str | None,
        Field(default=None, description="不可达原因：cost_exceeds_capacity / zero_refill_rate"),
    ] = None
    wait_seconds: Annotated[
        float | None, Field(default=None, description="该桶单独满足成本所需等待秒数；不可达为 null")
    ] = None
    projected_balance: Annotated[
        float | None,
        Field(default=None, description="预计可通过时刻的余额（不扣减）；链不可达时为 null"),
    ] = None


class ForecastResponse(BaseModel):
    reachable: Annotated[bool, Field(description="链上全部桶是否都能达到成本")]
    wait_seconds: Annotated[
        float | None, Field(description="全部桶首次同时满足成本所需等待秒数；不可达为 null")
    ]
    pass_at: Annotated[
        float | None, Field(description="预计可通过时刻 = now + wait_seconds；不可达为 null")
    ]
    limiting_bucket: Annotated[
        str | None,
        Field(description="等待最长的限制桶（并列取链上首个）；立即可通过或不可达时为 null"),
    ]
    buckets: Annotated[list[ForecastBucketResult], Field(description="链上各桶的预测，按链顺序")]


