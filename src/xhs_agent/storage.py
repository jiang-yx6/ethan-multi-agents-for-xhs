"""业务存储：增量迁移、画像快照、版本草稿和阶段产物。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import ContentRequest, Draft, PublishResult, ResearchBrief, ReviewReport, TaskView, UserProfile


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, topic TEXT NOT NULL, status TEXT NOT NULL,
                    current_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    version INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                    images TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0,
                    approved_at TEXT, created_at TEXT NOT NULL, UNIQUE(task_id, version)
                );
                CREATE TABLE IF NOT EXISTS publish_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL UNIQUE,
                    draft_version INTEGER NOT NULL, title TEXT NOT NULL, published_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stage_results (
                    task_id TEXT NOT NULL, stage TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL, PRIMARY KEY(task_id, stage, version)
                );
            """)
            # 原有列与数据保留；旧草稿 payload 为空时从旧列读取。
            additions = {
                "tasks": {"request_json": "TEXT", "profile_json": "TEXT", "last_error": "TEXT"},
                "drafts": {"payload": "TEXT"},
            }
            with self._conn:
                for table, columns in additions.items():
                    existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
                    for column, declaration in columns.items():
                        if column not in existing:
                            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
                self._conn.execute("PRAGMA user_version = 2")

    def close(self) -> None:
        self._conn.close()

    def create_task(self, task_id: str, topic: str,
                    request: ContentRequest | None = None, profile: UserProfile | None = None) -> None:
        now = _now()
        request = request or ContentRequest(goal=topic, topic=topic)
        profile = profile or UserProfile()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO tasks (task_id, topic, status, current_version, created_at, updated_at, "
                "request_json, profile_json) VALUES (?, ?, 'pending', 0, ?, ?, ?, ?)",
                (task_id, topic, now, now, request.model_dump_json(), profile.model_dump_json()),
            )

    def update_status(self, task_id: str, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                               (status, _now(), task_id))

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE tasks SET status = 'failed', last_error = ?, updated_at = ? WHERE task_id = ?",
                (error, _now(), task_id),
            )

    def clear_error(self, task_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE tasks SET last_error = NULL WHERE task_id = ?", (task_id,))

    def update_topic(self, task_id: str, topic: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE tasks SET topic = ?, updated_at = ? WHERE task_id = ?",
                               (topic, _now(), task_id))

    def save_stage(self, task_id: str, stage: str, payload, version: int = 0) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO stage_results (task_id, stage, version, payload) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(task_id, stage, version) DO UPDATE SET payload = excluded.payload",
                (task_id, stage, version, _json(payload)),
            )

    def get_stage(self, task_id: str, stage: str, version: int = 0):
        row = self._conn.execute(
            "SELECT payload FROM stage_results WHERE task_id = ? AND stage = ? AND version = ?",
            (task_id, stage, version),
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def bind_publish_target(self, task_id: str, target: dict) -> None:
        """首版任务绑定一个目标；并发命令不能互相覆盖平台或账号。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO stage_results (task_id, stage, version, payload) VALUES (?, 'publish_target', 0, ?)",
                (task_id, _json(target)),
            )
            if self.get_stage(task_id, "publish_target") != target:
                raise ValueError("任务已绑定其他发布账号，不能覆盖")

    def get_draft(self, task_id: str, version: int) -> Draft | None:
        row = self._conn.execute("SELECT * FROM drafts WHERE task_id = ? AND version = ?",
                                 (task_id, version)).fetchone()
        return self._draft_from_row(row) if row else None

    def save_draft(self, task_id: str, draft: Draft) -> None:
        """同版本相同内容可重放；写入失败回滚，旧批准不会被部分清除。"""
        with self._lock, self._conn:
            existing = self.get_draft(task_id, draft.version)
            if existing:
                if (existing.title, existing.body, existing.images, existing.tags,
                    existing.content_type, existing.revision_notes) != (
                    draft.title, draft.body, draft.images, draft.tags,
                    draft.content_type, draft.revision_notes
                ):
                    raise ValueError("同版本草稿已存在，不能覆盖")
                return
            task = self._conn.execute("SELECT current_version FROM tasks WHERE task_id = ?",
                                      (task_id,)).fetchone()
            if task is None or draft.version != task["current_version"] + 1:
                raise ValueError("草稿版本必须在当前版本基础上递增")
            clean = draft.model_copy(update={"approved": False, "approved_at": None})
            self._conn.execute("UPDATE drafts SET approved = 0, approved_at = NULL WHERE task_id = ?",
                               (task_id,))
            self._conn.execute(
                "INSERT INTO drafts (task_id, version, title, body, images, approved, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (task_id, clean.version, clean.title, clean.body,
                 _json([image.model_dump(mode="json") for image in clean.images]),
                 clean.created_at.isoformat(), clean.model_dump_json()),
            )
            self._conn.execute(
                "UPDATE tasks SET current_version = ?, status = 'pending_review', updated_at = ? WHERE task_id = ?",
                (clean.version, _now(), task_id),
            )

    def approve_draft(self, task_id: str, version: int) -> None:
        with self._lock, self._conn:
            task = self._conn.execute("SELECT current_version FROM tasks WHERE task_id = ?",
                                      (task_id,)).fetchone()
            if task is None or task["current_version"] != version or not self.get_draft(task_id, version):
                raise ValueError("只能批准当前存在的草稿版本")
            self._conn.execute(
                "UPDATE drafts SET approved = 1, approved_at = ? WHERE task_id = ? AND version = ?",
                (_now(), task_id, version),
            )
            self._conn.execute("UPDATE tasks SET status = 'approved', updated_at = ? WHERE task_id = ?",
                               (_now(), task_id))

    def record_publish(self, task_id: str, draft: Draft) -> bool:
        with self._lock, self._conn:
            if self.is_published(task_id):
                return False
            task = self._conn.execute("SELECT current_version FROM tasks WHERE task_id = ?",
                                      (task_id,)).fetchone()
            current = self.get_draft(task_id, draft.version)
            if not task or task["current_version"] != draft.version or not current or not current.approved:
                raise ValueError("只能发布已批准的当前版本")
            if current.model_dump(exclude={"approved", "approved_at", "created_at"}) != draft.model_dump(
                exclude={"approved", "approved_at", "created_at"}
            ):
                raise ValueError("发布内容与已审核版本不一致")
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO publish_records (task_id, draft_version, title, published_at) "
                "VALUES (?, ?, ?, ?)", (task_id, draft.version, draft.title, _now()),
            )
            self._conn.execute("UPDATE tasks SET status = 'published', updated_at = ? WHERE task_id = ?",
                               (_now(), task_id))
            return cursor.rowcount == 1

    def is_published(self, task_id: str) -> bool:
        return self._conn.execute("SELECT 1 FROM publish_records WHERE task_id = ?", (task_id,)).fetchone() is not None

    def publish_count(self, task_id: str) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM publish_records WHERE task_id = ?", (task_id,)).fetchone()[0]

    def get_task(self, task_id: str) -> TaskView | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            drafts = self._conn.execute("SELECT * FROM drafts WHERE task_id = ? ORDER BY version",
                                         (task_id,)).fetchall()
            pub = self._conn.execute("SELECT * FROM publish_records WHERE task_id = ?", (task_id,)).fetchone()
            brief = self.get_stage(task_id, "research")
            review = self.get_stage(task_id, "quality_review", row["current_version"])
            publish = self.get_stage(task_id, "publish", row["current_version"])
            return TaskView(
                task_id=task_id, topic=row["topic"], status=row["status"],
                current_version=row["current_version"],
                draft=self._draft_from_row(drafts[-1]) if drafts else None,
                published=pub is not None, published_version=pub["draft_version"] if pub else None,
                history=[{"version": d["version"], "title": d["title"], "approved": bool(d["approved"]),
                          "approved_at": d["approved_at"]} for d in drafts],
                request=ContentRequest.model_validate_json(row["request_json"]) if row["request_json"]
                    else ContentRequest(goal=row["topic"], topic=row["topic"]),
                profile=UserProfile.model_validate_json(row["profile_json"]) if row["profile_json"] else UserProfile(),
                trends=self.get_stage(task_id, "trends") or [],
                brief=ResearchBrief.model_validate(brief) if brief else None,
                review=ReviewReport.model_validate(review) if review else None,
                last_error=row["last_error"],
                publish_result=PublishResult.model_validate(publish) if publish else None,
            )

    @staticmethod
    def _draft_from_row(row: sqlite3.Row) -> Draft:
        data = json.loads(row["payload"]) if row["payload"] else {
            "version": row["version"], "title": row["title"], "body": row["body"],
            "images": json.loads(row["images"]), "created_at": row["created_at"],
        }
        data.update(approved=bool(row["approved"]), approved_at=row["approved_at"])
        return Draft.model_validate(data)
