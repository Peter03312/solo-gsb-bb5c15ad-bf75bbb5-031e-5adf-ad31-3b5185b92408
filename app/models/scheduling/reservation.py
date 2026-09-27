"""预留/路由/排队域（/reservation-simulate）：容量预留模型。"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import RequestValidationErrors
from app.models.common import _is_finite_number

class ReservationBucket(BaseModel):
    """预留试算桶定义：只有容量上限，没有补充概念。

    capacity: 桶容量，即任意时刻允许的并发占用上限
    """

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="桶唯一标识")]
    capacity: Annotated[float, Field(description="桶容量（并发占用上限），必须为正数")]

    @field_validator("capacity", mode="before")
    @classmethod
    def _validate_capacity(cls, value):
        if not _is_finite_number(value):
            raise ValueError("capacity 必须是有限数值")
        if value <= 0:
            raise ValueError("capacity 必须为正数")
        return value


class ReservationRequest(BaseModel):
    """单次预留请求：在半开区间 [start, end) 内占用链上每个桶的 cost 容量。"""

    model_config = ConfigDict(extra="forbid")

    start: Annotated[float, Field(description="预留起始时刻（含），秒，任意一致的时间原点")]
    end: Annotated[float, Field(description="预留结束时刻（不含），必须严格大于 start")]
    cost: Annotated[float, Field(gt=0, description="预留期间对链上每个桶的并发占用量")]
    chain: Annotated[list[str], Field(min_length=1, description="桶链：按顺序引用的桶 ID")]

    @field_validator("start", "end", mode="before")
    @classmethod
    def _validate_time(cls, value, info):
        if not _is_finite_number(value):
            raise ValueError(f"{info.field_name} 必须是有限数值")
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
    def _check_interval(self):
        if self.end <= self.start:
            raise ValueError("end 必须严格大于 start")
        return self


class ReservationSimulateRequest(BaseModel):
    """POST /reservation-simulate 请求体。"""

    model_config = ConfigDict(extra="forbid")

    buckets: Annotated[list[ReservationBucket], Field(min_length=1, description="预留桶集合")]
    reservations: Annotated[
        list[ReservationRequest], Field(min_length=1, description="按 start 升序试算的预留序列")
    ]

    @model_validator(mode="after")
    def _cross_validate(self):
        errors: list[dict] = []

        # 重复桶 ID
        seen: set[str] = set()
        duplicates: set[str] = set()
        for index, bucket in enumerate(self.buckets):
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
            raise RequestValidationErrors(errors)

        # 链中未知引用
        for r_index, reservation in enumerate(self.reservations):
            for c_index, bucket_id in enumerate(reservation.chain):
                if bucket_id not in seen:
                    errors.append(
                        {
                            "type": "unknown_bucket_ref",
                            "loc": ("reservations", r_index, "chain", c_index),
                            "msg": f"预留 {r_index} 引用了未定义的桶: {bucket_id}",
                            "input": bucket_id,
                        }
                    )

        if errors:
            raise RequestValidationErrors(errors)
        return self


class OccupancyInterval(BaseModel):
    """单个桶上并发占用量恒定的合并区间 [start, end)。"""

    start: Annotated[float, Field(description="区间起点（含）")]
    end: Annotated[float, Field(description="区间终点（不含）")]
    occupancy: Annotated[float, Field(description="该区间内的并发占用总量")]


class ReservationResult(BaseModel):
    """单项预留的试算结果。"""

    index: Annotated[int, Field(description="该预留在原始 reservations 数组中的下标")]
    start: float
    end: float
    cost: float
    allowed: Annotated[bool, Field(description="是否原子接受")]
    rejected_by: Annotated[
        str | None,
        Field(default=None, description="导致拒绝的第一个放不下的桶 ID；接受时为 null"),
    ] = None


class ReservationBucketOccupancy(BaseModel):
    """单个桶在全部接受预留叠加后的占用视图。"""

    id: str
    capacity: Annotated[float, Field(description="桶容量（并发占用上限）")]
    peak_occupancy: Annotated[float, Field(description="整个时间轴上的峰值并发占用")]
    intervals: Annotated[
        list[OccupancyInterval],
        Field(description="合并后的非零占用区间，按起点升序、相邻同占用相接区间已合并"),
    ]


class ReservationSimulateResponse(BaseModel):
    allowed_count: int
    rejected_count: int
    results: Annotated[
        list[ReservationResult], Field(description="逐项预留结果，按原始输入索引升序")
    ]
    buckets: Annotated[
        list[ReservationBucketOccupancy],
        Field(description="各桶合并后的占用区间与峰值并发，按 buckets 输入顺序"),
    ]


