"""严格 FIFO 排队试算（/queue-simulate）引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.main import app
from app.models import QueueSimulateRequest
from app.queueing import queue_simulate

client = TestClient(app)


def _run(buckets, requests):
    return queue_simulate(
        QueueSimulateRequest.model_validate({"buckets": buckets, "requests": requests})
    )


# ---------- 队首等待与执行 ----------

def test_immediate_execution_when_head_and_balance_ready():
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [{"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"], "deadline": 5}],
    )
    r = resp.results[0]
    assert r.status == "executed"
    assert r.executed_at == 0
    assert r.expired_at is None
    assert r.wait_seconds == 0
    assert r.limiting_bucket is None
    assert r.balances["A"].before == 10 and r.balances["A"].after == 6
    assert resp.executed_count == 1 and resp.expired_count == 0
    assert resp.final_balances == {"A": 6}


def test_head_waits_for_refill_and_executes_at_earliest_ready_time():
    # 初始 1，速率 1/s，cost 3 -> 最早可执行时刻 t=2；deadline 5 足够
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 1}],
        [{"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"], "deadline": 5}],
    )
    r = resp.results[0]
    assert r.status == "executed"
    assert r.executed_at == 2
    assert r.wait_seconds == 2
    assert r.limiting_bucket == "A"
    assert r.balances["A"].before == 3 and r.balances["A"].after == 0
    assert resp.final_balances == {"A": 0}


def test_balance_sufficient_exactly_at_deadline_still_executes():
    # deadline 时刻余额恰好充足：仍应原子扣减并成功
    resp = _run(
        [{"id": "A", "capacity": 5, "refill_rate": 1, "initial": 0}],
        [{"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"], "deadline": 3}],
    )
    r = resp.results[0]
    assert r.status == "executed"
    assert r.executed_at == 3
    assert r.wait_seconds == 3
    assert r.balances["A"].before == 3 and r.balances["A"].after == 0
    assert resp.final_balances == {"A": 0}


def test_chain_atomic_deduction_on_execute():
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 8, "refill_rate": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A", "B"], "deadline": 1}],
    )
    r = resp.results[0]
    assert r.status == "executed"
    assert r.balances["A"].after == 5
    assert r.balances["B"].after == 3


# ---------- 过期 ----------

def test_expires_at_deadline_when_refill_too_slow():
    # t=3 才能补到 3，但 deadline 是 2 -> 在 t=2 过期，不扣减
    resp = _run(
        [{"id": "A", "capacity": 5, "refill_rate": 1, "initial": 0}],
        [{"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"], "deadline": 2}],
    )
    r = resp.results[0]
    assert r.status == "expired"
    assert r.executed_at is None
    assert r.expired_at == 2
    assert r.wait_seconds == 2
    assert r.limiting_bucket == "A"
    # 过期时刻余额照常连续补充，但不扣减（原子性）
    assert r.balances["A"].before == 2 and r.balances["A"].after == 2
    assert resp.final_balances == {"A": 2}
    assert resp.executed_count == 0 and resp.expired_count == 1


def test_cost_exceeds_capacity_head_waits_until_deadline_then_expires():
    # cost 超过容量：不可达，但仍等待至 deadline 才过期；后继随后继续
    resp = _run(
        [{"id": "A", "capacity": 4, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "deadline": 3},
            {"id": "r2", "timestamp": 0, "cost": 4, "chain": ["A"], "deadline": 10},
        ],
    )
    r1, r2 = resp.results
    assert r1.status == "expired" and r1.expired_at == 3
    assert r1.limiting_bucket == "A"
    assert r1.balances["A"].before == 4 and r1.balances["A"].after == 4
    # r2 在 r1 过期后成为队首：t=3 时余额 4 恰好充足 -> 立即执行
    assert r2.status == "executed" and r2.executed_at == 3
    assert r2.wait_seconds == 3
    assert r2.limiting_bucket is None
    assert r2.balances["A"].before == 4 and r2.balances["A"].after == 0
    assert resp.final_balances == {"A": 0}


def test_zero_refill_insufficient_head_waits_until_deadline_then_expires():
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 0, "initial": 2}],
        [{"id": "r1", "timestamp": 1, "cost": 5, "chain": ["A"], "deadline": 4}],
    )
    r = resp.results[0]
    assert r.status == "expired"
    assert r.expired_at == 4
    assert r.wait_seconds == 3
    assert r.limiting_bucket == "A"
    assert r.balances["A"].before == 2 and r.balances["A"].after == 2
    assert resp.final_balances == {"A": 2}


def test_deadline_equal_timestamp_expires_immediately_when_insufficient():
    resp = _run(
        [{"id": "A", "capacity": 5, "refill_rate": 1, "initial": 1}],
        [{"id": "r1", "timestamp": 2, "cost": 3, "chain": ["A"], "deadline": 2}],
    )
    r = resp.results[0]
    assert r.status == "expired"
    assert r.expired_at == 2
    assert r.wait_seconds == 0
    assert r.balances["A"].before == 1 and r.balances["A"].after == 1


# ---------- 严格 FIFO ----------

def test_arriving_request_cannot_overtake_head():
    # r1 等待 A 补充至 t=3 才能执行；r2 在 t=1 到达时 B 已充足，
    # 但严格 FIFO 下不得越过队首，须等 r1 在 t=3 出队后才执行
    resp = _run(
        [
            {"id": "A", "capacity": 5, "refill_rate": 1, "initial": 0},
            {"id": "B", "capacity": 10, "refill_rate": 0, "initial": 10},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"], "deadline": 10},
            {"id": "r2", "timestamp": 1, "cost": 2, "chain": ["B"], "deadline": 10},
        ],
    )
    r1, r2 = resp.results
    assert r1.status == "executed" and r1.executed_at == 3
    assert r2.status == "executed" and r2.executed_at == 3
    assert r2.wait_seconds == 2  # t=1 到达，t=3 才成为队首并立即执行
    assert r2.limiting_bucket is None  # 成为队首即可执行，无桶限制
    assert r2.balances["B"].before == 10 and r2.balances["B"].after == 8


def test_expired_head_unblocks_successor():
    resp = _run(
        [{"id": "A", "capacity": 4, "refill_rate": 1, "initial": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"], "deadline": 2},
            {"id": "r2", "timestamp": 0, "cost": 4, "chain": ["A"], "deadline": 10},
        ],
    )
    r1, r2 = resp.results
    # r1：t=4 才能补到 4，deadline 2 -> 在 t=2 过期，余额 2 不扣
    assert r1.status == "expired" and r1.expired_at == 2
    assert r1.balances["A"].before == 2 and r1.balances["A"].after == 2
    # r2：t=2 成为队首，余额 2，还需 2 秒 -> t=4 执行
    assert r2.status == "executed" and r2.executed_at == 4
    assert r2.balances["A"].before == 4 and r2.balances["A"].after == 0
    assert resp.final_balances == {"A": 0}


def test_deadline_passed_while_queued_expires_on_becoming_head():
    # r2 的 deadline 在它成为队首之前就已过去：成为队首时立即过期
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "deadline": 100},
            {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "deadline": 3},
        ],
    )
    r1, r2 = resp.results
    assert r1.status == "executed" and r1.executed_at == 5
    # r2 在 t=5 才成为队首，deadline 3 已过 -> 立即过期
    assert r2.status == "expired" and r2.expired_at == 5
    assert r2.wait_seconds == 4
    assert r2.limiting_bucket == "A"
    # t=5 时 A 余额 0（r1 刚扣完），r2 不扣减
    assert r2.balances["A"].before == 0 and r2.balances["A"].after == 0


def test_same_timestamp_requests_keep_input_order():
    resp = _run(
        [{"id": "A", "capacity": 1, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": 1},
            {"id": "r2", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": 1},
        ],
    )
    assert resp.results[0].status == "executed" and resp.results[0].executed_at == 0
    # r2：零补充不可达，等到 deadline t=1 过期
    assert resp.results[1].status == "expired" and resp.results[1].expired_at == 1


def test_out_of_order_timestamps_queued_by_arrival_results_in_input_order():
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [
            {"id": "r_late", "timestamp": 5, "cost": 2, "chain": ["A"], "deadline": 10},
            {"id": "r_early", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": 10},
        ],
    )
    # 队列顺序按到达时间：r_early 先入队，t=1 执行；r_late t=5 到达即执行
    assert [r.request_id for r in resp.results] == ["r_late", "r_early"]
    assert [r.index for r in resp.results] == [0, 1]
    assert resp.results[1].executed_at == 1
    assert resp.results[0].executed_at == 5
    assert resp.results[0].wait_seconds == 0
    assert resp.final_balances == {"A": 2}


# ---------- 限制桶与终态 ----------

def test_limiting_bucket_is_slowest_on_chain():
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0},
            {"id": "B", "capacity": 10, "refill_rate": 0.5, "initial": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 2, "chain": ["A", "B"], "deadline": 10}],
    )
    r = resp.results[0]
    # A 需 2 秒，B 需 4 秒 -> B 限制，t=4 执行
    assert r.executed_at == 4
    assert r.limiting_bucket == "B"
    assert r.balances["A"].before == 4 and r.balances["A"].after == 2
    assert r.balances["B"].before == 2 and r.balances["B"].after == 0


def test_limiting_bucket_tie_takes_first_in_chain():
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0},
            {"id": "B", "capacity": 10, "refill_rate": 1, "initial": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 2, "chain": ["B", "A"], "deadline": 10}],
    )
    r = resp.results[0]
    assert r.executed_at == 2
    assert r.limiting_bucket == "B"  # 并列取链上首个


def test_final_balances_advance_to_last_resolve_time():
    # 最后一个请求在 t=6 才执行，终态应推进到 t=6（而非最后到达时刻）
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0},
            {"id": "B", "capacity": 100, "refill_rate": 1, "initial": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A"], "deadline": 10}],
    )
    assert resp.results[0].executed_at == 6
    # B 从未被引用，也推进到 t=6
    assert resp.final_balances == {"A": 0, "B": 6}


def test_readme_example_scenario():
    # 与 examples/queue-simulate.json 相同的完整场景
    resp = _run(
        [
            {"id": "api_qps", "capacity": 4, "refill_rate": 1, "initial": 4},
            {"id": "db_quota", "capacity": 10, "refill_rate": 2, "initial": 10},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["api_qps", "db_quota"], "deadline": 5},
            {"id": "r2", "timestamp": 0.5, "cost": 3, "chain": ["api_qps"], "deadline": 6},
            {"id": "r3", "timestamp": 1, "cost": 2, "chain": ["db_quota"], "deadline": 2.5},
            {"id": "r4", "timestamp": 2, "cost": 5, "chain": ["api_qps"], "deadline": 4},
            {"id": "r5", "timestamp": 3, "cost": 4, "chain": ["api_qps"], "deadline": 10},
        ],
    )
    assert [r.status for r in resp.results] == [
        "executed", "executed", "executed", "expired", "executed",
    ]
    assert [r.executed_at for r in resp.results[:3]] == [0, 2, 2]
    assert resp.results[3].expired_at == 4
    assert resp.results[4].executed_at == 6
    assert [r.wait_seconds for r in resp.results] == [0, 1.5, 1, 2, 3]
    assert [r.limiting_bucket for r in resp.results] == [
        None, "api_qps", None, "api_qps", "api_qps",
    ]
    assert resp.executed_count == 4 and resp.expired_count == 1
    assert resp.final_balances == {"api_qps": 0, "db_quota": 10}


# ---------- 校验 ----------

def _validate_error(payload):
    with pytest.raises(RequestValidationErrors) as exc:
        QueueSimulateRequest.model_validate(payload)
    return exc.value.errors


def test_duplicate_bucket_ids_rejected():
    errors = _validate_error(
        {
            "buckets": [
                {"id": "A", "capacity": 1, "refill_rate": 0},
                {"id": "A", "capacity": 2, "refill_rate": 0},
            ],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": 1}],
        }
    )
    assert all(e["type"] == "duplicate_bucket" for e in errors)


def test_unknown_bucket_reference_rejected():
    errors = _validate_error(
        {
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["X"], "deadline": 1}],
        }
    )
    assert any(e["type"] == "unknown_bucket_ref" and e["input"] == "X" for e in errors)


def test_duplicate_request_ids_rejected():
    errors = _validate_error(
        {
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": 1},
                {"id": "r", "timestamp": 1, "cost": 1, "chain": ["A"], "deadline": 2},
            ],
        }
    )
    assert any(e["type"] == "duplicate_request" for e in errors)


def test_deadline_before_timestamp_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        QueueSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [
                    {"id": "r", "timestamp": 2, "cost": 1, "chain": ["A"], "deadline": 1}
                ],
            }
        )


def test_deadline_equal_timestamp_allowed():
    resp = _run(
        [{"id": "A", "capacity": 1, "refill_rate": 0}],
        [{"id": "r", "timestamp": 2, "cost": 1, "chain": ["A"], "deadline": 2}],
    )
    assert resp.results[0].status == "executed"
    assert resp.results[0].executed_at == 2


def test_non_finite_deadline_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        QueueSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": "inf"}
                ],
            }
        )


def test_missing_deadline_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        QueueSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
            }
        )


def test_extra_field_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        QueueSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": 1, "weight": 1}
                ],
            }
        )


# ---------- HTTP ----------

def test_http_queue_simulate_ok():
    response = client.post(
        "/queue-simulate",
        json={
            "buckets": [
                {"id": "api_qps", "capacity": 4, "refill_rate": 1, "initial": 4},
                {"id": "db_quota", "capacity": 10, "refill_rate": 2, "initial": 10},
            ],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["api_qps", "db_quota"], "deadline": 5},
                {"id": "r2", "timestamp": 0.5, "cost": 3, "chain": ["api_qps"], "deadline": 6},
                {"id": "r3", "timestamp": 1, "cost": 2, "chain": ["db_quota"], "deadline": 2.5},
                {"id": "r4", "timestamp": 2, "cost": 5, "chain": ["api_qps"], "deadline": 4},
                {"id": "r5", "timestamp": 3, "cost": 4, "chain": ["api_qps"], "deadline": 10},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["executed_count"] == 4 and data["expired_count"] == 1
    assert [r["status"] for r in data["results"]] == [
        "executed", "executed", "executed", "expired", "executed",
    ]
    # r3 不得越过队首 r2：t=2.0 才执行
    assert data["results"][2]["executed_at"] == 2.0
    assert data["results"][2]["wait_seconds"] == 1.0
    # r4 不可达，等待至 deadline 过期，余额不扣减
    assert data["results"][3]["expired_at"] == 4.0
    assert data["results"][3]["limiting_bucket"] == "api_qps"
    assert data["results"][3]["balances"]["api_qps"] == {"before": 2.0, "after": 2.0}
    assert data["results"][4]["executed_at"] == 6.0
    assert data["final_balances"] == {"api_qps": 0.0, "db_quota": 10.0}


def test_http_validation_error_is_422():
    response = client.post(
        "/queue-simulate",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["GHOST"], "deadline": 1}],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_bucket_ref" for e in detail)


def test_http_deadline_before_timestamp_is_422():
    response = client.post(
        "/queue-simulate",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 2, "cost": 1, "chain": ["A"], "deadline": 1}],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
