"""跨领域共享的基础模型与工具：数值常量、有限数校验、通用余额视图。"""
from __future__ import annotations

import math
from typing import Annotated

from pydantic import BaseModel, Field

# Floating point tolerance for balance comparisons.
EPS = 1e-9


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _coerce_finite_number(value) -> float | None:
    """把宽松数值输入归一化为有限 float；无法得到有限数值时返回 None。

    覆盖两类非法输入：JSON 裸 token（NaN/Infinity/-Infinity 会被标准
    解析器读成 float）以及数字字符串转换后的非有限值（"NaN"、
    "Infinity"、"1e999" 等）。有限的数字字符串（如 "10"）保持与
    pydantic 默认 float 宽松转换一致的行为，归一化为 float。
    """
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except (ValueError, OverflowError):
            return None
    if not _is_finite_number(value):
        return None
    return float(value)


class BucketBalance(BaseModel):
    """链上单个桶在某次扣减判定时的余额视图。"""

    before: Annotated[float, Field(description="判定时刻（补充之后、扣减之前）的余额")]
    after: Annotated[float, Field(description="扣减之后的余额；被拒绝时与 before 相同")]


