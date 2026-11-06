from __future__ import annotations

import json
import unittest

from core.config import Settings
from core.llm import LLMTextDelta, LLMTurnComplete, OpenAICompatibleClient
from core.types import ToolCall


def _sse(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _tool_delta(tool_calls: list[dict]) -> bytes:
    return _sse({"choices": [{"delta": {"tool_calls": tool_calls}}]})


def _client() -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        Settings(base_url="http://example.invalid", api_key="test", model="test")
    )


class StreamParsingTests(unittest.TestCase):
    def test_text_and_tool_call_are_collected(self) -> None:
        response = [
            _sse({"choices": [{"delta": {"content": "我先查一下。"}}]}),
            _tool_delta(
                [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "Runcli", "arguments": ""},
                    }
                ]
            ),
            _tool_delta([{"index": 0, "function": {"arguments": '{"command": "git"}'}}]),
            b"data: [DONE]\n",
        ]

        events = list(_client()._consume_stream(response))

        texts = [event.text for event in events if isinstance(event, LLMTextDelta)]
        self.assertEqual(texts, ["我先查一下。"])
        complete = events[-1]
        self.assertIsInstance(complete, LLMTurnComplete)
        self.assertEqual(complete.content, "我先查一下。")
        self.assertEqual(
            complete.tool_calls, [ToolCall("call_1", "Runcli", '{"command": "git"}')]
        )

    def test_parallel_tool_calls_are_kept_separate(self) -> None:
        response = [
            _tool_delta(
                [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "fetch_web", "arguments": '{"url": "a"}'},
                    },
                    {
                        "index": 1,
                        "id": "call_2",
                        "function": {"name": "Runcli", "arguments": '{"command": "b"}'},
                    },
                ]
            ),
            b"data: [DONE]\n",
        ]

        complete = list(_client()._consume_stream(response))[-1]

        self.assertEqual(
            [(call.name, call.arguments) for call in complete.tool_calls],
            [("fetch_web", '{"url": "a"}'), ("Runcli", '{"command": "b"}')],
        )

    def test_nameless_fragment_is_merged_into_previous_call(self) -> None:
        response = [
            _tool_delta(
                [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "Runcli", "arguments": ""},
                    }
                ]
            ),
            _tool_delta(
                [
                    {
                        "index": 0,
                        "function": {
                            "arguments": '{"command": "curl", "args": ["https://x"]'
                        },
                    }
                ]
            ),
            _tool_delta([{"index": 1, "function": {"arguments": "}"}}]),
            b"data: [DONE]\n",
        ]

        complete = list(_client()._consume_stream(response))[-1]

        self.assertEqual(len(complete.tool_calls), 1)
        call = complete.tool_calls[0]
        self.assertEqual(call.name, "Runcli")
        self.assertEqual(json.loads(call.arguments), {"command": "curl", "args": ["https://x"]})

    def test_channel_tokens_are_removed_from_tool_name(self) -> None:
        response = [
            _tool_delta(
                [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {
                            "name": "fetch_web<|channel|>commentary",
                            "arguments": '{"url": "https://example.com"}',
                        },
                    }
                ]
            ),
            b"data: [DONE]\n",
        ]

        complete = list(_client()._consume_stream(response))[-1]

        self.assertEqual(complete.tool_calls[0].name, "fetch_web")

    def test_nameless_first_fragment_is_not_dropped(self) -> None:
        response = [
            _tool_delta([{"index": 0, "function": {"arguments": "{}"}}]),
            b"data: [DONE]\n",
        ]

        complete = list(_client()._consume_stream(response))[-1]

        self.assertEqual(len(complete.tool_calls), 1)
        self.assertEqual(complete.tool_calls[0].name, "")


if __name__ == "__main__":
    unittest.main()
