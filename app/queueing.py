"""严格 FIFO 排队试算：队首独占执行权，等待至最早可执行时刻或 deadline。

与 /simulate 共用同一套连续补充令牌桶语义（惰性补充、封顶 capacity、
初始余额在最早请求时刻持有、时间戳可用任意一致原点），区别在于请求按
（到达时间, 输入顺序）排入严格 FIFO 队列，仅队首可执行：

- 队首从成为队首的时刻 t0（自身到达与前序队首出队的较晚者）起等待；
  链上各桶连续补充后全部满足 cost 的最早时刻即最早可执行时刻 t_exec
  （成本超过容量或零补充且余额不足的桶永远不可达，t_exec 为无穷大）；
- t_exec 不晚于 deadline 时在 t_exec 原子扣减放行——即 deadline 时刻
  余额仍充足也一定会执行；
- 否则等待至 deadline 标记 expired 出队（不可达的队首同样等到
  deadline），链上桶时间推进但不扣减，随后继续处理后继；
- 等待期间到达的请求排入队尾，不得越过队首；事件按队首最早可执行
  时刻、下一次到达与 deadline 推进。
"""
from __future__ import annotations

import math
from typing import Literal

from app.models.common import EPS, BucketBalance
from app.models.scheduling.queueing import (
    QueueSimResult,
    QueueSimulateRequest,
    QueueSimulateResponse,
)
from app.simulation import _round


def queue_simulate(payload: QueueSimulateRequest) -> QueueSimulateResponse:
    if not payload.buckets:
        raise ValueError("buckets 不能为空")
    if not payload.requests:
        raise ValueError("requests 不能为空")

    # 与 /simulate 一致：所有桶在最早请求时刻持有初始余额，之后按
    # (now - last) * rate 连续补充，上限为容量；时间戳可用任意一致原点。
    t_start = min(request.timestamp for request in payload.requests)

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

    # 严格 FIFO：按（到达时间, 输入顺序）排队，仅队首可执行
    order = sorted(
        range(len(payload.requests)),
        key=lambda i: (payload.requests[i].timestamp, i),
    )

    results_by_index: dict[int, QueueSimResult] = {}
    t_resolved = t_start  # 上一个队首出队（执行或过期）的时刻；出队时刻单调不减

    for i in order:
        request = payload.requests[i]
        arrival = float(request.timestamp)
        deadline = float(request.deadline)
        cost = float(request.cost)

        # 成为队首的时刻：已到达且前序队首已出队的较晚者；
        # 等待期间到达的请求只在队尾排队，不得越过队首
        t0 = max(arrival, t_resolved)

        # 链上各桶补充到 t0，计算各自最早满足 cost 的时刻
        ready_at: dict[str, float] = {}
        for bucket_id in request.chain:
            current = refill(bucket_id, t0)
            if current + EPS >= cost:
                ready_at[bucket_id] = t0
            elif cost > capacity[bucket_id] + EPS or refill_rate[bucket_id] <= 0:
                # 成本超过容量或零补充：永远不可达，同样等待至 deadline
                ready_at[bucket_id] = math.inf
            else:
                ready_at[bucket_id] = t0 + (cost - current) / refill_rate[bucket_id]

        # 队首最早可执行时刻 = 链上各桶就绪时刻的最大值
        t_exec = max(ready_at[bucket_id] for bucket_id in request.chain)

        # 限制桶：就绪最晚的桶（并列取链上首个）；成为队首即可执行时为 None
        limiting_bucket: str | None = None
        if t_exec > t0 + EPS:
            for bucket_id in request.chain:
                if ready_at[bucket_id] == t_exec:
                    limiting_bucket = bucket_id
                    break

        status: Literal["executed", "expired"]
        if t_exec <= deadline + EPS:
            # deadline 前（含 deadline 时刻）可达：在 t_exec 原子扣减
            resolved_at = t_exec
            status = "executed"
            refilled = {bucket_id: refill(bucket_id, t_exec) for bucket_id in request.chain}
            for bucket_id in request.chain:
                balance[bucket_id] = refilled[bucket_id] - cost
        else:
            # 等待至 deadline 仍不可执行：过期出队，链上桶时间推进但不扣减；
            # 若成为队首时 deadline 已过，则立即过期
            resolved_at = max(deadline, t0)
            status = "expired"
            refilled = {
                bucket_id: refill(bucket_id, resolved_at) for bucket_id in request.chain
            }

        t_resolved = resolved_at

        results_by_index[i] = QueueSimResult(
            request_id=request.id,
            index=i,
            timestamp=_round(arrival),
            deadline=_round(deadline),
            status=status,
            executed_at=_round(resolved_at) if status == "executed" else None,
            expired_at=_round(resolved_at) if status == "expired" else None,
            wait_seconds=_round(resolved_at - arrival),
            limiting_bucket=limiting_bucket,
            balances={
                bucket_id: BucketBalance(
                    before=_round(refilled[bucket_id]),
                    # 过期时不扣减，after 与 before 相同（原子性）
                    after=_round(balance[bucket_id]),
                )
                for bucket_id in request.chain
            },
        )

    # 把所有桶补充到最后一个请求的出队时刻，给出终态余额
    final_balances = {
        bucket_id: _round(
            min(
                capacity[bucket_id],
                balance[bucket_id]
                + refill_rate[bucket_id] * max(0.0, t_resolved - last_time[bucket_id]),
            )
        )
        for bucket_id in balance
    }

    results = [results_by_index[i] for i in range(len(payload.requests))]
    executed_count = sum(1 for r in results if r.status == "executed")

    return QueueSimulateResponse(
        executed_count=executed_count,
        expired_count=len(results) - executed_count,
        results=results,
        final_balances=final_balances,
    )
