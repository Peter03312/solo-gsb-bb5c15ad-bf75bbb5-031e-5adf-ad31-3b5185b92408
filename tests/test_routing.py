"""候选链路由试算（/route-simulate）引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.main import app
from app.models import RouteSimulateRequest
from app.routing import route_simulate

client = TestClient(app)


def _run(buckets, requests):
    return route_simulate(
        RouteSimulateRequest.model_validate({"buckets": buckets, "requests": requests})
    )


# ---------- 选链与扣减 ----------

def test_single_candidate_behaves_like_simulate():
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 0}],
        [{"id": "r1", "timestamp": 0, "cost": 4, "chains": [["A"]]}],
    )
    r = resp.results[0]
    assert r.allowed is True
    assert r.selected_chain_index == 0
    assert r.selected_chain == ["A"]
    assert r.balances["A"].before == 10 and r.balances["A"].after == 6
    assert r.rejections is None
    assert resp.final_balances == {"A": 6}
    assert resp.allowed_count == 1 and resp.rejected_count == 0


def test_selects_min_post_deduction_max_occupancy_rate():
    # A：cap 10 满桶；B：cap 100 余 50。cost 5。
    # 扣减后占用率：A (10-5)/10 = 0.5，B (100-45)/100 = 0.55 -> 选 A，
    # 尽管 B 的绝对余额更高（按占用率而非绝对余额比较）
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 100, "refill_rate": 0, "initial": 50},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chains": [["A"], ["B"]]}],
    )
    r = resp.results[0]
    assert r.selected_chain_index == 0
    assert r.selected_chain == ["A"]
    assert r.balances["A"].after == 5
    assert r.balances["B"].after == 50  # 未选中，不扣


def test_selection_uses_post_deduction_not_pre_deduction_occupancy():
    # A：cap 10 余 6（扣前占用 0.4）；B：cap 100 余 50（扣前占用 0.5）。cost 5。
    # 扣减后：A (10-1)/10 = 0.9，B (100-45)/100 = 0.55 -> 选 B；
    # 若错误地按扣减前占用率比较会选 A
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0, "initial": 6},
            {"id": "B", "capacity": 100, "refill_rate": 0, "initial": 50},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chains": [["A"], ["B"]]}],
    )
    r = resp.results[0]
    assert r.selected_chain == ["B"]
    assert r.balances["B"].after == 45
    assert r.balances["A"].after == 6


def test_chain_score_is_worst_bucket_on_chain():
    # 链上最大占用率由链上最紧张的桶决定：
    # chain0 = [A, DB]：A 扣后 (10-6)/10 = 0.4，DB 扣后 (4-0)/4 = 1.0 -> 链分 1.0
    # chain1 = [B]：扣后 (10-2)/10 = 0.8 -> 选 chain1
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "DB", "capacity": 4, "refill_rate": 0},
            {"id": "B", "capacity": 10, "refill_rate": 0, "initial": 6},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 4, "chains": [["A", "DB"], ["B"]]}],
    )
    r = resp.results[0]
    assert r.selected_chain_index == 1
    assert r.balances["B"].after == 2
    assert r.balances["A"].after == 10
    assert r.balances["DB"].after == 4


def test_tie_breaks_by_candidate_order():
    # 两条候选扣减后最大占用率相同 -> 取候选顺序在前者
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 10, "refill_rate": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 4, "chains": [["A"], ["B"]]}],
    )
    r = resp.results[0]
    assert r.selected_chain_index == 0
    assert r.balances["A"].after == 6
    assert r.balances["B"].after == 10


def test_ineligible_candidate_skipped_regardless_of_order():
    # 候选 0 余额不足不可选，即使排在前面也落到候选 1
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0, "initial": 2},
            {"id": "B", "capacity": 10, "refill_rate": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chains": [["A"], ["B"]]}],
    )
    r = resp.results[0]
    assert r.allowed is True
    assert r.selected_chain_index == 1
    assert r.balances["A"].after == 2
    assert r.balances["B"].after == 5


def test_only_selected_chain_deducted():
    # 共享桶 DB 出现在未选中的候选链中：保持不扣
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0, "initial": 8},
            {"id": "DB", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 10, "refill_rate": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 3, "chains": [["A", "DB"], ["B"]]}],
    )
    r = resp.results[0]
    # chain0 链分 max((10-5)/10, (10-7)/10) = 0.5；chain1 (10-7)/10 = 0.3 -> 选 chain1
    assert r.selected_chain == ["B"]
    assert r.balances["A"].after == 8
    assert r.balances["DB"].after == 10
    assert r.balances["B"].after == 7


def test_balances_snapshot_covers_all_candidate_buckets_in_first_seen_order():
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 10, "refill_rate": 0},
            {"id": "C", "capacity": 10, "refill_rate": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 1, "chains": [["A", "B"], ["B", "C"]]}],
    )
    r = resp.results[0]
    assert list(r.balances.keys()) == ["A", "B", "C"]
    assert r.selected_chain == ["A", "B"]
    assert r.balances["C"].before == 10 and r.balances["C"].after == 10


# ---------- 原子拒绝 ----------

def test_all_candidates_insufficient_atomic_rejection():
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0, "initial": 3},
            {"id": "B", "capacity": 10, "refill_rate": 0, "initial": 4},
            {"id": "C", "capacity": 10, "refill_rate": 0, "initial": 4},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chains": [["A", "C"], ["B"], ["C"]]}],
    )
    r = resp.results[0]
    assert r.allowed is False
    assert r.selected_chain_index is None
    assert r.selected_chain is None
    # 逐候选给出首个不足桶
    assert [(x.chain_index, x.rejected_by, x.available) for x in r.rejections] == [
        (0, "A", 3),
        (1, "B", 4),
        (2, "C", 4),
    ]
    # 所有桶原子不变
    assert r.balances["A"].after == 3
    assert r.balances["B"].after == 4
    assert r.balances["C"].after == 4
    assert resp.final_balances == {"A": 3, "B": 4, "C": 4}
    assert resp.allowed_count == 0 and resp.rejected_count == 1


def test_rejection_reports_first_insufficient_bucket_in_chain_order():
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0, "initial": 1},
            {"id": "B", "capacity": 10, "refill_rate": 0, "initial": 1},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chains": [["A", "B"]]}],
    )
    r = resp.results[0]
    assert r.allowed is False
    assert len(r.rejections) == 1
    assert r.rejections[0].chain_index == 0
    assert r.rejections[0].rejected_by == "A"
    assert r.rejections[0].available == 1


def test_rejection_does_not_consume_or_block_refill():
    # t=0 拒绝（需要 10 只有 5），t=5 以 1/s 补到 10 后放行
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 5}],
        [
            {"id": "r1", "timestamp": 0, "cost": 10, "chains": [["A"]]},
            {"id": "r2", "timestamp": 5, "cost": 10, "chains": [["A"]]},
        ],
    )
    assert resp.results[0].allowed is False
    assert resp.results[0].balances["A"].after == 5
    assert resp.results[1].allowed is True
    assert resp.results[1].balances["A"].before == 10
    assert resp.results[1].balances["A"].after == 0


# ---------- 回放顺序与补充语义 ----------

def test_out_of_order_timestamps_simulated_chronologically():
    # 输入乱序，必须按时间戳处理；结果仍按原始下标返回
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [
            {"id": "r_late", "timestamp": 5, "cost": 5, "chains": [["A"]]},
            {"id": "r_early", "timestamp": 0, "cost": 1, "chains": [["A"]]},
        ],
    )
    assert resp.results[0].request_id == "r_late"
    assert resp.results[0].allowed is True
    assert resp.results[0].balances["A"].before == 5
    assert resp.results[1].request_id == "r_early"
    assert resp.results[1].allowed is False
    assert resp.results[1].rejections[0].rejected_by == "A"
    assert [r.index for r in resp.results] == [0, 1]


def test_same_timestamp_requests_keep_input_order():
    resp = _run(
        [{"id": "A", "capacity": 1, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "chains": [["A"]]},
            {"id": "r2", "timestamp": 0, "cost": 1, "chains": [["A"]]},
        ],
    )
    assert resp.results[0].allowed is True
    assert resp.results[1].allowed is False


def test_continuous_refill_shared_across_requests_and_chains():
    # 共享桶被不同请求的候选链反复触及：余额与补充在请求间连续累积
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 1},
            {"id": "B", "capacity": 10, "refill_rate": 1, "initial": 0},
        ],
        [
            # t=0：A 扣后占用 (10-6)/10 = 0.4，B 不足 -> 只能选 chain0
            {"id": "r1", "timestamp": 0, "cost": 4, "chains": [["A"], ["B"]]},
            # t=2：B 补到 2 仍不足 cost 4 -> 再选 chain0，A 8 -> 4
            {"id": "r2", "timestamp": 2, "cost": 4, "chains": [["A"], ["B"]]},
            # t=4：A 补到 6（扣后 0.8），B 补到 4（扣后 1.0）-> 选 chain0，A 6 -> 2
            {"id": "r3", "timestamp": 4, "cost": 4, "chains": [["A"], ["B"]]},
        ],
    )
    assert [r.selected_chain_index for r in resp.results] == [0, 0, 0]
    assert resp.results[1].balances["A"].before == 8
    assert resp.results[2].balances["B"].before == 4
    assert resp.final_balances == {"A": 2, "B": 4}


def test_balance_exactly_equal_cost_is_eligible():
    resp = _run(
        [{"id": "A", "capacity": 5, "refill_rate": 0}],
        [{"id": "r1", "timestamp": 0, "cost": 5, "chains": [["A"]]}],
    )
    assert resp.results[0].allowed is True
    assert resp.results[0].balances["A"].after == 0


def test_readme_example_scenario():
    # 与 examples/route-simulate.json 相同的完整场景
    resp = _run(
        [
            {"id": "chain_a_api", "capacity": 10, "refill_rate": 1, "initial": 10},
            {"id": "chain_b_api", "capacity": 10, "refill_rate": 1, "initial": 6},
            {"id": "shared_db", "capacity": 4, "refill_rate": 0.5, "initial": 4},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chains": [["chain_a_api", "shared_db"], ["chain_b_api"]]},
            {"id": "r2", "timestamp": 0, "cost": 3, "chains": [["chain_b_api"], ["chain_a_api"]]},
            {"id": "r3", "timestamp": 2, "cost": 4, "chains": [["chain_a_api", "shared_db"], ["chain_b_api"]]},
            {"id": "r4", "timestamp": 3, "cost": 2, "chains": [["chain_b_api"], ["chain_a_api", "shared_db"]]},
            {"id": "r5", "timestamp": 4, "cost": 5, "chains": [["chain_a_api"], ["chain_b_api"]]},
            {"id": "r6", "timestamp": 5, "cost": 6, "chains": [["chain_a_api"], ["chain_b_api"], ["chain_a_api", "shared_db"]]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, True, True, True, True, False]
    assert [r.selected_chain_index for r in resp.results[:5]] == [1, 1, 0, 0, 0]
    assert resp.allowed_count == 5 and resp.rejected_count == 1
    r6 = resp.results[5]
    assert [(x.chain_index, x.rejected_by) for x in r6.rejections] == [
        (0, "chain_a_api"),
        (1, "chain_b_api"),
        (2, "chain_a_api"),
    ]
    assert resp.final_balances == {"chain_a_api": 3, "chain_b_api": 5, "shared_db": 1.5}


# ---------- 校验 ----------

def _validate_error(payload):
    with pytest.raises(RequestValidationErrors) as exc:
        RouteSimulateRequest.model_validate(payload)
    return exc.value.errors


def test_duplicate_bucket_ids_rejected():
    errors = _validate_error(
        {
            "buckets": [
                {"id": "A", "capacity": 1, "refill_rate": 0},
                {"id": "A", "capacity": 2, "refill_rate": 0},
            ],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chains": [["A"]]}],
        }
    )
    assert all(e["type"] == "duplicate_bucket" for e in errors)


def test_unknown_bucket_reference_in_any_candidate_rejected():
    errors = _validate_error(
        {
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chains": [["A"], ["A", "X"]]}
            ],
        }
    )
    assert any(e["type"] == "unknown_bucket_ref" and e["input"] == "X" for e in errors)
    # loc 定位到具体候选链中的具体位置
    err = next(e for e in errors if e["type"] == "unknown_bucket_ref")
    assert err["loc"] == ("requests", 0, "chains", 1, 1)


def test_duplicate_request_ids_rejected():
    errors = _validate_error(
        {
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chains": [["A"]]},
                {"id": "r", "timestamp": 1, "cost": 1, "chains": [["A"]]},
            ],
        }
    )
    assert any(e["type"] == "duplicate_request" for e in errors)


def test_empty_chains_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RouteSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chains": []}],
            }
        )


def test_empty_candidate_chain_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RouteSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chains": [[]]}],
            }
        )


def test_duplicate_bucket_within_candidate_chain_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RouteSimulateRequest.model_validate(
            {
                "buckets": [
                    {"id": "A", "capacity": 1, "refill_rate": 0},
                    {"id": "B", "capacity": 1, "refill_rate": 0},
                ],
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "chains": [["A", "B", "A"]]}
                ],
            }
        )


def test_non_positive_cost_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RouteSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [{"id": "r", "timestamp": 0, "cost": 0, "chains": [["A"]]}],
            }
        )


def test_non_finite_timestamp_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RouteSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [{"id": "r", "timestamp": "inf", "cost": 1, "chains": [["A"]]}],
            }
        )


def test_extra_field_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RouteSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "chains": [["A"]], "chain": ["A"]}
                ],
            }
        )


# ---------- HTTP ----------

def test_http_route_simulate_ok():
    response = client.post(
        "/route-simulate",
        json={
            "buckets": [
                {"id": "A", "capacity": 10, "refill_rate": 0, "initial": 8},
                {"id": "DB", "capacity": 10, "refill_rate": 0},
                {"id": "B", "capacity": 10, "refill_rate": 0},
            ],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 3, "chains": [["A", "DB"], ["B"]]},
                {"id": "r2", "timestamp": 0, "cost": 20, "chains": [["A"], ["B"]]},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    # r1：chain0 链分 0.5 > chain1 链分 0.3 -> 选候选 1，仅 B 扣 3
    assert data["results"][0]["allowed"] is True
    assert data["results"][0]["selected_chain_index"] == 1
    assert data["results"][0]["selected_chain"] == ["B"]
    assert data["results"][0]["balances"]["A"]["after"] == 8
    assert data["results"][0]["balances"]["DB"]["after"] == 10
    assert data["results"][0]["balances"]["B"]["after"] == 7
    assert data["results"][0]["rejections"] is None
    # r2：cost 20 超过任何候选 -> 原子拒绝，逐候选给出首个不足桶
    assert data["results"][1]["allowed"] is False
    assert data["results"][1]["selected_chain"] is None
    assert data["results"][1]["rejections"] == [
        {"chain_index": 0, "rejected_by": "A", "available": 8.0},
        {"chain_index": 1, "rejected_by": "B", "available": 7.0},
    ]
    assert data["allowed_count"] == 1 and data["rejected_count"] == 1
    assert data["final_balances"] == {"A": 8.0, "DB": 10.0, "B": 7.0}


def test_http_validation_error_is_422():
    response = client.post(
        "/route-simulate",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chains": [["GHOST"]]}],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_bucket_ref" for e in detail)


def test_http_value_error_is_422_not_500():
    # 校验器抛出的 ValueError（initial > capacity）必须仍按 422 结构返回
    response = client.post(
        "/route-simulate",
        json={
            "buckets": [{"id": "A", "capacity": 5, "refill_rate": 1, "initial": 6}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chains": [["A"]]}],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
