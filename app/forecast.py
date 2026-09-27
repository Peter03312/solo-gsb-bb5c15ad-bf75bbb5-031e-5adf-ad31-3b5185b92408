"""等待时间预测：链上全部桶首次同时满足成本的时刻（不扣减任何状态）。"""
from __future__ import annotations

from app.models.analysis.forecast import (
    EPS,
    ForecastBucketResult,
    ForecastRequest,
    ForecastResponse,
)
from app.simulation import _round

# 不可达原因标记
REASON_COST_EXCEEDS_CAPACITY = "cost_exceeds_capacity"
REASON_ZERO_REFILL_RATE = "zero_refill_rate"


def forecast(payload: ForecastRequest) -> ForecastResponse:
    """纯计算：根据 now 时刻的桶状态快照，预测链上全部桶同时持有 >= cost 的最早时刻。

    单个桶 b 的等待时间：
    - cost > capacity        -> 永远不可达（cost_exceeds_capacity）
    - balance >= cost        -> 0（已满足，与补充速率无关）
    - refill_rate == 0       -> 永远不可达（zero_refill_rate）
    - 其余                   -> (cost - balance) / refill_rate

    链的等待时间为各桶等待的最大值；预计余额为该时刻连续补充后的余额
    （封顶 capacity），不做任何扣减。
    """
    capacity = {b.id: float(b.capacity) for b in payload.buckets}
    refill_rate = {b.id: float(b.refill_rate) for b in payload.buckets}
    balance = {b.id: float(b.balance) for b in payload.buckets}
    cost = float(payload.cost)

    waits: dict[str, float] = {}
    reasons: dict[str, str] = {}
    for bucket_id in payload.chain:
        if cost > capacity[bucket_id] + EPS:
            reasons[bucket_id] = REASON_COST_EXCEEDS_CAPACITY
        elif balance[bucket_id] + EPS >= cost:
            waits[bucket_id] = 0.0
        elif refill_rate[bucket_id] <= 0:
            reasons[bucket_id] = REASON_ZERO_REFILL_RATE
        else:
            waits[bucket_id] = (cost - balance[bucket_id]) / refill_rate[bucket_id]

    reachable = not reasons
    # 内部保留原始精度，仅在输出时舍入，避免回算预计余额时累积误差
    chain_wait_raw = max(waits[bucket_id] for bucket_id in payload.chain) if reachable else None
    chain_wait = _round(chain_wait_raw) if reachable else None

    # 等待最长的桶即限制桶；并列时取链上首个；无需等待（立即可通过）时无限制桶
    limiting_bucket: str | None = None
    if reachable and chain_wait > 0:
        for bucket_id in payload.chain:
            if waits[bucket_id] == chain_wait_raw:
                limiting_bucket = bucket_id
                break

    bucket_results = [
        ForecastBucketResult(
            id=bucket_id,
            balance_now=_round(balance[bucket_id]),
            reachable=bucket_id not in reasons,
            reason=reasons.get(bucket_id),
            wait_seconds=_round(waits[bucket_id]) if bucket_id in waits else None,
            projected_balance=(
                _round(
                    min(
                        capacity[bucket_id],
                        balance[bucket_id] + refill_rate[bucket_id] * chain_wait_raw,
                    )
                )
                if reachable
                else None
            ),
        )
        for bucket_id in payload.chain
    ]

    return ForecastResponse(
        reachable=reachable,
        wait_seconds=chain_wait,
        pass_at=_round(float(payload.now) + chain_wait_raw) if reachable else None,
        limiting_bucket=limiting_bucket,
        buckets=bucket_results,
    )
