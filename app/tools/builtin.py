import json
import re
import shutil
import subprocess
from pathlib import Path

from app.domain.enums import AgentRole, Mutability
from app.services.workspace_service import workspace_service
from app.tools.registry import tool_registry


def _discover_testcases(workspace_id: str, arguments: dict) -> tuple[list[str], int, str, str]:
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
    command = [executable, "--lint-only", "-Wall", *resolved_files]
    top_module = arguments.get("top_module")
    if top_module:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", str(top_module)):
            return command, 2, "", "TOOL_ARGUMENT: top_module 格式非法"
        command.extend(["--top-module", str(top_module)])
    workspace = workspace_service.get(workspace_id)
    try:
        completed = subprocess.run(
            command,
            cwd=workspace.root_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            shell=False,
            check=False,
        )
        return command, completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        return command, 124, exc.stdout or "", (exc.stderr or "") + "\nTOOL_TIMEOUT: Verilator Lint 超时"


def register_builtin_tools() -> None:
    if tool_registry.list():
        return
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
            "properties": {"directory": {"type": "string", "description": "验证目录，默认 dv"}},
        },
        output_schema={"type": "object", "properties": {"testcases": {"type": "array"}}},
        preconditions=["存在可访问的 Workspace"],
        side_effect="workspace_read",
        executor=_discover_testcases,
    )
    tool_registry.register(
        name="verilator_lint",
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
        executor=_verilator_lint,
    )


register_builtin_tools()
