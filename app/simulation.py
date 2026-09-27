"""核心模拟引擎：连续补充 + 链上原子扣减。"""
from __future__ import annotations

from app.models.common import EPS, BucketBalance
from app.models.simulation.core import SimResult, SimulateRequest, SimulateResponse

# 输出精度，避免二进制浮点产生的长尾数
_ROUND_DIGITS = 9


def _round(value: float) -> float:
    rounded = round(value, _ROUND_DIGITS)
    # 规范化 -0.0
    return rounded if rounded != 0 else 0.0


def simulate(payload: SimulateRequest) -> SimulateResponse:
    if not payload.buckets:
        raise ValueError("buckets 不能为空")
    if not payload.requests:
        raise ValueError("requests 不能为空")

    # 模拟开始时刻 = 最早请求时间戳：所有桶在该时刻持有初始余额，
    # 之后按 (now - last) * rate 连续补充，上限为容量。时间戳因此可以
    # 使用任意一致的时间原点（Unix 时间、相对秒数均可）。
    t_start = min(request.timestamp for request in payload.requests)
    t_end = max(request.timestamp for request in payload.requests)

    capacity = {b.id: float(b.capacity) for b in payload.buckets}
    refill_rate = {b.id: float(b.refill_rate) for b in payload.buckets}
    balance = {
        b.id: float(b.capacity if b.initial is None else b.initial) for b in payload.buckets
    }
    last_time = {b.id: t_start for b in payload.buckets}

    def refill(bucket_id: str, now: float) -> float:
        """把某个桶补充到时刻 now，返回补充后的余额。"""
        elapsed = now - last_time[bucket_id]
        if elapsed > 0:
            balance[bucket_id] = min(
                capacity[bucket_id], balance[bucket_id] + refill_rate[bucket_id] * elapsed
            )
        last_time[bucket_id] = now
        return balance[bucket_id]

    # 内部按时间顺序处理；同一时刻按原始输入顺序处理。
    order = sorted(
        range(len(payload.requests)),
        key=lambda i: (payload.requests[i].timestamp, i),
    )

    results_by_index: dict[int, SimResult] = {}

    for i in order:
        request = payload.requests[i]
        now = request.timestamp

        # 先对链上所有桶做连续补充（无论最终是否放行，时间都会推进）
        refilled = {bucket_id: refill(bucket_id, now) for bucket_id in request.chain}

        # 链上第一个余额不足的桶即限制桶；必须全部充足才放行
        rejected_by: str | None = None
        for bucket_id in request.chain:
            if refilled[bucket_id] + EPS < request.cost:
                rejected_by = bucket_id
                break

        allowed = rejected_by is None
        balances_view: dict[str, BucketBalance] = {}

        for bucket_id in request.chain:
            before = refilled[bucket_id]
            if allowed:
                balance[bucket_id] = before - request.cost
                after = balance[bucket_id]
            else:
                # 原子性：拒绝时链上所有桶保持不变
                after = before
            balances_view[bucket_id] = BucketBalance(
                before=_round(before), after=_round(after)
            )

        results_by_index[i] = SimResult(
            request_id=request.id,
            index=i,
            timestamp=_round(now),
            allowed=allowed,
            rejected_by=rejected_by,
            balances=balances_view,
        )

    # 把所有未再参与请求的桶补充到最后时刻，给出终态余额
    final_balances = {
        bucket_id: _round(
            min(
                capacity[bucket_id],
                balance[bucket_id]
                + refill_rate[bucket_id] * max(0.0, t_end - last_time[bucket_id]),
            )
        )
        for bucket_id in balance
    }

    results = [results_by_index[i] for i in range(len(payload.requests))]
    allowed_count = sum(1 for r in results if r.allowed)

    return SimulateResponse(
        allowed_count=allowed_count,
        rejected_count=len(results) - allowed_count,
        results=results,
        final_balances=final_balances,
    )
