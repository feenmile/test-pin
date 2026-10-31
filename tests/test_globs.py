"""Tests for glob matching and approval-file parsing."""

from __future__ import annotations

import unittest

from testpin import core


class GlobTests(unittest.TestCase):
    def check(self, pattern: str, yes: list, no: list) -> None:
        globs = core.GlobSet([pattern])
        for path in yes:
            self.assertTrue(globs.matches(path), f"{pattern} should match {path}")
        for path in no:
            self.assertFalse(globs.matches(path), f"{pattern} should not match {path}")

    def test_double_star_crosses_directories(self):
        self.check("tests/**", ["tests/a.py", "tests/x/y/z.py"], ["src/tests.py", "testsx/a.py"])

    def test_double_star_prefix(self):
        self.check("**/test_*.py", ["test_a.py", "pkg/test_a.py", "a/b/test_c.py"], ["pkg/a_test.py"])

    def test_single_star_stays_in_one_segment(self):
        self.check("src/*.test.js", ["src/a.test.js"], ["src/x/a.test.js", "a.test.js"])

    def test_bare_name_matches_in_any_directory(self):
        self.check("conftest.py", ["conftest.py", "a/b/conftest.py"], ["a/conftest.pyc"])

    def test_question_mark_and_class(self):
        self.check("t?st_[ab].py", ["test_a.py", "tost_b.py"], ["test_c.py", "t/st_a.py"])
        self.check("x[!0-9].py", ["xa.py"], ["x1.py"])

    def test_comma_separated_and_normalised(self):
        self.assertEqual(core.parse_globs(["./tests/**, **/*_test.go ,", "spec\\**"]),
                         ["tests/**", "**/*_test.go", "spec/**"])

    def test_empty_globset_matches_nothing(self):
        self.assertFalse(core.GlobSet([]).matches("tests/a.py"))


class ApprovalParsingTests(unittest.TestCase):
    def test_parse(self):
        sha = "a" * 64
        approvals = core.parse_approvals(
            f"# comment\n\n{sha} tests/a b.py\ndeleted tests/old.py\nallow-edit tests/fixtures/**\nbogus line here\n"
        )
        self.assertTrue(approvals.allows("tests/a b.py", sha))
        self.assertTrue(approvals.allows("tests/old.py", core.DELETED))
        self.assertEqual(approvals.allow_edit, ["tests/fixtures/**"])
        self.assertEqual(len(approvals.problems), 1)

    def test_short_or_uppercase_hash_is_not_an_approval(self):
        approvals = core.parse_approvals(f"{'a' * 63} tests/a.py\n{'A' * 64} tests/b.py\n")
        self.assertEqual(approvals.entries, {})
        self.assertEqual(len(approvals.problems), 2)

    def test_evaluate_reports_stale(self):
        change = core.Change("modified", "tests/a.py", "b" * 64)
        unapproved, stale = core.evaluate_approvals([change], core.parse_approvals(f"{'a' * 64} tests/a.py\n"))
        self.assertEqual(unapproved, [change])
        self.assertEqual(len(stale), 1)
        unapproved, stale = core.evaluate_approvals([change], core.parse_approvals(f"{'b' * 64} tests/a.py\n"))
        self.assertEqual((unapproved, stale), ([], []))


if __name__ == "__main__":
    unittest.main()
