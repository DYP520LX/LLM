"""Calendar-related tools implemented in local Python code."""

from __future__ import annotations

from .registry import Tool


def is_leap_year(year: int) -> str:
    """Return whether ``year`` is a leap year."""

    year = int(year)
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return f"{year} 年是闰年。" if leap else f"{year} 年不是闰年。"


def tools() -> list[Tool]:
    return [
        Tool(
            name="is_leap_year",
            description="判断一个年份是否为闰年，由 Agent 内部代码计算。",
            parameters={
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "要判断的年份，例如 2024",
                    }
                },
                "required": ["year"],
            },
            handler=is_leap_year,
        )
    ]
