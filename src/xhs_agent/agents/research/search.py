"""Tavily 实际检索与正文提取；结果来源由工具会话记录，不能由模型编造。"""
import ipaddress
import json
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from ...execution import SearchSettings
from ...llm import LLMConnectionError, LLMHTTPError, LLMResponseError
from ...models import Source, utcnow
from ..tools import AgentTool, ToolArgs
from .model import RetrievedDocument, SearchHit


def public_url(value):
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if parsed.scheme not in {"https", "http"} or not host or parsed.username or parsed.password:
        raise ValueError("需要无凭证的 HTTP(S) 来源地址")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise ValueError("只能访问公开来源")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return value
    if not address.is_global:
        raise ValueError("只能访问公开来源")
    return value


class TavilySearchProvider:
    def __init__(self, config: SearchSettings, *, transport=None):
        self.config, self.transport = config, transport

    def _post(self, endpoint, payload):
        try:
            with httpx.Client(timeout=self.config.timeout, trust_env=self.config.trust_env,
                              transport=self.transport, follow_redirects=False) as client:
                response = client.post(self.config.endpoint(endpoint), json=payload,
                                       headers={"Authorization": "Bearer " + self.config.key()})
        except httpx.HTTPError:
            raise LLMConnectionError("检索服务连接失败或超时") from None
        if not 200 <= response.status_code < 300:
            raise LLMHTTPError(response.status_code)
        try:
            value = response.json()
            if not isinstance(value, dict) or not isinstance(value.get("results"), list):
                raise ValueError
            return value["results"]
        except ValueError:
            raise LLMResponseError("检索服务响应格式无效") from None

    def search(self, query, limit=5):
        results = self._post("/search", {"query": query, "max_results": limit, "include_raw_content": False})
        try:
            return [SearchHit(title=row["title"], url=public_url(row["url"]), snippet=row.get("content", "")[:3000])
                    for row in results[:limit]]
        except (KeyError, TypeError, ValueError):
            raise LLMResponseError("检索结果缺少有效标题、地址或摘要") from None

    def fetch(self, url):
        public_url(url)
        results = self._post("/extract", {"urls": [url], "format": "markdown"})
        try:
            row = next(row for row in results if row["url"] == url)
            text = row["raw_content"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError
            return RetrievedDocument(title=row.get("title") or url, url=url, text=text[:40000],
                                     retrieved_at=utcnow().isoformat(), published_at=row.get("published_date"))
        except (KeyError, StopIteration, TypeError, ValueError):
            raise LLMResponseError("没有取得该来源的有效正文，请更换来源") from None


class SearchArgs(ToolArgs):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=5, ge=1, le=10)


class FetchArgs(ToolArgs):
    url: str = Field(min_length=1)


class ResearchTools:
    def __init__(self, provider):
        self.provider = provider
        self.hits, self.documents = {}, {}
        self.attempted = False

    def search(self, args):
        self.attempted = True
        hits = self.provider.search(args.query, args.limit)
        self.hits.update({public_url(hit.url): hit for hit in hits})
        return json.dumps([hit.model_dump() for hit in hits], ensure_ascii=False)

    def fetch(self, args):
        if args.url not in self.hits:
            raise PermissionError("先搜索，再读取实际结果中的地址")
        doc = self.provider.fetch(args.url)
        if doc.url != args.url:
            raise ValueError("正文地址与请求来源不一致")
        self.documents[doc.url] = doc
        return doc.model_dump_json()

    def tools(self):
        return (AgentTool("web_search", "搜索真实公开来源，返回标题、URL 和摘要；摘要不等于完整正文", SearchArgs, self.search),
                AgentTool("web_fetch", "提取本次搜索命中地址的正文，引用事实前应读取正文", FetchArgs, self.fetch))

    def validate(self, brief):
        if not self.attempted:
            raise ValueError("本次已提供检索工具，请先检索和读取正文再交付调研结果")
        requested = {source.url for source in brief.sources}
        if not requested.issubset(self.documents):
            raise ValueError("sources 只能引用本次 web_fetch 实际读取的来源")
        for item in brief.evidence:
            document = self.documents.get(item.source_url)
            if item.source_url not in requested or document is None or not item.excerpt.strip() or item.is_mock:
                raise ValueError("evidence 必须关联本次读取的真实来源，且摘录不能为空")
            if " ".join(item.excerpt.split()) not in " ".join(document.text.split()):
                raise ValueError("证据摘录必须存在于实际读取的正文中")
        brief.sources = [Source(title=self.hits[url].title, url=url, retrieved_at=self.documents[url].retrieved_at,
                                published_at=self.documents[url].published_at) for url in dict.fromkeys(source.url for source in brief.sources)]
        return brief
