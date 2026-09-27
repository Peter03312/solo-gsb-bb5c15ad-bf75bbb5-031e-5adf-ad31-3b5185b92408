"""初始余额规划引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import SimulateRequest
from app.planner import plan_initial_balances
from app.simulation import simulate

client = TestClient(app)


def _plan(buckets, requests):
    return plan_initial_balances(
        SimulateRequest.model_validate({"buckets": buckets, "requests": requests})
    )


def _planned(resp):
    return {b.id: b for b in resp.buckets}


def _replay_final(buckets, requests, initials):
    """用规划值回放，返回 (是否全部放行, final_balances)。"""
    replay = simulate(
        SimulateRequest.model_validate(
            {
                "buckets": [
                    {**b, "initial": initials[b["id"]]} for b in buckets
                ],
                "requests": requests,
            }
        )
    )
    return all(r.allowed for r in replay.results), replay.final_balances


def test_single_bucket_minimum_initial():
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 0}],
        [{"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"]}],
    )
    assert resp.reachable is True
    assert resp.first_failure is None
    b = _planned(resp)["A"]
    assert b.required_initial == 4
    # 请求未给 initial -> 按 capacity 计
    assert b.requested_initial == 10
    assert b.delta == -6
    # 用规划值 4 回放：4 - 4 = 0
    assert resp.final_balances == {"A": 0}


def test_refill_reduces_required_initial():
    # initial 3.5：r1 后余 2.5，1 秒补 0.5 恰好够 r2 的 3
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 0.5}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"]},
            {"id": "r2", "timestamp": 1, "cost": 3, "chain": ["A"]},
        ],
    )
    b = _planned(resp)["A"]
    assert b.required_initial == 3.5
    assert resp.final_balances == {"A": 0}


def test_planned_initial_is_minimal_and_sufficient():
    buckets = [{"id": "A", "capacity": 10, "refill_rate": 0.5}]
    requests = [
        {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"]},
        {"id": "r2", "timestamp": 1, "cost": 3, "chain": ["A"]},
    ]
    resp = _plan(buckets, requests)
    required = _planned(resp)["A"].required_initial
    # 规划值完整回放：全部放行
    all_allowed, _ = _replay_final(buckets, requests, {"A": required})
    assert all_allowed is True
    # 少一点都不够：r2 被拒
    below = simulate(
        SimulateRequest.model_validate(
            {
                "buckets": [{**buckets[0], "initial": required - 1e-3}],
                "requests": requests,
            }
        )
    )
    assert below.results[0].allowed is True
    assert below.results[1].allowed is False


def test_consumption_counted_in_full_regardless_of_other_buckets():
    # 真实模拟中 r1 会因 B 不足被原子拒绝、A 不扣减；但规划时 A 必须计入 r1 的消耗
    resp = _plan(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 100, "refill_rate": 0, "initial": 3},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A", "B"]},
            {"id": "r2", "timestamp": 0, "cost": 5, "chain": ["A"]},
        ],
    )
    assert resp.reachable is True
    planned = _planned(resp)
    assert planned["A"].required_initial == 10  # 5 + 5，而非 5
    assert planned["B"].required_initial == 5
    assert planned["B"].delta == 2  # 请求 initial 3 -> 需 5
    assert resp.final_balances == {"A": 0, "B": 0}


def test_multi_bucket_plan_matches_hand_computed():
    resp = _plan(
        [
            {"id": "tenant_quota", "capacity": 100, "refill_rate": 10},
            {"id": "api_qps", "capacity": 8, "refill_rate": 5},
            {"id": "user_quota", "capacity": 20, "refill_rate": 1, "initial": 3},
        ],
        [
            {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps", "user_quota"]},
            {"id": "req-2", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
            {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
            {"id": "req-4", "timestamp": 2, "cost": 4, "chain": ["tenant_quota", "api_qps", "user_quota"]},
        ],
    )
    assert resp.reachable is True
    planned = _planned(resp)
    # tenant_quota：t=0 连扣两笔 4，之后每秒补 10 足够覆盖 -> 8
    assert planned["tenant_quota"].required_initial == 8
    assert planned["tenant_quota"].requested_initial == 100
    assert planned["tenant_quota"].delta == -92
    # api_qps：t=0 两笔 4 恰好占满容量 -> 8
    assert planned["api_qps"].required_initial == 8
    assert planned["api_qps"].delta == 0
    # user_quota：t=0 扣 4，t=2 前补 2，还需 2 -> 6
    assert planned["user_quota"].required_initial == 6
    assert planned["user_quota"].delta == 3
    assert resp.final_balances == {"tenant_quota": 12, "api_qps": 2, "user_quota": 0}


def test_unreferenced_bucket_requires_zero_initial():
    resp = _plan(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "C", "capacity": 7, "refill_rate": 1, "initial": 7},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 2, "chain": ["A"]},
            {"id": "r2", "timestamp": 4, "cost": 2, "chain": ["A"]},
        ],
    )
    planned = _planned(resp)
    assert planned["A"].required_initial == 4
    assert planned["C"].required_initial == 0
    assert planned["C"].delta == -7
    # C 以规划值 0 起步，从 t_start=0 补到 t_end=4
    assert resp.final_balances == {"A": 0, "C": 4}


def test_initial_held_from_earliest_request_timestamp():
    # A 首个事件在 t=10，但 initial 在 t_start=0 就持有，期间补充计入
    resp = _plan(
        [
            {"id": "A", "capacity": 100, "refill_rate": 1},
            {"id": "B", "capacity": 100, "refill_rate": 0},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["B"]},
            {"id": "r2", "timestamp": 10, "cost": 15, "chain": ["A"]},
        ],
    )
    planned = _planned(resp)
    # A：t=0..10 补 10，需 15 -> initial 5
    assert planned["A"].required_initial == 5
    assert planned["B"].required_initial == 1


def test_same_timestamp_requests_accumulate():
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"]},
            {"id": "r2", "timestamp": 0, "cost": 4, "chain": ["A"]},
        ],
    )
    assert _planned(resp)["A"].required_initial == 7


def test_required_initial_within_tolerance():
    # 精确最低值 4.5：r1 后余 2.5，0.5 秒补 0.5 得 3.0 恰好够 r2
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 2, "chain": ["A"]},
            {"id": "r2", "timestamp": 0.5, "cost": 3, "chain": ["A"]},
        ],
    )
    required = _planned(resp)["A"].required_initial
    assert abs(required - 4.5) <= 1e-6


def test_required_initial_equal_to_capacity_is_reachable():
    resp = _plan(
        [{"id": "A", "capacity": 5, "refill_rate": 0}],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"]}],
    )
    assert resp.reachable is True
    assert _planned(resp)["A"].required_initial == 5
    assert resp.final_balances == {"A": 0}


def test_out_of_order_requests_planned_chronologically():
    # 输入乱序，规划必须按时间顺序：r_late(t=5) 前的补充只算到 r_early(t=0) 之后
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r_late", "timestamp": 5, "cost": 5, "chain": ["A"]},
            {"id": "r_early", "timestamp": 0, "cost": 4, "chain": ["A"]},
        ],
    )
    # t=0 需 4；t=5 需 5，期间补 5 -> 最低 initial = 4
    assert _planned(resp)["A"].required_initial == 4
    assert resp.final_balances == {"A": 0}


# ---------- 不可行 ----------

def test_unreachable_when_cost_exceeds_capacity():
    resp = _plan(
        [{"id": "A", "capacity": 5, "refill_rate": 0}],
        [{"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A"]}],
    )
    assert resp.reachable is False
    assert resp.buckets is None
    assert resp.final_balances is None
    f = resp.first_failure
    assert f.request_id == "r1" and f.index == 0 and f.timestamp == 0
    assert f.bucket_id == "A"
    assert f.cost == 6 and f.available == 5


def test_unreachable_reports_first_failing_request_in_processing_order():
    # 零速率容量 10：r1 扣 6 后余 4，r2 成首个无法满足的请求
    resp = _plan(
        [{"id": "A", "capacity": 10, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A"]},
            {"id": "r2", "timestamp": 1, "cost": 6, "chain": ["A"]},
        ],
    )
    assert resp.reachable is False
    f = resp.first_failure
    assert f.request_id == "r2" and f.index == 1
    assert f.available == 4


def test_first_failure_uses_processing_order_not_input_order():
    # 输入乱序：r_late 先出现但时间更晚；首个失败应是时间更早的 r_early
    resp = _plan(
        [{"id": "A", "capacity": 5, "refill_rate": 0}],
        [
            {"id": "r_late", "timestamp": 9, "cost": 5, "chain": ["A"]},
            {"id": "r_early", "timestamp": 1, "cost": 6, "chain": ["A"]},
        ],
    )
    assert resp.reachable is False
    assert resp.first_failure.request_id == "r_early"
    assert resp.first_failure.index == 1
    assert resp.first_failure.timestamp == 1


def test_first_failure_bucket_follows_chain_order():
    # 两个桶都不足时，报告链上首个
    resp = _plan(
        [
            {"id": "A", "capacity": 1, "refill_rate": 0},
            {"id": "B", "capacity": 1, "refill_rate": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chain": ["B", "A"]}],
    )
    assert resp.reachable is False
    assert resp.first_failure.bucket_id == "B"


def test_unreachable_even_with_later_refill_when_zero_rate_gap():
    # A 充足，B 零速率且容量装不下累计消耗
    resp = _plan(
        [
            {"id": "A", "capacity": 100, "refill_rate": 10},
            {"id": "B", "capacity": 5, "refill_rate": 0},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A", "B"]},
            {"id": "r2", "timestamp": 10, "cost": 4, "chain": ["A", "B"]},
        ],
    )
    assert resp.reachable is False
    f = resp.first_failure
    assert f.request_id == "r2" and f.bucket_id == "B"
    assert f.available == 1


# ---------- HTTP ----------

def test_http_plan_ok():
    response = client.post(
        "/plan-initial-balances",
        json={
            "buckets": [
                {"id": "A", "capacity": 10, "refill_rate": 1},
                {"id": "B", "capacity": 8, "refill_rate": 0},
            ],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A", "B"]},
                {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A", "B"]},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["reachable"] is True
    assert data["first_failure"] is None
    planned = {b["id"]: b for b in data["buckets"]}
    # A：t=0 需 6，t=1 需 1 但期间补 1 -> 6
    assert planned["A"]["required_initial"] == 6
    assert planned["A"]["requested_initial"] == 10
    assert planned["A"]["delta"] == -4
    # B：零速率，两笔全计 -> 7
    assert planned["B"]["required_initial"] == 7
    assert planned["B"]["delta"] == -1
    assert data["final_balances"] == {"A": 0, "B": 0}


def test_http_plan_unreachable():
    response = client.post(
        "/plan-initial-balances",
        json={
            "buckets": [{"id": "A", "capacity": 5, "refill_rate": 0}],
            "requests": [{"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A"]}],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["reachable"] is False
    assert data["buckets"] is None
    assert data["final_balances"] is None
    failure = data["first_failure"]
    assert failure["request_id"] == "r1"
    assert failure["bucket_id"] == "A"
    assert failure["cost"] == 6
    assert failure["available"] == 5


def test_http_plan_final_balances_match_replay():
    payload = {
        "buckets": [
            {"id": "A", "capacity": 10, "refill_rate": 0.5},
            {"id": "B", "capacity": 20, "refill_rate": 2, "initial": 1},
        ],
        "requests": [
            {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A", "B"]},
            {"id": "r2", "timestamp": 2, "cost": 4, "chain": ["B"]},
            {"id": "r3", "timestamp": 3, "cost": 2, "chain": ["A", "B"]},
        ],
    }
    data = client.post("/plan-initial-balances", json=payload).json()
    assert data["reachable"] is True
    initials = {b["id"]: b["required_initial"] for b in data["buckets"]}
    all_allowed, final = _replay_final(payload["buckets"], payload["requests"], initials)
    assert all_allowed is True
    assert data["final_balances"] == final


def test_http_plan_validation_error_is_422():
    response = client.post(
        "/plan-initial-balances",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["GHOST"]}],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_bucket_ref" for e in detail)


def test_http_plan_duplicate_request_id_is_422():
    response = client.post(
        "/plan-initial-balances",
        json={
            "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0}],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]},
                {"id": "r", "timestamp": 1, "cost": 1, "chain": ["A"]},
            ],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "duplicate_request" for e in detail)


def test_http_plan_extra_field_rejected():
    response = client.post(
        "/plan-initial-balances",
        json={
            "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
            "oops": 1,
        },
    )
    assert response.status_code == 422


def test_http_plan_value_error_is_422_not_500():
    # 校验器抛出的 ValueError（initial > capacity）含不可序列化的 ctx.error，
    # 必须仍按 422 结构返回而非 500
    response = client.post(
        "/plan-initial-balances",
        json={
            "buckets": [{"id": "A", "capacity": 5, "refill_rate": 1, "initial": 6}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert any("initial" in e["msg"] for e in detail)
