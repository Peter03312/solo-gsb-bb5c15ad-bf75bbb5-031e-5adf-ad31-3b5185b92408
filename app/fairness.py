"""公平性分析：按 principal 统计放行消耗/拒绝次数并计算加权 Jain 公平指数。

回放与 /simulate、/usage-report 完全一致（时间戳升序、同刻按输入顺序；
连续补充封顶 capacity；链上任一桶不足则全部不扣），再在回放结果上：

- 按 principal 汇总实际获得量（放行请求的 cost 合计）与拒绝次数；
- 以理论权重份额 = 全部放行消耗 × w / Σw 对齐实际获得量；
- 计算加权 Jain 公平指数 (Σx)² / (Σw · Σx²/w)；
- 找出短缺比例最高且有被拒请求的最受限 principal，以及按处理顺序
  首个造成其短缺的拒绝（链上第一个余额不足的桶）。

权重总和、理论份额、短缺比例与加权 Jain 指数全部使用**未舍入**的真实
权重与实际获得量计算：1e-10 等合法微小正权重不会被展示精度抹成 0，
权重经输入校验严格为正，故 Jain 分母不会除零；指数仅在实际获得量合计
恰好为 0 时定义为 0。响应中的 weight 按请求原值返回，其余展示字段才
做 _round。

统计同时给出跨全部窗口的全局报告与逐窗口报告。
"""
from __future__ import annotations

from fractions import Fraction

from app.models.analysis.fairness import (
    FairnessPrincipalRow,
    FairnessReportRequest,
    FairnessReportResponse,
    FairnessScopeReport,
    FairnessWindowReport,
    FirstShortfall,
)
from app.models.analysis.usage import WindowRange
from app.models.common import EPS
from app.simulation import _round


def _weighted_jain_index(allocations: list[float], weights: list[float]) -> float:
    """加权 Jain 公平指数：(Σx)² / (Σw · Σx²/w)。

    等权重时退化为经典 Jain 指数 (Σx)² / (n·Σx²)。份额完全按权重比例时
    取最大值 1；实际获得量与权重一律使用**未舍入**的真实数值（权重已由
    输入校验保证严格为正，因此分母不会为 0）；仅当实际获得量合计恰好为 0
    （无可分配量，无公平性可言）时把指数定义为 0。
    """
    total_x = sum(allocations)
    if total_x == 0.0:
        return 0.0
    total_w = sum(weights)
    weighted_squares = sum((x * x) / w for x, w in zip(allocations, weights))
    value = (total_x * total_x) / (total_w * weighted_squares)
    return _round(min(1.0, max(0.0, value)))


def fairness_report(payload: FairnessReportRequest) -> FairnessReportResponse:
    """与 /simulate 共享回放语义，按 principal 与 window_seconds 做公平性统计。

    窗口语义与 /usage-report 一致：以最早请求时刻 t_start 为原点，窗口 k 为
    半开区间 [t_start + k·window_seconds, t_start + (k+1)·window_seconds)，
    恰好落在边界上的请求属于右侧窗口。
    """
    requests = payload.requests
    t_start = min(request.timestamp for request in requests)
    t_end = max(request.timestamp for request in requests)
    width = float(payload.window_seconds)

    # 半开区间的精确判定基于浮点数的真实实数值（Fraction 按二进制位精确表示
    # 每个 float），既不靠 EPS 容差——容差会把右边界前紧邻的请求提前归入
    # 下一窗——也不受浮点除法在整数边界附近舍入的影响。
    origin = Fraction(t_start)
    window_width = Fraction(width)

    def window_index(timestamp: float) -> int:
        # 窗口 k 为 [t_start + k*width, t_start + (k+1)*width)：
        # floor 精确求值，恰好落在边界上的请求属于右侧窗口。
        return int((Fraction(timestamp) - origin) // window_width)

    window_count = window_index(t_end) + 1

    capacity = {b.id: float(b.capacity) for b in payload.buckets}
    refill_rate = {b.id: float(b.refill_rate) for b in payload.buckets}
    balance = {
        b.id: float(b.capacity if b.initial is None else b.initial) for b in payload.buckets
    }
    last_time = {b.id: t_start for b in payload.buckets}

    def refill(bucket_id: str, now: float) -> float:
        """把某个桶补充到时刻 now，返回补充后的余额（与 /simulate 相同）。"""
        elapsed = now - last_time[bucket_id]
        if elapsed > 0:
            balance[bucket_id] = min(
                capacity[bucket_id], balance[bucket_id] + refill_rate[bucket_id] * elapsed
            )
        last_time[bucket_id] = now
        return balance[bucket_id]

    # 与 /simulate 一致的处理顺序：时间戳升序，同刻按输入顺序
    order = sorted(
        range(len(requests)),
        key=lambda i: (requests[i].timestamp, i),
    )

    # 主体首次请求的处理序号（用于稳定排序与并列裁决）
    first_seen: dict[str, int] = {}
    # 每个主体按处理顺序的首次短缺拒绝（全局报告用）
    first_rejection: dict[str, FirstShortfall] = {}
    # 逐窗口：窗口内出现过的主体（按处理顺序）
    window_principals: list[list[str]] = [[] for _ in range(window_count)]
    window_seen: list[set[str]] = [set() for _ in range(window_count)]
    # 逐窗口逐主体：请求数/放行数/拒绝数/放行消耗
    w_request_count: list[dict[str, int]] = [dict() for _ in range(window_count)]
    w_allowed_count: list[dict[str, int]] = [dict() for _ in range(window_count)]
    w_rejected_count: list[dict[str, int]] = [dict() for _ in range(window_count)]
    w_consumption: list[dict[str, float]] = [dict() for _ in range(window_count)]
    # 逐窗口每主体在该窗口内按处理顺序的首次短缺拒绝
    w_first_rejection: list[dict[str, FirstShortfall]] = [
        dict() for _ in range(window_count)
    ]

    for seq, i in enumerate(order):
        request = requests[i]
        now = request.timestamp
        principal = request.principal
        k = window_index(now)

        if principal not in first_seen:
            first_seen[principal] = seq
        if principal not in window_seen[k]:
            window_seen[k].add(principal)
            window_principals[k].append(principal)

        stats_request = w_request_count[k]
        stats_request[principal] = stats_request.get(principal, 0) + 1

        # 链上连续补充 + 原子判定（与 /simulate 相同）
        refilled = {bid: refill(bid, now) for bid in request.chain}
        rejected_by: str | None = None
        for bid in request.chain:
            if refilled[bid] + EPS < request.cost:
                rejected_by = bid
                break
        allowed = rejected_by is None

        if allowed:
            for bid in request.chain:
                balance[bid] = refilled[bid] - request.cost
            w_allowed_count[k][principal] = w_allowed_count[k].get(principal, 0) + 1
            w_consumption[k][principal] = (
                w_consumption[k].get(principal, 0.0) + request.cost
            )
        else:
            # 原子性：拒绝时链上所有桶保持不变
            w_rejected_count[k][principal] = w_rejected_count[k].get(principal, 0) + 1
            shortfall = FirstShortfall(
                request_id=request.id,
                index=i,
                timestamp=_round(now),
                bucket_id=rejected_by,
                cost=_round(float(request.cost)),
                available=_round(refilled[rejected_by]),
            )
            if principal not in first_rejection:
                first_rejection[principal] = shortfall
            if principal not in w_first_rejection[k]:
                w_first_rejection[k][principal] = shortfall

    windows = [
        WindowRange(
            index=k,
            start=_round(t_start + k * width),
            end=_round(t_start + (k + 1) * width),
        )
        for k in range(window_count)
    ]

    # 主体固定权重（输入校验已保证同一 principal 权重一致）
    principal_weight: dict[str, float] = {}
    for request in requests:
        principal_weight.setdefault(request.principal, float(request.weight))

    def build_scope(
        members: list[str],
        req_counts: dict[str, int],
        allow_counts: dict[str, int],
        reject_counts: dict[str, int],
        consumption: dict[str, float],
        rejection_map: dict[str, FirstShortfall],
    ) -> FairnessScopeReport:
        """组装一个统计范围（全局或单窗口）的报告。"""
        # 权重总和、理论份额、短缺比例与加权 Jain 指数全部基于**未舍入**的
        # 权重与实际获得量计算；_round 只用于最终响应展示，避免把 1e-10 这类
        # 合法微小权重在计算前抹成 0（权重已由输入校验保证严格为正）。
        total_consumption = sum(consumption.values())
        total_weight = sum(principal_weight[p] for p in members)

        def share_of(p: str) -> float:
            return total_consumption * principal_weight[p] / total_weight

        rows: list[FairnessPrincipalRow] = []
        raw_ratio: dict[str, float] = {}
        for p in members:
            share = share_of(p)
            got = consumption.get(p, 0.0)
            deficit = max(0.0, share - got)
            deficit_ratio = deficit / share if share > 0.0 else 0.0
            raw_ratio[p] = deficit_ratio
            rows.append(
                FairnessPrincipalRow(
                    principal=p,
                    # 响应保留权重的有效数值（含 1e-10 等微小正权重），不做舍入
                    weight=principal_weight[p],
                    request_count=req_counts.get(p, 0),
                    allowed_count=allow_counts.get(p, 0),
                    rejected_count=reject_counts.get(p, 0),
                    allowed_consumption=_round(got),
                    theoretical_share=_round(share),
                    deficit=_round(deficit),
                    deficit_ratio=_round(deficit_ratio),
                )
            )

        jain = _weighted_jain_index(
            [consumption.get(p, 0.0) for p in members],
            [principal_weight[p] for p in members],
        )

        # 最受限主体：在有被拒请求的主体中取短缺比例最高者；
        # 全部获得量为 0（share 为 0）时，被拒主体按彻底短缺（比例 1）处理。
        candidates = [p for p in members if reject_counts.get(p, 0) > 0]
        starved = total_consumption == 0.0 and bool(candidates)

        def shortage_key(p: str) -> tuple[float, int]:
            ratio = 1.0 if starved else raw_ratio[p]
            return (-ratio, first_seen[p])

        most_constrained: FairnessPrincipalRow | None = None
        if candidates:
            winner = min(candidates, key=shortage_key)
            winner_ratio = 1.0 if starved else raw_ratio[winner]
            if winner_ratio > 0.0:
                most_constrained = next(r for r in rows if r.principal == winner)

        return FairnessScopeReport(
            principal_count=len(members),
            request_count=sum(req_counts.values()),
            allowed_count=sum(allow_counts.values()),
            rejected_count=sum(reject_counts.values()),
            allowed_consumption=_round(total_consumption),
            weighted_jain_index=jain,
            most_constrained_principal=(
                most_constrained.principal if most_constrained is not None else None
            ),
            first_shortfall=(
                rejection_map[most_constrained.principal]
                if most_constrained is not None
                else None
            ),
            principals=rows,
        )

    # ---- 逐窗口报告 ----
    window_reports: list[FairnessWindowReport] = []
    for k in range(window_count):
        scope = build_scope(
            window_principals[k],
            w_request_count[k],
            w_allowed_count[k],
            w_rejected_count[k],
            w_consumption[k],
            w_first_rejection[k],
        )
        window_reports.append(
            FairnessWindowReport(
                index=k,
                start=windows[k].start,
                end=windows[k].end,
                **scope.model_dump(),
            )
        )

    # ---- 全局聚合：主体按首次请求的处理顺序 ----
    all_principals = sorted(first_seen, key=lambda p: first_seen[p])
    global_req: dict[str, int] = {}
    global_allowed: dict[str, int] = {}
    global_rejected: dict[str, int] = {}
    global_consumption: dict[str, float] = {}
    for k in range(window_count):
        for p, n in w_request_count[k].items():
            global_req[p] = global_req.get(p, 0) + n
        for p, n in w_allowed_count[k].items():
            global_allowed[p] = global_allowed.get(p, 0) + n
        for p, n in w_rejected_count[k].items():
            global_rejected[p] = global_rejected.get(p, 0) + n
        for p, v in w_consumption[k].items():
            global_consumption[p] = global_consumption.get(p, 0.0) + v

    global_report = build_scope(
        all_principals,
        global_req,
        global_allowed,
        global_rejected,
        global_consumption,
        first_rejection,
    )

    return FairnessReportResponse(
        window_seconds=_round(width),
        window_count=window_count,
        windows=windows,
        global_report=global_report,
        window_reports=window_reports,
    )
