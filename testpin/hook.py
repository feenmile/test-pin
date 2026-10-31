"""Claude Code PreToolUse hook: block file-edit tools on test files.

Reads the hook event JSON on stdin. For Edit, Write, MultiEdit and NotebookEdit on a
path that matches the test globs, it exits 2 (which blocks the tool call and shows the
message to the agent) unless the approval file allows it:

* ``<sha256> <path>`` whose hash equals the file content the edit would produce, or
* ``allow-edit <glob>`` matching the path (a human opening a window for test work).

Edits to the approval file itself are always blocked.

This is a speed bump, not a wall: an agent can still change files through shell
commands. CI running ``testpin check`` is the real enforcement.

Configure with ``--tests`` / ``--approve-file`` or the environment variables
``TESTPIN_TESTS`` / ``TESTPIN_APPROVE_FILE``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Optional, TextIO

from .core import DEFAULT_APPROVE_FILE, GlobSet, load_approvals_from_disk, parse_globs, sha256_hex

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
ALLOW = 0
BLOCK = 2


def _block(message: str, err: TextIO) -> int:
    err.write(f"test-pin: {message}\n")
    err.flush()
    return BLOCK


def _find_root(start: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return os.path.normpath(proc.stdout.decode("utf-8").strip())
    except OSError:
        pass
    return os.path.normpath(start)


def _apply_edit(text: str, old: str, new: str, replace_all: bool) -> Optional[str]:
    if old == "":
        return new if text == "" else None
    if old not in text:
        return None
    return text.replace(old, new) if replace_all else text.replace(old, new, 1)


def resulting_content(tool: str, tool_input: dict, abs_path: str) -> Optional[bytes]:
    """The bytes the tool would leave in the file, or None if they cannot be predicted."""
    if tool == "Write":
        content = tool_input.get("content")
        return content.encode("utf-8") if isinstance(content, str) else None
    if tool not in ("Edit", "MultiEdit"):
        return None
    try:
        with open(abs_path, "r", encoding="utf-8", newline="") as fh:
            text: Optional[str] = fh.read()
    except FileNotFoundError:
        text = ""
    except (OSError, UnicodeDecodeError):
        return None
    edits = tool_input.get("edits") if tool == "MultiEdit" else [tool_input]
    if not isinstance(edits, list) or not edits:
        return None
    for edit in edits:
        if not isinstance(edit, dict) or text is None:
            return None
        old, new = edit.get("old_string"), edit.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return None
        text = _apply_edit(text, old, new, bool(edit.get("replace_all")))
    return text.encode("utf-8") if text is not None else None


def evaluate(event: dict, tests: list[str], approve_file: str, err: TextIO) -> int:
    tool = event.get("tool_name")
    if tool not in EDIT_TOOLS:
        return ALLOW
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return _block(f"{tool}: missing tool_input; blocking to be safe.", err)
    raw_path = tool_input.get("notebook_path" if tool == "NotebookEdit" else "file_path")
    if not isinstance(raw_path, str) or not raw_path:
        return _block(f"{tool}: missing file path; blocking to be safe.", err)

    cwd = event.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    root = _find_root(cwd)
    abs_path = os.path.realpath(os.path.join(cwd, raw_path))
    root_real = os.path.realpath(root)
    try:
        rel = os.path.relpath(abs_path, root_real)
    except ValueError:  # different drive on Windows
        return ALLOW
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return ALLOW
    rel = rel.replace(os.sep, "/")

    ignore_case = os.name == "nt"
    approve_rel = approve_file.replace("\\", "/").lstrip("/")
    same = rel.lower() == approve_rel.lower() if ignore_case else rel == approve_rel
    if same:
        return _block(
            f"{approve_rel} is the human approval file; agents may not edit it. "
            "A human runs `testpin approve`.", err)

    globs = GlobSet(tests, ignore_case=ignore_case)
    if not globs.matches(rel):
        return ALLOW

    approvals = load_approvals_from_disk(os.path.join(root, *approve_rel.split("/")))
    if approvals.allow_edit and GlobSet(approvals.allow_edit, ignore_case=ignore_case).matches(rel):
        return ALLOW

    content = resulting_content(tool, tool_input, abs_path)
    if content is not None:
        candidates = {sha256_hex(content), sha256_hex(content.replace(b"\r\n", b"\n"))}
        allowed = approvals.entries.get(rel, set())
        if ignore_case and not allowed:
            for path, values in approvals.entries.items():
                if path.lower() == rel.lower():
                    allowed = values
        if candidates & allowed:
            return ALLOW

    return _block(
        f"blocked {tool} on test file {rel}. Test files are pinned: fix the code, not the test. "
        f"If the test itself is wrong, ask a human to approve the change (`testpin approve`, "
        f"or an `allow-edit` line in {approve_rel}).", err)


def main(argv: Optional[list[str]] = None, stdin: Optional[TextIO] = None, err: Optional[TextIO] = None) -> int:
    parser = argparse.ArgumentParser(prog="testpin-hook", description="Claude Code PreToolUse hook for test-pin.")
    parser.add_argument("--tests", action="append", default=None,
                        help="comma-separated test globs (default: $TESTPIN_TESTS)")
    parser.add_argument("--approve-file", default=os.environ.get("TESTPIN_APPROVE_FILE", DEFAULT_APPROVE_FILE))
    args = parser.parse_args(argv)
    err = err or sys.stderr
    stdin = stdin or sys.stdin

    tests = parse_globs(args.tests or [os.environ.get("TESTPIN_TESTS", "")])
    try:
        event = json.loads(stdin.read())
        if not isinstance(event, dict):
            raise ValueError("event is not an object")
    except (ValueError, OSError) as exc:
        return _block(f"could not read hook input ({exc}); blocking to be safe.", err)
    if not tests:
        if event.get("tool_name") in EDIT_TOOLS:
            return _block("no test globs configured (--tests or TESTPIN_TESTS); blocking to be safe.", err)
        return ALLOW
    try:
        return evaluate(event, tests, args.approve_file, err)
    except Exception as exc:  # fail closed
        return _block(f"hook error ({exc!r}); blocking to be safe.", err)


if __name__ == "__main__":
    sys.exit(main())
