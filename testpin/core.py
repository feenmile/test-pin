"""Core logic for test-pin: git plumbing, glob matching, change detection and approvals.

Standard library only. Every git call goes through ``git()`` so failures surface as
``TestPinError`` instead of being read as empty output.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Iterable, Optional, TextIO

DEFAULT_APPROVE_FILE = ".testpin-approved"
DELETED = "deleted"
ALLOW_EDIT = "allow-edit"

# Exit codes for `testpin check` (bit flags, so 3 means "failed AND needs approval").
EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_NEEDS_APPROVAL = 2
EXIT_ERROR = 4
# Extra exit code for `testpin approve`.
EXIT_REFUSED = 5

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TestPinError(Exception):
    """A setup problem: not a git repo, unknown ref, git failure, bad arguments."""


# --------------------------------------------------------------------------- git


def git(args: list[str], cwd: str, *, input: Optional[bytes] = None) -> bytes:
    """Run git and return stdout bytes; raise TestPinError on a non-zero exit."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:  # git is not installed
        raise TestPinError("git was not found on PATH; test-pin needs git") from exc
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", errors="replace").strip()
        raise TestPinError(f"git {' '.join(args)} failed: {msg}")
    return proc.stdout


def repo_root(cwd: str) -> str:
    out = git(["rev-parse", "--show-toplevel"], cwd).decode("utf-8").strip()
    if not out:
        raise TestPinError(f"{cwd} is not inside a git work tree")
    return os.path.normpath(out)


def resolve_commit(root: str, ref: str) -> str:
    return git(["rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"], root).decode().strip()


def merge_base(root: str, a: str, b: str) -> str:
    out = git(["merge-base", a, b], root).decode().strip()
    if not out:
        raise TestPinError(f"no common ancestor between {a} and {b}")
    return out


def tree_files(root: str, commit: str) -> list[str]:
    out = git(["ls-tree", "-r", "-z", "--name-only", "--full-tree", commit], root)
    return [p.decode("utf-8", errors="surrogateescape") for p in out.split(b"\0") if p]


def blob_bytes(root: str, commit: str, path: str) -> bytes:
    return git(["cat-file", "blob", f"{commit}:{path}"], root)


def blob_exists(root: str, commit: str, path: str) -> bool:
    try:
        git(["cat-file", "-e", f"{commit}:{path}"], root)
        return True
    except TestPinError:
        return False


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------------- globs


def parse_globs(values: Iterable[str]) -> list[str]:
    """Split comma-separated glob arguments into a clean list."""
    globs: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip().replace("\\", "/")
            while part.startswith("./"):
                part = part[2:]
            part = part.lstrip("/")
            if part:
                globs.append(part)
    return globs


def glob_to_regex(pattern: str) -> str:
    """Translate a glob into a regex over repo-relative POSIX paths.

    ``**`` matches across directories, ``*`` and ``?`` stay inside one path
    segment, ``[...]`` is a character class. A pattern without ``/`` matches
    the file name in any directory (like .gitignore).
    """
    pat = pattern
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if i + 1 < n and pat[i + 1] == "*":
                i += 2
                if i < n and pat[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 2 if i + 1 < n and pat[i + 1] in "!^" else i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = pat[i + 1 : j]
                if body[:1] in ("!", "^"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    body = "".join(out)
    if "/" not in pattern:
        body = "(?:.*/)?" + body
    return "^" + body + "$"


class GlobSet:
    def __init__(self, patterns: Iterable[str], ignore_case: bool = False):
        self.patterns = list(patterns)
        flags = re.IGNORECASE if ignore_case else 0
        self._regexes = [re.compile(glob_to_regex(p), flags) for p in self.patterns]

    def matches(self, path: str) -> bool:
        path = path.replace("\\", "/")
        return any(r.match(path) for r in self._regexes)

    def __bool__(self) -> bool:
        return bool(self.patterns)


def matched_files(root: str, commit: str, globs: GlobSet, approve_file: str) -> list[str]:
    return [p for p in tree_files(root, commit) if p != approve_file and globs.matches(p)]


# ----------------------------------------------------------------------- changes


@dataclass(frozen=True)
class Change:
    """One changed test path. ``new_sha256`` is None when the path no longer exists."""

    status: str
    path: str
    new_sha256: Optional[str]

    @property
    def approval_value(self) -> str:
        return self.new_sha256 if self.new_sha256 is not None else DELETED


_STATUS_NAMES = {"A": "added", "M": "modified", "D": "deleted", "T": "type-changed"}


def changed_tests(root: str, base: str, head: str, globs: GlobSet, approve_file: str) -> list[Change]:
    """Test files that differ between ``base`` and ``head`` (added, modified, deleted, renamed)."""
    raw = git(["diff", "--name-status", "-z", "-M", "--no-ext-diff", base, head, "--"], root)
    tokens = [t.decode("utf-8", errors="surrogateescape") for t in raw.split(b"\0")]
    if tokens and tokens[-1] == "":
        tokens.pop()
    changes: dict[str, Change] = {}

    def is_test(path: str) -> bool:
        return path != approve_file and globs.matches(path)

    def add(status: str, path: str, deleted: bool) -> None:
        sha = None if deleted else sha256_hex(blob_bytes(root, head, path))
        changes[path] = Change(status, path, sha)

    i = 0
    while i < len(tokens):
        status = tokens[i]
        code = status[:1]
        if code in ("R", "C"):
            if i + 2 >= len(tokens):
                raise TestPinError(f"could not parse git diff output near {status!r}")
            old, new = tokens[i + 1], tokens[i + 2]
            i += 3
            if code == "R" and is_test(old):
                add(f"renamed to {new}", old, deleted=True)
            if is_test(new):
                add(f"renamed from {old}" if code == "R" else f"copied from {old}", new, deleted=False)
            continue
        if i + 1 >= len(tokens):
            raise TestPinError(f"could not parse git diff output near {status!r}")
        path = tokens[i + 1]
        i += 2
        if code not in _STATUS_NAMES:
            # Unmerged or unknown states: fail closed by treating them as changes.
            if is_test(path):
                add(f"unknown ({status})", path, deleted=not blob_exists(root, head, path))
            continue
        if is_test(path):
            add(_STATUS_NAMES[code], path, deleted=(code == "D"))
    return sorted(changes.values(), key=lambda c: c.path)


# --------------------------------------------------------------------- approvals


@dataclass
class Approvals:
    entries: dict[str, set[str]] = field(default_factory=dict)  # path -> {sha256 | "deleted"}
    allow_edit: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def allows(self, path: str, value: str) -> bool:
        return value in self.entries.get(path, set())


def parse_approvals(text: str) -> Approvals:
    """Parse an approval file.

    Lines: ``<sha256> <path>``, ``deleted <path>``, ``allow-edit <glob>`` (hook only),
    blank lines and ``#`` comments. Anything else is recorded as a problem and ignored.
    """
    result = Approvals()
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            result.problems.append(f"line {lineno}: expected '<sha256> <path>'")
            continue
        key, path = parts[0], parts[1].strip().replace("\\", "/")
        if key == ALLOW_EDIT:
            result.allow_edit.append(path)
        elif key == DELETED or _SHA256_RE.match(key):
            result.entries.setdefault(path, set()).add(key)
        else:
            result.problems.append(f"line {lineno}: unrecognised entry {key!r}")
    return result


def load_approvals_from_commit(root: str, commit: str, approve_file: str) -> Approvals:
    if not blob_exists(root, commit, approve_file):
        return Approvals()
    return parse_approvals(blob_bytes(root, commit, approve_file).decode("utf-8", errors="replace"))


def load_approvals_from_disk(path: str) -> Approvals:
    try:
        with open(path, "rb") as fh:
            return parse_approvals(fh.read().decode("utf-8", errors="replace"))
    except FileNotFoundError:
        return Approvals()


def evaluate_approvals(changes: list[Change], approvals: Approvals) -> tuple[list[Change], list[str]]:
    """Return (unapproved changes, notes about stale entries)."""
    unapproved: list[Change] = []
    stale: list[str] = []
    for change in changes:
        if approvals.allows(change.path, change.approval_value):
            continue
        unapproved.append(change)
        if change.path in approvals.entries:
            stale.append(f"{change.path}: approval does not match the current content (stale hash)")
    return unapproved, stale


def format_approvals(changes: list[Change], allow_edit: list[str], base: str) -> str:
    lines = [
        "# test-pin approvals. Each line approves the exact new content of one test file:",
        "#   <sha256 of new content> <path>   or   deleted <path>",
        "# Generated by `testpin approve`. A human must review every change to this file",
        "# (protect it with CODEOWNERS). Editing a test again invalidates its line.",
        f"# base: {base}",
    ]
    for glob in allow_edit:
        lines.append(f"{ALLOW_EDIT} {glob}")
    for change in changes:
        lines.append(f"{change.approval_value} {change.path}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- worktree


def _force_remove(func, path, _exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def rmtree(path: str) -> None:
    if os.path.exists(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_force_remove)
        else:
            shutil.rmtree(path, onerror=_force_remove)


def restore_base_tests(
    root: str, worktree: str, base: str, head: str, globs: GlobSet, approve_file: str
) -> tuple[int, int]:
    """Make every matched test file in ``worktree`` equal to its version at ``base``.

    Test files present at HEAD are removed first (this deletes tests that were added),
    then every matched file from ``base`` is checked out (this restores modified and
    deleted tests). Returns (restored_count, removed_count).
    """
    head_tests = matched_files(root, head, globs, approve_file)
    base_tests = matched_files(root, base, globs, approve_file)
    removed = 0
    for rel in head_tests:
        target = os.path.join(worktree, *rel.split("/"))
        if os.path.lexists(target):
            os.chmod(target, stat.S_IWRITE)
            os.remove(target)
            removed += 1
    if base_tests:
        payload = b"\0".join(p.encode("utf-8", errors="surrogateescape") for p in base_tests)
        git(
            ["--literal-pathspecs", "checkout", base, "--pathspec-from-file=-", "--pathspec-file-nul"],
            worktree,
            input=payload,
        )
    for rel in base_tests:
        target = os.path.join(worktree, *rel.split("/"))
        if not os.path.lexists(target):
            raise TestPinError(f"failed to restore base test file {rel}")
    return len(base_tests), removed


def _print(out: TextIO, text: str = "") -> None:
    out.write(text + "\n")
    out.flush()


def run_check(
    cwd: str,
    base_ref: str,
    globs: GlobSet,
    run_cmd: str,
    approve_file: str = DEFAULT_APPROVE_FILE,
    keep_worktree: bool = False,
    out: TextIO = sys.stdout,
) -> int:
    if not globs:
        raise TestPinError("--tests needs at least one glob")
    if not run_cmd.strip():
        raise TestPinError("--run needs a test command")
    root = repo_root(cwd)
    approve_file = approve_file.replace("\\", "/")
    head = resolve_commit(root, "HEAD")
    try:
        base_commit = resolve_commit(root, base_ref)
    except TestPinError as exc:
        raise TestPinError(f"unknown base ref {base_ref!r} (is the history fetched?)") from exc
    base = merge_base(root, base_commit, head)

    _print(out, f"test-pin: judging HEAD {head[:12]} with test files from {base[:12]} (merge-base with {base_ref})")
    _print(out, f"test-pin: test globs: {', '.join(globs.patterns)}")

    changes = changed_tests(root, base, head, globs, approve_file)
    if changes:
        _print(out, f"test-pin: {len(changes)} test file change(s) since base:")
        for change in changes:
            _print(out, f"  {change.status:<12} {change.path}")
    else:
        _print(out, "test-pin: no test files changed since base.")

    approvals = load_approvals_from_commit(root, head, approve_file)
    for problem in approvals.problems:
        _print(out, f"test-pin: warning: {approve_file} {problem}")
    unapproved, stale = evaluate_approvals(changes, approvals)

    if git(["status", "--porcelain"], root).strip():
        _print(out, "test-pin: note: uncommitted changes are ignored; only the HEAD commit is judged.")

    tmp = tempfile.mkdtemp(prefix="testpin-")
    worktree = os.path.join(tmp, "wt")
    added = False
    try:
        git(["worktree", "add", "--detach", "--quiet", worktree, head], root)
        added = True
        restored, removed = restore_base_tests(root, worktree, base, head, globs, approve_file)
        _print(out, f"test-pin: restored {restored} base test file(s), removed {removed} HEAD test file(s) first.")
        _print(out, f"test-pin: running: {run_cmd}")
        _print(out, "-" * 60)
        try:
            proc = subprocess.run(run_cmd, shell=True, cwd=worktree)
        except OSError as exc:
            raise TestPinError(f"could not run test command: {exc}") from exc
        out.flush()
        _print(out, "-" * 60)
        test_rc = proc.returncode
    finally:
        if keep_worktree and added:
            _print(out, f"test-pin: kept worktree at {worktree}")
        else:
            if added:
                try:
                    git(["worktree", "remove", "--force", worktree], root)
                except TestPinError:
                    pass
            rmtree(tmp)
            try:
                git(["worktree", "prune"], root)
            except TestPinError:
                pass

    code = EXIT_OK
    if test_rc == 0:
        _print(out, "RESULT: PASS (tests from base, code from HEAD)")
    else:
        _print(out, f"RESULT: FAIL (test command exited {test_rc})")
        code |= EXIT_TESTS_FAILED
    if unapproved:
        code |= EXIT_NEEDS_APPROVAL
        _print(out, f"TEST CHANGES NEED APPROVAL: {len(unapproved)} test file change(s) are not approved:")
        for change in unapproved:
            _print(out, f"  {change.status:<12} {change.path}  ({change.approval_value})")
        for note in stale:
            _print(out, f"  {note}")
        _print(out, f"  A human can review them and run `testpin approve`, then commit {approve_file}.")
    elif changes:
        _print(out, f"test-pin: all {len(changes)} test file change(s) are approved in {approve_file}.")
    return code


def dirty_test_files(root: str, globs: GlobSet, approve_file: str) -> list[str]:
    """Matched test files with uncommitted (staged, unstaged or untracked) changes."""
    paths: set[str] = set()
    for args in (["diff", "--name-only", "-z", "HEAD", "--"], ["ls-files", "--others", "--exclude-standard", "-z"]):
        for p in git(args, root).split(b"\0"):
            if p:
                paths.add(p.decode("utf-8", errors="surrogateescape"))
    return sorted(p for p in paths if p != approve_file and globs.matches(p))
