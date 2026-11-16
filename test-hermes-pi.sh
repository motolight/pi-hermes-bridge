#!/usr/bin/env bash
# Isolated, deterministic bridge contract tests. No installed Hermes or Pi used.
# NOTE: bridge smoke tier, NOT Attest model-cassette replay.
set -euo pipefail

root="$(cd "$(dirname "$0")" && pwd)"
if ! git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "FAIL sandbox"
  exit 1
fi
tmp="$(mktemp -d /tmp/hermes-pi-regression.XXXXXXXX)"
sandbox="$tmp/worktree"
cleanup() {
  git -C "$root" worktree remove --force "$sandbox" >/dev/null 2>&1 || true
  rm -rf -- "$tmp"
}
trap cleanup EXIT

if ! git -C "$root" worktree add --quiet --detach "$sandbox" HEAD; then
  echo "FAIL sandbox"
  exit 1
fi
mkdir -p "$tmp/home" "$tmp/logs"
cd "$sandbox"
failed=0
while read -r test label; do
  if timeout 90s env -i PATH="$PATH" HOME="$tmp/home" LANG="C.UTF-8" \
      PYTHONDONTWRITEBYTECODE=1 PI_BRIDGE_NO_PIWEB=1 \
      python3 -m pytest -q --tb=short \
      "tests/test_attest_regression.py::$test" \
      >"$tmp/logs/$label.log" 2>&1; then
    echo "PASS $label"
  else
    echo "FAIL $label"
    if printenv GITHUB_ACTIONS >/dev/null 2>&1; then
      tail -n 60 "$tmp/logs/$label.log" >&2
    fi
    failed=1
  fi
done <<'TESTS'
test_01_delegate_file_edit_and_return delegation_file_delivery
test_02_long_job_eventually_delivered long_job_delivery
test_03_gateway_downtime_and_recovery gateway_restart
test_04_pi_failure_bounded_no_loop pi_error_bounded
test_05_coding_changes_repo_and_tests_pass coding_repo_tests
TESTS
exit "$failed"
