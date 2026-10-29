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
    request_id = request.headers.get("X-Request-ID") or uuid4().hex[:12]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
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

