"""两情景可借贷桶回放差异：同一请求序列在 baseline / candidate 两组配置下
分别按补充先偿债、链上原子借贷规则回放，再按原始请求索引对齐比较。

服务本身不保存任何状态：本模块对两组情景各自独立调用 replay_debt，
输入不一致（请求数量、请求 ID、时间戳或桶集合）已在
DebtDiffRequest 跨字段校验阶段以 422 拒绝，不会进入回放。
"""
from __future__ import annotations

from app.debt import replay_debt
from app.models.debt.diff import (
    DebtDiffBucketDelta,
    DebtDiffFinalSnapshot,
    DebtDiffFirstDivergence,
    DebtDiffOutcome,
    DebtDiffRequest,
    DebtDiffResponse,
    DebtDiffResult,
)
from app.models.debt.simulate import DebtBucketFinalState, DebtSimResult
from app.simulation import _round


def _outcome(result: DebtSimResult) -> DebtDiffOutcome:
    return DebtDiffOutcome(
        allowed=result.allowed,
        rejected_by=result.rejected_by,
        reason=None if result.rejection is None else result.rejection.reason,
    )


def _is_divergence(base: DebtSimResult, cand: DebtSimResult) -> bool:
    """放行结论分歧：allowed 不同；或双方均拒绝但拒绝桶/原因不同。"""
    if base.allowed != cand.allowed:
        return True
    if not base.allowed:
        return (
            base.rejected_by != cand.rejected_by
            or (
                base.rejection is not None
                and cand.rejection is not None
                and base.rejection.reason != cand.rejection.reason
            )
        )
    return False


def _final_state(bucket_id: str, row: tuple) -> DebtBucketFinalState:
    """把 replay_debt 终态原始行渲染成 /debt-simulate 同款终态快照。"""
    _, capacity, refill_rate, debt_limit, balance, debt = row
    return DebtBucketFinalState(
        id=bucket_id,
        capacity=_round(capacity),
        refill_rate=_round(refill_rate),
        debt_limit=_round(debt_limit),
        balance=_round(balance),
        debt=_round(debt),
        available=_round(balance + (debt_limit - debt)),
    )


def debt_diff(payload: DebtDiffRequest) -> DebtDiffResponse:
    baseline = replay_debt(payload.baseline)
    candidate = replay_debt(payload.candidate)

    base_results = baseline["results_by_index"]
    cand_results = candidate["results_by_index"]
    base_snapshots = baseline["snapshots_by_index"]
    cand_snapshots = candidate["snapshots_by_index"]

    base_requests = payload.baseline.requests
    cand_requests = payload.candidate.requests

    # 桶顺序按 baseline 定义顺序（两情景桶 ID 集合已在校验阶段确认一致）
    bucket_order = [b.id for b in payload.baseline.buckets]

    results: list[DebtDiffResult] = []
    first_divergence: DebtDiffFirstDivergence | None = None
    divergence_count = 0

    for i in range(len(base_requests)):
        base_result = base_results[i]
        cand_result = cand_results[i]

        diverged = _is_divergence(base_result, cand_result)
        if diverged:
            divergence_count += 1
            if first_divergence is None:
                first_divergence = DebtDiffFirstDivergence(
                    request_id=base_result.request_id,
                    index=i,
                    timestamp=base_result.timestamp,
                    baseline=_outcome(base_result),
                    candidate=_outcome(cand_result),
                )

        # 该请求两情景链上引用桶的并集（baseline 链序优先，候选新桶随后）
        referenced = set()
        union_chain: list[str] = []
        for bucket_id in base_requests[i].chain:
            if bucket_id not in referenced:
                referenced.add(bucket_id)
                union_chain.append(bucket_id)
        for bucket_id in cand_requests[i].chain:
            if bucket_id not in referenced:
                referenced.add(bucket_id)
                union_chain.append(bucket_id)

        base_referenced = set(base_requests[i].chain)
        cand_referenced = set(cand_requests[i].chain)

        bucket_deltas: dict[str, DebtDiffBucketDelta] = {}
        for bucket_id in union_chain:
            base_balance_raw, base_debt_raw = base_snapshots[i][bucket_id]
            cand_balance_raw, cand_debt_raw = cand_snapshots[i][bucket_id]
            base_balance = _round(base_balance_raw)
            base_debt = _round(base_debt_raw)
            cand_balance = _round(cand_balance_raw)
            cand_debt = _round(cand_debt_raw)
            in_both = bucket_id in base_referenced and bucket_id in cand_referenced
            bucket_deltas[bucket_id] = DebtDiffBucketDelta(
                baseline_balance=base_balance,
                candidate_balance=cand_balance,
                balance_delta=(
                    _round(cand_balance - base_balance) if in_both else None
                ),
                baseline_debt=base_debt,
                candidate_debt=cand_debt,
                debt_delta=_round(cand_debt - base_debt) if in_both else None,
            )

        results.append(
            DebtDiffResult(
                request_id=base_result.request_id,
                index=i,
                timestamp=base_result.timestamp,
                diverged=diverged,
                baseline=_outcome(base_result),
                candidate=_outcome(cand_result),
                buckets=bucket_deltas,
            )
        )

    base_finals = {row[0]: row for row in baseline["final_states"]}
    cand_finals = {row[0]: row for row in candidate["final_states"]}

    final_buckets: list[DebtDiffFinalSnapshot] = []
    for bucket_id in bucket_order:
        base_final = _final_state(bucket_id, base_finals[bucket_id])
        cand_final = _final_state(bucket_id, cand_finals[bucket_id])
        final_buckets.append(
            DebtDiffFinalSnapshot(
                id=bucket_id,
                baseline=base_final,
                candidate=cand_final,
                balance_delta=_round(cand_final.balance - base_final.balance),
                debt_delta=_round(cand_final.debt - base_final.debt),
                available_delta=_round(cand_final.available - base_final.available),
            )
        )

    return DebtDiffResponse(
        request_count=len(results),
        divergence_count=divergence_count,
        first_divergence=first_divergence,
        results=results,
        final_buckets=final_buckets,
    )
