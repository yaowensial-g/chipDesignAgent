"""Propose and validate a bounded DAG without dispatching any actions."""

from datetime import datetime, timezone
from graphlib import CycleError, TopologicalSorter
from typing import Any, Callable
from uuid import uuid4

from pydantic import Field, ValidationError

from app.agents.runtime import runtime
from app.domain.enums import AgentRole, Mutability
from app.domain.models import ChatRequest, TaskIntent
from app.domain.orchestration import (
    ContractModel,
    Criterion,
    ExecutionPolicy,
    OutputSpec,
    PlanningResult,
    RunExecutionPolicy,
    RunInput,
    TaskConstraints,
    TaskContract,
    TaskOutputRef,
    TaskPlan,
    TaskPlanDraft,
)
from app.models.provider import ModelInvocationError
from app.services.delegation_service import ALLOWED_EXPERTS
from app.tools.builtin import register_builtin_tools
from app.tools.eda_catalog import register_eda_tools
from app.tools.orchestration import register_orchestration_tools
from app.tools.registry import ToolRegistry, tool_registry


class PlanningPolicy(ContractModel):
    primary_agent: AgentRole
    allowed_tools: dict[AgentRole, tuple[str, ...]]
    input_types: dict[str, str] = Field(default_factory=dict)
    required_inputs: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    allowed_mutability: tuple[Mutability, ...] = (Mutability.READ, Mutability.ANALYSIS)
    rule_ids: tuple[str, ...] = ("required_output",)
    schema_ids: tuple[str, ...] = ()
    user_constraint_ids: tuple[str, ...] = ()
    run_limits: RunExecutionPolicy = Field(default_factory=RunExecutionPolicy)
    task_limits: ExecutionPolicy = Field(default_factory=ExecutionPolicy)


class InvalidPlan(ValueError):
    """Stable diagnostic code, safe for a correction prompt or trace."""


class GlobalPlannerService:
    def __init__(
        self,
        registry: ToolRegistry = tool_registry,
        propose: Callable[..., Any] | None = None,
    ) -> None:
        self.registry = registry
        self.propose = propose
        if registry is tool_registry:
            register_builtin_tools()
            register_orchestration_tools()
            register_eda_tools()

    def plan(
        self,
        run_id: str,
        request: ChatRequest,
        intent: TaskIntent,
        context: dict[str, Any],
        policy: PlanningPolicy,
        *,
        simple_question: bool = False,
        validate_workflow: Callable[[TaskPlanDraft], None] | None = None,
        fixed_draft: TaskPlanDraft | None = None,
    ) -> PlanningResult:
        if set(policy.required_inputs) - policy.input_types.keys():
            return PlanningResult(status="waiting_for_user", message="请补充任务需要的工程输入。")
        catalog = self._catalog(policy)
        if set(policy.required_tools) - {name for tools in catalog.values() for name in tools}:
            return PlanningResult(
                status="capability_gap", message="当前环境缺少请求所需的真实工程能力。",
                diagnostics=("required_tool_unavailable",),
            )

        diagnostics: list[str] = []
        for _ in range(1 if simple_question or fixed_draft is not None else 2):
            try:
                raw = (
                    fixed_draft if fixed_draft is not None else self._single_task(intent, policy)
                    if simple_question
                    else (self.propose or runtime.propose_task_plan)(
                        {
                            "raw_request": request.message,
                            "intent": intent.model_dump(mode="json"),
                            "context": context,
                            "project_id": request.project_id,
                            "module_name": request.module_name,
                            "primary_agent": policy.primary_agent.value,
                            "catalog": catalog,
                            "input_types": policy.input_types,
                            "rule_ids": policy.rule_ids,
                            "schema_ids": policy.schema_ids,
                            "user_constraint_ids": policy.user_constraint_ids,
                            "allowed_mutability": policy.allowed_mutability,
                            "max_tasks": policy.run_limits.max_plan_tasks,
                            "validation_feedback": diagnostics[-1:],
                        }
                    )
                )
                draft = TaskPlanDraft.model_validate(raw)
                self._validate(draft, policy, catalog)
                if validate_workflow is not None:
                    validate_workflow(draft)
                return PlanningResult(
                    status="planned", plan=self._assign_ids(run_id, draft, policy),
                    message="任务计划已通过校验。", diagnostics=tuple(diagnostics),
                )
            except ValidationError:
                diagnostics.append("invalid_plan_shape")
            except InvalidPlan as exc:
                diagnostics.append(str(exc))
            except ModelInvocationError as exc:
                if exc.code == "MODEL_NOT_CONFIGURED":
                    return PlanningResult(
                        status="capability_gap", message="当前环境缺少可用的主模型。",
                        diagnostics=("planning_model_unavailable",),
                    )
                diagnostics.append("planner_output_unavailable")
            except TimeoutError:
                diagnostics.append("planner_output_unavailable")

        return PlanningResult(
            status="failed", message=f"暂时无法生成可执行的任务计划。Run ID {run_id}。",
            diagnostics=tuple(diagnostics),
        )

    def _catalog(self, policy: PlanningPolicy) -> dict[str, dict[str, dict]]:
        allowed_roles = {policy.primary_agent, *ALLOWED_EXPERTS.get(policy.primary_agent, set())}
        return {
            role.value: {
                definition.name: {
                    "capability": self.registry.get(definition.name).capability,
                    "description": definition.description,
                    "mutability": definition.mutability.value,
                }
                for definition in self.registry.for_role(role, task=True)
                if definition.name in policy.allowed_tools.get(role, ())
                and definition.mutability in policy.allowed_mutability
                and (role == policy.primary_agent or definition.mutability in {Mutability.READ, Mutability.ANALYSIS})
            }
            for role in sorted(allowed_roles, key=lambda item: item.value)
        }

    def validate_revision(self, draft: TaskPlanDraft, policy: PlanningPolicy) -> None:
        catalog = self._catalog(policy)
        if set(policy.required_tools) - {name for tools in catalog.values() for name in tools}:
            raise InvalidPlan("required_tool_unavailable")
        if set(policy.required_inputs) - policy.input_types.keys():
            raise InvalidPlan("required_input_unavailable")
        self._validate(draft, policy, catalog)

    @staticmethod
    def _single_task(intent: TaskIntent, policy: PlanningPolicy) -> TaskPlanDraft:
        criterion = Criterion(
            criterion_id="has_answer", description="提供回答", rule_id="required_output",
            output_keys=("answer",),
        )
        task = TaskContract(
            task_id="answer", title="回答问题", goal=intent.goal, assigned_agent=policy.primary_agent,
            capability_requirements=("compose_report",), inputs=(), depends_on=(),
            outputs=(OutputSpec(key="answer", artifact_type="report", media_type="text/markdown"),),
            constraints=TaskConstraints(allowed_mutability=(Mutability.ANALYSIS,)),
            acceptance_criteria=(criterion,), execution_policy=ExecutionPolicy(max_action_steps=0),
        )
        return TaskPlanDraft(
            goal=intent.goal, tasks=(task,), final_task_id=task.task_id, success_criteria=(criterion,),
        )

    @staticmethod
    def _validate(draft: TaskPlanDraft, policy: PlanningPolicy, catalog: dict[str, dict[str, dict]]) -> None:
        nodes = {node.task_id: node for node in draft.tasks}
        if len(draft.tasks) > policy.run_limits.max_plan_tasks:
            raise InvalidPlan("too_many_tasks")
        if len(nodes) != len(draft.tasks):
            raise InvalidPlan("duplicate_task")
        final = nodes.get(draft.final_task_id)
        if final is None or final.assigned_agent != policy.primary_agent:
            raise InvalidPlan("invalid_final_task")
        graph = {node.task_id: set(node.depends_on) for node in draft.tasks}
        if any(not dependencies <= nodes.keys() for dependencies in graph.values()):
            raise InvalidPlan("unknown_dependency")
        try:
            ordered = tuple(TopologicalSorter(graph).static_order())
        except CycleError as exc:
            raise InvalidPlan("dependency_cycle") from exc
        reachable = {final.task_id}
        for key in reversed(ordered):
            if key in reachable:
                reachable.update(graph[key])
        if reachable != nodes.keys():
            raise InvalidPlan("task_cannot_reach_final")

        outputs = {node.task_id: {output.key: output for output in node.outputs} for node in draft.tasks}
        for task in draft.tasks:
            allowed = set(task.constraints.allowed_mutability)
            if (
                not allowed <= set(policy.allowed_mutability)
                or (task.assigned_agent != policy.primary_agent and not task.constraints.read_only)
                or not set(task.constraints.user_constraint_ids) <= set(policy.user_constraint_ids)
            ):
                raise InvalidPlan("invalid_task_constraints")
            tools = {
                name: entry for name, entry in catalog.get(task.assigned_agent.value, {}).items()
                if entry["mutability"] in allowed and entry["capability"]
            }
            if (
                not set(task.capability_requirements) <= {entry["capability"] for entry in tools.values()}
                or not set(task.tool_hints) <= tools.keys()
                or any(tools[name]["capability"] not in task.capability_requirements for name in task.tool_hints)
            ):
                raise InvalidPlan("unauthorized_capability")
            if (
                len(outputs[task.task_id]) != len(task.outputs)
                or len({item.key for item in task.inputs}) != len(task.inputs)
                or len(graph[task.task_id]) != len(task.depends_on)
            ):
                raise InvalidPlan("duplicate_contract_key")
            if any(item.schema_id and item.schema_id not in policy.schema_ids for item in task.outputs):
                raise InvalidPlan("unknown_output_schema")
            for item in task.inputs:
                if isinstance(item.source, RunInput):
                    actual = policy.input_types.get(item.source.key)
                else:
                    parent = item.source.task_id
                    if parent not in graph[task.task_id]:
                        raise InvalidPlan("input_requires_direct_dependency")
                    upstream = outputs[parent].get(item.source.output_key)
                    actual = upstream.artifact_type if upstream and (upstream.required or not item.required) else None
                if actual != item.artifact_type and (actual is not None or item.required or not isinstance(item.source, RunInput)):
                    raise InvalidPlan("invalid_input_source")
            GlobalPlannerService._check_criteria(task.acceptance_criteria, outputs[task.task_id], policy)
        GlobalPlannerService._check_criteria(draft.success_criteria, outputs[final.task_id], policy)

    @staticmethod
    def _check_criteria(criteria: tuple[Criterion, ...], outputs: dict, policy: PlanningPolicy) -> None:
        if len({item.criterion_id for item in criteria}) != len(criteria):
            raise InvalidPlan("duplicate_criterion")
        if any(
            item.rule_id not in policy.rule_ids or not set(item.output_keys) <= outputs.keys()
            for item in criteria
        ):
            raise InvalidPlan("invalid_acceptance_rule")

    @staticmethod
    def _assign_ids(run_id: str, draft: TaskPlanDraft, policy: PlanningPolicy) -> TaskPlan:
        ids = {node.task_id: str(uuid4()) for node in draft.tasks}
        tasks = []
        for node in draft.tasks:
            limits = {
                name: min(value, getattr(policy.task_limits, name))
                for name, value in node.execution_policy.model_dump().items()
            }
            inputs = tuple(
                item.model_copy(update={"source": item.source.model_copy(update={"task_id": ids[item.source.task_id]})})
                if isinstance(item.source, TaskOutputRef) else item
                for item in node.inputs
            )
            tasks.append(node.model_copy(update={
                "task_id": ids[node.task_id],
                "depends_on": tuple(ids[key] for key in node.depends_on),
                "inputs": inputs,
                "constraints": node.constraints.model_copy(update={"user_constraint_ids": policy.user_constraint_ids}),
                "execution_policy": ExecutionPolicy(**limits),
            }))
        return TaskPlan(
            plan_id=str(uuid4()), run_id=run_id, goal=draft.goal, tasks=tuple(tasks),
            success_criteria=draft.success_criteria, final_task_id=ids[draft.final_task_id],
            created_at=datetime.now(timezone.utc), version=1, status="active",
        )
