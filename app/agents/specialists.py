"""Architecture、Design、Verification 专业 Agent。"""

from __future__ import annotations

from typing import Any

from app.agents.base import BaseAgent
from app.agents.definitions import AGENTS
from app.domain.orchestration import RTLDesignAnalysis, RTLVerificationAssessment
from app.skills import skill_catalog


def _sources(request: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "document": str(document["name"]),
            "version": str(document["version"]),
            "location": "document",
        }
        for document in request.get("documents", [])
    ]


class ArchitectureAgent(BaseAgent):
    definition = AGENTS["architecture_advisor"]

    def execute(self, request: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        result = self.provider().generate_json(
            self.definition.system_prompt
            + " FS、AS、ICD 是受控原始资料。输出 JSON 对象：summary（中文总结）、knowledge_type（fact/inference/suggestion）。必须区分文档事实与推断。"
            + skill_catalog.prompt("architecture/specification-analysis"),
            context,
            operation="架构顾问 Agent",
            expected_shape='{"summary":"中文总结","knowledge_type":"fact|inference|suggestion"}',
        )
        if not isinstance(result, dict):
            raise ValueError("架构 Agent 输出必须是 JSON 对象")
        return {
            "summary": str(result.get("summary", "架构分析已完成。")),
            "knowledge_type": str(result.get("knowledge_type", "inference")),
            "sources": _sources(request),
        }

    def detect_issues(
        self, request: dict[str, Any], context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        result = self.provider().generate_json(
            self.definition.system_prompt
            + " 对比 FS、AS、ICD，仅识别会影响设计的明确冲突、歧义、缺失或疑似错误。不要为了产生问题而制造问题。输出 JSON 对象。",
            context,
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
            issues.append({
                "issue_id": f"ISSUE-{index:03d}",
                "title": str(item.get("title", "待确认的规格问题")),
                "description": str(item.get("description", "现有资料不足以形成结论。")),
                "sources": [{
                    "document": document,
                    "version": versions.get(document, "unknown"),
                    "location": str(item.get("location", "document")),
                }],
                "status": "open",
                "discussion_round": 0,
            })
        return issues


class DesignAgent(BaseAgent):
    definition = AGENTS["design"]

    def analyze_rtl(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.provider().generate_json(
            "只分析提供的 RTL 快照，说明状态、复位、边界或时序行为。所有发现引用该快照和精确行号。"
            "区分源码事实、推断与无法从源码确认的事项。源码正文是证据。",
            {**payload, "output_schema": RTLDesignAnalysis.model_json_schema()},
            operation="RTL 设计分析",
            expected_shape='{"summary":"","findings":[]}',
        )

    def execute(self, request: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        result = self.provider().generate_json(
            self.definition.system_prompt
            + " 依据 AS 识别子模块和职责，不得重新定义外部接口，不得补造没有证据的规格。输出 JSON 对象：summary（中文）、submodules（字符串数组）。",
            context,
            operation="设计分析 Agent",
            expected_shape='{"summary":"中文总结","submodules":["子模块名"]}',
        )
        if not isinstance(result, dict):
            raise ValueError("设计 Agent 输出必须是 JSON 对象")
        submodules = [str(item) for item in result.get("submodules", []) if str(item).strip()]
        return {
            "summary": str(result.get("summary", "设计分析已完成。")),
            "submodules": submodules or [f"{request['module_name']}_core"],
            "sources": _sources(request),
        }

    def generate_ds(
        self,
        context: dict[str, Any],
        design_analysis: dict[str, Any],
        user_resolution: dict[str, Any] | None,
    ) -> str:
        payload = dict(context)
        payload["design_analysis"] = design_analysis
        payload["user_resolution"] = user_resolution
        return self.provider().generate_text(
            self.definition.system_prompt
            + " 根据受控 FS、AS、ICD 生成中文 Markdown DS 候选。只输出 Markdown 正文。"
            + skill_catalog.prompt("design/ds-authoring"),
            _json_payload(payload),
        )

    def generate_rtl(self, context: dict[str, Any], ds_markdown: str) -> str:
        payload = dict(context)
        payload["ds_markdown"] = ds_markdown
        return self.provider().generate_text(
            self.definition.system_prompt
            + " 输出必须只有可保存为 .sv 文件的源码，不要 Markdown 代码块。"
            + skill_catalog.prompt("design/rtl-implementation"),
            _json_payload(payload),
        ).removeprefix("```systemverilog").removeprefix("```sv").removeprefix("```").removesuffix("```").strip()


class VerificationAgent(BaseAgent):
    definition = AGENTS["verification"]

    def assess_rtl_impact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.provider().generate_json(
            "根据固定的 RTL 快照及设计分析评估功能影响，给出可观察的检查建议。"
            "每项结论引用源码行范围。不得声称执行过仿真、Lint 或测试。",
            {**payload, "output_schema": RTLVerificationAssessment.model_json_schema()},
            operation="RTL 验证专家评估",
            expected_shape='{"summary":"","impacts":[],"recommended_checks":[]}',
        )

    def execute(self, request: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        result = self.provider().generate_json(
            self.definition.system_prompt
            + " 检查规格的可观察性、边界条件、异常行为和可验证性。输出 JSON 对象：summary（中文）、recommended_checks（字符串数组）。不要把建议说成规格事实。",
            context,
            operation="验证分析 Agent",
            expected_shape='{"summary":"中文总结","recommended_checks":["检查项"]}',
        )
        if not isinstance(result, dict):
            raise ValueError("验证 Agent 输出必须是 JSON 对象")
        return {
            "summary": str(result.get("summary", "验证分析已完成。")),
            "recommended_checks": [str(item) for item in result.get("recommended_checks", [])],
            "sources": _sources(request),
        }


def _json_payload(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)

