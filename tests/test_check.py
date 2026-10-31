"""End-to-end tests for `testpin check` and `testpin approve` on throwaway git repos.

Each guard is proven by a case where it must REJECT, and where possible the cheat is
first shown to fool a plain test run, so a pass here is not a pass by coincidence.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from _support import (
    ALWAYS_PASS_TEST,
    BROKEN_CALC,
    EXTRA_TEST,
    FAILING_NEW_TEST,
    REAL_TEST,
    REFACTORED_CALC,
    TEST_CMD,
    TRIVIAL_TEST,
    RepoTestCase,
)

from testpin import cli, core

FAIL = core.EXIT_TESTS_FAILED
NEEDS = core.EXIT_NEEDS_APPROVAL


class SourceOnlyChanges(RepoTestCase):
    def test_correct_source_change_passes(self):
        self.repo.write("calc.py", REFACTORED_CALC)
        self.repo.commit("refactor")
        code, out = self.repo.check()
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT: PASS", out)
        self.assertIn("no test files changed", out)
        self.assertNotIn("NEED APPROVAL", out)

    def test_broken_source_fails_with_real_result(self):
        self.repo.write("calc.py", BROKEN_CALC)
        self.repo.commit("break")
        code, out = self.repo.check()
        self.assertEqual(code, FAIL, out)
        self.assertIn("RESULT: FAIL", out)
        self.assertNotIn("NEED APPROVAL", out)

    def test_worktree_is_removed_after_check(self):
        self.repo.write("calc.py", BROKEN_CALC)
        self.repo.commit("break")
        self.repo.check()
        self.assertEqual(self.repo.worktree_count(), 1)


class WeakenedTests(RepoTestCase):
    def test_test_edited_to_always_pass_is_caught(self):
        self.repo.write("calc.py", BROKEN_CALC)
        self.repo.write("tests/test_calc.py", ALWAYS_PASS_TEST)
        self.repo.commit("break and cheat")
        # The cheat works on a plain run, so the guard is what makes the difference.
        plain_rc, plain_out = self.repo.run_shell(TEST_CMD)
        self.assertEqual(plain_rc, 0, plain_out)
        code, out = self.repo.check()
        self.assertEqual(code, FAIL | NEEDS, out)
        self.assertIn("RESULT: FAIL", out)
        self.assertIn("TEST CHANGES NEED APPROVAL", out)
        self.assertIn("modified", out)
        self.assertIn("tests/test_calc.py", out)
        # HEAD's working copy is untouched: the restore happened only in the worktree.
        self.assertEqual(self.repo.read("tests/test_calc.py"), ALWAYS_PASS_TEST)

    def test_deleted_test_is_restored_and_fails(self):
        self.repo.write("calc.py", BROKEN_CALC)
        self.repo.delete("tests/test_calc.py")
        self.repo.write("tests/test_trivial.py", TRIVIAL_TEST)  # so the plain run is not empty
        self.repo.commit("break and delete test")
        self.assertEqual(self.repo.run_shell(TEST_CMD)[0], 0)
        code, out = self.repo.check()
        self.assertBit(code, FAIL, out)
        self.assertBit(code, NEEDS, out)
        self.assertIn("deleted", out)

    def test_test_renamed_out_of_the_globs_is_restored(self):
        self.repo.write("calc.py", BROKEN_CALC)
        self.repo.git("mv", "tests/test_calc.py", "notes_test_calc.txt")
        self.repo.write("tests/test_trivial.py", TRIVIAL_TEST)
        self.repo.commit("break and hide test")
        self.assertEqual(self.repo.run_shell(TEST_CMD)[0], 0)
        code, out = self.repo.check()
        self.assertBit(code, FAIL, out)
        self.assertBit(code, NEEDS, out)
        self.assertIn("renamed to notes_test_calc.txt", out)

    def test_added_test_needs_approval(self):
        self.repo.write("tests/test_extra.py", EXTRA_TEST)
        self.repo.commit("add test")
        code, out = self.repo.check()
        self.assertEqual(code, NEEDS, out)
        self.assertIn("RESULT: PASS", out)
        self.assertIn("added", out)
        self.assertIn("tests/test_extra.py", out)

    def test_added_tests_are_not_run_by_check(self):
        # The code is judged by the base tests only; HEAD's new test (failing here) is removed.
        self.repo.write("tests/test_wrong.py", FAILING_NEW_TEST)
        self.repo.commit("add a failing test")
        self.assertNotEqual(self.repo.run_shell(TEST_CMD)[0], 0)
        code, out = self.repo.check()
        self.assertEqual(code, NEEDS, out)
        self.assertIn("RESULT: PASS", out)


class Approvals(RepoTestCase):
    def _add_extra_test(self):
        self.repo.write("tests/test_extra.py", EXTRA_TEST)
        self.repo.commit("add test")

    def test_approved_change_passes_the_approval_check(self):
        self._add_extra_test()
        code, out = self.repo.approve("--i-am-a-human")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.repo.exists(".testpin-approved"))
        self.repo.commit("approve")
        code, out = self.repo.check()
        self.assertEqual(code, 0, out)
        self.assertIn("are approved", out)
        self.assertNotIn("NEED APPROVAL", out)

    def test_approved_deletion_passes_when_source_is_fine(self):
        self.repo.delete("tests/test_calc.py")
        self.repo.write("tests/test_extra.py", EXTRA_TEST)
        self.repo.commit("replace test")
        self.assertEqual(self.repo.approve("--i-am-a-human")[0], 0)
        self.assertIn("deleted tests/test_calc.py", self.repo.read(".testpin-approved"))
        self.repo.commit("approve")
        code, out = self.repo.check()
        self.assertEqual(code, 0, out)

    def test_approval_does_not_rescue_broken_source(self):
        self.repo.write("calc.py", BROKEN_CALC)
        self.repo.write("tests/test_calc.py", ALWAYS_PASS_TEST)
        self.repo.commit("break and cheat")
        self.assertEqual(self.repo.approve("--i-am-a-human")[0], 0)
        self.repo.commit("approve")
        code, out = self.repo.check()
        self.assertEqual(code, FAIL, out)

    def test_stale_hash_is_rejected(self):
        self._add_extra_test()
        self.assertEqual(self.repo.approve("--i-am-a-human")[0], 0)
        self.repo.commit("approve")
        self.repo.write("tests/test_extra.py", EXTRA_TEST + "\n# edited after approval\n")
        self.repo.commit("edit approved test")
        code, out = self.repo.check()
        self.assertEqual(code, NEEDS, out)
        self.assertIn("stale hash", out)

    def test_hand_written_wrong_hash_is_rejected(self):
        self._add_extra_test()
        wrong = core.sha256_hex(b"something else")
        self.repo.write(".testpin-approved", f"{wrong} tests/test_extra.py\n")
        self.repo.commit("bogus approval")
        code, out = self.repo.check()
        self.assertEqual(code, NEEDS, out)

    def test_right_hash_on_the_wrong_path_is_rejected(self):
        self._add_extra_test()
        right = core.sha256_hex(EXTRA_TEST.encode())
        self.repo.write(".testpin-approved", f"{right} tests/test_other.py\n")
        self.repo.commit("misplaced approval")
        code, out = self.repo.check()
        self.assertEqual(code, NEEDS, out)

    def test_uncommitted_approval_does_not_count(self):
        self._add_extra_test()
        self.assertEqual(self.repo.approve("--i-am-a-human")[0], 0)
        code, out = self.repo.check()
        self.assertEqual(code, NEEDS, out)
        self.assertIn("uncommitted changes are ignored", out)

    def test_allow_edit_lines_do_not_approve_in_check(self):
        self._add_extra_test()
        self.repo.write(".testpin-approved", "allow-edit tests/**\n")
        self.repo.commit("allow-edit only")
        code, out = self.repo.check()
        self.assertEqual(code, NEEDS, out)

    def test_approve_refuses_non_interactive_use(self):
        self._add_extra_test()
        code, out = self.repo.approve()
        self.assertEqual(code, core.EXIT_REFUSED, out)
        self.assertFalse(self.repo.exists(".testpin-approved"))

    def test_approve_refuses_uncommitted_test_changes(self):
        self._add_extra_test()
        self.repo.write("tests/test_extra.py", EXTRA_TEST + "# not committed\n")
        code, out = self.repo.approve("--i-am-a-human")
        self.assertEqual(code, core.EXIT_ERROR, out)
        self.assertFalse(self.repo.exists(".testpin-approved"))

    def test_interactive_prompt_defaults_to_no(self):
        self._add_extra_test()
        for answer, expected, written in (("", 1, False), ("n", 1, False), ("y", 0, True)):
            with self.subTest(answer=answer):
                code = self._approve_in_process(answer)
                self.assertEqual(code, expected)
                self.assertEqual(self.repo.exists(".testpin-approved"), written)

    def test_approve_keeps_allow_edit_lines(self):
        self._add_extra_test()
        self.repo.write(".testpin-approved", "allow-edit tests/fixtures/**\n")
        self.repo.commit("allow fixtures")
        self.assertEqual(self.repo.approve("--i-am-a-human")[0], 0)
        self.assertIn("allow-edit tests/fixtures/**", self.repo.read(".testpin-approved"))

    def _approve_in_process(self, answer: str) -> int:
        argv = ["-C", self.repo.path, "approve", "--base", "main", "--tests", "tests/**"]
        env = {k: v for k, v in self.env.items() if k.startswith("GIT_")}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cli, "_is_interactive", return_value=True), \
                mock.patch("builtins.input", return_value=answer), \
                contextlib.redirect_stdout(io.StringIO()):
            return cli.main(argv)


class BaseSelection(RepoTestCase):
    def test_merge_base_ignores_later_test_changes_on_the_base_branch(self):
        self.repo.write("calc.py", REFACTORED_CALC)
        self.repo.commit("feature work")
        self.repo.git("checkout", "-q", "main")
        self.repo.write("tests/test_more.py", EXTRA_TEST)
        self.repo.commit("main moves on")
        self.repo.git("checkout", "-q", "feature")
        code, out = self.repo.check()
        self.assertEqual(code, 0, out)
        self.assertIn("no test files changed", out)

    def test_unknown_base_is_an_error(self):
        code, out = self.repo.check(base="no-such-branch")
        self.assertEqual(code, core.EXIT_ERROR, out)
        self.assertIn("unknown base ref", out)

    def test_usage_error_is_not_confused_with_needs_approval(self):
        code, out = self.repo.testpin("check", "--base", "main")
        self.assertEqual(code, core.EXIT_ERROR, out)

    def test_files_outside_the_globs_are_not_guarded(self):
        # Documented limitation: a test outside the globs is invisible to test-pin.
        self.repo.write("calc.py", BROKEN_CALC)
        self.repo.write("tests/test_calc.py", ALWAYS_PASS_TEST)
        self.repo.commit("break and cheat")
        code, out = self.repo.check(tests="spec/**")
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
