"""Composition root for the Textual interface."""

from __future__ import annotations

import argparse
import sys

from core.bootstrap import create_agent
from core.config import load_settings
from core.errors import ConfigError
from tui.app import DAgentApp


def main() -> None:
    parser = argparse.ArgumentParser(description="DAgent Textual TUI")
    parser.add_argument(
        "-c",
        "--config",
        help="配置文件路径（默认：当前项目目录/config.ini，与 .agents 同级）",
    )
    args = parser.parse_args()

    try:
        settings = load_settings(args.config)
    except ConfigError as error:
        raise SystemExit(f"配置错误：{error}") from error

    agent = create_agent(settings)
    for note in agent.startup_notes:
        print(f"警告：{note}", file=sys.stderr)

    try:
        DAgentApp(agent, settings).run()
    finally:
        agent.close()


if __name__ == "__main__":
    main()
