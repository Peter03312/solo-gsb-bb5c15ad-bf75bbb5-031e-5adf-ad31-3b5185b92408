"""半开滑动窗口配额试算：多策略、多主体的链上原子记账。

每个策略有独立的窗口宽度 window_seconds 与累计消耗上限 limit；记账按
(policy, principal) 隔离——每个主体拥有独立的窗口额度。策略维护一份按时间
升序的记账事件（timestamp, cost）列表：

- 判定时刻 t 的半开窗口为 (t - window_seconds, t]：左端不含、右端含，
  恰好在左边界到期（event_time == t - window_seconds）的事件已过期；
- 判定前先移除窗口左端已过期事件，再统计窗口内该主体的累计消耗；
- 仅当请求引用的全部策略都满足 used + cost <= limit 时才原子记账
  （向每个策略追加同一事件），否则不改变任何策略，返回引用顺序上首个
  超限的策略作为拒绝原因。
"""
from __future__ import annotations

from app.models.common import EPS
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


def window_simulate(payload: WindowSimulateRequest) -> WindowSimulateResponse:
    requests = payload.requests

    window_seconds = {p.id: float(p.window_seconds) for p in payload.policies}
    limit = {p.id: float(p.limit) for p in payload.policies}
    policy_ids = [p.id for p in payload.policies]

    # 已记账事件：events[policy_id][principal] = [(timestamp, cost), ...]，
    # 事件按处理顺序追加；请求按时间排序处理，故时间戳非降序。events 中只登记
    # 成功记账的主体（拒绝不产生任何痕迹）。
    events: dict[str, dict[str, list[tuple[float, float]]]] = {
        pid: {} for pid in policy_ids
    }
    # 每个策略上各主体首次成功记账的顺序，用于终态输出
    principal_order: dict[str, list[str]] = {pid: [] for pid in policy_ids}

    def active_events(pid: str, principal: str, now: float) -> list[tuple[float, float]]:
        """返回 (now - w, now] 半开窗口内的事件，并就地移除左端已过期事件。

        事件按时间非降序排列，左端弹出即可；EPS 吸附浮点毛刺，保证
        event_time 恰为左边界（半开左端不含）时一定被移除。
        """
        log = events[pid].get(principal)
        if not log:
            return []
        left = now - window_seconds[pid]
        while log and log[0][0] <= left + EPS:
            log.pop(0)
        return log

    # 内部按时间顺序处理；同一时刻按原始输入顺序处理
    order = sorted(range(len(requests)), key=lambda i: (requests[i].timestamp, i))

    results_by_index: dict[int, WindowSimResult] = {}

    for i in order:
        request = requests[i]
        now = float(request.timestamp)
        cost = float(request.cost)

        # 先对每个引用策略移除过期事件并统计窗口内占用（不改写，仅左端弹出）
        used_before: dict[str, float] = {}
        for pid in request.policy_refs:
            used_before[pid] = sum(c for _, c in active_events(pid, request.principal, now))

        # 引用顺序上首个 used + cost 超过 limit 的策略即拒绝策略
        rejected_pid: str | None = None
        for pid in request.policy_refs:
            if used_before[pid] + cost > limit[pid] + EPS:
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
                start=_round(now - window_seconds[pid]),
                end=_round(now),
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
                window_seconds=_round(window_seconds[pid]),
                limit=_round(limit[pid]),
                window=_window(pid),
                used_before=_round(used_before[pid]),
                used_after=_round(used_after),
                remaining_before=_round(limit[pid] - used_before[pid]),
                remaining_after=_round(limit[pid] - used_after),
            )

        rejection: WindowRejection | None = None
        if rejected_pid is not None:
            used = used_before[rejected_pid]
            rejection = WindowRejection(
                policy_id=rejected_pid,
                reason="limit_exceeded",
                window_seconds=_round(window_seconds[rejected_pid]),
                limit=_round(limit[rejected_pid]),
                used=_round(used),
                cost=_round(cost),
                required=_round(used + cost),
                available=_round(limit[rejected_pid] - used),
                window=_window(rejected_pid),
            )

        results_by_index[i] = WindowSimResult(
            request_id=request.id,
            index=i,
            timestamp=_round(now),
            principal=request.principal,
            cost=_round(cost),
            allowed=rejected_pid is None,
            policies=policy_views,
            rejected_by=rejected_pid,
            rejection=rejection,
        )

    results = [results_by_index[i] for i in range(len(requests))]
    allowed_count = sum(1 for r in results if r.allowed)

    # 终态：以最后一个请求时间戳为观察时刻，再对每个策略移除一次过期事件
    t_end = max(float(request.timestamp) for request in requests)

    final_states: list[WindowPolicyFinalState] = []
    for pid in policy_ids:
        final_window = SlidingWindowRange(
            start=_round(t_end - window_seconds[pid]),
            end=_round(t_end),
        )
        principal_rows: list[WindowPrincipalOccupancy] = []
        for principal in principal_order[pid]:
            log = active_events(pid, principal, t_end)
            # 从未成功记账或终态窗口内事件全部过期的主体不输出
            if not log:
                continue
            used = sum(c for _, c in log)
            principal_rows.append(
                WindowPrincipalOccupancy(
                    principal=principal,
                    event_count=len(log),
                    used=_round(used),
                    remaining=_round(limit[pid] - used),
                    window=final_window,
                )
            )
        total_used = sum(row.used for row in principal_rows)
        final_states.append(
            WindowPolicyFinalState(
                id=pid,
                window_seconds=_round(window_seconds[pid]),
                limit=_round(limit[pid]),
                total_used=_round(total_used),
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
