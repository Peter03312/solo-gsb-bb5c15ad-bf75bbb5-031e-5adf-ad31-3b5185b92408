"""半开滑动窗口配额试算：多策略、多主体的链上原子记账。

每个策略有独立的窗口宽度 window_seconds 与累计消耗上限 limit；记账按
(policy, principal) 隔离——每个主体拥有独立的窗口额度。策略维护一份按时间
升序的记账事件（timestamp, cost）列表：

- 判定时刻 t 的半开窗口为 (t - window_seconds, t]：左端不含、右端含，
  恰好在左边界（event_time == t - window_seconds）的事件才过期；
- 判定前先移除窗口左端已过期事件，再统计窗口内该主体的累计消耗；
- 仅当请求引用的全部策略都满足 used + cost <= limit 时才原子记账
  （向每个策略追加同一事件），否则不改变任何策略，返回引用顺序上首个
  超限的策略作为拒绝原因。

边界与额度比较一律按输入十进制字面值做精确判定（Decimal），不使用任何
固定容差：紧邻左边界但仍在窗口内的事件（如 t=9.9999999995、w=10 时
event_time=0）继续占用额度，只有 event_time <= timestamp - window_seconds
成立的事件才被移除。
"""
from __future__ import annotations

from decimal import Decimal

from app.models.windows.sliding import (
    SlidingWindowRange,
    WindowPolicyFinalState,
    WindowPolicyView,
    WindowPrincipalOccupancy,
    WindowRejection,
    WindowSimResult,
    WindowSimulateRequest,
    WindowSimulateResponse,
)
from app.simulation import _round


def _exact(value: float) -> Decimal:
    """把已解析的 JSON 数值还原为其输入十进制字面值的精确 Decimal。

    请求体经标准 JSON 解析与 float 校验后，二进制浮点是输入十进制数的
    最短往返表示；``Decimal(repr(value))`` 取回调用方实际书写的十进制值
    （如 9.9999999995、0.1），边界比较因此按十进制字面值精确成立，
    不受二进制浮点毛刺影响，也不需要任何固定容差。
    """
    return Decimal(repr(value))


def _r(value: Decimal) -> float:
    """Decimal 内部数值经既有精度规则归一化为响应 float。"""
    return _round(float(value))


def window_simulate(payload: WindowSimulateRequest) -> WindowSimulateResponse:
    requests = payload.requests

    window_seconds = {p.id: _exact(float(p.window_seconds)) for p in payload.policies}
    limit = {p.id: _exact(float(p.limit)) for p in payload.policies}
    policy_ids = [p.id for p in payload.policies]

    # 已记账事件：events[policy_id][principal] = [(timestamp, cost), ...]，
    # 时间与消耗均为输入十进制字面值的精确 Decimal；请求按时间排序处理，
    # 故事件时间戳非降序。events 中只登记成功记账的主体（拒绝不产生任何痕迹）。
    events: dict[str, dict[str, list[tuple[Decimal, Decimal]]]] = {
        pid: {} for pid in policy_ids
    }
    # 每个策略上各主体首次成功记账的顺序，用于终态输出
    principal_order: dict[str, list[str]] = {pid: [] for pid in policy_ids}

    def active_events(pid: str, principal: str, now: Decimal) -> list[tuple[Decimal, Decimal]]:
        """返回 (now - w, now] 半开窗口内的事件，并就地移除左端已过期事件。

        事件按时间非降序排列，左端弹出即可。过期判定按输入十进制值精确
        比较（无容差）：event_time <= now - window_seconds 才移除——
        恰在左边界上的事件属于窗口外，紧邻边界但仍在窗口内的事件保留。
        """
        log = events[pid].get(principal)
        if not log:
            return []
        left = now - window_seconds[pid]
        while log and log[0][0] <= left:
            log.pop(0)
        return log

    # 内部按时间顺序处理；同一时刻按原始输入顺序处理
    order = sorted(
        range(len(requests)),
        key=lambda i: (_exact(float(requests[i].timestamp)), i),
    )

    results_by_index: dict[int, WindowSimResult] = {}

    for i in order:
        request = requests[i]
        now = _exact(float(request.timestamp))
        cost = _exact(float(request.cost))

        # 先对每个引用策略移除过期事件并统计窗口内占用（不改写，仅左端弹出）
        used_before: dict[str, Decimal] = {}
        for pid in request.policy_refs:
            used_before[pid] = sum(
                (c for _, c in active_events(pid, request.principal, now)),
                Decimal(0),
            )

        # 引用顺序上首个 used + cost 超过 limit 的策略即拒绝策略
        rejected_pid: str | None = None
        for pid in request.policy_refs:
            if used_before[pid] + cost > limit[pid]:
                rejected_pid = pid
                break

        if rejected_pid is None:
            # 全部通过：原子记账——一次性向所有引用策略追加事件。
            # 被拒请求从不产生账本（events 中查不到），因此这里查不到的主体
            # 一定是首次成功记账；旧事件全部过期后重新记账时账本仍存在（为空）。
            for pid in request.policy_refs:
                per_principal = events[pid]
                log = per_principal.get(request.principal)
                if log is None:
                    log = []
                    per_principal[request.principal] = log
                    principal_order[pid].append(request.principal)
                log.append((now, cost))

        def _window(pid: str) -> SlidingWindowRange:
            return SlidingWindowRange(
                start=_r(now - window_seconds[pid]),
                end=_r(now),
            )

        policy_views: dict[str, WindowPolicyView] = {}
        for pid in request.policy_refs:
            if rejected_pid is None:
                used_after = used_before[pid] + cost
            else:
                # 原子性：拒绝时所有策略保持不变
                used_after = used_before[pid]
            policy_views[pid] = WindowPolicyView(
                id=pid,
                window_seconds=_r(window_seconds[pid]),
                limit=_r(limit[pid]),
                window=_window(pid),
                used_before=_r(used_before[pid]),
                used_after=_r(used_after),
                remaining_before=_r(limit[pid] - used_before[pid]),
                remaining_after=_r(limit[pid] - used_after),
            )

        rejection: WindowRejection | None = None
        if rejected_pid is not None:
            used = used_before[rejected_pid]
            rejection = WindowRejection(
                policy_id=rejected_pid,
                reason="limit_exceeded",
                window_seconds=_r(window_seconds[rejected_pid]),
                limit=_r(limit[rejected_pid]),
                used=_r(used),
                cost=_r(cost),
                required=_r(used + cost),
                available=_r(limit[rejected_pid] - used),
                window=_window(rejected_pid),
            )

        results_by_index[i] = WindowSimResult(
            request_id=request.id,
            index=i,
            timestamp=_r(now),
            principal=request.principal,
            cost=_r(cost),
            allowed=rejected_pid is None,
            policies=policy_views,
            rejected_by=rejected_pid,
            rejection=rejection,
        )

    results = [results_by_index[i] for i in range(len(requests))]
    allowed_count = sum(1 for r in results if r.allowed)

    # 终态：以最后一个请求时间戳为观察时刻，再对每个策略移除一次过期事件
    t_end = max(_exact(float(request.timestamp)) for request in requests)

    final_states: list[WindowPolicyFinalState] = []
    for pid in policy_ids:
        final_window = SlidingWindowRange(
            start=_r(t_end - window_seconds[pid]),
            end=_r(t_end),
        )
        principal_rows: list[WindowPrincipalOccupancy] = []
        used_rows: list[Decimal] = []
        for principal in principal_order[pid]:
            log = active_events(pid, principal, t_end)
            # 从未成功记账或终态窗口内事件全部过期的主体不输出
            if not log:
                continue
            used = sum((c for _, c in log), Decimal(0))
            used_rows.append(used)
            principal_rows.append(
                WindowPrincipalOccupancy(
                    principal=principal,
                    event_count=len(log),
                    used=_r(used),
                    remaining=_r(limit[pid] - used),
                    window=final_window,
                )
            )
        total_used = sum(used_rows, Decimal(0))
        final_states.append(
            WindowPolicyFinalState(
                id=pid,
                window_seconds=_r(window_seconds[pid]),
                limit=_r(limit[pid]),
                total_used=_r(total_used),
                window=final_window,
                principals=principal_rows,
            )
        )

    return WindowSimulateResponse(
        allowed_count=allowed_count,
        rejected_count=len(results) - allowed_count,
        results=results,
        policies=final_states,
    )
