"""平台连接器只负责确定性的 IO，不理解或执行 Skill 指令。"""
from pathlib import Path
from typing import Protocol

from .models import (
    AccountIdentity, PlatformCapabilities, PlatformContent, PublishJob,
    PublishReceipt, PublishRequest, ValidationReport,
)


class UnsupportedCapability(ValueError):
    pass


class PlatformConnector(Protocol):
    def capabilities(self) -> PlatformCapabilities: ...

    def login(self, profile_dir: Path, *, timeout: int = 180) -> AccountIdentity: ...

    def whoami(self, profile_dir: Path) -> AccountIdentity: ...

    def validate(self, content: PlatformContent) -> ValidationReport: ...

    def submit(self, request: PublishRequest, profile_dir: Path) -> PublishReceipt: ...

    def create_draft(self, request: PublishRequest, profile_dir: Path) -> PublishReceipt: ...

    def reconcile(self, job: PublishJob, profile_dir: Path) -> PublishReceipt: ...
