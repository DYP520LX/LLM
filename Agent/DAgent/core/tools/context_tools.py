"""Function-calling tool for searching compressed context archives."""

from __future__ import annotations

from pathlib import Path

from ..context import recall_context
from .registry import Tool


def tools(context_dir: str | Path) -> list[Tool]:
    root = Path(context_dir)

    def recall(text: str, id: str | None = None) -> str:
        return recall_context(root, text, archive_id=id)

    return [
        Tool(
            name="recallContext",
            description=(
                "在当前项目的 .agents/context 历史归档中检索原始上下文。"
                "当压缩摘要缺少某些历史细节时使用。text 为必需的关键词；"
                "id 可选，指定某个归档编号；不传 id 时检索全部归档。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "可选的归档编号，例如 20261022-153000。",
                    },
                    "text": {
                        "type": "string",
                        "description": "需要查找的关键词或文本内容。",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=recall,
        )
    ]
