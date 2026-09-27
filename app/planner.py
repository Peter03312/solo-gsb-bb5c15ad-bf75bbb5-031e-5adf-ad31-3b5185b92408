"""初始余额规划：以全部请求放行为目标，反推每个桶所需的最低 initial。"""
from __future__ import annotations

from app.models.common import EPS
from app.models.simulation.core import BucketSpec, SimulateRequest
from app.models.simulation.planner import (
    PlanBucketResult,
    PlanFailure,
    PlanInitialBalancesResponse,
)
from app.simulation import _round, simulate


def plan_initial_balances(payload: SimulateRequest) -> PlanInitialBalancesResponse:
    """为每个桶反推 [0, capacity] 内满足其全部链上消耗的最低初始余额。

    规划语义与 /simulate 共享时间排序（时间戳升序、同刻按输入顺序）与连续补充
    规则，但假设**每个请求都会放行**：链上消耗按目标全量计入，不因其他桶
    暂时不足而跳过。因此各桶相互独立：

    1. 满桶起步回放（initial = capacity，全量扣减）判定可行性；由于满桶轨迹
       处处不劣于任何其他初始值，首个余额不足的 (请求, 桶) 即不可行的证据。
    2. 可行时逐桶反向递推：从最后一个消耗事件起，
       need_before = need_after + cost，再跨过补充区间
       need_prev = max(0, need_before - rate * dt)，递推到模拟起始时刻即
       最低 initial。
    3. 用规划值完整回放（复用 simulate），给出终态余额。
    """
    requests = payload.requests
    # 与模拟引擎一致：所有桶在最早请求时刻持有初始余额
    t_start = min(request.timestamp for request in requests)

    capacity = {b.id: float(b.capacity) for b in payload.buckets}
    refill_rate = {b.id: float(b.refill_rate) for b in payload.buckets}
    requested_initial = {
        b.id: float(b.capacity if b.initial is None else b.initial) for b in payload.buckets
    }

    # 内部按时间顺序处理；同一时刻按原始输入顺序处理
    order = sorted(
        range(len(requests)),
        key=lambda i: (requests[i].timestamp, i),
    )

    # ---- 第一步：满桶起步的可行性回放 ----
    # 链上消耗按目标全量计入：链上所有桶都够才扣（与模拟相同的判定），
    # 首个（处理顺序、链顺序）余额不足的 (请求, 桶) 即不可行证据。
    balance = {bucket_id: capacity[bucket_id] for bucket_id in capacity}
    last_time = {bucket_id: t_start for bucket_id in capacity}
    first_failure: PlanFailure | None = None

    for i in order:
        request = requests[i]
        now = request.timestamp

        for bucket_id in request.chain:
            elapsed = now - last_time[bucket_id]
            if elapsed > 0:
                balance[bucket_id] = min(
                    capacity[bucket_id],
                    balance[bucket_id] + refill_rate[bucket_id] * elapsed,
                )
            last_time[bucket_id] = now

        failing: str | None = None
        for bucket_id in request.chain:
            if balance[bucket_id] + EPS < request.cost:
                failing = bucket_id
                break

        if failing is not None:
            first_failure = PlanFailure(
                request_id=request.id,
                index=i,
                timestamp=_round(now),
                bucket_id=failing,
                cost=_round(float(request.cost)),
                available=_round(balance[failing]),
            )
            break

        for bucket_id in request.chain:
            balance[bucket_id] -= request.cost

    if first_failure is not None:
        return PlanInitialBalancesResponse(
            reachable=False,
            buckets=None,
            final_balances=None,
            first_failure=first_failure,
        )

    # ---- 第二步：逐桶反向递推最低 initial ----
    # 桶的事件 = 所有 chain 引用该桶的请求（处理顺序），每个事件在判定时刻
    # 要求补充后的余额 >= cost。从未被引用的桶没有事件，最低 initial 为 0。
    events: dict[str, list[tuple[float, float]]] = {b.id: [] for b in payload.buckets}
    for i in order:
        request = requests[i]
        for bucket_id in request.chain:
            events[bucket_id].append((request.timestamp, float(request.cost)))

    planned: dict[str, float] = {}
    for bucket in payload.buckets:
        bucket_id = bucket.id
        bucket_events = events[bucket_id]
        need = 0.0  # 当前事件之后（含后续所有事件）所需的余额
        for k in range(len(bucket_events) - 1, -1, -1):
            t_event, cost = bucket_events[k]
            need += cost
            t_prev = bucket_events[k - 1][0] if k > 0 else t_start
            need = max(0.0, need - refill_rate[bucket_id] * (t_event - t_prev))
        # 可行性已保证 need <= capacity（满桶起步可通过）；clamp 仅防浮点毛刺
        planned[bucket_id] = _round(min(capacity[bucket_id], need))

    # ---- 第三步：用规划值完整回放，给出终态余额 ----
    replay = simulate(
        SimulateRequest(
            buckets=[
                BucketSpec(
                    id=b.id,
                    capacity=b.capacity,
                    refill_rate=b.refill_rate,
                    initial=planned[b.id],
                )
                for b in payload.buckets
            ],
            requests=payload.requests,
        )
    )

    return PlanInitialBalancesResponse(
        reachable=True,
        buckets=[
            PlanBucketResult(
                id=b.id,
                required_initial=planned[b.id],
                requested_initial=_round(requested_initial[b.id]),
                delta=_round(planned[b.id] - requested_initial[b.id]),
            )
            for b in payload.buckets
        ],
        final_balances=replay.final_balances,
        first_failure=None,
    )
