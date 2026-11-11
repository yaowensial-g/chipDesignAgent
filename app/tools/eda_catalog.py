"""Tool metadata for the manifest-based EDA task workflow."""

from app.domain.enums import AgentRole, Mutability
from app.tools.adapters.verilator_adapter import SIMULATION_TOOLS, VERILATOR_TOOLS, simulation_environment
from app.tools.builtin import verilator_available, verilator_identity
from app.tools.registry import ToolRegistry, tool_registry


EDA_INSPECT = {
    "get_project_manifest": "读取并冻结 Workspace 的工程清单、RTL、宏和 Include 输入。",
    "get_module_hierarchy": "从冻结源码索引模块和实例层级。",
    "find_signal_references": "从冻结源码定位信号声明、赋值和引用。",
    "parse_eda_log": "从已绑定的真实 ToolRun 读取标准化问题。",
    "get_waveform_artifact": "查找真实仿真保存的波形产物。",
    "analyze_signal_trace": "从已保存的 VCD 分析信号变化、时钟沿和握手。",
    "propose_eda_patch": "根据实际工程失败证据为已授权的目标 RTL 生成待审核 Patch。",
    "get_coverage_summary": "汇总已绑定的 Verilator 覆盖率计数和缺失用例。",
}
EDA_EXECUTE = {
    "run_compile": "使用 Verilator 解析和展开多文件 RTL，并生成编译模型。",
    "run_lint": "使用与 verilator_lint 相同的 Verilator 适配能力检查冻结工程。",
    "run_simulation": "构建并运行清单中指定的 SystemVerilog Testbench。",
    "run_testcase": "构建并运行一个已在工程清单登记的用例。",
    "run_test_suite": "构建并运行已在工程清单登记且由用户选定的一组用例。",
    "run_regression": "执行当前冻结工程清单中的完整已授权回归套件。",
    "run_impacted_tests": "根据已应用 Patch 和清单依赖执行可能受影响的用例。",
}
EDA_TOOLS = {**EDA_INSPECT, **EDA_EXECUTE}
WAVE_TOOLS = {"get_waveform_artifact", "analyze_signal_trace"}


def register_eda_tools(registry: ToolRegistry = tool_registry) -> None:
    existing = {definition.name for definition in registry.list(include_task_only=True)}
    string = {"type": "string", "minLength": 1, "maxLength": 250}
    for name, description in EDA_TOOLS.items():
        if name in existing:
            continue
        properties = {}
        required = []
        if name == "get_project_manifest":
            properties = {"manifest_path": string, "applied_patch_id": string}
            required = ["manifest_path"]
        elif name in SIMULATION_TOOLS:
            properties = {
                "testcases": {"type": "array", "items": string, "minItems": 1, "maxItems": 4, "uniqueItems": True},
                "seed": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647},
                "coverage": {"type": "boolean"},
            }
            required = ["testcases", "seed"]
        elif name == "find_signal_references":
            properties, required = {"signal": string}, ["signal"]
        elif name in WAVE_TOOLS:
            properties, required = {"testcase": string}, ["testcase"]
            if name == "analyze_signal_trace":
                properties.update({"signal": string, "clock": string, "ready": string})
                required += ["signal"]
        elif name == "propose_eda_patch":
            properties = {
                "target_path": string,
                "after_content": {"type": "string", "minLength": 1, "maxLength": 65_000},
            }
            required = ["target_path", "after_content"]
        execute = name in VERILATOR_TOOLS
        patch = name == "propose_eda_patch"
        registry.register(
            name=name, description=description,
            allowed_roles=[AgentRole.DESIGN] if patch else [AgentRole.DESIGN, AgentRole.VERIFICATION],
            scope="task", execution_kind="orchestration", capability=name, enabled=True,
            mutability=Mutability.EXECUTION if execute else Mutability.CANDIDATE_WRITE if patch else Mutability.READ,
            requires_user_approval=execute, idempotent=not execute and not patch, timeout_seconds=300 if execute else 15,
            task_enabled=(
                _simulation_enabled if name in SIMULATION_TOOLS else verilator_available if execute else None
            ),
            input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        )


def _simulation_enabled() -> bool:
    identity = verilator_identity()
    if identity is None:
        return False
    try:
        simulation_environment(identity["executable"])
    except (OSError, ValueError):
        return False
    return True
