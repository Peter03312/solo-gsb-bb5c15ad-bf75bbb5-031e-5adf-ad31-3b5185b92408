"""两情景可借贷桶差异试算（/debt-diff）引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.debt import debt_simulate
from app.debt_diff import debt_diff
from app.errors import RequestValidationErrors
from app.main import app
from app.models import DebtDiffRequest, DebtSimulateRequest

client = TestClient(app)


def _bucket(**overrides):
    bucket = {
        "id": "A",
        "capacity": 10,
        "refill_rate": 1,
        "initial": 5,
        "debt_limit": 0,
    }
    bucket.update(overrides)
    return bucket


def _request(**overrides):
    request = {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}
    request.update(overrides)
    return request


def _scenario(buckets, requests):
    return {"buckets": buckets, "requests": requests}


def _payload(baseline, candidate):
    return {"baseline": baseline, "candidate": candidate}


def _run(buckets, requests):
    return debt_simulate(
        DebtSimulateRequest.model_validate({"buckets": buckets, "requests": requests})
    )


def _diff(baseline, candidate):
    return debt_diff(
        DebtDiffRequest.model_validate(
            _payload(
                _scenario(*baseline),
                _scenario(*candidate),
            )
        )
    )


# ---------- 逐侧回放与 /debt-simulate 一致 ----------


def test_each_side_replays_exactly_like_debt_simulate():
    buckets_base = [_bucket(debt_limit=0, initial=2)]
    buckets_cand = [_bucket(debt_limit=5, initial=2)]
    requests = [
        _request(id="r1", timestamp=0, cost=4),
        _request(id="r2", timestamp=3, cost=2),
    ]
    resp = _diff((buckets_base, requests), (buckets_cand, requests))

    direct_base = _run(buckets_base, requests)
    direct_cand = _run(buckets_cand, requests)

    for diff_result, rb, rc in zip(resp.results, direct_base.results, direct_cand.results):
        assert diff_result.baseline.allowed == rb.allowed
        assert diff_result.baseline.rejected_by == rb.rejected_by
        assert diff_result.candidate.allowed == rc.allowed
        assert diff_result.candidate.rejected_by == rc.rejected_by

    assert resp.final_buckets[0].baseline.model_dump() == direct_base.final_buckets[0].model_dump()
    assert (
        resp.final_buckets[0].candidate.model_dump()
        == direct_cand.final_buckets[0].model_dump()
    )


def test_identical_scenarios_have_no_divergence_and_zero_deltas():
    buckets = [_bucket(debt_limit=5, initial=3)]
    requests = [
        _request(id="r1", timestamp=0, cost=4),
        _request(id="r2", timestamp=2, cost=3),
    ]
    resp = _diff((buckets, requests), (buckets, requests))
    assert resp.divergence_count == 0
    assert resp.first_divergence is None
    assert all(r.diverged is False for r in resp.results)
    for result in resp.results:
        for bucket_delta in result.buckets.values():
            assert bucket_delta.balance_delta == 0
            assert bucket_delta.debt_delta == 0
    for final in resp.final_buckets:
        assert (final.balance_delta, final.debt_delta, final.available_delta) == (
            0,
            0,
            0,
        )
        assert final.baseline.model_dump() == final.candidate.model_dump()


# ---------- 分歧定位 ----------


def test_first_allowed_divergence_is_aligned_by_original_index():
    # baseline 不允许借债：cost 6 > 余额 5 → 拒绝；
    # candidate 允许借债 5：5 余额 + 1 借贷 → 放行。
    requests = [
        _request(id="r1", timestamp=0, cost=6),
        _request(id="r2", timestamp=2, cost=4),
    ]
    resp = _diff(
        ([_bucket(debt_limit=0)], requests),
        ([_bucket(debt_limit=5)], requests),
    )
    assert resp.request_count == 2
    assert resp.divergence_count == 1

    first = resp.first_divergence
    assert first is not None
    assert (first.request_id, first.index, first.timestamp) == ("r1", 0, 0.0)
    assert first.baseline.allowed is False
    assert first.baseline.rejected_by == "A"
    assert first.baseline.reason == "debt_limit_exceeded"
    assert first.candidate.allowed is True
    assert (first.candidate.rejected_by, first.candidate.reason) == (None, None)

    r1, r2 = resp.results
    assert r1.diverged is True
    assert r2.diverged is False
    # r1：baseline 原子拒绝余额保持 5；candidate 余额扣光、借入 1
    d1 = r1.buckets["A"]
    assert (
        d1.baseline_balance,
        d1.candidate_balance,
        d1.balance_delta,
        d1.baseline_debt,
        d1.candidate_debt,
        d1.debt_delta,
    ) == (5.0, 0.0, -5.0, 0.0, 1.0, 1.0)


def test_first_divergence_follows_original_index_not_processing_order():
    # 乱序输入：内部先处理 t=0 的 late，但结果仍按原始索引对齐，
    # 首个分歧必须是原始索引 0（t=1 的 early）。
    requests = [
        _request(id="early", timestamp=1, cost=8),
        _request(id="late", timestamp=0, cost=2),
    ]
    resp = _diff(
        ([_bucket(debt_limit=0, initial=4)], requests),
        ([_bucket(debt_limit=5, initial=4)], requests),
    )
    # 原始索引 0：baseline 4 < 8 且无借贷 → 拒绝；candidate 4+5 足够 → 放行
    assert resp.results[0].diverged is True
    assert resp.first_divergence.request_id == "early"
    assert resp.first_divergence.index == 0


def test_both_rejected_but_different_reason_is_divergence():
    # 链顺序 [B, A]，cost=16：
    # baseline 的 B：capacity 10 + debt_limit 0 = 10 < 16 → cost_exceeds_limit；
    # candidate 的 B：capacity 10 + debt_limit 10 = 20 ≥ 16（并非永久不可达），
    # 但当前余额 0 + 剩余额度 10 < 16 → debt_limit_exceeded。
    requests = [_request(id="r1", timestamp=0, cost=16, chain=["B", "A"])]
    baseline_buckets = [
        _bucket(id="A", initial=10, debt_limit=5),
        _bucket(id="B", capacity=10, initial=0, debt_limit=0),
    ]
    candidate_buckets = [
        _bucket(id="A", initial=10, debt_limit=5),
        _bucket(id="B", capacity=10, initial=0, debt_limit=10),
    ]
    resp = _diff((baseline_buckets, requests), (candidate_buckets, requests))
    assert resp.divergence_count == 1
    first = resp.first_divergence
    assert first.baseline.allowed is False and first.candidate.allowed is False
    assert first.baseline.rejected_by == "B"
    assert first.baseline.reason == "cost_exceeds_limit"
    assert first.candidate.rejected_by == "B"
    assert first.candidate.reason == "debt_limit_exceeded"


def test_both_rejected_same_reason_different_bucket_is_divergence():
    # 链 [A, B]：两侧都拒绝且原因都是 debt_limit_exceeded，
    # 但首个失败桶不同（A 的余额/借贷配置不同）。
    requests = [_request(id="r1", timestamp=0, cost=6, chain=["A", "B"])]
    baseline_buckets = [
        _bucket(id="A", initial=0, debt_limit=10),   # 0+10 够，A 不失败
        _bucket(id="B", initial=0, debt_limit=0),    # B：0 < 6 → 失败
    ]
    candidate_buckets = [
        _bucket(id="A", initial=0, debt_limit=0),    # A：0 < 6 → 先失败
        _bucket(id="B", initial=0, debt_limit=10),
    ]
    resp = _diff((baseline_buckets, requests), (candidate_buckets, requests))
    assert resp.results[0].diverged is True
    assert resp.first_divergence.baseline.rejected_by == "B"
    assert resp.first_divergence.candidate.rejected_by == "A"


def test_both_rejected_same_bucket_and_reason_is_not_divergence():
    # 两侧都是 A、debt_limit_exceeded（2+5=7<9；3+5=8<9），仅额度不同
    requests = [_request(id="r1", timestamp=0, cost=9, chain=["A"])]
    resp = _diff(
        ([_bucket(initial=2, debt_limit=5)], requests),
        ([_bucket(initial=3, debt_limit=5)], requests),
    )
    assert resp.results[0].diverged is False
    assert resp.divergence_count == 0
    assert resp.first_divergence is None


def test_divergence_count_and_first_only_counts_conclusion_differences():
    # r1 分歧（拒绝 vs 放行），r2 两侧都放行（结论相同，仅余额不同）
    requests = [
        _request(id="r1", timestamp=0, cost=6),
        _request(id="r2", timestamp=2, cost=4),
    ]
    resp = _diff(
        ([_bucket(debt_limit=0)], requests),
        ([_bucket(debt_limit=5)], requests),
    )
    assert [r.diverged for r in resp.results] == [True, False]
    assert resp.divergence_count == 1
    assert resp.first_divergence.index == 0


# ---------- 逐桶差值与终态快照 ----------


def test_bucket_delta_after_borrow_and_repayment():
    requests = [
        _request(id="r1", timestamp=0, cost=4),
        _request(id="r2", timestamp=3, cost=2),
    ]
    resp = _diff(
        ([_bucket(initial=2, debt_limit=0)], requests),
        ([_bucket(initial=2, debt_limit=5)], requests),
    )
    # r1：baseline 2<4 拒绝（余额 2、债务 0）；candidate 2→0 借 2（债务 2）
    d1 = resp.results[0].buckets["A"]
    assert (d1.balance_delta, d1.debt_delta) == (-2.0, 2.0)
    # r2（cost 2）：baseline r1 被拒余额保持 2，t=3 补到 5，扣 2 → 3；
    # candidate 补充 3 先还债 2、余 1 进余额，cost 2 扣光再借 1（债务 1）
    d2 = resp.results[1].buckets["A"]
    assert (d2.baseline_balance, d2.candidate_balance) == (3.0, 0.0)
    assert (d2.baseline_debt, d2.candidate_debt) == (0.0, 1.0)
    assert (d2.balance_delta, d2.debt_delta) == (-3.0, 1.0)

    final = resp.final_buckets[0]
    assert (final.baseline.balance, final.baseline.debt, final.baseline.available) == (
        3.0,
        0.0,
        3.0,
    )
    assert (
        final.candidate.balance,
        final.candidate.debt,
        final.candidate.available,
    ) == (0.0, 1.0, 4.0)
    assert (
        final.balance_delta,
        final.debt_delta,
        final.available_delta,
    ) == (-3.0, 1.0, 1.0)


def test_bucket_unreferenced_by_one_chain_reports_values_but_null_delta():
    # r1 baseline 只引用 A；candidate 引用 [A, B]。两情景桶集合一致。
    baseline_buckets = [
        _bucket(id="A", initial=4, debt_limit=0),
        _bucket(id="B", initial=4, debt_limit=0),
    ]
    candidate_buckets = [
        _bucket(id="A", initial=4, debt_limit=2),
        _bucket(id="B", initial=4, debt_limit=2),
    ]
    requests = [_request(id="r1", timestamp=0, cost=5, chain=["A"])]
    # 手工把 candidate 的链换成 [A, B]
    cand_scenario = _scenario(
        candidate_buckets,
        [_request(id="r1", timestamp=0, cost=5, chain=["A", "B"])],
    )
    resp = debt_diff(
        DebtDiffRequest.model_validate(
            _payload(_scenario(baseline_buckets, requests), cand_scenario)
        )
    )
    view = resp.results[0].buckets
    assert set(view) == {"A", "B"}
    # A 两侧链上均引用：baseline 4<5 拒绝余 4；candidate 4 扣光借 1
    assert (view["A"].baseline_balance, view["A"].candidate_balance) == (4.0, 0.0)
    assert (view["A"].baseline_debt, view["A"].candidate_debt) == (0.0, 1.0)
    assert (view["A"].balance_delta, view["A"].debt_delta) == (-4.0, 1.0)
    # B 仅 candidate 链引用 → delta 为 null，但两侧数值仍给出
    assert view["B"].balance_delta is None
    assert view["B"].debt_delta is None
    # baseline 的 B 未被链引用：仅时间推进（t=0 无补充），余额 4、债务 0
    assert (view["B"].baseline_balance, view["B"].baseline_debt) == (4.0, 0.0)
    # candidate 的 B：cost 5 把余额 4 扣光，缺口 1 借入
    assert (view["B"].candidate_balance, view["B"].candidate_debt) == (0.0, 1.0)


def test_final_buckets_follow_baseline_bucket_order_even_if_candidate_reorders():
    requests = [_request(id="r1", timestamp=0, cost=1, chain=["A", "B"])]
    baseline_buckets = [
        _bucket(id="A", initial=2, debt_limit=0),
        _bucket(id="B", initial=3, debt_limit=0),
    ]
    candidate_buckets = [
        _bucket(id="B", initial=3, debt_limit=1),
        _bucket(id="A", initial=2, debt_limit=1),
    ]
    resp = _diff((baseline_buckets, requests), (candidate_buckets, requests))
    assert [f.id for f in resp.final_buckets] == ["A", "B"]


def test_bucket_config_differences_show_in_final_snapshots():
    requests = [_request(id="r1", timestamp=0, cost=1)]
    resp = _diff(
        ([_bucket(initial=5, capacity=10, refill_rate=1, debt_limit=0)], requests),
        ([_bucket(initial=5, capacity=8, refill_rate=2, debt_limit=3)], requests),
    )
    b, c = resp.final_buckets[0].baseline, resp.final_buckets[0].candidate
    assert (b.capacity, b.refill_rate, b.debt_limit) == (10.0, 1.0, 0.0)
    assert (c.capacity, c.refill_rate, c.debt_limit) == (8.0, 2.0, 3.0)


# ---------- 对齐校验（422，定位错误，不回放） ----------


def _validate_error(payload):
    with pytest.raises(RequestValidationErrors) as exc:
        DebtDiffRequest.model_validate(payload)
    return exc.value.errors


def test_request_count_mismatch_is_rejected_with_location():
    requests_a = [_request(id="r1"), _request(id="r2")]
    requests_b = [_request(id="r1")]
    scenario = _scenario([_bucket()], requests_a)
    errors = _validate_error(
        _payload(scenario, _scenario([_bucket()], requests_b))
    )
    assert len(errors) == 1
    assert errors[0]["type"] == "scenario_request_count_mismatch"
    assert errors[0]["loc"] == ("candidate", "requests")
    assert errors[0]["input"] == {"baseline": 2, "candidate": 1}


def test_request_id_mismatch_is_indexed():
    requests_a = [_request(id="r1"), _request(id="r2")]
    requests_b = [_request(id="r1"), _request(id="rX")]
    errors = _validate_error(
        _payload(
            _scenario([_bucket()], requests_a),
            _scenario([_bucket()], requests_b),
        )
    )
    assert len(errors) == 1
    assert errors[0]["type"] == "scenario_request_id_mismatch"
    assert errors[0]["loc"] == ("candidate", "requests", 1, "id")
    assert errors[0]["input"] == {"index": 1, "baseline": "r2", "candidate": "rX"}


def test_timestamp_mismatch_is_indexed():
    requests_a = [_request(id="r1", timestamp=0), _request(id="r2", timestamp=2)]
    requests_b = [_request(id="r1", timestamp=0), _request(id="r2", timestamp=3)]
    errors = _validate_error(
        _payload(
            _scenario([_bucket()], requests_a),
            _scenario([_bucket()], requests_b),
        )
    )
    assert len(errors) == 1
    assert errors[0]["type"] == "scenario_timestamp_mismatch"
    assert errors[0]["loc"] == ("candidate", "requests", 1, "timestamp")
    assert errors[0]["input"] == {
        "index": 1,
        "request_id": "r2",
        "baseline": 2.0,
        "candidate": 3.0,
    }


def test_bucket_set_mismatch_lists_missing_and_extra():
    errors = _validate_error(
        _payload(
            _scenario(
                [_bucket(id="A"), _bucket(id="B", debt_limit=0)],
                [_request(chain=["A"])],
            ),
            _scenario(
                [_bucket(id="A"), _bucket(id="C", debt_limit=0)],
                [_request(chain=["A"])],
            ),
        )
    )
    assert len(errors) == 1
    assert errors[0]["type"] == "scenario_bucket_set_mismatch"
    assert errors[0]["loc"] == ("candidate", "buckets")
    assert errors[0]["input"] == {
        "missing_in_candidate": ["B"],
        "extra_in_candidate": ["C"],
    }


def test_multiple_alignment_errors_are_reported_together():
    # 两侧链都只引用各自已定义的桶，以免内部 unknown_bucket_ref 干扰；
    # 外层跨情景校验应同时报桶集合、请求 ID 与时间戳三类不一致。
    errors = _validate_error(
        _payload(
            _scenario([_bucket(id="A")], [_request(id="r1", timestamp=0, chain=["A"])]),
            _scenario([_bucket(id="B")], [_request(id="rX", timestamp=1, chain=["B"])]),
        )
    )
    assert {e["type"] for e in errors} == {
        "scenario_bucket_set_mismatch",
        "scenario_request_id_mismatch",
        "scenario_timestamp_mismatch",
    }


def test_inner_scenario_still_runs_standard_debt_validation():
    # candidate 内部引用未知桶：沿用 /debt-simulate 的跨字段校验
    with pytest.raises(RequestValidationErrors) as exc:
        DebtDiffRequest.model_validate(
            _payload(
                _scenario([_bucket()], [_request()]),
                _scenario([_bucket()], [_request(chain=["GHOST"])]),
            )
        )
    assert any(e["type"] == "unknown_bucket_ref" for e in exc.value.errors)


def test_inner_scenario_bad_debt_limit_is_pydantic_error():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DebtDiffRequest.model_validate(
            _payload(
                _scenario([_bucket()], [_request()]),
                _scenario([_bucket(debt_limit=-1)], [_request()]),
            )
        )


def test_extra_fields_forbidden():
    from pydantic import ValidationError

    payload = _payload(
        _scenario([_bucket()], [_request()]),
        _scenario([_bucket()], [_request()]),
    )
    payload["unexpected"] = 1
    with pytest.raises(ValidationError):
        DebtDiffRequest.model_validate(payload)


# ---------- HTTP ----------


def test_http_debt_diff_ok():
    requests = [
        _request(id="r1", timestamp=0, cost=6),
        _request(id="r2", timestamp=2, cost=4),
    ]
    response = client.post(
        "/debt-diff",
        json=_payload(
            _scenario([_bucket(debt_limit=0)], requests),
            _scenario([_bucket(debt_limit=5)], requests),
        ),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["request_count"] == 2
    assert data["divergence_count"] == 1
    assert data["first_divergence"]["request_id"] == "r1"
    assert data["first_divergence"]["baseline"]["allowed"] is False
    assert data["first_divergence"]["candidate"]["allowed"] is True
    assert data["results"][0]["buckets"]["A"]["debt_delta"] == 1.0
    final = data["final_buckets"][0]
    assert (
        final["baseline"]["debt_limit"],
        final["candidate"]["debt_limit"],
        final["debt_delta"],
    ) == (0.0, 5.0, 3.0)


def test_http_identical_scenarios_have_null_first_divergence():
    scenario = _scenario(
        [_bucket(debt_limit=5, initial=3)],
        [_request(id="r1", timestamp=0, cost=4)],
    )
    response = client.post("/debt-diff", json=_payload(scenario, scenario))
    assert response.status_code == 200
    data = response.json()
    assert data["divergence_count"] == 0
    assert data["first_divergence"] is None
    assert data["results"][0]["diverged"] is False


def test_http_timestamp_mismatch_is_422_without_state_effect():
    # 服务无状态：错误前后同一合法请求必须得到完全一致的结果
    good = _payload(
        _scenario([_bucket(debt_limit=5)], [_request(id="r1", timestamp=0, cost=4)]),
        _scenario([_bucket(debt_limit=0)], [_request(id="r1", timestamp=0, cost=4)]),
    )
    before = client.post("/debt-diff", json=good).json()

    bad = _payload(
        _scenario([_bucket()], [_request(id="r1", timestamp=0)]),
        _scenario([_bucket()], [_request(id="r1", timestamp=9)]),
    )
    bad_response = client.post("/debt-diff", json=bad)
    assert bad_response.status_code == 422
    detail = bad_response.json()["detail"]
    assert any(e["type"] == "scenario_timestamp_mismatch" for e in detail)

    after = client.post("/debt-diff", json=good).json()
    assert after == before


def test_http_id_and_bucket_set_mismatches_are_422():
    response = client.post(
        "/debt-diff",
        json=_payload(
            _scenario([_bucket()], [_request(id="r1")]),
            _scenario([_bucket()], [_request(id="rX")]),
        ),
    )
    assert response.status_code == 422
    assert any(
        e["type"] == "scenario_request_id_mismatch"
        for e in response.json()["detail"]
    )

    response = client.post(
        "/debt-diff",
        json=_payload(
            _scenario([_bucket(id="A")], [_request(chain=["A"])]),
            _scenario([_bucket(id="B")], [_request(chain=["B"])]),
        ),
    )
    assert response.status_code == 422
    assert any(
        e["type"] == "scenario_bucket_set_mismatch"
        for e in response.json()["detail"]
    )


def test_http_request_count_mismatch_is_422():
    response = client.post(
        "/debt-diff",
        json=_payload(
            _scenario([_bucket()], [_request(id="r1"), _request(id="r2")]),
            _scenario([_bucket()], [_request(id="r1")]),
        ),
    )
    assert response.status_code == 422
    assert any(
        e["type"] == "scenario_request_count_mismatch"
        for e in response.json()["detail"]
    )


def test_http_inner_validation_error_is_422():
    response = client.post(
        "/debt-diff",
        json=_payload(
            _scenario([_bucket()], [_request()]),
            _scenario([_bucket()], [_request(chain=["GHOST"])]),
        ),
    )
    assert response.status_code == 422
    assert any(
        e["type"] == "unknown_bucket_ref" for e in response.json()["detail"]
    )


def test_http_root_lists_debt_diff():
    root = client.get("/").json()
    assert root["debt_diff"] == "/debt-diff"
