"""工程 Tool 的异步执行、日志落盘和 Issue 队列。

顶层 Run 表示用户任务；ToolRun 表示一次真实进程执行。二者分开后，同一个用户任务
可以关联工具命令、退出码和原始日志，而不会把进程细节塞进通用 Run 状态机。
"""

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
import os
import shutil
from threading import RLock
from uuid import uuid4

from app.domain.enums import EngineeringIssueStatus, ToolRunStatus
from app.domain.models import (
    EngineeringIssueView,
    IssueDecisionRequest,
    ToolRunRequest,
    ToolRunView,
)
from app.services.workspace_service import WorkspaceService, utc_now, workspace_service
from app.storage.database import Database, database
from app.storage.orchestration_fence import frozen_approval_is_current
from app.tools.builtin import check_lint_source, register_builtin_tools, run_verilator_lint, verilator_identity
from app.tools.registry import ToolRegistry, tool_registry
from app.tools.adapters.base import EDAToolResult
from app.tools.adapters import verilator_adapter
from app.tools.project_manifest import FrozenProject


class ToolRunService:
    """校验调用权限，在后台执行 Tool，并把日志标准化为逐条 Issue。"""
    def __init__(
        self,
        db: Database = database,
        registry: ToolRegistry = tool_registry,
        workspaces: WorkspaceService = workspace_service,
    ) -> None:
        self.db = db
        self.registry = registry
        self.workspaces = workspaces
        self._frozen_lock = RLock()
        self._frozen_workers: set[str] = set()
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chipflow-tool")
        if registry is tool_registry:
            register_builtin_tools()

    def start(self, request: ToolRunRequest) -> ToolRunView:
        """校验 Workspace、角色、审批和 executor，然后创建 queued ToolRun。"""
        # resolve_path(".") 同时验证 Workspace 存在且根路径仍在受控目录中。
        self.workspaces.resolve_path(request.workspace_id, ".")
        registered = self.registry.authorize(request.tool_name, request.role)
        if registered.definition.requires_user_approval and not request.user_approved:
            raise PermissionError("Tool requires explicit user approval")
        if registered.executor is None:
            raise ValueError(f"Tool {request.tool_name} is orchestrator-managed and cannot run as a local process")
        self.registry.validate_arguments(request.tool_name, request.arguments)
        run_id = str(uuid4())
        now = utc_now()
        self.db.execute(
            "INSERT INTO tool_runs VALUES (?, ?, ?, ?, ?, ?, NULL, '', '', ?, ?)",
            (
                run_id,
                request.workspace_id,
                request.tool_name,
                ToolRunStatus.QUEUED.value,
                self.db.dumps(request.arguments),
                self.db.dumps([]),
                now,
                now,
            ),
        )
        # 进程执行放在线程池，HTTP 请求可以立即返回 run_id 给前端轮询。
        self.executor.submit(self._execute, run_id, request)
        return self.get(run_id)  # type: ignore[return-value]

    def _execute(self, run_id: str, request: ToolRunRequest) -> None:
        """后台执行器：运行 Tool、截断数据库日志、保存原始日志并解析 Issue。"""
        self._set_status(run_id, ToolRunStatus.RUNNING)
        try:
            registered = self.registry.authorize(request.tool_name, request.role)
            if registered.executor is None:
                raise ValueError(f"Tool {request.tool_name} has no engineering executor")
            command, exit_code, stdout, stderr = registered.executor(
                request.workspace_id, request.arguments
            )
        except Exception as exc:
            command, exit_code, stdout, stderr = [], 1, "", f"TOOL_INTERNAL: {exc}"
        self._save_execution(run_id, request.workspace_id, request.tool_name, command, exit_code, stdout, stderr)

    def frozen_tool_link(self, action_id: str) -> dict | None:
        return self.db.fetch_one("SELECT * FROM orchestration_tool_runs WHERE action_id=?", (action_id,))

    def start_frozen_lint(self, action_id: str, request: ToolRunRequest, target: dict, content: bytes) -> ToolRunView:
        """Register the linked job before launching it, and reuse the job on replay."""
        registered = self.registry.authorize(request.tool_name, request.role, task=True)
        if request.tool_name != "verilator_lint" or not registered.definition.requires_user_approval or not request.user_approved:
            raise PermissionError("Frozen Lint requires an explicitly approved Verilator action")
        self.registry.validate_arguments(request.tool_name, request.arguments)
        if request.arguments != {"files": [target["relative_path"]], "top_module": target["top_module"]}:
            raise ValueError("Lint arguments do not match the approved target")
        self.workspaces.resolve_path(request.workspace_id, ".")
        if hashlib.sha256(content).hexdigest() != target["content_hash"]:
            raise ValueError("Lint snapshot hash mismatch")
        return self._start_frozen(action_id, request, target, self._execute_frozen_lint, content)

    def start_frozen_eda(
        self, action_id: str, request: ToolRunRequest, target: dict, project: FrozenProject,
    ) -> ToolRunView:
        registered = self.registry.authorize(request.tool_name, request.role, task=True)
        if (
            request.tool_name not in verilator_adapter.VERILATOR_TOOLS
            or not registered.definition.requires_user_approval or not request.user_approved
        ):
            raise PermissionError("Frozen EDA requires approval")
        self.registry.validate_arguments(request.tool_name, request.arguments)
        self.workspaces.resolve_path(request.workspace_id, ".")
        if project.input_hash != target["content_hash"] or request.arguments != target["arguments"]:
            raise ValueError("EDA inputs do not match the approved target")
        return self._start_frozen(action_id, request, target, self._execute_frozen_eda, project)

    def _start_frozen(self, action_id, request, target, execute, inputs) -> ToolRunView:
        serialized = self.db.dumps(target)
        with self._frozen_lock:
            with self.db.transaction() as conn:
                if not frozen_approval_is_current(conn, action_id, target, request.arguments):
                    raise PermissionError("The frozen ToolRun no longer has an active approved action")
                existing = conn.execute(
                    "SELECT tool_run_id, target_json FROM orchestration_tool_runs WHERE action_id=?", (action_id,),
                ).fetchone()
                if existing is None:
                    run_id = str(uuid4())
                    timestamp = utc_now()
                    conn.execute(
                        "INSERT INTO tool_runs VALUES (?, ?, ?, 'queued', ?, '[]', NULL, '', '', ?, ?)",
                        (run_id, request.workspace_id, request.tool_name, self.db.dumps(request.arguments), timestamp, timestamp),
                    )
                    conn.execute(
                        "INSERT INTO orchestration_tool_runs VALUES (?, ?, ?)", (action_id, run_id, serialized),
                    )
                else:
                    run_id = existing["tool_run_id"]
                    if existing["target_json"] != serialized:
                        raise ValueError("Existing ToolRun has a different approved target")
            run = self.get(run_id)
            if run.status == ToolRunStatus.QUEUED and run_id not in self._frozen_workers:
                self._frozen_workers.add(run_id)
                try:
                    future = self.executor.submit(execute, run_id, request, target, inputs)
                    future.add_done_callback(lambda _: self._forget_worker(run_id))
                except RuntimeError:
                    self._frozen_workers.discard(run_id)
                    raise
            return self.get(run_id)

    def eda_result(self, run_id: str) -> EDAToolResult | None:
        row = self.db.fetch_one("SELECT result_json FROM eda_tool_results WHERE tool_run_id=?", (run_id,))
        return EDAToolResult.model_validate_json(row["result_json"]) if row else None

    def eda_artifact(self, run_id: str, name: str) -> bytes:
        row = self.db.fetch_one(
            "SELECT content_hash, content FROM eda_run_artifacts WHERE tool_run_id=? AND name=?", (run_id, name),
        )
        if row is None or hashlib.sha256(row["content"]).hexdigest() != row["content_hash"]:
            raise KeyError(name)
        return row["content"]

    def _execute_frozen_eda(
        self, run_id: str, request: ToolRunRequest, target: dict, project: FrozenProject,
    ) -> None:
        if not self._claim_frozen(run_id):
            return
        try:
            self.registry.authorize(request.tool_name, request.role, task=True)
            identity = verilator_identity()
            if identity is None or any(target[key] != value for key, value in identity.items()):
                raise ValueError("Approved Verilator executable is unavailable")
            if request.tool_name in verilator_adapter.SIMULATION_TOOLS and (
                target["simulation_environment"] != verilator_adapter.simulation_environment(identity["executable"])
            ):
                raise ValueError("Approved simulation toolchain changed")
            # The child only receives the frozen inputs and its own output directory.
            scratch = self.db.path.parent / f"eda-{run_id}"
            scratch.mkdir(mode=0o777 if os.name == "nt" else 0o700)
            try:
                result, stdout, stderr, files = verilator_adapter.execute(
                    request.tool_name, identity["executable"], identity["version"], project,
                    tuple(request.arguments.get("testcases", ())), request.arguments.get("seed", 1), scratch,
                    coverage=request.arguments.get("coverage", False),
                )
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
        except (OSError, ValueError, KeyError, PermissionError) as error:
            result = EDAToolResult(
                tool_name=request.tool_name, tool_version=target["version"], input_hash=project.input_hash,
                status="ERROR", exit_code=None,
            )
            stdout, stderr, files = "", str(error), {}
        result.stdout_path = f"logs/{run_id}.stdout.log"
        result.stderr_path = f"logs/{run_id}.stderr.log"
        result.issues = parse_tool_issues(request.tool_name, stdout + "\n" + stderr)
        with self.db.transaction() as conn:
            for name, content in files.items():
                digest = hashlib.sha256(content).hexdigest()
                conn.execute("INSERT INTO eda_run_artifacts VALUES (?, ?, ?, ?)", (run_id, name, digest, content))
                result.artifacts.append({"name": name, "sha256": digest, "bytes": len(content)})
            conn.execute("INSERT INTO eda_tool_results VALUES (?, ?)", (run_id, result.model_dump_json()))
        status_code = 0 if result.status == "PASS" else 124 if result.status == "TIMEOUT" else (result.exit_code or 1)
        self._save_execution(
            run_id, request.workspace_id, request.tool_name, result.commands[0] if result.commands else [],
            status_code, stdout, stderr,
        )

    def reconcile_frozen_tool(self, run_id: str) -> ToolRunView:
        """Reconcile a job after restart. A running process is never relaunched."""
        with self._frozen_lock:
            if run_id not in self._frozen_workers:
                self.db.execute(
                    """UPDATE tool_runs SET status='failed', updated_at=?,
                       stderr=stderr || ? WHERE run_id=? AND status IN ('running', 'parsing')""",
                    (utc_now(), "\nTOOL_INTERRUPTED: External EDA result is unknown after restart.", run_id),
                )
            run = self.get(run_id)
            if run is None:
                raise KeyError(run_id)
            return run

    def cancel_queued_tool(self, run_id: str) -> None:
        with self._frozen_lock:
            self.db.execute(
                """UPDATE tool_runs SET status='cancelled', updated_at=?, stderr=?
                   WHERE run_id=? AND status='queued'""",
                (utc_now(), "TOOL_APPROVAL: Approved EDA target changed before launch.", run_id),
            )

    def _forget_worker(self, run_id: str) -> None:
        with self._frozen_lock:
            self._frozen_workers.discard(run_id)

    def _execute_frozen_lint(self, run_id: str, request: ToolRunRequest, target: dict, content: bytes) -> None:
        if not self._claim_frozen(run_id):
            return
        try:
            self.registry.authorize(request.tool_name, request.role, task=True)
            identity = verilator_identity()
            if identity is None or any(target[key] != value for key, value in identity.items()):
                raise ValueError("Approved Verilator executable is unavailable")
            check_lint_source(content)
            directory = self.db.path.parent / "lint-inputs" / run_id
            directory.mkdir(parents=True)
            filename = PurePosixPath(target["relative_path"].replace("\\", "/")).name
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:v|sv)", filename, re.I):
                raise ValueError("Invalid frozen RTL filename")
            source = directory / filename
            try:
                with source.open("xb") as handle:
                    handle.write(content)
                command, code, stdout, stderr = run_verilator_lint(
                    target["executable"], [filename], directory, target["top_module"],
                )
            finally:
                source.unlink(missing_ok=True)
                try:
                    directory.rmdir()
                except OSError:
                    pass
        except Exception as exc:
            command, code, stdout, stderr = [], 1, "", f"TOOL_INTERNAL: {exc}"
        self._save_execution(run_id, request.workspace_id, request.tool_name, command, code, stdout, stderr)

    def _claim_frozen(self, run_id: str) -> bool:
        with self.db.transaction() as conn:
            row = conn.execute(
                """SELECT l.action_id,l.target_json,t.arguments_json FROM orchestration_tool_runs l
                   JOIN tool_runs t ON t.run_id=l.tool_run_id WHERE t.run_id=? AND t.status='queued'""", (run_id,),
            ).fetchone()
            if row is None:
                return False
            if not frozen_approval_is_current(conn, row["action_id"], json.loads(row["target_json"]), json.loads(row["arguments_json"])):
                conn.execute("UPDATE tool_runs SET status='cancelled', updated_at=?, stderr=? WHERE run_id=?",
                             (utc_now(), "TOOL_APPROVAL: Frozen action is no longer current.", run_id))
                return False
            claimed = conn.execute(
                "UPDATE tool_runs SET status='running', updated_at=? WHERE run_id=? AND status='queued'",
                (utc_now(), run_id),
            ).rowcount
        return bool(claimed)

    def _save_execution(
        self, run_id: str, workspace_id: str, name: str, command: list[str],
        exit_code: int, stdout: str, stderr: str,
    ) -> None:
        status = (
            ToolRunStatus.COMPLETED if exit_code == 0 else
            ToolRunStatus.TIMED_OUT if exit_code == 124 else ToolRunStatus.FAILED
        )
        # 先进入 parsing，表示外部进程已经结束但 Issue 尚未完全生成。
        # 数据库只保留末尾 1MB，完整原始输出另外写入 Workspace/logs。
        self.db.execute(
            "UPDATE tool_runs SET status = ?, command_json = ?, exit_code = ?, stdout = ?, stderr = ?, updated_at = ? WHERE run_id = ?",
            (
                ToolRunStatus.PARSING.value,
                self.db.dumps(command),
                exit_code,
                stdout[-1_000_000:],
                stderr[-1_000_000:],
                utc_now(),
                run_id,
            ),
        )
        try:
            self.workspaces.write_text(workspace_id, f"logs/{run_id}.stdout.log", stdout)
            self.workspaces.write_text(workspace_id, f"logs/{run_id}.stderr.log", stderr)
            self._store_issues(
                run_id, name, stdout + "\n" + stderr,
                f"logs/{run_id}.stderr.log" if stderr else f"logs/{run_id}.stdout.log",
            )
        except (OSError, ValueError, KeyError):
            self.db.execute(
                "UPDATE tool_runs SET stderr=stderr || ? WHERE run_id=?",
                ("\nTOOL_LOGS: 无法完成日志或问题落盘。", run_id),
            )
            status = ToolRunStatus.FAILED
        self._set_status(run_id, status)

    def _set_status(self, run_id: str, status: ToolRunStatus) -> None:
        self.db.execute(
            "UPDATE tool_runs SET status = ?, updated_at = ? WHERE run_id = ?",
            (status.value, utc_now(), run_id),
        )

    def get(self, run_id: str) -> ToolRunView | None:
        row = self.db.fetch_one("SELECT * FROM tool_runs WHERE run_id = ?", (run_id,))
        if not row:
            return None
        return ToolRunView(
            run_id=row["run_id"],
            workspace_id=row["workspace_id"],
            tool_name=row["tool_name"],
            status=row["status"],
            arguments=self.db.loads(row["arguments_json"], {}),
            command=self.db.loads(row["command_json"], []),
            exit_code=row["exit_code"],
            stdout=row["stdout"],
            stderr=row["stderr"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def issues(self, run_id: str) -> list[EngineeringIssueView]:
        rows = self.db.fetch_all(
            "SELECT * FROM engineering_issues WHERE run_id = ? ORDER BY queue_index",
            (run_id,),
        )
        return [self._issue_view(row) for row in rows]

    def next_issue(self, run_id: str) -> EngineeringIssueView | None:
        """一次只激活队列中的一个问题，避免同时向用户抛出大量错误。"""
        row = self.db.fetch_one(
            "SELECT * FROM engineering_issues WHERE run_id = ? AND status IN (?, ?, ?) ORDER BY queue_index LIMIT 1",
            (
                run_id,
                EngineeringIssueStatus.AWAITING_USER.value,
                EngineeringIssueStatus.DETECTED.value,
                EngineeringIssueStatus.REOPENED.value,
            ),
        )
        if not row:
            return None
        if row["status"] != EngineeringIssueStatus.AWAITING_USER.value:
            self.db.execute(
                "UPDATE engineering_issues SET status = ? WHERE issue_id = ?",
                (EngineeringIssueStatus.AWAITING_USER.value, row["issue_id"]),
            )
            row["status"] = EngineeringIssueStatus.AWAITING_USER.value
        return self._issue_view(row)

    def decide(self, issue_id: str, decision: IssueDecisionRequest) -> EngineeringIssueView:
        """记录按建议、跳过、自定义或继续解释；当前不会自动修改 RTL。"""
        row = self.db.fetch_one(
            "SELECT * FROM engineering_issues WHERE issue_id = ?", (issue_id,)
        )
        if not row:
            raise KeyError(issue_id)
        statuses = {
            "approve": EngineeringIssueStatus.APPROVED,
            "skip": EngineeringIssueStatus.SKIPPED,
            "custom": EngineeringIssueStatus.CUSTOM_REQUESTED,
            "explain": EngineeringIssueStatus.AWAITING_USER,
        }
        status = statuses[decision.action.value]
        self.db.execute(
            "UPDATE engineering_issues SET status = ?, user_decision_json = ? WHERE issue_id = ?",
            (status.value, self.db.dumps(decision.model_dump(mode="json")), issue_id),
        )
        return self._issue_view(
            self.db.fetch_one("SELECT * FROM engineering_issues WHERE issue_id = ?", (issue_id,))
        )

    def _store_issues(
        self, run_id: str, tool_name: str, text: str, raw_log_ref: str
    ) -> None:
        """解析日志、生成稳定指纹，并用唯一索引忽略同 Run 内重复问题。"""
        parsed = parse_tool_issues(tool_name, text)
        for index, item in enumerate(parsed, start=1):
            analysis = issue_analysis(item)
            self.db.execute(
                """INSERT OR IGNORE INTO engineering_issues(
                    issue_id, run_id, queue_index, stage, tool, severity, code,
                    message, file, line, raw_log_ref, fingerprint, status,
                    user_decision_json, analysis_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    f"ISSUE-{uuid4().hex[:12].upper()}",
                    run_id,
                    index,
                    item["stage"],
                    tool_name,
                    item["severity"],
                    item.get("code"),
                    item["message"],
                    item.get("file"),
                    item.get("line"),
                    raw_log_ref,
                    item["fingerprint"],
                    EngineeringIssueStatus.DETECTED.value,
                    self.db.dumps(analysis),
                ),
            )

    def _issue_view(self, row: dict) -> EngineeringIssueView:
        analysis = self.db.loads(row.get("analysis_json"), {})
        return EngineeringIssueView(
            issue_id=row["issue_id"],
            run_id=row["run_id"],
            queue_index=row["queue_index"],
            stage=row["stage"],
            tool=row["tool"],
            severity=row["severity"],
            code=row["code"],
            message=row["message"],
            file=row["file"],
            line=row["line"],
            raw_log_ref=row["raw_log_ref"],
            fingerprint=row["fingerprint"],
            status=row["status"],
            user_decision=self.db.loads(row["user_decision_json"], None),
            root_cause_analysis=analysis.get("root_cause_analysis", ""),
            evidence=analysis.get("evidence", []),
            impact=analysis.get("impact", ""),
            suggestion=analysis.get("suggestion", ""),
            confidence=analysis.get("confidence", 5),
            verification_scope=analysis.get("verification_scope", []),
        )


VERILATOR_PATTERN = re.compile(
    r"%(?P<severity>Error|Warning|Fatal)(?:-(?P<code>[A-Z0-9_]+))?:\s*(?:(?P<file>(?:[A-Za-z]:)?[^:\r\n]+):(?P<line>\d+)(?::\d+)?:\s*)?(?P<message>.+)",
    flags=re.IGNORECASE,
)


def parse_tool_issues(tool_name: str, text: str) -> list[dict]:
    """把 Verilator/环境错误文本解析成不含业务建议的基础 Issue。"""
    results: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = VERILATOR_PATTERN.search(line)
        if match:
            data = match.groupdict()
            severity = "warning" if data["severity"].lower() == "warning" else "error"
            message = data["message"].strip()
            file = data.get("file")
            line_number = int(data["line"]) if data.get("line") else None
            code = data.get("code")
        elif line.startswith(("TOOL_ENVIRONMENT:", "TOOL_ARGUMENT:", "TOOL_INTERNAL:", "TOOL_TIMEOUT:")):
            code, message = line.split(":", 1)
            severity, file, line_number = "error", None, None
            message = message.strip()
        else:
            continue
        # 指纹不使用随机 ID，而使用工具、位置和消息；同一错误重复打印只保留一次。
        fingerprint = hashlib.sha256(
            f"{tool_name}|{code}|{file}|{line_number}|{message}".encode("utf-8")
        ).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        results.append(
            {
                "stage": (
                    "lint" if "lint" in tool_name else
                    "simulation" if tool_name in verilator_adapter.SIMULATION_TOOLS else
                    "compile" if tool_name == "run_compile" else "tool_execution"
                ),
                "severity": severity,
                "code": code,
                "message": message,
                "file": file,
                "line": line_number,
                "fingerprint": fingerprint,
            }
        )
    return results


def issue_analysis(issue: dict) -> dict:
    """为常见错误生成可解释的初步分析；未知错误采用保守建议。

    这里的规则是候选建议，不会直接创建或应用 Patch。用户仍需要逐条确认。
    """
    code = str(issue.get("code") or "").upper()
    location = (
        f"{issue['file']}:{issue['line']}"
        if issue.get("file") and issue.get("line")
        else issue.get("file") or "工具运行环境"
    )
    if code == "TOOL_ENVIRONMENT":
        root = "工具可执行文件或运行环境未满足前置条件，并非 RTL 逻辑错误。"
        suggestion = "安装或配置该工具并确认其位于后端进程 PATH 中，然后重新执行同一 ToolRun。"
        impact = "当前检查未真正执行，不能据此判断 RTL 质量。"
        confidence = 10
    elif "WIDTH" in code:
        root = "表达式两侧位宽、符号属性或截断规则可能不一致。"
        suggestion = "检查报错表达式的声明宽度和 signed 属性，优先使用显式扩展、截断或类型转换生成候选 Patch。"
        impact = "可能引入数据截断、符号扩展错误或综合/仿真差异。"
        confidence = 8
    elif "MULTIDR" in code:
        root = "同一信号可能由多个过程块或连续赋值同时驱动。"
        suggestion = "定位所有驱动点，收敛为单一时序或组合赋值来源后生成候选 Patch。"
        impact = "可能导致不可综合逻辑、X 传播或实现结果不确定。"
        confidence = 8
    elif "LATCH" in code:
        root = "组合逻辑分支可能未对输出完整赋值，从而推断锁存器。"
        suggestion = "为组合块添加默认赋值并补齐所有条件分支，再生成候选 Patch。"
        impact = "可能产生非预期状态保持和时序路径。"
        confidence = 8
    else:
        root = "工具已在指定位置报告工程问题；需要结合源代码和 DS 判断具体根因。"
        suggestion = "先检查首个报错位置及其上游声明，不自动修改；确认根因后再生成最小候选 Diff。"
        impact = "该问题可能阻断当前工具阶段或造成后续级联错误。"
        confidence = 6
    return {
        "root_cause_analysis": root,
        "evidence": [location, issue.get("message", "")],
        "impact": impact,
        "suggestion": suggestion,
        "confidence": confidence,
        "verification_scope": ["重新执行当前 Tool", "检查相同文件的关联告警"],
    }


tool_run_service = ToolRunService()
