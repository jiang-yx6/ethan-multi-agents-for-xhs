"""本地业务数据存储（SQLite）：任务、草稿版本、发布记录。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import Draft, ImageRef, TaskView


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """业务数据仓储。

    与 LangGraph 的检查点（``checkpoints.db``）分开，存放：
    - ``tasks``：任务与状态
    - ``drafts``：草稿版本（版本管理、批准状态）
    - ``publish_records``：本地发布记录（防止重复发布）
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    images TEXT NOT NULL,
                    approved INTEGER NOT NULL DEFAULT 0,
                    approved_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, version)
                );

                CREATE TABLE IF NOT EXISTS publish_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL UNIQUE,
                    draft_version INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    published_at TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- 任务 ----
    def create_task(self, task_id: str, topic: str) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (task_id, topic, status, current_version, created_at, updated_at)"
                " VALUES (?, ?, 'pending', 0, ?, ?)",
                (task_id, topic, now, now),
            )
            self._conn.commit()

    def update_status(self, task_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (status, _now(), task_id),
            )
            self._conn.commit()

    # ---- 草稿 ----
    def save_draft(self, task_id: str, draft: Draft) -> None:
        """保存新版本草稿；同时使该任务所有旧版本的批准失效。"""
        now = _now()
        images_json = json.dumps(
            [img.model_dump(mode="json") for img in draft.images], ensure_ascii=False
        )
        with self._lock:
            # 关键：修改（生成新版本）后，旧批准一律失效。
            self._conn.execute(
                "UPDATE drafts SET approved = 0, approved_at = NULL WHERE task_id = ?",
                (task_id,),
            )
            self._conn.execute(
                "INSERT INTO drafts (task_id, version, title, body, images, approved, approved_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, 0, NULL, ?)",
                (task_id, draft.version, draft.title, draft.body, images_json, now),
            )
            self._conn.execute(
                "UPDATE tasks SET current_version = ?, status = 'pending_review', updated_at = ? WHERE task_id = ?",
                (draft.version, now, task_id),
            )
            self._conn.commit()

    def approve_draft(self, task_id: str, version: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE drafts SET approved = 1, approved_at = ? WHERE task_id = ? AND version = ?",
                (_now(), task_id, version),
            )
            self._conn.execute(
                "UPDATE tasks SET status = 'approved', updated_at = ? WHERE task_id = ?",
                (_now(), task_id),
            )
            self._conn.commit()

    # ---- 发布 ----
    def record_publish(self, task_id: str, draft: Draft) -> bool:
        """写入发布记录；若该任务已发布则返回 False 且不重复写入。"""
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM publish_records WHERE task_id = ?", (task_id,)
            ).fetchone()
            if exists:
                return False
            self._conn.execute(
                "INSERT INTO publish_records (task_id, draft_version, title, published_at)"
                " VALUES (?, ?, ?, ?)",
                (task_id, draft.version, draft.title, _now()),
            )
            self._conn.execute(
                "UPDATE tasks SET status = 'published', updated_at = ? WHERE task_id = ?",
                (_now(), task_id),
            )
            self._conn.commit()
            return True

    def is_published(self, task_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM publish_records WHERE task_id = ?", (task_id,)
        ).fetchone()
        return row is not None

    def publish_count(self, task_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM publish_records WHERE task_id = ?", (task_id,)
        ).fetchone()
        return int(row["c"])

    # ---- 读取 ----
    def get_task(self, task_id: str) -> TaskView | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        draft_rows = self._conn.execute(
            "SELECT * FROM drafts WHERE task_id = ? ORDER BY version ASC", (task_id,)
        ).fetchall()
        pub = self._conn.execute(
            "SELECT * FROM publish_records WHERE task_id = ?", (task_id,)
        ).fetchone()
        latest = self._draft_from_row(draft_rows[-1]) if draft_rows else None
        history = [
            {
                "version": d["version"],
                "title": d["title"],
                "approved": bool(d["approved"]),
                "approved_at": d["approved_at"],
            }
            for d in draft_rows
        ]
        return TaskView(
            task_id=row["task_id"],
            topic=row["topic"],
            status=row["status"],
            current_version=row["current_version"],
            draft=latest,
            published=pub is not None,
            published_version=pub["draft_version"] if pub is not None else None,
            history=history,
        )

    @staticmethod
    def _draft_from_row(row: sqlite3.Row) -> Draft:
        images = [ImageRef.model_validate(img) for img in json.loads(row["images"])]
        return Draft(
            version=row["version"],
            title=row["title"],
            body=row["body"],
            images=images,
            approved=bool(row["approved"]),
            approved_at=datetime.fromisoformat(row["approved_at"]) if row["approved_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )
