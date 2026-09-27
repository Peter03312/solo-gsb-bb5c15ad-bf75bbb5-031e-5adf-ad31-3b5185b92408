"""候选链路由试算：在共享连续补充令牌桶上为每个请求挑选一条候选链。

与 /simulate 共用同一套连续补充令牌桶语义（惰性补充、封顶 capacity、
按时间戳升序 + 输入顺序回放），区别在于每个请求携带多条候选桶链：

- 一条候选链仅当链上**每个桶**余额都 >= cost 时才可被选；
- 在可行候选中，按"扣减后链上最大占用率最小"选择——占用率 =
  (capacity - 扣减后余额) / capacity，取链上各桶最大值；并列时按候选
  顺序取在前者；
- 放行时仅扣选中链上的桶（每个桶扣 cost），其余桶保持不变；
- 无候选可行时原子拒绝：所有桶保持不变，并逐候选给出首个不足桶。
"""
from __future__ import annotations

from app.models.common import EPS, BucketBalance
from app.models.scheduling.routing import (
    RouteCandidateRejection,
    RouteSimResult,
    RouteSimulateRequest,
    RouteSimulateResponse,
)
from app.simulation import _round


def route_simulate(payload: RouteSimulateRequest) -> RouteSimulateResponse:
    if not payload.buckets:
        raise ValueError("buckets 不能为空")
    if not payload.requests:
        raise ValueError("requests 不能为空")

    # 与 /simulate 一致：所有桶在最早请求时刻持有初始余额，之后按
    # (now - last) * rate 连续补充，上限为容量；时间戳可用任意一致原点。
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

    results_by_index: dict[int, RouteSimResult] = {}

    for i in order:
        request = payload.requests[i]
        now = request.timestamp
        cost = float(request.cost)

        # 本请求触及的桶 = 全部候选链引用桶的并集（按首次出现顺序）；
        # 无论最终放行与否，这些桶的时间都会推进到 now
        touched = list(dict.fromkeys(bid for chain in request.chains for bid in chain))
        refilled = {bucket_id: refill(bucket_id, now) for bucket_id in touched}

        # 逐候选评估：全部充足才可选；否则记录该候选首个不足桶
        eligible: list[int] = []
        rejections: list[RouteCandidateRejection] = []
        for c_index, chain in enumerate(request.chains):
            first_short: str | None = None
            for bucket_id in chain:
                if refilled[bucket_id] + EPS < cost:
                    first_short = bucket_id
                    break
            if first_short is None:
                eligible.append(c_index)
            else:
                rejections.append(
                    RouteCandidateRejection(
                        chain_index=c_index,
                        rejected_by=first_short,
                        available=_round(refilled[first_short]),
                    )
                )

        selected_index: int | None = None
        if eligible:
            # 扣减后链上最大占用率 = max((capacity - (balance - cost)) / capacity)；
            # 最小者胜出，并列（EPS 容差内）按候选顺序取在前者
            def occupancy_after(c_index: int) -> float:
                return max(
                    (capacity[bid] - (refilled[bid] - cost)) / capacity[bid]
                    for bid in request.chains[c_index]
                )

            selected_index = eligible[0]
            best_score = occupancy_after(selected_index)
            for c_index in eligible[1:]:
                score = occupancy_after(c_index)
                if score < best_score - EPS:
                    selected_index = c_index
                    best_score = score

            # 原子扣减：仅扣选中链上的桶
            for bucket_id in request.chains[selected_index]:
                balance[bucket_id] = refilled[bucket_id] - cost

        balances_view = {
            bucket_id: BucketBalance(
                before=_round(refilled[bucket_id]),
                # 拒绝时所有桶保持补充后的余额不变（原子性）
                after=_round(balance[bucket_id]),
            )
            for bucket_id in touched
        }

        results_by_index[i] = RouteSimResult(
            request_id=request.id,
            index=i,
            timestamp=_round(now),
            allowed=selected_index is not None,
            selected_chain_index=selected_index,
            selected_chain=(
                list(request.chains[selected_index]) if selected_index is not None else None
            ),
            balances=balances_view,
            rejections=rejections if selected_index is None else None,
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

    return RouteSimulateResponse(
        allowed_count=allowed_count,
        rejected_count=len(results) - allowed_count,
        results=results,
        final_balances=final_balances,
    )
