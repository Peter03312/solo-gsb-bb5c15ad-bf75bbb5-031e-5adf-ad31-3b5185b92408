"""半开滑动窗口试算（/window-simulate）引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.main import app
from app.models import WindowSimulateRequest
from app.windowing import window_simulate

client = TestClient(app)


def _run(policies, requests):
    return window_simulate(
        WindowSimulateRequest.model_validate({"policies": policies, "requests": requests})
    )


def _view(result, policy_id):
    return result.policies[policy_id]


# ---------- 基本放行 / 记账 ----------

def test_first_request_allowed_and_booked():
    resp = _run(
        [{"id": "P", "window_seconds": 10, "limit": 5}],
        [{"id": "r1", "timestamp": 0, "principal": "a", "cost": 3, "policy_refs": ["P"]}],
    )
    r = resp.results[0]
    assert r.allowed is True
    assert r.rejected_by is None and r.rejection is None
    v = _view(r, "P")
    assert v.used_before == 0
    assert v.used_after == 3
    assert v.remaining_before == 5
    assert v.remaining_after == 2
    assert (v.window.start, v.window.end) == (-10, 0)
    p = resp.policies[0]
    assert p.total_used == 3
    assert p.principals[0].principal == "a"
    assert p.principals[0].used == 3
    assert p.principals[0].remaining == 2
    assert p.principals[0].event_count == 1
    assert resp.allowed_count == 1 and resp.rejected_count == 0


def test_accumulation_until_limit():
    resp = _run(
        [{"id": "P", "window_seconds": 100, "limit": 10}],
        [
            {"id": "r1", "timestamp": 0, "principal": "a", "cost": 6, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 1, "principal": "a", "cost": 4, "policy_refs": ["P"]},
            {"id": "r3", "timestamp": 2, "principal": "a", "cost": 1, "policy_refs": ["P"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, True, False]
    v3 = _view(resp.results[2], "P")
    assert v3.used_before == 10
    assert v3.used_after == 10  # 拒绝不改账
    assert v3.remaining_before == 0
    assert v3.remaining_after == 0
    rej = resp.results[2].rejection
    assert rej.policy_id == "P"
    assert rej.reason == "limit_exceeded"
    assert (rej.used, rej.cost, rej.required, rej.available) == (10, 1, 11, 0)


def test_cost_equal_to_remaining_is_allowed():
    resp = _run(
        [{"id": "P", "window_seconds": 100, "limit": 10}],
        [
            {"id": "r1", "timestamp": 0, "principal": "a", "cost": 6, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 1, "principal": "a", "cost": 4, "policy_refs": ["P"]},
        ],
    )
    assert all(r.allowed for r in resp.results)
    assert _view(resp.results[1], "P").used_after == 10


def test_cost_exceeding_limit_alone_is_rejected():
    resp = _run(
        [{"id": "P", "window_seconds": 100, "limit": 4}],
        [{"id": "r1", "timestamp": 0, "principal": "a", "cost": 5, "policy_refs": ["P"]}],
    )
    assert resp.results[0].allowed is False
    assert resp.results[0].rejected_by == "P"
    assert resp.policies[0].total_used == 0
    assert resp.policies[0].principals == []


# ---------- 半开窗口与过期 ----------

def test_event_exactly_on_left_edge_is_expired():
    # 窗口 (t-w, t]：t=10 时 t=0 的事件恰好落在左边界，必须已过期
    resp = _run(
        [{"id": "P", "window_seconds": 10, "limit": 10}],
        [
            {"id": "r1", "timestamp": 0, "principal": "a", "cost": 10, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 10, "principal": "a", "cost": 10, "policy_refs": ["P"]},
        ],
    )
    assert all(r.allowed for r in resp.results)
    assert _view(resp.results[1], "P").used_before == 0
    assert (_view(resp.results[1], "P").window.start,
            _view(resp.results[1], "P").window.end) == (0, 10)
    # 终态 t=10：旧事件已过期，只剩 r2
    assert resp.policies[0].total_used == 10
    assert resp.policies[0].principals[0].event_count == 1


def test_event_just_inside_left_edge_is_active():
    # t=9.999999999 时 t=0 的事件仍在 (0-eps? ...) 窗口内：used=10 -> 拒绝
    resp = _run(
        [{"id": "P", "window_seconds": 10, "limit": 10}],
        [
            {"id": "r1", "timestamp": 0, "principal": "a", "cost": 10, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 9.999999999, "principal": "a", "cost": 1,
             "policy_refs": ["P"]},
        ],
    )
    assert resp.results[1].allowed is False
    assert resp.results[1].rejected_by == "P"


def test_sliding_window_capacity_freed_at_boundary():
    # 独立、语义清晰的版本：t=5 的事件在 t=15 过期
    resp = _run(
        [{"id": "P", "window_seconds": 10, "limit": 7}],
        [
            {"id": "r1", "timestamp": 0, "principal": "a", "cost": 7, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 5, "principal": "a", "cost": 1, "policy_refs": ["P"]},
            {"id": "r3", "timestamp": 10, "principal": "a", "cost": 1, "policy_refs": ["P"]},
            {"id": "r4", "timestamp": 15, "principal": "a", "cost": 7, "policy_refs": ["P"]},
        ],
    )
    # r2：used=7 拒绝；r3（t=10）：t=0 事件过期 -> used=0 -> 放行 cost 1；
    # r4（t=15）：t=5 无事件（r2 被拒未记账），t=10 事件 1 在 (5,15] 内 -> used=1，1+7=8 拒绝
    assert [r.allowed for r in resp.results] == [True, False, True, False]
    assert _view(resp.results[3], "P").used_before == 1
    # 终态 t=15：窗口 (5,15] 仅 r3 的 cost 1
    assert resp.policies[0].total_used == 1
    assert resp.policies[0].principals[0].event_count == 1


# ---------- principal 隔离 ----------

def test_principals_have_independent_windows():
    resp = _run(
        [{"id": "P", "window_seconds": 100, "limit": 10}],
        [
            {"id": "r1", "timestamp": 0, "principal": "a", "cost": 10, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 1, "principal": "b", "cost": 10, "policy_refs": ["P"]},
            {"id": "r3", "timestamp": 2, "principal": "a", "cost": 1, "policy_refs": ["P"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, True, False]
    p = resp.policies[0]
    rows = {row.principal: row for row in p.principals}
    assert set(rows) == {"a", "b"}
    assert rows["a"].used == 10 and rows["b"].used == 10
    assert p.total_used == 20  # 跨主体合计可超过单主体上限


# ---------- 多策略原子性 ----------

def test_multi_policy_atomic_rejection_changes_nothing():
    resp = _run(
        [
            {"id": "P1", "window_seconds": 100, "limit": 10},
            {"id": "P2", "window_seconds": 100, "limit": 10},
        ],
        [
            {"id": "r1", "timestamp": 0, "principal": "a", "cost": 9,
             "policy_refs": ["P1", "P2"]},
            {"id": "r2", "timestamp": 1, "principal": "a", "cost": 2,
             "policy_refs": ["P1", "P2"]},
        ],
    )
    assert resp.results[0].allowed is True
    r2 = resp.results[1]
    assert r2.allowed is False
    assert r2.rejected_by == "P1"
    # 两个策略都保持 r1 之后的状态，r2 未记任何账
    assert _view(r2, "P1").used_after == 9
    assert _view(r2, "P2").used_after == 9
    states = {p.id: p for p in resp.policies}
    assert states["P1"].total_used == 9
    assert states["P2"].total_used == 9
    assert states["P1"].principals[0].event_count == 1


def test_first_over_limit_policy_follows_refs_order():
    # P2 先超限、P1 后超限：按 policy_refs 顺序应报 P2
    resp = _run(
        [
            {"id": "P1", "window_seconds": 100, "limit": 10},
            {"id": "P2", "window_seconds": 100, "limit": 3},
        ],
        [{"id": "r1", "timestamp": 0, "principal": "a", "cost": 5,
          "policy_refs": ["P1", "P2"]}],
    )
    r = resp.results[0]
    assert r.allowed is False
    assert r.rejected_by == "P2"
    assert r.rejection.policy_id == "P2"
    assert r.rejection.available == 3
    # 原子：P1 也未记账
    assert _view(r, "P1").used_after == 0


def test_allowed_request_books_all_referenced_policies():
    resp = _run(
        [
            {"id": "P1", "window_seconds": 100, "limit": 10},
            {"id": "P2", "window_seconds": 100, "limit": 10},
        ],
        [{"id": "r1", "timestamp": 0, "principal": "a", "cost": 4,
          "policy_refs": ["P1", "P2"]}],
    )
    states = {p.id: p for p in resp.policies}
    assert states["P1"].total_used == 4
    assert states["P2"].total_used == 4


def test_policy_view_only_includes_referenced_policies():
    resp = _run(
        [
            {"id": "P1", "window_seconds": 100, "limit": 10},
            {"id": "P2", "window_seconds": 100, "limit": 10},
        ],
        [
            {"id": "r1", "timestamp": 0, "principal": "a", "cost": 1, "policy_refs": ["P1"]},
            {"id": "r2", "timestamp": 1, "principal": "a", "cost": 1,
             "policy_refs": ["P1", "P2"]},
        ],
    )
    assert set(resp.results[0].policies) == {"P1"}
    assert set(resp.results[1].policies) == {"P1", "P2"}
    # dict 顺序按 policy_refs
    assert list(resp.results[1].policies) == ["P1", "P2"]


def test_independent_window_seconds_per_policy():
    # Psec 窗口 1、限额 10；Pmin 窗口 100、限额 10
    resp = _run(
        [
            {"id": "Psec", "window_seconds": 1, "limit": 10},
            {"id": "Pmin", "window_seconds": 100, "limit": 10},
        ],
        [
            {"id": "r1", "timestamp": 0, "principal": "a", "cost": 8,
             "policy_refs": ["Psec", "Pmin"]},
            {"id": "r2", "timestamp": 1, "principal": "a", "cost": 8,
             "policy_refs": ["Psec", "Pmin"]},
        ],
    )
    # r2：Psec 的 t=0 事件恰在左边界过期 -> used 0；Pmin 仍记 8 -> 8+8>10 拒绝
    assert resp.results[1].allowed is False
    assert resp.results[1].rejected_by == "Pmin"
    assert _view(resp.results[1], "Psec").used_before == 0
    assert _view(resp.results[1], "Pmin").used_before == 8


# ---------- 排序与终态 ----------

def test_unsorted_input_processed_chronologically_results_keep_order():
    resp = _run(
        [{"id": "P", "window_seconds": 100, "limit": 1}],
        [
            {"id": "late", "timestamp": 5, "principal": "a", "cost": 1, "policy_refs": ["P"]},
            {"id": "early", "timestamp": 0, "principal": "a", "cost": 1, "policy_refs": ["P"]},
        ],
    )
    assert [r.request_id for r in resp.results] == ["late", "early"]
    assert [r.index for r in resp.results] == [0, 1]
    assert resp.results[0].allowed is False  # 实际先记账的是 t=0 的 early
    assert resp.results[1].allowed is True


def test_same_timestamp_keeps_input_order():
    resp = _run(
        [{"id": "P", "window_seconds": 100, "limit": 1}],
        [
            {"id": "r1", "timestamp": 0, "principal": "a", "cost": 1, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 0, "principal": "a", "cost": 1, "policy_refs": ["P"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, False]


def test_final_state_observed_at_last_request_timestamp():
    resp = _run(
        [{"id": "P", "window_seconds": 10, "limit": 100}],
        [
            {"id": "r1", "timestamp": 0, "principal": "a", "cost": 3, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 20, "principal": "a", "cost": 5, "policy_refs": ["P"]},
        ],
    )
    p = resp.policies[0]
    # 终态窗口 (10,20]：t=0 事件已过期
    assert (p.window.start, p.window.end) == (10, 20)
    assert p.total_used == 5
    assert p.principals[0].event_count == 1


def test_rejected_request_leaves_no_trace():
    # 被原子拒绝的请求不产生任何记账痕迹：used 不变、终态事件数不含它
    resp = _run(
        [{"id": "P", "window_seconds": 100, "limit": 1}],
        [
            {"id": "r1", "timestamp": 0, "principal": "a", "cost": 1, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 1, "principal": "a", "cost": 1, "policy_refs": ["P"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, False]
    assert _view(resp.results[1], "P").used_after == 1
    p = resp.policies[0]
    assert [row.principal for row in p.principals] == ["a"]
    assert p.principals[0].event_count == 1
    assert p.total_used == 1


# ---------- Pydantic / 跨字段校验 ----------

def _validate_error(payload):
    with pytest.raises(RequestValidationErrors) as exc:
        WindowSimulateRequest.model_validate(payload)
    return exc.value.errors


def test_duplicate_policy_ids_rejected():
    errors = _validate_error(
        {
            "policies": [
                {"id": "P", "window_seconds": 1, "limit": 1},
                {"id": "P", "window_seconds": 2, "limit": 2},
            ],
            "requests": [
                {"id": "r", "timestamp": 0, "principal": "a", "cost": 1,
                 "policy_refs": ["P"]}
            ],
        }
    )
    assert all(e["type"] == "duplicate_policy" for e in errors)


def test_unknown_policy_reference_rejected():
    errors = _validate_error(
        {
            "policies": [{"id": "P", "window_seconds": 1, "limit": 1}],
            "requests": [
                {"id": "r", "timestamp": 0, "principal": "a", "cost": 1,
                 "policy_refs": ["P", "X"]}
            ],
        }
    )
    assert any(
        e["type"] == "unknown_policy_ref" and e["input"] == "X" for e in errors
    )


def test_duplicate_request_ids_rejected():
    errors = _validate_error(
        {
            "policies": [{"id": "P", "window_seconds": 1, "limit": 1}],
            "requests": [
                {"id": "r", "timestamp": 0, "principal": "a", "cost": 1,
                 "policy_refs": ["P"]},
                {"id": "r", "timestamp": 1, "principal": "a", "cost": 1,
                 "policy_refs": ["P"]},
            ],
        }
    )
    assert any(e["type"] == "duplicate_request" for e in errors)


def test_duplicate_policy_ref_within_request_rejected_by_pydantic():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WindowSimulateRequest.model_validate(
            {
                "policies": [{"id": "P", "window_seconds": 1, "limit": 1}],
                "requests": [
                    {"id": "r", "timestamp": 0, "principal": "a", "cost": 1,
                     "policy_refs": ["P", "P"]}
                ],
            }
        )


def test_non_positive_window_and_limit_rejected_by_pydantic():
    from pydantic import ValidationError

    for bad_policy in (
        {"id": "P", "window_seconds": 0, "limit": 1},
        {"id": "P", "window_seconds": -1, "limit": 1},
        {"id": "P", "window_seconds": 1, "limit": 0},
        {"id": "P", "window_seconds": 1, "limit": -2},
        {"id": "P", "window_seconds": "nan", "limit": 1},
    ):
        with pytest.raises(ValidationError):
            WindowSimulateRequest.model_validate(
                {
                    "policies": [bad_policy],
                    "requests": [
                        {"id": "r", "timestamp": 0, "principal": "a", "cost": 1,
                         "policy_refs": ["P"]}
                    ],
                }
            )


def test_bad_request_fields_rejected_by_pydantic():
    from pydantic import ValidationError

    base_policy = [{"id": "P", "window_seconds": 1, "limit": 1}]

    def validate(req):
        WindowSimulateRequest.model_validate(
            {"policies": base_policy, "requests": [req]}
        )

    with pytest.raises(ValidationError):
        validate({"id": "r", "timestamp": 0, "principal": "a", "cost": 0,
                  "policy_refs": ["P"]})
    with pytest.raises(ValidationError):
        validate({"id": "r", "timestamp": 0, "principal": "", "cost": 1,
                  "policy_refs": ["P"]})
    with pytest.raises(ValidationError):
        validate({"id": "r", "timestamp": 0, "principal": "a", "cost": 1,
                  "policy_refs": []})
    with pytest.raises(ValidationError):
        validate({"id": "r", "timestamp": 0, "principal": "a", "cost": 1,
                  "policy_refs": ["P"], "unexpected": 1})


def test_empty_policies_or_requests_rejected_by_pydantic():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        WindowSimulateRequest.model_validate({"policies": [], "requests": []})


# ---------- HTTP ----------

def test_http_window_simulate_ok():
    response = client.post(
        "/window-simulate",
        json={
            "policies": [
                {"id": "sec", "window_seconds": 1, "limit": 10},
                {"id": "min", "window_seconds": 60, "limit": 15},
            ],
            "requests": [
                {"id": "r1", "timestamp": 0, "principal": "alpha", "cost": 6,
                 "policy_refs": ["sec", "min"]},
                {"id": "r2", "timestamp": 0, "principal": "alpha", "cost": 6,
                 "policy_refs": ["sec", "min"]},
                {"id": "r3", "timestamp": 1, "principal": "alpha", "cost": 6,
                 "policy_refs": ["sec", "min"]},
                {"id": "r4", "timestamp": 1, "principal": "alpha", "cost": 4,
                 "policy_refs": ["min"]},
                {"id": "r5", "timestamp": 2, "principal": "beta", "cost": 9,
                 "policy_refs": ["sec"]},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert [r["allowed"] for r in data["results"]] == [True, False, True, False, True]
    assert data["results"][1]["rejected_by"] == "sec"
    assert data["results"][1]["rejection"]["reason"] == "limit_exceeded"
    assert data["results"][3]["rejected_by"] == "min"
    assert (data["allowed_count"], data["rejected_count"]) == (3, 2)
    states = {p["id"]: p for p in data["policies"]}
    # 终态 t=2：sec 窗口 (1,2] 仅 beta 的 9；min 窗口 (-58,2] alpha 累计 12
    assert states["sec"]["total_used"] == 9
    assert states["min"]["total_used"] == 12
    assert states["sec"]["window"] == {"start": 1.0, "end": 2.0}


def test_http_validation_error_is_422():
    response = client.post(
        "/window-simulate",
        json={
            "policies": [{"id": "P", "window_seconds": 1, "limit": 1}],
            "requests": [
                {"id": "r", "timestamp": 0, "principal": "a", "cost": 1,
                 "policy_refs": ["GHOST"]}
            ],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_policy_ref" for e in detail)


def test_http_root_lists_window_simulate():
    root = client.get("/").json()
    assert root["window_simulate"] == "/window-simulate"
