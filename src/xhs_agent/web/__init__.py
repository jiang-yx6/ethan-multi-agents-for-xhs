"""本地单用户 Web API；依赖通过 web extra 安装。"""


def create_app(*args, **kwargs):
    from .app import create_app as factory
    return factory(*args, **kwargs)
