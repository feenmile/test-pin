# test-pin

Judge a code change with the tests it started from.

AI coding agents sometimes make a failing test suite pass by editing, deleting or
weakening the tests instead of fixing the code. [ImpossibleBench](https://arxiv.org/abs/2510.20270)
(Zhong, Raghunathan and Carlini, 2025) measures this with tasks whose tests contradict the
specification, so any pass is a cheat. It reports that "GPT-5 cheats 54.0% of the time on
Conflicting-SWEbench", and that Claude models and Qwen3-Coder "cheat primarily (>79%) through
modifying test cases". OpenAI models used more varied tricks, often special-casing the
tests in the source code, which test-pin does not catch (see
[What it does not do](#what-it-does-not-do)).

`test-pin` runs your test command against the new code but with the **test files from the
base commit**. Edits to tests cannot make a change pass. When a test really does need to
change, a human approves the exact new content, and that approval is reviewed like any
other code.

Built by Feenmile.

## What it does

`testpin check --base main --tests "tests/**" --run "python -m pytest -q"`:

1. Finds the merge-base of `--base` and `HEAD`.
2. Lists the test files (paths matching `--tests`) that changed between the merge-base and
   `HEAD`: added, modified, deleted, renamed.
3. Creates a temporary git worktree of `HEAD`, removes every matched test file in it,
   and checks out every matched test file from the merge-base. Added tests disappear,
   modified tests revert, deleted or renamed tests come back.
4. Runs `--run` through the shell from the worktree root and reports `PASS` or `FAIL`.
5. If test files changed, reports `TEST CHANGES NEED APPROVAL` unless the approval file in
   the `HEAD` commit lists the sha256 of each changed file's new content.
6. Removes the worktree. Your checkout is never modified.

`testpin approve --base main --tests "tests/**"` shows the changed test files and asks
`Approve these test changes? [y/N]`. On yes it writes `.testpin-approved`:

```
# base: <merge-base sha>
3f5a...e9 tests/test_parser.py
deleted tests/test_old_api.py
```

It refuses to run without a terminal unless you pass `--i-am-a-human`, and it refuses if
matched test files have uncommitted changes (commit first, so you approve exactly what CI
sees). Editing an approved test again changes its hash and invalidates the approval.

Approved tests still are not run by `check`: the code is always judged by the base tests.
Approval answers a different question ("a human accepted this test change"). New and
changed tests become the base tests once the change is merged.

### Exit codes

`testpin check` (bit flags):

| Code | Meaning |
| --- | --- |
| 0 | Tests pass with the base test files, and every test change is approved |
| 1 | Tests fail with the base test files |
| 2 | Tests pass, but test changes need approval |
| 3 | Both 1 and 2 |
| 4 | Error: not a git repo, unknown ref, bad arguments, git failure |

`testpin approve`: 0 written (or nothing to approve), 1 declined at the prompt,
4 error or uncommitted test changes, 5 refused because it is not interactive and
`--i-am-a-human` was not passed.

## Quick start

Requires Python 3.10+ and git. No other dependencies.

```bash
pip install .            # from a clone of this repository; provides `testpin` and `testpin-hook`
cd your-project
testpin check --base main --tests "tests/**,**/conftest.py" --run "python -m pytest -q"
```

Without installing: `python path/to/test-pin/testpin check ...`.

Globs are matched against repository-relative paths with `/` separators. `**` crosses
directories, `*` and `?` stay within one path segment, and a glob with no `/` matches
the file name in any directory (`conftest.py`). Separate several globs with commas or
repeat `--tests`.

### GitHub Action

```yaml
on: pull_request
jobs:
  test-pin:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: <account>/test-pin@<ref>
        with:
          tests: "tests/**,**/conftest.py,pytest.ini"
          run: "pip install -e . && python -m pytest -q"
```

Inputs: `tests`, `run` (required), `base` (defaults to the pull request's base sha),
`approve-file`, `setup-python`, `python-version`. See `examples/workflow.yml`.

Protect the approval file and the workflow with a CODEOWNERS file and turn on "Require review
from Code Owners" (see `examples/CODEOWNERS`). Without that, whoever writes the change
can also write its approval.

### Claude Code hook

`testpin-hook` is a PreToolUse hook. It blocks `Edit`, `Write`, `MultiEdit` and
`NotebookEdit` on files matching the test globs, and on the approval file itself, and
tells the agent to fix the code instead. It allows an edit when:

- the approval file has `<sha256> <path>` equal to the content the edit would produce, or
- the approval file has an `allow-edit <glob>` line matching the path (a human opening a
  window for test work; `check` ignores these lines).

Add it to `.claude/settings.json` (see `examples/claude-settings.json`):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [{ "type": "command", "command": "testpin-hook --tests \"tests/**\"" }]
      }
    ]
  }
}
```

The hook fails closed: unreadable input, a missing path or missing globs block the edit.

**The hook is a speed bump, not a wall.** An agent can still change test files with shell
commands (`sed`, `git checkout`, a Python one-liner), and it can run `testpin approve
--i-am-a-human`. CI running `testpin check`, with human review of the approval file, is
the real enforcement.

## What it does not do

- **It only covers files matching your globs.** Tests, fixtures, snapshots, `conftest.py`,
  test configuration (`pytest.ini`, `setup.cfg`, `jest.config.js`) or test scripts in
  `package.json` that are not matched are not pinned. Cast the globs wide.
- **It does not stop cheating in the source code**, such as special-casing test inputs,
  catching and hiding errors, or detecting a test environment. Code review still matters.
- **The test command comes from the caller.** On `pull_request`, the workflow file and its
  `run` input come from the pull request branch, so protect `.github/` too.
- **It judges the committed `HEAD`.** Uncommitted changes are ignored, and only an approval
  file committed in `HEAD` counts.
- **It needs git** and enough history to find the merge-base (`fetch-depth: 0` in Actions).
  Submodules are not initialised in the temporary worktree.
- **The hook is bypassable**, as above. It also predicts the result of an edit to check the
  hash; if line-ending conversion changes the bytes, it compares both the raw and the
  LF-normalised content.

## Development

```bash
python -m unittest discover -s tests      # builds throwaway git repos in a temp dir
python scripts/mutation_check.py          # breaks each guard in a copy; every mutation must be killed
```

The mutation check copies the project to a temporary directory, disables one guard at a
time (the base-test restore step, the approval check, the hook, the non-interactive
refusal), and confirms the test suite fails for each, plus an unmodified control that must pass.

## License

MIT. See `LICENSE`.
