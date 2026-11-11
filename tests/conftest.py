from xhs_agent.config import AgentConfig
import pytest

from xhs_agent.service import TaskService
from xhs_agent.agents.ideation import MockIdeationAgent


@pytest.fixture(autouse=True)
def isolate_local_environment(monkeypatch):
    """测试不读取开发者本机的模型密钥与启动文件配置。"""
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def service_factory(data_dir):
    """返回一个工厂，每次调用都新建 TaskService（模拟新进程）。"""

    def _make():
        return TaskService(data_dir, ideation_agent=MockIdeationAgent(), config=AgentConfig(ideation_backend="mock", supervisor_backend="mock"))

    return _make
