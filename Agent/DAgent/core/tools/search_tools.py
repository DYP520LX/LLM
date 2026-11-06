"""Web search tool backed by Baidu Qianfan's Baidu Search API.

The API key comes from ``config.ini`` (``[search] baidu_qianfan_api_key``) and
is injected at composition time, never hard-coded here. Results are formatted
as compact text with the summary, title and URL of every reference so the
model can answer with up-to-date information and cite its sources.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from .registry import Tool

SEARCH_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"
DEFAULT_TIMEOUT = 20.0
DEFAULT_TOP_K = 5
MAX_TOP_K = 10
MAX_SNIPPET_CHARS = 400
_MAX_ERROR_BODY = 500
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class SearchToolError(RuntimeError):
    """A configuration, network or API failure reported to the model."""


def _clean_text(value: object) -> str:
    """Collapse whitespace and drop control characters from API text."""

    text = "" if value is None else str(value)
    text = _CONTROL_CHARS.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_api_key(api_key: str) -> str:
    key = (api_key or "").strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    if not key:
        raise SearchToolError(
            "未配置百度千帆搜索 API Key。请在当前项目目录的 config.ini "
            "的 [search] 段设置 "
            "baidu_qianfan_api_key，例如：\n"
            "[search]\n"
            "baidu_qianfan_api_key = bce-v3/ALTAK-..."
        )
    return key


def _api_error_text(raw: bytes) -> str:
    """Extract a useful message from an error response without leaking secrets."""

    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return "（响应中没有错误详情）"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:_MAX_ERROR_BODY]
    if isinstance(data, dict):
        message = data.get("message") or data.get("msg") or data.get("error_msg")
        code = data.get("code") or data.get("error_code")
        if message and code is not None:
            return f"code={code}，{message}"
        if message:
            return str(message)
        if data.get("error"):
            return str(data["error"])
    return text[:_MAX_ERROR_BODY]


def _request_search(
    question: str, api_key: str, top_k: int, timeout: float
) -> list[dict]:
    payload = {
        "messages": [{"role": "user", "content": question}],
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": top_k}],
    }
    request = urllib.request.Request(
        SEARCH_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = _api_error_text(error.read())
        hints = {
            401: "API Key 无效或未授权",
            403: "API Key 没有百度搜索权限",
            429: "请求过于频繁，已触发限流",
        }
        hint = hints.get(error.code, "请求失败")
        raise SearchToolError(
            f"{hint}（HTTP {error.code}）：{detail}"
        ) from error
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, TimeoutError):
            raise SearchToolError(
                f"搜索 API 请求超时（超过 {timeout:g} 秒）"
            ) from error
        raise SearchToolError(f"无法连接搜索 API：{reason}") from error
    except TimeoutError as error:
        raise SearchToolError(
            f"搜索 API 请求超时（超过 {timeout:g} 秒）"
        ) from error
    except OSError as error:
        raise SearchToolError(f"搜索请求失败：{error}") from error

    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as error:
        raise SearchToolError(f"搜索 API 返回了无法解析的 JSON：{error}") from error
    if not isinstance(data, dict):
        raise SearchToolError("搜索 API 返回格式异常：预期 JSON 对象")

    code = data.get("code")
    if code not in (None, 0, "0"):
        message = data.get("message") or "未知错误"
        raise SearchToolError(f"搜索 API 返回错误（code={code}）：{message}")

    references = data.get("references")
    if references is None and isinstance(data.get("data"), dict):
        references = data["data"].get("references")
    if not isinstance(references, list):
        raise SearchToolError("搜索 API 返回格式异常：缺少 references 字段")
    return references


def _format_results(question: str, references: list[dict]) -> str:
    if not references:
        return f"搜索：{question}\n没有找到相关搜索结果。"

    lines = [f"搜索：{question}", f"结果数：{len(references)}", ""]
    for index, reference in enumerate(references, start=1):
        if not isinstance(reference, dict):
            continue
        title = _clean_text(reference.get("title")) or "（无标题）"
        url = _clean_text(reference.get("url"))
        summary = _clean_text(reference.get("content") or reference.get("snippet"))
        published = _clean_text(reference.get("date"))
        if len(summary) > MAX_SNIPPET_CHARS:
            summary = summary[:MAX_SNIPPET_CHARS].rstrip() + "..."

        lines.append(f"{index}. {title}")
        if url:
            lines.append(f"   URL：{url}")
        if published:
            lines.append(f"   发布时间：{published}")
        if summary:
            lines.append(f"   摘要：{summary}")
        lines.append("")

    return "\n".join(lines).strip()


def search_web(
    query: str,
    api_key: str,
    top_k: int = DEFAULT_TOP_K,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Search the web and return summaries with source titles and URLs."""

    try:
        question = _clean_text(query)
        if not question:
            raise SearchToolError("搜索关键词 query 不能为空")
        key = _normalize_api_key(api_key)
        try:
            limit = int(top_k)
        except (TypeError, ValueError):
            limit = DEFAULT_TOP_K
        limit = min(max(limit, 1), MAX_TOP_K)

        references = _request_search(question, key, limit, timeout)
        return _format_results(question, references)
    except SearchToolError as error:
        return f"错误：{error}"


def tools(api_key: str = "") -> list[Tool]:
    def handler(query: str, top_k: int = DEFAULT_TOP_K) -> str:
        return search_web(query, api_key, top_k=top_k)

    return [
        Tool(
            name="searchweb",
            description=(
                "使用百度千帆搜索 API 检索互联网上的最新信息。"
                "当问题涉及实时新闻、近期事件、最新数据或你不确定的事实时使用。"
                "返回搜索摘要以及每条参考来源的标题和 URL，便于引用来源。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或需要查询的问题",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": (
                            f"返回的参考结果数量，默认 {DEFAULT_TOP_K}，"
                            f"范围 1-{MAX_TOP_K}"
                        ),
                        "minimum": 1,
                        "maximum": MAX_TOP_K,
                    },
                },
                "required": ["query"],
            },
            handler=handler,
        )
    ]
