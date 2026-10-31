"""Helpers that build throwaway git repositories for the test suite."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GOOD_CALC = "def add(a, b):\n    return a + b\n"
REFACTORED_CALC = "def add(a, b):\n    return sum((a, b))\n"
BROKEN_CALC = "def add(a, b):\n    return a - b\n"

REAL_TEST = (
    "import unittest\n"
    "import calc\n\n\n"
    "class AddTest(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(calc.add(2, 3), 5)\n"
)
ALWAYS_PASS_TEST = (
    "import unittest\n\n\n"
    "class AddTest(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertTrue(True)\n"
)
EXTRA_TEST = (
    "import unittest\n"
    "import calc\n\n\n"
    "class ExtraTest(unittest.TestCase):\n"
    "    def test_zero(self):\n"
    "        self.assertEqual(calc.add(0, 0), 0)\n"
)
TRIVIAL_TEST = (
    "import unittest\n\n\n"
    "class TrivialTest(unittest.TestCase):\n"
    "    def test_truth(self):\n"
    "        self.assertTrue(True)\n"
)
FAILING_NEW_TEST = (
    "import unittest\n"
    "import calc\n\n\n"
    "class WrongTest(unittest.TestCase):\n"
    "    def test_wrong(self):\n"
    "        self.assertEqual(calc.add(1, 1), 3)\n"
)

TEST_CMD = f'"{sys.executable}" -m unittest discover -s tests -q'
TEST_GLOBS = "tests/**"


def _force_remove(func, path, _exc):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def rmtree(path: str) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:
        shutil.rmtree(path, onerror=_force_remove)


def isolated_git_env(tmp: str) -> dict:
    """Environment that keeps the user's git config (hooks, signing, autocrlf) out of the tests."""
    cfg = os.path.join(tmp, "empty-gitconfig")
    open(cfg, "wb").close()
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_GLOBAL=cfg,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
        GIT_TERMINAL_PROMPT="0",
    )
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["PYTHONPATH"] = PROJECT_ROOT
    return env


class Repo:
    def __init__(self, tmp: str, env: dict):
        self.path = os.path.join(tmp, "repo")
        os.makedirs(self.path)
        self.env = env
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        proc = subprocess.run(["git", *args], cwd=self.path, env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise AssertionError(f"git {args} failed: {proc.stderr.decode(errors='replace')}")
        return proc.stdout.decode("utf-8", errors="replace")

    def write(self, rel: str, text: str) -> None:
        target = os.path.join(self.path, *rel.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(text.encode("utf-8"))

    def read(self, rel: str) -> str:
        with open(os.path.join(self.path, *rel.split("/")), "rb") as fh:
            return fh.read().decode("utf-8")

    def exists(self, rel: str) -> bool:
        return os.path.exists(os.path.join(self.path, *rel.split("/")))

    def delete(self, rel: str) -> None:
        os.remove(os.path.join(self.path, *rel.split("/")))

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def run(self, args: list, stdin: bytes = b"") -> tuple[int, str]:
        proc = subprocess.run(args, cwd=self.path, env=self.env, input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return proc.returncode, proc.stdout.decode("utf-8", errors="replace")

    def run_shell(self, command: str) -> tuple[int, str]:
        proc = subprocess.run(command, shell=True, cwd=self.path, env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return proc.returncode, proc.stdout.decode("utf-8", errors="replace")

    def testpin(self, *args: str) -> tuple[int, str]:
        return self.run([sys.executable, "-m", "testpin", "-C", self.path, *args])

    def check(self, base: str = "main", tests: str = TEST_GLOBS, run: str = TEST_CMD, *extra: str) -> tuple[int, str]:
        return self.testpin("check", "--base", base, "--tests", tests, "--run", run, *extra)

    def approve(self, *extra: str, base: str = "main", tests: str = TEST_GLOBS) -> tuple[int, str]:
        return self.testpin("approve", "--base", base, "--tests", tests, *extra)

    def worktree_count(self) -> int:
        return sum(1 for line in self.git("worktree", "list", "--porcelain").splitlines()
                   if line.startswith("worktree "))


class RepoTestCase(unittest.TestCase):
    """Each test gets a repo whose `main` branch has calc.py and one real test."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testpin-suite-")
        self.addCleanup(rmtree, self.tmp)
        self.env = isolated_git_env(self.tmp)
        self.repo = Repo(self.tmp, self.env)
        self.repo.write("calc.py", GOOD_CALC)
        self.repo.write("tests/test_calc.py", REAL_TEST)
        self.repo.commit("base")
        self.repo.git("checkout", "-q", "-b", "feature")

    def assertBit(self, code: int, bit: int, output: str) -> None:
        self.assertTrue(code & bit, f"expected exit bit {bit} in {code}\n{output}")

    def assertNoBit(self, code: int, bit: int, output: str) -> None:
        self.assertFalse(code & bit, f"did not expect exit bit {bit} in {code}\n{output}")
