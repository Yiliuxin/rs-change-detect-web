# -*- coding: utf-8 -*-
"""统一错误封装（HLD 2.2.3 / 第 5 章 EX1–EX11、SYS-01–05）。

- :class:`ApiError` 由 service 层显式抛出，携带 code / run_id；
- :func:`install` 在 ``main.py`` 注册异常处理器，统一输出 ``{code,message,run_id}``；
- 文案逐字取自 ``constants.ERROR_MESSAGES``，不臆造；
- HTTP 映射按 HLD 表 5-2：EX2→404，EX1/EX3/EX4/EX6/EX8/EX9/EX10/EX11→400，
  EX7→429，SYS-*→500（EX5 为告警档，不走错误体）。
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config.constants import ERROR_MESSAGES

logger = logging.getLogger(__name__)

# HLD 表 5-2 错误码 → HTTP 状态码（EX5 为告警，仍登记但不作错误抛出）
CODE_HTTP = {
    "EX1": 400, "EX2": 404, "EX3": 400, "EX4": 400, "EX5": 400,
    "EX6": 400, "EX7": 429, "EX8": 400, "EX9": 400, "EX10": 400,
    "EX11": 400,
    "SYS-01": 500, "SYS-02": 500, "SYS-03": 500, "SYS-04": 500, "SYS-05": 500,
}

__all__ = ["ApiError", "CODE_HTTP", "as_api_error", "install"]


class ApiError(Exception):
    """业务/系统错误：``{code, message, run_id}``。"""

    def __init__(self, code: str, run_id: Optional[str] = None,
                 http_status: Optional[int] = None, detail: Optional[str] = None):
        self.code = code
        # 逐字取 ERROR_MESSAGES，不臆造中文提示
        self.message = ERROR_MESSAGES.get(code, code)
        self.run_id = run_id
        self.http_status = http_status if http_status is not None else CODE_HTTP.get(code, 400)
        self.detail = detail
        super().__init__(detail or self.message)


def as_api_error(exc: Exception, run_id: Optional[str] = None) -> ApiError:
    """把任意异常归一为 :class:`ApiError`（HLD 表 5-2 全覆盖）。

    - 算法层带 ``.code`` 的异常（AoiTooLargeError / GridMismatchError /
      FormulaMixedError / BaselineUndeterminableError / SampleInsufficientError /
      ExportFormatError）按码直接映射；
    - psycopg2 连接/查询异常 → SYS-04；内存 → SYS-02；超时 → SYS-05；
    - 其余兜底 SYS-01（COG/栅格读取等内建失败）。
    """
    if isinstance(exc, ApiError):
        if run_id is not None and exc.run_id is None:
            exc.run_id = run_id
        return exc

    code = getattr(exc, "code", None)
    if code in ERROR_MESSAGES:
        return ApiError(code, run_id=run_id, detail=str(exc))

    module = type(exc).__module__ or ""
    if module.startswith("psycopg2"):
        return ApiError("SYS-04", run_id=run_id, detail=str(exc))
    if isinstance(exc, MemoryError):
        return ApiError("SYS-02", run_id=run_id, detail=str(exc))
    if isinstance(exc, (TimeoutError,)):
        return ApiError("SYS-05", run_id=run_id, detail=str(exc))
    return ApiError("SYS-01", run_id=run_id, detail=str(exc))


def install(app: FastAPI) -> None:
    """注册统一异常处理器（仅此一处，路由保持薄）。"""

    @app.exception_handler(ApiError)
    def _api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": exc.code, "message": exc.message, "run_id": exc.run_id},
        )

    @app.exception_handler(Exception)
    def _generic_handler(_request: Request, exc: Exception) -> JSONResponse:
        # 兜底：路由/service 未显式转换的异常，也统一成 {code,message,run_id}
        mapped = as_api_error(exc)
        logger.exception("未显式映射异常，兜底 %s：%s", mapped.code, mapped.detail)
        return JSONResponse(
            status_code=mapped.http_status,
            content={"code": mapped.code, "message": mapped.message, "run_id": mapped.run_id},
        )
