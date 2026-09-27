"""令牌桶 capacity/refill_rate 非有限数值回归测试。

JSON 裸 token NaN/Infinity/-Infinity 以及数字字符串转换后的非有限值
（"NaN"、"Infinity"、"1e999" 等）必须在请求模型阶段被拒绝：
/simulate、/queue-simulate、/route-simulate、/debt-simulate 及其复用
同一 BucketSpec 的 /plan-initial-balances、/usage-report、
/fairness-report，加上 /debt-diff 的嵌套情景与 /forecast 的
ForecastBucket，全部返回结构一致的 HTTP 422，不得进入模拟引擎
（否则会产生 NaN 余额或在响应渲染时退化为 500）。
"""
from __future__ import annotations

import json
import math

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.main import app

client = TestClient(app)

# 两类 JSON 边界输入：
# - float：经 json.dumps(allow_nan=True) 写成裸 token NaN/Infinity/-Infinity；
# - str：JSON 字符串，由服务端按宽松数值转换，转换结果仍为非有限值。
NON_FINITE_TOKENS = [
    float("nan"),
    float("inf"),
    float("-inf"),
]
NON_FINITE_STRINGS = [
    "NaN",
    "Infinity",
    "-Infinity",
    "nan",
    "inf",
    "-infinity",
    "1e999",   # 转换为 +Infinity
    "-1e999",  # 转换为 -Infinity
]

RATE_FIELDS = ("capacity", "refill_rate")


def _bucket(**overrides) -> dict:
    bucket = {"id": "A", "capacity": 10, "refill_rate": 1}
    bucket.update(overrides)
    return bucket


def _request(endpoint: str) -> dict:
    if endpoint == "/queue-simulate":
        return {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "deadline": 1}
    if endpoint == "/route-simulate":
        return {"id": "r", "timestamp": 0, "cost": 1, "chains": [["A"]]}
    return {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"]}


def _payload(endpoint: str, field: str, value) -> dict:
    """构造在桶参数 field 上携带非有限值的请求体。"""
    if endpoint == "/forecast":
        return {
            "now": 0,
            "cost": 1,
            "chain": ["A"],
            "buckets": [_bucket(balance=5, **{field: value})],
        }

    bucket = _bucket(**{field: value})
    request = _request(endpoint)

    if endpoint == "/debt-simulate":
        bucket["debt_limit"] = 0
    if endpoint == "/debt-diff":
        bucket["debt_limit"] = 0
        return {
            "baseline": {
                "buckets": [_bucket(debt_limit=0)],
                "requests": [_request("/simulate")],
            },
            "candidate": {
                "buckets": [bucket],
                "requests": [_request("/simulate")],
            },
        }
    if endpoint == "/usage-report":
        return {"buckets": [bucket], "requests": [request], "window_seconds": 10}
    if endpoint == "/fairness-report":
        request = {
            "id": "r",
            "timestamp": 0,
            "cost": 1,
            "chain": ["A"],
            "principal": "p",
        }
        return {"buckets": [bucket], "requests": [request], "window_seconds": 10}
    return {"buckets": [bucket], "requests": [request]}


# 令牌桶四大入口 + 复用 BucketSpec 的分析/规划入口 + /forecast
# （独立的 ForecastBucket）+ /debt-diff（嵌套 DebtSimulateRequest）
TOKEN_BUCKET_ENDPOINTS = [
    "/simulate",
    "/queue-simulate",
    "/route-simulate",
    "/debt-simulate",
    "/plan-initial-balances",
    "/usage-report",
    "/fairness-report",
    "/forecast",
    "/debt-diff",
]


@pytest.mark.parametrize("endpoint", TOKEN_BUCKET_ENDPOINTS)
@pytest.mark.parametrize("field", RATE_FIELDS)
@pytest.mark.parametrize("value", NON_FINITE_TOKENS)
def test_non_finite_json_tokens_rejected_with_422(endpoint, field, value):
    # allow_nan=True：把 NaN/Infinity 以裸 JSON token 发送（标准 json
    # 解析器可读出 float('nan')/float('inf')）
    raw = json.dumps(_payload(endpoint, field, value), allow_nan=True)
    response = client.post(
        endpoint, content=raw, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422, (endpoint, field, value, response.text)
    data = response.json()
    assert set(data.keys()) == {"detail"}
    assert isinstance(data["detail"], list) and data["detail"]
    # 错误定位到具体桶字段，结构与普通 422 一致
    locations = [tuple(err["loc"]) for err in data["detail"]]
    assert any(field in loc for loc in locations), (endpoint, field, locations)
    assert all("msg" in err for err in data["detail"])


@pytest.mark.parametrize("endpoint", TOKEN_BUCKET_ENDPOINTS)
@pytest.mark.parametrize("field", RATE_FIELDS)
@pytest.mark.parametrize("value", NON_FINITE_STRINGS)
def test_non_finite_numeric_strings_rejected_with_422(endpoint, field, value):
    response = client.post(endpoint, json=_payload(endpoint, field, value))
    assert response.status_code == 422, (endpoint, field, value, response.text)
    data = response.json()
    assert isinstance(data["detail"], list) and data["detail"]
    locations = [tuple(err["loc"]) for err in data["detail"]]
    assert any(field in loc for loc in locations), (endpoint, field, locations)


@pytest.mark.parametrize("endpoint", TOKEN_BUCKET_ENDPOINTS)
def test_non_finite_input_never_enters_simulation(endpoint):
    # 防御性断言：非有限输入只能得到 422，绝不允许 200/500 或任何余额输出
    for field in RATE_FIELDS:
        for value in (float("nan"), float("inf"), "NaN", "Infinity", "1e999"):
            raw = json.dumps(_payload(endpoint, field, value), allow_nan=True)
            response = client.post(
                endpoint, content=raw, headers={"content-type": "application/json"}
            )
            assert response.status_code == 422, (endpoint, field, value, response.status_code)
            body = response.text
            assert "results" not in body or '"detail"' in body
            assert "final_balances" not in body
            assert "final_buckets" not in body


def test_422_error_echoes_input_as_json_safe_value():
    # NaN 在错误回显中必须是可 JSON 解析的安全形式（字符串），
    # 而不是让 422 处理器在渲染时崩溃退化为 500
    raw = json.dumps(
        _payload("/simulate", "capacity", float("nan")), allow_nan=True
    )
    response = client.post(
        "/simulate", content=raw, headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    echoed = [err["input"] for err in detail if "input" in err]
    assert "NaN" in echoed
    assert all(isinstance(item, (str, int, float, bool, type(None), dict, list))
               for item in echoed)


# ---------- 现有 sign 规则保持不变 ----------

@pytest.mark.parametrize(
    "capacity,refill_rate",
    [(0, 1), (-1, 1), (-1e-9, 1), (1, -0.5), (1, -1e308)],
)
def test_sign_rules_still_enforced(capacity, refill_rate):
    response = client.post(
        "/simulate",
        json={
            "buckets": [_bucket(capacity=capacity, refill_rate=refill_rate)],
            "requests": [_request("/simulate")],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    fields = {loc for err in detail for loc in err["loc"]}
    assert "capacity" in fields or "refill_rate" in fields


def test_zero_refill_rate_is_still_allowed():
    response = client.post(
        "/simulate",
        json={
            "buckets": [_bucket(capacity=3, refill_rate=0)],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A"]},
                {"id": "r2", "timestamp": 100, "cost": 1, "chain": ["A"]},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["results"][1]["allowed"] is False
    assert data["results"][1]["balances"]["A"]["before"] == 0


# ---------- 正常有限值（含边界大数与数字字符串）保持可用 ----------

@pytest.mark.parametrize(
    "capacity,refill_rate",
    [
        (10, 1),
        (10, 0),
        (1e308, 0),          # 可表示的最大有限数之一
        (5e-324, 0),         # 可表示的最小正有限数
        ("10", "1"),         # 有限数字字符串维持宽松转换
        ("10.5", "0.25"),
    ],
)
def test_finite_values_still_accepted_on_simulate(capacity, refill_rate):
    response = client.post(
        "/simulate",
        json={
            "buckets": [_bucket(capacity=capacity, refill_rate=refill_rate)],
            "requests": [_request("/simulate")],
        },
    )
    assert response.status_code == 200, (capacity, refill_rate, response.text)
    data = response.json()
    # 响应体中不得出现 NaN/Infinity 文本
    assert all(math.isfinite(v) for v in data["final_balances"].values())


@pytest.mark.parametrize(
    "endpoint,payload_factory",
    [
        (
            "/queue-simulate",
            lambda: {
                "buckets": [_bucket(capacity=10, refill_rate=1)],
                "requests": [_request("/queue-simulate")],
            },
        ),
        (
            "/route-simulate",
            lambda: {
                "buckets": [_bucket(capacity=10, refill_rate=1)],
                "requests": [_request("/route-simulate")],
            },
        ),
        (
            "/debt-simulate",
            lambda: {
                "buckets": [_bucket(capacity=10, refill_rate=1, debt_limit=2)],
                "requests": [_request("/simulate")],
            },
        ),
        (
            "/forecast",
            lambda: {
                "now": 0,
                "cost": 1,
                "chain": ["A"],
                "buckets": [_bucket(capacity=10, refill_rate=1, balance=5)],
            },
        ),
        (
            "/plan-initial-balances",
            lambda: {
                "buckets": [_bucket(capacity=10, refill_rate=1)],
                "requests": [_request("/simulate")],
            },
        ),
        (
            "/usage-report",
            lambda: {
                "buckets": [_bucket(capacity=10, refill_rate=1)],
                "requests": [_request("/simulate")],
                "window_seconds": 10,
            },
        ),
        (
            "/fairness-report",
            lambda: {
                "buckets": [_bucket(capacity=10, refill_rate=1)],
                "requests": [
                    {
                        "id": "r",
                        "timestamp": 0,
                        "cost": 1,
                        "chain": ["A"],
                        "principal": "p",
                    }
                ],
                "window_seconds": 10,
            },
        ),
        (
            "/debt-diff",
            lambda: {
                "baseline": {
                    "buckets": [_bucket(capacity=10, refill_rate=1, debt_limit=2)],
                    "requests": [_request("/simulate")],
                },
                "candidate": {
                    "buckets": [_bucket(capacity=8, refill_rate=2, debt_limit=2)],
                    "requests": [_request("/simulate")],
                },
            },
        ),
    ],
)
def test_finite_requests_still_succeed_on_all_entries(endpoint, payload_factory):
    response = client.post(endpoint, json=payload_factory())
    assert response.status_code == 200, (endpoint, response.text)


# ---------- 模型层直接校验（不经 HTTP） ----------

@pytest.mark.parametrize("field", RATE_FIELDS)
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", "1e999", True, False],
)
def test_models_reject_non_finite_values(field, value):
    from app.models import BucketSpec, ForecastBucket

    bucket_kwargs = {"id": "A", "capacity": 10, "refill_rate": 1, field: value}
    with pytest.raises(ValidationError) as exc:
        BucketSpec.model_validate(bucket_kwargs)
    assert any(field in err["loc"] for err in exc.value.errors())

    forecast_kwargs = {"id": "A", "capacity": 10, "refill_rate": 1, "balance": 5, field: value}
    with pytest.raises(ValidationError) as exc:
        ForecastBucket.model_validate(forecast_kwargs)
    assert any(field in err["loc"] for err in exc.value.errors())


def test_debt_bucket_spec_inherits_finite_validation():
    # DebtBucketSpec 继承 BucketSpec：capacity/refill_rate 的有限性
    # 校验必须随之继承，不需要重复实现
    from app.models import DebtBucketSpec

    for field in RATE_FIELDS:
        kwargs = {"id": "A", "capacity": 10, "refill_rate": 1, "debt_limit": 0, field: float("inf")}
        with pytest.raises(ValidationError):
            DebtBucketSpec.model_validate(kwargs)
