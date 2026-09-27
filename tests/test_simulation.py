"""模拟引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.main import app
from app.models import SimulateRequest
from app.simulation import simulate

client = TestClient(app)


def _run(buckets, requests):
    return simulate(SimulateRequest.model_validate({"buckets": buckets, "requests": requests}))


def test_basic_allow_and_deduct():
    # 初始 10，消耗 4 -> 余 6
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 0}],
        [{"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"]}],
    )
    r = resp.results[0]
    assert r.allowed is True
    assert r.rejected_by is None
    assert r.balances["A"].before == 10
    assert r.balances["A"].after == 6
    assert resp.final_balances == {"A": 6}
    assert resp.allowed_count == 1 and resp.rejected_count == 0


def test_continuous_refill_capped_at_capacity():
    # t=0 消耗 8 -> 余 2；t=10 以 1/s 补满到 10（而非 12）
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 8, "chain": ["A"]},
            {"id": "r2", "timestamp": 10, "cost": 1, "chain": ["A"]},
        ],
    )
    assert resp.results[0].balances["A"].after == 2
    assert resp.results[1].balances["A"].before == 10
    assert resp.results[1].balances["A"].after == 9


def test_partial_refill_math():
    # 初始 0，速率 2/s：t=0 被拒（不足 5），t=3 补到 6，消耗 5 -> 余 1
    resp = _run(
        [{"id": "A", "capacity": 100, "refill_rate": 2, "initial": 0}],
        [
            {"id": "r0", "timestamp": 0, "cost": 5, "chain": ["A"]},
            {"id": "r3", "timestamp": 3, "cost": 5, "chain": ["A"]},
        ],
    )
    assert resp.results[0].allowed is False
    assert resp.results[0].balances["A"].after == 0
    assert resp.results[1].balances["A"].before == 6
    assert resp.results[1].balances["A"].after == 1


def test_absolute_timestamps_use_elapsed_time_only():
    # 大时间戳（如 Unix 毫秒/秒）也只应按时间差补充，初始余额在首请求时刻持有
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 1_000_000, "cost": 10, "chain": ["A"]},
            {"id": "r2", "timestamp": 1_000_002, "cost": 2, "chain": ["A"]},
        ],
    )
    assert resp.results[0].balances["A"].after == 0
    # 2 秒后补 2 个令牌
    assert resp.results[1].balances["A"].before == 2
    assert resp.results[1].balances["A"].after == 0


def test_chain_atomic_rejection_leaves_all_buckets_untouched():
    # A=10 充足，B=2 不足 -> 两个桶都不扣
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 2, "refill_rate": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A", "B"]}],
    )
    r = resp.results[0]
    assert r.allowed is False
    assert r.rejected_by == "B"
    assert r.balances["A"].before == 10 and r.balances["A"].after == 10
    assert r.balances["B"].before == 2 and r.balances["B"].after == 2
    assert resp.final_balances == {"A": 10, "B": 2}


def test_rejected_by_first_insufficient_in_chain_order():
    resp = _run(
        [
            {"id": "A", "capacity": 1, "refill_rate": 0},
            {"id": "B", "capacity": 10, "refill_rate": 0},
            {"id": "C", "capacity": 1, "refill_rate": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A", "B", "C"]}],
    )
    assert resp.results[0].rejected_by == "A"


def test_atomic_chain_allow_deducts_every_bucket():
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 8, "refill_rate": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A", "B"]}],
    )
    assert resp.results[0].allowed is True
    assert resp.results[0].balances["A"].after == 5
    assert resp.results[0].balances["B"].after == 3


def test_out_of_order_timestamps_simulated_chronologically():
    # 输入乱序，但必须严格按时间戳处理：
    # 实际顺序 r_early(t=0)：余额 0，cost=1，拒绝、不扣；
    # r_late(t=5)：以 1/s 补到 5，放行并扣到 0。
    # 若错误地按数组顺序处理，r_late 也会因余额 0 被拒。
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [
            {"id": "r_late", "timestamp": 5, "cost": 5, "chain": ["A"]},
            {"id": "r_early", "timestamp": 0, "cost": 1, "chain": ["A"]},
        ],
    )
    # 结果仍按原始下标返回
    assert resp.results[0].request_id == "r_late"
    assert resp.results[0].allowed is True
    assert resp.results[0].balances["A"].before == 5
    assert resp.results[0].balances["A"].after == 0
    assert resp.results[1].request_id == "r_early"
    assert resp.results[1].allowed is False
    assert resp.results[1].rejected_by == "A"
    assert resp.results[1].balances["A"].before == 0
    assert resp.results[1].balances["A"].after == 0


def test_same_timestamp_requests_keep_input_order():
    resp = _run(
        [{"id": "A", "capacity": 1, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"]},
            {"id": "r2", "timestamp": 0, "cost": 1, "chain": ["A"]},
        ],
    )
    assert resp.results[0].allowed is True
    assert resp.results[1].allowed is False
    assert resp.results[1].rejected_by == "A"
    assert resp.results[1].balances["A"].before == 0


def test_rejection_does_not_consume_or_block_refill():
    # t=0 拒绝（需要 10 但只有 5），t=5 以 1/s 补到 10 后放行
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 5}],
        [
            {"id": "r1", "timestamp": 0, "cost": 10, "chain": ["A"]},
            {"id": "r2", "timestamp": 5, "cost": 10, "chain": ["A"]},
        ],
    )
    assert resp.results[0].allowed is False
    assert resp.results[0].balances["A"].after == 5
    assert resp.results[1].allowed is True
    assert resp.results[1].balances["A"].before == 10
    assert resp.results[1].balances["A"].after == 0


def test_zero_refill_bucket_never_recovers():
    resp = _run(
        [{"id": "A", "capacity": 3, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"]},
            {"id": "r2", "timestamp": 100, "cost": 1, "chain": ["A"]},
        ],
    )
    assert resp.results[1].allowed is False
    assert resp.results[1].balances["A"].before == 0


def test_results_in_original_order_with_index():
    resp = _run(
        [{"id": "A", "capacity": 100, "refill_rate": 0}],
        [
            {"id": "r3", "timestamp": 3, "cost": 1, "chain": ["A"]},
            {"id": "r1", "timestamp": 1, "cost": 1, "chain": ["A"]},
            {"id": "r2", "timestamp": 2, "cost": 1, "chain": ["A"]},
        ],
    )
    assert [r.request_id for r in resp.results] == ["r3", "r1", "r2"]
    assert [r.index for r in resp.results] == [0, 1, 2]


# ---------- 校验 ----------

def _validate_error(payload):
    with pytest.raises(RequestValidationErrors) as exc:
        SimulateRequest.model_validate(payload)
    return exc.value.errors


def test_duplicate_bucket_ids_rejected():
    errors = _validate_error(
        {
            "buckets": [
                {"id": "A", "capacity": 1, "refill_rate": 0},
                {"id": "A", "capacity": 2, "refill_rate": 0},
            ],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
        }
    )
    assert all(e["type"] == "duplicate_bucket" for e in errors)


def test_negative_rate_and_capacity_rejected():
    with pytest.raises(Exception):
        SimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": -1, "refill_rate": -2}],
                "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
            }
        )


def test_unknown_bucket_reference_rejected():
    errors = _validate_error(
        {
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A", "X"]}],
        }
    )
    assert any(e["type"] == "unknown_bucket_ref" and e["input"] == "X" for e in errors)


def test_duplicate_bucket_within_chain_rejected_by_pydantic():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SimulateRequest.model_validate(
            {
                "buckets": [
                    {"id": "A", "capacity": 1, "refill_rate": 0},
                    {"id": "B", "capacity": 1, "refill_rate": 0},
                ],
                "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A", "B", "A"]}],
            }
        )


def test_initial_above_capacity_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 5, "refill_rate": 1, "initial": 6}],
                "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
            }
        )


# ---------- HTTP ----------

def test_http_simulate_ok():
    response = client.post(
        "/simulate",
        json={
            "buckets": [
                {"id": "A", "capacity": 10, "refill_rate": 1},
                {"id": "B", "capacity": 5, "refill_rate": 0},
            ],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A", "B"]},
                {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A", "B"]},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    # r1: A=10 充足, B=5 不足 -> 原子拒绝
    assert data["results"][0]["allowed"] is False
    assert data["results"][0]["rejected_by"] == "B"
    assert data["results"][0]["balances"]["A"]["after"] == 10
    # r2: t=1, A 补满 10, B 仍 5 -> 放行，各扣 1
    assert data["results"][1]["allowed"] is True
    assert data["results"][1]["balances"]["A"]["after"] == 9
    assert data["results"][1]["balances"]["B"]["after"] == 4


def test_http_validation_error_is_422():
    response = client.post(
        "/simulate",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["GHOST"]}],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_bucket_ref" for e in detail)


def test_http_value_error_is_422_not_500():
    # 校验器抛出的 ValueError（initial > capacity）含不可序列化的 ctx.error，
    # 必须仍按 422 结构返回而非 500
    response = client.post(
        "/simulate",
        json={
            "buckets": [{"id": "A", "capacity": 5, "refill_rate": 1, "initial": 6}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail


def test_http_health_and_root():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
