"""分析域（/usage-report）：按时间窗口统计消耗/拒绝/峰值的请求与响应模型。"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from app.models.common import _is_finite_number
from app.models.simulation.core import SimulateRequest

class UsageReportRequest(SimulateRequest):
    """POST /usage-report 请求体：/simulate 请求体 + 统计窗口宽度。

    桶与请求的字段、跨字段校验（重复桶、未知引用、重复请求 ID）全部沿用
    SimulateRequest。
    """

    window_seconds: Annotated[float, Field(description="统计窗口宽度（秒），必须为正的有限数值")]

    @field_validator("window_seconds", mode="before")
    @classmethod
    def _validate_window_seconds(cls, value):
        if not _is_finite_number(value):
            raise ValueError("window_seconds 必须是有限数值")
        if value <= 0:
            raise ValueError("window_seconds 必须为正数")
        return value


class WindowRange(BaseModel):
    """半开窗口区间 [start, end)。"""

    index: Annotated[int, Field(description="窗口序号（从 0 开始）")]
    start: Annotated[float, Field(description="窗口起点（含）")]
    end: Annotated[float, Field(description="窗口终点（不含）")]


class UsageWindowStats(BaseModel):
    """单个桶在单个窗口内的使用统计。"""

    index: Annotated[int, Field(description="窗口序号")]
    allowed_consumption: Annotated[
        float, Field(description="窗口内放行请求对该桶的消耗合计")
    ]
    rejected_count: Annotated[
        int, Field(description="窗口内引用该桶且被原子拒绝的请求数（无论限制桶是否为该桶）")
    ]
    peak_occupancy: Annotated[
        float, Field(description="窗口内峰值占用 = capacity - 窗口内最低余额")
    ]


class UsageBucketReport(BaseModel):
    """单个桶的逐窗统计与峰值信息。"""

    id: str
    allowed_consumption: Annotated[float, Field(description="全部窗口放行消耗合计")]
    rejected_count: Annotated[int, Field(description="全部窗口拒绝次数合计")]
    peak_occupancy: Annotated[float, Field(description="全部窗口中的最高峰值占用")]
    peak_window: Annotated[
        WindowRange, Field(description="达到峰值占用的窗口起止时间（并列取最早窗口）")
    ]
    windows: Annotated[
        list[UsageWindowStats], Field(description="逐窗口统计，按窗口序号升序")
    ]


class BusiestWindow(WindowRange):
    """全局最忙窗口：跨桶放行消耗合计最大的窗口（并列取最早）。"""

    allowed_consumption: Annotated[float, Field(description="该窗口全部桶放行消耗之和")]
    allowed_count: Annotated[int, Field(description="该窗口放行的请求数")]
    rejected_count: Annotated[int, Field(description="该窗口被拒的请求数")]


class UsageReportResponse(BaseModel):
    window_seconds: Annotated[float, Field(description="统计窗口宽度（秒）")]
    window_count: Annotated[int, Field(description="覆盖请求时间跨度的窗口总数")]
    windows: Annotated[
        list[WindowRange], Field(description="全部窗口的起止时间，按窗口序号升序")
    ]
    buckets: Annotated[
        list[UsageBucketReport], Field(description="各桶统计，按 buckets 输入顺序")
    ]
    busiest_window: Annotated[BusiestWindow, Field(description="全局最忙窗口")]


