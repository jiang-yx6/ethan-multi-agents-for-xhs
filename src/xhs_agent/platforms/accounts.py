"""账号服务把本地账号标签与真实平台身份绑定；用户画像单独保存。"""
from __future__ import annotations

import sqlite3

from .base import PlatformConnector
from .factory import create_platform_connector
from .models import AccountIdentity, AccountRef, PlatformAccount
from .storage import PlatformStore


class AccountService:
    def __init__(self, store: PlatformStore, connector_factory=create_platform_connector):
        self.store = store
        self.connector_factory = connector_factory

    def connector(self, ref: AccountRef) -> PlatformConnector:
        connector = self.connector_factory(ref.platform)
        if connector.capabilities().platform != ref.platform:
            raise ValueError("连接器与目标平台不一致")
        return connector

    def add(self, ref: AccountRef, label: str = "") -> PlatformAccount:
        self.connector(ref)  # 不为不支持的平台创建不可用记录。
        try:
            return self.store.add_account(ref, label)
        except sqlite3.IntegrityError as exc:
            raise ValueError("账号已登记，请使用 accounts list 查看") from exc

    def _remember(self, ref: AccountRef, identity: AccountIdentity) -> AccountIdentity:
        if identity.state == "authenticated":
            if not identity.user_id:
                raise ValueError("登录校验缺少平台用户 ID，不能绑定账号")
            self.store.bind_identity(ref, identity.user_id, identity.nickname)
        return identity

    def login(self, ref: AccountRef, *, timeout: int = 180) -> AccountIdentity:
        try:
            return self._remember(ref, self.connector(ref).login(self.store.profile_dir(ref), timeout=timeout))
        except (ValueError, RuntimeError, OSError):
            raise
        except Exception as exc:
            raise RuntimeError("浏览器登录未完成：" + str(exc)) from exc

    def whoami(self, ref: AccountRef) -> AccountIdentity:
        try:
            return self._remember(ref, self.connector(ref).whoami(self.store.profile_dir(ref)))
        except (ValueError, RuntimeError, OSError):
            raise
        except Exception as exc:
            raise RuntimeError("浏览器身份校验未完成：" + str(exc)) from exc
