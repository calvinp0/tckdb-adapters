#!/usr/bin/env bash
# Publish step of .github/workflows/tckdb-drift.yml: open or update the bump PR, or
# the issue that explains why there is no PR. Kept out of the workflow so it can be
# exercised with a bare remote and a fake `gh` (tckdb_arc/tests/test_tckdb_drift_publish.py).
#
# Environment (all set by the workflow):
#   GH_TOKEN REPO RUN_URL DRIFT(true|false)
#   when DRIFT=true: RESULT(pass|fail|install-failed) SCHEMAS CLIENT SHA SCHEMAS_LINE
#                    CLIENT_LINE OLD_SCHEMAS SINCE_FILE SUMMARY_FILE
#   optional: PUSH_URL (default: https://x-access-token:$GH_TOKEN@github.com/$REPO.git)
set -euo pipefail

BOT_NAME="github-actions[bot]"
BOT_EMAIL="41898282+github-actions[bot]@users.noreply.github.com"
ISSUE_PREFIX="TCKDB drift: "
LABEL=tckdb-drift

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

labels="$(gh label list --limit 200 --json name -q '.[].name' 2>/dev/null || true)"
label_args=()
if grep -qx "$LABEL" <<<"$labels"; then label_args=(--label "$LABEL"); fi

open_issue_numbers() {  # $1 = title prefix
  gh issue list --state open --limit 200 --json number,title \
    -q ".[] | select(.title | startswith(\"$1\")) | .number"
}

close_drift_issues() {  # $1 = reason
  local n
  for n in $(open_issue_numbers "$ISSUE_PREFIX"); do
    gh issue close "$n" --comment "$1"
  done
}

if [ "$DRIFT" != true ]; then
  close_drift_issues "No TCKDB drift as of ${RUN_URL}; closing."
  exit 0
fi

SHA7="${SHA:0:7}"
PAIR="schemas ${SCHEMAS_LINE} / client ${CLIENT_LINE}"
BRANCH="chore/tckdb-drift-schemas-${SCHEMAS_LINE}-client-${CLIENT_LINE}"
BUMP_CMD="python tools/tckdb_drift.py --bump --sha $SHA --schemas $SCHEMAS --client $CLIENT"
PUSH_URL="${PUSH_URL:-https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git}"
# The --since output carries its own headings; nest them under this page's sections.
SINCE="$(head -c 40000 "$SINCE_FILE" | sed -E 's/^#{1,6}[[:space:]]+/#### /')"
SUMMARY="$(cat "$SUMMARY_FILE")"

# Create the issue for this line pair, or update the open one (title and body);
# comment only when the versions or the failing tests changed since the last body.
upsert_issue() {  # $1 kind (breaks the adapter|install failed|is ready to adopt), $2 body file
  local title="${ISSUE_PREFIX}${PAIR} $1" n old state
  state="$(printf '%s\n%s\n%s\n%s\n' "$SCHEMAS" "$CLIENT" "$RESULT" \
           "$(grep -E '^(FAILED|ERROR) ' <<<"$SUMMARY" || true)" | sha256sum | cut -c1-16)"
  printf '\n<!-- tckdb-drift-state:%s -->\n' "$state" >> "$2"
  n="$(open_issue_numbers "${ISSUE_PREFIX}${PAIR} " || true)"
  n="${n%%$'\n'*}"
  if [ -n "$n" ]; then
    old="$(gh issue view "$n" --json body -q .body)"
    gh issue edit "$n" --title "$title" --body-file "$2"
    if ! grep -qF "tckdb-drift-state:${state}" <<<"$old"; then
      gh issue comment "$n" --body "Updated for TCKDB ${SHA7} (schemas ${SCHEMAS}, client ${CLIENT}): ${RUN_URL}"
    fi
  else
    gh issue create --title "$title" --body-file "$2" "${label_args[@]}" \
      || gh issue create --title "$title" --body-file "$2"
  fi
}

ready_issue() {  # $1 = why there is no PR
  cat > "$tmp/issue.md" <<BODY
TCKDB ${SHA7} (schemas ${SCHEMAS}, client ${CLIENT}) passes the adapter's tests, but $1

Run: ${RUN_URL}

To adopt it by hand: \`${BUMP_CMD}\`, read the contract diff below, then open a PR.

### \`python -m tckdb_schemas.contract --since ${OLD_SCHEMAS}\`
${SINCE}
BODY
  upsert_issue "is ready to adopt" "$tmp/issue.md"
}

if [ "$RESULT" != pass ]; then
  if [ "$RESULT" = install-failed ]; then
    kind="install failed"; desc="could not be installed with the adapter's pins"
  else
    kind="breaks the adapter"; desc="does not work with the adapter's tests"
  fi
  cat > "$tmp/issue.md" <<BODY
TCKDB ${SHA7} (schemas ${SCHEMAS}, client ${CLIENT}) ${desc}.

Run: ${RUN_URL}

### Failing summary
${SUMMARY}

### \`python -m tckdb_schemas.contract --since ${OLD_SCHEMAS}\`
${SINCE}

To reproduce locally: \`${BUMP_CMD}\`, install both packages from TCKDB ${SHA}, then run the suite.
Read the contract diff above (CLAUDE.md) and fix the adapter to conform before moving the pins.
BODY
  upsert_issue "$kind" "$tmp/issue.md"
  exit 0
fi

# ---- tests passed: build the bump branch ---------------------------------
TITLE="Track TCKDB: schemas ${SCHEMAS} / client ${CLIENT} (TCKDB ${SHA7})"
cat > "$tmp/pr.md" <<BODY
Automated bump by \`tckdb-drift.yml\` (run: ${RUN_URL}).

- tckdb-schemas ${SCHEMAS}, tckdb-client ${CLIENT}, TCKDB ${SHA}
- \`tools/tckdb_drift.py --bump\` moved the pyproject bounds (\`tckdb_arc\` and \`tckdb_core\`), \`TARGET_SCHEMAS_LINE\`, \`tckdb-pin.toml\`, the README install lines and version sentences, and the adapter patch version.

### What the bot already did
It ran the full test suite against the new packages with the target already moved, and it passed. It did not read the contract change: that is your job. Read the \`--since\` changelog below (and \`python -m tckdb_schemas.contract --print\`), decide whether the adapter handles what moved, then merge or close. CI on this PR re-runs the suite.

${SUMMARY}

### \`python -m tckdb_schemas.contract --since ${OLD_SCHEMAS}\`
${SINCE}

### Before merging (CLAUDE.md)
- [ ] I read the \`--since\` changelog above.
- [ ] Every changed route, field or refusal code is handled by the adapter or judged irrelevant.
- [ ] No producer convention was defaulted to fit a new required field (refuse to build the block instead).
BODY

git config user.name "$BOT_NAME"
git config user.email "$BOT_EMAIL"
BASE_SHA="$(git rev-parse HEAD)"
python tools/tckdb_drift.py --bump --sha "$SHA" --schemas "$SCHEMAS" --client "$CLIENT"
git switch -C "$BRANCH"
git add tckdb_arc/pyproject.toml tckdb_core/pyproject.toml tckdb_core/tckdb_core/testing/contract.py tckdb_arc/README.md tckdb-pin.toml README.md
git diff --cached --quiet || git commit -q -m "$TITLE"

pushed=false
remote_sha="$(git ls-remote --heads "$PUSH_URL" "refs/heads/${BRANCH}" | cut -f1)"
human_commits=false
if [ -n "$remote_sha" ]; then
  git fetch -q "$PUSH_URL" "+refs/heads/${BRANCH}:refs/remotes/drift/${BRANCH}"
  if [ "$(git rev-parse "${remote_sha}^{tree}")" = "$(git rev-parse 'HEAD^{tree}')" ]; then
    echo "remote ${BRANCH} already has this tree; not pushing"
  else
    # Commits on the branch that are not on main: any not made by the bot stop the push.
    others="$(git log --format='%ae %ce' "${BASE_SHA}..${remote_sha}" | grep -vxF "$BOT_EMAIL $BOT_EMAIL" || true)"
    if [ -n "$others" ]; then
      human_commits=true
    elif git push -q --force-with-lease="refs/heads/${BRANCH}:${remote_sha}" "$PUSH_URL" "HEAD:refs/heads/${BRANCH}"; then
      pushed=true
    else
      ready_issue "the workflow could not update ${BRANCH} (push failed)."
      exit 0
    fi
  fi
elif git push -q "$PUSH_URL" "HEAD:refs/heads/${BRANCH}"; then
  pushed=true
else
  ready_issue "the workflow could not push ${BRANCH} (check the repository's Actions permissions)."
  exit 0
fi

pr="$(gh pr list --head "$BRANCH" --state open --json number -q '.[0].number // empty')"

if [ "$human_commits" = true ]; then
  # Someone added commits to the bot branch: leave it alone and say so once per TCKDB commit.
  msg="TCKDB ${SHA7} (schemas ${SCHEMAS}, client ${CLIENT}) is newer than this branch, but the branch has commits not made by the bot, so it was not updated. To adopt the newer state: \`${BUMP_CMD}\`.<!-- tckdb-drift-newer:${SHA} -->"
  if [ -n "$pr" ]; then
    seen="$(gh pr view "$pr" --json comments -q '.comments[].body' || true)"
    if ! grep -qF "tckdb-drift-newer:${SHA}" <<<"$seen"; then
      gh pr comment "$pr" --body "$msg"
    fi
  else
    ready_issue "the existing branch ${BRANCH} has human commits and no open PR, so it was not updated."
  fi
  exit 0
fi

if [ -n "$pr" ]; then
  gh pr edit "$pr" --title "$TITLE" --body-file "$tmp/pr.md"
else
  if ! { gh pr create --base main --head "$BRANCH" --title "$TITLE" --body-file "$tmp/pr.md" "${label_args[@]}" \
         || gh pr create --base main --head "$BRANCH" --title "$TITLE" --body-file "$tmp/pr.md"; }; then
    ready_issue "the workflow pushed ${BRANCH} but could not open the PR."
    exit 0
  fi
fi

# A PR or push made with GITHUB_TOKEN does not trigger workflows; start CI once per real push.
if [ "$pushed" = true ]; then
  gh workflow run ci.yml --ref "$BRANCH" || echo "::warning::could not dispatch ci.yml on ${BRANCH}"
fi

close_drift_issues "TCKDB ${SHA7} (${PAIR}) now has a passing bump branch \`${BRANCH}\`; closing."

# Older bot PRs for other line pairs are superseded (commented once, not closed).
for row in $(gh pr list --state open --json number,headRefName \
      -q '.[] | select(.headRefName | startswith("chore/tckdb-drift-")) | "\(.number):\(.headRefName)"'); do
  n="${row%%:*}"; b="${row#*:}"
  [ "$b" = "$BRANCH" ] && continue
  seen="$(gh pr view "$n" --json comments -q '.comments[].body' || true)"
  if ! grep -qF "tckdb-drift-superseded:${BRANCH}" <<<"$seen"; then
    gh pr comment "$n" --body "Superseded by \`${BRANCH}\` (schemas ${SCHEMAS}, client ${CLIENT}). Close this PR once that one is handled.<!-- tckdb-drift-superseded:${BRANCH} -->"
  fi
done
