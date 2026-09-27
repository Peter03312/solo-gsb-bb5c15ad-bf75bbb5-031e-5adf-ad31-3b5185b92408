"""周期结转额度试算：多策略的周期性额度 + 跨期结转链上原子扣减。

每个策略由周期锚点 anchor、周期长度 period_seconds、每周期基础额度
allowance 与结转上限 carry_cap 定义：

- 时刻 t 所在周期编号为 floor((t - anchor) / period_seconds)，周期 k 的
  半开区间为 [anchor + k·period_seconds, anchor + (k+1)·period_seconds)。
  周期归属按输入的**十进制数值**精确判定（分数运算求 floor），不受二进制
  浮点误差影响：period_seconds 可以是小数，边界上的请求进入右侧新周期
  （如 anchor=0、period_seconds=0.1 时 t=0.3 属于周期 3 而非周期 2）；
- 策略在**首次被请求触及的那个周期**开始时拥有 base = allowance、carry = 0；
- 时间推进到更后的周期时逐期结转：旧 carry 立即失效，上一周期未用的基础
  额度结转为新周期的 carry（不超过 carry_cap），新周期的 base 重置为
  allowance；跨多期时结转只在相邻周期间传递，两期即收敛（空转周期的
  base 全额结转，随后只剩 min(carry_cap, allowance) 这一不动点）；
- 消费先扣即将失效的结转余额 carry，不足部分再扣本期基础额度 base；
- 仅当请求引用的全部策略两类余额合计都足够时才原子扣减，否则所有策略
  保持不变（跨期结转作为时间推进照常发生），并返回 policy_refs 顺序上
  首个余额不足的策略。
"""
from __future__ import annotations

from fractions import Fraction

from app.models.common import EPS, BucketBalance
from app.models.windows.rollover import (
    PeriodRange,
    RolloverBalances,
    RolloverPolicyFinalState,
    RolloverPolicyView,
    RolloverRejection,
    RolloverSimResult,
    RolloverSimulateRequest,
    RolloverSimulateResponse,
)
from app.simulation import _round


def _exact_time(value: float) -> Fraction:
    """输入时间/周期数值的十进制精确分数值。

    JSON 数值解析为 float 后，str() 给出最短回环十进制表示（即调用方
    输入的十进制值），据此构造的分数让周期归属判定不受二进制浮点误差
    影响：0.3 / 0.1 恰好为 3，而不是 2.9999999999999996。
    """
    return Fraction(str(value))


class _PolicyState:
    """策略运行时状态：当前所在周期 period 与两类余额 base / carry。"""

    __slots__ = ("period", "base", "carry")

    def __init__(self, period: int, base: float, carry: float = 0.0):
        self.period = period
        self.base = base
        self.carry = carry


def rollover_simulate(payload: RolloverSimulateRequest) -> RolloverSimulateResponse:
    requests = payload.requests

    anchor = {p.id: float(p.anchor) for p in payload.policies}
    period_seconds = {p.id: float(p.period_seconds) for p in payload.policies}
    allowance = {p.id: float(p.allowance) for p in payload.policies}
    carry_cap = {p.id: float(p.carry_cap) for p in payload.policies}
    policy_ids = [p.id for p in payload.policies]

    # 周期归属与周期区间按十进制精确值计算，保证小数周期（如 0.1 秒）的
    # 边界时刻恰好整除、归入右侧新周期
    anchor_x = {p.id: _exact_time(p.anchor) for p in payload.policies}
    period_x = {p.id: _exact_time(p.period_seconds) for p in payload.policies}

    def period_index(pid: str, now: Fraction) -> int:
        # Fraction 的 // 即 floor 语义（负周期编号同样向负无穷取整）
        return (now - anchor_x[pid]) // period_x[pid]

    def period_window(pid: str, index: int) -> PeriodRange:
        start = anchor_x[pid] + index * period_x[pid]
        return PeriodRange(
            index=index,
            start=_round(float(start)),
            end=_round(float(start + period_x[pid])),
        )

    # 延迟初始化：策略首次被请求触及时，按其所在周期以满额起步、无结转
    states: dict[str, _PolicyState] = {}

    def advance(pid: str, now: Fraction) -> _PolicyState:
        """把策略推进到 now 所在周期（惰性跨期结转），返回最新状态。"""
        target = period_index(pid, now)
        state = states.get(pid)
        if state is None:
            states[pid] = _PolicyState(target, allowance[pid], 0.0)
            return states[pid]

        if target <= state.period:
            return state

        # 逐期结转。旧 carry 一律失效；上期未用 base 结转且不超过 carry_cap，
        # 新周期 base 重置为满额。跨 ≥ 2 个空转周期后进入不动点
        # carry = min(carry_cap, allowance)，可直接跳到目标周期，
        # 避免时间戳跨越极大周期数时逐期循环。
        if target - state.period >= 2:
            state.period = target
            state.carry = min(carry_cap[pid], allowance[pid])
            state.base = allowance[pid]
        else:
            while state.period < target:
                state.carry = min(carry_cap[pid], state.base)
                state.base = allowance[pid]
                state.period += 1
        return state

    # 内部按时间顺序处理；同一时刻按原始输入顺序处理
    order = sorted(range(len(requests)), key=lambda i: (requests[i].timestamp, i))

    results_by_index: dict[int, RolloverSimResult] = {}

    for i in order:
        request = requests[i]
        now = float(request.timestamp)
        now_x = _exact_time(now)
        cost = float(request.cost)

        # 先把每个引用策略推进到当前周期（跨期结转作为时间推进，拒绝也会发生）
        before: dict[str, tuple[float, float]] = {}
        periods: dict[str, int] = {}
        for pid in request.policy_refs:
            state = advance(pid, now_x)
            before[pid] = (state.base, state.carry)
            periods[pid] = state.period

        # 引用顺序上首个两类余额合计不足的策略即拒绝策略
        rejected_pid: str | None = None
        for pid in request.policy_refs:
            base_before, carry_before = before[pid]
            if base_before + carry_before + EPS < cost:
                rejected_pid = pid
                break

        if rejected_pid is None:
            # 全部通过：原子扣减。逐策略先扣即将失效的 carry，再扣本期 base。
            for pid in request.policy_refs:
                state = states[pid]
                from_carry = min(state.carry, cost)
                state.carry -= from_carry
                state.base -= cost - from_carry

        policy_views: dict[str, RolloverPolicyView] = {}
        for pid in request.policy_refs:
            state = states[pid]
            base_before, carry_before = before[pid]
            if rejected_pid is None:
                base_after, carry_after = state.base, state.carry
            else:
                # 原子性：拒绝时所有策略余额保持不变
                base_after, carry_after = base_before, carry_before
            policy_views[pid] = RolloverPolicyView(
                id=pid,
                period=periods[pid],
                period_window=period_window(pid, periods[pid]),
                allowance=_round(allowance[pid]),
                carry_cap=_round(carry_cap[pid]),
                balances=RolloverBalances(
                    base=BucketBalance(
                        before=_round(base_before), after=_round(base_after)
                    ),
                    carry=BucketBalance(
                        before=_round(carry_before), after=_round(carry_after)
                    ),
                ),
            )

        rejection: RolloverRejection | None = None
        if rejected_pid is not None:
            base_before, carry_before = before[rejected_pid]
            available = base_before + carry_before
            rejection = RolloverRejection(
                policy_id=rejected_pid,
                reason="insufficient_balance",
                period=periods[rejected_pid],
                period_window=period_window(rejected_pid, periods[rejected_pid]),
                allowance=_round(allowance[rejected_pid]),
                carry_cap=_round(carry_cap[rejected_pid]),
                cost=_round(cost),
                base_available=_round(base_before),
                carry_available=_round(carry_before),
                available=_round(available),
                shortage=_round(cost - available),
            )

        results_by_index[i] = RolloverSimResult(
            request_id=request.id,
            index=i,
            timestamp=_round(now),
            cost=_round(cost),
            allowed=rejected_pid is None,
            policies=policy_views,
            rejected_by=rejected_pid,
            rejection=rejection,
        )

    results = [results_by_index[i] for i in range(len(requests))]
    allowed_count = sum(1 for r in results if r.allowed)

    # 终态：以最后一个请求时间戳为观察时刻，各策略推进到其所在周期
    t_end = max(float(request.timestamp) for request in requests)
    t_end_x = _exact_time(t_end)

    final_states: list[RolloverPolicyFinalState] = []
    for pid in policy_ids:
        state = advance(pid, t_end_x)
        final_states.append(
            RolloverPolicyFinalState(
                id=pid,
                anchor=_round(anchor[pid]),
                period_seconds=_round(period_seconds[pid]),
                allowance=_round(allowance[pid]),
                carry_cap=_round(carry_cap[pid]),
                period=state.period,
                period_window=period_window(pid, state.period),
                base=_round(state.base),
                carry=_round(state.carry),
                available=_round(state.base + state.carry),
            )
        )

    return RolloverSimulateResponse(
        allowed_count=allowed_count,
        rejected_count=len(results) - allowed_count,
        results=results,
        policies=final_states,
    )
