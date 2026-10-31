"""Tests for the Claude Code PreToolUse hook. Each guard is shown to block."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

from _support import EXTRA_TEST, REAL_TEST, Repo, isolated_git_env, rmtree

from testpin import core, hook

ALLOW, BLOCK = hook.ALLOW, hook.BLOCK


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="testpin-hook-")
        self.addCleanup(rmtree, self.tmp)
        self.env = isolated_git_env(self.tmp)
        self.repo = Repo(self.tmp, self.env)
        self.repo.write("calc.py", "x = 1\n")
        self.repo.write("tests/test_calc.py", REAL_TEST)

    def call(self, tool: str, tool_input: dict, tests: str = "tests/**") -> tuple[int, str]:
        event = {"hook_event_name": "PreToolUse", "tool_name": tool,
                 "tool_input": tool_input, "cwd": self.repo.path}
        err = io.StringIO()
        code = hook.main(["--tests", tests], stdin=io.StringIO(json.dumps(event)), err=err)
        return code, err.getvalue()

    def path(self, rel: str) -> str:
        return os.path.join(self.repo.path, *rel.split("/"))

    def approve(self, rel: str, content: str) -> None:
        self.repo.write(".testpin-approved", f"{core.sha256_hex(content.encode())} {rel}\n")

    # --- blocks -----------------------------------------------------------------

    def test_write_to_test_file_is_blocked(self):
        code, err = self.call("Write", {"file_path": self.path("tests/test_calc.py"), "content": "pass\n"})
        self.assertEqual(code, BLOCK)
        self.assertIn("tests/test_calc.py", err)

    def test_new_test_file_is_blocked(self):
        code, _ = self.call("Write", {"file_path": self.path("tests/test_new.py"), "content": EXTRA_TEST})
        self.assertEqual(code, BLOCK)

    def test_edit_and_multiedit_are_blocked(self):
        edit = {"file_path": self.path("tests/test_calc.py"), "old_string": "5)", "new_string": "-1)"}
        self.assertEqual(self.call("Edit", edit)[0], BLOCK)
        multi = {"file_path": self.path("tests/test_calc.py"),
                 "edits": [{"old_string": "5)", "new_string": "-1)"}]}
        self.assertEqual(self.call("MultiEdit", multi)[0], BLOCK)

    def test_notebook_edit_on_test_path_is_blocked(self):
        code, _ = self.call("NotebookEdit", {"notebook_path": self.path("tests/demo.ipynb"), "new_source": ""})
        self.assertEqual(code, BLOCK)

    def test_relative_and_dotdot_paths_are_resolved(self):
        sneaky = os.path.join("src", "..", "tests", "test_calc.py")
        self.assertEqual(self.call("Write", {"file_path": sneaky, "content": "pass\n"})[0], BLOCK)

    def test_approval_file_is_always_blocked(self):
        self.repo.write(".testpin-approved", "allow-edit **\n")
        code, err = self.call("Write", {"file_path": self.path(".testpin-approved"), "content": "allow-edit **\n"})
        self.assertEqual(code, BLOCK)
        self.assertIn("approval file", err)

    def test_stale_approved_hash_is_blocked(self):
        self.approve("tests/test_calc.py", REAL_TEST + "# v1\n")
        code, _ = self.call("Write", {"file_path": self.path("tests/test_calc.py"), "content": REAL_TEST + "# v2\n"})
        self.assertEqual(code, BLOCK)

    def test_hash_for_another_path_is_blocked(self):
        self.approve("tests/test_other.py", EXTRA_TEST)
        code, _ = self.call("Write", {"file_path": self.path("tests/test_new.py"), "content": EXTRA_TEST})
        self.assertEqual(code, BLOCK)

    def test_malformed_input_is_blocked(self):
        err = io.StringIO()
        self.assertEqual(hook.main(["--tests", "tests/**"], stdin=io.StringIO("{not json"), err=err), BLOCK)
        self.assertEqual(self.call("Edit", {"old_string": "a", "new_string": "b"})[0], BLOCK)

    def test_missing_globs_block_edit_tools(self):
        event = {"tool_name": "Write", "tool_input": {"file_path": "calc.py", "content": ""}, "cwd": self.repo.path}
        env_backup = os.environ.pop("TESTPIN_TESTS", None)
        try:
            code = hook.main([], stdin=io.StringIO(json.dumps(event)), err=io.StringIO())
        finally:
            if env_backup is not None:
                os.environ["TESTPIN_TESTS"] = env_backup
        self.assertEqual(code, BLOCK)

    # --- allows -----------------------------------------------------------------

    def test_source_files_are_allowed(self):
        self.assertEqual(self.call("Write", {"file_path": self.path("calc.py"), "content": "x = 2\n"})[0], ALLOW)

    def test_other_tools_are_allowed(self):
        self.assertEqual(self.call("Read", {"file_path": self.path("tests/test_calc.py")})[0], ALLOW)
        self.assertEqual(self.call("Bash", {"command": "echo hi"})[0], ALLOW)

    def test_paths_outside_the_repo_are_allowed(self):
        outside = os.path.join(self.tmp, "elsewhere", "tests", "test_x.py")
        self.assertEqual(self.call("Write", {"file_path": outside, "content": ""})[0], ALLOW)

    def test_write_with_approved_hash_is_allowed(self):
        self.approve("tests/test_new.py", EXTRA_TEST)
        code, _ = self.call("Write", {"file_path": self.path("tests/test_new.py"), "content": EXTRA_TEST})
        self.assertEqual(code, ALLOW)

    def test_edit_whose_result_is_approved_is_allowed(self):
        new_content = REAL_TEST.replace("(2, 3), 5)", "(2, 2), 4)")
        self.approve("tests/test_calc.py", new_content)
        edit = {"file_path": self.path("tests/test_calc.py"), "old_string": "(2, 3), 5)", "new_string": "(2, 2), 4)"}
        self.assertEqual(self.call("Edit", edit)[0], ALLOW)
        wrong = dict(edit, new_string="(2, 2), 5)")
        self.assertEqual(self.call("Edit", wrong)[0], BLOCK)

    def test_allow_edit_glob_is_allowed(self):
        self.repo.write(".testpin-approved", "allow-edit tests/fixtures/**\n")
        ok = self.call("Write", {"file_path": self.path("tests/fixtures/data.json"), "content": "{}"})
        self.assertEqual(ok[0], ALLOW)
        still_blocked = self.call("Write", {"file_path": self.path("tests/test_calc.py"), "content": "pass\n"})
        self.assertEqual(still_blocked[0], BLOCK)

    def test_hook_runs_as_a_module(self):
        event = {"tool_name": "Write", "cwd": self.repo.path,
                 "tool_input": {"file_path": self.path("tests/test_calc.py"), "content": "pass\n"}}
        proc = subprocess.run([sys.executable, "-m", "testpin.hook", "--tests", "tests/**"],
                              input=json.dumps(event).encode(), env=self.env, cwd=self.repo.path,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(proc.returncode, BLOCK, proc.stderr)
        self.assertIn(b"blocked Write", proc.stderr)


if __name__ == "__main__":
    unittest.main()
