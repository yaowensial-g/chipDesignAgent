from app.domain.enums import AgentRole, Mutability
from app.tools.registry import tool_registry


def register_orchestration_tools() -> None:
    definitions = [
        dict(
            name="reply_to_user",
            description="直接回答、解释、讨论或总结用户问题；它也是普通聊天任务的唯一执行步骤。",
            when_to_use="请求不需要工程副作用，或已有观察结果足以形成最终答复时。",
            when_not_to_use="用户明确要求生成工程交付物、修改候选文件或执行 EDA 工具时。",
            mutability=Mutability.ANALYSIS,
            input_schema={
                "type": "object",
                "properties": {"message": {"type": "string", "description": "给用户的完整中文答复"}},
                "required": ["message"],
            },
            output_schema={"type": "object", "properties": {"message": {"type": "string"}}},
            side_effect="conversation_reply",
        ),
        dict(
            name="ask_user",
            description="当缺失信息会显著改变工程结果时，暂停 Run 并向用户提出一个明确问题。",
            when_to_use="目标、交付物、目标文件、权威规格结论或执行授权缺失且无法安全推断时。",
            when_not_to_use="信息可从当前上下文得到，或可采用低风险默认值并显式记录假设时。",
            mutability=Mutability.ANALYSIS,
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "required_fields": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question"],
            },
            output_schema={"type": "object", "properties": {"pending": {"type": "boolean"}}},
            side_effect="pause_for_user",
        ),
        dict(
            name="consult_architecture_agent",
            description="让虚拟架构角色解释、质疑 FS/AS/ICD 并指出歧义，不修改原始规格。",
            when_to_use="需要从架构视角理解规格、检查接口语义或识别架构问题时。",
            when_not_to_use="要求真实架构决策、批准或直接修改受控规格时。",
            mutability=Mutability.ANALYSIS,
            input_schema={"type": "object", "properties": {"focus": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
            side_effect="none",
        ),
        dict(
            name="consult_design_agent",
            description="让设计角色分析微架构、子模块、电路实现和 RTL 方案。",
            when_to_use="需要设计分析、微架构权衡或实现建议时。",
            when_not_to_use="仅需执行外部 EDA 工具，或需要真实架构师批准规格时。",
            mutability=Mutability.ANALYSIS,
            input_schema={"type": "object", "properties": {"focus": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
            side_effect="none",
        ),
        dict(
            name="consult_verification_agent",
            description="让验证角色检查可观察性、边界条件、异常行为和可验证性。",
            when_to_use="需要验证视角、测试关注点或设计可验证性分析时。",
            when_not_to_use="设计人员只要求执行已有用例且无需验证分析时。",
            mutability=Mutability.ANALYSIS,
            input_schema={"type": "object", "properties": {"focus": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
            side_effect="none",
        ),
        dict(
            name="create_ds_candidate",
            description="依据受控规格创建 DS Markdown 候选，并组织架构、设计、验证及多视角评审。",
            when_to_use="用户明确要求完成、生成、修改或评审 DS 时。",
            when_not_to_use="用户只是询问 DS 概念，或缺少 AS 且用户尚未补充时。",
            mutability=Mutability.CANDIDATE_WRITE,
            input_schema={"type": "object", "properties": {"instruction": {"type": "string"}}},
            output_schema={"type": "object", "properties": {"task_id": {"type": "string"}}},
            preconditions=["存在 AS 设计基线"],
            side_effect="candidate_artifact",
        ),
        dict(
            name="create_rtl_candidate",
            description="根据用户已确认的 DS 生成 RTL 候选和 Diff；不会直接覆盖正式文件。",
            when_to_use="用户明确要求实现、生成或修改 RTL 时。",
            when_not_to_use="没有已确认 DS，或用户只讨论 RTL 概念时。",
            mutability=Mutability.CANDIDATE_WRITE,
            input_schema={
                "type": "object",
                "properties": {"target_path": {"type": "string"}, "instruction": {"type": "string"}},
            },
            output_schema={"type": "object", "properties": {"patch_id": {"type": "string"}}},
            preconditions=["存在用户确认的 DS"],
            side_effect="candidate_patch",
        ),
        dict(
            name="report_capability_gap",
            description="诚实说明当前缺少某个工程 Tool Adapter，并给出接入所需信息。",
            when_to_use="用户明确要求执行仿真、CDC、综合、STA 等尚未注册的真实工具时。",
            when_not_to_use="已有可执行 Tool，或用户只是询问相关概念时。",
            mutability=Mutability.ANALYSIS,
            input_schema={
                "type": "object",
                "properties": {"capability": {"type": "string"}},
                "required": ["capability"],
            },
            output_schema={"type": "object", "properties": {"message": {"type": "string"}}},
            side_effect="pause_for_user",
        ),
    ]

    existing = {tool.name for tool in tool_registry.list()}
    for definition in definitions:
        if definition["name"] in existing:
            continue
        tool_registry.register(
            **definition,
            allowed_roles=[AgentRole.ORCHESTRATOR],
            requires_user_approval=False,
            idempotent=True,
            timeout_seconds=120,
            execution_kind="orchestration",
        )


register_orchestration_tools()
