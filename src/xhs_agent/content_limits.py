"""跨审核与发布共用的平台文本硬限制。"""

XIAOHONGSHU_TITLE_MAX_CHARS = 20
XIAOHONGSHU_BODY_MAX_CHARS = 1000


def xiaohongshu_rendered_body(body: str, tags: list[str]) -> str:
    """按发布器的实际写入格式计算正文与话题文本。"""
    topics = ["#" + tag.lstrip("#").strip() for tag in tags]
    return body + ("\n" + " ".join(topics) if topics else "")
