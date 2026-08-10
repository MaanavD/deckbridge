#!/usr/bin/env bash
# Which environment variable does cmux use to name a surface?
#
# WHY THIS EXISTS
#
# A press has to answer "which tab is this agent in". There are three possible
# answers, in descending order of trustworthiness:
#
#   1. A surface id the agent already knows.   Nothing to match. Exact.
#   2. The agent's tty.                        One lookup in the cmux tree.
#   3. The agent's working directory.          NOT AN IDENTITY -- every tab
#                                              open in one repo shares it.
#
# (3) is where presses went wrong: with no tty recorded, the resolver matched a
# directory, eight tabs matched it equally, and the press focused whichever one
# came first in the tree. deckbridge now refuses that guess instead of making
# it, which is honest but not useful. (1) is what makes it useful.
#
# The catch: the variable's NAME is a property of the cmux build, and cannot be
# read from anywhere but a running cmux. This script reads it, from inside one.
#
# USAGE
#
#   Run it INSIDE a cmux tab (any tab):
#
#       ./find_surface_var.sh
#
#   If it finds a candidate, add that name to SURFACE_ENV_VARS in agent_shim.py
#   (or pass --surface "$THE_VAR" in your hook command) and restart the agent.
set -u

printf 'cmux surface environment probe\n'
printf -- '------------------------------\n'

if ! command -v cmux >/dev/null 2>&1; then
  printf 'cmux is not on PATH. Run this from a shell that can see it.\n'
  exit 1
fi

printf 'cmux:  %s\n' "$(command -v cmux)"
printf 'shell pid: %s\n\n' "$$"

# 1. Anything cmux-shaped in this shell's environment. The name is unknown, so
#    the filter is deliberately broad -- a false positive costs a line of
#    output, a false negative costs the whole feature.
printf 'CMUX-ish variables visible here:\n'
found=0
while IFS='=' read -r name value; do
  case "$name" in
    *CMUX*|*SURFACE*|*PANE*|*TERM_SESSION*)
      printf '  %-28s = %s\n' "$name" "$value"
      found=1
      ;;
  esac
done < <(env | sort)
[ "$found" = 1 ] || printf '  (none)\n'

# 2. Cross-reference against the tree. A variable whose VALUE appears as a
#    surface ref is not a guess -- it is the answer.
printf '\nSurfaces cmux currently reports:\n'
# Current cmux exports a stable UUID in CMUX_SURFACE_ID while its default tree
# prints only short `surface:N` refs. Request both forms or the exact variable
# is present in the environment yet invisible to this cross-check.
tree=$(cmux --id-format both tree --all --json 2>/dev/null || true)
if [ -z "$tree" ]; then
  printf '  (cmux tree returned nothing)\n'
  exit 1
fi
printf '%s' "$tree" | tr ',' '\n' | grep -o 'surface:[0-9]*' | sort -u | sed 's/^/  /'

printf '\nMatch (a variable naming a real surface):\n'
matched=0
while IFS='=' read -r name value; do
  [ -n "$value" ] || continue
  # A workspace/tab UUID also appears in the all-ID tree, but it cannot be
  # passed as --surface. Restrict the match to variables claiming to identify
  # a surface (plus cmux's legacy PANEL synonym).
  case "$name" in
    *SURFACE*|CMUX_PANEL_ID) ;;
    *) continue ;;
  esac
  case "$value" in
    surface:*|pane:*|*-*-*-*-*)
      if printf '%s' "$tree" | grep -Fq "\"$value\""; then
        printf '  %s = %s   <-- USE THIS\n' "$name" "$value"
        matched=1
      fi
      ;;
  esac
done < <(env | sort)

if [ "$matched" = 0 ]; then
  printf '  none found.\n\n'
  printf 'That means this cmux build does not export its surface id, so the tty\n'
  printf 'remains the best available signal. That is fine -- it is exact once it\n'
  printf 'is recorded. Restart your agents so their hooks record one, then:\n\n'
  printf '    ./focus_agent.sh --diagnose --source claude-code --name probe\n\n'
  printf 'and check that "tty resolved" is not <none found>.\n'
fi
