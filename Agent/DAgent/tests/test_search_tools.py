from __future__ import annotations

import json
import urllib.error
import unittest
from io import BytesIO
from unittest import mock

from core.tools import build_registry, search_tools
from core.types import ToolCall

SEARCH_RESPONSE = {
    "request_id": "req-1",
    "references": [
        {
            "id": 1,
            "title": "北京今日天气",
            "url": "https://www.weather.com.cn/beijing",
            "content": "北京今天晴，气温 18-27 度，适合出行。",
            "date": "2026-10-29 08:00:00",
            "type": "web",
        },
        {
            "id": 2,
            "title": "北京周末活动",
            "url": "https://example.com/events",
            "content": "本周末北京有多场展览和演出。",
            "type": "web",
        },
    ],
}


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _tool_call(query: str, **extra) -> ToolCall:
    return ToolCall(
        "call_1",
        "searchweb",
        json.dumps({"query": query, **extra}, ensure_ascii=False),
    )


class SearchToolTests(unittest.TestCase):
    def test_registry_contains_searchweb(self) -> None:
        registry = build_registry("test-key")
        self.assertIn("searchweb", registry.names())
        self.assertEqual(len(registry), 15)

    def test_search_formats_summaries_titles_and_urls(self) -> None:
        with mock.patch.object(
            search_tools.urllib.request,
            "urlopen",
            return_value=FakeResponse(SEARCH_RESPONSE),
        ) as urlopen:
            output = search_tools.search_web("北京天气", "test-key")

        self.assertIn("搜索：北京天气", output)
        self.assertIn("结果数：2", output)
        self.assertIn("1. 北京今日天气", output)
        self.assertIn("URL：https://www.weather.com.cn/beijing", output)
        self.assertIn("摘要：北京今天晴", output)
        self.assertIn("发布时间：2026-10-29 08:00:00", output)
        self.assertIn("2. 北京周末活动", output)

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://qianfan.baidubce.com/v2/ai_search/web_search",
        )
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            body["messages"], [{"role": "user", "content": "北京天气"}]
        )
        self.assertEqual(body["search_source"], "baidu_search_v2")
        self.assertEqual(
            body["resource_type_filter"], [{"type": "web", "top_k": 5}]
        )

    def test_search_accepts_full_bearer_key(self) -> None:
        with mock.patch.object(
            search_tools.urllib.request,
            "urlopen",
            return_value=FakeResponse(SEARCH_RESPONSE),
        ) as urlopen:
            search_tools.search_web("天气", "Bearer test-key")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")

    def test_registry_executes_searchweb_with_configured_key(self) -> None:
        registry = build_registry("test-key")
        with mock.patch.object(
            search_tools.urllib.request,
            "urlopen",
            return_value=FakeResponse(SEARCH_RESPONSE),
        ):
            result = registry.execute(_tool_call("北京天气", top_k=2))

        self.assertTrue(result.ok)
        self.assertIn("北京今日天气", result.content)

    def test_missing_api_key_returns_clear_error(self) -> None:
        registry = build_registry()
        with mock.patch.object(search_tools.urllib.request, "urlopen") as urlopen:
            result = registry.execute(_tool_call("北京天气"))

        self.assertIn("baidu_qianfan_api_key", result.content)
        self.assertIn("[search]", result.content)
        urlopen.assert_not_called()

    def test_empty_query_is_rejected(self) -> None:
        output = search_tools.search_web("   ", "test-key")
        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("不能为空", output)

    def test_empty_results_are_reported(self) -> None:
        with mock.patch.object(
            search_tools.urllib.request,
            "urlopen",
            return_value=FakeResponse({"references": []}),
        ):
            output = search_tools.search_web("不存在的关键词", "test-key")

        self.assertIn("没有找到相关搜索结果", output)

    def test_api_error_code_in_success_response(self) -> None:
        payload = {
            "code": 216003,
            "message": "Authentication error: Fail to parse apikey authorization",
        }
        with mock.patch.object(
            search_tools.urllib.request,
            "urlopen",
            return_value=FakeResponse(payload),
        ):
            output = search_tools.search_web("天气", "bad-key")

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("216003", output)
        self.assertIn("Authentication error", output)

    def test_http_error_is_reported(self) -> None:
        error = urllib.error.HTTPError(
            search_tools.SEARCH_ENDPOINT,
            401,
            "Unauthorized",
            {},
            BytesIO(b'{"code": 216003, "message": "invalid api key"}'),
        )
        with mock.patch.object(
            search_tools.urllib.request, "urlopen", side_effect=error
        ):
            output = search_tools.search_web("天气", "bad-key")

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("HTTP 401", output)
        self.assertIn("invalid api key", output)

    def test_network_error_is_reported(self) -> None:
        error = urllib.error.URLError("connection refused")
        with mock.patch.object(
            search_tools.urllib.request, "urlopen", side_effect=error
        ):
            output = search_tools.search_web("天气", "test-key")

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("无法连接搜索 API", output)

    def test_timeout_is_reported(self) -> None:
        with mock.patch.object(
            search_tools.urllib.request,
            "urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            output = search_tools.search_web("天气", "test-key")

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("超时", output)

    def test_invalid_json_response_is_reported(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"<html>gateway error</html>"
        with mock.patch.object(
            search_tools.urllib.request, "urlopen", return_value=response
        ):
            output = search_tools.search_web("天气", "test-key")

        self.assertTrue(output.startswith("错误："), output)
        self.assertIn("JSON", output)

    def test_long_summaries_are_truncated(self) -> None:
        payload = {
            "references": [
                {
                    "title": "长摘要",
                    "url": "https://example.com/long",
                    "content": "很长的摘要内容。" * 200,
                }
            ]
        }
        with mock.patch.object(
            search_tools.urllib.request,
            "urlopen",
            return_value=FakeResponse(payload),
        ):
            output = search_tools.search_web("长摘要", "test-key")

        summary_line = next(
            line for line in output.splitlines() if line.strip().startswith("摘要：")
        )
        self.assertLess(len(summary_line), search_tools.MAX_SNIPPET_CHARS + 20)
        self.assertTrue(summary_line.endswith("..."))


if __name__ == "__main__":
    unittest.main()
