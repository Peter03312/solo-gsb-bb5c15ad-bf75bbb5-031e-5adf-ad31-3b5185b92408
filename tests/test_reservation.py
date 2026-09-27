"""区间预留试算（/reservation-simulate）引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.main import app
from app.models import ReservationSimulateRequest
from app.reservation import reservation_simulate

client = TestClient(app)


def _run(buckets, reservations):
    return reservation_simulate(
        ReservationSimulateRequest.model_validate(
            {"buckets": buckets, "reservations": reservations}
        )
    )


def _bucket(resp, bucket_id):
    return next(b for b in resp.buckets if b.id == bucket_id)


def _intervals(bucket):
    return [(iv.start, iv.end, iv.occupancy) for iv in bucket.intervals]


# ---------- 基本接受 / 拒绝 ----------

def test_single_reservation_accepted():
    resp = _run(
        [{"id": "A", "capacity": 5}],
        [{"start": 0, "end": 10, "cost": 5, "chain": ["A"]}],
    )
    r = resp.results[0]
    assert r.allowed is True
    assert r.rejected_by is None
    assert r.index == 0
    a = _bucket(resp, "A")
    assert a.peak_occupancy == 5
    assert _intervals(a) == [(0, 10, 5)]
    assert resp.allowed_count == 1 and resp.rejected_count == 0


def test_cost_exceeding_capacity_rejected_even_when_empty():
    # 整区间恒有占用 0 + cost > capacity，任何时刻都放不下
    resp = _run(
        [{"id": "A", "capacity": 4}],
        [{"start": 0, "end": 10, "cost": 5, "chain": ["A"]}],
    )
    assert resp.results[0].allowed is False
    assert resp.results[0].rejected_by == "A"
    assert _bucket(resp, "A").intervals == []
    assert _bucket(resp, "A").peak_occupancy == 0


def test_overlapping_respect_capacity():
    # capacity 2：两个 cost=1 的重叠接受，第三个重叠的被拒
    resp = _run(
        [{"id": "A", "capacity": 2}],
        [
            {"start": 0, "end": 5, "cost": 1, "chain": ["A"]},
            {"start": 2, "end": 7, "cost": 1, "chain": ["A"]},
            {"start": 3, "end": 4, "cost": 1, "chain": ["A"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, True, False]
    assert resp.results[2].rejected_by == "A"
    # 拒绝不改变占用
    assert _intervals(_bucket(resp, "A")) == [(0, 2, 1), (2, 5, 2), (5, 7, 1)]
    assert _bucket(resp, "A").peak_occupancy == 2


def test_half_open_endpoint_does_not_overlap():
    # [0,2) 与 [2,4) 在 t=2 端点相接：不并发，可同时 cost=capacity
    resp = _run(
        [{"id": "A", "capacity": 3}],
        [
            {"start": 0, "end": 2, "cost": 3, "chain": ["A"]},
            {"start": 2, "end": 4, "cost": 3, "chain": ["A"]},
        ],
    )
    assert all(r.allowed for r in resp.results)
    a = _bucket(resp, "A")
    assert a.peak_occupancy == 3
    # 端点相接但占用相同（3），合并为一段
    assert _intervals(a) == [(0, 4, 3)]


def test_adjacent_different_occupancy_stays_split():
    resp = _run(
        [{"id": "A", "capacity": 2}],
        [
            {"start": 0, "end": 2, "cost": 2, "chain": ["A"]},
            {"start": 2, "end": 4, "cost": 1, "chain": ["A"]},
        ],
    )
    assert _intervals(_bucket(resp, "A")) == [(0, 2, 2), (2, 4, 1)]


def test_rejected_reservation_does_not_change_any_chain_bucket():
    # A 容量充足，B 放不下 -> 原子拒绝，A 也不登记占用
    resp = _run(
        [{"id": "A", "capacity": 10}, {"id": "B", "capacity": 1}],
        [
            {"start": 0, "end": 5, "cost": 2, "chain": ["A", "B"]},
        ],
    )
    assert resp.results[0].allowed is False
    assert resp.results[0].rejected_by == "B"
    assert _bucket(resp, "A").intervals == []
    assert _bucket(resp, "B").intervals == []


def test_chain_atomic_accept_records_on_every_bucket():
    resp = _run(
        [{"id": "A", "capacity": 5}, {"id": "B", "capacity": 3}],
        [{"start": 1, "end": 4, "cost": 2, "chain": ["A", "B"]}],
    )
    assert resp.results[0].allowed is True
    assert _intervals(_bucket(resp, "A")) == [(1, 4, 2)]
    assert _intervals(_bucket(resp, "B")) == [(1, 4, 2)]
    assert _bucket(resp, "A").peak_occupancy == 2
    assert _bucket(resp, "B").peak_occupancy == 2


def test_rejected_by_first_bucket_over_capacity_in_chain():
    resp = _run(
        [{"id": "A", "capacity": 1}, {"id": "B", "capacity": 10}, {"id": "C", "capacity": 1}],
        [{"start": 0, "end": 1, "cost": 2, "chain": ["A", "B", "C"]}],
    )
    assert resp.results[0].allowed is False
    assert resp.results[0].rejected_by == "A"


def test_only_partial_overlap_window_must_fit():
    # 已接受 [0,10) 占用 4；新请求 [8,12) cost 2：[8,10) 重叠段占用 6 > 5，拒绝。
    # 若只看起点/终点或只统计完全包含，会错误放行。
    resp = _run(
        [{"id": "A", "capacity": 5}],
        [
            {"start": 0, "end": 10, "cost": 4, "chain": ["A"]},
            {"start": 8, "end": 12, "cost": 2, "chain": ["A"]},
        ],
    )
    assert resp.results[1].allowed is False
    assert _intervals(_bucket(resp, "A")) == [(0, 10, 4)]


def test_overlapping_intervals_with_gap_between_zero_levels():
    # [0,2) 与 [5,7) 之间存在占用为 0 的空档，不应跨空档合并
    resp = _run(
        [{"id": "A", "capacity": 2}],
        [
            {"start": 0, "end": 2, "cost": 2, "chain": ["A"]},
            {"start": 5, "end": 7, "cost": 2, "chain": ["A"]},
        ],
    )
    assert _intervals(_bucket(resp, "A")) == [(0, 2, 2), (5, 7, 2)]
    assert _bucket(resp, "A").peak_occupancy == 2


# ---------- 处理顺序 ----------

def test_processed_by_start_ascending_with_input_order_ties():
    # 输入乱序：必须按 start 升序处理；同 start 按输入顺序
    resp = _run(
        [{"id": "A", "capacity": 1}],
        [
            {"start": 5, "end": 6, "cost": 1, "chain": ["A"]},   # index 0
            {"start": 0, "end": 1, "cost": 1, "chain": ["A"]},   # index 1
            {"start": 0, "end": 2, "cost": 1, "chain": ["A"]},   # index 2
        ],
    )
    # index 1（[0,1)）先接受；index 2（[0,2) 与前者重叠）拒绝；index 0（[5,6)）接受
    assert [r.allowed for r in resp.results] == [True, True, False]
    assert resp.results[2].rejected_by == "A"
    # 结果仍按原始索引返回
    assert [r.index for r in resp.results] == [0, 1, 2]
    assert _intervals(_bucket(resp, "A")) == [(0, 1, 1), (5, 6, 1)]


def test_rejected_early_does_not_block_later_fitting_reservation():
    resp = _run(
        [{"id": "A", "capacity": 1}],
        [
            {"start": 0, "end": 10, "cost": 2, "chain": ["A"]},   # 永远放不下
            {"start": 0, "end": 5, "cost": 1, "chain": ["A"]},    # 可接受
        ],
    )
    assert resp.results[0].allowed is False
    assert resp.results[1].allowed is True
    assert _intervals(_bucket(resp, "A")) == [(0, 5, 1)]


# ---------- 合并与峰值（多区间叠加） ----------

def test_merged_intervals_with_weighted_costs():
    resp = _run(
        [{"id": "A", "capacity": 10}],
        [
            {"start": 0, "end": 10, "cost": 2, "chain": ["A"]},
            {"start": 3, "end": 7, "cost": 3, "chain": ["A"]},
        ],
    )
    assert _intervals(_bucket(resp, "A")) == [
        (0, 3, 2),
        (3, 7, 5),
        (7, 10, 2),
    ]
    assert _bucket(resp, "A").peak_occupancy == 5


def test_peak_occupancy_is_maximum_concurrency():
    resp = _run(
        [{"id": "A", "capacity": 10}],
        [
            {"start": 0, "end": 4, "cost": 1, "chain": ["A"]},
            {"start": 1, "end": 5, "cost": 2, "chain": ["A"]},
            {"start": 2, "end": 3, "cost": 3, "chain": ["A"]},
        ],
    )
    # 分段：[0,1)=1, [1,2)=3, [2,3)=6, [3,4)=3, [4,5)=2
    assert _intervals(_bucket(resp, "A")) == [
        (0, 1, 1),
        (1, 2, 3),
        (2, 3, 6),
        (3, 4, 3),
        (4, 5, 2),
    ]
    assert _bucket(resp, "A").peak_occupancy == 6


def test_bucket_reports_in_input_order_including_unreferenced():
    resp = _run(
        [{"id": "A", "capacity": 1}, {"id": "UNUSED", "capacity": 9}, {"id": "B", "capacity": 1}],
        [{"start": 0, "end": 1, "cost": 1, "chain": ["A", "B"]}],
    )
    assert [b.id for b in resp.buckets] == ["A", "UNUSED", "B"]
    unused = _bucket(resp, "UNUSED")
    assert unused.capacity == 9
    assert unused.peak_occupancy == 0
    assert unused.intervals == []


def test_results_carry_input_fields():
    resp = _run(
        [{"id": "A", "capacity": 5}],
        [{"start": 1.5, "end": 2.5, "cost": 3, "chain": ["A"]}],
    )
    r = resp.results[0]
    assert (r.start, r.end, r.cost) == (1.5, 2.5, 3)


# ---------- 校验 ----------

def _validate_error(payload):
    with pytest.raises(RequestValidationErrors) as exc:
        ReservationSimulateRequest.model_validate(payload)
    return exc.value.errors


def test_duplicate_bucket_ids_rejected():
    errors = _validate_error(
        {
            "buckets": [
                {"id": "A", "capacity": 1},
                {"id": "A", "capacity": 2},
            ],
            "reservations": [{"start": 0, "end": 1, "cost": 1, "chain": ["A"]}],
        }
    )
    assert all(e["type"] == "duplicate_bucket" for e in errors)


def test_non_positive_capacity_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReservationSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 0}],
                "reservations": [{"start": 0, "end": 1, "cost": 1, "chain": ["A"]}],
            }
        )


def test_end_not_after_start_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReservationSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1}],
                "reservations": [{"start": 1, "end": 1, "cost": 1, "chain": ["A"]}],
            }
        )


def test_non_positive_cost_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReservationSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1}],
                "reservations": [{"start": 0, "end": 1, "cost": 0, "chain": ["A"]}],
            }
        )


def test_unknown_bucket_reference_rejected():
    errors = _validate_error(
        {
            "buckets": [{"id": "A", "capacity": 1}],
            "reservations": [{"start": 0, "end": 1, "cost": 1, "chain": ["A", "X"]}],
        }
    )
    assert any(e["type"] == "unknown_bucket_ref" and e["input"] == "X" for e in errors)


def test_duplicate_bucket_within_chain_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReservationSimulateRequest.model_validate(
            {
                "buckets": [
                    {"id": "A", "capacity": 5},
                    {"id": "B", "capacity": 5},
                ],
                "reservations": [{"start": 0, "end": 1, "cost": 1, "chain": ["A", "B", "A"]}],
            }
        )


def test_extra_field_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReservationSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1}],
                "reservations": [
                    {"start": 0, "end": 1, "cost": 1, "chain": ["A"], "id": "r1"}
                ],
            }
        )


def test_non_finite_timestamp_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReservationSimulateRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1}],
                "reservations": [{"start": "nan", "end": 1, "cost": 1, "chain": ["A"]}],
            }
        )


# ---------- HTTP ----------

def test_http_reservation_simulate_ok():
    response = client.post(
        "/reservation-simulate",
        json={
            "buckets": [
                {"id": "room_a", "capacity": 2},
                {"id": "room_b", "capacity": 1},
            ],
            "reservations": [
                {"start": 0, "end": 3, "cost": 1, "chain": ["room_a", "room_b"]},
                {"start": 2, "end": 5, "cost": 1, "chain": ["room_a", "room_b"]},
                {"start": 5, "end": 8, "cost": 1, "chain": ["room_a"]},
                {"start": 6, "end": 7, "cost": 1, "chain": ["room_a"]},
                {"start": 7, "end": 9, "cost": 1, "chain": ["room_a", "room_b"]},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert [r["allowed"] for r in data["results"]] == [True, False, True, True, True]
    assert data["results"][1]["rejected_by"] == "room_b"
    assert data["allowed_count"] == 4 and data["rejected_count"] == 1
    room_a = next(b for b in data["buckets"] if b["id"] == "room_a")
    assert room_a["peak_occupancy"] == 2
    # [0,3)=1；[5,6) 仅第三个预留=1；[6,7) 第三+四=2；[7,8) 第三+五=2
    # （第四在 t=7 结束、第五在 t=7 开始，半开相接抵消）；[8,9) 仅第五=1
    assert room_a["intervals"] == [
        {"start": 0.0, "end": 3.0, "occupancy": 1.0},
        {"start": 5.0, "end": 6.0, "occupancy": 1.0},
        {"start": 6.0, "end": 8.0, "occupancy": 2.0},
        {"start": 8.0, "end": 9.0, "occupancy": 1.0},
    ]
    room_b = next(b for b in data["buckets"] if b["id"] == "room_b")
    assert room_b["peak_occupancy"] == 1
    assert room_b["intervals"] == [
        {"start": 0.0, "end": 3.0, "occupancy": 1.0},
        {"start": 7.0, "end": 9.0, "occupancy": 1.0},
    ]


def test_http_validation_error_is_422():
    response = client.post(
        "/reservation-simulate",
        json={
            "buckets": [{"id": "A", "capacity": 1}],
            "reservations": [
                {"start": 0, "end": 1, "cost": 1, "chain": ["GHOST"]}
            ],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_bucket_ref" for e in detail)


def test_http_bad_interval_is_422_not_500():
    response = client.post(
        "/reservation-simulate",
        json={
            "buckets": [{"id": "A", "capacity": 5}],
            "reservations": [
                {"start": 2, "end": 1, "cost": 1, "chain": ["A"]}
            ],
        },
    )
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
