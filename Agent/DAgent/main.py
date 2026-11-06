"""Composition root for the Textual interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.bootstrap import create_agent
from core.config import ensure_config, load_settings
from core.errors import ConfigError
from core.sessions import SessionStore
from tui.app import DAgentApp
from tui.screens import ConfigCreatedApp


def main() -> None:
    parser = argparse.ArgumentParser(description="DAgent Textual TUI")
    parser.add_argument(
        "-c",
        "--config",
        help="配置文件路径（默认：当前项目目录/config.ini，与 .agents 同级）",
    )
    args = parser.parse_args()

    try:
        config_path, created = ensure_config(args.config)
    except ConfigError as error:
        raise SystemExit(f"配置错误：{error}") from error

    if created:
        resolved = Path(config_path).expanduser().resolve()
        print(f"已创建配置文件：{resolved}", file=sys.stderr)
        print("请修改其中的 mock 配置后重新启动 DAgent。", file=sys.stderr)
        ConfigCreatedApp(resolved).run()
        return

    try:
        settings = load_settings(config_path)
    except ConfigError as error:
        raise SystemExit(f"配置错误：{error}") from error

    project_root = Path.cwd()
    sessions_path = Path(settings.sessions_dir).expanduser()
    if not sessions_path.is_absolute():
        sessions_path = project_root / sessions_path
    session_store = SessionStore(sessions_path)

    agent = create_agent(settings, project_root=project_root, load_mcp=False)
    for note in agent.startup_notes:
        print(f"警告：{note}", file=sys.stderr)

    try:
        DAgentApp(
            agent,
            settings,
            project_root=project_root,
            session_store=session_store,
            load_mcp=True,
        ).run()
    finally:
        agent.close()


if __name__ == "__main__":
    main()
