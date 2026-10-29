import os
import shutil
from datetime import datetime, timezone

from app.config.model_settings import (
    DEFAULT_CONFIG_PATH,
    SAVED_CONFIG_PATH,
    model_settings_service,
)
from app.domain.models import DiagnosticCheck, DiagnosticReport
from app.storage.database import database
from app.tools.registry import tool_registry


class DiagnosticsService:
    """Runs local, non-billable checks for the web application."""

    def run(self) -> DiagnosticReport:
        checks: list[DiagnosticCheck] = [
            DiagnosticCheck(
                check_id="backend_api",
                label="后端 API",
                status="pass",
                detail="FastAPI 服务可以响应自检请求。",
            )
        ]

        try:
            defaults = model_settings_service._load_defaults()
            required = {"provider", "api_style", "model", "base_url"}
            missing = sorted(required - defaults.keys())
            if missing:
                raise ValueError(f"缺少字段：{', '.join(missing)}")
            checks.append(
                DiagnosticCheck(
                    check_id="default_config",
                    label="默认模型配置",
                    status="pass",
                    detail=f"已读取 {DEFAULT_CONFIG_PATH.name}。",
                )
            )
        except Exception as exc:
            checks.append(
                DiagnosticCheck(
                    check_id="default_config",
                    label="默认模型配置",
                    status="error",
                    detail=f"默认配置无法读取：{exc}",
                    action="检查 config/model.toml 是否存在且字段完整。",
                )
            )

        try:
            model_settings_service._load_saved()
            checks.append(
                DiagnosticCheck(
                    check_id="saved_config",
                    label="本地模型设置",
                    status="pass",
                    detail=(
                        "本地覆盖配置格式正确。"
                        if SAVED_CONFIG_PATH.exists()
                        else "尚未生成本地覆盖配置，将使用默认设置。"
                    ),
                )
            )
        except Exception as exc:
            checks.append(
                DiagnosticCheck(
                    check_id="saved_config",
                    label="本地模型设置",
                    status="error",
                    detail=f"本地配置无法读取：{exc}",
                    action="在模型设置页面重新保存；必要时先备份损坏的 data/model_settings.json。",
                )
            )

        try:
            settings = model_settings_service.effective()
            if settings.enabled and settings.api_key:
                status = "pass"
                detail = f"真实模型已就绪：{settings.provider} / {settings.model}。未发送测试请求。"
                action = None
            elif settings.enabled:
                status = "warning"
                detail = "真实模型已启用，但当前提供商没有可用 API Key。"
                action = "打开模型设置，填写并保存当前提供商的 API Key。"
            else:
                status = "warning"
                detail = "真实模型未启用，系统将使用确定性模拟运行时。"
                action = "如需开放式问答和真实 Agent 推理，请在模型设置中启用真实模型。"
            checks.append(
                DiagnosticCheck(
                    check_id="model_runtime",
                    label="模型运行时",
                    status=status,
                    detail=detail,
                    action=action,
                )
            )
        except Exception as exc:
            checks.append(
                DiagnosticCheck(
                    check_id="model_runtime",
                    label="模型配置与密钥",
                    status="error",
                    detail=f"无法加载模型设置或解密 API Key：{exc}",
                    action="使用保存该 Key 的同一 Windows 用户运行程序，或重新输入 API Key。",
                )
            )

        data_directory = SAVED_CONFIG_PATH.parent
        writable = data_directory.exists() and os.access(data_directory, os.W_OK)
        checks.append(
            DiagnosticCheck(
                check_id="data_directory",
                label="本地数据目录",
                status="pass" if writable else "error",
                detail=(
                    f"目录可写：{data_directory}"
                    if writable
                    else f"目录不存在或不可写：{data_directory}"
                ),
                action=None if writable else "检查目录是否存在以及当前 Windows 用户的写权限。",
            )
        )

        try:
            database.fetch_one("SELECT 1 AS ok")
            checks.append(
                DiagnosticCheck(
                    check_id="sqlite_store",
                    label="任务与工程数据存储",
                    status="pass",
                    detail=f"SQLite 可读写：{database.path.name}。",
                )
            )
        except Exception as exc:
            checks.append(
                DiagnosticCheck(
                    check_id="sqlite_store",
                    label="任务与工程数据存储",
                    status="error",
                    detail=f"SQLite 不可用：{exc}",
                    action="检查 data 目录权限，并确认数据库文件未被独占锁定。",
                )
            )

        tool_names = [tool.name for tool in tool_registry.list()]
        checks.append(
            DiagnosticCheck(
                check_id="tool_registry",
                label="工程 Tool 注册表",
                status="pass" if tool_names else "error",
                detail=f"已注册 {len(tool_names)} 个 Tool：{', '.join(tool_names) or '无'}。",
                action=None if tool_names else "检查 app/tools/builtin.py 的注册过程。",
            )
        )
        verilator = shutil.which("verilator")
        checks.append(
            DiagnosticCheck(
                check_id="verilator",
                label="Verilator Lint 环境",
                status="pass" if verilator else "warning",
                detail=f"可执行文件：{verilator}" if verilator else "当前 PATH 中未发现 Verilator。",
                action=None if verilator else "安装 Verilator 并重启后端；未安装时系统会返回明确的环境 Issue。",
            )
        )

        statuses = {check.status for check in checks}
        overall = "error" if "error" in statuses else ("warning" if "warning" in statuses else "ok")
        return DiagnosticReport(
            status=overall,
            generated_at=datetime.now(timezone.utc).isoformat(),
            version="0.5.0",
            checks=checks,
        )


diagnostics_service = DiagnosticsService()
