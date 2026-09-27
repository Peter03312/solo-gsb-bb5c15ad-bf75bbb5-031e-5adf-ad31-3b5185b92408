"""配额使用分析：按时间窗口统计各桶的放行消耗、拒绝次数与峰值占用。"""
from __future__ import annotations

import math
from fractions import Fraction

from app.models.analysis.usage import (
    BusiestWindow,
    UsageBucketReport,
    UsageReportRequest,
    UsageReportResponse,
    UsageWindowStats,
    WindowRange,
)
from app.models.common import EPS
from app.simulation import _round


def usage_report(payload: UsageReportRequest) -> UsageReportResponse:
    """与 /simulate 共享连续补充与链式原子判定，按 window_seconds 切窗统计。

    窗口语义：
    - 以最早请求时刻 t_start 为原点，窗口 k 为半开区间
      [t_start + k * window_seconds, t_start + (k + 1) * window_seconds)；
    - 每个请求只计入其 timestamp 所在的那个窗口（恰好落在边界上属于右侧窗口），
      绝不跨窗重复计数；
    - 峰值占用 = capacity - 窗口内最低余额，即该窗口内配额被占用（消耗待补充）
      的峰值；最低余额在窗口起点与每次放行扣减之后采样——余额在事件之间只会
      因连续补充而上升，因此这些采样点足以刻画窗口内的最低值。
    """
    requests = payload.requests
    # 与模拟引擎一致：所有桶在最早请求时刻持有初始余额
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
    bucket_ids = [b.id for b in payload.buckets]

    def refill(bucket_id: str, now: float) -> float:
        """把某个桶补充到时刻 now，返回补充后的余额（与 /simulate 相同）。"""
        elapsed = now - last_time[bucket_id]
        if elapsed > 0:
            balance[bucket_id] = min(
                capacity[bucket_id], balance[bucket_id] + refill_rate[bucket_id] * elapsed
            )
        last_time[bucket_id] = now
        return balance[bucket_id]

    # 每桶每窗统计
    allowed_consumption = {bid: [0.0] * window_count for bid in bucket_ids}
    rejected_count = {bid: [0] * window_count for bid in bucket_ids}
    min_balance = {bid: [math.inf] * window_count for bid in bucket_ids}
    # 窗口 0 起点余额 = 初始余额
    for bid in bucket_ids:
        min_balance[bid][0] = balance[bid]

    # 请求级窗口计数（用于全局最忙窗口）
    window_allowed_requests = [0] * window_count
    window_rejected_requests = [0] * window_count

    # 与 /simulate 一致的处理顺序：时间戳升序，同刻按输入顺序
    order = sorted(
        range(len(requests)),
        key=lambda i: (requests[i].timestamp, i),
    )

    current_window = 0
    for i in order:
        request = requests[i]
        now = request.timestamp
        k = window_index(now)

        # 真实跨窗后才逐窗推进：只在请求确实落在更右侧窗口时（半开判定保证
        # 该窗口的左边界不晚于请求时刻），把每个桶连续补充到中间每个窗口边界，
        # 记录各窗起点余额。边界按浮点计算可能产生几 ulp 误差，用 min 钳制，
        # 确保补充时钟绝不越过当前请求（分段补充与一次性补充结果相同：线性
        # 补充、同一容量上限）。
        while current_window < k:
            current_window += 1
            boundary = min(t_start + current_window * width, now)
            for bid in bucket_ids:
                min_balance[bid][current_window] = refill(bid, boundary)

        # 链上连续补充 + 原子判定（与 /simulate 相同）
        refilled = {bid: refill(bid, now) for bid in request.chain}
        rejected_by: str | None = None
        for bid in request.chain:
            if refilled[bid] + EPS < request.cost:
                rejected_by = bid
                break
        allowed = rejected_by is None

        if allowed:
            window_allowed_requests[k] += 1
            for bid in request.chain:
                balance[bid] = refilled[bid] - request.cost
                allowed_consumption[bid][k] += request.cost
                if balance[bid] < min_balance[bid][k]:
                    min_balance[bid][k] = balance[bid]
        else:
            # 原子性：拒绝时链上所有桶保持不变
            window_rejected_requests[k] += 1
            for bid in request.chain:
                rejected_count[bid][k] += 1

    windows = [
        WindowRange(
            index=k,
            start=_round(t_start + k * width),
            end=_round(t_start + (k + 1) * width),
        )
        for k in range(window_count)
    ]

    bucket_reports: list[UsageBucketReport] = []
    for bid in bucket_ids:
        # 峰值占用 = capacity - 窗口内最低余额；并列取最早窗口
        peak_window_index = min(
            range(window_count), key=lambda k: min_balance[bid][k]
        )
        peak_occupancy = capacity[bid] - min_balance[bid][peak_window_index]
        bucket_reports.append(
            UsageBucketReport(
                id=bid,
                allowed_consumption=_round(sum(allowed_consumption[bid])),
                rejected_count=sum(rejected_count[bid]),
                peak_occupancy=_round(peak_occupancy),
                peak_window=windows[peak_window_index],
                windows=[
                    UsageWindowStats(
                        index=k,
                        allowed_consumption=_round(allowed_consumption[bid][k]),
                        rejected_count=rejected_count[bid][k],
                        peak_occupancy=_round(capacity[bid] - min_balance[bid][k]),
                    )
                    for k in range(window_count)
                ],
            )
        )

    # 全局最忙窗口：跨桶放行消耗合计最大；并列取最早
    window_totals = [
        sum(allowed_consumption[bid][k] for bid in bucket_ids)
        for k in range(window_count)
    ]
    busiest_index = max(range(window_count), key=lambda k: window_totals[k])
    busiest_window = BusiestWindow(
        index=busiest_index,
        start=windows[busiest_index].start,
        end=windows[busiest_index].end,
        allowed_consumption=_round(window_totals[busiest_index]),
        allowed_count=window_allowed_requests[busiest_index],
        rejected_count=window_rejected_requests[busiest_index],
    )

    return UsageReportResponse(
        window_seconds=_round(width),
        window_count=window_count,
        windows=windows,
        buckets=bucket_reports,
        busiest_window=busiest_window,
    )
