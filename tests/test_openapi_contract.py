"""OpenAPI 契约锁定：重构后 schema 名称、请求/响应结构与路由保持不变。

``tests/fixtures/openapi_schema.json`` 是重构前（单体 app/models.py）从
运行中的 FastAPI 应用导出的 OpenAPI 文档。模型拆分后 FastAPI 按类所在
模块生成的 ``\$defs``/组件必须与之一致——这同时锁定了：

- 组件（schema）名称集合；
- 每个请求/响应模型的字段、校验注解与嵌套引用；
- 路由路径、方法、标签与 response_model 绑定。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.main import app

_FIXTURE = Path(__file__).parent / "fixtures" / "openapi_schema.json"


def test_openapi_schema_matches_baseline():
    with _FIXTURE.open(encoding="utf-8") as handle:
        baseline = json.load(handle)
    current = app.openapi()

    # 组件名集合显式断言，失败信息比整份文档 diff 更易定位。
    assert set(current["components"]["schemas"]) == set(baseline["components"]["schemas"])
    assert current == baseline


def test_routes_match_baseline():
    with _FIXTURE.open(encoding="utf-8") as handle:
        baseline_paths = json.load(handle)["paths"]

    current_paths = app.openapi()["paths"]
    assert set(current_paths) == set(baseline_paths)
    for path, operations in baseline_paths.items():
        assert set(current_paths[path]) == set(operations), path
        for method, operation in operations.items():
            assert current_paths[path][method]["operationId"] == operation["operationId"], (
                path,
                method,
            )
            assert current_paths[path][method].get("tags") == operation.get("tags"), (path, method)
