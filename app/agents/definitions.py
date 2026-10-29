"""Agent 角色目录。

角色按真实生产职责划分，而不是“每个函数一个 Agent”。这里保存相对稳定的职责、
Prompt 版本和委派关系；具体 Tool 权限仍以 Tool Registry 为准，避免两处权限漂移。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentDefinition:
    """角色的静态说明，用于文档、Prompt 组装和能力发现。"""
    agent_id: str
    display_name: str
    prompt_version: str
    read_only: bool
    purpose: str
    system_prompt: str = ""
    can_be_primary: bool = False
    delegation_targets: tuple[str, ...] = ()


AGENTS: dict[str, AgentDefinition] = {
    "orchestrator": AgentDefinition(
        # v0.5 中仅保留历史语义。真正顶层是确定性 Control Plane，
        # 该角色不能成为会话 primary_agent。
        agent_id="orchestrator",
        display_name="Orchestrator Agent",
        prompt_version="v3",
        read_only=True,
        purpose="完善任务意图并通过 Function Calling 动态编排角色和 Tool，不自行决定研发阶段。",
    ),
    "chat": AgentDefinition(
        agent_id="chat",
        display_name="聊天 Agent",
        prompt_version="v1",
        read_only=True,
        purpose="作为 reply_to_user Tool 的推理实现，结合上下文回答普通问题。",
    ),
    "architecture_advisor": AgentDefinition(
        agent_id="architecture_advisor",
        display_name="Architecture Agent",
        prompt_version="v2",
        # 虚拟架构专家没有组织授权，只能解释和质疑受控规格。
        read_only=True,
        purpose="解释并质疑 FS、AS 和 ICD，不作正式架构决策。",
        system_prompt="你是只读 Architecture 专家，负责架构与微架构分析，不批准或修改受控规格。",
    ),
    "design": AgentDefinition(
        agent_id="design",
        display_name="Design Agent",
        prompt_version="v2",
        read_only=False,
        purpose="在架构基线约束下生成 DS、RTL 候选并调用设计侧工程 Tool。",
        system_prompt="你是 Design 主 Agent，负责设计任务执行、专家委派和结果整合，正式修改必须经过审批。",
        can_be_primary=True,
        delegation_targets=("verification", "architecture_advisor"),
    ),
    "verification": AgentDefinition(
        agent_id="verification",
        display_name="Verification Agent",
        prompt_version="v2",
        read_only=True,
        purpose="理解验证环境、执行已有用例并在明确验证任务下维护验证资产。",
        system_prompt="你是 Verification 主 Agent，负责验证任务、错误定位和结果整合，需要设计判断时委派 Design 专家。",
        can_be_primary=True,
        delegation_targets=("design", "architecture_advisor"),
    ),
    "spec_reviewer": AgentDefinition(
        agent_id="spec_reviewer",
        display_name="规格一致性评审 Agent",
        prompt_version="v1",
        read_only=True,
        purpose="独立检查规格与 DS 的一致性。",
    ),
    "microarchitecture_reviewer": AgentDefinition(
        agent_id="microarchitecture_reviewer",
        display_name="微架构评审 Agent",
        prompt_version="v1",
        read_only=True,
        purpose="独立检查电路设计的完整性和可实现性。",
    ),
    "verification_reviewer": AgentDefinition(
        agent_id="verification_reviewer",
        display_name="可验证性评审 Agent",
        prompt_version="v1",
        read_only=True,
        purpose="独立检查 DS 是否具备可验证行为。",
    ),
    "interface_reviewer": AgentDefinition(
        agent_id="interface_reviewer",
        display_name="接口一致性评审 Agent",
        prompt_version="v1",
        read_only=True,
        purpose="独立检查外部接口引用和内部接口定义。",
    ),
    "specialist_reviewer": AgentDefinition(
        agent_id="specialist_reviewer",
        display_name="Specialist Reviewer",
        prompt_version="v2",
        read_only=True,
        purpose="按任务加载 CDC、低功耗、安全、DFT、PPA 或时序专项检查清单。",
    ),
}


DEFAULT_AGENT_IDS = tuple(
    agent_id
    for agent_id in AGENTS
    if agent_id not in {"orchestrator", "chat", "specialist_reviewer"}
)

