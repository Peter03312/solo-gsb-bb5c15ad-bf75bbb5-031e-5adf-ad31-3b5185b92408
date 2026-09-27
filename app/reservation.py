"""区间预留试算：按 start 升序做容量受限的原子区间预留。

与 /simulate 的令牌桶不同，这里的桶没有补充概念，只有固定容量 capacity，
表示任意时刻允许的**并发占用**上限。每个预留在半开区间 [start, end) 内对
链上每个桶持续占用 cost；仅当链上每个桶在该区间内每一处的并发占用加 cost
都不超过 capacity 时才原子接受，否则拒绝且不改变任何桶的占用。
"""
from __future__ import annotations

from app.models.common import EPS
from app.models.scheduling.reservation import (
    OccupancyInterval,
    ReservationBucketOccupancy,
    ReservationResult,
    ReservationSimulateRequest,
    ReservationSimulateResponse,
)
from app.simulation import _round

# 已接受区间：(start, end, cost)
AcceptedInterval = tuple[float, float, float]


def _peak_overlap(accepted: list[AcceptedInterval], start: float, end: float) -> float:
    """已接受区间在半开区间 [start, end) 内的并发占用峰值。

    半开语义：恰好在 start 结束（e == start）的区间不重叠；恰好在 start
    开始（s == start）的区间在 start 处活跃。占用只在已接受区间的起点上升，
    故对事件点做一次扫描即可得到区间内最高占用。
    """
    deltas: dict[float, float] = {}
    for s, e, cost in accepted:
        if s < end and e > start:  # 与 [start, end) 相交（端点相接不算）
            deltas[s] = deltas.get(s, 0.0) + cost
            deltas[e] = deltas.get(e, 0.0) - cost

    level = 0.0
    peak = 0.0
    for t in sorted(deltas):
        level += deltas[t]
        # t <= start 的事件决定 start 时刻的占用；start < t < end 的事件
        # 决定区间内部各常值段；t == end（含）之后不属于该区间
        if t <= start or t < end:
            if level > peak:
                peak = level
    return peak


def _build_occupancy(
    accepted: list[AcceptedInterval],
) -> tuple[list[OccupancyInterval], float]:
    """把已接受区间叠加成按时间升序的合并占用区间，并求峰值并发。

    同一时刻的起止事件先相互抵消（半开区间：结束先于开始），占用恒定的
    相邻段合并，占用为 0 的段不输出。
    """
    deltas: dict[float, float] = {}
    for s, e, cost in accepted:
        deltas[s] = deltas.get(s, 0.0) + cost
        deltas[e] = deltas.get(e, 0.0) - cost

    # 原始值分段，输出前统一舍入
    raw_segments: list[list[float]] = []  # [start, end, occupancy]
    peak = 0.0
    level = 0.0
    prev_t: float | None = None
    for t in sorted(deltas):
        # [prev_t, t) 上的占用为应用 t 时刻事件之前的 level
        if prev_t is not None and level > EPS:
            if level > peak:
                peak = level
            if (
                raw_segments
                and raw_segments[-1][1] == prev_t
                and abs(raw_segments[-1][2] - level) <= EPS
            ):
                # 同刻事件抵消导致占用不变：与前一段相接且占用相同，合并
                raw_segments[-1][1] = t
            else:
                raw_segments.append([prev_t, t, level])
        level += deltas[t]
        prev_t = t

    intervals = [
        OccupancyInterval(
            start=_round(seg_start),
            end=_round(seg_end),
            occupancy=_round(occupancy),
        )
        for seg_start, seg_end, occupancy in raw_segments
    ]
    return intervals, _round(peak)


def reservation_simulate(payload: ReservationSimulateRequest) -> ReservationSimulateResponse:
    """按 start 升序逐项做链上原子容量判定，返回逐项结果与每桶合并占用。"""
    capacity = {b.id: float(b.capacity) for b in payload.buckets}
    accepted: dict[str, list[AcceptedInterval]] = {b.id: [] for b in payload.buckets}

    # 内部按 start 升序处理；同一 start 按原始输入顺序处理
    order = sorted(
        range(len(payload.reservations)),
        key=lambda i: (payload.reservations[i].start, i),
    )

    results_by_index: dict[int, ReservationResult] = {}

    for i in order:
        reservation = payload.reservations[i]
        start = float(reservation.start)
        end = float(reservation.end)
        cost = float(reservation.cost)

        # 先对链上每个桶做整段容量判定，链上第一个放不下的桶即限制桶
        rejected_by: str | None = None
        for bucket_id in reservation.chain:
            peak = _peak_overlap(accepted[bucket_id], start, end)
            if peak + cost > capacity[bucket_id] + EPS:
                rejected_by = bucket_id
                break

        if rejected_by is None:
            # 全部通过：原子接受，链上每个桶登记同一段占用
            for bucket_id in reservation.chain:
                accepted[bucket_id].append((start, end, cost))

        results_by_index[i] = ReservationResult(
            index=i,
            start=_round(start),
            end=_round(end),
            cost=_round(cost),
            allowed=rejected_by is None,
            rejected_by=rejected_by,
        )

    results = [results_by_index[i] for i in range(len(payload.reservations))]
    allowed_count = sum(1 for r in results if r.allowed)

    bucket_reports: list[ReservationBucketOccupancy] = []
    for bucket in payload.buckets:
        intervals, peak = _build_occupancy(accepted[bucket.id])
        bucket_reports.append(
            ReservationBucketOccupancy(
                id=bucket.id,
                capacity=_round(capacity[bucket.id]),
                peak_occupancy=peak,
                intervals=intervals,
            )
        )

    return ReservationSimulateResponse(
        allowed_count=allowed_count,
        rejected_count=len(results) - allowed_count,
        results=results,
        buckets=bucket_reports,
    )
