"""Build a validated task graph from explicitly requested project operations."""

from app.domain.enums import AgentRole, Mutability
from app.domain.models import ChatRequest
from app.domain.orchestration import Criterion, ExecutionPolicy, InputSpec, OutputSpec, TaskConstraints, TaskContract, TaskOutputRef, TaskPlanDraft
from app.tools.adapters.verilator_adapter import SIMULATION_TOOLS, VERILATOR_TOOLS
from app.tools.eda_catalog import EDA_TOOLS, WAVE_TOOLS
from app.tools.project_manifest import relative_path


def eda_workflow(request: ChatRequest, role: AgentRole) -> tuple[TaskPlanDraft, dict[str, dict]]:
    operations = list(dict.fromkeys(request.eda_operations))
    if len(operations) != len(request.eda_operations):
        raise ValueError("EDA operations must be unique")
    unavailable = [name for name in operations if name not in EDA_TOOLS]
    if unavailable:
        raise ValueError("工程能力尚未接入：" + "、".join(unavailable))
    operations = [name for name in operations if name != "get_project_manifest"]
    arguments: dict[str, dict] = {"get_project_manifest": {"manifest_path": relative_path(request.eda_manifest_path)}}
    if request.eda_applied_patch_id:
        arguments["get_project_manifest"]["applied_patch_id"] = request.eda_applied_patch_id
    if "run_impacted_tests" in operations and not request.eda_applied_patch_id:
        raise ValueError("run_impacted_tests requires eda_applied_patch_id")
    cases = request.eda_testcases
    if len(set(cases)) != len(cases):
        raise ValueError("EDA testcase IDs must be unique")
    if any(name in SIMULATION_TOOLS for name in operations) and not cases:
        raise ValueError("Explicitly select manifest-declared testcases for simulation")
    if {"run_simulation", "run_testcase"} & set(operations) and len(cases) != 1:
        raise ValueError("Single-testcase operations require exactly one testcase")
    producers = [name for name in operations if name in VERILATOR_TOOLS]
    simulators = [name for name in producers if name in SIMULATION_TOOLS]
    if "parse_eda_log" in operations and not producers:
        raise ValueError("parse_eda_log requires a real EDA operation in the same Run")
    if "get_coverage_summary" in operations and len(simulators) != 1:
        raise ValueError("Coverage summary requires exactly one simulation operation")
    if "propose_eda_patch" in operations and (not producers or not request.eda_repair_path or role != AgentRole.DESIGN):
        raise ValueError("Patch proposal requires a Design role, an explicit eda_repair_path and an EDA operation")
    if WAVE_TOOLS & set(operations) and (len(simulators) != 1 or (request.eda_wave_testcase or cases[0]) not in cases):
        raise ValueError("Waveform tools require one simulation operation and an authorized testcase")

    def source(task_id: str, artifact_type: str) -> InputSpec:
        return InputSpec(
            key=task_id, artifact_type=artifact_type,
            source=TaskOutputRef(kind="task_output", task_id=task_id, output_key="result"),
        )

    def node(name: str, artifact_type: str, inputs: tuple[InputSpec, ...] = ()) -> TaskContract:
        capability = "compose_report" if name == "engineering_report" else name
        external = name in VERILATOR_TOOLS
        patch = name == "propose_eda_patch"
        allowed = (Mutability.ANALYSIS, Mutability.EXECUTION if external else Mutability.READ)
        if patch:
            allowed += (Mutability.CANDIDATE_WRITE,)
        return TaskContract(
            task_id=name, title=name, goal=EDA_TOOLS.get(name, "根据已验证的真实工程证据汇总结论。"),
            assigned_agent=role, capability_requirements=(capability,), inputs=inputs,
            depends_on=tuple(item.source.task_id for item in inputs),
            outputs=(OutputSpec(key="result", artifact_type=artifact_type, media_type=(
                "text/markdown" if artifact_type == "report" else "application/json"
            )),),
            constraints=TaskConstraints(read_only=not external and not patch, allowed_mutability=allowed),
            execution_policy=ExecutionPolicy(max_action_steps=3) if patch else ExecutionPolicy(),
            acceptance_criteria=(Criterion(
                criterion_id="evidence", description="交付可核验的真实工程结果", rule_id="required_output",
                output_keys=("result",),
            ),),
        )

    nodes = [node("get_project_manifest", "project_manifest")]
    project = source("get_project_manifest", "project_manifest")
    for name in operations:
        arguments[name] = {}
        inputs = (project,)
        if name in SIMULATION_TOOLS:
            arguments[name] = {"testcases": cases, "seed": request.eda_seed}
            if "get_coverage_summary" in operations:
                arguments[name]["coverage"] = True
        elif name == "find_signal_references":
            if not request.eda_signal:
                raise ValueError("eda_signal is required for signal references")
            arguments[name] = {"signal": request.eda_signal}
        elif name == "parse_eda_log":
            inputs = (source(producers[-1], "eda_result"),)
        elif name == "get_coverage_summary":
            inputs = (source(simulators[0], "eda_result"),)
        elif name == "propose_eda_patch":
            inputs = (project, source(producers[-1], "eda_result"))
            arguments[name] = {"target_path": relative_path(request.eda_repair_path)}
        elif name in WAVE_TOOLS:
            inputs = (source(simulators[0], "eda_result"),)
            arguments[name] = {"testcase": request.eda_wave_testcase or cases[0]}
            if name == "analyze_signal_trace":
                if not request.eda_signal:
                    raise ValueError("eda_signal is required for trace analysis")
                arguments[name].update({
                    key: value for key, value in {
                        "signal": request.eda_signal, "clock": request.eda_clock, "ready": request.eda_ready,
                    }.items() if value
                })
        nodes.append(node(name, "eda_result" if name in VERILATOR_TOOLS else name, inputs))
    report = node("engineering_report", "report", tuple(
        source(item.task_id, item.outputs[0].artifact_type) for item in nodes
    ))
    return TaskPlanDraft(
        goal=request.message, tasks=(*nodes, report), success_criteria=report.acceptance_criteria,
        final_task_id=report.task_id,
    ), arguments
