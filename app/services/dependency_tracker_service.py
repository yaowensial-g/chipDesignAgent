"""Track impact through persisted data bindings of current task attempts."""

from dataclasses import dataclass
from sqlite3 import Connection


@dataclass(frozen=True)
class ArtifactImpact:
    consumers: frozenset[str]
    producers: frozenset[str]
    artifacts: frozenset[str]

    @property
    def reruns(self) -> frozenset[str]:
        return self.consumers | self.producers


class DependencyTrackerService:
    @staticmethod
    def outputs(conn: Connection, run_id: str, task_ids: set[str]) -> set[str]:
        return {
            row["artifact_id"] for row in conn.execute(
                """SELECT a.artifact_id, a.task_id FROM orchestration_artifacts a
                   JOIN orchestration_tasks t USING(task_id) WHERE t.run_id=? AND a.status='verified'
                   AND json_extract(t.state_json,'$.generation')=json_extract(a.artifact_json,'$.source_generation')""",
                (run_id,),
            ) if row["task_id"] in task_ids
        }

    @staticmethod
    def direct_consumers(conn: Connection, run_id: str, artifact_id: str) -> set[str]:
        return {row["task_id"] for row in conn.execute(
            """SELECT DISTINCT b.task_id FROM orchestration_attempt_bindings b
               JOIN orchestration_tasks t ON t.task_id=b.task_id AND t.run_id=b.run_id
               WHERE b.run_id=? AND b.artifact_id=? AND b.generation=json_extract(t.state_json,'$.generation')""",
            (run_id, artifact_id),
        )}

    def impact(self, conn: Connection, run_id: str, roots: set[str]) -> ArtifactImpact:
        producers = {
            row["task_id"] for row in conn.execute(
                """SELECT a.artifact_id, a.task_id FROM orchestration_artifacts a
                   JOIN orchestration_tasks t USING(task_id) WHERE t.run_id=?""", (run_id,),
            ) if row["artifact_id"] in roots
        }
        affected = set(roots)
        consumers: set[str] = set()
        frontier = set(roots)
        while frontier:
            found = set().union(*(self.direct_consumers(conn, run_id, item) for item in frontier)) - consumers
            consumers.update(found)
            frontier = self.outputs(conn, run_id, found) - affected
            affected.update(frontier)
        return ArtifactImpact(frozenset(consumers), frozenset(producers), frozenset(affected))
