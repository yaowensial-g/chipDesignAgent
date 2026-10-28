import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from app.domain.enums import EngineeringIssueStatus, ToolRunStatus
from app.domain.models import (
    EngineeringIssueView,
    IssueDecisionRequest,
    ToolRunRequest,
    ToolRunView,
)
from app.services.workspace_service import utc_now, workspace_service
from app.storage.database import Database, database
from app.tools.builtin import register_builtin_tools
from app.tools.registry import ToolRegistry, tool_registry


class ToolRunService:
    def __init__(
        self,
        db: Database = database,
        registry: ToolRegistry = tool_registry,
    ) -> None:
        self.db = db
        self.registry = registry
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="chipflow-tool")
        if registry is tool_registry:
            register_builtin_tools()

    def start(self, request: ToolRunRequest) -> ToolRunView:
        workspace_service.resolve_path(request.workspace_id, ".")
        registered = self.registry.get(request.tool_name)
        if request.role not in registered.definition.allowed_roles:
            raise PermissionError(f"Role {request.role.value} cannot call {request.tool_name}")
        if registered.definition.requires_user_approval and not request.user_approved:
            raise PermissionError("Tool requires explicit user approval")
        if registered.executor is None:
            raise ValueError(f"Tool {request.tool_name} is orchestrator-managed and cannot run as a local process")
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
        self.executor.submit(self._execute, run_id, request)
        return self.get(run_id)  # type: ignore[return-value]

    def _execute(self, run_id: str, request: ToolRunRequest) -> None:
        self._set_status(run_id, ToolRunStatus.RUNNING)
        registered = self.registry.get(request.tool_name)
        try:
            if registered.executor is None:
                raise ValueError(f"Tool {request.tool_name} has no engineering executor")
            command, exit_code, stdout, stderr = registered.executor(
                request.workspace_id, request.arguments
            )
            status = (
                ToolRunStatus.COMPLETED
                if exit_code == 0
                else ToolRunStatus.TIMED_OUT
                if exit_code == 124
                else ToolRunStatus.FAILED
            )
        except Exception as exc:
            command, exit_code, stdout, stderr = [], 1, "", f"TOOL_INTERNAL: {exc}"
            status = ToolRunStatus.FAILED
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
        workspace_service.write_text(request.workspace_id, f"logs/{run_id}.stdout.log", stdout)
        workspace_service.write_text(request.workspace_id, f"logs/{run_id}.stderr.log", stderr)
        self._store_issues(
            run_id,
            request.tool_name,
            stdout + "\n" + stderr,
            f"logs/{run_id}.stderr.log" if stderr else f"logs/{run_id}.stdout.log",
        )
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
    r"%(?P<severity>Error|Warning)(?:-(?P<code>[A-Z0-9_]+))?:\s*(?:(?P<file>[^:\r\n]+):(?P<line>\d+)(?::\d+)?:\s*)?(?P<message>.+)",
    flags=re.IGNORECASE,
)


def parse_tool_issues(tool_name: str, text: str) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = VERILATOR_PATTERN.search(line)
        if match:
            data = match.groupdict()
            severity = data["severity"].lower()
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
        fingerprint = hashlib.sha256(
            f"{tool_name}|{code}|{file}|{line_number}|{message}".encode("utf-8")
        ).hexdigest()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        results.append(
            {
                "stage": "lint" if "lint" in tool_name else "tool_execution",
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
