"""Web 元数据持久化；每次访问使用独立连接，后台作业可跨线程执行。"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from pydantic import SecretStr

from ..models import UserProfile, utcnow
from ..workflow import WorkflowConflictError
from .credentials import CredentialCipher


class ConflictError(WorkflowConflictError):
    pass


def now() -> str:
    return utcnow().isoformat()


class WebRepository:
    def __init__(self, data_dir: Path):
        self.root = data_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "web.db"
        self.cipher = CredentialCipher(self.root)
        with self.connection() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS profiles (
                    profile_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS credentials (scope TEXT PRIMARY KEY, encrypted BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                    resource_key TEXT NOT NULL, task_id TEXT, kind TEXT NOT NULL, params TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_resource ON jobs(resource_key)
                    WHERE status IN ('queued', 'running');
                CREATE INDEX IF NOT EXISTS task_jobs ON jobs(task_id, created_at);
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS media (
                    media_id TEXT PRIMARY KEY, filename TEXT NOT NULL, path TEXT NOT NULL,
                    content_type TEXT NOT NULL
                );
            """)
            conn.execute("INSERT OR IGNORE INTO profiles VALUES ('default', ?, ?)",
                         (UserProfile().model_dump_json(), now()))

    @contextmanager
    def execution_lock(self):
        handle = (self.root / ".web.lock").open("a+b")
        try:
            handle.seek(0, 2)
            if not handle.tell():
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("该数据目录已有 Web 服务运行，请使用现有服务或选择其他 data-dir") from None
            yield
        finally:
            handle.close()

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def profiles(self) -> list[dict]:
        with self.connection() as conn:
            return [{"profile_id": r["profile_id"], "profile": json.loads(r["payload"]), "updated_at": r["updated_at"]}
                    for r in conn.execute("SELECT * FROM profiles ORDER BY profile_id='default' DESC, updated_at DESC")]

    def profile(self, profile_id: str) -> UserProfile:
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM profiles WHERE profile_id=?", (profile_id,)).fetchone()
        if not row:
            raise KeyError("画像不存在")
        return UserProfile.model_validate_json(row[0])

    def save_profile(self, profile: UserProfile, profile_id: str | None = None) -> str:
        identifier = profile_id or uuid.uuid4().hex
        with self.connection() as conn:
            conn.execute("INSERT INTO profiles VALUES (?, ?, ?) ON CONFLICT(profile_id) DO UPDATE SET "
                         "payload=excluded.payload, updated_at=excluded.updated_at",
                         (identifier, profile.model_dump_json(), now()))
        return identifier

    def setting(self, role: str = "ideation") -> dict | None:
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM settings WHERE key=?", (role,)).fetchone()
        return json.loads(row[0]) if row else None

    def delete_setting(self, role: str) -> None:
        # 只撤销该角色的网页覆盖；已有加密凭据保留，但跟随启动配置时不读取它们。
        with self.connection() as conn:
            conn.execute("DELETE FROM settings WHERE key=?", (role,))

    def has_api_key(self, scope: str) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM credentials WHERE scope=?", (scope,)).fetchone() is not None

    def api_key(self, scope: str) -> SecretStr | None:
        with self.connection() as conn:
            row = conn.execute("SELECT encrypted FROM credentials WHERE scope=?", (scope,)).fetchone()
        return self.cipher.decrypt(scope, bytes(row[0])) if row else None

    def save_setting(self, payload: dict, *, scope: str | None = None,
                     api_key: SecretStr | None = None, clear_api_key: bool = False, role: str = "ideation") -> None:
        # 元数据和凭据更新处于同一事务；加密失败时不提交新配置。
        encrypted = self.cipher.encrypt(scope, api_key) if api_key is not None and scope is not None else None
        public = {key: value for key, value in payload.items() if key not in {"api_key", "clear_api_key"}}
        with self.connection() as conn:
            if scope is not None:
                if clear_api_key:
                    conn.execute("DELETE FROM credentials WHERE scope=?", (scope,))
                elif encrypted is not None:
                    conn.execute("INSERT INTO credentials VALUES (?, ?) ON CONFLICT(scope) DO UPDATE SET encrypted=excluded.encrypted",
                                 (scope, encrypted))
            conn.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                         (role, json.dumps(public, ensure_ascii=False)))

    @staticmethod
    def _job(row) -> dict | None:
        if not row:
            return None
        data = dict(row)
        data["params"] = json.loads(data["params"])
        data["result"] = json.loads(data["result"]) if data["result"] else None
        return data

    def job(self, job_id: str) -> dict:
        with self.connection() as conn:
            result = self._job(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())
        if not result:
            raise KeyError("后台作业不存在")
        return result

    def request_job(self, request_id: str, fingerprint: str) -> dict | None:
        with self.connection() as conn:
            job = self._job(conn.execute("SELECT * FROM jobs WHERE request_id=?", (request_id,)).fetchone())
        if job and job["fingerprint"] != fingerprint:
            raise ConflictError("该请求 ID 已用于其他操作或内容")
        return job

    def latest(self, task_id: str | None = None, resource: str | None = None) -> dict | None:
        column, value = ("task_id", task_id) if task_id is not None else ("resource_key", resource)
        with self.connection() as conn:
            return self._job(conn.execute(f"SELECT * FROM jobs WHERE {column}=? ORDER BY created_at DESC LIMIT 1",
                                           (value,)).fetchone())

    def active(self, resource: str) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM jobs WHERE resource_key=? AND status IN ('queued','running')",
                                (resource,)).fetchone() is not None

    def enqueue(self, *, request_id: str, fingerprint: str, resource: str, task_id: str | None,
                kind: str, params: dict) -> tuple[dict, bool]:
        identifier = uuid.uuid4().hex
        try:
            with self.connection() as conn:
                conn.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, ?, ?)",
                             (identifier, request_id, fingerprint, resource, task_id, kind,
                              json.dumps(params, ensure_ascii=False), now(), now()))
        except sqlite3.IntegrityError:
            existing = self.request_job(request_id, fingerprint)
            if existing:
                return existing, False
            raise ConflictError("该任务或账号正在执行其他操作，请等待完成") from None
        return self.job(identifier), True

    def start(self, job_id: str) -> bool:
        with self.connection() as conn:
            return conn.execute("UPDATE jobs SET status='running', updated_at=? WHERE job_id=? AND status='queued'",
                                (now(), job_id)).rowcount == 1

    def finish(self, job_id: str, *, result: dict | None = None, error: str | None = None) -> None:
        with self.connection() as conn:
            conn.execute("UPDATE jobs SET status=?, result=?, error=?, updated_at=? WHERE job_id=?",
                         ("failed" if error else "succeeded", json.dumps(result, ensure_ascii=False) if result else None,
                          error, now(), job_id))

    def recover(self) -> list[dict]:
        with self.connection() as conn:
            rows = [self._job(row) for row in conn.execute("SELECT * FROM jobs WHERE status IN ('queued','running')")]
            conn.execute("UPDATE jobs SET status='interrupted', error='服务已中断，请检查任务状态后手动重试', "
                         "updated_at=? WHERE status IN ('queued','running')", (now(),))
        return rows

    def event(self, job_id: str, payload: dict) -> None:
        with self.connection() as conn:
            conn.execute("INSERT INTO events(job_id,payload,created_at) VALUES (?,?,?)",
                         (job_id, json.dumps(payload, ensure_ascii=False), now()))

    def events(self, job_id: str) -> list[dict]:
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM events WHERE job_id=? ORDER BY event_id DESC LIMIT 60", (job_id,)).fetchall()
        return [{"event_id": r["event_id"], "created_at": r["created_at"], **json.loads(r["payload"])} for r in reversed(rows)]

    def add_media(self, media_id: str, filename: str, path: Path, content_type: str) -> dict:
        with self.connection() as conn:
            conn.execute("INSERT INTO media VALUES (?,?,?,?)", (media_id, filename, str(path), content_type))
        return {"media_id": media_id, "filename": filename, "url": f"/api/media/{media_id}"}

    def media(self, media_id: str) -> dict:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM media WHERE media_id=?", (media_id,)).fetchone()
        if not row:
            raise KeyError("媒体不存在")
        result = dict(row)
        path = Path(result["path"]).resolve()
        if not path.is_relative_to((self.root / "media").resolve()) or not path.is_file():
            raise ValueError("媒体文件不可用")
        return result
