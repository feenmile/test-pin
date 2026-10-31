"""Mutation check: break each guard in a copy of the project and confirm the suite notices.

For every mutation this script copies the project to a temporary directory, applies one
exact source edit (failing loudly if the target text is not found exactly once), runs the
unittest suite in the copy, and expects it to FAIL. A mutation the suite survives means
the tests do not actually protect that guard.

Usage:  python scripts/mutation_check.py
Exit:   0 if every mutation is killed; 1 if any survives or the unmutated control
        fails; 2 if a mutation could not be applied.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MUTATIONS = [
    ("control: unmodified copy must pass", "testpin/core.py", None, None),
    (
        "restore step disabled",
        "testpin/core.py",
        "        restored, removed = restore_base_tests(root, worktree, base, head, globs, approve_file)\n",
        "        restored, removed = 0, 0\n",
    ),
    (
        "approval check always passes",
        "testpin/core.py",
        "        if approvals.allows(change.path, change.approval_value):\n",
        "        if True:\n",
    ),
    (
        "hook allows everything",
        "testpin/hook.py",
        "    tool = event.get(\"tool_name\")\n    if tool not in EDIT_TOOLS:\n",
        "    tool = event.get(\"tool_name\")\n    if True:\n",
    ),
    (
        "approve skips the non-interactive refusal",
        "testpin/cli.py",
        "    elif not args.i_am_a_human:\n",
        "    elif False:\n",
    ),
]


def _ignore(_dir, names):
    return [n for n in names if n in (".git", "__pycache__", ".pytest_cache") or n.endswith(".egg-info")]


def _rmtree(path: str) -> None:
    def force(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=force)
    else:
        shutil.rmtree(path, onerror=force)


def run_mutation(name: str, rel: str, old, new) -> tuple[str, str]:
    tmp = tempfile.mkdtemp(prefix="testpin-mutant-")
    try:
        copy = os.path.join(tmp, "project")
        shutil.copytree(ROOT, copy, ignore=_ignore)
        target = os.path.join(copy, *rel.split("/"))
        with open(target, "rb") as fh:
            text = fh.read().decode("utf-8")
        if old is not None:
            count = text.count(old)
            if count != 1:
                return "NOT APPLIED", f"target text found {count} times in {rel}"
            with open(target, "wb") as fh:
                fh.write(text.replace(old, new).encode("utf-8"))
        env = dict(os.environ, PYTHONPATH=copy, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                              cwd=copy, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = proc.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
        # The suite's own summary is the last "Ran N tests" line and the verdict after it.
        tail = out[out.rfind("\nRan "):] if "\nRan " in out else out
        summary = re.findall(r"^(Ran \d+ tests.*|FAILED \(.*\)|OK)$", tail, re.MULTILINE)
        failed = re.findall(r"^(?:FAIL|ERROR): \S+ \((\S+)\)", out, re.MULTILINE)
        detail = "; ".join(summary) + (f" -- e.g. {failed[0]}" if failed else "")
        if old is None:
            return ("CONTROL OK" if proc.returncode == 0 else "CONTROL FAILED"), detail
        return ("KILLED" if proc.returncode != 0 else "SURVIVED"), detail
    finally:
        _rmtree(tmp)


def main() -> int:
    worst = 0
    for name, rel, old, new in MUTATIONS:
        verdict, detail = run_mutation(name, rel, old, new)
        print(f"{verdict:<12} {name}: {detail}")
        if verdict in ("SURVIVED", "CONTROL FAILED"):
            worst = max(worst, 1)
        elif verdict == "NOT APPLIED":
            worst = 2
    print("mutation check:", "all mutations killed" if worst == 0 else "PROBLEM")
    return worst


if __name__ == "__main__":
    sys.exit(main())
