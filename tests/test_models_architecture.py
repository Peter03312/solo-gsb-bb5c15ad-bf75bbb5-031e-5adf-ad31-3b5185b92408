"""模型包结构约束：门面导出与领域依赖边界。

这些测试不校验业务语义（语义由各 test_* 模块覆盖），只锁定重构后的包
结构，防止以下回退：

1. ``app.models`` 门面少导出/改名（破坏历史导入路径）；
2. 领域叶子模块跨领域互导（重新引入跨领域耦合与潜在循环依赖）；
3. 业务处理模块绕过所属领域、改走门面或其他领域导入；
4. 模型包整体存在循环导入（正常 import 链即会暴露）。
"""
from __future__ import annotations

import ast
from pathlib import Path

import app
import app.models

APP_DIR = Path(app.__file__).parent
MODELS_DIR = APP_DIR / "models"

# 领域子包名 -> 允许导入的其他 app.models 顶层模块集合。
# 依赖方向（无环）：
#   common
#     <- simulation <- analysis / scheduling / debt
#   windows 仅依赖 common；同域叶子模块可互相导入。
_ALLOWED_MODEL_DEPS: dict[str, frozenset[str]] = {
    "common": frozenset(),
    "simulation": frozenset({"common"}),
    "analysis": frozenset({"common", "simulation"}),
    "scheduling": frozenset({"common", "simulation"}),
    "windows": frozenset({"common"}),
    "debt": frozenset({"common", "simulation"}),
}

# 各领域子包内允许的 app.models 绝对导入前缀。
_ALLOWED_PREFIXES: dict[str, tuple[str, ...]] = {
    "common": ("app.models.common",),
    "simulation": (
        "app.models.common",
        "app.models.simulation",
    ),
    "analysis": (
        "app.models.common",
        "app.models.simulation",
        "app.models.analysis",
    ),
    "scheduling": (
        "app.models.common",
        "app.models.simulation",
        "app.models.scheduling",
    ),
    "windows": (
        "app.models.common",
        "app.models.windows",
    ),
    "debt": (
        "app.models.common",
        "app.models.simulation",
        "app.models.debt",
    ),
}

# 门面必须稳定导出的全部公开名字（与重构前 app/models.py 的公开类一致）。
_EXPECTED_FACADE_EXPORTS = frozenset(
    {
        "EPS",
        "BucketBalance",
        "BucketSpec",
        "PlanBucketResult",
        "PlanFailure",
        "PlanInitialBalancesResponse",
        "SimRequest",
        "SimResult",
        "SimulateRequest",
        "SimulateResponse",
        "BusiestWindow",
        "FairnessPrincipalRow",
        "FairnessReportRequest",
        "FairnessReportResponse",
        "FairnessScopeReport",
        "FairnessSimRequest",
        "FairnessWindowReport",
        "FirstShortfall",
        "ForecastBucket",
        "ForecastBucketResult",
        "ForecastRequest",
        "ForecastResponse",
        "UsageBucketReport",
        "UsageReportRequest",
        "UsageReportResponse",
        "UsageWindowStats",
        "WindowRange",
        "OccupancyInterval",
        "QueueSimRequest",
        "QueueSimResult",
        "QueueSimulateRequest",
        "QueueSimulateResponse",
        "ReservationBucket",
        "ReservationBucketOccupancy",
        "ReservationRequest",
        "ReservationResult",
        "ReservationSimulateRequest",
        "ReservationSimulateResponse",
        "RouteCandidateRejection",
        "RouteSimRequest",
        "RouteSimResult",
        "RouteSimulateRequest",
        "RouteSimulateResponse",
        "PeriodRange",
        "RolloverBalances",
        "RolloverPolicyFinalState",
        "RolloverPolicySpec",
        "RolloverPolicyView",
        "RolloverRejection",
        "RolloverSimRequest",
        "RolloverSimResult",
        "RolloverSimulateRequest",
        "RolloverSimulateResponse",
        "SlidingWindowRange",
        "WindowPolicyFinalState",
        "WindowPolicySpec",
        "WindowPolicyView",
        "WindowPrincipalOccupancy",
        "WindowRejection",
        "WindowSimRequest",
        "WindowSimResult",
        "WindowSimulateRequest",
        "WindowSimulateResponse",
        "DebtBucketBalance",
        "DebtBucketFinalState",
        "DebtBucketSpec",
        "DebtDiffBucketDelta",
        "DebtDiffFinalSnapshot",
        "DebtDiffFirstDivergence",
        "DebtDiffOutcome",
        "DebtDiffRequest",
        "DebtDiffResponse",
        "DebtDiffResult",
        "DebtRejection",
        "DebtSimResult",
        "DebtSimulateRequest",
        "DebtSimulateResponse",
    }
)

# 处理函数模块 -> 模型导入允许落在的领域前缀（只能从公共基础或所属领域导入）。
_ENGINE_PREFIXES: dict[str, tuple[str, ...]] = {
    "simulation.py": ("app.models.common", "app.models.simulation"),
    "planner.py": ("app.models.common", "app.models.simulation"),
    "forecast.py": ("app.models.common", "app.models.analysis"),
    "usage.py": ("app.models.common", "app.models.analysis"),
    "fairness.py": ("app.models.common", "app.models.analysis"),
    "reservation.py": ("app.models.common", "app.models.scheduling"),
    "routing.py": ("app.models.common", "app.models.scheduling"),
    "queueing.py": ("app.models.common", "app.models.scheduling"),
    "windowing.py": ("app.models.common", "app.models.windows"),
    "rollover.py": ("app.models.common", "app.models.windows"),
    "debt.py": ("app.models.common", "app.models.debt"),
    "debt_diff.py": ("app.models.common", "app.models.debt"),
}


def _imports_from_app_models(path: Path) -> set[str]:
    """收集文件中 ``app.models...`` 的全部导入（from 模块 + import 模块名）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.models"):
            roots.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app.models"):
                    roots.add(alias.name)
    return roots


def _model_module_files() -> list[tuple[str, Path]]:
    """:return: (领域名, 文件路径)，含各领域叶子模块，不含 __init__ 门面。"""
    files: list[tuple[str, Path]] = []
    for domain in _ALLOWED_MODEL_DEPS:
        package_dir = MODELS_DIR / domain
        for py_file in sorted(package_dir.glob("*.py")):
            if py_file.name != "__init__.py":
                files.append((domain, py_file))
    files.append(("common", MODELS_DIR / "common.py"))
    return files


def test_facade_exports_expected_public_names():
    missing = _EXPECTED_FACADE_EXPORTS - set(app.models.__all__)
    assert not missing, f"app.models 门面缺少导出: {sorted(missing)}"

    not_resolved = [name for name in _EXPECTED_FACADE_EXPORTS if not hasattr(app.models, name)]
    assert not not_resolved, f"app.models 门面导出了但无法解析的名字: {not_resolved}"


def test_facade_all_has_no_extra_public_names():
    extra = set(app.models.__all__) - _EXPECTED_FACADE_EXPORTS
    assert not extra, f"app.models 门面出现未登记的新导出（请显式确认是否公开）: {sorted(extra)}"


def test_leaf_modules_only_import_allowed_domains():
    violations: list[str] = []
    for domain, path in _model_module_files():
        allowed = _ALLOWED_PREFIXES[domain]
        for module_root in _imports_from_app_models(path):
            if not module_root.startswith(allowed):
                violations.append(
                    f"{path.relative_to(APP_DIR)} 导入了越界模块 {module_root}；"
                    f"{domain} 域只允许 {allowed}"
                )
    assert not violations, "\n".join(violations)


def test_domain_dependency_graph_is_acyclic():
    domains = set(_ALLOWED_MODEL_DEPS)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, stack: tuple[str, ...]) -> None:
        if node in visiting:
            raise AssertionError(f"领域依赖存在环: {' -> '.join(stack + (node,))}")
        if node in visited:
            return
        visiting.add(node)
        for dep in _ALLOWED_MODEL_DEPS[node]:
            visit(dep, stack + (node,))
        visiting.remove(node)
        visited.add(node)

    for domain in domains:
        visit(domain, ())


def test_engines_import_only_from_common_or_own_domain():
    violations: list[str] = []
    for filename, allowed in _ENGINE_PREFIXES.items():
        path = APP_DIR / filename
        for module_root in _imports_from_app_models(path):
            if not module_root.startswith(allowed):
                violations.append(
                    f"{filename} 不应从 {module_root} 导入；只允许 {allowed}"
                )
    assert not violations, "\n".join(violations)


def test_only_assembly_layer_uses_facade():
    """除应用装配层（main.py）外，app/ 内业务代码不得再依赖门面。"""
    allowed_facade_users = {"main.py"}
    violations: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.name in allowed_facade_users or MODELS_DIR in path.parents:
            continue
        for module_root in _imports_from_app_models(path):
            if module_root == "app.models":
                violations.append(
                    f"{path.relative_to(APP_DIR)} 仍从门面 app.models 导入；"
                    "请迁移到公共基础或所属领域模块"
                )
    assert not violations, "\n".join(violations)


def test_leaf_modules_exist_for_each_endpoint_domain():
    expected = {
        "simulation": {"core.py", "planner.py"},
        "analysis": {"forecast.py", "usage.py", "fairness.py"},
        "scheduling": {"reservation.py", "routing.py", "queueing.py"},
        "windows": {"sliding.py", "rollover.py"},
        "debt": {"simulate.py", "diff.py"},
    }
    for domain, leaves in expected.items():
        actual = {
            p.name
            for p in (MODELS_DIR / domain).glob("*.py")
            if p.name != "__init__.py"
        }
        assert actual == leaves, f"{domain} 域叶子模块集合发生变化: {actual} != {leaves}"
