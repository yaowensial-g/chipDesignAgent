"""FastAPI 应用装配入口。

这里负责中间件、统一异常响应和路由挂载，不放具体业务逻辑。开发时如果某个路由
变得很长，应优先把状态转换拆到 Service，而不是继续扩展 main.py。
"""

import logging
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.config.secret_protection import SecretProtectionError
from app.models.provider import ModelInvocationError


logger = logging.getLogger("chipflow.errors")


app = FastAPI(
    title="Chip Design Multi-Agent API",
    version="0.5.0",
    description="Windows-first role-based chip design multi-agent workbench.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    """为每个请求附加可搜索的 ID，前端错误与后端日志可用同一编号关联。"""
    request_id = request.headers.get("X-Request-ID") or uuid4().hex[:12]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """把未知异常转换成稳定 JSON，避免浏览器只看到无信息的 HTML 500 页面。

    已知模型/密钥问题给出可操作建议；真正未知异常只向用户返回安全概述，详细
    traceback 写入后端日志，避免把本机路径或敏感上下文泄露到前端。
    """
    request_id = getattr(request.state, "request_id", uuid4().hex[:12])
    if isinstance(exc, SecretProtectionError):
        code = "MODEL_KEY_UNREADABLE"
        message = str(exc)
        suggestions = ["使用保存该 Key 的同一 Windows 用户启动服务", "在模型设置中重新输入并保存 API Key"]
    elif isinstance(exc, ModelInvocationError):
        code = "MODEL_INVOCATION_FAILED"
        message = str(exc)
        suggestions = ["运行系统自检", "检查模型名称、Base URL、API Key 和网络连接"]
    elif isinstance(exc, ValueError) and "model settings" in str(exc).lower():
        code = "MODEL_CONFIG_INVALID"
        message = str(exc)
        suggestions = ["检查 data/model_settings.json 是否为有效配置", "在模型设置页面重新保存"]
    else:
        code = "INTERNAL_ERROR"
        message = "后端处理请求时发生未预期错误。"
        suggestions = ["运行系统自检查看具体检查项", "查看后端控制台中相同请求编号的错误日志"]

    logger.exception("request_id=%s code=%s path=%s", request_id, code, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "suggestions": suggestions,
            }
        },
        headers={"X-Request-ID": request_id},
    )


app.include_router(router, prefix="/api/v1")

