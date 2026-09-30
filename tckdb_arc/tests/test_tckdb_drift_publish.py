"""Exercise tools/tckdb_drift_publish.sh against a bare remote and a fake ``gh``.

Covers the branch-safety rules (never wipe human commits, skip a push that would
change nothing, lease-protected push otherwise) and the issue/PR fallbacks. No
network: the "remote" is a local bare repo and ``gh`` only records its arguments.
"""

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import _drift_fixture as fx

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "tckdb_drift_publish.sh"
BRANCH = "chore/tckdb-drift-schemas-0.58-client-0.98"
SHA_1 = "1" * 40
SHA_2 = "2" * 40
GH_SHIM = """#!/bin/bash
echo "$@" >> "$FAKE/gh.log"
case "$1 $2" in
  "label list") cat "$FAKE/labels" 2>/dev/null ;;
  "issue list") cat "$FAKE/issues" 2>/dev/null ;;
  "issue view") cat "$FAKE/issue_body" 2>/dev/null ;;
  "pr list") if [[ " $* " == *" --head "* ]]; then cat "$FAKE/pr_head" 2>/dev/null; else cat "$FAKE/pr_all" 2>/dev/null; fi ;;
  "pr view") cat "$FAKE/pr_comments" 2>/dev/null ;;
  "pr create") [ -e "$FAKE/pr_create_fails" ] && exit 1 ;;
esac
exit 0
"""

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(cwd, *args, **kw):
    env = {**os.environ, "GIT_AUTHOR_NAME": "h", "GIT_AUTHOR_EMAIL": "h@x", "GIT_COMMITTER_NAME": "h", "GIT_COMMITTER_EMAIL": "h@x"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env, **kw).stdout.strip()


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.remote = tmp / "remote.git"
        self.fake = tmp / "fake"
        self.fake.mkdir()
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(GH_SHIM)
        (bin_dir / "python").write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
        for f in ("gh", "python"):
            (bin_dir / f).chmod(0o755)
        self.bin = bin_dir
        seed = tmp / "seed"
        seed.mkdir()
        fx.build(seed)  # synthetic pins (0.54 / 0.95): the suite never depends on the real ones
        for rel in ("tools/tckdb_drift.py", "tools/tckdb_drift_publish.sh"):  # code under test, not data
            dest = seed / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / rel, dest)
        git(seed, "init", "-q", "-b", "main")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "base")
        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(seed, "push", "-q", str(self.remote), "main")
        self.runs = 0

    def set(self, name, text):
        (self.fake / name).write_text(text)

    def run(self, sha=SHA_2, result="pass", drift="true", summary="```\n1 passed\n```\n"):
        self.runs += 1
        work = self.tmp / f"work{self.runs}"
        git(self.tmp, "clone", "-q", str(self.remote), str(work))
        (self.tmp / "since.md").write_text("# Changes\nsome text\n")
        (self.tmp / "summary.md").write_text(summary)
        (self.fake / "gh.log").write_text("")
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE": str(self.fake),
            "PUSH_URL": str(self.remote),
            "GH_TOKEN": "t", "REPO": "o/r", "RUN_URL": "http://run",
            "DRIFT": drift, "RESULT": result, "SHA": sha,
            "SCHEMAS": "0.58.0", "CLIENT": "0.98.0", "SCHEMAS_LINE": "0.58", "CLIENT_LINE": "0.98",
            "OLD_SCHEMAS": "0.54.0",
            "SINCE_FILE": str(self.tmp / "since.md"), "SUMMARY_FILE": str(self.tmp / "summary.md"),
        }
        proc = subprocess.run(["bash", str(work / "tools" / "tckdb_drift_publish.sh")], cwd=work, env=env,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        return proc

    def gh_log(self):
        return (self.fake / "gh.log").read_text()

    def remote_branch(self):
        out = git(self.tmp, "ls-remote", "--heads", str(self.remote), f"refs/heads/{BRANCH}")
        return out.split()[0] if out else None


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_first_run_pushes_a_bot_branch_opens_the_pr_and_dispatches_ci(env):
    env.run()
    tip = env.remote_branch()
    assert tip
    assert git(env.remote, "log", "-1", "--format=%ae", tip) == "41898282+github-actions[bot]@users.noreply.github.com"
    changed = set(git(env.remote, "diff", "--name-only", "main", tip).split())
    assert ".github/workflows/ci.yml" not in changed
    assert "tckdb-pin.toml" in changed
    log = env.gh_log()
    assert "pr create" in log and "workflow run ci.yml --ref " + BRANCH in log


def test_unchanged_tree_is_not_pushed_or_redispatched_but_the_pr_is_updated(env):
    env.run()
    tip = env.remote_branch()
    env.set("pr_head", "7\n")
    env.run()
    assert env.remote_branch() == tip
    log = env.gh_log()
    assert "pr edit 7" in log and "workflow run" not in log and "pr create" not in log


def test_branch_with_human_commits_is_never_overwritten(env):
    env.run(sha=SHA_1)
    human = env.tmp / "human"
    git(env.tmp, "clone", "-q", "-b", BRANCH, str(env.remote), str(human))
    (human / "README.md").write_text((human / "README.md").read_text() + "\nhuman note\n")
    git(human, "commit", "-qam", "my fix")
    git(human, "push", "-q", "origin", BRANCH)
    tip = env.remote_branch()
    env.set("pr_head", "7\n")
    env.run(sha=SHA_2)
    assert env.remote_branch() == tip
    log = env.gh_log()
    assert "pr comment 7" in log and "--bump --sha" in log and "workflow run" not in log


def test_bot_only_stale_branch_is_updated_with_a_lease(env):
    env.run(sha=SHA_1)
    old = env.remote_branch()
    env.set("pr_head", "7\n")
    env.run(sha=SHA_2)
    new = env.remote_branch()
    assert new and new != old
    assert SHA_2 in git(env.remote, "show", f"{new}:tckdb-pin.toml")
    assert "workflow run ci.yml" in env.gh_log()


def test_pr_creation_failure_after_the_push_opens_a_ready_to_adopt_issue(env):
    env.set("pr_create_fails", "")
    env.run()
    assert env.remote_branch()
    assert "issue create --title TCKDB drift: schemas 0.58 / client 0.98 is ready to adopt" in env.gh_log()


def test_failing_tests_open_one_issue_per_line_pair_and_comment_only_on_change(env):
    env.run(result="fail", summary="```\nFAILED tests/a.py::t\n1 failed\n```\n")
    assert "issue create --title TCKDB drift: schemas 0.58 / client 0.98 breaks the adapter" in env.gh_log()
    assert env.remote_branch() is None
    env.set("issues", "5\n")
    env.set("issue_body", "body <!-- tckdb-drift-state:stale -->")
    env.run(result="fail", summary="```\nFAILED tests/a.py::t\n1 failed\n```\n")
    log = env.gh_log()
    assert "issue edit 5" in log and "issue comment 5" in log  # marker differs from the stored one

    # Same state as stored (marker = hash of versions, result, failing tests only): no comment.
    blob = "0.58.0\n0.98.0\nfail\nFAILED tests/a.py::t\n"
    marker = hashlib.sha256(blob.encode()).hexdigest()[:16]
    env.set("issue_body", f"body <!-- tckdb-drift-state:{marker} -->")
    env.run(result="fail", summary="```\nFAILED tests/a.py::t\n5 failed in 3.1s\n```\n")
    log = env.gh_log()
    assert "issue edit 5" in log and "issue comment" not in log


def test_install_failure_is_titled_install_failed(env):
    env.run(result="install-failed", summary="pip blew up\n")
    assert "TCKDB drift: schemas 0.58 / client 0.98 install failed" in env.gh_log()


def test_no_drift_closes_open_drift_issues(env):
    env.set("issues", "5\n6\n")
    env.run(drift="false")
    log = env.gh_log()
    assert "issue close 5" in log and "issue close 6" in log


def test_a_passing_bump_closes_drift_issues_and_marks_older_prs_superseded(env):
    env.set("issues", "5\n")
    env.set("pr_all", "3:chore/tckdb-drift-schemas-0.56-client-0.96\n")
    env.run()
    log = env.gh_log()
    assert "issue close 5" in log
    assert "pr comment 3" in log and "Superseded" in log
