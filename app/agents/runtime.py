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
    def chat(request: dict[str, Any]) -> str:
        message = request["message"].strip()
        lowered = message.lower()
        if any(token in lowered for token in ("你好", "hello", "hi", "嗨")):
            return "你好。你可以直接和我讨论芯片架构、RTL、验证方法或这个项目本身；只有明确要求完成 DS、RTL 或执行工程任务时，我才会创建统一 Run。"
        if any(token in message for token in ("能做什么", "功能", "怎么用")):
            return "我可以进行普通技术问答，也可以结合文档讨论规格；所有请求都会进入统一编排，普通回答使用 Chat Tool，工程任务由 Planner 动态选择角色 Agent 和受控 Tool。"
        documents = request.get("documents", [])
        document_note = ""
        if documents:
            names = "、".join(document["name"] for document in documents)
            document_note = f" 当前会话已提供文档：{names}。"
        return (
            "当前处于模拟运行时，聊天路由已经生效，但模拟 Chat Agent 不具备开放式语义推理能力。"
            "请启用 DeepSeek 等真实模型后再次提问。" + document_note
        )

    @staticmethod
    def enrich_intent(request: dict[str, Any], tool_names: list[str]) -> dict[str, Any]:
        message = request["message"].strip()
        compact = re.sub(r"\s+", "", message.lower())
        candidates: list[str] = []
        mappings = (
            ("create_rtl_candidate", r"rtl|systemverilog|verilog|硬件代码"),
            ("create_ds_candidate", r"ds|详细设计|设计说明"),
            ("verilator_lint", r"lint|verilator|静态检查"),
            ("discover_testcases", r"testcase|用例|回归"),
        )
        for name, pattern in mappings:
            if name in tool_names and re.search(pattern, compact):
                candidates.append(name)
        if not candidates:
            candidates.append("reply_to_user")
        missing_information: list[str] = []
        if re.search(r"检查一下|看一下|看看|处理一下|帮我弄", compact) and not re.search(
            r"ds|详细设计|rtl|systemverilog|verilog|lint|仿真|simulation|cdc|综合|synthesis|sta|时序|用例|回归",
            compact,
        ):
            missing_information.append("希望检查的对象和检查方式尚不明确")
        evidence = [f"用户原始请求：{message}"]
        if request.get("documents"):
            evidence.append("当前会话包含受控设计文档")
        if request.get("workspace_id"):
            evidence.append("当前会话已选择 Workspace")
        return {
            "raw_request": message,
            "goal": message,
            "deliverables": [],
            "target_module": request.get("module_name"),
            "known_context": {
                "document_names": [item["name"] for item in request.get("documents", [])],
                "workspace_id": request.get("workspace_id"),
                "has_confirmed_ds": bool(request.get("ds_markdown")),
            },
            "assumptions": [],
            "missing_information": missing_information,
            "candidate_tools": candidates,
            "acceptance_criteria": [],
            "evidence": evidence,
            "confidence": 6,
        }

    def select_tool(
        self,
        request: dict[str, Any],
        intent: dict[str, Any],
        tools: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if intent.get("missing_information"):
            return {
                "name": "ask_user",
                "arguments": {
                    "question": "你希望我从哪个角度检查：阅读代码并评审、执行 Lint，还是结合规格检查设计一致性？",
                    "required_fields": ["检查方式"],
                },
                "reason": "任务意图存在会显著改变执行路径的缺失信息",
            }
        if observations:
            summaries = [str(item.get("summary", "")) for item in observations if item.get("summary")]
            return {
                "name": "reply_to_user",
                "arguments": {"message": "\n\n".join(summaries) or self.chat(request)},
                "reason": "已有角色观察结果，向用户汇总",
            }
        message = re.sub(r"\s+", "", request["message"].lower())
        create_action = r"生成|编写|撰写|完成|输出|创建|评审|审查|修改|更新|实现|review"
        execute_action = r"执行|运行|开始|跑一下|run"
        choices = (
            ("create_rtl_candidate", create_action, r"rtl|systemverilog|verilog|硬件代码"),
            ("create_ds_candidate", create_action, r"ds|详细设计|设计说明"),
            ("verilator_lint", execute_action, r"lint|verilator|静态检查"),
            ("discover_testcases", execute_action, r"testcase|用例|回归"),
        )
        available = {tool["name"] for tool in tools}
        for name, action, target in choices:
            if name in available and (
                re.search(rf"(?:{action}).{{0,16}}(?:{target})", message)
                or re.search(rf"(?:{target}).{{0,16}}(?:{action})", message)
            ):
                arguments: dict[str, Any] = {}
                if name == "verilator_lint":
                    arguments = dict(request.get("tool_arguments") or {})
                return {"name": name, "arguments": arguments, "reason": "模拟 Planner 选择匹配 Tool"}
        unsupported = (
            ("simulation", r"仿真|simulation|sim"),
            ("cdc", r"cdc|跨时钟"),
            ("synthesis", r"综合|synthesis"),
            ("sta", r"sta|时序分析|静态时序"),
        )
        if re.search(execute_action, message):
            for capability, target in unsupported:
                if re.search(target, message):
                    return {
                        "name": "report_capability_gap",
                        "arguments": {"capability": capability},
                        "reason": "请求的真实 Tool Adapter 尚未注册",
                    }
        return {
            "name": "reply_to_user",
            "arguments": {"message": self.chat(request)},
            "reason": "普通讨论由 Chat Tool 完成",
        }

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

    def chat(self, request: dict[str, Any]) -> str:
        payload = self._context(request)
        payload["current_message"] = request["message"]
        return self._provider().generate_text(
            """你是 ChipFlow 的通用聊天 Agent，也熟悉数字芯片架构、RTL 设计和验证。直接回答用户当前问题，不要求用户先上传 FS/AS。若已提供文档，可以把文档作为只读上下文；明确区分文档事实、你的推断和建议，不得虚构文档内容。你可以解释、讨论和提出建议，但不能代表真实架构师修改或批准 FS、AS、ICD。使用中文，回答应清晰且适度简洁。""",
            json_payload(payload),
        )

    def enrich_intent(self, request: dict[str, Any], tool_names: list[str]) -> dict[str, Any]:
        payload = self._context(request)
        payload["raw_request"] = request["message"]
        payload["workspace_id"] = request.get("workspace_id")
        payload["has_confirmed_ds"] = bool(request.get("ds_markdown"))
        payload["available_tools"] = tool_names
        result = self._provider().generate_json(
            """你是任务意图完善 Agent。把用户不完整的表达整理成结构化任务意图，但绝不能补造 FS、AS、ICD 中没有的工程事实。保留 raw_request；可从对话、文档清单和 Workspace 得到的内容写入 known_context；低风险默认值写入 assumptions；会显著改变结果的缺失项写入 missing_information。confidence 为 1 到 10。只返回 JSON。""",
            payload,
            operation="任务意图完善 Agent",
            expected_shape='{"raw_request":"原话","goal":"目标","deliverables":[],"target_module":"模块","known_context":{},"assumptions":[],"missing_information":[],"candidate_tools":[],"acceptance_criteria":[],"evidence":[],"confidence":1}',
        )
        if not isinstance(result, dict):
            raise ValueError("任务意图完善 Agent 输出必须是 JSON 对象")
        result["raw_request"] = request["message"]
        result.setdefault("goal", request["message"])
        result.setdefault("target_module", request.get("module_name"))
        for field in ("deliverables", "assumptions", "missing_information", "candidate_tools", "acceptance_criteria", "evidence"):
            value = result.get(field, [])
            result[field] = value if isinstance(value, list) else [str(value)]
        if not isinstance(result.get("known_context"), dict):
            result["known_context"] = {}
        try:
            result["confidence"] = max(1, min(10, int(result.get("confidence", 5))))
        except (TypeError, ValueError):
            result["confidence"] = 5
        return result

    def select_tool(
        self,
        request: dict[str, Any],
        intent: dict[str, Any],
        tools: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = self._context(request)
        payload.update(
            {
                "raw_request": request["message"],
                "task_intent": intent,
                "workspace_id": request.get("workspace_id"),
                "has_confirmed_ds": bool(request.get("ds_markdown")),
                "previous_observations": observations,
                "already_called_tools": [item.get("tool_name") for item in observations],
            }
        )
        schemas = []
        for tool in tools:
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["function_description"],
                        "parameters": tool["input_schema"],
                    },
                }
            )
        return self._provider().choose_tool(
            """你是 ChipFlow 的统一任务编排 Agent。所有请求都必须通过 Tool 完成，普通聊天也调用 reply_to_user。根据用户明确目标动态选择一个最合适的下一步 Tool，不得强制套用固定 Workflow，不得自行推进用户未要求的研发阶段。可以先调用角色咨询 Tool 获取观察，再重新规划。若缺失的是权威规格事实、目标文件或必要授权，调用 ask_user；不要猜测。不得重复调用 already_called_tools 中的分析 Tool。候选写入和本地进程必须遵守 Tool 描述及审批策略。""",
            payload,
            schemas,
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

