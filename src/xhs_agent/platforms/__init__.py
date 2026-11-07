"""多平台连接层；首版实现小红书浏览器图文发布。"""
from .accounts import AccountService
from .base import PlatformConnector, UnsupportedCapability
from .factory import create_platform_connector
from .models import (
    AccountIdentity, AccountRef, PlatformAccount, PlatformCapabilities, PlatformContent,
    PublishJob, PublishReceipt, PublishRequest, ValidationReport,
)
from .publishing import PlatformPublisher, PlatformPublishingService
from .storage import PlatformStore

__all__ = [
    "AccountIdentity", "AccountRef", "AccountService", "PlatformAccount", "PlatformCapabilities",
    "PlatformConnector", "PlatformContent", "PlatformPublisher", "PlatformPublishingService",
    "PlatformStore", "PublishJob", "PublishReceipt", "PublishRequest", "UnsupportedCapability",
    "ValidationReport", "create_platform_connector",
]
