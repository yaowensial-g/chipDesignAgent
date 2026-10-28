import os
import re
from typing import Any

from app.config.model_settings import model_settings_service
from app.domain.models import SourceRef
from app.models.provider import OpenAIModelProvider
from app.skills import skill_catalog


class DeterministicAgentRuntime:
    """A deterministic stand-in for the model layer.

    It makes the workflow runnable and testable before a real model provider is
    introduced. The public methods intentionally return dictionaries matching
    the future structured-output contracts.
    """

    @staticmethod
    def source_refs(request: dict[str, Any]) -> list[dict[str, str]]:
        return [
            SourceRef(
                document=document["name"],
                version=document["version"],
                location="document",
            ).model_dump()
            for document in request["documents"]
        ]

    @staticmethod
    def route_conversation(request: dict[str, Any]) -> dict[str, str]:
        message = re.sub(r"\s+", "", request["message"].lower())
        create_action = r"生成|编写|撰写|完成|输出|创建|评审|审查|修改|更新|实现|review"
        execute_action = r"执行|运行|开始|跑一下|run"

        routes = (
            ("ds_workflow", create_action, r"ds|详细设计|设计说明"),
            ("rtl_workflow", create_action, r"rtl|systemverilog|verilog|硬件代码"),
            ("lint_workflow", execute_action, r"lint|verilator|静态检查"),
            ("simulation_workflow", execute_action, r"simulation|sim|仿真"),
            ("cdc_workflow", execute_action, r"cdc|跨时钟"),
            ("synthesis_workflow", execute_action, r"synthesis|综合"),
            ("sta_workflow", execute_action, r"sta|时序分析|静态时序"),
            ("test_execution_workflow", execute_action, r"testcase|用例|回归"),
        )
        for route, action, target in routes:
            if re.search(rf"(?:{action}).{{0,16}}(?:{target})", message):
                return {
                    "route": route,
                    "reason": f"检测到用户明确要求启动 {route}",
                }
        for route, action, target in routes:
            if re.search(rf"(?:{target}).{{0,16}}(?:{action})", message):
                return {
                    "route": route,
                    "reason": f"检测到用户明确要求启动 {route}",
                }
        return {"route": "chat", "reason": "普通问答或讨论请求"}

    @staticmethod
    def chat(request: dict[str, Any]) -> str:
        message = request["message"].strip()
        lowered = message.lower()
        if any(token in lowered for token in ("你好", "hello", "hi", "嗨")):
            return "你好。你可以直接和我讨论芯片架构、RTL、验证方法或这个项目本身；只有明确要求完成 DS、RTL 或执行工程任务时，我才会创建统一 Run。"
        if any(token in message for token in ("能做什么", "功能", "怎么用")):
            return "我可以进行普通技术问答，也可以结合文档讨论规格；当你明确要求完成 DS、RTL、Lint 或已有用例任务时，我会路由到相应 Workflow、角色 Agent 和 Tool。"
        documents = request.get("documents", [])
        document_note = ""
        if documents:
            names = "、".join(document["name"] for document in documents)
            document_note = f" 当前会话已提供文档：{names}。"
        return (
            "当前处于模拟运行时，聊天路由已经生效，但模拟 Chat Agent 不具备开放式语义推理能力。"
            "请启用 DeepSeek 等真实模型后再次提问。" + document_note
        )

    def architecture_analysis(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": f"已从架构视角检查模块 {request['module_name']} 的输入资料。",
            "sources": self.source_refs(request),
            "knowledge_type": "fact",
        }

    def design_analysis(self, request: dict[str, Any]) -> dict[str, Any]:
        submodules: list[str] = []
        for document in request["documents"]:
            if document["kind"] != "AS":
                continue
            submodules.extend(
                match.strip()
                for match in re.findall(
                    r"(?im)^\s*SUBMODULE\s*:\s*([^\r\n]+)",
                    document["content"],
                )
            )
        if not submodules:
            submodules = [f"{request['module_name']}_core"]
        return {
            "summary": "已识别 AS 中的目标子模块。",
            "submodules": list(dict.fromkeys(submodules)),
            "sources": self.source_refs(request),
        }

    def verification_analysis(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": "已检查功能可观察性、边界条件和异常行为描述。",
            "recommended_checks": [
                "正常数据处理",
                "复位与初始化",
                "边界输入",
                "异常恢复",
            ],
            "sources": self.source_refs(request),
        }

    def detect_issues(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        for index, document in enumerate(request["documents"], start=1):
            if "[CONFLICT]" in document["content"] or "规格冲突" in document["content"]:
                issues.append(
                    {
                        "issue_id": f"ISSUE-{index:03d}",
                        "title": f"{document['name']} 中存在待确认的规格冲突",
                        "description": (
                            "MVP 检测到冲突标记。现有资料不足以形成正式架构结论，"
                            "需要用户联系真实架构师。"
                        ),
                        "sources": [
                            {
                                "document": document["name"],
                                "version": document["version"],
                                "location": "conflict marker",
                            }
                        ],
                        "status": "open",
                        "discussion_round": 0,
                    }
                )
        return issues

    def generate_ds(
        self,
        request: dict[str, Any],
        design_analysis: dict[str, Any],
        user_resolution: dict[str, Any] | None,
    ) -> str:
        module_name = request["module_name"]
        submodules = design_analysis.get("submodules") or [f"{module_name}_core"]
        source_lines = "\n".join(
            f"- {doc['kind']}：{doc['name']}，版本 {doc['version']}"
            for doc in request["documents"]
        )
        submodule_sections = "\n\n".join(
            self._submodule_section(name, position)
            for position, name in enumerate(submodules, start=1)
        )
        resolution_text = "无。"
        if user_resolution:
            resolution_text = (
                f"{user_resolution.get('content') or user_resolution.get('action')}"
            )
            if user_resolution.get("evidence_reference"):
                resolution_text += f"（证据：{user_resolution['evidence_reference']}）"

        return f"""# {module_name} 详细设计说明（候选）

## 1. 引用基线

{source_lines}

## 2. 模块职责

本模块按照当前 AS 中定义的子模块边界完成详细设计。本候选不修改 FS、AS 或 ICD。

## 3. 子模块设计

{submodule_sections}

## 4. 子模块内部接口

- 内部接口需要在用户补充具体信号后进一步细化方向、位宽、握手和时序。
- 模块对外接口直接引用 ICD，不在本 DS 中重新定义。

## 5. Clock、Reset 与初始化

- 所有状态寄存器必须定义复位值。
- 复位释放后的首个合法事务行为需要在正式 DS 中确认。

## 6. 可验证性建议

- 覆盖正常处理、复位、边界输入和异常恢复场景。
- 为关键握手和状态转换补充 Assertion。

## 7. 用户需要补充的电路图

- `DS-FIG-01`：模块顶层及子模块连接关系。
- `DS-FIG-02`：关键控制状态机和数据通路。

## 8. 假设、决策与开放问题

- 用户反馈或临时处理：{resolution_text}
- 当前内容为 Agent 生成的候选版本，必须经过用户 Review 后才能成为正式设计资产。
"""

    @staticmethod
    def generate_rtl(request: dict[str, Any]) -> str:
        module_name = re.sub(r"[^A-Za-z0-9_$]", "_", request["module_name"])
        return f"""// ChipFlow candidate RTL
// Source: confirmed DS supplied by the user
// Simulation runtime: deterministic scaffold; replace TODO sections after review.
module {module_name} (
    input  logic clk,
    input  logic rst_n
);

  // TODO(design): implement registers, combinational logic, and interfaces from the DS.
  // This scaffold intentionally does not invent external ports absent from the source documents.

endmodule
"""

    def review(
        self,
        agent_id: str,
        category: str,
        request: dict[str, Any],
        ds_markdown: str,
        custom_prompt: str | None = None,
    ) -> list[dict[str, Any]]:
        description = (
            f"评审提示词：{custom_prompt}"
            if custom_prompt
            else "MVP 评审器已完成结构检查；接入真实模型后将输出具体工程问题。"
        )
        return [
            {
                "finding_id": f"F-{agent_id.upper().replace('_', '-')}-001",
                "reviewer": agent_id,
                "title": f"{category}评审完成",
                "description": description,
                "severity": "suggestion",
                "sources": [],
                "status": "open",
            }
        ]

    @staticmethod
    def _submodule_section(name: str, position: int) -> str:
        return f"""### 3.{position} {name}

- **职责**：依据 AS 定义完成对应功能。
- **输入条件**：由上游内部接口和模块工作状态共同决定。
- **输出行为**：在合法输入和握手条件满足时更新输出或内部状态。
- **内部状态**：需要用户结合实际功能补充寄存器、计数器和状态位。
- **组合逻辑**：描述译码、选择和条件生成逻辑。
- **时序逻辑**：描述状态更新、数据锁存和复位行为。
- **异常处理**：非法输入不得静默改变有效状态。
- **关键电路说明**：当前仅生成文字设计框架，正式电路图由用户补充。"""


class LLMChipDesignRuntime:
    """Real-model implementation used when model settings are enabled and ready."""

    @staticmethod
    def _provider() -> OpenAIModelProvider:
        return OpenAIModelProvider(model_settings_service.effective())

    @staticmethod
    def _context(request: dict[str, Any]) -> dict[str, Any]:
        documents = []
        remaining = 120_000
        for document in request["documents"]:
            content = document["content"][:remaining]
            remaining -= len(content)
            documents.append({**document, "content": content})
            if remaining <= 0:
                break
        return {
            "project_id": request["project_id"],
            "module_name": request["module_name"],
            "instruction": request.get("instruction") or request.get("message", ""),
            "documents": documents,
            "history": request.get("history", [])[-20:],
        }

    def route_conversation(self, request: dict[str, Any]) -> dict[str, str]:
        payload = {
            "message": request["message"],
            "recent_history": request.get("history", [])[-8:],
            "available_documents": [
                {"kind": document["kind"], "name": document["name"], "version": document["version"]}
                for document in request.get("documents", [])
            ],
        }
        result = self._provider().generate_json(
            """你是 ChipFlow 的编排路由 Agent。只识别用户明确要求，不回答问题，也不能自行决定进入下一研发阶段。可选 route：chat、ds_workflow、rtl_workflow、lint_workflow、simulation_workflow、cdc_workflow、synthesis_workflow、sta_workflow、test_execution_workflow。只有用户明确使用生成、实现、执行、运行等动作词并指定交付物或工程阶段时才进入工作流；知识问答、讨论、解释、建议和意图不清晰的请求必须 route=chat。不要因为存在文档就启动任务。输出 JSON 对象：{\"route\":\"上述值之一\",\"reason\":\"简短中文理由\"}。""",
            payload,
            operation="对话路由 Agent",
            expected_shape='{"route":"chat|ds_workflow|rtl_workflow|lint_workflow|simulation_workflow|cdc_workflow|synthesis_workflow|sta_workflow|test_execution_workflow","reason":"中文理由"}',
        )
        if not isinstance(result, dict):
            raise ValueError("路由 Agent 输出必须是 JSON 对象")
        route = str(result.get("route", "chat"))
        if route not in {
            "chat",
            "ds_workflow",
            "rtl_workflow",
            "lint_workflow",
            "simulation_workflow",
            "cdc_workflow",
            "synthesis_workflow",
            "sta_workflow",
            "test_execution_workflow",
        }:
            route = "chat"
        return {"route": route, "reason": str(result.get("reason", "路由 Agent 判定"))}

    def chat(self, request: dict[str, Any]) -> str:
        payload = self._context(request)
        payload["current_message"] = request["message"]
        return self._provider().generate_text(
            """你是 ChipFlow 的通用聊天 Agent，也熟悉数字芯片架构、RTL 设计和验证。直接回答用户当前问题，不要求用户先上传 FS/AS。若已提供文档，可以把文档作为只读上下文；明确区分文档事实、你的推断和建议，不得虚构文档内容。你可以解释、讨论和提出建议，但不能代表真实架构师修改或批准 FS、AS、ICD。使用中文，回答应清晰且适度简洁。""",
            json_payload(payload),
        )

    def architecture_analysis(self, request: dict[str, Any]) -> dict[str, Any]:
        result = self._provider().generate_json(
            """你是芯片架构顾问 Agent。FS、AS、ICD 是受控原始资料，你只能解释、质疑和指出歧义，不能擅自修改或虚构架构决策。输出 JSON 对象：summary（中文总结）、knowledge_type（fact/inference/suggestion）。必须区分文档事实与推断。"""
            + skill_catalog.prompt("architecture/specification-analysis"),
            self._context(request),
            operation="架构顾问 Agent",
            expected_shape='{"summary":"中文总结","knowledge_type":"fact|inference|suggestion"}',
        )
        if not isinstance(result, dict):
            raise ValueError("架构 Agent 输出必须是 JSON 对象")
        return {
            "summary": str(result.get("summary", "架构分析已完成。")),
            "knowledge_type": str(result.get("knowledge_type", "inference")),
            "sources": DeterministicAgentRuntime.source_refs(request),
        }

    def design_analysis(self, request: dict[str, Any]) -> dict[str, Any]:
        result = self._provider().generate_json(
            """你是资深 RTL 设计 Agent。依据 AS 识别子模块和职责，不得重新定义外部接口，不得补造没有证据的规格。输出 JSON 对象：summary（中文）、submodules（字符串数组，只列实际识别出的子模块）。""",
            self._context(request),
            operation="设计分析 Agent",
            expected_shape='{"summary":"中文总结","submodules":["子模块名"]}',
        )
        if not isinstance(result, dict):
            raise ValueError("设计 Agent 输出必须是 JSON 对象")
        submodules = [str(item) for item in result.get("submodules", []) if str(item).strip()]
        return {
            "summary": str(result.get("summary", "设计分析已完成。")),
            "submodules": submodules or [f"{request['module_name']}_core"],
            "sources": DeterministicAgentRuntime.source_refs(request),
        }

    def verification_analysis(self, request: dict[str, Any]) -> dict[str, Any]:
        result = self._provider().generate_json(
            """你是芯片验证 Agent。检查规格的可观察性、边界条件、异常行为和可验证性。输出 JSON 对象：summary（中文）、recommended_checks（字符串数组）。不要把建议说成规格事实。""",
            self._context(request),
            operation="验证分析 Agent",
            expected_shape='{"summary":"中文总结","recommended_checks":["检查项"]}',
        )
        if not isinstance(result, dict):
            raise ValueError("验证 Agent 输出必须是 JSON 对象")
        return {
            "summary": str(result.get("summary", "验证分析已完成。")),
            "recommended_checks": [str(item) for item in result.get("recommended_checks", [])],
            "sources": DeterministicAgentRuntime.source_refs(request),
        }

    def detect_issues(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        result = self._provider().generate_json(
            """你是规格质疑 Agent。对比 FS、AS、ICD，仅识别会影响设计的明确冲突、歧义、缺失或疑似错误。不要为了产生问题而制造问题。输出 JSON 对象 {\"issues\":[{\"title\":中文标题,\"description\":具体问题,\"document\":来源文档名,\"location\":章节或描述}]}。没有问题时 issues 为空数组。""",
            self._context(request),
            operation="规格质疑 Agent",
            expected_shape='{"issues":[{"title":"标题","description":"描述","document":"文档名","location":"位置"}]}',
        )
        if not isinstance(result, dict):
            raise ValueError("规格质疑 Agent 输出必须是 JSON 对象")
        versions = {doc["name"]: doc["version"] for doc in request["documents"]}
        issues = []
        for index, item in enumerate(result.get("issues", []), start=1):
            if not isinstance(item, dict):
                continue
            document = str(item.get("document") or request["documents"][0]["name"])
            issues.append(
                {
                    "issue_id": f"ISSUE-{index:03d}",
                    "title": str(item.get("title", "待确认的规格问题")),
                    "description": str(item.get("description", "现有资料不足以形成结论。")),
                    "sources": [
                        {
                            "document": document,
                            "version": versions.get(document, "unknown"),
                            "location": str(item.get("location", "document")),
                        }
                    ],
                    "status": "open",
                    "discussion_round": 0,
                }
            )
        return issues

    def generate_ds(
        self,
        request: dict[str, Any],
        design_analysis: dict[str, Any],
        user_resolution: dict[str, Any] | None,
    ) -> str:
        payload = self._context(request)
        payload["design_analysis"] = design_analysis
        payload["user_resolution"] = user_resolution
        return self._provider().generate_text(
            """你是资深芯片 RTL 设计 Agent。根据受控 FS、AS、ICD 生成中文 Markdown DS 候选。只输出 Markdown 正文。"""
            + skill_catalog.prompt("design/ds-authoring"),
            json_payload(payload),
        )

    def generate_rtl(self, request: dict[str, Any]) -> str:
        payload = self._context(request)
        payload["ds_markdown"] = request["ds_markdown"]
        return self._provider().generate_text(
            """你是资深数字芯片设计工程师。输出必须只有可保存为 .sv 文件的源码，不要 Markdown 代码块。"""
            + skill_catalog.prompt("design/rtl-implementation"),
            json_payload(payload),
        ).removeprefix("```systemverilog").removeprefix("```sv").removeprefix("```").removesuffix("```").strip()

    def review(
        self,
        agent_id: str,
        category: str,
        request: dict[str, Any],
        ds_markdown: str,
        custom_prompt: str | None = None,
    ) -> list[dict[str, Any]]:
        payload = self._context(request)
        payload["ds_markdown"] = ds_markdown
        payload["review_category"] = category
        payload["custom_prompt"] = custom_prompt
        result = self._provider().generate_json(
            f"""你是独立的{category}评审 Agent。逐项检查 DS 与来源资料，输出 JSON 对象 {{\"findings\":[{{\"title\":中文标题,\"description\":具体问题和建议,\"severity\":\"blocker|major|minor|suggestion\"}}]}}。{custom_prompt or ''}"""
            + skill_catalog.prompt("review/independent-review"),
            payload,
            operation=f"{category}评审 Agent（{agent_id}）",
            expected_shape='{"findings":[{"title":"标题","description":"问题和建议","severity":"blocker|major|minor|suggestion"}]}',
        )
        if not isinstance(result, dict):
            raise ValueError("评审 Agent 输出必须是 JSON 对象")
        findings = []
        allowed = {"blocker", "major", "minor", "suggestion"}
        for index, item in enumerate(result.get("findings", []), start=1):
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "suggestion")).lower()
            findings.append(
                {
                    "finding_id": f"F-{agent_id.upper().replace('_', '-')}-{index:03d}",
                    "reviewer": agent_id,
                    "title": str(item.get("title", f"{category}评审意见")),
                    "description": str(item.get("description", "")),
                    "severity": severity if severity in allowed else "suggestion",
                    "sources": DeterministicAgentRuntime.source_refs(request),
                    "status": "open",
                }
            )
        return findings


def json_payload(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)


class ConfigurableAgentRuntime:
    def __init__(self) -> None:
        self.deterministic = DeterministicAgentRuntime()
        self.llm = LLMChipDesignRuntime()

    def _active(self):
        forced_enabled = os.getenv("CHIP_AGENT_MODEL_ENABLED")
        if forced_enabled is not None and forced_enabled.strip().lower() in {"0", "false", "no", "off"}:
            return self.deterministic
        return self.llm if model_settings_service.effective().ready else self.deterministic

    def __getattr__(self, name: str):
        return getattr(self._active(), name)


runtime = ConfigurableAgentRuntime()

