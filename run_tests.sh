#!/bin/bash
# Run every test in the repo, print one line per file, and exit non-zero if any
# failed.
#
# Correctness note: a test file's verdict is its EXIT CODE, not a word in its
# output. An earlier version of this script grepped the last line for "fail",
# which is wrong in both directions -- a suite printing "0 failed" was counted
# as a failure, and a file that crashed before printing anything was counted as
# a pass. A harness that miscounts is worse than no harness, because it is
# trusted.
cd "$(dirname "$0")" || exit 1

PY=python3
[ -x .venv/bin/python3 ] && PY=.venv/bin/python3

ok=0
bad=0
failed_files=()

run_one() {  # run_one RUNNER FILE
  local out status timeout_seconds
  timeout_seconds=${DECKBRIDGE_TEST_TIMEOUT_SECONDS:-120}
  printf 'running %-26s\n' "$2"
  # Keep one wedged macOS integration fixture from occupying a public runner
  # for hours. Perl ships on macOS and preserves the alarm across exec.
  out=$(/usr/bin/perl -e 'alarm shift; exec @ARGV' \
    "$timeout_seconds" "$1" "$2" 2>&1)
  status=$?
  printf '%-34s %s\n' "$2" "$(printf '%s\n' "$out" | tail -1)"
  if [ "$status" -eq 0 ]; then
    ok=$((ok + 1))
  else
    bad=$((bad + 1))
    failed_files+=("$2")
    [ "$status" -ne 142 ] || printf '    TIMEOUT after %ss\n' "$timeout_seconds"
    # Surface the actual failures rather than making the reader re-run it.
    printf '%s\n' "$out" | grep -E '^(FAIL|not ok)' | head -8 | sed 's/^/    /'
  fi
}

for t in test_*.py; do
  [ -e "$t" ] || continue
  run_one "$PY" "$t"
done

for t in test_*.sh; do
  [ -e "$t" ] || continue
  run_one bash "$t"
done

echo
if [ "$bad" -eq 0 ]; then
  echo "all $ok test files passed"
else
  echo "$ok passed, $bad FAILED: ${failed_files[*]}"
fi
exit "$bad"
