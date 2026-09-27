"""结构化请求校验错误。"""
from __future__ import annotations


class RequestValidationErrors(Exception):
    """跨字段校验收集到的一组错误。

    FastAPI 通过 app 中注册的异常处理器把它渲染成 HTTP 422。
    """

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(str(errors))
