"""周期结转额度试算（/rollover-simulate）引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.main import app
from app.models import RolloverSimulateRequest
from app.rollover import rollover_simulate

client = TestClient(app)


def _run(policies, requests):
    return rollover_simulate(
        RolloverSimulateRequest.model_validate(
            {"policies": policies, "requests": requests}
        )
    )


def _view(result, policy_id):
    return result.policies[policy_id]


def _bal(result, policy_id):
    v = result.policies[policy_id]
    return v.balances.base, v.balances.carry


# ---------- 基本扣减 ----------

def test_first_request_starts_with_full_allowance_and_no_carry():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 5, "carry_cap": 2}],
        [{"id": "r1", "timestamp": 0, "cost": 3, "policy_refs": ["P"]}],
    )
    r = resp.results[0]
    assert r.allowed is True
    v = _view(r, "P")
    assert v.period == 0
    assert (v.period_window.start, v.period_window.end) == (0, 10)
    assert (v.balances.base.before, v.balances.base.after) == (5, 2)
    assert (v.balances.carry.before, v.balances.carry.after) == (0, 0)
    p = resp.policies[0]
    assert (p.period, p.base, p.carry, p.available) == (0, 2, 0, 2)
    assert resp.allowed_count == 1 and resp.rejected_count == 0


def test_cost_equal_to_available_is_allowed():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 5, "carry_cap": 2}],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 0, "cost": 2, "policy_refs": ["P"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, True]
    base, carry = _bal(resp.results[1], "P")
    assert (base.before, base.after) == (2, 0)


# ---------- 消费先扣结转额 ----------

def test_consumption_deducts_expiring_carry_first():
    # 周期 0 用剩 base=7；周期 1 初 carry=min(5,7)=5、base=10。
    # cost=7：先扣 carry 5→0，再扣 base 10→8。
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5}],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 10, "cost": 7, "policy_refs": ["P"]},
        ],
    )
    assert all(r.allowed for r in resp.results)
    v = _view(resp.results[1], "P")
    assert v.period == 1
    assert (v.period_window.start, v.period_window.end) == (10, 20)
    assert (v.balances.carry.before, v.balances.carry.after) == (5, 0)
    assert (v.balances.base.before, v.balances.base.after) == (10, 8)


def test_carry_covers_partial_cost_leaving_base_intact():
    # cost=3 全部由 carry 支付，base 不被扣减
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5}],
        [
            {"id": "r1", "timestamp": 0, "cost": 7, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 10, "cost": 3, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    assert (v.balances.carry.before, v.balances.carry.after) == (3, 0)
    assert (v.balances.base.before, v.balances.base.after) == (10, 10)


# ---------- 跨期结转 ----------

def test_unused_base_rolls_over_capped_at_carry_cap():
    # cap=2 < 未用 base 4：只结转 2
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 2}],
        [
            {"id": "r1", "timestamp": 0, "cost": 6, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 10, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    assert v.period == 1
    assert (v.balances.carry.before, v.balances.carry.after) == (2, 1)
    assert (v.balances.base.before, v.balances.base.after) == (10, 10)


def test_unused_base_smaller_than_cap_rolls_over_in_full():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 20}],
        [
            {"id": "r1", "timestamp": 0, "cost": 6, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 10, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    assert (v.balances.carry.before, v.balances.carry.after) == (4, 3)
    assert (v.balances.base.before, v.balances.base.after) == (10, 10)


def test_old_carry_does_not_stack_across_periods():
    # cap 很大时结转也只来自上一期未用 base（≤ allowance），旧 carry 绝不叠加：
    # 周期 0 用剩 6 → 周期 1 carry=6；周期 2 carry=min(cap, 10)=10（不是 16）
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 20}],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 20, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    assert v.period == 2
    assert v.balances.carry.before == 10
    assert v.balances.base.before == 10


def test_carry_cap_zero_means_no_rollover():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 10, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    assert v.balances.carry.before == 0
    assert (v.balances.base.before, v.balances.base.after) == (10, 9)


def test_skipping_multiple_empty_periods_converges_to_fixed_point():
    # 跨 ≥ 2 个空转周期：carry 收敛到 min(cap, allowance)，与逐期结转结果一致
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5}],
        [
            {"id": "r1", "timestamp": 0, "cost": 2, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 35, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    assert v.period == 3
    assert (v.period_window.start, v.period_window.end) == (30, 40)
    assert v.balances.carry.before == 5
    assert v.balances.base.before == 10
    assert (v.balances.carry.after, v.balances.base.after) == (4, 10)
    p = resp.policies[0]
    assert p.period == 3 and p.base == 10 and p.carry == 4


def test_period_boundary_request_belongs_to_new_period():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5}],
        [
            {"id": "r1", "timestamp": 9.999999999, "cost": 8, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 10, "cost": 8, "policy_refs": ["P"]},
        ],
    )
    assert _view(resp.results[0], "P").period == 0
    assert _view(resp.results[1], "P").period == 1
    # 上一期未用 base=2 → carry=min(5,2)=2，合计 12 ≥ 8：carry 2→0、base 10→4
    v = _view(resp.results[1], "P")
    assert (v.balances.carry.before, v.balances.base.after) == (2, 4)
    assert resp.results[1].allowed is True


def test_negative_period_index_with_anchor_after_timestamp():
    resp = _run(
        [{"id": "P", "anchor": 100, "period_seconds": 10, "allowance": 5, "carry_cap": 2}],
        [{"id": "r1", "timestamp": 0, "cost": 1, "policy_refs": ["P"]}],
    )
    v = _view(resp.results[0], "P")
    assert v.period == -10
    assert (v.period_window.start, v.period_window.end) == (0, 10)


# ---------- 小数周期边界 ----------

def test_fractional_period_boundary_enters_new_period():
    # 回归：anchor=0、period_seconds=0.1 时 t=0.3 曾被二进制浮点误差
    # （0.3/0.1 = 2.9999999999999996）错归周期 2 而拒绝；按十进制值判定
    # 后 t=0.3 恰好是周期 3 起点，进入新周期获得满额 base
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 0.1, "allowance": 1, "carry_cap": 0}],
        [
            {"id": "r1", "timestamp": 0.2, "cost": 1, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 0.3, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, True]
    v1 = _view(resp.results[0], "P")
    assert v1.period == 2
    assert (v1.period_window.start, v1.period_window.end) == (0.2, 0.3)
    assert (v1.balances.base.before, v1.balances.base.after) == (1, 0)
    v2 = _view(resp.results[1], "P")
    assert v2.period == 3
    assert (v2.period_window.start, v2.period_window.end) == (0.3, 0.4)
    assert (v2.balances.base.before, v2.balances.base.after) == (1, 0)
    assert (v2.balances.carry.before, v2.balances.carry.after) == (0, 0)
    p = resp.policies[0]
    assert p.period == 3
    assert (p.period_window.start, p.period_window.end) == (0.3, 0.4)
    assert (p.base, p.carry, p.available) == (0, 0, 0)


def test_fractional_period_adjacent_non_boundary_moments():
    # 边界两侧的相邻非边界时刻归属不变：0.299999999 仍属周期 2（余额已耗尽
    # 而拒绝），0.300000001 属周期 3；逐项 period/period_window 与终态一致
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 0.1, "allowance": 1, "carry_cap": 0}],
        [
            {"id": "r1", "timestamp": 0.2, "cost": 1, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 0.299999999, "cost": 1, "policy_refs": ["P"]},
            {"id": "r3", "timestamp": 0.3, "cost": 1, "policy_refs": ["P"]},
            {"id": "r4", "timestamp": 0.300000001, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, False, True, False]
    v2 = _view(resp.results[1], "P")
    assert v2.period == 2
    assert (v2.period_window.start, v2.period_window.end) == (0.2, 0.3)
    rej2 = resp.results[1].rejection
    assert rej2.period == 2
    assert (rej2.period_window.start, rej2.period_window.end) == (0.2, 0.3)
    assert (rej2.available, rej2.shortage) == (0, 1)
    v3 = _view(resp.results[2], "P")
    assert v3.period == 3
    assert (v3.period_window.start, v3.period_window.end) == (0.3, 0.4)
    v4 = _view(resp.results[3], "P")
    assert v4.period == 3
    assert (v4.period_window.start, v4.period_window.end) == (0.3, 0.4)
    rej4 = resp.results[3].rejection
    assert rej4.period == 3
    assert (rej4.period_window.start, rej4.period_window.end) == (0.3, 0.4)
    p = resp.policies[0]
    assert p.period == 3 and (p.base, p.carry) == (0, 0)


def test_fractional_period_boundary_rollover_carry():
    # 跨期时旧 carry 失效、上期未用 base 按 carry_cap 结转、本期 base 重置，
    # 在小数周期边界上同样成立
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 0.1, "allowance": 1, "carry_cap": 0.5}],
        [
            {"id": "r1", "timestamp": 0.2, "cost": 0.4, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 0.3, "cost": 0.8, "policy_refs": ["P"]},
        ],
    )
    assert all(r.allowed for r in resp.results)
    v = _view(resp.results[1], "P")
    assert v.period == 3
    assert (v.period_window.start, v.period_window.end) == (0.3, 0.4)
    # 周期 2 未用 base 0.6 → carry = min(0.5, 0.6) = 0.5；cost 0.8 先扣 carry 再扣 base
    assert (v.balances.carry.before, v.balances.carry.after) == (0.5, 0)
    assert (v.balances.base.before, v.balances.base.after) == (1, 0.7)
    p = resp.policies[0]
    assert p.period == 3 and (p.base, p.carry, p.available) == (0.7, 0, 0.7)


def test_fractional_period_with_fractional_anchor():
    # 小数锚点：anchor=0.2、period=0.1 时 t=0.5 恰好是周期 3 起点
    resp = _run(
        [{"id": "P", "anchor": 0.2, "period_seconds": 0.1, "allowance": 1, "carry_cap": 0}],
        [
            {"id": "r1", "timestamp": 0.4, "cost": 1, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 0.5, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, True]
    v1 = _view(resp.results[0], "P")
    assert v1.period == 2
    assert (v1.period_window.start, v1.period_window.end) == (0.4, 0.5)
    v2 = _view(resp.results[1], "P")
    assert v2.period == 3
    assert (v2.period_window.start, v2.period_window.end) == (0.5, 0.6)
    p = resp.policies[0]
    assert p.period == 3 and (p.base, p.carry) == (0, 0)


def test_fractional_period_multi_policy_atomic_rejection():
    # 同一时刻 A 跨入新周期（0.3/0.1 = 3）而 B 仍在旧周期（0.3/0.2 = 1.5）：
    # B 不足 → 原子拒绝，已跨期的 A 余额不被扣减
    resp = _run(
        [
            {"id": "A", "anchor": 0, "period_seconds": 0.1, "allowance": 1, "carry_cap": 0},
            {"id": "B", "anchor": 0, "period_seconds": 0.2, "allowance": 1, "carry_cap": 0},
        ],
        [
            {"id": "r1", "timestamp": 0.2, "cost": 1, "policy_refs": ["A", "B"]},
            {"id": "r2", "timestamp": 0.3, "cost": 1, "policy_refs": ["A", "B"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, False]
    r2 = resp.results[1]
    assert r2.rejected_by == "B"
    va = _view(r2, "A")
    assert va.period == 3
    assert (va.period_window.start, va.period_window.end) == (0.3, 0.4)
    assert (va.balances.base.before, va.balances.base.after) == (1, 1)
    vb = _view(r2, "B")
    assert vb.period == 1
    assert (vb.period_window.start, vb.period_window.end) == (0.2, 0.4)
    assert (vb.balances.base.before, vb.balances.base.after) == (0, 0)
    states = {p.id: p for p in resp.policies}
    assert (states["A"].period, states["A"].base, states["A"].carry) == (3, 1, 0)
    assert (states["B"].period, states["B"].base, states["B"].carry) == (1, 0, 0)


# ---------- 拒绝与原子性 ----------

def test_insufficient_total_balance_is_rejected_with_reason():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 5, "carry_cap": 2}],
        [{"id": "r1", "timestamp": 0, "cost": 6, "policy_refs": ["P"]}],
    )
    r = resp.results[0]
    assert r.allowed is False
    assert r.rejected_by == "P"
    rej = r.rejection
    assert rej.reason == "insufficient_balance"
    assert (rej.base_available, rej.carry_available, rej.available) == (5, 0, 5)
    assert rej.shortage == 1
    # 原子性：拒绝后余额不变
    base, carry = _bal(r, "P")
    assert (base.before, base.after) == (5, 5)
    assert (carry.before, carry.after) == (0, 0)
    p = resp.policies[0]
    assert (p.base, p.carry) == (5, 0)


def test_rejection_advances_period_rollover_but_does_not_consume():
    # t=0 用剩 base=3；t=10 结转 carry=3、base=7，cost=11 合计 10 不足 → 拒绝，
    # 但跨期结转照常发生，终态停留在周期 1 的结转结果
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 7, "carry_cap": 5}],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 10, "cost": 11, "policy_refs": ["P"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, False]
    v = _view(resp.results[1], "P")
    assert v.period == 1
    assert (v.balances.carry.before, v.balances.carry.after) == (3, 3)
    assert (v.balances.base.before, v.balances.base.after) == (7, 7)
    assert resp.results[1].rejection.shortage == 1
    p = resp.policies[0]
    assert p.period == 1 and p.base == 7 and p.carry == 3


def test_multi_policy_atomic_rejection_changes_nothing():
    # r1 两策略都充足并各扣 4（A base 10→6，B base 10→6）；
    # r2 cost 8：A、B 均仅余 6 都不足，按引用顺序报 A，且两个策略都保持 r1 后的状态
    resp = _run(
        [
            {"id": "A", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5},
            {"id": "B", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 1},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "policy_refs": ["A", "B"]},
            {"id": "r2", "timestamp": 0, "cost": 8, "policy_refs": ["A", "B"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, False]
    r2 = resp.results[1]
    assert r2.rejected_by == "A"
    base_a, carry_a = _bal(r2, "A")
    assert (base_a.before, base_a.after) == (6, 6)
    assert (carry_a.before, carry_a.after) == (0, 0)
    base_b, carry_b = _bal(r2, "B")
    assert (base_b.before, base_b.after) == (6, 6)
    assert (carry_b.before, carry_b.after) == (0, 0)
    rej = r2.rejection
    assert (rej.base_available, rej.carry_available, rej.available) == (6, 0, 6)
    assert rej.shortage == 2
    states = {p.id: p for p in resp.policies}
    assert (states["A"].base, states["A"].carry) == (6, 0)
    assert (states["B"].base, states["B"].carry) == (6, 0)


def test_first_insufficient_policy_follows_refs_order():
    # 两个策略都不足时，按 policy_refs 顺序报第一个
    resp = _run(
        [
            {"id": "A", "anchor": 0, "period_seconds": 10, "allowance": 1, "carry_cap": 0},
            {"id": "B", "anchor": 0, "period_seconds": 10, "allowance": 1, "carry_cap": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "policy_refs": ["A", "B"]}],
    )
    r = resp.results[0]
    assert r.allowed is False and r.rejected_by == "A"
    assert r.rejection.policy_id == "A"


def test_later_policy_insufficient_blocks_earlier_deduction():
    # 单独成功：r1 两策略都够；r2 仅 B 不足 → A 不得被扣
    resp = _run(
        [
            {"id": "A", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 0},
            {"id": "B", "anchor": 0, "period_seconds": 10, "allowance": 4, "carry_cap": 0},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "policy_refs": ["A", "B"]},
            {"id": "r2", "timestamp": 0, "cost": 1, "policy_refs": ["A", "B"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, False]
    r2 = resp.results[1]
    assert r2.rejected_by == "B"
    base_a, _ = _bal(r2, "A")
    assert (base_a.before, base_a.after) == (6, 6)
    states = {p.id: p for p in resp.policies}
    assert states["A"].base == 6
    assert states["B"].base == 0  # r1: B 4→0


# ---------- 排序与终态 ----------

def test_unsorted_input_processed_chronologically_results_keep_order():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 1, "carry_cap": 0}],
        [
            {"id": "late", "timestamp": 5, "cost": 1, "policy_refs": ["P"]},
            {"id": "early", "timestamp": 0, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    assert [r.request_id for r in resp.results] == ["late", "early"]
    assert [r.index for r in resp.results] == [0, 1]
    assert resp.results[0].allowed is False  # 实际先扣的是 t=0 的 early
    assert resp.results[1].allowed is True


def test_same_timestamp_keeps_input_order():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 1, "carry_cap": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 0, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, False]


def test_final_state_observed_at_last_request_timestamp():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5}],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 25, "cost": 2, "policy_refs": ["P"]},
        ],
    )
    p = resp.policies[0]
    assert p.period == 2
    assert (p.period_window.start, p.period_window.end) == (20, 30)
    # 周期 0 剩 7 → 周期 1 carry=5/base=10 空转 → 周期 2 carry=5/base=10；
    # r2 先扣 carry 5 中的 2 → carry 3、base 10
    assert (p.base, p.carry, p.available) == (10, 3, 13)


def test_unreferenced_policy_starts_fresh_in_its_end_period():
    resp = _run(
        [
            {"id": "used", "anchor": 0, "period_seconds": 10, "allowance": 8, "carry_cap": 2},
            {"id": "idle", "anchor": 0, "period_seconds": 5, "allowance": 4, "carry_cap": 1},
        ],
        [{"id": "r1", "timestamp": 12, "cost": 1, "policy_refs": ["used"]}],
    )
    states = {p.id: p for p in resp.policies}
    idle = states["idle"]
    assert idle.period == 2
    assert (idle.period_window.start, idle.period_window.end) == (10, 15)
    assert (idle.base, idle.carry, idle.available) == (4, 0, 4)
    used = states["used"]
    assert used.period == 1 and used.base == 7 and used.carry == 0


def test_policy_view_only_includes_referenced_policies_in_refs_order():
    resp = _run(
        [
            {"id": "A", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 0},
            {"id": "B", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 0},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "policy_refs": ["A"]},
            {"id": "r2", "timestamp": 0, "cost": 1, "policy_refs": ["B", "A"]},
        ],
    )
    assert set(resp.results[0].policies) == {"A"}
    assert list(resp.results[1].policies) == ["B", "A"]


# ---------- Pydantic / 跨字段校验 ----------

def _validate_error(payload):
    with pytest.raises(RequestValidationErrors) as exc:
        RolloverSimulateRequest.model_validate(payload)
    return exc.value.errors


def _pdp():
    return {
        "id": "P",
        "anchor": 0,
        "period_seconds": 10,
        "allowance": 5,
        "carry_cap": 1,
    }


def _req(**overrides):
    req = {"id": "r", "timestamp": 0, "cost": 1, "policy_refs": ["P"]}
    req.update(overrides)
    return req


def test_duplicate_policy_ids_rejected():
    errors = _validate_error(
        {"policies": [_pdp(), {**_pdp(), "period_seconds": 20}], "requests": [_req()]}
    )
    assert all(e["type"] == "duplicate_policy" for e in errors)


def test_unknown_policy_reference_rejected():
    errors = _validate_error(
        {"policies": [_pdp()], "requests": [_req(policy_refs=["P", "X"])]}
    )
    assert any(e["type"] == "unknown_policy_ref" and e["input"] == "X" for e in errors)


def test_duplicate_request_ids_rejected():
    errors = _validate_error(
        {"policies": [_pdp()], "requests": [_req(), _req()]}
    )
    assert any(e["type"] == "duplicate_request" for e in errors)


def test_duplicate_policy_ref_within_request_rejected_by_pydantic():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RolloverSimulateRequest.model_validate(
            {"policies": [_pdp()], "requests": [_req(policy_refs=["P", "P"])]}
        )


def test_bad_policy_fields_rejected_by_pydantic():
    from pydantic import ValidationError

    for mutate in (
        lambda p: p.update(period_seconds=0),
        lambda p: p.update(period_seconds=-3),
        lambda p: p.update(allowance=0),
        lambda p: p.update(carry_cap=-1),
        lambda p: p.update(anchor="nan"),
        lambda p: p.update(period_seconds="inf"),
    ):
        bad = _pdp()
        mutate(bad)
        with pytest.raises(ValidationError):
            RolloverSimulateRequest.model_validate(
                {"policies": [bad], "requests": [_req()]}
            )


def test_negative_anchor_is_allowed():
    req = RolloverSimulateRequest.model_validate(
        {"policies": [{**_pdp(), "anchor": -100}], "requests": [_req()]}
    )
    assert req.policies[0].anchor == -100


def test_bad_request_fields_rejected_by_pydantic():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RolloverSimulateRequest.model_validate(
            {"policies": [_pdp()], "requests": [_req(cost=0)]}
        )
    with pytest.raises(ValidationError):
        RolloverSimulateRequest.model_validate(
            {"policies": [_pdp()], "requests": [_req(policy_refs=[])]}
        )
    with pytest.raises(ValidationError):
        RolloverSimulateRequest.model_validate(
            {"policies": [_pdp()], "requests": [_req(id="")]}
        )
    with pytest.raises(ValidationError):
        RolloverSimulateRequest.model_validate(
            {"policies": [_pdp()], "requests": [_req(timestamp="oops")]}
        )
    with pytest.raises(ValidationError):
        RolloverSimulateRequest.model_validate(
            {
                "policies": [_pdp()],
                "requests": [{**_req(), "unexpected": 1}],
            }
        )


def test_extra_policy_field_rejected_by_pydantic():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RolloverSimulateRequest.model_validate(
            {"policies": [{**_pdp(), "nope": 1}], "requests": [_req()]}
        )


def test_empty_policies_or_requests_rejected_by_pydantic():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RolloverSimulateRequest.model_validate({"policies": [], "requests": []})


# ---------- HTTP ----------

def test_http_rollover_simulate_ok():
    response = client.post(
        "/rollover-simulate",
        json={
            "policies": [
                {"id": "p10", "anchor": 0, "period_seconds": 10,
                 "allowance": 10, "carry_cap": 5},
                {"id": "p7", "anchor": 0, "period_seconds": 7,
                 "allowance": 7, "carry_cap": 3},
            ],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 4,
                 "policy_refs": ["p10", "p7"]},
                {"id": "r2", "timestamp": 0, "cost": 4,
                 "policy_refs": ["p10", "p7"]},
                {"id": "r3", "timestamp": 9, "cost": 4,
                 "policy_refs": ["p10", "p7"]},
                {"id": "r4", "timestamp": 10, "cost": 4,
                 "policy_refs": ["p10", "p7"]},
                {"id": "r5", "timestamp": 10, "cost": 7,
                 "policy_refs": ["p10"]},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert [r["allowed"] for r in data["results"]] == [
        True, False, True, True, True
    ]
    assert data["results"][1]["rejected_by"] == "p7"
    assert data["results"][1]["rejection"]["reason"] == "insufficient_balance"
    assert data["results"][1]["rejection"]["shortage"] == 1.0
    assert data["results"][3]["policies"]["p10"]["period"] == 1
    assert data["results"][3]["policies"]["p10"]["balances"]["carry"] == {
        "before": 2.0, "after": 0.0
    }
    assert (data["allowed_count"], data["rejected_count"]) == (4, 1)
    states = {p["id"]: p for p in data["policies"]}
    assert states["p10"]["period"] == 1
    assert (states["p10"]["base"], states["p10"]["carry"]) == (1.0, 0.0)
    assert states["p7"]["period"] == 1
    assert (states["p7"]["base"], states["p7"]["carry"]) == (2.0, 0.0)


def test_http_rollover_fractional_period_boundary():
    # 回归：小数周期边界请求按十进制时间值归入右侧新周期
    response = client.post(
        "/rollover-simulate",
        json={
            "policies": [
                {"id": "P", "anchor": 0, "period_seconds": 0.1,
                 "allowance": 1, "carry_cap": 0}
            ],
            "requests": [
                {"id": "r1", "timestamp": 0.2, "cost": 1, "policy_refs": ["P"]},
                {"id": "r2", "timestamp": 0.3, "cost": 1, "policy_refs": ["P"]},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert [r["allowed"] for r in data["results"]] == [True, True]
    view = data["results"][1]["policies"]["P"]
    assert view["period"] == 3
    assert view["period_window"] == {"index": 3, "start": 0.3, "end": 0.4}
    assert view["balances"]["base"] == {"before": 1.0, "after": 0.0}
    state = data["policies"][0]
    assert state["period"] == 3
    assert state["period_window"] == {"index": 3, "start": 0.3, "end": 0.4}
    assert (state["base"], state["carry"], state["available"]) == (0.0, 0.0, 0.0)


def test_http_validation_error_is_422():
    response = client.post(
        "/rollover-simulate",
        json={
            "policies": [
                {"id": "P", "anchor": 0, "period_seconds": 10,
                 "allowance": 5, "carry_cap": 1}
            ],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "policy_refs": ["GHOST"]}
            ],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_policy_ref" for e in detail)


def test_http_root_lists_rollover_simulate():
    root = client.get("/").json()
    assert root["rollover_simulate"] == "/rollover-simulate"
