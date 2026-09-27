"""分析域（/fairness-report）：主体公平性请求/响应模型。

桶与请求的跨字段校验沿用基础模拟域的同一套规则。
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import RequestValidationErrors
from app.models.common import _is_finite_number
from app.models.analysis.usage import WindowRange
from app.models.simulation.core import (
    BucketSpec,
    _simulate_cross_validation_errors,
)

class FairnessSimRequest(BaseModel):
    """公平性试算请求：在 /simulate 请求字段上增加 principal 与 weight。"""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1, description="请求唯一标识")]
    timestamp: Annotated[float, Field(description="请求时间（秒，任意一致的时间原点）")]
    cost: Annotated[float, Field(gt=0, description="消耗量（从链上每个桶扣减的令牌数）")]
    chain: Annotated[list[str], Field(min_length=1, description="桶链：按顺序引用的桶 ID")]
    principal: Annotated[
        str, Field(min_length=1, description="请求归属主体（租户/用户/调用方 ID）")
    ]
    weight: Annotated[
        float,
        Field(default=1.0, description="主体权重，必须为正的有限数值（允许 1e-10 等微小值，按真实数值参与计算）；缺省为 1.0"),
    ] = 1.0

    @field_validator("timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value):
        if not _is_finite_number(value):
            raise ValueError("timestamp 必须是有限数值")
        return value

    @field_validator("cost", mode="before")
    @classmethod
    def _validate_cost(cls, value):
        if not _is_finite_number(value):
            raise ValueError("cost 必须是有限数值")
        if value <= 0:
            raise ValueError("cost 必须为正数")
        return value

    @field_validator("principal", mode="before")
    @classmethod
    def _validate_principal(cls, value):
        if not isinstance(value, str) or not value:
            raise ValueError("principal 必须是非空字符串")
        return value

    @field_validator("weight", mode="before")
    @classmethod
    def _validate_weight(cls, value):
        if not _is_finite_number(value):
            raise ValueError("weight 必须是有限数值")
        if value <= 0:
            raise ValueError("weight 必须为正数")
        return value

    @field_validator("chain")
    @classmethod
    def _validate_chain_shape(cls, value: list[str]):
        if not value:
            raise ValueError("chain 至少需要引用一个桶")
        seen: set[str] = set()
        for bucket_id in value:
            if not isinstance(bucket_id, str) or not bucket_id:
                raise ValueError("chain 中的桶 ID 必须是非空字符串")
            if bucket_id in seen:
                raise ValueError(f"chain 中存在重复桶引用: {bucket_id}")
            seen.add(bucket_id)
        return value


class FairnessReportRequest(BaseModel):
    """POST /fairness-report 请求体：带 principal/weight 的请求序列 + 统计窗口宽度。

    桶与请求的字段、跨字段校验（重复桶、未知引用、重复请求 ID）沿用
    SimulateRequest 的同一套规则。
    """

    model_config = ConfigDict(extra="forbid")

    buckets: Annotated[list[BucketSpec], Field(min_length=1, description="令牌桶集合")]
    requests: Annotated[
        list[FairnessSimRequest], Field(min_length=1, description="按时间戳模拟的请求序列")
    ]
    window_seconds: Annotated[float, Field(description="统计窗口宽度（秒），必须为正的有限数值")]

    @field_validator("window_seconds", mode="before")
    @classmethod
    def _validate_window_seconds(cls, value):
        if not _is_finite_number(value):
            raise ValueError("window_seconds 必须是有限数值")
        if value <= 0:
            raise ValueError("window_seconds 必须为正数")
        return value

    @model_validator(mode="after")
    def _cross_validate(self):
        errors = _simulate_cross_validation_errors(self.buckets, self.requests)

        # 同一 principal 的权重必须一致：加权 Jain 指数以主体为单位计算。
        # 按 float 的真实数值精确比较——固定绝对容差会把 1e-10 与 2e-10
        # 这类相差一倍但都远小于容差的合法微小权重误判为一致。
        principal_weight: dict[str, float] = {}
        for r_index, request in enumerate(self.requests):
            current = float(request.weight)
            previous = principal_weight.get(request.principal)
            if previous is None:
                principal_weight[request.principal] = current
            elif previous != current:
                errors.append(
                    {
                        "type": "inconsistent_principal_weight",
                        "loc": ("requests", r_index, "weight"),
                        "msg": (
                            f"principal {request.principal!r} 的权重不一致："
                            f"{previous} 与 {current}"
                        ),
                        "input": request.weight,
                    }
                )

        if errors:
            raise RequestValidationErrors(errors)
        return self


class FairnessPrincipalRow(BaseModel):
    """单个主体在某个统计范围（全局或单个窗口）内的公平性数据。"""

    principal: Annotated[str, Field(description="主体标识")]
    weight: Annotated[float, Field(description="主体权重（按请求中的真实数值原样返回，含微小正权重）")]
    request_count: Annotated[int, Field(description="该范围内该主体的请求总数")]
    allowed_count: Annotated[int, Field(description="放行请求数")]
    rejected_count: Annotated[int, Field(description="被原子拒绝的请求数")]
    allowed_consumption: Annotated[
        float, Field(description="放行请求的 cost 合计（该主体的实际获得量）")
    ]
    theoretical_share: Annotated[
        float,
        Field(description="理论权重份额 = 该范围全部放行消耗 × weight / 权重合计"),
    ]
    deficit: Annotated[
        float, Field(description="短缺量 = max(0, theoretical_share - allowed_consumption)")
    ]
    deficit_ratio: Annotated[
        float,
        Field(description="短缺比例 = deficit / theoretical_share；理论份额为 0 时为 0"),
    ]


class FirstShortfall(BaseModel):
    """最受限主体的短缺证据：按处理顺序首个造成其短缺的拒绝。"""

    request_id: Annotated[str, Field(description="被拒绝的请求 ID")]
    index: Annotated[int, Field(description="该请求在原始 requests 数组中的下标")]
    timestamp: Annotated[float, Field(description="该请求的时间戳")]
    bucket_id: Annotated[str, Field(description="导致原子拒绝的第一个余额不足的桶 ID")]
    cost: Annotated[float, Field(description="该请求的消耗量")]
    available: Annotated[
        float, Field(description="判定时刻（补充之后、扣减之前）限制桶的余额")
    ]


class FairnessScopeReport(BaseModel):
    """一个统计范围（全局或单个窗口）的公平性报告。"""

    principal_count: Annotated[int, Field(description="参与该范围统计的主体数")]
    request_count: Annotated[int, Field(description="该范围内的请求总数")]
    allowed_count: Annotated[int, Field(description="放行请求总数")]
    rejected_count: Annotated[int, Field(description="拒绝请求总数")]
    allowed_consumption: Annotated[
        float, Field(description="全部主体放行消耗（实际获得量）合计")
    ]
    weighted_jain_index: Annotated[
        float,
        Field(
            description=(
                "加权 Jain 公平指数 = (Σ x)² / (Σ w · Σ x²/w)，"
                "x 为主体实际获得量、w 为未舍入的权重；取值 (0, 1]，"
                "越大越公平；仅当实际获得量合计为 0 时定义为 0"
            )
        ),
    ]
    most_constrained_principal: Annotated[
        str | None,
        Field(
            description=(
                "短缺比例最高且有被拒请求的主体（并列取首次拒绝最早者）；"
                "不存在有短缺的被拒主体时为 null"
            )
        ),
    ]
    first_shortfall: Annotated[
        FirstShortfall | None,
        Field(
            description="最受限主体按处理顺序的首个短缺拒绝；无最受限主体时为 null"
        ),
    ]
    principals: Annotated[
        list[FairnessPrincipalRow],
        Field(description="各主体统计，按主体首次请求的处理顺序"),
    ]


class FairnessWindowReport(FairnessScopeReport):
    """单个窗口的公平性报告：范围数据 + 窗口起止。"""

    index: Annotated[int, Field(description="窗口序号")]
    start: Annotated[float, Field(description="窗口起点（含）")]
    end: Annotated[float, Field(description="窗口终点（不含）")]


class FairnessReportResponse(BaseModel):
    window_seconds: Annotated[float, Field(description="统计窗口宽度（秒）")]
    window_count: Annotated[int, Field(description="覆盖请求时间跨度的窗口总数")]
    windows: Annotated[
        list[WindowRange], Field(description="全部窗口的起止时间，按窗口序号升序")
    ]
    global_report: Annotated[FairnessScopeReport, Field(description="跨全部窗口的全局报告")]
    window_reports: Annotated[
        list[FairnessWindowReport], Field(description="逐窗口报告，按窗口序号升序")
    ]


