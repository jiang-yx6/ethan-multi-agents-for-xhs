import pytest

from xhs_agent.service import TaskService
from xhs_agent.agents.ideation import MockIdeationAgent


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "data"


@pytest.fixture
def service_factory(data_dir):
    """返回一个工厂，每次调用都新建 TaskService（模拟新进程）。"""

    def _make():
        return TaskService(data_dir, ideation_agent=MockIdeationAgent())

    return _make
