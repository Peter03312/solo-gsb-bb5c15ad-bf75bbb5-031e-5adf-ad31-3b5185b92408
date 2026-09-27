"""可借贷连续补充桶试算（/debt-simulate）引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.main import app
from app.models import DebtSimulateRequest
from app.debt import debt_simulate

client = TestClient(app)


def _run(buckets, requests):
    return debt_simulate(
        DebtSimulateRequest.model_validate({"buckets": buckets, "requests": requests})
    )


def _bdp(**overrides):
    bucket = {
        "id": "B",
        "capacity": 10,
        "refill_rate": 1,
        "initial": 10,
        "debt_limit": 5,
    }
    bucket.update(overrides)
    return bucket


def _req(**overrides):
    request = {"id": "r", "timestamp": 0, "cost": 1, "chain": ["B"]}
    request.update(overrides)
    return request


def _view(result, bucket_id):
    return result.buckets[bucket_id]


# ---------- 基本借债扣减 ----------

def test_cost_within_balance_pays_from_balance_without_debt():
    resp = _run([_bdp(initial=8)], [_req(cost=3)])
    r = resp.results[0]
    v = _view(r, "B")
    assert r.allowed is True and r.rejected_by is None
    assert (v.balance.before, v.balance.after) == (8, 5)
    assert (v.debt.before, v.debt.after) == (0, 0)


def test_shortfall_is_borrowed_up_to_debt_limit():
    resp = _run([_bdp(initial=2, debt_limit=5)], [_req(cost=4)])
    v = _view(resp.results[0], "B")
    assert resp.results[0].allowed is True
    assert (v.balance.before, v.balance.after) == (2, 0)
    assert (v.debt.before, v.debt.after) == (0, 2)
    fb = {b.id: b for b in resp.final_buckets}["B"]
    assert (fb.balance, fb.debt) == (0, 2)  # t_end=0，无补充


def test_zero_balance_pays_entirely_on_credit():
    resp = _run([_bdp(initial=0, debt_limit=5)], [_req(cost=5)])
    v = _view(resp.results[0], "B")
    assert (v.balance.before, v.balance.after) == (0, 0)
    assert (v.debt.before, v.debt.after) == (0, 5)


def test_cost_equal_to_balance_plus_remaining_credit_is_allowed():
    # 已有债务 2 → 剩余借贷额度 3，余额 4：恰好可支付 7
    resp = _run(
        [_bdp(initial=5, debt_limit=5)],
        [
            _req(id="r1", cost=3),   # 5→2，无债务
            _req(id="r2", cost=5),   # 2→0，借 3，债务=3
            _req(id="r3", cost=2),   # 余额 0 + 剩余额度 2，恰好放行
        ],
    )
    assert [r.allowed for r in resp.results] == [True, True, True]
    v = _view(resp.results[2], "B")
    assert (v.balance.before, v.balance.after) == (0, 0)
    assert (v.debt.before, v.debt.after) == (3, 5)


# ---------- 拒绝原因 ----------

def test_cost_exceeding_capacity_plus_debt_limit_is_unreachable():
    resp = _run([_bdp(initial=10, debt_limit=5)], [_req(cost=16)])
    r = resp.results[0]
    assert r.allowed is False and r.rejected_by == "B"
    rej = r.rejection
    assert rej.reason == "cost_exceeds_limit"
    assert (rej.capacity, rej.debt_limit, rej.cost) == (10, 5, 16)
    assert (rej.balance_available, rej.debt_before) == (10, 0)
    assert rej.available == 15 and rej.shortage == 1
    # 原子性：余额/债务不变
    v = _view(r, "B")
    assert (v.balance.before, v.balance.after) == (10, 10)
    assert (v.debt.before, v.debt.after) == (0, 0)


def test_unreachable_reason_takes_precedence_even_with_existing_debt():
    # 即便当前一个更便宜的请求也付不起，cost 超总上限仍报 cost_exceeds_limit
    resp = _run(
        [_bdp(initial=0, debt_limit=5)],
        [
            _req(id="r1", cost=5),  # 借满，债务=5
            _req(id="r2", cost=16),  # 超过 capacity+debt_limit=15
        ],
    )
    r2 = resp.results[1]
    assert r2.allowed is False
    assert r2.rejection.reason == "cost_exceeds_limit"
    assert r2.rejection.available == 0 and r2.rejection.shortage == 16


def test_exhausted_credit_is_rejected_as_debt_limit_exceeded():
    resp = _run(
        [_bdp(initial=2, debt_limit=5)],
        [
            _req(id="r1", cost=4),  # 余额 2→0，借 2，债务=2
            _req(id="r2", cost=4),  # 余额 0 + 剩余额度 3 < 4
        ],
    )
    r2 = resp.results[1]
    assert r2.allowed is False and r2.rejected_by == "B"
    rej = r2.rejection
    assert rej.reason == "debt_limit_exceeded"
    assert (rej.balance_available, rej.debt_before) == (0, 2)
    assert (rej.remaining_credit, rej.available, rej.shortage) == (3, 3, 1)
    v = _view(r2, "B")
    assert (v.balance.before, v.balance.after) == (0, 0)
    assert (v.debt.before, v.debt.after) == (2, 2)


def test_zero_debt_limit_matches_plain_simulate_rejection():
    resp = _run([_bdp(initial=3, debt_limit=0)], [_req(cost=4)])
    r = resp.results[0]
    assert r.allowed is False
    assert r.rejection.reason == "debt_limit_exceeded"
    assert r.rejection.available == 3 and r.rejection.shortage == 1


def test_zero_debt_limit_replay_matches_plain_simulate():
    # debt_limit=0 时逐请求放行结果、余额快照与终态必须与 /simulate 完全一致
    from app.models import SimulateRequest
    from app.simulation import simulate

    buckets = [
        {"id": "A", "capacity": 10, "refill_rate": 1, "initial": 3},
        {"id": "B", "capacity": 5, "refill_rate": 2},
    ]
    requests = [
        {"id": "r1", "timestamp": -5, "cost": 4, "chain": ["A", "B"]},
        {"id": "r2", "timestamp": 0, "cost": 4, "chain": ["A"]},
        {"id": "r3", "timestamp": 2, "cost": 9, "chain": ["A", "B"]},
    ]
    plain = simulate(
        SimulateRequest.model_validate({"buckets": buckets, "requests": requests})
    )
    debt = _run(
        [{**b, "debt_limit": 0} for b in buckets],
        requests,
    )
    assert [r.allowed for r in plain.results] == [r.allowed for r in debt.results]
    for p, d in zip(plain.results, debt.results):
        assert p.rejected_by == d.rejected_by
        for bid, bb in p.balances.items():
            assert d.buckets[bid].balance.before == bb.before
            assert d.buckets[bid].balance.after == bb.after
            assert d.buckets[bid].debt.before == 0
            assert d.buckets[bid].debt.after == 0
    assert plain.final_balances == {b.id: b.balance for b in debt.final_buckets}
    assert all(b.debt == 0 for b in debt.final_buckets)


def test_cost_exceeding_capacity_alone_with_zero_debt_limit_is_unreachable():
    resp = _run([_bdp(initial=10, debt_limit=0)], [_req(cost=11)])
    assert resp.results[0].rejection.reason == "cost_exceeds_limit"


def test_cost_equal_to_capacity_plus_debt_limit_with_full_balance_is_allowed():
    resp = _run([_bdp(initial=10, debt_limit=5)], [_req(cost=15)])
    r = resp.results[0]
    assert r.allowed is True
    v = _view(r, "B")
    assert (v.balance.before, v.balance.after) == (10, 0)
    assert (v.debt.before, v.debt.after) == (0, 5)


def test_within_total_limit_but_beyond_current_credit_is_not_unreachable():
    # cost=15 ≤ 10+5（并非永久不可达），但空桶此刻只有 5 的借贷额度
    resp = _run([_bdp(initial=0, refill_rate=0, debt_limit=5)], [_req(cost=15)])
    rej = resp.results[0].rejection
    assert rej.reason == "debt_limit_exceeded"
    assert (rej.available, rej.shortage) == (5, 10)


def test_zero_refill_rate_bucket_with_debt_never_recovers():
    resp = _run(
        [_bdp(initial=3, refill_rate=0, debt_limit=5)],
        [
            _req(id="r1", timestamp=0, cost=8),    # 3 + 5 恰好放行，债务=5
            _req(id="r2", timestamp=100, cost=1),  # 零补充：债务不偿还，不可再借
        ],
    )
    assert [r.allowed for r in resp.results] == [True, False]
    rej = resp.results[1].rejection
    assert rej.reason == "debt_limit_exceeded"
    assert (rej.balance_available, rej.debt_before, rej.available) == (0, 5, 0)
    # cost=16 超过 capacity(10)+debt_limit(5)=15 → 永久不可达原因
    resp2 = _run(
        [_bdp(initial=3, refill_rate=0, debt_limit=5)],
        [_req(id="x", timestamp=0, cost=16)],
    )
    assert resp2.results[0].rejection.reason == "cost_exceeds_limit"


def test_zero_refill_repayment_restores_no_credit_so_later_small_cost_fails():
    # 零补充下部分借债后，时间推进不偿还任何债务，额度不会恢复
    resp = _run(
        [_bdp(initial=3, refill_rate=0, debt_limit=5)],
        [
            _req(id="r1", timestamp=0, cost=6),    # 余额 3 扣光，借 3，债务=3
            _req(id="r2", timestamp=10, cost=1),   # 剩余额度 2 ≥ 1 → 仍可借
            _req(id="r3", timestamp=20, cost=2),   # 债务=4，剩余额度 1 < 2 → 拒绝
        ],
    )
    assert [r.allowed for r in resp.results] == [True, True, False]
    assert resp.results[2].rejection.reason == "debt_limit_exceeded"


# ---------- 连续补充先还债 ----------

def test_refill_repays_debt_before_entering_balance():
    # t=0：余额 2 支付 4 → 债务 2；t=3 补充 3：先还债 2（债务→0），
    # 余 1 进入余额；cost 2 → 余额 1→0，再借 1（债务→1）
    resp = _run(
        [_bdp(initial=2, debt_limit=5)],
        [
            _req(id="r1", timestamp=0, cost=4),
            _req(id="r2", timestamp=3, cost=2),
        ],
    )
    v = _view(resp.results[1], "B")
    assert (v.balance.before, v.balance.after) == (1, 0)
    assert (v.debt.before, v.debt.after) == (0, 1)


def test_refill_fully_consumed_by_debt_leaves_zero_balance():
    # t=0 借满债务=5；t=3 补充 3 全部用于还债（债务 5→2），余额仍为 0，
    # 但还债恢复了 3 的借贷额度，cost 1 靠借债放行（债务 2→3）
    resp = _run(
        [_bdp(initial=0, debt_limit=5)],
        [
            _req(id="r1", timestamp=0, cost=5),
            _req(id="r2", timestamp=3, cost=1),
        ],
    )
    r2 = resp.results[1]
    assert r2.allowed is True
    v = _view(r2, "B")
    assert (v.balance.before, v.balance.after) == (0, 0)
    assert (v.debt.before, v.debt.after) == (2, 3)


def test_refill_beyond_debt_accumulates_balance_capped_at_capacity():
    # 债务 2；长时间补充：2 还债，之后余额恢复且封顶 capacity=10
    resp = _run(
        [_bdp(initial=2, capacity=10, refill_rate=1, debt_limit=5)],
        [
            _req(id="r1", timestamp=0, cost=4),    # 债务=2
            _req(id="r2", timestamp=100, cost=1),  # 早已补满
        ],
    )
    v = _view(resp.results[1], "B")
    assert (v.balance.before, v.balance.after) == (10, 9)
    assert (v.debt.before, v.debt.after) == (0, 0)


def test_partial_repayment_then_borrow_again_keeps_debt_cumulative():
    # 债务=4；t=1 补充 1：债务 4→3、余额仍 0；cost 2：借 2 → 债务 5（触顶）
    resp = _run(
        [_bdp(initial=1, debt_limit=5)],
        [
            _req(id="r1", timestamp=0, cost=5),  # 1→0，借 4
            _req(id="r2", timestamp=1, cost=2),  # 还债 1 后再借 2
        ],
    )
    v = _view(resp.results[1], "B")
    assert (v.balance.before, v.balance.after) == (0, 0)
    assert (v.debt.before, v.debt.after) == (3, 5)


def test_rejected_request_still_advances_time_and_repays_debt():
    # t=0 借满债务 5（余额 0）；t=2 请求 cost 8（capacity+debt_limit=15，未超总上限，
    # 但可用 余额 0 + 额度 3 + 补充还债 2 后恢复的额度 = 5 < 8）→ 拒绝，
    # 但补充还债照常发生：债务 5→3
    resp = _run(
        [_bdp(initial=0, debt_limit=5)],
        [
            _req(id="r1", timestamp=0, cost=5),
            _req(id="r2", timestamp=2, cost=8),
        ],
    )
    r2 = resp.results[1]
    assert r2.allowed is False
    assert r2.rejection.reason == "debt_limit_exceeded"
    v = _view(r2, "B")
    assert v.balance.before == 0 and v.debt.before == 3
    # 原子性：拒绝不动余额/债务（补充已把债务从 5 降到 3）
    assert (v.balance.after, v.debt.after) == (0, 3)
    fb = {b.id: b for b in resp.final_buckets}["B"]
    assert (fb.balance, fb.debt) == (0, 3)


# ---------- 链上原子性 ----------

def test_chain_atomic_rejection_changes_nothing():
    # r1 两桶各扣 4（A 10→6，B 借 4：债务=4）；
    # r2 cost 5：A 余额 6 够；B 余额 0、剩余额度 0（零补充）→ 不可满足，
    # 报 B，A 也不得被扣
    resp = _run(
        [
            _bdp(id="A", initial=10, refill_rate=0, debt_limit=0),
            _bdp(id="B", initial=0, refill_rate=0, debt_limit=4),
        ],
        [
            _req(id="r1", cost=4, chain=["A", "B"]),
            _req(id="r2", cost=5, chain=["A", "B"]),
        ],
    )
    assert [r.allowed for r in resp.results] == [True, False]
    r2 = resp.results[1]
    assert r2.rejected_by == "B"
    va = _view(r2, "A")
    assert (va.balance.before, va.balance.after) == (6, 6)
    assert (va.debt.before, va.debt.after) == (0, 0)
    vb = _view(r2, "B")
    assert (vb.balance.before, vb.balance.after) == (0, 0)
    assert (vb.debt.before, vb.debt.after) == (4, 4)


def test_chain_first_failing_bucket_follows_chain_order():
    resp = _run(
        [
            _bdp(id="A", capacity=2, initial=2, debt_limit=0),
            _bdp(id="B", capacity=2, initial=2, debt_limit=0),
        ],
        [_req(id="r1", cost=5, chain=["A", "B"])],
    )
    r = resp.results[0]
    assert r.allowed is False and r.rejected_by == "A"
    assert r.rejection.reason == "cost_exceeds_limit"


def test_chain_allowance_deducts_each_bucket_independently():
    resp = _run(
        [
            _bdp(id="A", initial=3, debt_limit=10),
            _bdp(id="B", initial=8, debt_limit=2),
        ],
        [_req(id="r1", cost=5, chain=["A", "B"])],
    )
    v = resp.results[0]
    assert v.allowed is True
    va, vb = _view(v, "A"), _view(v, "B")
    assert (va.balance.after, va.debt.after) == (0, 2)
    assert (vb.balance.after, vb.debt.after) == (3, 0)


# ---------- 终态快照 ----------

def test_final_snapshot_refills_unreferenced_buckets_to_last_timestamp():
    resp = _run(
        [
            _bdp(id="A", initial=0, debt_limit=5),
            _bdp(id="B", initial=0, refill_rate=2, debt_limit=5),
        ],
        [
            _req(id="r1", timestamp=0, cost=4, chain=["A"]),  # A 债务=4
            _req(id="r2", timestamp=3, cost=1, chain=["A"]),  # A：补 3 先还债 4→1
        ],
    )
    states = {b.id: b for b in resp.final_buckets}
    # A：t=0 借 4（债务=4）；t=3 补 3 → 还债 2（r3 借 1 后债务=2）。
    # 终态观察时刻即 t=3：余额 0、债务 2，可立即支付 = 0 + (5-2) = 3
    assert (states["A"].balance, states["A"].debt, states["A"].available) == (0, 2, 3)
    # B 从未被引用，从 t_start=0 起零余额无债务补到 t=3：2·3=6 封顶 10
    assert (states["B"].balance, states["B"].debt, states["B"].available) == (6, 0, 11)


# ---------- 排序与结果顺序 ----------

def test_unsorted_input_processed_chronologically_results_keep_order():
    resp = _run(
        [_bdp(initial=1, debt_limit=0)],
        [
            _req(id="late", timestamp=1, cost=1),
            _req(id="early", timestamp=0, cost=1),
        ],
    )
    assert [r.request_id for r in resp.results] == ["late", "early"]
    assert [r.index for r in resp.results] == [0, 1]
    # 实际先处理 t=0 的 early（放行，1→0），t=1 的 late 补 1 后也放行
    assert [r.allowed for r in resp.results] == [True, True]


def test_same_timestamp_keeps_input_order():
    resp = _run(
        [_bdp(initial=1, debt_limit=0)],
        [
            _req(id="r1", cost=1),
            _req(id="r2", cost=1),
        ],
    )
    assert [r.allowed for r in resp.results] == [True, False]


# ---------- Pydantic / 跨字段校验 ----------

def _validate_error(payload):
    with pytest.raises(RequestValidationErrors) as exc:
        DebtSimulateRequest.model_validate(payload)
    return exc.value.errors


def test_duplicate_bucket_ids_rejected():
    errors = _validate_error(
        {"buckets": [_bdp(), _bdp()], "requests": [_req()]}
    )
    assert all(e["type"] == "duplicate_bucket" for e in errors)


def test_unknown_bucket_reference_rejected():
    errors = _validate_error(
        {"buckets": [_bdp()], "requests": [_req(chain=["B", "X"])]}
    )
    assert any(e["type"] == "unknown_bucket_ref" and e["input"] == "X" for e in errors)


def test_duplicate_request_ids_rejected():
    errors = _validate_error(
        {"buckets": [_bdp()], "requests": [_req(), _req()]}
    )
    assert any(e["type"] == "duplicate_request" for e in errors)


def test_bad_debt_limit_rejected_by_pydantic():
    from pydantic import ValidationError

    for bad in (-1, "-1", "nan", "inf"):
        with pytest.raises(ValidationError):
            DebtSimulateRequest.model_validate(
                {"buckets": [_bdp(debt_limit=bad)], "requests": [_req()]}
            )


def test_debt_limit_zero_is_allowed():
    req = DebtSimulateRequest.model_validate(
        {"buckets": [_bdp(debt_limit=0)], "requests": [_req()]}
    )
    assert req.buckets[0].debt_limit == 0.0


def test_inherited_bucket_validators_still_apply():
    from pydantic import ValidationError

    for mutate in (
        lambda b: b.update(capacity=0),
        lambda b: b.update(refill_rate=-1),
        lambda b: b.update(initial=11),
        lambda b: b.update(capacity="oops"),
    ):
        bad = _bdp()
        mutate(bad)
        with pytest.raises(ValidationError):
            DebtSimulateRequest.model_validate(
                {"buckets": [bad], "requests": [_req()]}
            )


def test_bad_request_fields_rejected_by_pydantic():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DebtSimulateRequest.model_validate(
            {"buckets": [_bdp()], "requests": [_req(cost=0)]}
        )
    with pytest.raises(ValidationError):
        DebtSimulateRequest.model_validate(
            {"buckets": [_bdp()], "requests": [_req(chain=[])]}
        )
    with pytest.raises(ValidationError):
        DebtSimulateRequest.model_validate(
            {"buckets": [_bdp()], "requests": [_req(chain=["B", "B"])]}
        )
    with pytest.raises(ValidationError):
        DebtSimulateRequest.model_validate(
            {"buckets": [_bdp()], "requests": [{**_req(), "unexpected": 1}]}
        )


def test_extra_bucket_field_rejected_by_pydantic():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DebtSimulateRequest.model_validate(
            {"buckets": [{**_bdp(), "nope": 1}], "requests": [_req()]}
        )


def test_empty_buckets_or_requests_rejected_by_pydantic():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DebtSimulateRequest.model_validate({"buckets": [], "requests": []})


# ---------- HTTP ----------

def test_http_debt_simulate_ok():
    response = client.post(
        "/debt-simulate",
        json={
            "buckets": [
                {"id": "A", "capacity": 10, "refill_rate": 1, "initial": 2, "debt_limit": 5},
                {"id": "B", "capacity": 4, "refill_rate": 0, "initial": 0, "debt_limit": 4},
            ],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A", "B"]},
                {"id": "r2", "timestamp": 0, "cost": 9, "chain": ["A"]},
                {"id": "r3", "timestamp": 3, "cost": 2, "chain": ["A"]},
                {"id": "r4", "timestamp": 0, "cost": 20, "chain": ["A"]},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert [r["allowed"] for r in data["results"]] == [True, False, True, False]
    r1 = data["results"][0]
    assert r1["buckets"]["A"] == {
        "balance": {"before": 2.0, "after": 0.0},
        "debt": {"before": 0.0, "after": 2.0},
    }
    assert r1["buckets"]["B"]["debt"] == {"before": 0.0, "after": 4.0}
    r2 = data["results"][1]
    assert r2["rejected_by"] == "A"
    assert r2["rejection"]["reason"] == "debt_limit_exceeded"
    assert r2["rejection"]["shortage"] == 6.0
    # r3（t=3）：补充 3 先还债 2，余 1 进余额，cost 2 再借 1
    r3 = data["results"][2]
    assert r3["buckets"]["A"] == {
        "balance": {"before": 1.0, "after": 0.0},
        "debt": {"before": 0.0, "after": 1.0},
    }
    r4 = data["results"][3]
    assert r4["rejection"]["reason"] == "cost_exceeds_limit"
    assert (data["allowed_count"], data["rejected_count"]) == (2, 2)
    states = {b["id"]: b for b in data["final_buckets"]}
    assert (states["A"]["balance"], states["A"]["debt"], states["A"]["available"]) == (
        0.0, 1.0, 4.0
    )
    assert (states["B"]["balance"], states["B"]["debt"], states["B"]["available"]) == (
        0.0, 4.0, 0.0
    )


def test_http_validation_error_is_422():
    response = client.post(
        "/debt-simulate",
        json={
            "buckets": [
                {"id": "B", "capacity": 5, "refill_rate": 1, "debt_limit": 1}
            ],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chain": ["GHOST"]}
            ],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_bucket_ref" for e in detail)


def test_http_bad_debt_limit_is_422():
    response = client.post(
        "/debt-simulate",
        json={
            "buckets": [
                {"id": "B", "capacity": 5, "refill_rate": 1, "debt_limit": -2}
            ],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chain": ["B"]}
            ],
        },
    )
    assert response.status_code == 422


def test_http_root_lists_debt_simulate():
    root = client.get("/").json()
    assert root["debt_simulate"] == "/debt-simulate"
