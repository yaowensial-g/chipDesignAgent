"""Propose an incremental change and validate it against the original Run policy."""

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from app.domain.orchestration import (
    AddedTask, ExecutionPolicy, InputSpec, PlanDelta, ReplacedTask, ReplanTrigger, TaskContract,
    TaskOutputRef, TaskPlan, TaskPlanDraft, TaskState, UpdatedDependencies,
)
from app.services.global_planner_service import GlobalPlannerService, InvalidPlan, PlanningPolicy


class ReplannerService:
    def __init__(self, planner: GlobalPlannerService) -> None:
        self.planner = planner

    def propose(self, runtime, plan: TaskPlan, policy: PlanningPolicy, trigger: ReplanTrigger, context: dict) -> PlanDelta:
        return PlanDelta.model_validate(runtime.propose_plan_delta({
            "plan": plan.model_dump(mode="json"), "trigger": trigger.model_dump(mode="json"),
            "catalog": self.planner._catalog(policy), "policy": policy.model_dump(mode="json"), **context,
        }))

    def revise(
        self, plan: TaskPlan, delta: PlanDelta, policy: PlanningPolicy, states: dict[str, TaskState],
        trigger: ReplanTrigger, *, consumers_of: Callable[[set[str]], set[str]],
        validate_workflow: Callable[[TaskPlanDraft], None],
    ) -> tuple[TaskPlan, set[str], set[str]]:
        original = {task.task_id: task for task in plan.tasks}
        tasks = dict(original)
        aliases: dict[str, str] = {}
        for edit in delta.operations:
            if isinstance(edit, AddedTask):
                alias = edit.task.task_id
                if alias in aliases or alias in states:
                    raise InvalidPlan("duplicate_task_alias")
                aliases[alias] = str(uuid4())

        def resolve(key: str) -> str:
            if key in aliases:
                return aliases[key]
            if key not in original:
                raise InvalidPlan("unknown_delta_task")
            return key

        final = plan.final_task_id
        requested: set[str] = set()
        removed: set[str] = set()
        changes: set[tuple[str, str]] = set()
        for edit in delta.operations:
            key = resolve(edit.task.task_id if isinstance(edit, (AddedTask, ReplacedTask)) else edit.task_id)
            change_key = ("final", "") if edit.op == "update_final_task" else (key, edit.op)
            if change_key in changes:
                raise InvalidPlan("duplicate_delta_operation")
            changes.add(change_key)
            if isinstance(edit, (AddedTask, ReplacedTask)):
                if isinstance(edit, ReplacedTask) and key not in original:
                    raise InvalidPlan("replace_requires_existing_task")
                tasks[key] = edit.task.model_copy(update={"task_id": key})
            elif isinstance(edit, UpdatedDependencies):
                if key not in tasks:
                    raise InvalidPlan("dependency_edit_requires_task")
                tasks[key] = tasks[key].model_copy(update={
                    "depends_on": edit.depends_on, "inputs": tasks[key].inputs if edit.inputs is None else edit.inputs,
                })
            elif edit.op == "update_final_task":
                final = key
            elif edit.op == "rerun_task":
                if key not in original:
                    raise InvalidPlan("rerun_requires_existing_task")
                requested.add(key)
            elif key not in original or key == plan.final_task_id or states[key].status != "pending" or states[key].current_iteration:
                raise InvalidPlan("remove_requires_unstarted_nonfinal_task")
            else:
                removed.add(key)

        for key in removed:
            if any(task_key == key and op != "remove_task" for task_key, op in changes):
                raise InvalidPlan("conflicting_delta_operations")
            del tasks[key]
        changed: set[str] = set()
        for key, task in tasks.items():
            old = original.get(key)
            limits = old.execution_policy if old else policy.task_limits
            task = task.model_copy(update={
                "depends_on": tuple(resolve(parent) for parent in task.depends_on),
                "inputs": tuple(self._resolve_input(item, resolve) for item in task.inputs),
                "constraints": task.constraints.model_copy(update={"user_constraint_ids": policy.user_constraint_ids})
                if old is None else task.constraints,
                "execution_policy": ExecutionPolicy(**{
                    name: min(value, getattr(limits, name)) for name, value in task.execution_policy.model_dump().items()
                }),
            })
            if old is not None:
                self._preserve_contract(old, task)
                if task != old:
                    changed.add(key)
            tasks[key] = task

        if not (set(aliases) or removed or changed or requested or final != plan.final_task_id or trigger.required_reruns):
            raise InvalidPlan("no_effective_plan_change")
        if final not in tasks:
            raise InvalidPlan("invalid_final_task")
        # Preserve the final output contract even when another node will publish it.
        if final != plan.final_task_id:
            self._preserve_contract(original[plan.final_task_id], tasks[final])
        draft = TaskPlanDraft(goal=plan.goal, tasks=tuple(tasks.values()), success_criteria=plan.success_criteria, final_task_id=final)
        self.planner.validate_revision(draft, policy)
        validate_workflow(draft)
        invalidation_reruns = set(trigger.required_reruns)
        contract_reruns = {
            key for key in changed if states[key].status != "completed"
            or original[key].model_dump(exclude={"title", "depends_on"}) != tasks[key].model_dump(exclude={"title", "depends_on"})
        }
        reruns = contract_reruns | requested | invalidation_reruns
        # A confirmed bad output does not make every other output of its producer invalid.
        reruns |= consumers_of(contract_reruns | (requested - invalidation_reruns))
        if final != plan.final_task_id and final in original:
            reruns.add(final)
        if trigger.task_id in tasks and states[trigger.task_id].status != "completed":
            reruns.add(trigger.task_id)
        if reruns & removed:
            raise InvalidPlan("cannot_remove_affected_task")
        if any(not original[key].constraints.read_only and set(original[key].capability_requirements) != {"real_rtl_lint"}
               for key in reruns):
            raise InvalidPlan("external_write_cannot_be_replayed")
        affected = changed | reruns | removed | set(aliases.values())
        return plan.model_copy(update={
            "plan_id": str(uuid4()), "version": plan.version + 1, "previous_plan_id": plan.plan_id,
            "created_at": datetime.now(timezone.utc), "change_reason": trigger.reason, "affected_tasks": tuple(sorted(affected)),
            "tasks": draft.tasks, "final_task_id": final,
        }), reruns, removed

    @staticmethod
    def _resolve_input(item: InputSpec, resolve: Callable[[str], str]) -> InputSpec:
        if isinstance(item.source, TaskOutputRef):
            return item.model_copy(update={"source": item.source.model_copy(update={"task_id": resolve(item.source.task_id)})})
        return item

    @staticmethod
    def _preserve_contract(old: TaskContract, new: TaskContract) -> None:
        required_inputs = {(item.key, item.artifact_type) for item in new.inputs if item.required}
        if (
            old.assigned_agent != new.assigned_agent
            or not set(new.constraints.allowed_mutability) <= set(old.constraints.allowed_mutability)
            or (old.constraints.read_only and not new.constraints.read_only)
            or not set(old.constraints.user_constraint_ids) <= set(new.constraints.user_constraint_ids)
            or not set(old.constraints.notes) <= set(new.constraints.notes)
            or any(item.required and item not in new.outputs for item in old.outputs)
            or any(item.required and (item.key, item.artifact_type) not in required_inputs for item in old.inputs)
            or any(item.required and not isinstance(item.source, TaskOutputRef) and item not in new.inputs for item in old.inputs)
            or any(rule not in new.acceptance_criteria for rule in old.acceptance_criteria)
        ):
            raise InvalidPlan("original_contract_must_be_preserved")
