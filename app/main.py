"""FastAPI 应用入口。"""
from __future__ import annotations

import math

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import RequestValidationErrors
from app.debt import debt_simulate
from app.debt_diff import debt_diff
from app.fairness import fairness_report
from app.forecast import forecast
from app.models import (
    DebtDiffRequest,
    DebtDiffResponse,
    DebtSimulateRequest,
    DebtSimulateResponse,
    FairnessReportRequest,
    FairnessReportResponse,
    ForecastRequest,
    ForecastResponse,
    PlanInitialBalancesResponse,
    QueueSimulateRequest,
    QueueSimulateResponse,
    ReservationSimulateRequest,
    ReservationSimulateResponse,
    RolloverSimulateRequest,
    RolloverSimulateResponse,
    RouteSimulateRequest,
    RouteSimulateResponse,
    SimulateRequest,
    SimulateResponse,
    UsageReportRequest,
    UsageReportResponse,
    WindowSimulateRequest,
    WindowSimulateResponse,
)
from app.planner import plan_initial_balances
from app.queueing import queue_simulate
from app.reservation import reservation_simulate
from app.rollover import rollover_simulate
from app.routing import route_simulate
from app.simulation import simulate
from app.usage import usage_report
from app.windowing import window_simulate

app = FastAPI(
    title="多层配额原子试算 API",
    description=(
        "接收若干令牌桶与带时间戳的请求序列，按时间顺序模拟连续补充；"
        "仅当请求桶链上所有桶余额充足时原子扣减，否则全部保持不变。"
        "另提供 /forecast：在不扣减状态的前提下，预测桶链首次同时满足成本所需的等待时间；"
        "/plan-initial-balances：以全部请求放行为目标，反推各桶所需的最低初始余额；"
        "/usage-report：按时间窗口统计各桶的放行消耗、拒绝次数与峰值占用；"
        "/fairness-report：按 principal 统计放行消耗与拒绝次数，给出加权 Jain "
        "公平指数、最受限主体及其首个短缺桶（全局与逐窗口）；"
        "以及 /reservation-simulate：按 start 升序对区间预留做容量受限的链上原子试算，"
        "返回逐项结果与每桶合并后的占用区间、峰值并发；"
        "以及 /route-simulate：在共享连续补充令牌桶上为携带多条候选桶链的请求选路——"
        "仅候选链全部桶有余额时可选，按扣减后链上最大占用率最小（并列按候选顺序）"
        "确定选中链并仅扣该链，无候选可行时全部桶原子不变并给出各候选首个不足桶；"
        "以及 /queue-simulate：在 /simulate 的桶与请求字段上为每个请求增加不早于 "
        "timestamp 的 deadline，请求按（到达时间, 输入顺序）排入严格 FIFO 队列，"
        "仅队首可执行；以队首各桶连续补充后的最早可执行时刻、下一次到达与 deadline "
        "推进事件，deadline 前（含）余额充足即原子扣减放行，否则等待至 deadline 标记 "
        "expired 后继续处理后继（成本超过容量或零补充不可达的队首同样等到 deadline）。"
        "以及 /window-simulate：给定多个带 window_seconds 与 limit 的半开滑动窗口策略"
        "（额度按 principal 隔离）与按时间排序、携带 principal/cost/policy_refs 的请求，"
        "判定前移除各策略窗口左端已过期事件，仅当请求引用的全部策略在窗口内累计消耗"
        "加 cost 均不超过 limit 时原子记账，否则不改变任何策略并返回引用顺序上首个超限策略；"
        "响应按原始顺序给出 allowed、窗口边界、剩余额度、拒绝原因与各策略终态占用。"
        "以及 /rollover-simulate：给定多个由 anchor/period_seconds/allowance/carry_cap "
        "定义的周期结转策略与按时间排序、携带 cost/policy_refs 的请求，跨期时旧结转额失效、"
        "上期未用基础额度按 carry_cap 上限结转到新周期，消费先扣即将失效的结转额再扣本期额度，"
        "仅当全部引用策略两类余额合计都足够时原子扣减，否则全部不变并返回首个不足策略；"
        "响应按原始顺序给出周期编号、两类余额扣减前后快照、拒绝原因与各策略终态。"
        "以及 /debt-simulate：桶在 /simulate 字段上增加 debt_limit，余额不足时"
        "允许借入缺口但累计债务不得超过 debt_limit（补充量先偿还债务、剩余才进入"
        "可用余额）；cost > capacity + debt_limit 的请求永远不可达；链上任一桶"
        "无法在借贷上限内满足成本则整笔原子拒绝；响应按原始索引返回 allowed、"
        "每桶 before/after 的 balance 与 debt、rejected_by/reason 及终态快照。"
        "以及 /debt-diff：对 baseline 与 candidate 两组 /debt-simulate 格式的"
        "情景分别独立回放，按原始请求索引对齐，返回首个 allowed/拒绝原因分歧、"
        "逐请求各桶余额/债务差值与终态快照差值；请求数量、请求 ID、时间戳或"
        "桶集合不一致时返回带定位的 422 且不做任何回放。"
    ),
    version="1.0.0",
)


def _sanitize_non_finite(value):
    """递归把 NaN/Infinity 等无法 JSON 编码的浮点替换为字符串标记。

    校验错误会原样回显输入（error["input"]）；JSON 裸 token NaN/Infinity
    在标准 json 解析后即为非有限浮点，不做处理会让 422 响应在渲染阶段
    抛出 ValueError 退化为 500。替换为 "NaN"/"Infinity"/"-Infinity"
    后，422 结构保持 {"detail": [...]} 且可被客户端解析定位。
    """
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, dict):
        return {key: _sanitize_non_finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_non_finite(item) for item in value]
    return value


@app.exception_handler(RequestValidationErrors)
async def cross_validation_exception_handler(request: Request, exc: RequestValidationErrors):
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(_sanitize_non_finite(exc.errors))},
    )


@app.exception_handler(RequestValidationError)
async def pydantic_validation_exception_handler(request: Request, exc: RequestValidationError):
    # jsonable_encoder：校验器抛出的 ValueError 会进入 ctx.error，需转成可序列化形式
    errors = jsonable_encoder(exc.errors())
    return JSONResponse(
        status_code=422, content={"detail": _sanitize_non_finite(errors)}
    )


@app.get("/", tags=["meta"])
def root():
    return {
        "service": app.title,
        "docs": "/docs",
        "health": "/health",
        "simulate": "/simulate",
        "forecast": "/forecast",
        "plan_initial_balances": "/plan-initial-balances",
        "usage_report": "/usage-report",
        "fairness_report": "/fairness-report",
        "reservation_simulate": "/reservation-simulate",
        "route_simulate": "/route-simulate",
        "queue_simulate": "/queue-simulate",
        "window_simulate": "/window-simulate",
        "rollover_simulate": "/rollover-simulate",
        "debt_simulate": "/debt-simulate",
        "debt_diff": "/debt-diff",
    }


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.post("/simulate", response_model=SimulateResponse, tags=["simulation"])
def simulate_endpoint(payload: SimulateRequest) -> SimulateResponse:
    return simulate(payload)


@app.post("/forecast", response_model=ForecastResponse, tags=["forecast"])
def forecast_endpoint(payload: ForecastRequest) -> ForecastResponse:
    return forecast(payload)


@app.post("/plan-initial-balances", response_model=PlanInitialBalancesResponse, tags=["planner"])
def plan_initial_balances_endpoint(payload: SimulateRequest) -> PlanInitialBalancesResponse:
    return plan_initial_balances(payload)


@app.post("/usage-report", response_model=UsageReportResponse, tags=["usage"])
def usage_report_endpoint(payload: UsageReportRequest) -> UsageReportResponse:
    return usage_report(payload)


@app.post("/fairness-report", response_model=FairnessReportResponse, tags=["fairness"])
def fairness_report_endpoint(payload: FairnessReportRequest) -> FairnessReportResponse:
    return fairness_report(payload)


@app.post(
    "/reservation-simulate",
    response_model=ReservationSimulateResponse,
    tags=["reservation"],
)
def reservation_simulate_endpoint(
    payload: ReservationSimulateRequest,
) -> ReservationSimulateResponse:
    return reservation_simulate(payload)


@app.post(
    "/route-simulate",
    response_model=RouteSimulateResponse,
    tags=["routing"],
)
def route_simulate_endpoint(payload: RouteSimulateRequest) -> RouteSimulateResponse:
    return route_simulate(payload)


@app.post(
    "/queue-simulate",
    response_model=QueueSimulateResponse,
    tags=["queue"],
)
def queue_simulate_endpoint(payload: QueueSimulateRequest) -> QueueSimulateResponse:
    return queue_simulate(payload)


@app.post(
    "/window-simulate",
    response_model=WindowSimulateResponse,
    tags=["window"],
)
def window_simulate_endpoint(payload: WindowSimulateRequest) -> WindowSimulateResponse:
    return window_simulate(payload)


@app.post(
    "/rollover-simulate",
    response_model=RolloverSimulateResponse,
    tags=["rollover"],
)
def rollover_simulate_endpoint(payload: RolloverSimulateRequest) -> RolloverSimulateResponse:
    return rollover_simulate(payload)


@app.post(
    "/debt-simulate",
    response_model=DebtSimulateResponse,
    tags=["debt"],
)
def debt_simulate_endpoint(payload: DebtSimulateRequest) -> DebtSimulateResponse:
    return debt_simulate(payload)


@app.post(
    "/debt-diff",
    response_model=DebtDiffResponse,
    tags=["debt"],
)
def debt_diff_endpoint(payload: DebtDiffRequest) -> DebtDiffResponse:
    return debt_diff(payload)
