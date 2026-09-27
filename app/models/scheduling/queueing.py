"""预留/路由/排队域（/queue-simulate）：严格 FIFO 排队模型。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import RequestValidationErrors
from app.models.common import BucketBalance, _is_finite_number
from app.models.simulation.core import (
    BucketSpec,
    SimRequest,
    _simulate_cross_validation_errors,
)

class QueueSimRequest(SimRequest):
    """单次排队试算请求：/simulate 请求字段 + 不早于 timestamp 的 deadline。

    deadline: 最晚可执行时刻（秒）；队首在 deadline 前（含）余额充足即原子扣减，
    否则等待至 deadline 标记 expired 出队。
    """

    deadline: Annotated[
        float, Field(description="最晚可执行时刻（秒），必须为有限数值且不早于 timestamp")
    ]

    @field_validator("deadline", mode="before")
    @classmethod
    def _validate_deadline(cls, value):
        if not _is_finite_number(value):
            raise ValueError("deadline 必须是有限数值")
        return value

    @model_validator(mode="after")
    def _check_deadline_not_before_timestamp(self):
        if self.deadline < self.timestamp:
            raise ValueError("deadline 必须不早于 timestamp")
        return self


class QueueSimulateRequest(BaseModel):
    """POST /queue-simulate 请求体。"""

    model_config = ConfigDict(extra="forbid")

    buckets: Annotated[list[BucketSpec], Field(min_length=1, description="令牌桶集合")]
    requests: Annotated[
        list[QueueSimRequest],
        Field(min_length=1, description="按（到达时间, 输入顺序）排入严格 FIFO 队列的请求序列"),
    ]

    @model_validator(mode="after")
    def _cross_validate(self):
        errors = _simulate_cross_validation_errors(self.buckets, self.requests)
        if errors:
            raise RequestValidationErrors(errors)
        return self


class QueueSimResult(BaseModel):
    """单项排队请求的结果。"""

    request_id: str
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: float
    deadline: float
    status: Annotated[
        Literal["executed", "expired"],
        Field(
            description="executed=在 deadline 前（含）原子扣减放行；expired=等待至 deadline 仍不可执行"
        ),
    ]
    executed_at: Annotated[
        float | None, Field(default=None, description="实际执行时刻；expired 时为 null")
    ] = None
    expired_at: Annotated[
        float | None, Field(default=None, description="过期出队时刻；executed 时为 null")
    ] = None
    wait_seconds: Annotated[float, Field(description="从到达到执行/过期的等待秒数")]
    limiting_bucket: Annotated[
        str | None,
        Field(
            default=None,
            description="就绪最晚的限制桶（并列取链上首个）；成为队首即可执行时为 null",
        ),
    ] = None
    balances: Annotated[
        dict[str, BucketBalance],
        Field(description="链上各桶在判定时刻（补充之后、扣减之前/之后）的余额"),
    ]


class QueueSimulateResponse(BaseModel):
    executed_count: int
    expired_count: int
    results: Annotated[
        list[QueueSimResult], Field(description="逐项排队结果，按原始输入索引升序")
    ]
    final_balances: Annotated[
        dict[str, float], Field(description="最后一个请求出队时刻所有桶的余额")
    ]


