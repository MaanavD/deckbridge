#!/usr/bin/env bash
# probe_surface.sh -- run this INSIDE the pane where an agent actually runs.
#
# Every focus failure so far has come from guessing which piece of identity the
# Mac really exposes. This prints all of them at once so the guessing stops:
# what the tty looks like by each detection method, which environment variables
# name the host surface, and what cmux itself reports.
#
# Nothing here changes any state. Paste the whole output.

set -u

printf '=== probe_surface (build %s) ===\n' "${BUILD_STAMP:-see focus_agent.sh}"
printf 'host: %s  os: %s\n' "$(hostname 2>/dev/null)" "$(uname -s)"
printf 'pwd: %s\n\n' "$PWD"

printf -- '--- tty, by each method the shim can use ---\n'
printf 'tty(1):            %s\n' "$(tty 2>&1)"
printf 'ps -o tty= $$:     %s\n' "$(ps -o tty= -p $$ 2>/dev/null | tr -d ' ')"
printf 'ps -o tty= parent: %s\n' "$(ps -o tty= -p "$PPID" 2>/dev/null | tr -d ' ')"
if command -v python3 >/dev/null 2>&1; then
  printf 'python ttyname(0): %s\n' \
    "$(python3 -c 'import os
try: print(os.ttyname(0))
except OSError as e: print("FAILED:", e)' 2>&1)"
fi
printf '\n'

printf -- '--- environment variables that identify a surface ---\n'
# A terminal that sets one of these gives us identity even when the tty is
# hidden, which is the case inside a hook whose fds are all pipes.
found=0
for v in CMUX_PANE CMUX_SESSION CMUX_SURFACE CMUX_ID TMUX TMUX_PANE \
         TERM_SESSION_ID ITERM_SESSION_ID WEZTERM_PANE KITTY_WINDOW_ID \
         ALACRITTY_WINDOW_ID GHOSTTY_RESOURCES_DIR TERM_PROGRAM; do
  if [ -n "${!v:-}" ]; then
    printf '%-24s = %s\n' "$v" "${!v}"
    found=1
  fi
done
[ "$found" -eq 1 ] || printf '(none set -- this pane advertises no surface identity)\n'
printf '\nall CMUX*/TMUX* in env:\n'
env | grep -Ei '^(cmux|tmux)' || printf '(none)\n'
printf '\n'

printf -- '--- what cmux reports ---\n'
if command -v cmux >/dev/null 2>&1; then
  printf 'cmux: %s\n' "$(command -v cmux)"
  printf 'version: %s\n' "$(cmux --version 2>&1 | head -1)"
  printf 'tree --all --json:\n'
  cmux tree --all --json 2>&1 | head -80
else
  # If cmux is missing from the agent's PATH then every press fails here, no
  # matter how good the resolution logic is.
  printf 'cmux: NOT ON PATH in this pane\n'
  printf 'PATH=%s\n' "$PATH"
fi
printf '\n'

printf -- '--- recorded agent state ---\n'
state="${CMUX_STATE_FILE:-$HOME/.deckbridge/cmux_state.json}"
if [ -f "$state" ]; then
  printf '%s:\n' "$state"
  cat "$state"
else
  printf '%s: MISSING\n' "$state"
fi
printf '\n=== end probe ===\n'
