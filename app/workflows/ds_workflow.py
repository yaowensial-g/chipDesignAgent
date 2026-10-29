"""DS 专业子流程的 LangGraph 定义。

顶层主 Agent 通过 ``create_ds_candidate`` Tool 启动这个子图。图适合表达 DS 内部
相对稳定的并行分析、冲突循环、人工中断和并行评审；它不是所有任务的固定总流程。
"""

import operator
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agents.definitions import DEFAULT_AGENT_IDS
from app.agents.runtime import runtime


class DSWorkflowState(TypedDict, total=False):
    """图节点共享状态。

    ``events`` 使用 ``Annotated[..., operator.add]``，表示并行节点返回的事件应追加，
    而不是后完成的节点覆盖先完成节点。其余字段由对应节点独占写入。
    """
    task_id: str
    request: dict[str, Any]
    status: str
    events: Annotated[list[dict[str, Any]], operator.add]
    quality_warnings: list[str]
    architecture_analysis: dict[str, Any]
    design_analysis: dict[str, Any]
    verification_analysis: dict[str, Any]
    issues: list[dict[str, Any]]
    discussion_round: int
    user_resolution: dict[str, Any] | None
    ds_markdown: str
    spec_findings: list[dict[str, Any]]
    design_findings: list[dict[str, Any]]
    verification_findings: list[dict[str, Any]]
    interface_findings: list[dict[str, Any]]
    custom_findings: list[dict[str, Any]]
    findings: list[dict[str, Any]]


def _event(event_type: str, message: str, agent_id: str | None = None) -> dict[str, Any]:
    return {"event_type": event_type, "message": message, "agent_id": agent_id}


def _disabled(state: DSWorkflowState, agent_id: str) -> bool:
    return agent_id in set(state["request"].get("disabled_agents", []))


async def prepare_context(state: DSWorkflowState) -> dict[str, Any]:
    """冻结输入并记录被用户禁用角色带来的质量降级。"""
    disabled = set(state["request"].get("disabled_agents", []))
    warnings = [
        f"用户禁用了 {agent_id}；相关检查未执行，交付质量可能下降。"
        for agent_id in DEFAULT_AGENT_IDS
        if agent_id in disabled
    ]
    return {
        "status": "executing",
        "quality_warnings": warnings,
        "discussion_round": 0,
        "events": [_event("workflow", "已冻结输入文档并开始准备 Agent 上下文。")],
    }


async def architecture_analysis(state: DSWorkflowState) -> dict[str, Any]:
    """只读理解架构基线；虚拟架构角色不能批准或修改 FS/AS。"""
    if _disabled(state, "architecture_advisor"):
        result = {"summary": "架构顾问 Agent 已被用户禁用。", "sources": []}
    else:
        result = runtime.architecture_analysis(state["request"])
    return {
        "architecture_analysis": result,
        "events": [_event("agent_completed", result["summary"], "architecture_advisor")],
    }


async def design_analysis(state: DSWorkflowState) -> dict[str, Any]:
    if _disabled(state, "design"):
        result = {
            "summary": "设计 Agent 已被用户禁用，使用最小 DS 模板继续。",
            "submodules": [f"{state['request']['module_name']}_core"],
            "sources": [],
        }
    else:
        result = runtime.design_analysis(state["request"])
    return {
        "design_analysis": result,
        "events": [_event("agent_completed", result["summary"], "design")],
    }


async def verification_analysis(state: DSWorkflowState) -> dict[str, Any]:
    if _disabled(state, "verification"):
        result = {
            "summary": "验证 Agent 已被用户禁用。",
            "recommended_checks": [],
            "sources": [],
        }
    else:
        result = runtime.verification_analysis(state["request"])
    return {
        "verification_analysis": result,
        "events": [_event("agent_completed", result["summary"], "verification")],
    }


async def detect_conflicts(state: DSWorkflowState) -> dict[str, Any]:
    """在三类分析汇合后检测需要讨论或升级真实架构师的问题。"""
    issues = runtime.detect_issues(state["request"])
    message = "未发现需要升级的规格冲突。"
    status = "executing"
    if issues:
        message = f"发现 {len(issues)} 个需要内部讨论的规格问题。"
        status = "internal_discussion"
    return {
        "issues": issues,
        "status": status,
        "events": [_event("conflict_detection", message)],
    }


def route_after_conflict_detection(state: DSWorkflowState) -> str:
    return "discussion" if state.get("issues") else "generate_ds"


async def discussion(state: DSWorkflowState) -> dict[str, Any]:
    """推进一轮内部讨论；没有新正式证据时不会擅自关闭 Issue。"""
    round_number = state.get("discussion_round", 0) + 1
    issues = [dict(issue, discussion_round=round_number) for issue in state.get("issues", [])]
    return {
        "discussion_round": round_number,
        "issues": issues,
        "status": "internal_discussion",
        "events": [
            _event(
                "discussion_round",
                f"完成第 {round_number} 轮设计/验证与架构顾问讨论，尚无正式证据解决冲突。",
                "architecture_advisor",
            )
        ],
    }


def route_after_discussion(state: DSWorkflowState) -> str:
    """达到用户配置轮次后停止内部循环并升级，而不是无限消耗模型。"""
    maximum = state["request"].get("max_discussion_rounds", 2)
    if state.get("discussion_round", 0) < maximum:
        return "discussion"
    return "await_user"


async def await_user(state: DSWorkflowState) -> dict[str, Any]:
    """使用 LangGraph interrupt 暂停，等待真实架构结论、假设或取消决定。"""
    response = interrupt(
        {
            "type": "architecture_resolution_required",
            "message": "内部讨论已达到上限，请联系真实架构师或决定是否采用临时假设。",
            "issues": state.get("issues", []),
            "allowed_actions": [
                "provide_resolution",
                "accept_assumption",
                "ignore",
                "cancel",
            ],
        }
    )
    return {
        "user_resolution": response,
        "status": "cancelled" if response.get("action") == "cancel" else "executing",
        "events": [_event("user_resolution", "已收到用户对架构问题的处理意见。")],
    }


def route_after_user(state: DSWorkflowState) -> str:
    resolution = state.get("user_resolution") or {}
    return "cancelled" if resolution.get("action") == "cancel" else "generate_ds"


async def cancelled(state: DSWorkflowState) -> dict[str, Any]:
    return {
        "status": "cancelled",
        "events": [_event("workflow", "用户取消了当前任务。")],
    }


async def generate_ds(state: DSWorkflowState) -> dict[str, Any]:
    """生成 Markdown Candidate；关键电路图仍由真实工程师补充。"""
    markdown = runtime.generate_ds(
        state["request"],
        state.get("design_analysis", {}),
        state.get("user_resolution"),
    )
    return {
        "ds_markdown": markdown,
        "status": "reviewing",
        "events": [_event("artifact_created", "已生成 DS Markdown 候选。", "design")],
    }


def _review_result(
    state: DSWorkflowState,
    agent_id: str,
    category: str,
    custom_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """统一调用独立 Reviewer；禁用角色返回空 Finding，不伪装成已评审。"""
    if _disabled(state, agent_id):
        return []
    return runtime.review(
        agent_id,
        category,
        state["request"],
        state.get("ds_markdown", ""),
        custom_prompt,
    )


async def spec_review(state: DSWorkflowState) -> dict[str, Any]:
    return {
        "spec_findings": _review_result(state, "spec_reviewer", "规格一致性"),
        "events": [_event("review_completed", "规格一致性评审已完成。", "spec_reviewer")],
    }


async def design_review(state: DSWorkflowState) -> dict[str, Any]:
    return {
        "design_findings": _review_result(
            state, "microarchitecture_reviewer", "微架构"
        ),
        "events": [
            _event("review_completed", "微架构评审已完成。", "microarchitecture_reviewer")
        ],
    }


async def verification_review(state: DSWorkflowState) -> dict[str, Any]:
    return {
        "verification_findings": _review_result(
            state, "verification_reviewer", "可验证性"
        ),
        "events": [
            _event("review_completed", "可验证性评审已完成。", "verification_reviewer")
        ],
    }


async def interface_review(state: DSWorkflowState) -> dict[str, Any]:
    return {
        "interface_findings": _review_result(state, "interface_reviewer", "接口一致性"),
        "events": [
            _event("review_completed", "接口一致性评审已完成。", "interface_reviewer")
        ],
    }


async def custom_review(state: DSWorkflowState) -> dict[str, Any]:
    findings = []
    for index, prompt in enumerate(state["request"].get("custom_reviewers", []), start=1):
        findings.extend(
            _review_result(
                state,
                f"custom_reviewer_{index}",
                "自定义视角",
                prompt,
            )
        )
    return {
        "custom_findings": findings,
        "events": [_event("review_completed", f"完成 {len(findings)} 个自定义评审。")],
    }


async def aggregate_findings(state: DSWorkflowState) -> dict[str, Any]:
    """聚合各独立视角，不自动根据多数意见修改 DS。"""
    findings = [
        *state.get("spec_findings", []),
        *state.get("design_findings", []),
        *state.get("verification_findings", []),
        *state.get("interface_findings", []),
        *state.get("custom_findings", []),
    ]
    return {
        "findings": findings,
        "status": "delivered",
        "events": [
            _event(
                "workflow_completed",
                f"DS 已交付，共保留 {len(findings)} 条独立评审意见。",
            )
        ],
    }


def build_ds_workflow():
    """声明节点和有向边，并编译为可执行 LangGraph。

    读图技巧：先看 START 到 prepare_context，再看三路分析如何汇合；冲突路径可能
    循环或 interrupt，无冲突路径生成 DS；生成后五路评审并行，最终汇合交付。
    """
    builder = StateGraph(DSWorkflowState)
    builder.add_node("prepare_context", prepare_context)
    builder.add_node("architecture_analysis", architecture_analysis)
    builder.add_node("design_analysis", design_analysis)
    builder.add_node("verification_analysis", verification_analysis)
    builder.add_node("detect_conflicts", detect_conflicts)
    builder.add_node("discussion", discussion)
    builder.add_node("await_user", await_user)
    builder.add_node("cancelled", cancelled)
    builder.add_node("generate_ds", generate_ds)
    builder.add_node("spec_review", spec_review)
    builder.add_node("design_review", design_review)
    builder.add_node("verification_review", verification_review)
    builder.add_node("interface_review", interface_review)
    builder.add_node("custom_review", custom_review)
    builder.add_node("aggregate_findings", aggregate_findings)

    # 第一阶段：一次准备后并行执行架构、设计、验证分析。
    builder.add_edge(START, "prepare_context")
    builder.add_edge("prepare_context", "architecture_analysis")
    builder.add_edge("prepare_context", "design_analysis")
    builder.add_edge("prepare_context", "verification_analysis")
    builder.add_edge("architecture_analysis", "detect_conflicts")
    builder.add_edge("design_analysis", "detect_conflicts")
    builder.add_edge("verification_analysis", "detect_conflicts")
    # 第二阶段：有冲突进入有限讨论，无冲突直接生成 DS。
    builder.add_conditional_edges(
        "detect_conflicts",
        route_after_conflict_detection,
        {"discussion": "discussion", "generate_ds": "generate_ds"},
    )
    builder.add_conditional_edges(
        "discussion",
        route_after_discussion,
        {"discussion": "discussion", "await_user": "await_user"},
    )
    builder.add_conditional_edges(
        "await_user",
        route_after_user,
        {"cancelled": "cancelled", "generate_ds": "generate_ds"},
    )
    builder.add_edge("cancelled", END)
    # 第三阶段：DS 生成后独立并行评审，避免生成者自我批准。
    builder.add_edge("generate_ds", "spec_review")
    builder.add_edge("generate_ds", "design_review")
    builder.add_edge("generate_ds", "verification_review")
    builder.add_edge("generate_ds", "interface_review")
    builder.add_edge("generate_ds", "custom_review")
    builder.add_edge("spec_review", "aggregate_findings")
    builder.add_edge("design_review", "aggregate_findings")
    builder.add_edge("verification_review", "aggregate_findings")
    builder.add_edge("interface_review", "aggregate_findings")
    builder.add_edge("custom_review", "aggregate_findings")
    builder.add_edge("aggregate_findings", END)

    # MemorySaver 只负责当前进程内的 interrupt/resume；应用层 TaskService 另存
    # SQLite 快照。它不是生产级持久化 LangGraph Checkpointer。
    return builder.compile(checkpointer=MemorySaver())


ds_workflow = build_ds_workflow()

