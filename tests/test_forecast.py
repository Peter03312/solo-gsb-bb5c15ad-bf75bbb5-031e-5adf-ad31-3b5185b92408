"""等待时间预测引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.main import app
from app.models import ForecastRequest
from app.forecast import forecast

client = TestClient(app)


def _run(now, cost, chain, buckets):
    return forecast(
        ForecastRequest.model_validate(
            {"now": now, "cost": cost, "chain": chain, "buckets": buckets}
        )
    )


def test_immediately_passable_when_all_balances_sufficient():
    resp = _run(
        now=100,
        cost=5,
        chain=["A", "B"],
        buckets=[
            {"id": "A", "capacity": 10, "refill_rate": 1, "balance": 10},
            {"id": "B", "capacity": 5, "refill_rate": 0, "balance": 5},
        ],
    )
    assert resp.reachable is True
    assert resp.wait_seconds == 0
    assert resp.pass_at == 100
    # 立即可通过时没有限制桶
    assert resp.limiting_bucket is None
    # 预计余额即当前余额（不扣减）
    assert [b.projected_balance for b in resp.buckets] == [10, 5]
    assert all(b.wait_seconds == 0 for b in resp.buckets)


def test_single_bucket_wait_time():
    # 缺 2 个令牌，速率 2/s -> 等 1 秒
    resp = _run(
        now=0,
        cost=5,
        chain=["A"],
        buckets=[{"id": "A", "capacity": 10, "refill_rate": 2, "balance": 3}],
    )
    assert resp.reachable is True
    assert resp.wait_seconds == 1
    assert resp.pass_at == 1
    assert resp.limiting_bucket == "A"
    # 到达可通过时刻时恰好补到 cost（不扣减）
    assert resp.buckets[0].projected_balance == 5


def test_chain_wait_is_max_and_limiting_bucket_identified():
    # A 已满足；B 缺 4、速率 1/s -> 等 4 秒，B 为限制桶
    resp = _run(
        now=10,
        cost=5,
        chain=["A", "B"],
        buckets=[
            {"id": "A", "capacity": 10, "refill_rate": 1, "balance": 10},
            {"id": "B", "capacity": 10, "refill_rate": 1, "balance": 1},
        ],
    )
    assert resp.reachable is True
    assert resp.wait_seconds == 4
    assert resp.pass_at == 14
    assert resp.limiting_bucket == "B"
    # A 在通过时刻早已补满，封顶 capacity
    assert resp.buckets[0].projected_balance == 10
    assert resp.buckets[1].projected_balance == 5


def test_projected_balance_capped_at_capacity():
    # A 需等 0.5 秒；B 已满足且速率快，0.5 秒本应补到 10.5，封顶 10
    resp = _run(
        now=0,
        cost=5,
        chain=["A", "B"],
        buckets=[
            {"id": "A", "capacity": 10, "refill_rate": 2, "balance": 4},
            {"id": "B", "capacity": 10, "refill_rate": 10, "balance": 9.5},
        ],
    )
    assert resp.wait_seconds == 0.5
    assert resp.limiting_bucket == "A"
    assert resp.buckets[1].projected_balance == 10


def test_limiting_bucket_tie_breaks_by_chain_order():
    # A、B 等待时间相同 -> 取链上靠前的 A
    resp = _run(
        now=0,
        cost=4,
        chain=["A", "B"],
        buckets=[
            {"id": "A", "capacity": 10, "refill_rate": 1, "balance": 2},
            {"id": "B", "capacity": 10, "refill_rate": 2, "balance": 0},
        ],
    )
    assert resp.wait_seconds == 2
    assert resp.limiting_bucket == "A"


def test_zero_refill_rate_marked_unreachable():
    resp = _run(
        now=0,
        cost=5,
        chain=["A", "B"],
        buckets=[
            {"id": "A", "capacity": 10, "refill_rate": 1, "balance": 1},
            {"id": "B", "capacity": 10, "refill_rate": 0, "balance": 2},
        ],
    )
    assert resp.reachable is False
    assert resp.wait_seconds is None
    assert resp.pass_at is None
    assert resp.limiting_bucket is None
    a, b = resp.buckets
    # A 单独可达，仍给出自身等待时间
    assert a.reachable is True and a.wait_seconds == 4
    assert b.reachable is False and b.reason == "zero_refill_rate"
    assert b.wait_seconds is None
    # 链不可达时没有预计余额
    assert all(x.projected_balance is None for x in resp.buckets)


def test_cost_exceeds_capacity_marked_unreachable():
    # 速率再快也装不下 cost
    resp = _run(
        now=0,
        cost=11,
        chain=["A"],
        buckets=[{"id": "A", "capacity": 10, "refill_rate": 100, "balance": 10}],
    )
    assert resp.reachable is False
    assert resp.buckets[0].reason == "cost_exceeds_capacity"
    assert resp.wait_seconds is None and resp.pass_at is None


def test_zero_rate_bucket_already_sufficient_is_reachable():
    # 零速率但余额已够 -> 立即可通过
    resp = _run(
        now=7,
        cost=5,
        chain=["A"],
        buckets=[{"id": "A", "capacity": 10, "refill_rate": 0, "balance": 6}],
    )
    assert resp.reachable is True
    assert resp.wait_seconds == 0
    assert resp.pass_at == 7
    assert resp.buckets[0].projected_balance == 6


def test_absolute_now_pass_at_uses_offset_only():
    # 大时间原点（如 Unix 秒）：pass_at = now + 等待秒数
    resp = _run(
        now=1_700_000_000,
        cost=10,
        chain=["A"],
        buckets=[{"id": "A", "capacity": 10, "refill_rate": 2, "balance": 0}],
    )
    assert resp.wait_seconds == 5
    assert resp.pass_at == 1_700_000_005


def test_fractional_rates_round_cleanly():
    # 缺 4、速率 3/s -> 4/3 秒；预计余额应收敛回 cost 而非浮点长尾
    resp = _run(
        now=0,
        cost=5,
        chain=["A"],
        buckets=[{"id": "A", "capacity": 10, "refill_rate": 3, "balance": 1}],
    )
    assert resp.wait_seconds == round(4 / 3, 9)
    assert resp.buckets[0].projected_balance == 5


# ---------- 校验 ----------

def _validate_error(payload):
    with pytest.raises(RequestValidationErrors) as exc:
        ForecastRequest.model_validate(payload)
    return exc.value.errors


def test_duplicate_bucket_ids_rejected():
    errors = _validate_error(
        {
            "now": 0,
            "cost": 1,
            "chain": ["A"],
            "buckets": [
                {"id": "A", "capacity": 1, "refill_rate": 0, "balance": 0},
                {"id": "A", "capacity": 2, "refill_rate": 0, "balance": 0},
            ],
        }
    )
    assert all(e["type"] == "duplicate_bucket" for e in errors)


def test_unknown_bucket_reference_rejected():
    errors = _validate_error(
        {
            "now": 0,
            "cost": 1,
            "chain": ["A", "X"],
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0, "balance": 0}],
        }
    )
    assert any(e["type"] == "unknown_bucket_ref" and e["input"] == "X" for e in errors)


def test_balance_above_capacity_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ForecastRequest.model_validate(
            {
                "now": 0,
                "cost": 1,
                "chain": ["A"],
                "buckets": [{"id": "A", "capacity": 5, "refill_rate": 1, "balance": 6}],
            }
        )


def test_invalid_numbers_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ForecastRequest.model_validate(
            {
                "now": 0,
                "cost": -1,
                "chain": ["A"],
                "buckets": [{"id": "A", "capacity": 5, "refill_rate": 1, "balance": 0}],
            }
        )
    with pytest.raises(ValidationError):
        ForecastRequest.model_validate(
            {
                "now": 0,
                "cost": 1,
                "chain": ["A"],
                "buckets": [{"id": "A", "capacity": 5, "refill_rate": -1, "balance": 0}],
            }
        )


def test_duplicate_bucket_within_chain_rejected_by_pydantic():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ForecastRequest.model_validate(
            {
                "now": 0,
                "cost": 1,
                "chain": ["A", "B", "A"],
                "buckets": [
                    {"id": "A", "capacity": 1, "refill_rate": 0, "balance": 0},
                    {"id": "B", "capacity": 1, "refill_rate": 0, "balance": 0},
                ],
            }
        )


# ---------- HTTP ----------

def test_http_forecast_ok():
    response = client.post(
        "/forecast",
        json={
            "now": 1000,
            "cost": 4,
            "chain": ["tenant_quota", "api_qps"],
            "buckets": [
                {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "balance": 96},
                {"id": "api_qps", "capacity": 5, "refill_rate": 5, "balance": 1},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["reachable"] is True
    # api_qps 缺 3、速率 5/s -> 0.6 秒，为限制桶
    assert data["wait_seconds"] == 0.6
    assert data["pass_at"] == 1000.6
    assert data["limiting_bucket"] == "api_qps"
    buckets = {b["id"]: b for b in data["buckets"]}
    assert buckets["tenant_quota"]["projected_balance"] == 100  # 封顶
    assert buckets["api_qps"]["projected_balance"] == 4


def test_http_forecast_unreachable():
    response = client.post(
        "/forecast",
        json={
            "now": 0,
            "cost": 5,
            "chain": ["A"],
            "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0, "balance": 1}],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["reachable"] is False
    assert data["wait_seconds"] is None
    assert data["pass_at"] is None
    assert data["limiting_bucket"] is None
    assert data["buckets"][0]["reason"] == "zero_refill_rate"


def test_http_forecast_validation_error_is_422():
    response = client.post(
        "/forecast",
        json={
            "now": 0,
            "cost": 1,
            "chain": ["GHOST"],
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0, "balance": 0}],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_bucket_ref" for e in detail)


def test_http_forecast_extra_field_rejected():
    response = client.post(
        "/forecast",
        json={
            "now": 0,
            "cost": 1,
            "chain": ["A"],
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0, "balance": 0}],
            "oops": 1,
        },
    )
    assert response.status_code == 422
