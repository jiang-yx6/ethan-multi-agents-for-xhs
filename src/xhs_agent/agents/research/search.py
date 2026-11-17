"""Tavily 实际检索与正文提取；结果来源由工具会话记录，不能由模型编造。"""
import ipaddress
import json
import re
from threading import Lock
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
            return [SearchHit(title=row["title"], url=public_url(row["url"]), snippet=row.get("content", "")[:1000])
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
    query: str | None = Field(default=None, min_length=1, max_length=1000)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=3000, ge=256, le=12000)


def _query_terms(query: str) -> set[str]:
    """Extract deterministic Chinese n-grams and ASCII words without another model call."""
    terms = {word.lower() for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]+", query)}
    for run in re.findall(r"[\u3400-\u9fff]+", query):
        terms.update(run[index:index + 2] for index in range(max(1, len(run) - 1)))
        if len(run) <= 6:
            terms.add(run)
    return {term for term in terms if term.strip()}


def _document_blocks(text: str, block_chars: int = 1200):
    """Yield exact source ranges; long paragraphs are split without rewriting their text."""
    for paragraph in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", text, re.DOTALL):
        start, stop = paragraph.span()
        cursor = start
        while stop - cursor > block_chars:
            proposed = cursor + block_chars
            boundary = max(text.rfind(mark, cursor + block_chars // 2, proposed)
                           for mark in ("。", "！", "？", ".", "!", "?", "\n"))
            end = boundary + 1 if boundary >= cursor + block_chars // 2 else proposed
            yield cursor, end, text[cursor:end]
            cursor = end
        if cursor < stop:
            yield cursor, stop, text[cursor:stop]


def _relevant_passages(text: str, query: str, max_chars: int, max_passages: int = 5):
    terms = _query_terms(query)
    ranked = []
    for start, end, value in _document_blocks(text):
        lowered = value.lower()
        hits = sum(lowered.count(term) * max(1, len(term)) for term in terms)
        if hits:
            ranked.append((hits / max(80, len(value)), hits, start, end, value))
    selected, used = [], 0
    for score, _, start, end, value in sorted(ranked, key=lambda item: (-item[0], -item[1], item[2])):
        remaining = max_chars - used
        if remaining < 80 or len(selected) >= max_passages:
            break
        if len(value) > remaining:
            value, end = value[:remaining], start + remaining
        selected.append({"start": start, "end": end, "score": round(score, 4), "text": value})
        used += len(value)
    return sorted(selected, key=lambda item: item["start"])


class ResearchTools:
    def __init__(self, provider, *, max_search_calls: int | None = None,
                 max_fetch_calls: int | None = None):
        self.provider = provider
        self._lock = Lock()
        self.hits, self.documents = {}, {}
        self.hit_queries = {}
        self.read_ranges = {}
        config = getattr(provider, "config", None)
        configured_search_limit = getattr(config, "max_search_calls", 3)
        configured_fetch_limit = getattr(config, "max_fetch_calls", 5)
        self.max_search_calls = max_search_calls if max_search_calls is not None else configured_search_limit
        self.max_fetch_calls = max_fetch_calls if max_fetch_calls is not None else configured_fetch_limit
        self.search_calls = self.fetch_calls = 0
        self.attempted = False

    def search(self, args):
        with self._lock:
            if self.search_calls >= self.max_search_calls:
                raise LLMResponseError(
                    f"本次调研最多允许 {self.max_search_calls} 个并行 web_search 查询；"
                    "请基于已有搜索结果选择来源")
            self.search_calls += 1
            self.attempted = True
        hits = self.provider.search(args.query, args.limit)
        with self._lock:
            self.hits.update({public_url(hit.url): hit for hit in hits})
            self.hit_queries.update({public_url(hit.url): args.query for hit in hits})
        return json.dumps([hit.model_dump() for hit in hits], ensure_ascii=False)

    def fetch(self, args):
        with self._lock:
            if args.url not in self.hits:
                raise PermissionError("先搜索，再读取实际结果中的地址")
            if self.fetch_calls >= self.max_fetch_calls:
                raise LLMResponseError(
                    f"本次调研最多允许 {self.max_fetch_calls} 个并行 web_fetch 请求，已达到上限；"
                    "请基于已经读取的来源完成简报，不要继续抓取")
            self.fetch_calls += 1
            cached = self.documents.get(args.url)
        doc = cached or self.provider.fetch(args.url)
        if doc.url != args.url:
            raise ValueError("正文地址与请求来源不一致")
        with self._lock:
            self.documents[doc.url] = doc
        positional = "offset" in args.model_fields_set
        query = args.query or self.hit_queries.get(doc.url)
        if positional:
            if args.offset >= len(doc.text):
                raise ValueError("正文 offset 超过有效范围")
            end = min(len(doc.text), args.offset + args.max_chars)
            passages = [{"start": args.offset, "end": end, "score": None,
                         "text": doc.text[args.offset:end]}]
        else:
            passages = _relevant_passages(doc.text, query or "", args.max_chars)
            if not passages:
                # A small lead fallback gives the model enough context to reformulate the query.
                end = min(len(doc.text), min(args.max_chars, 1200))
                passages = [{"start": 0, "end": end, "score": 0.0, "text": doc.text[:end]}]
        with self._lock:
            ranges = sorted([*self.read_ranges.get(doc.url, []),
                             *((item["start"], item["end"]) for item in passages)])
            merged = []
            for start, stop in ranges:
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
                else:
                    merged.append((start, stop))
            self.read_ranges[doc.url] = merged
            fetch_calls = self.fetch_calls
        text = "\n\n".join(item["text"] for item in passages)
        result = {**doc.model_dump(exclude={"text"}), "text": text, "passages": passages,
                  "query": None if positional else query, "total_chars": len(doc.text),
                  "returned_chars": len(text), "context_compressed": not positional,
                  "fetch_calls_used": fetch_calls,
                  "fetch_calls_remaining": self.max_fetch_calls - fetch_calls}
        if positional:
            result.update({"offset": args.offset, "next_offset": end if end < len(doc.text) else None,
                           "truncated": end < len(doc.text)})
        return json.dumps(result, ensure_ascii=False)

    def tools(self):
        return (AgentTool("web_search", f"并行搜索真实公开来源；单轮最多 {self.max_search_calls} 个查询，返回标题、URL 和摘要", SearchArgs, self.search),
                AgentTool("web_fetch", f"并行读取搜索命中正文；单轮最多 {self.max_fetch_calls} 个请求。默认按 query 提取最多 3000 字符的相关原文片段，并返回精确 start/end", FetchArgs, self.fetch))

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
            if not any(" ".join(item.excerpt.split()) in " ".join(document.text[start:end].split())
                       for start, end in self.read_ranges.get(item.source_url, [])):
                raise ValueError("证据摘录必须存在于实际读取的正文中")
        brief.sources = [Source(title=self.hits[url].title, url=url, retrieved_at=self.documents[url].retrieved_at,
                                published_at=self.documents[url].published_at) for url in dict.fromkeys(source.url for source in brief.sources)]
        return brief
