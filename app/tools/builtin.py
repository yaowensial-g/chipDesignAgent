"""当前已接入的本地工程 Tool。

Tool executor 接收 Workspace ID 和结构化参数，返回
``(command, exit_code, stdout, stderr)``。执行器绝不能直接接受模型生成的任意
Shell 字符串；所有路径和参数都必须先经过白名单校验，并使用 ``shell=False``。
"""

import json
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from app.domain.enums import AgentRole, Mutability
from app.services.workspace_service import workspace_service
from app.tools.registry import tool_registry
from app.tools.adapters.verilator_adapter import LINT_FLAGS


def verilator_enabled() -> bool:
    return os.getenv("CHIP_AGENT_VERILATOR_ENABLED", "").lower() in {"1", "true"}


def verilator_executable() -> str | None:
    configured = os.getenv("CHIP_AGENT_VERILATOR_PATH")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            return None
        executable = shutil.which(str(path))
    else:
        executable = shutil.which("verilator") or shutil.which("verilator_bin")
    if executable:
        resolved = Path(executable).resolve()
        if os.name != "nt" or resolved.suffix.lower() == ".exe":
            return str(resolved)
    return None


@lru_cache(maxsize=8)
def _probe_verilator(executable: str, size: int, mtime_ns: int, root: str | None, path: str | None) -> str | None:
    try:
        probe = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, shell=False, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    banner = probe.stdout.strip().splitlines()
    return banner[0][:250] if probe.returncode == 0 and banner and banner[0].startswith("Verilator ") else None


def verilator_identity() -> dict | None:
    if not verilator_enabled() or (executable := verilator_executable()) is None:
        return None
    try:
        info = Path(executable).stat()
    except OSError:
        return None
    version = _probe_verilator(executable, info.st_size, info.st_mtime_ns, os.getenv("VERILATOR_ROOT"), os.getenv("PATH"))
    if version is None:
        return None
    return {
        "executable": executable, "version": version, "executable_size": info.st_size,
        "executable_mtime_ns": info.st_mtime_ns, "verilator_root": os.getenv("VERILATOR_ROOT"),
    }


def verilator_available() -> bool:
    return verilator_identity() is not None


def check_lint_source(content: bytes) -> None:
    # Phase C compiles one self-contained source. No unapproved preprocessor includes or macros.
    if b"`" in content:
        raise ValueError("当前真实 Lint 只支持无预处理指令的单文件 RTL。")


def run_verilator_lint(executable: str, files: list[str], cwd: Path, top_module: str) -> tuple[list[str], int, str, str]:
    command = [executable, *LINT_FLAGS, *files]
    if top_module:
        command.extend(["--top-module", top_module])
    try:
        completed = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, shell=False, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return command, completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        return command, 124, stdout, stderr + "\nTOOL_TIMEOUT: Verilator Lint 超时"


def _discover_testcases(workspace_id: str, arguments: dict) -> tuple[list[str], int, str, str]:
    """只读扫描已有验证文件，不生成或修改验证计划。"""
    root_name = str(arguments.get("directory", "dv"))
    root = workspace_service.resolve_path(workspace_id, root_name)
    if not root.exists():
        return [], 0, json.dumps({"testcases": []}, ensure_ascii=False), ""
    extensions = {".sv", ".svh", ".py", ".tcl", ".yaml", ".yml", ".json"}
    testcases = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        lowered = path.name.lower()
        if "test" not in lowered and "case" not in lowered and "regress" not in lowered:
            continue
        testcases.append(
            {
                "id": path.stem,
                "name": path.stem,
                "path": path.relative_to(Path(workspace_service.get(workspace_id).root_path)).as_posix(),
            }
        )
    return [], 0, json.dumps({"testcases": testcases}, ensure_ascii=False), ""


def _verilator_lint(workspace_id: str, arguments: dict) -> tuple[list[str], int, str, str]:
    """构造并执行受控 Verilator Lint 命令。

    文件必须位于 Workspace 内，后缀和 top_module 都经过检查。返回原始输出，
    后续由 ToolRunService 统一落盘并解析为 EngineeringIssue。
    """
    # which 返回绝对可执行文件路径。找不到工具时返回标准化环境错误，
    # 不能用一段假日志伪装成 Lint 已经执行。
    executable = shutil.which("verilator")
    if not executable:
        return ["verilator", "--lint-only"], 127, "", "TOOL_ENVIRONMENT: Verilator 未安装或不在 PATH 中"
    files = arguments.get("files")
    if not isinstance(files, list) or not files:
        return [executable, "--lint-only"], 2, "", "TOOL_ARGUMENT: files 必须是非空数组"
    resolved_files: list[str] = []
    for item in files:
        path = workspace_service.resolve_path(workspace_id, str(item))
        if path.suffix.lower() not in {".sv", ".svh", ".v", ".vh"}:
            return [executable, "--lint-only"], 2, "", f"TOOL_ARGUMENT: 不支持的 HDL 文件 {item}"
        if not path.exists():
            return [executable, "--lint-only"], 2, "", f"TOOL_ARGUMENT: 文件不存在 {item}"
        resolved_files.append(str(path))
    top_module = arguments.get("top_module", "")
    if top_module and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top_module):
        return [executable, "--lint-only"], 2, "", "TOOL_ARGUMENT: top_module 格式非法"
    workspace = workspace_service.get(workspace_id)
    return run_verilator_lint(executable, resolved_files, Path(workspace.root_path), top_module)


def register_builtin_tools() -> None:
    """幂等注册真实工程 Tool；声明权限和审批要求，执行器只负责动作本身。"""
    existing = {tool.name for tool in tool_registry.list(include_task_only=True)}
    if "read_rtl_source" not in existing:
        tool_registry.register(
            name="read_rtl_source",
            description="读取已授权的 Workspace RTL 文件并保留精确字节版本。",
            allowed_roles=[AgentRole.DESIGN, AgentRole.VERIFICATION],
            mutability=Mutability.READ,
            requires_user_approval=False,
            idempotent=True,
            timeout_seconds=15,
            input_schema={
                "type": "object",
                "properties": {"relative_path": {"type": "string", "minLength": 1, "maxLength": 500}},
                "required": ["relative_path"],
                "additionalProperties": False,
            },
            execution_kind="orchestration",
            scope="task",
            capability="read_rtl",
        )
    if "discover_testcases" not in existing:
        tool_registry.register(
            name="discover_testcases",
            description="在 Workspace 的验证目录中发现已有用例，不创建新用例。",
            when_to_use="设计或验证人员要求查找、列出或准备执行已有用例时。",
            when_not_to_use="用户要求创建新验证用例，或当前没有 Workspace 时。",
            allowed_roles=[AgentRole.DESIGN, AgentRole.VERIFICATION],
            mutability=Mutability.READ,
            requires_user_approval=False,
            idempotent=True,
            timeout_seconds=30,
            input_schema={
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "验证目录，默认 dv"}
                },
            },
            output_schema={
                "type": "object",
                "properties": {"testcases": {"type": "array"}},
            },
            preconditions=["存在可访问的 Workspace"],
            side_effect="workspace_read",
            category="verification",
            executor=_discover_testcases,
        )
    if "verilator_lint" not in existing:
        tool_registry.register(
            name="verilator_lint",
            scope="shared",
            capability="real_rtl_lint",
            description="使用 Verilator 对指定 HDL 文件执行受控 Lint。",
            when_to_use="用户明确要求执行 Lint，或设计/验证任务需要用真实工具检查 HDL 时。",
            when_not_to_use="用户只询问 Lint 概念，或尚未明确授权启动本地进程时。",
            allowed_roles=[AgentRole.DESIGN, AgentRole.VERIFICATION],
            mutability=Mutability.EXECUTION,
            requires_user_approval=True,
            idempotent=False,
            timeout_seconds=300,
            input_schema={
                "type": "object",
                "properties": {
                    "files": {"type": "array", "items": {"type": "string"}},
                    "top_module": {"type": "string"},
                },
                "required": ["files"],
            },
            output_schema={
                "type": "object",
                "properties": {
                    "exit_code": {"type": "integer"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                },
            },
            preconditions=["存在可访问的 Workspace", "Verilator 已安装", "用户授权启动本地进程"],
            side_effect="local_process",
            category="common",
            # 保留旧入口的禁用行为，分层入口按部署开关和版本探测单独开放。
            enabled=False,
            task_enabled=verilator_available,
            executor=_verilator_lint,
        )


register_builtin_tools()
