from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from core.config import Settings
from core.tools import build_registry, web_tools
from core.types import ToolCall

ARTICLE_HTML = """<html><head><title>表格测试</title></head><body>
<nav><a href="/">首页导航</a> <a href="/about">关于我们</a></nav>
<div class="ad">广告位广告位广告位</div>
<article>
<h1>主标题</h1>
<p>这是第一段比较长的正文内容，用来让提取器认为这是一个真实文章页面。它包含足够多的文字。</p>
<h2>数据表</h2>
<table><thead><tr><th>名称</th><th>数量</th></tr></thead>
<tbody><tr><td>苹果</td><td>10</td></tr><tr><td>香蕉</td><td>20</td></tr></tbody></table>
<ul><li>列表项目一</li><li>列表项目二，包含<a href="/docs">文档链接</a></li></ul>
<p>这是又一段比较长的正文内容，继续补充文章长度，确保提取器不会把它当作导航或页脚。第二句话。</p>
<p>最后一段正文，再次补充内容，描述一些细节并包含<a href="https://example.com/x">外部链接</a>。</p>
</article>
<footer>版权所有 广告合作 联系我们</footer></body></html>"""


def _curl_result(
    html: str,
    *,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
    effective: str = "https://example.com/post",
) -> subprocess.CompletedProcess:
    payload = (
        html.encode("utf-8")
        + web_tools._META_MARKER.encode("utf-8")
        + f"{status}\t{content_type}\t{effective}".encode("utf-8")
    )
    return subprocess.CompletedProcess([], 0, stdout=payload, stderr=b"")


class FetchWebTests(unittest.TestCase):
    def test_registry_contains_fetch_web(self) -> None:
        registry = build_registry()
        self.assertIn("fetch_web", registry.names())
        schema = next(
            item for item in registry.schemas() if item["function"]["name"] == "fetch_web"
        )
        self.assertEqual(schema["function"]["parameters"]["required"], ["url"])

    def test_registry_uses_configured_web_timeout_and_limits(self) -> None:
        settings = Settings(
            base_url="http://127.0.0.1:9999/v1",
            api_key="mock",
            model="mock",
            web_timeout=1.5,
            web_default_max_chars=500,
            web_max_chars=600,
        )
        registry = build_registry(settings=settings)
        with mock.patch.object(
            web_tools,
            "_download",
            return_value=(b"hello", 200, "text/plain", "https://example.com/"),
        ) as download:
            result = registry.execute(
                ToolCall(
                    "call_1",
                    "fetch_web",
                    '{"url": "https://example.com/", "max_chars": 500}',
                )
            )

        self.assertTrue(result.ok)
        self.assertEqual(download.call_args.args[1], 1.5)
        self.assertIn("hello", result.content)
        schema = next(
            item for item in registry.schemas() if item["function"]["name"] == "fetch_web"
        )
        self.assertEqual(
            schema["function"]["parameters"]["properties"]["max_chars"]["maximum"],
            600,
        )

    def test_fetch_web_returns_clean_markdown(self) -> None:
        with mock.patch.object(
            web_tools.subprocess, "run", return_value=_curl_result(ARTICLE_HTML)
        ) as run:
            output = web_tools.fetch_web("https://example.com/post")

        self.assertIn("URL：https://example.com/post", output)
        self.assertIn("标题：主标题", output)
        self.assertIn("HTTP 状态码：200", output)
        self.assertIn("# 主标题", output)
        self.assertIn("| 名称 | 数量 |", output)
        self.assertIn("- 列表项目一", output)
        # Relative links are resolved against the page URL.
        self.assertIn("[文档链接](https://example.com/docs)", output)
        self.assertIn("[外部链接](https://example.com/x)", output)
        # Boilerplate is removed by trafilatura.
        self.assertNotIn("首页导航", output)
        self.assertNotIn("版权所有", output)
        # The subprocess is an argument list, never a shell string.
        command = run.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertTrue(command[0])
        self.assertIn("--silent", command)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertNotIn("--insecure", command)

    def test_fetch_web_truncates_long_content(self) -> None:
        long_html = ARTICLE_HTML.replace(
            "<p>最后一段正文", "<p>" + "很长的正文内容。" * 500 + "最后一段正文"
        )
        with mock.patch.object(
            web_tools.subprocess, "run", return_value=_curl_result(long_html)
        ):
            output = web_tools.fetch_web("https://example.com/post", max_chars=500)

        self.assertIn("内容已截断", output)
        body = output.split("\n\n", 1)[1]
        self.assertLess(len(body), 700)

    def test_fetch_web_returns_plain_text_response(self) -> None:
        with mock.patch.object(
            web_tools.subprocess,
            "run",
            return_value=_curl_result(
                "183.220.31.81",
                content_type="text/plain; charset=utf-8",
                effective="https://icanhazip.com",
            ),
        ):
            output = web_tools.fetch_web("https://icanhazip.com")

        self.assertFalse(output.startswith("错误："), output)
        self.assertIn("内容类型：text/plain; charset=utf-8", output)
        self.assertIn("183.220.31.81", output)

    def test_fetch_web_returns_json_response(self) -> None:
        payload = '{"ip": "183.220.31.81"}'
        with mock.patch.object(
            web_tools.subprocess,
            "run",
            return_value=_curl_result(
                payload,
                content_type="application/json",
                effective="https://example.com/ip",
            ),
        ):
            output = web_tools.fetch_web("https://example.com/ip")

        self.assertFalse(output.startswith("错误："), output)
        self.assertIn(payload, output)

    def test_fetch_web_detects_html_without_content_type(self) -> None:
        with mock.patch.object(
            web_tools.subprocess,
            "run",
            return_value=_curl_result(ARTICLE_HTML, content_type=""),
        ):
            output = web_tools.fetch_web("https://example.com/post")

        self.assertIn("# 主标题", output)
        self.assertIn("| 名称 | 数量 |", output)

    def test_fetch_web_reports_http_error(self) -> None:
        with mock.patch.object(
            web_tools.subprocess,
            "run",
            return_value=_curl_result("<html></html>", status=404),
        ):
            output = web_tools.fetch_web("https://example.com/missing")

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("404", output)

    def test_fetch_web_reports_timeout(self) -> None:
        with mock.patch.object(
            web_tools.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["curl"], 25),
        ):
            output = web_tools.fetch_web("https://example.com/slow")

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("超时", output)

    def test_fetch_web_reports_curl_failure(self) -> None:
        failed = subprocess.CompletedProcess(
            [], 6, stdout=b"", stderr=b"curl: (6) Could not resolve host"
        )
        with mock.patch.object(web_tools.subprocess, "run", return_value=failed):
            output = web_tools.fetch_web("https://no-such-host.invalid/")

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("域名解析失败", output)

    def test_fetch_web_rejects_non_http_url_without_running_curl(self) -> None:
        with mock.patch.object(web_tools.subprocess, "run") as run:
            output = web_tools.fetch_web("file:///C:/Windows/win.ini")

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("http", output)
        run.assert_not_called()

    def test_fetch_web_reports_empty_extraction(self) -> None:
        empty_html = "<html><head><title>空页面</title></head><body></body></html>"
        with mock.patch.object(
            web_tools.subprocess, "run", return_value=_curl_result(empty_html)
        ):
            output = web_tools.fetch_web("https://example.com/empty")

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("无法从该网页提取正文", output)

    def test_decode_html_respects_declared_charset(self) -> None:
        text = "<p>中文内容</p>"
        decoded = web_tools._decode_html(
            text.encode("gbk"), "text/html; charset=gbk"
        )
        self.assertEqual(decoded, text)

    def test_decode_html_falls_back_when_charset_is_missing(self) -> None:
        text = "<p>中文内容</p>"
        self.assertEqual(web_tools._decode_html(text.encode("utf-8"), ""), text)


if __name__ == "__main__":
    unittest.main()
