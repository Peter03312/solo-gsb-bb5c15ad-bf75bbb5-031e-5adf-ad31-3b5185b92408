"""可借贷连续补充桶试算：允许借入缺口、债务先于余额得到偿还的链上原子扣减。

每个桶在普通令牌桶（capacity / refill_rate / initial）之外携带一个
累计债务上限 debt_limit：

- 时间推进时按 (now - last) · refill_rate 连续补充，**补充量先偿还债务**，
  偿还后剩余的部分才进入可用余额（余额仍封顶 capacity）；
- 判定一笔 cost 时，桶先把可用余额扣到 0，仍有缺口则允许借入，
  借入后累计债务不得超过 debt_limit，即可支付总额为
  balance + (debt_limit - debt)；
- cost > capacity + debt_limit 的请求对该桶永远不可达
  （cost_exceeds_limit）——即使满桶且零债务也无法支付；
- 其余当前无法在借贷上限内满足的情形以 debt_limit_exceeded 拒绝；
- 仅当链上每个桶都能在其借贷上限内满足 cost 时才原子扣减，否则链上
  所有桶保持不变（连续补充与债务偿还作为时间推进照常发生）。
"""
from __future__ import annotations

from typing import Literal

from app.models.common import EPS, BucketBalance
from app.models.debt.simulate import (
    DebtBucketBalance,
    DebtBucketFinalState,
    DebtRejection,
    DebtSimResult,
    DebtSimulateRequest,
    DebtSimulateResponse,
)
from app.simulation import _round


class _BucketState:
    """桶运行时状态：可用余额 balance、累计债务 debt 与上次更新时刻。"""

    __slots__ = ("balance", "debt", "last_time")

    def __init__(self, balance: float, debt: float, last_time: float):
        self.balance = balance
        self.debt = debt
        self.last_time = last_time


def replay_debt(payload: DebtSimulateRequest) -> dict:
    """按补充先偿债、限额内借债规则完整回放一次请求序列。

    返回原始（未取整）内部数据，供 /debt-simulate 与 /debt-diff 复用：

    - ``results_by_index``：按**原始输入索引**排列的逐项结果
      （DebtSimResult，数值已取整，与 /debt-simulate 完全一致）；
    - ``snapshots_by_index``：每个原始索引在该请求时刻（链上桶判定之后）
      **全部桶**的 (balance, debt) 原始快照——所有桶（含未被该请求链
      触及的桶）都已先连续补充/偿债到该时刻；
    - ``final_states``：按 buckets 输入顺序的终态原始数据
      ``(id, capacity, refill_rate, debt_limit, balance, debt)``，
      观察时刻为最后一个请求的时间戳。
    """
    buckets = payload.buckets
    requests = payload.requests

    capacity = {b.id: float(b.capacity) for b in buckets}
    refill_rate = {b.id: float(b.refill_rate) for b in buckets}
    debt_limit = {b.id: float(b.debt_limit) for b in buckets}
    bucket_ids = [b.id for b in buckets]

    # 与 /simulate 一致：模拟开始时刻 = 最早请求时间戳，所有桶在该时刻
    # 持有初始余额且无债务，时间戳因此可以使用任意一致的时间原点。
    t_start = min(float(request.timestamp) for request in requests)
    t_end = max(float(request.timestamp) for request in requests)

    states: dict[str, _BucketState] = {
        b.id: _BucketState(
            balance=float(b.capacity if b.initial is None else b.initial),
            debt=0.0,
            last_time=t_start,
        )
        for b in buckets
    }

    def advance(bucket_id: str, now: float) -> _BucketState:
        """把某个桶连续补充到时刻 now：补充量先偿还债务，余下进入余额。"""
        state = states[bucket_id]
        elapsed = now - state.last_time
        if elapsed > 0:
            refill = refill_rate[bucket_id] * elapsed
            # 先偿还债务；偿还后仍有剩余才增加可用余额，余额封顶容量
            repaid = min(state.debt, refill)
            state.debt -= repaid
            state.balance = min(
                capacity[bucket_id], state.balance + (refill - repaid)
            )
        state.last_time = now
        return state

    # 内部按时间顺序处理；同一时刻按原始输入顺序处理
    order = sorted(range(len(requests)), key=lambda i: (requests[i].timestamp, i))

    results_by_index: dict[int, DebtSimResult] = {}
    snapshots_by_index: dict[int, dict[str, tuple[float, float]]] = {}

    for i in order:
        request = requests[i]
        now = float(request.timestamp)
        cost = float(request.cost)

        # 先对链上所有桶做连续补充（无论最终是否放行，时间都会推进）
        before: dict[str, tuple[float, float]] = {}
        for bucket_id in request.chain:
            state = advance(bucket_id, now)
            before[bucket_id] = (state.balance, state.debt)

        # 链上第一个无法在借贷上限内满足成本的桶即拒绝桶：
        # cost > capacity + debt_limit 永远不可达；
        # 否则要求 可用余额 + 剩余借贷额度 >= cost。
        rejected_by: str | None = None
        reason: Literal["cost_exceeds_limit", "debt_limit_exceeded"] | None = None
        for bucket_id in request.chain:
            bal, debt = before[bucket_id]
            if cost > capacity[bucket_id] + debt_limit[bucket_id] + EPS:
                rejected_by, reason = bucket_id, "cost_exceeds_limit"
                break
            purchasable = bal + (debt_limit[bucket_id] - debt)
            if purchasable + EPS < cost:
                rejected_by, reason = bucket_id, "debt_limit_exceeded"
                break

        if rejected_by is None:
            # 全部通过：原子扣减。每桶先用可用余额支付，缺口计入债务。
            for bucket_id in request.chain:
                state = states[bucket_id]
                from_balance = min(state.balance, cost)
                state.balance -= from_balance
                state.debt += cost - from_balance

        bucket_views: dict[str, DebtBucketBalance] = {}
        for bucket_id in request.chain:
            state = states[bucket_id]
            bal_before, debt_before = before[bucket_id]
            if rejected_by is None:
                bal_after, debt_after = state.balance, state.debt
            else:
                # 原子性：拒绝时链上所有桶的余额/债务保持不变
                bal_after, debt_after = bal_before, debt_before
            bucket_views[bucket_id] = DebtBucketBalance(
                balance=BucketBalance(
                    before=_round(bal_before), after=_round(bal_after)
                ),
                debt=BucketBalance(
                    before=_round(debt_before), after=_round(debt_after)
                ),
            )

        rejection: DebtRejection | None = None
        if rejected_by is not None:
            bal_before, debt_before = before[rejected_by]
            remaining_credit = debt_limit[rejected_by] - debt_before
            available = bal_before + remaining_credit
            rejection = DebtRejection(
                bucket_id=rejected_by,
                reason=reason,
                capacity=_round(capacity[rejected_by]),
                debt_limit=_round(debt_limit[rejected_by]),
                cost=_round(cost),
                balance_available=_round(bal_before),
                debt_before=_round(debt_before),
                available=_round(available),
                remaining_credit=_round(remaining_credit),
                shortage=_round(cost - available),
            )

        results_by_index[i] = DebtSimResult(
            request_id=request.id,
            index=i,
            timestamp=_round(now),
            cost=_round(cost),
            allowed=rejected_by is None,
            buckets=bucket_views,
            rejected_by=rejected_by,
            rejection=rejection,
        )

        # /debt-diff 需要按桶对齐两组回放：把链外桶也推进到当前时刻，
        # 记录该请求时刻全部桶的余额/债务原始快照（仅用于差异比较）。
        snapshot: dict[str, tuple[float, float]] = {}
        for bucket_id in bucket_ids:
            state = advance(bucket_id, now)
            snapshot[bucket_id] = (state.balance, state.debt)
        snapshots_by_index[i] = snapshot

    # 循环结束时每个桶都在上一个处理请求中被推进过；再统一推进到
    # 最后时刻（链外桶在循环中已逐刻推进，此处通常无增量），给出终态。
    final_states: list[tuple[str, float, float, float, float, float]] = []
    for bucket_id in bucket_ids:
        state = advance(bucket_id, t_end)
        final_states.append(
            (
                bucket_id,
                capacity[bucket_id],
                refill_rate[bucket_id],
                debt_limit[bucket_id],
                state.balance,
                state.debt,
            )
        )

    return {
        "results_by_index": results_by_index,
        "snapshots_by_index": snapshots_by_index,
        "final_states": final_states,
    }


def debt_simulate(payload: DebtSimulateRequest) -> DebtSimulateResponse:
    replayed = replay_debt(payload)
    results_by_index = replayed["results_by_index"]

    results = [results_by_index[i] for i in range(len(payload.requests))]
    allowed_count = sum(1 for r in results if r.allowed)

    # 把所有未再参与请求的桶补充到最后时刻，给出终态快照
    final_buckets: list[DebtBucketFinalState] = []
    for bucket_id, cap, rate, dlimit, balance, debt in replayed["final_states"]:
        final_buckets.append(
            DebtBucketFinalState(
                id=bucket_id,
                capacity=_round(cap),
                refill_rate=_round(rate),
                debt_limit=_round(dlimit),
                balance=_round(balance),
                debt=_round(debt),
                available=_round(balance + (dlimit - debt)),
            )
        )

    return DebtSimulateResponse(
        allowed_count=allowed_count,
        rejected_count=len(results) - allowed_count,
        results=results,
        final_buckets=final_buckets,
    )
