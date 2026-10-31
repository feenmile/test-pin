"""Command line interface: `testpin check` and `testpin approve`."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from . import __version__
from .core import (
    DEFAULT_APPROVE_FILE,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REFUSED,
    GlobSet,
    TestPinError,
    changed_tests,
    dirty_test_files,
    format_approvals,
    load_approvals_from_disk,
    merge_base,
    parse_globs,
    repo_root,
    resolve_commit,
    run_check,
)

EXIT_DECLINED = 1

EXIT_CODES_HELP = """\
exit codes (check):
  0  tests pass with the base test files and every test change is approved
  1  tests fail with the base test files
  2  tests pass, but test changes need approval
  3  tests fail AND test changes need approval (1 | 2)
  4  error: not a git repo, unknown ref, bad arguments, git failure

exit codes (approve):
  0  approval file written (or nothing to approve)
  1  declined at the prompt
  4  error, or test files have uncommitted changes
  5  refused: not an interactive terminal and --i-am-a-human was not passed
"""


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors, which would collide with 'needs approval'."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise SystemExit(EXIT_ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="testpin",
        description="Judge a change with the test files from its base commit. Built by Feenmile.",
        epilog=EXIT_CODES_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"testpin {__version__}")
    parser.add_argument("-C", dest="directory", default=None, help="run as if started in this directory")
    sub = parser.add_subparsers(dest="command", parser_class=_Parser)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--base", required=True, help="base git ref (branch, tag or sha); its merge-base with HEAD is used")
        p.add_argument("--tests", required=True, action="append",
                       help="comma-separated globs for test files, e.g. 'tests/**,**/test_*.py' (repeatable)")
        p.add_argument("--approve-file", default=DEFAULT_APPROVE_FILE,
                       help=f"approval file, relative to the repo root (default {DEFAULT_APPROVE_FILE})")

    check = sub.add_parser("check", help="run the test command against HEAD with base test files",
                           epilog=EXIT_CODES_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    common(check)
    check.add_argument("--run", required=True, help="test command, run through the shell from the worktree root")
    check.add_argument("--keep-worktree", action="store_true", help="keep the temporary worktree for debugging")

    approve = sub.add_parser("approve", help="approve the current test changes (humans only)",
                             epilog=EXIT_CODES_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    common(approve)
    approve.add_argument("--i-am-a-human", action="store_true",
                         help="allow non-interactive use; CI must still require human review of the approval file")
    return parser


def _is_interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def cmd_approve(args: argparse.Namespace, cwd: str) -> int:
    globs = GlobSet(parse_globs(args.tests))
    if not globs:
        raise TestPinError("--tests needs at least one glob")
    approve_file = args.approve_file.replace("\\", "/")
    root = repo_root(cwd)
    head = resolve_commit(root, "HEAD")
    try:
        base_commit = resolve_commit(root, args.base)
    except TestPinError as exc:
        raise TestPinError(f"unknown base ref {args.base!r}") from exc
    base = merge_base(root, base_commit, head)

    dirty = dirty_test_files(root, globs, approve_file)
    if dirty:
        print("testpin approve: these test files have uncommitted changes; commit them first so the")
        print("approved content is exactly what CI will see:")
        for path in dirty:
            print(f"  {path}")
        return EXIT_ERROR

    changes = changed_tests(root, base, head, globs, approve_file)
    if not changes:
        print("testpin approve: no test files changed since base; nothing to approve.")
        return EXIT_OK

    print(f"testpin approve: {len(changes)} test file change(s) between {base[:12]} and HEAD {head[:12]}:")
    for change in changes:
        print(f"  {change.status:<12} {change.path}  ({change.approval_value})")
    print("Review the diff first, for example: git diff " + base[:12] + " HEAD -- <path>")

    if _is_interactive():
        try:
            answer = input("Approve these test changes? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("testpin approve: not approved; nothing written.")
            return EXIT_DECLINED
    elif not args.i_am_a_human:
        print("testpin approve: refusing: not an interactive terminal. Approval is a human decision.")
        print("Run it in a terminal, or pass --i-am-a-human if a person really is driving this.")
        return EXIT_REFUSED

    target = os.path.join(root, *approve_file.split("/"))
    existing = load_approvals_from_disk(target)
    content = format_approvals(changes, existing.allow_edit, base)
    os.makedirs(os.path.dirname(target) or root, exist_ok=True)
    with open(target, "wb") as fh:
        fh.write(content.encode("utf-8"))
    print(f"testpin approve: wrote {len(changes)} approval(s) to {approve_file}.")
    print("Commit that file. Require human review of it in CI (for example with CODEOWNERS).")
    return EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_ERROR
    cwd = os.path.abspath(args.directory) if args.directory else os.getcwd()
    try:
        if args.command == "check":
            return run_check(
                cwd,
                args.base,
                GlobSet(parse_globs(args.tests)),
                args.run,
                approve_file=args.approve_file,
                keep_worktree=args.keep_worktree,
            )
        return cmd_approve(args, cwd)
    except TestPinError as exc:
        print(f"testpin: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("testpin: interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
