"""Portable entrypoint. Resolve machine-local workspace before importing the engine."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from runtime.douyin_text.identity import env_value, state_root


def main() -> int:
    selector = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    selector.add_argument("--workspace", type=Path)
    selector.add_argument("--session-id", help="Actual host chat ID, for one update notice per conversation")
    try:
        args, remaining = selector.parse_known_args()
    except argparse.ArgumentError as exc:
        from runtime.douyin_text.messages import UsageError
        raise UsageError(str(exc)) from None
    workspace = args.workspace or env_value("WORKSPACE")
    if not workspace:
        settings = state_root() / "settings.json"
        if settings.exists():
            workspace = json.loads(settings.read_text(encoding="utf-8-sig"))["workspace"]
    workspace = Path(workspace or Path.home() / "Documents/cy-charmyee").expanduser().resolve()
    resource = Path(__file__).resolve().parent / "runtime"
    skill_root = resource.parent.parent
    if workspace == skill_root or workspace.is_relative_to(skill_root) or skill_root.is_relative_to(workspace):
        raise ValueError("工作目录应与 Skill 程序目录分开")
    os.environ["CY_WORKSPACE"] = str(workspace)
    if args.session_id:
        os.environ["CY_SESSION_ID"] = args.session_id
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(workspace / ".local/browsers")
    links = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Microsoft/WinGet/Links"
    os.environ["PATH"] = str(links) + os.pathsep + os.environ.get("PATH", "")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(resource))
    if remaining and remaining[0] in {"check-update", "update"}:
        from runtime.douyin_text.messages import ArgumentParser, emit_json
        from updates import apply_update, check_updates, notice_text
        update_parser = ArgumentParser(prog=remaining[0])
        update_parser.add_argument("--force", action="store_true")
        if remaining[0] == "update":
            update_parser.add_argument("--apply", action="store_true")
        options = update_parser.parse_args(remaining[1:])
        apply = getattr(options, "apply", False)
        result = apply_update(skill_root, state_root(), workspace) if apply else check_updates(
            skill_root, state_root(), force=options.force, notify=True)
        emit_json(result, command=remaining[0], update_notice=notice_text(result), root=workspace)
        return int(apply and result["status"] not in {"updated", "up_to_date"})
    from douyin_text.cli import main as run
    return run(remaining)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, KeyError, OSError, argparse.ArgumentError) as exc:
        from runtime.douyin_text.messages import emit_json, error_result
        emit_json(error_result(exc))
        raise SystemExit(1)
