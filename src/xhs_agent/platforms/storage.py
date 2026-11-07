"""独立的平台账号和发布作业库；提交前原子占用作业，重启不会再次点击。"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from .models import AccountRef, PlatformAccount, PublishJob, PublishReceipt, PublishRequest


class PlatformStore:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.root / "platforms.db"), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS platform_accounts (
                platform TEXT NOT NULL, account_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(platform, account_id)
            );
            CREATE TABLE IF NOT EXISTS platform_publish_jobs (
                job_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, draft_version INTEGER NOT NULL,
                platform TEXT NOT NULL, account_id TEXT NOT NULL,
                request TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT,
                UNIQUE(task_id, draft_version, platform, account_id)
            );
        """)

    def close(self) -> None:
        self.conn.close()

    def add_account(self, ref: AccountRef, label: str = "") -> PlatformAccount:
        account = PlatformAccount(**ref.model_dump(), label=label)
        with self.conn:
            self.conn.execute("INSERT INTO platform_accounts VALUES (?, ?, ?)",
                              (ref.platform, ref.account_id, account.model_dump_json()))
        return account

    def account(self, ref: AccountRef) -> PlatformAccount:
        row = self.conn.execute("SELECT payload FROM platform_accounts WHERE platform=? AND account_id=?",
                                (ref.platform, ref.account_id)).fetchone()
        if not row:
            raise ValueError(f"账号未登记：{ref.platform}/{ref.account_id}")
        return PlatformAccount.model_validate_json(row["payload"])

    def accounts(self) -> list[PlatformAccount]:
        return [PlatformAccount.model_validate_json(r[0]) for r in self.conn.execute(
            "SELECT payload FROM platform_accounts ORDER BY platform, account_id")]

    def bind_identity(self, ref: AccountRef, user_id: str, nickname: str) -> None:
        # 条件更新防止两个登录进程把同一个本地账号绑定到不同平台用户。
        with self.conn:
            account = self.account(ref)
            original = account.model_dump_json()
            if account.user_id and account.user_id != user_id:
                raise ValueError("浏览器登录账号与已绑定身份不同，请为另一个账号使用新的 account_id")
            account.user_id, account.nickname = user_id, nickname
            cursor = self.conn.execute(
                "UPDATE platform_accounts SET payload=? WHERE platform=? AND account_id=? AND payload=?",
                (account.model_dump_json(), ref.platform, ref.account_id, original),
            )
            if not cursor.rowcount:
                raise RuntimeError("账号绑定发生并发冲突，请重试身份校验")

    def profile_dir(self, ref: AccountRef) -> Path:
        self.account(ref)
        root = self.root / "accounts"
        target = root / ref.platform / ref.account_id / "browser"
        if not target.resolve().is_relative_to(self.root):
            raise ValueError("浏览器会话目录越界")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def ensure_job(self, request: PublishRequest) -> PublishJob:
        key = f"{request.task_id}:{request.draft_version}:{request.account.platform}:{request.account.account_id}"
        job_id = hashlib.sha256(key.encode()).hexdigest()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO platform_publish_jobs VALUES (?, ?, ?, ?, ?, ?, 'ready', NULL)",
                (job_id, request.task_id, request.draft_version, request.account.platform,
                 request.account.account_id, request.model_dump_json()),
            )
        job = self.job(job_id)
        if job.request != request:
            raise ValueError("发布作业已绑定其他内容或账号身份，不能覆盖")
        return job

    def job(self, job_id: str) -> PublishJob:
        row = self.conn.execute("SELECT * FROM platform_publish_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("发布作业不存在")
        return PublishJob(job_id=job_id, request=PublishRequest.model_validate_json(row["request"]),
                          state=row["state"],
                          receipt=PublishReceipt.model_validate_json(row["receipt"]) if row["receipt"] else None)

    def find_job(self, task_id: str, draft_version: int, ref: AccountRef) -> PublishJob | None:
        row = self.conn.execute(
            "SELECT job_id FROM platform_publish_jobs WHERE task_id=? AND draft_version=? AND platform=? AND account_id=?",
            (task_id, draft_version, ref.platform, ref.account_id),
        ).fetchone()
        return self.job(row[0]) if row else None

    def jobs(self, task_id: str) -> list[PublishJob]:
        return [self.job(r[0]) for r in self.conn.execute(
            "SELECT job_id FROM platform_publish_jobs WHERE task_id=? ORDER BY draft_version", (task_id,))]

    def claim(self, job_id: str) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE platform_publish_jobs SET state='submitting', receipt=NULL "
                "WHERE job_id=? AND state IN ('ready', 'failed')", (job_id,),
            )
            return cursor.rowcount == 1

    def finish(self, job_id: str, receipt: PublishReceipt) -> None:
        with self.conn:
            self.conn.execute("UPDATE platform_publish_jobs SET state=?, receipt=? WHERE job_id=? AND state<>'published'",
                              (receipt.state, receipt.model_dump_json(), job_id))
