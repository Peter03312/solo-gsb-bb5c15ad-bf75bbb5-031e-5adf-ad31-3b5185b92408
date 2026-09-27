"""债务对比域（/debt-simulate）：可借贷令牌桶模型。"""
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

# ---------- 可借贷连续补充桶试算 (/debt-simulate) ----------


class DebtBucketSpec(BucketSpec):
    """可借贷令牌桶定义：在 /simulate 桶字段上增加 debt_limit。

    debt_limit: 允许累计借入（债务余额）的上限，必须为非负有限数值；
    0 表示完全不允许借债，此时语义与 /simulate 相同。
    """

    debt_limit: Annotated[
        float,
        Field(description="累计债务上限，必须为非负有限数值；0 表示不允许借债"),
    ]

    @field_validator("debt_limit", mode="before")
    @classmethod
    def _validate_debt_limit(cls, value):
        if not _is_finite_number(value):
            raise ValueError("debt_limit 必须是有限数值")
        if value < 0:
            raise ValueError("debt_limit 必须为非负数")
        return value


class DebtSimulateRequest(BaseModel):
    """POST /debt-simulate 请求体。"""

    model_config = ConfigDict(extra="forbid")

    buckets: Annotated[list[DebtBucketSpec], Field(min_length=1, description="可借贷令牌桶集合")]
    requests: Annotated[
        list[SimRequest], Field(min_length=1, description="按时间戳模拟的请求序列（可乱序输入）")
    ]

    @model_validator(mode="after")
    def _cross_validate(self):
        errors = _simulate_cross_validation_errors(self.buckets, self.requests)
        if errors:
            raise RequestValidationErrors(errors)
        return self


class DebtBucketBalance(BaseModel):
    """链上单个桶在某次请求判定时的余额/债务视图。"""

    balance: Annotated[BucketBalance, Field(description="可用余额（补充偿还债务后的余额）扣减前后")]
    debt: Annotated[BucketBalance, Field(description="累计债务在偿还/新增借贷前后的数额")]


class DebtRejection(BaseModel):
    """拒绝原因：链上首个无法在借贷上限内满足成本的桶。"""

    bucket_id: Annotated[str, Field(description="首个无法满足成本的桶 ID（按 chain 顺序）")]
    reason: Annotated[
        Literal["cost_exceeds_limit", "debt_limit_exceeded"],
        Field(
            description=(
                "拒绝原因：cost_exceeds_limit=成本超过 capacity + debt_limit，"
                "任何时刻都不可达；debt_limit_exceeded=成本未超总上限，"
                "但当前可用余额加剩余借贷额度不足"
            )
        ),
    ]
    capacity: Annotated[float, Field(description="该桶容量")]
    debt_limit: Annotated[float, Field(description="该桶累计债务上限")]
    cost: Annotated[float, Field(description="本次请求的消耗量")]
    balance_available: Annotated[
        float, Field(description="判定时刻（补充偿还债务之后）的可用余额")
    ]
    debt_before: Annotated[float, Field(description="判定时刻偿还债务之后的累计债务")]
    available: Annotated[
        float, Field(description="判定时刻可支付总额 = 可用余额 + 剩余借贷额度")
    ]
    remaining_credit: Annotated[
        float, Field(description="剩余可借额度 = debt_limit - debt_before")
    ]
    shortage: Annotated[float, Field(description="缺口 = cost - available")]


class DebtSimResult(BaseModel):
    """单项请求的可借贷试算结果。"""

    request_id: str
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: float
    cost: float
    allowed: bool
    buckets: Annotated[
        dict[str, DebtBucketBalance],
        Field(description="链上各桶判定时刻可用余额与累计债务的扣减前后快照，按 chain 顺序"),
    ]
    rejected_by: Annotated[
        str | None, Field(default=None, description="首个无法满足成本的桶 ID；放行时为 null")
    ] = None
    rejection: Annotated[
        DebtRejection | None, Field(default=None, description="拒绝原因明细；放行时为 null")
    ] = None


class DebtBucketFinalState(BaseModel):
    """单个可借贷桶在模拟结束时刻（最后一个请求时间戳）的终态快照。"""

    id: str
    capacity: Annotated[float, Field(description="桶容量（令牌上限）")]
    refill_rate: Annotated[float, Field(description="令牌补充速率（令牌/秒）")]
    debt_limit: Annotated[float, Field(description="累计债务上限")]
    balance: Annotated[float, Field(description="终态可用余额")]
    debt: Annotated[float, Field(description="终态累计债务")]
    available: Annotated[
        float, Field(description="终态可立即支付总额 = balance + (debt_limit - debt)")
    ]


class DebtSimulateResponse(BaseModel):
    allowed_count: int
    rejected_count: int
    results: Annotated[
        list[DebtSimResult], Field(description="逐项请求结果，按原始输入索引升序")
    ]
    final_buckets: Annotated[
        list[DebtBucketFinalState],
        Field(description="各桶在最后请求时刻的终态快照，按 buckets 输入顺序"),
    ]


