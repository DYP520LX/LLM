"""Web page tool: download HTML with curl, extract readable Markdown.

The tool deliberately does the network access in a separate ``curl`` process
(argument list, never ``shell=True``) and leaves boilerplate removal to
trafilatura, which keeps headings, paragraphs, lists, tables and links while
dropping navigation, ads and footers. Returning Markdown instead of raw HTML
keeps the context sent to the LLM small.
"""

from __future__ import annotations

import codecs
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .registry import Tool

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_CHARS = 12000
MAX_CHARS = 50000
MIN_CHARS = 500
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_META_MARKER = "\n__DAGENT_CURL_META__"
_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.IGNORECASE)
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)(\s+\"[^\"]*\")?\)")
_CURL_ERRORS = {
    1: "不支持的协议",
    5: "无法解析代理",
    6: "域名解析失败",
    7: "无法连接到服务器",
    28: "访问超时",
    35: "TLS 握手失败",
    47: "重定向次数过多",
    52: "服务器返回空响应",
    56: "接收数据失败",
    60: "HTTPS 证书校验失败",
    63: "响应内容超过大小限制",
}


class WebToolError(RuntimeError):
    """A fetch or extraction failure that should be reported to the model."""


def _find_curl() -> str | None:
    """Locate the curl executable without going through a shell.

    PowerShell aliases ``curl`` to ``Invoke-WebRequest``, but subprocesses
    bypass aliases; on Windows we additionally probe System32 in case PATH is
    incomplete.
    """

    executable = shutil.which("curl")
    if executable:
        return executable
    if os.name == "nt":
        candidate = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "curl.exe"
        )
        if candidate.is_file():
            return str(candidate)
    return None


def _normalize_url(url: str) -> str:
    candidate = str(url or "").strip()
    if not candidate:
        raise WebToolError("URL 不能为空")
    if "://" not in candidate:
        candidate = "https://" + candidate.lstrip("/")

    parsed = urlparse(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise WebToolError(f"只支持 http/https URL，收到：{parsed.scheme or '空'}")
    if not parsed.netloc:
        raise WebToolError(f"URL 缺少主机名：{candidate}")
    return candidate


def _decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _decode_html(data: bytes, content_type: str) -> str:
    """Decode HTML bytes using the declared charset, then common fallbacks."""

    candidates: list[str] = []
    header_charset = _CHARSET_RE.search(content_type or "")
    if header_charset:
        candidates.append(header_charset.group(1))

    head = data[:4096].decode("ascii", errors="ignore")
    meta_charset = _CHARSET_RE.search(head)
    if meta_charset:
        candidates.append(meta_charset.group(1))

    candidates.extend(("utf-8", "gb18030"))
    for encoding in dict.fromkeys(candidates):
        try:
            codecs.lookup(encoding)
            text = data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        return text.lstrip("\ufeff")
    return data.decode("utf-8", errors="replace").lstrip("\ufeff")


def _curl_command(curl: str, url: str, timeout: float) -> list[str]:
    return [
        curl,
        "--silent",
        "--show-error",
        "--location",
        "--max-redirs",
        str(MAX_REDIRECTS),
        "--compressed",
        "--max-time",
        f"{timeout:g}",
        "--connect-timeout",
        f"{min(timeout, 10.0):g}",
        "--max-filesize",
        str(MAX_HTML_BYTES),
        "--user-agent",
        USER_AGENT,
        "--header",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "--header",
        "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        "--write-out",
        f"{_META_MARKER}%{{http_code}}\t%{{content_type}}\t%{{url_effective}}",
        url,
    ]


def _download(url: str, timeout: float) -> tuple[bytes, int, str, str]:
    """Return ``(body, status, content_type, effective_url)`` for ``url``."""

    curl = _find_curl()
    if curl is None:
        raise WebToolError(
            "未找到 curl 程序，请先安装 curl 并确保它在 PATH 中"
            "（Windows 10/11 通常自带 C:\\Windows\\System32\\curl.exe）"
        )

    command = _curl_command(curl, url, timeout)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout + 5.0,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise WebToolError(
            f"访问网页超时（超过 {timeout:g} 秒）：{url}"
        ) from error
    except OSError as error:
        raise WebToolError(f"无法启动 curl：{error}") from error

    if completed.returncode != 0:
        detail = _decode_bytes(completed.stderr or b"").strip()
        reason = _CURL_ERRORS.get(
            completed.returncode, f"curl 退出码 {completed.returncode}"
        )
        if completed.returncode == 63:
            raise WebToolError(
                f"网页内容超过大小限制（{MAX_HTML_BYTES // (1024 * 1024)} MB）：{url}"
            )
        raise WebToolError(
            f"获取网页失败（{reason}）：{url}"
            + (f"\ncurl 输出：{detail}" if detail else "")
        )

    marker = _META_MARKER.encode("utf-8")
    if marker in completed.stdout:
        body, _, raw_meta = completed.stdout.rpartition(marker)
    else:
        body, raw_meta = completed.stdout, b""

    status = 0
    content_type = ""
    effective_url = url
    fields = _decode_bytes(raw_meta).strip().split("\t", 2)
    if fields and fields[0].isdigit():
        status = int(fields[0])
    if len(fields) > 1:
        content_type = fields[1]
    if len(fields) > 2 and fields[2].strip():
        effective_url = fields[2].strip()

    return body, status, content_type, effective_url


def _extract_title(html: str, url: str) -> str:
    try:
        import trafilatura

        metadata = trafilatura.extract_metadata(html, default_url=url)
    except Exception:  # Metadata is optional; never fail the whole tool for it.
        return ""
    title = (getattr(metadata, "title", None) or "").strip()
    return re.sub(r"\s+", " ", title)


def _extract_markdown(html: str, url: str) -> str:
    try:
        import trafilatura
    except ImportError as error:
        raise WebToolError(
            "缺少 trafilatura 依赖，请运行：python -m pip install trafilatura"
        ) from error

    options = {
        "url": url,
        "output_format": "markdown",
        "include_tables": True,
        "include_links": True,
        "include_formatting": True,
        "include_comments": False,
        "include_images": False,
        "deduplicate": True,
    }
    try:
        markdown = trafilatura.extract(html, **options)
        if not (markdown or "").strip():
            # Some pages need the more permissive extraction mode.
            markdown = trafilatura.extract(html, favor_recall=True, **options)
    except Exception as error:
        raise WebToolError(f"HTML 解析失败：{type(error).__name__}: {error}") from error

    text = (markdown or "").strip()
    if not text:
        raise WebToolError(
            "无法从该网页提取正文：页面可能是导航页、由脚本动态渲染，"
            "或内容不足以识别为文章"
        )
    return text


def _looks_like_html(content_type: str, text: str) -> bool:
    """Decide whether the response should go through HTML extraction."""

    lowered = (content_type or "").lower()
    if "html" in lowered or "xhtml" in lowered:
        return True
    if lowered.startswith(
        (
            "text/plain",
            "text/csv",
            "text/xml",
            "application/json",
            "application/xml",
            "application/xhtml",
        )
    ):
        return "html" in lowered
    head = text[:2048].lstrip().lower()
    return head.startswith(("<!doctype html", "<html", "<head", "<body"))


def _absolutize_links(markdown: str, base_url: str) -> str:
    """Make relative Markdown links absolute so the model can cite them."""

    def rewrite(match: re.Match[str]) -> str:
        href = match.group(2)
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            return match.group(0)
        title = match.group(3) or ""
        return f"[{match.group(1)}]({urljoin(base_url, href)}{title})"

    return _MD_LINK_RE.sub(rewrite, markdown)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip(), True


def fetch_web(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Fetch ``url`` and return its main content as Markdown.

    Failures are reported as ``错误：...`` strings (like the file tools) so the
    model can read them and decide what to do next.
    """

    try:
        return _fetch_web(url, max_chars)
    except WebToolError as error:
        return f"错误：{error}"


def _fetch_web(url: str, max_chars: int) -> str:
    target = _normalize_url(url)
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_CHARS
    limit = min(max(limit, MIN_CHARS), MAX_CHARS)

    body, status, content_type, effective_url = _download(target, DEFAULT_TIMEOUT)

    if status and status >= 400:
        raise WebToolError(f"网页返回 HTTP {status}：{effective_url}")
    if not body:
        raise WebToolError(f"网页返回空内容：{effective_url}")
    if len(body) > MAX_HTML_BYTES:
        raise WebToolError(
            f"网页内容超过大小限制（{MAX_HTML_BYTES // (1024 * 1024)} MB）：{effective_url}"
        )

    lowered_type = (content_type or "").lower()
    if lowered_type.startswith(
        ("image/", "audio/", "video/", "application/pdf", "application/zip")
    ):
        raise WebToolError(
            f"该 URL 不是网页而是 {content_type}，无法提取正文：{effective_url}"
        )

    html = _decode_html(body, content_type)
    if _looks_like_html(content_type, html):
        title = _extract_title(html, effective_url)
        markdown = _extract_markdown(html, effective_url)
        markdown = _absolutize_links(markdown, effective_url or target)
    else:
        # Plain text, JSON and similar endpoints are returned as-is. Running
        # trafilatura on them would report a false "cannot extract article"
        # error (for example an IP echo service).
        title = ""
        markdown = html.strip()
        if not markdown:
            raise WebToolError(f"网页返回空内容：{effective_url}")

    markdown, truncated = _truncate(markdown, limit)

    lines = [f"URL：{target}"]
    if effective_url and effective_url != target:
        lines.append(f"最终 URL：{effective_url}")
    if title:
        lines.append(f"标题：{title}")
    if status:
        lines.append(f"HTTP 状态码：{status}")
    if content_type:
        lines.append(f"内容类型：{content_type}")
    lines.append("")
    lines.append(markdown)
    if truncated:
        lines.append("")
        lines.append(
            f"[内容已截断：仅保留前 {limit} 个字符，"
            f"如需更多内容可调大 max_chars（上限 {MAX_CHARS}）]"
        )
    return "\n".join(lines)


def tools() -> list[Tool]:
    return [
        Tool(
            name="fetch_web",
            description=(
                "获取指定网页的主要内容。通过本地 curl 下载 HTML，再用 trafilatura "
                "提取正文并转换为 Markdown（保留标题、段落、列表、表格和链接，"
                "去除导航栏、广告、页脚），最后返回正文 Markdown 与原始 URL。"
                "如果 URL 返回的是 text/plain、JSON 等非 HTML 内容，则原样返回。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": (
                            "要抓取的网页 URL，支持 http/https，"
                            "省略协议时默认使用 https"
                        ),
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            "正文最大字符数，默认 12000，范围 "
                            f"{MIN_CHARS}-{MAX_CHARS}；超出部分会被截断"
                        ),
                        "minimum": MIN_CHARS,
                        "maximum": MAX_CHARS,
                    },
                },
                "required": ["url"],
            },
            handler=fetch_web,
        )
    ]
