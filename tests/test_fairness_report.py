"""公平性分析（/fairness-report）引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.fairness import _weighted_jain_index, fairness_report
from app.main import app
from app.models import FairnessReportRequest, SimulateRequest
from app.simulation import simulate

client = TestClient(app)


def _run(buckets, requests, window_seconds):
    return fairness_report(
        FairnessReportRequest.model_validate(
            {"buckets": buckets, "requests": requests, "window_seconds": window_seconds}
        )
    )


def _principal(scope, principal):
    return next(p for p in scope.principals if p.principal == principal)


def _window(resp, index):
    return resp.window_reports[index]


# ---------- 加权 Jain 指数 ----------

def test_weighted_jain_formula():
    # 等权重退化为经典 Jain：(3,1) -> 16/(2*10) = 0.8
    assert _weighted_jain_index([3.0, 1.0], [1.0, 1.0]) == 0.8
    # 完全按权重比例分配（x∝w）-> 1
    assert _weighted_jain_index([4.0, 2.0], [2.0, 1.0]) == 1.0
    # 全部为 0 -> 0
    assert _weighted_jain_index([0.0, 0.0], [1.0, 1.0]) == 0.0
    # 单主体恒为 1
    assert _weighted_jain_index([5.0], [3.0]) == 1.0
    # (x=(3,0), w=(2,1))：9 / (3 * (9/2 + 0)) = 2/3
    assert _weighted_jain_index([3.0, 0.0], [2.0, 1.0]) == pytest.approx(2 / 3, abs=1e-9)


# ---------- 引擎语义 ----------

def test_single_window_equal_weights():
    # A 容量 10 无补充：alpha 放行两笔各 3（余 4），beta 的 cost 5 被拒
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"], "principal": "alpha"},
            {"id": "r2", "timestamp": 0, "cost": 3, "chain": ["A"], "principal": "alpha"},
            {"id": "r3", "timestamp": 1, "cost": 5, "chain": ["A"], "principal": "beta"},
        ],
        window_seconds=10,
    )
    assert resp.window_count == 1
    g = resp.global_report
    assert g.principal_count == 2
    assert g.request_count == 3 and g.allowed_count == 2 and g.rejected_count == 1
    assert g.allowed_consumption == 6
    # (6,0) 等权：36 / (2 * 36) = 0.5
    assert g.weighted_jain_index == 0.5
    assert g.most_constrained_principal == "beta"
    fs = g.first_shortfall
    assert fs.request_id == "r3" and fs.index == 2 and fs.timestamp == 1
    assert fs.bucket_id == "A" and fs.cost == 5 and fs.available == 4

    alpha = _principal(g, "alpha")
    assert alpha.weight == 1
    assert alpha.request_count == 2 and alpha.allowed_count == 2 and alpha.rejected_count == 0
    assert alpha.allowed_consumption == 6
    assert alpha.theoretical_share == 3
    assert alpha.deficit == 0 and alpha.deficit_ratio == 0

    beta = _principal(g, "beta")
    assert beta.request_count == 1 and beta.allowed_count == 0 and beta.rejected_count == 1
    assert beta.allowed_consumption == 0
    assert beta.theoretical_share == 3
    assert beta.deficit == 3 and beta.deficit_ratio == 1

    # 主体顺序：按首次请求处理顺序
    assert [p.principal for p in g.principals] == ["alpha", "beta"]


def test_weighted_theoretical_shares():
    # alpha 权重 2、beta 权重 1；总放行消耗 6 -> 理论份额 4 / 2
    resp = _run(
        [{"id": "A", "capacity": 6, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A"], "principal": "alpha", "weight": 2},
            {"id": "r2", "timestamp": 1, "cost": 2, "chain": ["A"], "principal": "beta", "weight": 1},
        ],
        window_seconds=10,
    )
    g = resp.global_report
    alpha, beta = _principal(g, "alpha"), _principal(g, "beta")
    assert alpha.theoretical_share == 4 and alpha.deficit == 0
    assert beta.theoretical_share == 2 and beta.deficit == 2 and beta.deficit_ratio == 1
    assert g.most_constrained_principal == "beta"
    # (x=(6,0), w=(2,1))：36 / (3 * 18) = 2/3
    assert g.weighted_jain_index == pytest.approx(2 / 3, abs=1e-9)


def test_proportional_allocation_is_perfectly_fair():
    # 放行量恰好按权重 2:1 分配 -> Jain = 1，无最受限主体
    resp = _run(
        [{"id": "A", "capacity": 100, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"], "principal": "alpha", "weight": 2},
            {"id": "r2", "timestamp": 0, "cost": 2, "chain": ["A"], "principal": "beta", "weight": 1},
        ],
        window_seconds=10,
    )
    g = resp.global_report
    assert g.weighted_jain_index == 1
    assert g.most_constrained_principal is None
    assert g.first_shortfall is None
    assert _principal(g, "alpha").deficit == 0
    assert _principal(g, "beta").deficit == 0


def test_all_starved_most_constrained_is_earliest_rejected():
    # 零补充、零初始：谁都没拿到，短缺比例均为 1 -> 取首次拒绝最早者
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 0, "initial": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "alpha"},
            {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "beta"},
            {"id": "r3", "timestamp": 2, "cost": 1, "chain": ["A"], "principal": "beta"},
        ],
        window_seconds=10,
    )
    g = resp.global_report
    assert g.allowed_consumption == 0
    assert g.weighted_jain_index == 0
    assert g.most_constrained_principal == "alpha"
    assert g.first_shortfall.request_id == "r1"
    assert g.first_shortfall.available == 0


def test_rejected_but_over_share_is_not_constrained():
    # alpha 已拿到全部 6（理论份额 3），其后续被拒不构成短缺；
    # beta 未拿到任何东西且被拒 -> 最受限仍是 beta
    resp = _run(
        [{"id": "A", "capacity": 6, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A"], "principal": "alpha"},
            {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "alpha"},
            {"id": "r3", "timestamp": 2, "cost": 1, "chain": ["A"], "principal": "beta"},
        ],
        window_seconds=10,
    )
    g = resp.global_report
    assert g.most_constrained_principal == "beta"
    alpha = _principal(g, "alpha")
    assert alpha.rejected_count == 1 and alpha.deficit == 0


def test_first_shortfall_picks_first_insufficient_bucket_in_chain():
    # 链 [A, B]：A 充足、B 不足 -> 首个限制桶为 B
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 1, "refill_rate": 0},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A", "B"], "principal": "alpha"},
            {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A", "B"], "principal": "beta"},
        ],
        window_seconds=10,
    )
    g = resp.global_report
    # beta 的 cost 1 在 B 余额 1 时可放行；alpha 被拒且份额短缺 -> alpha 最受限
    assert g.most_constrained_principal == "alpha"
    fs = g.first_shortfall
    assert fs.request_id == "r1" and fs.bucket_id == "B" and fs.available == 1


def test_per_window_reports_and_half_open_boundary():
    # 容量 10、速率 1：窗口宽 5；r2 恰在 t=5 边界 -> 属于窗口 1
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A"], "principal": "alpha"},
            {"id": "r2", "timestamp": 5, "cost": 6, "chain": ["A"], "principal": "beta"},
        ],
        window_seconds=5,
    )
    assert resp.window_count == 2
    assert [w.model_dump() for w in resp.windows] == [
        {"index": 0, "start": 0.0, "end": 5.0},
        {"index": 1, "start": 5.0, "end": 10.0},
    ]
    w0, w1 = _window(resp, 0), _window(resp, 1)
    assert [p.principal for p in w0.principals] == ["alpha"]
    assert [p.principal for p in w1.principals] == ["beta"]
    # 窗口内单主体：Jain 恒为 1
    assert w0.principal_count == 1 and w0.weighted_jain_index == 1
    assert w1.principal_count == 1 and w1.weighted_jain_index == 1
    assert w0.allowed_consumption == 6 and w0.rejected_count == 0
    # 窗口 1 边界补充到 10，beta 放行
    assert w1.allowed_consumption == 6 and w1.rejected_count == 0
    # 各窗口均无最受限主体（没有短缺主体）
    assert w0.most_constrained_principal is None
    assert w1.most_constrained_principal is None


def test_near_boundary_request_stays_in_own_window():
    # 回归：t=999999999.5 在右边界 1e9 之前，必须留在窗口 0——旧实现用 EPS
    # 吸附把它提前归入窗口 1，逐窗主体请求数/放行消耗/拒绝次数与窗口数量随之失真。
    buckets = [{"id": "A", "capacity": 1, "refill_rate": 0.75, "initial": 1}]
    requests = [
        {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "alpha"},
        {"id": "r2", "timestamp": 999999998.5, "cost": 1, "chain": ["A"], "principal": "beta"},
        {"id": "r3", "timestamp": 999999999.5, "cost": 1, "chain": ["A"], "principal": "gamma"},
    ]
    resp = _run(buckets, requests, window_seconds=1_000_000_000)
    sim = simulate(
        SimulateRequest.model_validate(
            {
                "buckets": buckets,
                "requests": [
                    {k: v for k, v in r.items() if k != "principal"} for r in requests
                ],
            }
        )
    )
    assert [r.allowed for r in sim.results] == [True, True, False]

    # 三个请求都在窗口 0 [0, 1e9) 内，不产生第二个窗口
    assert resp.window_count == 1
    assert [w.model_dump() for w in resp.windows] == [
        {"index": 0, "start": 0.0, "end": 1e9}
    ]
    w0 = _window(resp, 0)
    assert w0.principal_count == 3
    assert w0.request_count == 3 and w0.allowed_count == 2 and w0.rejected_count == 1
    assert w0.allowed_consumption == 2
    gamma = _principal(w0, "gamma")
    assert gamma.request_count == 1 and gamma.allowed_count == 0
    assert gamma.rejected_count == 1 and gamma.allowed_consumption == 0
    # gamma 份额 2/3、获得 0 -> 最受限；首个短缺即边界前的 r3（余额 0.75）
    assert w0.most_constrained_principal == "gamma"
    assert w0.first_shortfall.request_id == "r3"
    assert w0.first_shortfall.available == 0.75
    # 全局汇总与逐窗统计一致（单窗口时完全相等）
    g = resp.global_report
    assert g.principal_count == w0.principal_count
    assert g.request_count == w0.request_count
    assert g.allowed_count == w0.allowed_count
    assert g.rejected_count == w0.rejected_count
    assert g.allowed_consumption == w0.allowed_consumption
    assert g.weighted_jain_index == w0.weighted_jain_index
    assert g.most_constrained_principal == "gamma"
    assert g.first_shortfall.request_id == "r3"


def test_boundary_sweep_window_assignment():
    # 半开归属扫掠：t3 < 1e9 时留在窗口 0，t3 >= 1e9 时进入窗口 1；
    # 窗口数量、逐窗统计与全局汇总随归属一致变化，判定始终与 /simulate 相同。
    buckets = [{"id": "A", "capacity": 1, "refill_rate": 0.75, "initial": 1}]
    width = 1_000_000_000
    for offset in (-1, -0.5, 0, 0.5, 1):
        t3 = width + offset
        requests = [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "alpha"},
            {"id": "r2", "timestamp": width - 1.5, "cost": 1, "chain": ["A"], "principal": "beta"},
            {"id": "r3", "timestamp": t3, "cost": 1, "chain": ["A"], "principal": "beta"},
        ]
        resp = _run(buckets, requests, window_seconds=width)
        sim = simulate(
            SimulateRequest.model_validate(
                {
                    "buckets": buckets,
                    "requests": [
                        {k: v for k, v in r.items() if k != "principal"} for r in requests
                    ],
                }
            )
        )
        verdicts = [r.allowed for r in sim.results]
        # 边界前余额只补到 0.75 以下被拒，边界上及之后补满放行
        assert verdicts == [True, True, offset >= 0]

        r3_window = 0 if t3 < width else 1
        assert resp.window_count == r3_window + 1
        w0 = _window(resp, 0)
        # 窗口 0：r1/r2 放行各计 1；r3 留在窗口 0 时被拒计 1
        assert w0.request_count == (3 if r3_window == 0 else 2)
        assert w0.allowed_consumption == 2
        assert w0.rejected_count == (1 if r3_window == 0 else 0)
        beta0 = _principal(w0, "beta")
        assert beta0.request_count == (2 if r3_window == 0 else 1)
        if r3_window == 1:
            w1 = _window(resp, 1)
            assert w1.request_count == 1 and w1.allowed_count == 1
            assert _principal(w1, "beta").allowed_consumption == 1
        # 全局汇总 = 逐窗合计，且与 /simulate 判定一致
        g = resp.global_report
        assert g.request_count == 3
        assert g.allowed_count == sum(verdicts)
        assert g.rejected_count == verdicts.count(False)
        assert g.allowed_consumption == sum(
            w.allowed_consumption for w in resp.window_reports
        )


def test_per_window_first_shortfall_is_window_local():
    # beta 在两个窗口各被拒一次；窗口 1 的 first_shortfall 必须指向窗口内拒绝
    resp = _run(
        [{"id": "A", "capacity": 5, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "principal": "alpha"},
            {"id": "r2", "timestamp": 0, "cost": 5, "chain": ["A"], "principal": "beta"},
            {"id": "r3", "timestamp": 5, "cost": 5, "chain": ["A"], "principal": "alpha"},
            {"id": "r4", "timestamp": 5, "cost": 5, "chain": ["A"], "principal": "beta"},
        ],
        window_seconds=5,
    )
    w0, w1 = _window(resp, 0), _window(resp, 1)
    assert w0.most_constrained_principal == "beta"
    assert w0.first_shortfall.request_id == "r2"
    assert w1.most_constrained_principal == "beta"
    assert w1.first_shortfall.request_id == "r4"
    # 全局首个短缺仍是最早那次
    assert resp.global_report.first_shortfall.request_id == "r2"


def test_global_most_constrained_aggregates_across_windows():
    # alpha 窗口 0 拿 6；beta 窗口 0 被拒、窗口 1 拿 1（补充 1 后放行 cost 1）
    resp = _run(
        [{"id": "A", "capacity": 6, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A"], "principal": "alpha"},
            {"id": "r2", "timestamp": 1, "cost": 2, "chain": ["A"], "principal": "beta"},
            {"id": "r3", "timestamp": 5, "cost": 1, "chain": ["A"], "principal": "beta"},
        ],
        window_seconds=5,
    )
    g = resp.global_report
    assert g.allowed_consumption == 7
    alpha, beta = _principal(g, "alpha"), _principal(g, "beta")
    assert alpha.allowed_consumption == 6 and alpha.rejected_count == 0
    assert beta.allowed_consumption == 1 and beta.rejected_count == 1
    assert beta.theoretical_share == 3.5
    assert beta.deficit == pytest.approx(2.5, abs=1e-9)
    assert g.most_constrained_principal == "beta"
    assert g.first_shortfall.request_id == "r2"


def test_deficit_ratio_tie_picks_earliest_first_rejection():
    # 零补充：alpha、beta 各被拒一笔（同刻按输入顺序），gamma 拿 1
    resp = _run(
        [{"id": "A", "capacity": 1, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "gamma"},
            {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "alpha"},
            {"id": "r3", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "beta"},
        ],
        window_seconds=10,
    )
    g = resp.global_report
    # alpha、beta 份额各 1/3、获得 0，短缺比例均为 1 -> 首次拒绝较早的 alpha
    assert g.most_constrained_principal == "alpha"
    assert g.first_shortfall.request_id == "r2"


def test_out_of_order_inputs_processed_chronologically():
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1, "initial": 0}],
        [
            {"id": "r_late", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p"},
            {"id": "r_early", "timestamp": 5, "cost": 5, "chain": ["A"], "principal": "q"},
        ],
        window_seconds=5,
    )
    w0, w1 = _window(resp, 0), _window(resp, 1)
    p0 = _principal(w0, "p")
    q1 = _principal(w1, "q")
    assert p0.allowed_consumption == 0 and p0.rejected_count == 1
    assert q1.allowed_consumption == 5 and q1.rejected_count == 0


def test_consistent_with_simulate_verdicts():
    buckets = [
        {"id": "A", "capacity": 10, "refill_rate": 1},
        {"id": "B", "capacity": 5, "refill_rate": 0.5},
    ]
    raw_requests = [
        {"id": f"r{i}", "timestamp": t, "cost": 3, "chain": ["A", "B"], "principal": f"u{i % 3}"}
        for i, t in enumerate([0, 0, 1, 2, 5, 9, 9, 12])
    ]
    report = _run(buckets, raw_requests, window_seconds=4)
    sim = simulate(
        SimulateRequest.model_validate(
            {
                "buckets": buckets,
                "requests": [
                    {k: v for k, v in r.items() if k != "principal"} for r in raw_requests
                ],
            }
        )
    )
    g = report.global_report
    assert g.allowed_count == sim.allowed_count
    assert g.rejected_count == sim.rejected_count
    # 放行消耗 = 放行请求数 * cost（每个放行请求只给主体计一次）
    assert g.allowed_consumption == 3 * sim.allowed_count
    assert sum(p.request_count for p in g.principals) == len(raw_requests)


def test_default_weight_is_one():
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"], "principal": "a"},
            {"id": "r2", "timestamp": 0, "cost": 4, "chain": ["A"], "principal": "b"},
        ],
        window_seconds=10,
    )
    assert {p.principal: p.weight for p in resp.global_report.principals} == {
        "a": 1.0,
        "b": 1.0,
    }


# ---------- 微小正权重（回归：不得舍入成 0 后除零） ----------

def test_tiny_positive_weights_not_rounded_to_zero():
    # 两个主体权重 1e-10 / 2e-10、各放行 cost=1：旧实现把响应权重 _round 成 0
    # 后送进加权 Jain 公式，Σx²/w 除零崩溃，理论份额也误报为 0。
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "a", "weight": 1e-10},
            {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "b", "weight": 2e-10},
        ],
        window_seconds=10,
    )
    # 理论份额按未舍入权重 1:2 切分总消耗 2 -> 2/3 与 4/3
    expected_jain = pytest.approx(8 / 9, abs=1e-9)
    for scope in [resp.global_report, resp.window_reports[0]]:
        assert scope.allowed_consumption == 2
        assert scope.weighted_jain_index == expected_jain
        a = _principal(scope, "a")
        b = _principal(scope, "b")
        # 响应保留正权重的有效值，而非舍入后的 0
        assert a.weight == 1e-10 and b.weight == 2e-10
        assert a.theoretical_share == pytest.approx(2 / 3, abs=1e-9)
        assert b.theoretical_share == pytest.approx(4 / 3, abs=1e-9)
        assert a.deficit == 0
        assert b.deficit == pytest.approx(1 / 3, abs=1e-9)
        assert b.deficit_ratio == pytest.approx(0.25, abs=1e-9)
        # 均无被拒请求 -> 无最受限主体
        assert scope.most_constrained_principal is None
        assert scope.first_shortfall is None


def test_tiny_weights_with_rejection_still_picks_constrained():
    # 微小权重下容量仅够 1 笔：a 放行、b 被拒；份额 2/3 vs 4/3，b 短缺比例 1
    resp = _run(
        [{"id": "A", "capacity": 1, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "a", "weight": 1e-10},
            {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "b", "weight": 2e-10},
        ],
        window_seconds=10,
    )
    g = resp.global_report
    assert g.allowed_consumption == 1
    # (x=(1,0), w=(1e-10,2e-10))：1 / (3e-10 · 1/1e-10) = 1/3
    assert g.weighted_jain_index == pytest.approx(1 / 3, abs=1e-9)
    assert g.most_constrained_principal == "b"
    assert g.first_shortfall.request_id == "r2"
    assert g.first_shortfall.available == 0
    b = _principal(g, "b")
    assert b.weight == 2e-10 and b.theoretical_share == pytest.approx(2 / 3, abs=1e-9)
    assert b.deficit_ratio == 1


def test_jain_index_zero_only_when_all_allocations_zero():
    # 单主体获得量为极小正值（>0）时指数应为 1，而不是按 EPS 误判成 0
    assert _weighted_jain_index([1e-12], [1e-12]) == 1.0
    assert _weighted_jain_index([1e-12], [5.0]) == 1.0
    assert _weighted_jain_index([1e-12, 2e-12], [1e-10, 2e-10]) == 1.0
    # 仅实际获得量恰好为 0 时定义为 0（权重为任意正数都不除零）
    assert _weighted_jain_index([0.0, 0.0], [1e-10, 2e-10]) == 0.0


# ---------- 校验 ----------

def test_principal_must_be_nonempty_string():
    for bad in ("", 1, None, True):
        with pytest.raises(Exception):
            FairnessReportRequest.model_validate(
                {
                    "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                    "requests": [
                        {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": bad}
                    ],
                    "window_seconds": 1,
                }
            )


def test_weight_must_be_positive_finite():
    for bad in (0, -1, -0.5, "1", None, True, float("inf"), float("nan")):
        with pytest.raises(Exception):
            FairnessReportRequest.model_validate(
                {
                    "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                    "requests": [
                        {
                            "id": "r",
                            "timestamp": 0,
                            "cost": 1,
                            "chain": ["A"],
                            "principal": "p",
                            "weight": bad,
                        }
                    ],
                    "window_seconds": 1,
                }
            )


def test_inconsistent_principal_weight_rejected():
    with pytest.raises(RequestValidationErrors) as exc:
        FairnessReportRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0}],
                "requests": [
                    {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p", "weight": 1},
                    {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "p", "weight": 2},
                ],
                "window_seconds": 1,
            }
        )
    assert any(e["type"] == "inconsistent_principal_weight" for e in exc.value.errors)


def test_inconsistent_tiny_weights_rejected_by_true_value():
    # 回归：1e-10 与 2e-10 真实数值相差一倍，不能因固定绝对容差（EPS=1e-9）
    # 误判一致；错误须定位到请求 weight 字段。
    with pytest.raises(RequestValidationErrors) as exc:
        FairnessReportRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0}],
                "requests": [
                    {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p", "weight": 1e-10},
                    {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "p", "weight": 2e-10},
                ],
                "window_seconds": 1,
            }
        )
    errors = [e for e in exc.value.errors if e["type"] == "inconsistent_principal_weight"]
    assert len(errors) == 1
    assert errors[0]["loc"] == ("requests", 1, "weight")
    assert errors[0]["input"] == 2e-10


def test_repeated_tiny_weight_is_consistent():
    # 同一主体多次给出相同的微小权重（真实数值相同）必须正常通过
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p", "weight": 1e-10},
            {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "p", "weight": 1e-10},
        ],
        window_seconds=10,
    )
    assert resp.global_report.principals[0].weight == 1e-10
    assert resp.global_report.weighted_jain_index == 1.0


def test_shared_validation_still_applies():
    base_bucket = [{"id": "A", "capacity": 1, "refill_rate": 0}]
    base_request = {
        "id": "r",
        "timestamp": 0,
        "cost": 1,
        "chain": ["GHOST"],
        "principal": "p",
    }
    # 未知桶引用
    with pytest.raises(RequestValidationErrors) as exc:
        FairnessReportRequest.model_validate(
            {"buckets": base_bucket, "requests": [base_request], "window_seconds": 1}
        )
    assert any(e["type"] == "unknown_bucket_ref" for e in exc.value.errors)

    # 重复桶 ID
    with pytest.raises(RequestValidationErrors) as exc:
        FairnessReportRequest.model_validate(
            {
                "buckets": [
                    {"id": "A", "capacity": 1, "refill_rate": 0},
                    {"id": "A", "capacity": 2, "refill_rate": 0},
                ],
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p"}
                ],
                "window_seconds": 1,
            }
        )
    assert all(e["type"] == "duplicate_bucket" for e in exc.value.errors)

    # 重复请求 ID
    with pytest.raises(RequestValidationErrors) as exc:
        FairnessReportRequest.model_validate(
            {
                "buckets": base_bucket,
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p"},
                    {"id": "r", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "q"},
                ],
                "window_seconds": 1,
            }
        )
    assert any(e["type"] == "duplicate_request" for e in exc.value.errors)


def test_window_seconds_validation():
    template = {
        "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
        "requests": [
            {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p"}
        ],
    }
    for bad in (0, -1, "1", None, float("inf"), float("nan")):
        with pytest.raises(Exception):
            FairnessReportRequest.model_validate({**template, "window_seconds": bad})


# ---------- HTTP ----------

def test_http_fairness_report_ok():
    response = client.post(
        "/fairness-report",
        json={
            "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0}],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 6, "chain": ["A"], "principal": "alpha"},
                {"id": "r2", "timestamp": 1, "cost": 6, "chain": ["A"], "principal": "beta"},
            ],
            "window_seconds": 5,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["window_seconds"] == 5 and data["window_count"] == 1
    assert data["windows"] == [{"index": 0, "start": 0.0, "end": 5.0}]
    g = data["global_report"]
    assert g["principal_count"] == 2
    assert g["allowed_count"] == 1 and g["rejected_count"] == 1
    assert g["allowed_consumption"] == 6
    assert g["weighted_jain_index"] == 0.5
    assert g["most_constrained_principal"] == "beta"
    assert g["first_shortfall"] == {
        "request_id": "r2",
        "index": 1,
        "timestamp": 1.0,
        "bucket_id": "A",
        "cost": 6.0,
        "available": 4.0,
    }
    assert len(data["window_reports"]) == 1
    assert data["window_reports"][0]["index"] == 0


def test_http_invalid_weight_is_422():
    for bad in (0, -2, "x", None):
        response = client.post(
            "/fairness-report",
            json={
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p", "weight": bad}
                ],
                "window_seconds": 1,
            },
        )
        assert response.status_code == 422, bad


def test_http_inconsistent_weight_is_422():
    response = client.post(
        "/fairness-report",
        json={
            "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0}],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p", "weight": 1},
                {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "p", "weight": 2},
            ],
            "window_seconds": 1,
        },
    )
    assert response.status_code == 422
    assert any(
        e["type"] == "inconsistent_principal_weight"
        for e in response.json()["detail"]
    )


def test_http_inconsistent_tiny_weight_is_422():
    response = client.post(
        "/fairness-report",
        json={
            "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0}],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p", "weight": 1e-10},
                {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "p", "weight": 2e-10},
            ],
            "window_seconds": 1,
        },
    )
    assert response.status_code == 422
    errors = response.json()["detail"]
    assert any(e["type"] == "inconsistent_principal_weight" for e in errors)
    # 定位到第二个请求的 weight
    hit = next(e for e in errors if e["type"] == "inconsistent_principal_weight")
    assert hit["loc"] == ["requests", 1, "weight"]


def test_http_tiny_positive_weights_ok():
    response = client.post(
        "/fairness-report",
        json={
            "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0}],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "a", "weight": 1e-10},
                {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "b", "weight": 2e-10},
            ],
            "window_seconds": 10,
        },
    )
    assert response.status_code == 200
    data = response.json()
    weights = {p["principal"]: p["weight"] for p in data["global_report"]["principals"]}
    assert weights == {"a": 1e-10, "b": 2e-10}
    assert data["global_report"]["weighted_jain_index"] == pytest.approx(8 / 9, abs=1e-9)


def test_http_missing_principal_is_422():
    response = client.post(
        "/fairness-report",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}],
            "window_seconds": 1,
        },
    )
    assert response.status_code == 422


def test_http_window_seconds_missing_is_422():
    response = client.post(
        "/fairness-report",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p"}
            ],
        },
    )
    assert response.status_code == 422


def test_http_extra_field_rejected():
    response = client.post(
        "/fairness-report",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p"}
            ],
            "window_seconds": 1,
            "bogus": 1,
        },
    )
    assert response.status_code == 422


def test_http_root_lists_fairness_report():
    assert client.get("/").json()["fairness_report"] == "/fairness-report"
