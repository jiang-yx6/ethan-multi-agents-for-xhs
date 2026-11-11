"""委派账本和不可变产物，使用业务 Store 的连接与事务锁。"""
import json

from .models import ArtifactReference, DelegationResult, DelegationTask
from .storage import Store


class TeamStore:
    def __init__(self, store: Store):
        self.store = store

    def artifacts(self, task_id: str) -> tuple[ArtifactReference, ...]:
        rows = self.store._conn.execute(
            "SELECT reference_json FROM team_artifacts WHERE task_id=? ORDER BY rowid", (task_id,)
        ).fetchall()
        return tuple(ArtifactReference.model_validate_json(row[0]) for row in rows)

    def put_artifact(self, ref: ArtifactReference, payload) -> ArtifactReference:
        encoded = json.dumps(payload, ensure_ascii=False)
        with self.store._lock, self.store._conn:
            row = self.store._conn.execute(
                "SELECT reference_json, payload FROM team_artifacts WHERE task_id=? AND artifact_id=? AND version=?",
                (ref.task_id, ref.artifact_id, ref.version),
            ).fetchone()
            if row:
                if ArtifactReference.model_validate_json(row[0]) != ref or json.loads(row[1]) != payload:
                    raise ValueError("同一产物版本不能覆盖")
                return ref
            self.store._conn.execute("INSERT INTO team_artifacts VALUES (?, ?, ?, ?, ?)",
                                     (ref.task_id, ref.artifact_id, ref.version, ref.model_dump_json(), encoded))
        return ref

    def resolve(self, ref: ArtifactReference):
        row = self.store._conn.execute(
            "SELECT reference_json, payload FROM team_artifacts WHERE task_id=? AND artifact_id=? AND version=?",
            (ref.task_id, ref.artifact_id, ref.version),
        ).fetchone()
        if row is None or ArtifactReference.model_validate_json(row[0]) != ref:
            raise ValueError("产物不存在、版本失效或引用被修改")
        return json.loads(row[1])

    def begin(self, task: DelegationTask) -> tuple[int, DelegationResult | None]:
        with self.store._lock, self.store._conn:
            current = self.store.get_task(task.task_id)
            if current is None:
                raise KeyError(task.task_id)
            self.store._conn.execute("INSERT OR IGNORE INTO delegations VALUES (?, ?, ?, ?, NULL)",
                                     (task.task_id, task.delegation_id, task.model_dump_json(), current.current_version))
            row = self.store._conn.execute("SELECT * FROM delegations WHERE task_id=? AND delegation_id=?",
                                            (task.task_id, task.delegation_id)).fetchone()
            if DelegationTask.model_validate_json(row["task_json"]) != task:
                raise ValueError("委派 ID 已绑定其他执行要求")
            return row["base_version"], DelegationResult.model_validate_json(row["result_json"]) if row["result_json"] else None

    def finish(self, task: DelegationTask, result: DelegationResult) -> None:
        result.validate_for(task)
        for ref in result.artifacts:
            self.resolve(ref)
        with self.store._lock, self.store._conn:
            row = self.store._conn.execute("SELECT task_json, result_json FROM delegations WHERE task_id=? AND delegation_id=?",
                                            (task.task_id, task.delegation_id)).fetchone()
            if row is None:
                raise ValueError("委派尚未登记")
            if DelegationTask.model_validate_json(row["task_json"]) != task:
                raise ValueError("委派 ID 已绑定其他执行要求")
            if row["result_json"] and DelegationResult.model_validate_json(row["result_json"]) != result:
                raise ValueError("已完成委派不能覆盖结果")
            self.store._conn.execute("UPDATE delegations SET result_json=? WHERE task_id=? AND delegation_id=?",
                                     (result.model_dump_json(), task.task_id, task.delegation_id))
