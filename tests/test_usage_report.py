"""配额使用分析（/usage-report）引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.main import app
from app.models import SimulateRequest, UsageReportRequest
from app.simulation import simulate
from app.usage import usage_report

client = TestClient(app)


def _run(buckets, requests, window_seconds):
    return usage_report(
        UsageReportRequest.model_validate(
            {"buckets": buckets, "requests": requests, "window_seconds": window_seconds}
        )
    )


def _bucket(resp, bucket_id):
    return next(b for b in resp.buckets if b.id == bucket_id)


def test_single_window_basic_stats():
    # A=10 无补充：r1/r2 各扣 4（余 2），r3 需要 4 被拒
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"]},
            {"id": "r2", "timestamp": 0, "cost": 4, "chain": ["A"]},
            {"id": "r3", "timestamp": 1, "cost": 4, "chain": ["A"]},
        ],
        window_seconds=10,
    )
    assert resp.window_count == 1
    assert resp.windows[0].model_dump() == {"index": 0, "start": 0.0, "end": 10.0}
    a = _bucket(resp, "A")
    assert a.allowed_consumption == 8
    assert a.rejected_count == 1
    # 窗口内最低余额 2 -> 峰值占用 10 - 2 = 8
    assert a.peak_occupancy == 8
    assert a.peak_window.model_dump() == {"index": 0, "start": 0.0, "end": 10.0}
    assert [w.allowed_consumption for w in a.windows] == [8]
    assert [w.rejected_count for w in a.windows] == [1]
    assert [w.peak_occupancy for w in a.windows] == [8]
    assert resp.busiest_window.index == 0
    assert resp.busiest_window.allowed_consumption == 8
    assert resp.busiest_window.allowed_count == 2
    assert resp.busiest_window.rejected_count == 1


def test_half_open_boundary_request_belongs_to_right_window():
    # r2 恰好落在边界 t=2：属于右侧窗口 [2, 4)
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"]},
            {"id": "r2", "timestamp": 2, "cost": 5, "chain": ["A"]},
        ],
        window_seconds=2,
    )
    assert resp.window_count == 2
    assert [w.model_dump() for w in resp.windows] == [
        {"index": 0, "start": 0.0, "end": 2.0},
        {"index": 1, "start": 2.0, "end": 4.0},
    ]
    a = _bucket(resp, "A")
    assert [w.allowed_consumption for w in a.windows] == [5, 5]
    # 窗口 0：起点 10，扣后 5 -> 峰值占用 5
    # 窗口 1：边界补充到 7，扣后 2 -> 峰值占用 8
    assert [w.peak_occupancy for w in a.windows] == [5, 8]
    assert a.peak_window.index == 1
    assert a.peak_window.start == 2 and a.peak_window.end == 4


def test_consumption_counted_once_in_own_window_only():
    # 跨窗口的请求只计入发生时刻所在窗：r2(t=3) 落在窗口 1，不影响窗口 0/2
    resp = _run(
        [{"id": "A", "capacity": 100, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 10, "chain": ["A"]},
            {"id": "r2", "timestamp": 3, "cost": 10, "chain": ["A"]},
            {"id": "r3", "timestamp": 7, "cost": 10, "chain": ["A"]},
        ],
        window_seconds=3,
    )
    assert resp.window_count == 3
    a = _bucket(resp, "A")
    assert [w.allowed_consumption for w in a.windows] == [10, 10, 10]
    assert a.allowed_consumption == 30


def test_peak_occupancy_decays_across_eventless_windows():
    # 事件之后的空窗：余额持续补充，峰值占用（capacity - 最低余额）逐窗衰减
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [{"id": "r1", "timestamp": 0, "cost": 8, "chain": ["A"]}],
        window_seconds=2,
    )
    # t_start = t_end = 0 -> 只有窗口 0
    assert resp.window_count == 1

    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 8, "chain": ["A"]},
            {"id": "r2", "timestamp": 6, "cost": 1, "chain": ["A"]},
        ],
        window_seconds=2,
    )
    a = _bucket(resp, "A")
    # 窗口 0 [0,2)：扣后 2 -> 占用 8；窗口 1 [2,4)：起点 4 -> 占用 6；
    # 窗口 2 [4,6)：起点 6 -> 占用 4；窗口 3 [6,8)：起点 8，扣后 7 -> 占用 3
    assert [w.peak_occupancy for w in a.windows] == [8, 6, 4, 3]
    assert a.peak_window.index == 0
    assert a.peak_window.start == 0 and a.peak_window.end == 2


def test_rejection_counted_for_every_chain_bucket():
    # r1 因 B 不足被原子拒绝：A、B 都计一次拒绝；r2 只走 A 并放行
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 3, "refill_rate": 0},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A", "B"]},
            {"id": "r2", "timestamp": 1, "cost": 2, "chain": ["A"]},
        ],
        window_seconds=10,
    )
    a = _bucket(resp, "A")
    b = _bucket(resp, "B")
    assert a.allowed_consumption == 2 and a.rejected_count == 1
    assert b.allowed_consumption == 0 and b.rejected_count == 1
    # B 从未被扣减：最低余额 = 3 -> 峰值占用 0
    assert b.peak_occupancy == 0
    # A 最低余额 8 -> 峰值占用 2
    assert a.peak_occupancy == 2


def test_peak_window_tie_picks_earliest():
    # 两个窗口峰值占用相同（都是 8）-> 取最早窗口
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"]},
            {"id": "r2", "timestamp": 0, "cost": 4, "chain": ["A"]},
            {"id": "r3", "timestamp": 1, "cost": 4, "chain": ["A"]},
        ],
        window_seconds=1,
    )
    a = _bucket(resp, "A")
    assert [w.peak_occupancy for w in a.windows] == [8, 8]
    assert a.peak_window.index == 0
    assert a.peak_window.start == 0 and a.peak_window.end == 1


def test_busiest_window_by_total_consumption():
    # 窗口 0 消耗 12（req-1/req-3 放行），窗口 1 消耗 4 -> 最忙为窗口 0
    resp = _run(
        [
            {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
            {"id": "api_qps", "capacity": 5, "refill_rate": 5},
        ],
        [
            {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
            {"id": "req-2", "timestamp": 0.5, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
            {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"]},
            {"id": "req-4", "timestamp": 3, "cost": 4, "chain": ["tenant_quota"]},
        ],
        window_seconds=2,
    )
    busiest = resp.busiest_window
    assert busiest.index == 0
    assert busiest.start == 0 and busiest.end == 2
    # 跨桶合计：tenant 8 + api 8 = 16
    assert busiest.allowed_consumption == 16
    assert busiest.allowed_count == 2
    assert busiest.rejected_count == 1


def test_out_of_order_requests_simulated_chronologically():
    # 输入乱序也必须按时间顺序统计：r_late 先发生（t=0 被拒），r_early 后发生
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [
            {"id": "r_early", "timestamp": 5, "cost": 5, "chain": ["A"]},
            {"id": "r_late", "timestamp": 0, "cost": 1, "chain": ["A"]},
        ],
        window_seconds=5,
    )
    a = _bucket(resp, "A")
    # 窗口 0 [0,5)：t=0 余额 0 被拒；窗口 1 [5,10)：补到 5 放行
    assert [w.rejected_count for w in a.windows] == [1, 0]
    assert [w.allowed_consumption for w in a.windows] == [0, 5]


def test_consistent_with_simulate_verdicts():
    # 与 /simulate 完全一致的放行/拒绝序列：逐窗合计必须等于模拟总计数
    buckets = [
        {"id": "A", "capacity": 10, "refill_rate": 1},
        {"id": "B", "capacity": 5, "refill_rate": 0.5},
    ]
    requests = [
        {"id": f"r{i}", "timestamp": t, "cost": 3, "chain": ["A", "B"]}
        for i, t in enumerate([0, 0, 1, 2, 5, 9, 9, 12])
    ]
    report = _run(buckets, requests, window_seconds=4)
    sim = simulate(SimulateRequest.model_validate({"buckets": buckets, "requests": requests}))

    assert sum(w.allowed_count for w in [report.busiest_window]) >= 0  # 结构存在
    total_allowed = sum(
        1 for r in sim.results if r.allowed
    )
    total_rejected = len(sim.results) - total_allowed
    # 每桶逐窗放行消耗合计 = 放行请求数 * cost（链上两桶相同）
    a = _bucket(report, "A")
    b = _bucket(report, "B")
    assert a.allowed_consumption == 3 * total_allowed
    assert b.allowed_consumption == 3 * total_allowed
    assert a.rejected_count == total_rejected
    assert b.rejected_count == total_rejected


def test_near_boundary_request_stays_in_own_window():
    # 回归：t=999999999.5 在右边界 1e9 之前，必须留在窗口 0——旧实现用 EPS
    # 吸附把它提前归入窗口 1，跨窗补充又把桶推进到未来边界 t=1e9（余额补满
    # 到 1），导致 r3 被误放行；/simulate 在 t=999999999.5 只补到 0.75，拒绝。
    buckets = [{"id": "A", "capacity": 1, "refill_rate": 0.75, "initial": 1}]
    requests = [
        {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"]},
        {"id": "r2", "timestamp": 999999998.5, "cost": 1, "chain": ["A"]},
        {"id": "r3", "timestamp": 999999999.5, "cost": 1, "chain": ["A"]},
    ]
    resp = _run(buckets, requests, window_seconds=1_000_000_000)
    sim = simulate(
        SimulateRequest.model_validate({"buckets": buckets, "requests": requests})
    )
    assert [r.allowed for r in sim.results] == [True, True, False]

    # 三个请求都在窗口 0 [0, 1e9) 内，不产生第二个窗口
    assert resp.window_count == 1
    assert resp.windows[0].model_dump() == {"index": 0, "start": 0.0, "end": 1e9}
    a = _bucket(resp, "A")
    assert a.allowed_consumption == 2
    assert a.rejected_count == 1
    assert [w.allowed_consumption for w in a.windows] == [2]
    assert [w.rejected_count for w in a.windows] == [1]
    # 最低余额 0（r2 在 t=999999998.5 补满后扣到 0）-> 峰值占用 1
    assert [w.peak_occupancy for w in a.windows] == [1]
    assert a.peak_window.index == 0
    assert resp.busiest_window.index == 0
    assert resp.busiest_window.allowed_count == 2
    assert resp.busiest_window.rejected_count == 1


def test_verdicts_match_simulate_across_near_boundary_sweep():
    # 回归：围绕窗口右边界的扫掠点上，逐窗归属、放行消耗、拒绝次数与
    # 逐窗峰值都必须反映与 /simulate 一致的实际回放（补充时钟不越过请求）。
    # r1/r2 在窗口 0 各消费 1（r2 在 t=1e9-1.5 补满后扣到 0）；r3 落在
    # 边界前时余额只补到 0.75 以下被拒，落在边界上/后时跨窗补满放行。
    buckets = [{"id": "A", "capacity": 1, "refill_rate": 0.75, "initial": 1}]
    width = 1_000_000_000
    for offset in (-1, -0.5, 0, 0.5, 1):
        t3 = width + offset
        requests = [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"]},
            {"id": "r2", "timestamp": width - 1.5, "cost": 1, "chain": ["A"]},
            {"id": "r3", "timestamp": t3, "cost": 1, "chain": ["A"]},
        ]
        resp = _run(buckets, requests, window_seconds=width)
        sim = simulate(
            SimulateRequest.model_validate({"buckets": buckets, "requests": requests})
        )
        verdicts = [r.allowed for r in sim.results]
        # 边界前拒绝（余额 0.75/0.375 < 1），边界上及之后跨窗补满放行
        assert verdicts == [True, True, offset >= 0]
        a = _bucket(resp, "A")

        # 半开归属：t3 < 1e9 时三个请求都在窗口 0；t3 >= 1e9 时 r3 在窗口 1
        r3_window = 0 if t3 < width else 1
        assert resp.window_count == r3_window + 1
        # 窗口 0：r1/r2 放行各计 1，r3 若留在窗口 0 则被拒计 1
        assert a.windows[0].allowed_consumption == 2
        assert a.windows[0].rejected_count == (0 if r3_window == 1 else 1)
        assert a.windows[0].peak_occupancy == 1
        if r3_window == 1:
            # 窗口 1：边界补充到 1 后 r3 放行扣到 0 -> 峰值占用 1
            assert a.windows[1].allowed_consumption == 1
            assert a.windows[1].rejected_count == 0
            assert a.windows[1].peak_occupancy == 1
        # 总计与 /simulate 判定一致
        assert a.allowed_consumption == sum(verdicts) * 1
        assert a.rejected_count == verdicts.count(False)
        assert resp.busiest_window.index == 0
        assert resp.busiest_window.allowed_count == 2
        assert resp.busiest_window.rejected_count == (0 if r3_window == 1 else 1)


def test_unused_bucket_reports_zero_consumption_and_occupancy():
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "idle", "capacity": 7, "refill_rate": 1},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"]}],
        window_seconds=5,
    )
    idle = _bucket(resp, "idle")
    assert idle.allowed_consumption == 0
    assert idle.rejected_count == 0
    # 满桶且无人引用：占用始终为 0
    assert idle.peak_occupancy == 0
    assert idle.peak_window.index == 0


def test_large_timestamps_windowed_by_elapsed_time():
    # 任意时间原点：窗口以最早请求时刻为原点
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 1_000_000, "cost": 10, "chain": ["A"]},
            {"id": "r2", "timestamp": 1_000_002, "cost": 2, "chain": ["A"]},
        ],
        window_seconds=2,
    )
    assert resp.windows[0].start == 1_000_000
    assert resp.windows[0].end == 1_000_002
    a = _bucket(resp, "A")
    assert [w.allowed_consumption for w in a.windows] == [10, 2]


# ---------- 校验 ----------

def test_window_seconds_must_be_positive():
    for bad in (0, -1, -0.5):
        with pytest.raises(Exception):
            UsageReportRequest.model_validate(
                {
                    "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                    "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
                    "window_seconds": bad,
                }
            )


def test_window_seconds_must_be_finite_number():
    for bad in ("1", None, True, float("inf"), float("nan")):
        with pytest.raises(Exception):
            UsageReportRequest.model_validate(
                {
                    "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                    "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
                    "window_seconds": bad,
                }
            )


def test_shared_validation_still_applies():
    # 沿用 /simulate 的跨字段校验：未知桶引用
    with pytest.raises(RequestValidationErrors) as exc:
        UsageReportRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["GHOST"]}],
                "window_seconds": 1,
            }
        )
    assert any(e["type"] == "unknown_bucket_ref" for e in exc.value.errors)

    # 重复桶 ID
    with pytest.raises(RequestValidationErrors) as exc:
        UsageReportRequest.model_validate(
            {
                "buckets": [
                    {"id": "A", "capacity": 1, "refill_rate": 0},
                    {"id": "A", "capacity": 2, "refill_rate": 0},
                ],
                "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
                "window_seconds": 1,
            }
        )
    assert all(e["type"] == "duplicate_bucket" for e in exc.value.errors)


# ---------- HTTP ----------

def test_http_usage_report_ok():
    response = client.post(
        "/usage-report",
        json={
            "buckets": [{"id": "A", "capacity": 10, "refill_rate": 1}],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A"]},
                {"id": "r2", "timestamp": 1, "cost": 6, "chain": ["A"]},
                {"id": "r3", "timestamp": 2, "cost": 6, "chain": ["A"]},
            ],
            "window_seconds": 2,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["window_seconds"] == 2
    assert data["window_count"] == 2
    assert data["windows"] == [
        {"index": 0, "start": 0.0, "end": 2.0},
        {"index": 1, "start": 2.0, "end": 4.0},
    ]
    bucket = data["buckets"][0]
    # 窗口 0：r1 放行（10->4），r2 在 t=1 补到 5 被拒；
    # 窗口 1：r3 在 t=2 补到 6 放行并扣到 0 -> 峰值占用 10
    assert bucket["windows"][0] == {
        "index": 0,
        "allowed_consumption": 6.0,
        "rejected_count": 1,
        "peak_occupancy": 6.0,
    }
    assert bucket["windows"][1] == {
        "index": 1,
        "allowed_consumption": 6.0,
        "rejected_count": 0,
        "peak_occupancy": 10.0,
    }
    assert bucket["peak_window"] == {"index": 1, "start": 2.0, "end": 4.0}
    # 两个窗口消耗各 6，并列取最早
    assert data["busiest_window"]["index"] == 0
    assert data["busiest_window"]["allowed_consumption"] == 6.0


def test_http_window_seconds_invalid_is_422():
    for bad in (0, -2, "1", None):
        response = client.post(
            "/usage-report",
            json={
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
                "window_seconds": bad,
            },
        )
        assert response.status_code == 422, bad
        detail = response.json()["detail"]
        assert isinstance(detail, list) and detail


def test_http_window_seconds_missing_is_422():
    response = client.post(
        "/usage-report",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
        },
    )
    assert response.status_code == 422


def test_http_shared_validation_error_is_422():
    response = client.post(
        "/usage-report",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["GHOST"]}],
            "window_seconds": 1,
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_bucket_ref" for e in detail)


def test_http_extra_field_rejected():
    response = client.post(
        "/usage-report",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
            "window_seconds": 1,
            "bogus": 1,
        },
    )
    assert response.status_code == 422


def test_http_root_lists_usage_report():
    assert client.get("/").json()["usage_report"] == "/usage-report"
