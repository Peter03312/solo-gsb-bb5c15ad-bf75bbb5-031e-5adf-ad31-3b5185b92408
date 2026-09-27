"""基础模拟域（/plan-initial-balances）：初始余额规划响应模型。"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from app.models.simulation.core import SimulateRequest

class PlanBucketResult(BaseModel):
    """单个桶的初始余额规划结果。"""

    id: str
    required_initial: Annotated[
        float, Field(description="满足该桶全部链上消耗所需的最低初始余额（[0, capacity] 内）")
    ]
    requested_initial: Annotated[
        float, Field(description="请求中给出的 initial；缺省按 capacity 计")
    ]
    delta: Annotated[
        float,
        Field(description="required_initial - requested_initial；为正表示请求初始余额不足"),
    ]


class PlanFailure(BaseModel):
    """满桶起步仍不可行时，按处理顺序首个无法满足的请求与桶。"""

    request_id: str
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: float
    bucket_id: Annotated[str, Field(description="链上首个余额不足的桶 ID")]
    cost: Annotated[float, Field(description="该请求的消耗量")]
    available: Annotated[
        float, Field(description="满桶起步回放中，该桶在判定时刻（补充之后、扣减之前）的余额")
    ]


class PlanInitialBalancesResponse(BaseModel):
    reachable: Annotated[bool, Field(description="满桶起步是否能让全部请求放行")]
    buckets: Annotated[
        list[PlanBucketResult] | None,
        Field(description="各桶规划结果，按 buckets 输入顺序；不可行为 null"),
    ]
    final_balances: Annotated[
        dict[str, float] | None,
        Field(description="用规划值完整回放得到的终态余额；不可行为 null"),
    ]
    first_failure: Annotated[
        PlanFailure | None,
        Field(description="不可行时按处理顺序首个无法满足的请求与桶；可行时为 null"),
    ]


