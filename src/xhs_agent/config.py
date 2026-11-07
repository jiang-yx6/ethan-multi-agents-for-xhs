"""各入口共用的流程设置。"""
from pydantic import BaseModel, Field


class WorkflowConfig(BaseModel):
    max_auto_revisions: int = Field(default=2, ge=0, le=5)
