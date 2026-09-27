"""预留/路由/排队域（/route-simulate）：多候选链选路模型。"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import RequestValidationErrors
from app.models.common import BucketBalance, _is_finite_number
from app.models.simulation.core import BucketSpec

class RouteSimRequest(BaseModel):
    """单次路由试算请求：携带多条候选桶链，由服务挑选一条放行。"""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="请求唯一标识")]
    timestamp: Annotated[float, Field(description="请求时间（秒，任意一致的时间原点）")]
    cost: Annotated[float, Field(gt=0, description="消耗量（从选中链上每个桶扣减的令牌数）")]
    chains: Annotated[
        list[list[str]],
        Field(min_length=1, description="候选桶链列表：至少一条，每条链按顺序引用桶 ID"),
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

    @field_validator("chains")
    @classmethod
    def _validate_chains_shape(cls, value: list[list[str]]):
        if not value:
            raise ValueError("chains 至少需要一条候选链")
        for chain in value:
            if not chain:
                raise ValueError("候选链至少需要引用一个桶")
            seen: set[str] = set()
            for bucket_id in chain:
                if not isinstance(bucket_id, str) or not bucket_id:
                    raise ValueError("候选链中的桶 ID 必须是非空字符串")
                if bucket_id in seen:
                    raise ValueError(f"候选链中存在重复桶引用: {bucket_id}")
                seen.add(bucket_id)
        return value


class RouteSimulateRequest(BaseModel):
    """POST /route-simulate 请求体。"""

    model_config = ConfigDict(extra="forbid")

    buckets: Annotated[list[BucketSpec], Field(min_length=1, description="令牌桶集合")]
    requests: Annotated[
        list[RouteSimRequest], Field(min_length=1, description="按时间戳路由试算的请求序列")
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

        # 候选链中未知引用
        for r_index, request in enumerate(self.requests):
            for c_index, chain in enumerate(request.chains):
                for b_index, bucket_id in enumerate(chain):
                    if bucket_id not in seen:
                        errors.append(
                            {
                                "type": "unknown_bucket_ref",
                                "loc": ("requests", r_index, "chains", c_index, b_index),
                                "msg": (
                                    f"请求 {request.id!r} 的候选链 {c_index} "
                                    f"引用了未定义的桶: {bucket_id}"
                                ),
                                "input": bucket_id,
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


class RouteCandidateRejection(BaseModel):
    """候选链的拒绝原因：该链上第一个余额不足的桶。"""

    chain_index: Annotated[int, Field(description="候选链在 chains 中的下标")]
    rejected_by: Annotated[str, Field(description="该候选链上第一个余额不足的桶 ID")]
    available: Annotated[
        float, Field(description="判定时刻（补充之后、扣减之前）该桶的余额")
    ]


class RouteSimResult(BaseModel):
    """单项路由请求的试算结果。"""

    request_id: str
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: float
    allowed: bool
    selected_chain_index: Annotated[
        int | None,
        Field(default=None, description="选中候选链在 chains 中的下标；被拒绝时为 null"),
    ] = None
    selected_chain: Annotated[
        list[str] | None,
        Field(default=None, description="选中的桶链；被拒绝时为 null"),
    ] = None
    balances: Annotated[
        dict[str, BucketBalance],
        Field(description="本请求触及的所有桶（全部候选链的并集）扣减前后余额"),
    ]
    rejections: Annotated[
        list[RouteCandidateRejection] | None,
        Field(default=None, description="被拒绝时逐候选链的首个不足桶；放行时为 null"),
    ] = None


class RouteSimulateResponse(BaseModel):
    allowed_count: int
    rejected_count: int
    results: Annotated[
        list[RouteSimResult], Field(description="逐项路由结果，按原始输入索引升序")
    ]
    final_balances: Annotated[dict[str, float], Field(description="模拟结束时所有桶的余额")]


