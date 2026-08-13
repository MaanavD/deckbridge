#!/usr/bin/env bash
# Linux-safe tests for focus_agent.sh's argument and pure-resolution logic.

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SCRIPT="$SCRIPT_DIR/focus_agent.sh"
TMP_DIR=${TMPDIR:-/tmp}/focus-agent-test.$$
mkdir -p "$TMP_DIR/bin"
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM

passed=0
total=0

pass() {
  passed=$((passed + 1))
  total=$((total + 1))
  printf 'ok - %s\n' "$1"
}

fail() {
  total=$((total + 1))
  printf 'not ok - %s\n' "$1"
}

if "$SCRIPT" --source codex-cli --name cx --cwd /tmp --dry-run >/dev/null 2>&1; then
  pass 'valid arguments are accepted'
else
  fail 'valid arguments are accepted'
fi

if "$SCRIPT" --source t3code-claude --name task --session thread-1 \
    --app 'T3 Code (Alpha)' --web-url http://127.0.0.1:3773/env/thread-1 \
    --dry-run >/dev/null 2>&1; then
  pass 'T3 Code provider sessions are accepted'
else
  fail 'T3 Code provider sessions are accepted'
fi

check_t3_app() {
  FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT"
  [ "$(app_bundle_id 'T3 Code (Alpha)')" = com.t3tools.t3code ]
}
if check_t3_app; then
  pass 'T3 Code maps to its installed bundle identifier'
else
  fail 'T3 Code maps to its installed bundle identifier'
fi

check_canonical_app() {
  FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT"
  parse_args --source claude-code --name cc --app claude
  [ "$APP_HINT" = Claude ]
}
if check_canonical_app; then
  pass 'hook-recorded lowercase Claude app is canonicalized'
else
  fail 'hook-recorded lowercase Claude app is canonicalized'
fi

if "$SCRIPT" --source not-a-source --name x --dry-run >/tmp/focus-agent-unknown.$$.out 2>&1; then
  fail 'unknown source exits nonzero'
else
  if grep -q 'unknown --source' /tmp/focus-agent-unknown.$$.out; then
    pass 'unknown source exits nonzero with a message'
  else
    fail 'unknown source exits nonzero with a message'
  fi
fi
rm -f /tmp/focus-agent-unknown.$$.out

if "$SCRIPT" --source codex-cli --cwd /tmp --dry-run >/tmp/focus-agent-missing-name.$$.out 2>&1; then
  fail 'missing name exits nonzero'
else
  if grep -q -- '--name is required' /tmp/focus-agent-missing-name.$$.out; then
    pass 'missing name exits nonzero with a message'
  else
    fail 'missing name exits nonzero with a message'
  fi
fi
rm -f /tmp/focus-agent-missing-name.$$.out

# Every candidate executable writes a marker if invoked. Dry-run must not call
# command -v, cmux, tmux, pgrep, lsof, ps, osascript, or open.
for tool in cmux tmux pgrep lsof ps osascript open; do
  printf '#!/bin/sh\nprintf invoked > "%s/marker"\nexit 0\n' "$TMP_DIR" > "$TMP_DIR/bin/$tool"
  chmod +x "$TMP_DIR/bin/$tool"
done
if PATH="$TMP_DIR/bin:$PATH" "$SCRIPT" --source codex-cli --name dry --cwd /tmp --dry-run >"$TMP_DIR/dry-run.out" 2>&1 && [ ! -e "$TMP_DIR/marker" ]; then
  pass 'dry-run executes no external candidate command'
else
  fail 'dry-run executes no external candidate command'
fi
if grep -q 'WOULD RUN: tmux list-panes' "$TMP_DIR/dry-run.out" && grep -q 'chosen command:' "$TMP_DIR/dry-run.out"; then
  pass 'dry-run prints the resolution chain and chosen command'
else
  fail 'dry-run prints the resolution chain and chosen command'
fi

# Source only the functions: this fixture does not need macOS, tmux, or a live
# pane. The first exact cwd match must win.
if FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT" && \
   got=$(printf '%s\n' \
     'alpha:0.0 /tmp/other' \
     'build:2.3 /Users/example/project' \
     'later:1.0 /Users/example/project' | tmux_pane_for_cwd /Users/example/project) && \
   [ "$got" = 'build:2.3' ]; then
  pass 'tmux pane-matching function picks the first matching pane'
else
  fail 'tmux pane-matching function picks the first matching pane'
fi

# --- hermes-ssh: Hermes agents reached with `cmux ssh hermes` ----------------
# These sessions have a REMOTE cwd and a local process that is an ssh client,
# so they must be found by ssh-target matching, not by cwd matching.

check_sh() {  # check_sh DESCRIPTION SHELL_SNIPPET
  if ( eval "$2" ) >/dev/null 2>&1; then pass "$1"; else fail "$1"; fi
}

cd "$SCRIPT_DIR" || exit 1

check_sh 'hermes-ssh is an accepted source' \
  '"$SCRIPT" --source hermes-ssh --name x --dry-run'

check_sh 'hermes-ssh does not require --url' \
  '"$SCRIPT" --source hermes-ssh --name x --dry-run | grep -q "ssh pane for host"'

check_sh 'hermes-ssh never defaults cwd to the local directory' \
  '"$SCRIPT" --source hermes-ssh --name x --dry-run | grep -q "cwd=<none>"'

check_sh 'hermes-ssh reports the configured ssh host' \
  'HERMES_SSH_HOST=myhermes "$SCRIPT" --source hermes-ssh --name x --dry-run | grep -q myhermes'

# The local T3 HTTP route requires a separate browser bootstrap credential.
# Falling back to it from a native-app key strands the user on a pairing-token
# screen, so exact native focus must fail closed instead of opening a browser.
check_sh 'T3 focus never opens its pairing-required browser route' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   printf "#!/bin/sh\nprintf \"%%s\\n\" \"\$*\" >> \"$TMP_DIR/t3-opened\"\nexit 0\n" > "$TMP_DIR/bin/open";
   printf "#!/bin/sh\nexit 1\n" > "$TMP_DIR/t3-control";
   chmod +x "$TMP_DIR/bin/open" "$TMP_DIR/t3-control";
   PATH="$TMP_DIR/bin:$PATH"; DECKBRIDGE_CONTROL_CLI="$TMP_DIR/t3-control";
   DECKBRIDGE_DISABLE_HAMMERSPOON=1;
   NAME=missing; SESSION=thread-1; WEB_URL=http://127.0.0.1:3773/env/thread-1;
   ! focus_t3code;
   ! grep -q "http://" "$TMP_DIR/t3-opened"'

check_sh 'T3 focus retries a transient native Accessibility miss' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   printf "#!/bin/sh\ncase \"\$1\" in\n  --helper-press-button) n=\$(cat \"$TMP_DIR/t3-tries\" 2>/dev/null || echo 0); n=\$((n+1)); echo \$n > \"$TMP_DIR/t3-tries\"; [ \$n -gt 1 ] ;;\n  --helper-web-url) echo t3code://app/\#/env/thread-1 ;;\nesac\n" > "$TMP_DIR/t3-retry-control";
   chmod +x "$TMP_DIR/t3-retry-control";
   PATH="$TMP_DIR/bin:$PATH"; DECKBRIDGE_CONTROL_CLI="$TMP_DIR/t3-retry-control";
   DECKBRIDGE_DISABLE_HAMMERSPOON=1; NAME=task; SESSION=thread-1; focus_t3code;
   [ "$(cat "$TMP_DIR/t3-tries")" -eq 2 ]'

check_sh 'T3 focus dismisses Settings before selecting the exact thread' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   printf "#!/bin/sh\necho \"\$*\" >> \"$TMP_DIR/t3-control-log\"\ncase \"\$1\" in\n  --helper-press-button) exit 0 ;;\n  --helper-web-url) echo t3code://app/\#/env/thread-1 ;;\nesac\n" > "$TMP_DIR/t3-settings-control";
   chmod +x "$TMP_DIR/t3-settings-control";
   PATH="$TMP_DIR/bin:$PATH"; DECKBRIDGE_CONTROL_CLI="$TMP_DIR/t3-settings-control";
   DECKBRIDGE_DISABLE_HAMMERSPOON=1; NAME=task; SESSION=thread-1; focus_t3code;
   head -n 1 "$TMP_DIR/t3-control-log" | grep -q -- "--helper-press-button com.t3tools.t3code Back"'

check_sh 'T3 focus uses the durable Hammerspoon Accessibility bridge' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   printf "#!/bin/sh\necho t3code://app/\#/env/thread-1\n" > "$TMP_DIR/bin/hs";
   printf "#!/bin/sh\nexit 1\n" > "$TMP_DIR/t3-no-helper";
   chmod +x "$TMP_DIR/bin/hs" "$TMP_DIR/t3-no-helper";
   PATH="$TMP_DIR/bin:/usr/bin:/bin"; DECKBRIDGE_CONTROL_CLI="$TMP_DIR/t3-no-helper";
   NAME=task; SESSION=thread-1; focus_t3code'

check_sh 'T3 focus verifies the LaunchServices Hammerspoon acknowledgement' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   mkdir -p "$TMP_DIR/url-home/.deckbridge/t3-focus-results";
   printf "#!/bin/sh\ncase \"\$*\" in\n  *hammerspoon://*) u=\${!#}; q=\${u#*?}; session=\$(printf \"%%s\" \"\$q\" | tr \"&\" \"\\n\" | sed -n \"s/^session=//p\"); request=\$(printf \"%%s\" \"\$q\" | tr \"&\" \"\\n\" | sed -n \"s/^request=//p\"); mkdir -p \"\$HOME/.deckbridge/t3-focus-results\"; echo \"t3code://app/#/env/\$session\" > \"\$HOME/.deckbridge/t3-focus-results/\$request\" ;;\nesac\n" > "$TMP_DIR/bin/open";
   chmod +x "$TMP_DIR/bin/open";
   PATH="$TMP_DIR/bin:/usr/bin:/bin"; HOME="$TMP_DIR/url-home";
   NAME=task; SESSION=thread-1; focus_t3code 2>&1 | grep -q "via Hammerspoon (verified)"'

matcher() {
  FOCUS_AGENT_LIB_ONLY=1 bash -c ". \"$SCRIPT\"; ssh_pane_for_host \"\$1\" \"\$2\"" _ "$1" "$2"
}

got=$(matcher 'a:0.0 vim x
b:1.0 ssh hermes
c:2.0 ssh other@box' hermes)
if [ "$got" = "b:1.0" ]; then
  pass 'ssh pane matcher picks the pane running ssh to the host'
else
  fail "ssh pane matcher picks the pane running ssh to the host (got '$got')"
fi

got=$(matcher 'a:0.0 ssh elsewhere' hermes)
if [ -z "$got" ]; then
  pass 'ssh pane matcher ignores a different host'
else
  fail "ssh pane matcher ignores a different host (got '$got')"
fi

got=$(matcher 'a:0.0 ssh user@example-host' example-host)
if [ "$got" = "a:0.0" ]; then
  pass 'ssh pane matcher handles user@host form'
else
  fail "ssh pane matcher handles user@host form (got '$got')"
fi

check_sh 'HERMES_SSH_FOCUS_CMD override is reported in dry-run' \
  'HERMES_SSH_FOCUS_CMD="echo focus {host}" "$SCRIPT" --source hermes-ssh --name x --dry-run | grep -q HERMES_SSH_FOCUS_CMD'

check_sh 'HERMES_SSH_FOCUS_CMD substitutes the host name' \
  'HERMES_SSH_FOCUS_CMD="echo focus {host}" "$SCRIPT" --source hermes-ssh --name x --dry-run | grep -q "focus hermes"'

check_sh 'an unknown source still fails after adding hermes-ssh' \
  '! "$SCRIPT" --source nonsense --name x --dry-run'

# --- regressions: pressing a key opened a NEW zsh window ---------------------
# Root cause 1: a Claude/Codex hook session_id is a UUID, not a cmux surface
# ref. It was passed straight to `cmux focus-panel --panel`, which can never
# match, so every branch failed and the chain reached "activate Terminal" --
# and AppleScript activate LAUNCHES Terminal.app, producing a blank zsh window.
if FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT"; then
  if is_cmux_ref 'surface:2' && is_cmux_ref 'pane:1' && is_cmux_ref '0'; then
    pass 'is_cmux_ref accepts real cmux surface refs'
  else
    fail 'is_cmux_ref accepts real cmux surface refs'
  fi

  if is_cmux_uuid 'B854DA82-6647-4ED2-AC3E-0F679082354D' \
     && ! is_cmux_uuid '3f2a91c4-not-a-uuid'; then
    pass 'cmux surface UUIDs are recognised only on the surface-hint path'
  else
    fail 'cmux surface UUIDs are recognised only on the surface-hint path'
  fi

  if ! is_cmux_ref '3f2a91c4-55de-4f6e-9a1b-70b1d2c8e0aa' \
     && ! is_cmux_ref '20260805_074625_625b44e7' && ! is_cmux_ref ''; then
    pass 'is_cmux_ref rejects hook UUIDs and Hermes session ids'
  else
    fail 'is_cmux_ref rejects hook UUIDs and Hermes session ids'
  fi

  # Root cause 2: the surface must be resolvable from the agent's cwd.
  tree_json='{"workspaces":[{"ref":"workspace:1","surfaces":[
    {"ref":"surface:1","cwd":"/Users/example/code/other"},
    {"ref":"surface:7","cwd":"/Users/example/code/sample-api"}]}]}'
  got=$(cmux_ref_for_cwd "$tree_json" /Users/example/code/sample-api)
  if [ "$got" = "surface:7" ]; then
    pass 'cmux surface is resolved from the agent cwd'
  else
    fail "cmux surface is resolved from the agent cwd (got '$got')"
  fi

  got=$(cmux_ref_for_cwd "$tree_json" /Users/example/code/sample-api/)
  if [ "$got" = "surface:7" ]; then
    pass 'cmux cwd match tolerates a trailing slash'
  else
    fail "cmux cwd match tolerates a trailing slash (got '$got')"
  fi

  if ! cmux_ref_for_cwd "$tree_json" /nowhere >/dev/null 2>&1; then
    pass 'cmux cwd resolver fails rather than guessing a surface'
  else
    fail 'cmux cwd resolver fails rather than guessing a surface'
  fi

  if ! cmux_ref_for_cwd 'not json' /tmp >/dev/null 2>&1; then
    pass 'cmux cwd resolver survives malformed cmux output'
  else
    fail 'cmux cwd resolver survives malformed cmux output'
  fi

  # Root cause 5, found on the real Mac: cmux v3.9.6 `tree --all --json` has NO
  # cwd field. A surface is {ref,title,tty,type,pane_ref}, nested under
  # windows[].workspaces[].panes[].surfaces[]. The old resolver only looked for
  # cwd-ish keys, matched nothing on real output, and fell through to the
  # terminal fallback. These fixtures are copied from real v3.9.6 output.
  real_json='{"active":{"pane_ref":"pane:23","surface_ref":"surface:27"},
    "windows":[{"ref":"window:1","workspaces":[
      {"ref":"workspace:1","panes":[{"ref":"pane:1","surfaces":[
        {"ref":"surface:1","title":"~/Documents/deckbridge","tty":"ttys000","type":"terminal"},
        {"ref":"surface:9","title":"ssh hermes","tty":"ttys009","type":"terminal"}]}]},
      {"ref":"workspace:13","panes":[{"ref":"pane:23","surfaces":[
        {"ref":"surface:27","title":"~/code/sample-api","tty":"ttys027","type":"terminal"},
        {"ref":"surface:31","title":"~/code/mirror","tty":"ttys031","type":"terminal"}]}]}]}]}'

  got=$(HOME=/Users/example cmux_resolve_surface "$real_json" /Users/example/code/sample-api '')
  if [ "$got" = "surface:27" ]; then
    pass 'real cmux schema: ~-abbreviated title resolves to the surface'
  else
    fail "real cmux schema: ~-abbreviated title resolves (got '$got')"
  fi

  # macOS filesystems are case-INSENSITIVE. A shell reporting
  # /Users/example/downloads/deckbridge and a cmux title reading
  # ~/Downloads/deckbridge are the same directory, but an exact string compare
  # called it no-match -- which then let the desktop-app guess fire and open
  # Claude instead of the cmux tab the operator pressed.
  #
  # The basename differs in case too, on purpose: matching only the parent
  # directories would still pass through the single-basename-hit fallback and
  # prove nothing about the comparison itself.
  case_json='{"windows":[{"ref":"window:1","workspaces":[
    {"ref":"workspace:1","panes":[{"ref":"pane:1","surfaces":[
      {"ref":"surface:5","title":"~/Downloads/Deckbridge","tty":"ttys005","type":"terminal"},
      {"ref":"surface:6","title":"~/code/other","tty":"ttys006","type":"terminal"}]}]}]}]}'
  got=$(HOME=/Users/example cmux_resolve_surface "$case_json" /Users/example/downloads/deckbridge '')
  if [ "$got" = "surface:5" ]; then
    pass 'a differently-cased cwd still matches its surface'
  else
    fail "a differently-cased cwd still matches its surface (got '$got')"
  fi

  # A DIRECTORY IS NOT AN IDENTITY. Eight tabs open in one repo all report the
  # same cwd and the same ~-abbreviated title, so taking the first hit lands on
  # an unrelated tab seven times out of eight -- while reporting success, which
  # is worse than not moving. Without a tty the surface cannot be identified,
  # and the honest answer is to refuse.
  ambig_cwd_json='{"windows":[{"ref":"window:1","workspaces":[
    {"ref":"workspace:1","panes":[{"ref":"pane:1","surfaces":[
      {"ref":"surface:36","title":"~/Downloads/deckbridge","tty":"ttys010","type":"terminal"},
      {"ref":"surface:37","title":"~/Downloads/deckbridge","tty":"ttys011","type":"terminal"},
      {"ref":"surface:29","title":"~/Downloads/deckbridge","tty":"ttys013","type":"terminal"},
      {"ref":"surface:24","title":"~/Downloads/deckbridge","tty":"ttys001","type":"terminal"}]}]}]}]}'
  if HOME=/Users/example cmux_resolve_surface "$ambig_cwd_json" \
       /Users/example/Downloads/deckbridge '' >/dev/null 2>&1; then
    fail 'four tabs in one directory must not resolve without a tty'
  else
    pass 'four tabs in one directory refuse to resolve without a tty'
  fi

  # The tty disambiguates them, which is the whole reason the hook records it.
  got=$(HOME=/Users/example cmux_resolve_surface "$ambig_cwd_json" \
          /Users/example/Downloads/deckbridge ttys013)
  if [ "$got" = "surface:29" ]; then
    pass 'a recorded tty picks the right tab out of four identical ones'
  else
    fail "a recorded tty picks the right tab out of four (got '$got')"
  fi

  # The same rule via the cwd field alone (no matching titles), so the cwd
  # guard is pinned independently of the title guard. Either one passing on its
  # own would leave the other free to regress.
  ambig_field_json='{"windows":[{"ref":"window:1","workspaces":[
    {"ref":"workspace:1","panes":[{"ref":"pane:1","surfaces":[
      {"ref":"surface:41","cwd":"/Users/example/Downloads/deckbridge","title":"claude","tty":"ttys020","type":"terminal"},
      {"ref":"surface:42","cwd":"/Users/example/Downloads/deckbridge","title":"codex","tty":"ttys021","type":"terminal"}]}]}]}]}'
  if HOME=/Users/example cmux_resolve_surface "$ambig_field_json" \
       /Users/example/Downloads/deckbridge '' >/dev/null 2>&1; then
    fail 'two surfaces with the same cwd field must not resolve without a tty'
  else
    pass 'two surfaces with the same cwd field refuse to resolve without a tty'
  fi

  # ...and the tty still picks one out.
  got=$(HOME=/Users/example cmux_resolve_surface "$ambig_field_json" \
          /Users/example/Downloads/deckbridge ttys021)
  if [ "$got" = "surface:42" ]; then
    pass 'a tty disambiguates two surfaces sharing a cwd field'
  else
    fail "a tty disambiguates two surfaces sharing a cwd field (got '$got')"
  fi

  # ...and the stricter rule must not break the ordinary one-tab-per-repo case.
  solo_json='{"windows":[{"ref":"window:1","workspaces":[
    {"ref":"workspace:1","panes":[{"ref":"pane:1","surfaces":[
      {"ref":"surface:5","title":"~/code/solo","tty":"ttys005","type":"terminal"},
      {"ref":"surface:6","title":"~/code/other","tty":"ttys006","type":"terminal"}]}]}]}]}'
  got=$(HOME=/Users/example cmux_resolve_surface "$solo_json" /Users/example/code/solo '')
  if [ "$got" = "surface:5" ]; then
    pass 'one tab in a directory still resolves from cwd alone'
  else
    fail "one tab in a directory still resolves from cwd alone (got '$got')"
  fi

  got=$(HOME=/Users/example cmux_resolve_surface "$real_json" /Users/example/code/mirror '')
  if [ "$got" = "surface:31" ]; then
    pass 'real cmux schema: a second nested workspace still resolves'
  else
    fail "real cmux schema: a second nested workspace resolves (got '$got')"
  fi

  got=$(cmux_resolve_surface "$real_json" '' ttys031)
  if [ "$got" = "surface:31" ]; then
    pass 'real cmux schema: a tty resolves to the surface'
  else
    fail "real cmux schema: a tty resolves to the surface (got '$got')"
  fi

  # ps reports `ttys009`; the tree may carry either form.
  got=$(cmux_resolve_surface "$real_json" '' /dev/ttys009)
  if [ "$got" = "surface:9" ]; then
    pass 'real cmux schema: /dev/tty and bare tty names compare equal'
  else
    fail "real cmux schema: /dev/tty and bare tty compare equal (got '$got')"
  fi

  # A title is a label any program can overwrite; a tty is a kernel fact.
  got=$(HOME=/Users/example cmux_resolve_surface "$real_json" /Users/example/code/sample-api ttys031)
  if [ "$got" = "surface:31" ]; then
    pass 'tty beats title when the two disagree'
  else
    fail "tty beats title when the two disagree (got '$got')"
  fi

  # A pane/window ref must never be focused in place of a surface.
  got=$(HOME=/Users/example cmux_resolve_surface "$real_json" /Users/example/code/sample-api '')
  case "$got" in
    surface:*) pass 'resolver returns a surface ref, never a pane/window ref' ;;
    *) fail "resolver returns a surface ref (got '$got')" ;;
  esac

  if ! HOME=/Users/example cmux_resolve_surface "$real_json" /Users/example/code/nope '' >/dev/null 2>&1; then
    pass 'real cmux schema: unknown cwd still refuses to guess'
  else
    fail 'real cmux schema: unknown cwd still refuses to guess'
  fi

  # Root cause 6: a RUNNING agent rewrites its terminal title, so the exact
  # ~/code/sample-api title the resolver wanted is gone the moment claude starts.
  # This is why a press still missed on the real Mac even after the schema fix.
  live_json='{"windows":[{"workspaces":[{"panes":[{"surfaces":[
    {"ref":"surface:5","title":"✳ sample-api — claude","tty":"ttys005"},
    {"ref":"surface:6","title":"~/code/mirror","tty":"ttys006"}]}]}]}]}'
  got=$(HOME=/Users/example cmux_resolve_surface "$live_json" /Users/example/code/sample-api '')
  if [ "$got" = "surface:5" ]; then
    pass 'a running agent that rewrote its title still resolves by basename'
  else
    fail "a running agent that rewrote its title resolves (got '$got')"
  fi

  # But a basename is weak evidence. Two candidates is a coin flip, not a match.
  ambig_json='{"windows":[{"workspaces":[{"panes":[{"surfaces":[
    {"ref":"surface:5","title":"✳ sample-api — claude","tty":"ttys005"},
    {"ref":"surface:8","title":"sample-api tests","tty":"ttys008"}]}]}]}]}'
  if ! HOME=/Users/example cmux_resolve_surface "$ambig_json" /Users/example/code/sample-api '' >/dev/null 2>&1; then
    pass 'two basename candidates is refused rather than guessed'
  else
    fail 'two basename candidates is refused rather than guessed'
  fi

  # An exact title must still beat a fuzzy basename hit elsewhere.
  mixed_json='{"windows":[{"workspaces":[{"panes":[{"surfaces":[
    {"ref":"surface:5","title":"sample-api scratch","tty":"ttys005"},
    {"ref":"surface:7","title":"~/code/sample-api","tty":"ttys007"}]}]}]}]}'
  got=$(HOME=/Users/example cmux_resolve_surface "$mixed_json" /Users/example/code/sample-api '')
  if [ "$got" = "surface:7" ]; then
    pass 'an exact title beats a fuzzy basename hit'
  else
    fail "an exact title beats a fuzzy basename hit (got '$got')"
  fi

  # Root cause 7: the resolver is Python. On a Mac without a usable interpreter
  # it returned nothing at all, and the chain read that as "no surface" and
  # raised a terminal. Fail loudly instead of silently.
  if ! FOCUS_PYTHON=/nonexistent/python cmux_resolve_surface "$real_json" /tmp '' >/dev/null 2>&1; then
    pass 'a missing python interpreter fails instead of silently resolving nothing'
  else
    fail 'a missing python interpreter fails instead of silently resolving nothing'
  fi

  # Root cause 8, reported from the real Mac: "when it does open cmux it's not
  # the right tab -- just the default tab". A surface lives in a workspace, and
  # `focus-panel` alone raises the window while leaving whatever workspace was
  # already selected on screen. The resolver therefore has to report the
  # ANCESTRY, not just the surface, so the workspace can be selected first.
  got=$(HOME=/Users/example cmux_resolve_full "$real_json" /Users/example/code/mirror '')
  if [ "$got" = "surface:31 pane:23 workspace:13 window:1 -" ]; then
    pass 'resolver reports the surface AND its workspace/window ancestry'
  else
    fail "resolver reports surface + ancestry (got '$got')"
  fi

  # The nearer workspace must win: a surface in workspace:13 must never report
  # workspace:1 just because that container was walked first.
  got=$(HOME=/Users/example cmux_resolve_full "$real_json" '' ttys009)
  if [ "$got" = "surface:9 pane:1 workspace:1 window:1 -" ]; then
    pass 'ancestry names the nearest enclosing workspace, not the first seen'
  else
    fail "ancestry names the nearest workspace (got '$got')"
  fi

  # A surface that names its own containers inline must be believed over the
  # position it happens to occupy in the tree.
  inline_json='{"windows":[{"ref":"window:2","workspaces":[{"ref":"workspace:1",
    "panes":[{"ref":"pane:4","surfaces":[
      {"ref":"surface:44","title":"~/code/x","tty":"ttys044",
       "workspace_ref":"workspace:99","pane_ref":"pane:98"}]}]}]}]}'
  got=$(cmux_resolve_full "$inline_json" '' ttys044)
  if [ "$got" = "surface:44 pane:98 workspace:99 window:2 -" ]; then
    pass 'a surface own workspace_ref beats its position in the tree'
  else
    fail "a surface own workspace_ref beats its position (got '$got')"
  fi

  uuid_json='{"windows":[{"ref":"window:2","workspaces":[{"ref":"workspace:8",
    "panes":[{"ref":"pane:9","surfaces":[
      {"id":"B854DA82-6647-4ED2-AC3E-0F679082354D","ref":"surface:44","tty":"ttys044"}]}]}]}]}'
  got=$(cmux_resolve_full "$uuid_json" '' '' B854DA82-6647-4ED2-AC3E-0F679082354D)
  if [ "$got" = "surface:44 pane:9 workspace:8 window:2 -" ]; then
    pass 'a documented CMUX_SURFACE_ID UUID resolves exactly with ancestry'
  else
    fail "a CMUX_SURFACE_ID UUID resolves exactly (got '$got')"
  fi

  # The old single-ref helper must keep working for every existing caller.
  got=$(HOME=/Users/example cmux_resolve_surface "$real_json" /Users/example/code/mirror '')
  if [ "$got" = "surface:31" ]; then
    pass 'cmux_resolve_surface still returns a bare surface ref'
  else
    fail "cmux_resolve_surface still returns a bare ref (got '$got')"
  fi

  # Root cause 9: "Claude and Codex apps don't open anything". Those sessions
  # run in DESKTOP apps -- no tty, no cmux surface -- so every resolver above
  # misses them. The host app recorded by the hook is the only way in, and it
  # must not be confused with a terminal that merely hosts an agent.
  if is_terminal_host_app cmux && is_terminal_host_app iTerm2 \
     && is_terminal_host_app Terminal; then
    pass 'terminal hosts are recognised as terminals'
  else
    fail 'terminal hosts are recognised as terminals'
  fi
  if ! is_terminal_host_app Claude && ! is_terminal_host_app ChatGPT; then
    pass 'the desktop Claude/Codex apps are not treated as terminals'
  else
    fail 'the desktop Claude/Codex apps are not treated as terminals'
  fi

  # The no-launch guard still governs AGENT keys: a quit app means the session
  # it hosted is gone, and opening a blank window would be a lie about state.
  # The target Mac may genuinely have Claude running. Stub process detection so
  # this test exercises the quit-app branch deterministically on every host.
  printf '#!/bin/sh\nexit 1\n' > "$TMP_DIR/bin/pgrep"
  chmod +x "$TMP_DIR/bin/pgrep"
  out=$(PATH="$TMP_DIR/bin:$PATH" APP_HINT=Claude SOURCE=claude-code app_focus 2>&1 || true)
  case "$out" in
    *"not running"*) pass 'an agent key refuses to launch its quit host app' ;;
    *) fail "an agent key refuses to launch a quit host app (got '$out')" ;;
  esac

  # ...but an explicit --launch key is the opposite rule, on purpose.
  out=$("$SCRIPT" --launch Discord --dry-run 2>&1 || true)
  case "$out" in
    *"WOULD RUN: open -a Discord"*) pass '--launch opens an app outright' ;;
    *) fail "--launch opens an app outright (got '$out')" ;;
  esac

  # --launch is a mode of its own and must not demand agent arguments.
  if "$SCRIPT" --launch Discord --dry-run >/dev/null 2>&1; then
    pass '--launch needs no --source or --name'
  else
    fail '--launch needs no --source or --name'
  fi

  if python_bin >/dev/null 2>&1; then
    pass 'python_bin finds an interpreter on a normal host'
  else
    fail 'python_bin finds an interpreter on a normal host'
  fi

  # Root cause 8: the real defect behind "still opens a normal zsh". Even with
  # every guard, last_resort_focus would activate a RUNNING terminal, and a
  # running terminal asked to activate with no window opens a fresh zsh window.
  # A cmux-hosted agent has exactly one correct target; there is no useful
  # fallback, so it must not be offered one.
  for src in claude-code codex-cli cmux hermes-ssh; do
    if is_cmux_hosted_source "$src"; then
      pass "$src is treated as cmux-hosted (no terminal fallback)"
    else
      fail "$src is treated as cmux-hosted (no terminal fallback)"
    fi
  done

  if ! is_cmux_hosted_source hermes-discord; then
    pass 'hermes-discord is not cmux-hosted'
  else
    fail 'hermes-discord is not cmux-hosted'
  fi

  out=$(SOURCE=claude-code last_resort_focus 2>&1 || true)
  case "$out" in
    *"lives in a cmux surface"*)
      pass 'claude-code never reaches the app-activation fallback' ;;
    *) fail "claude-code never reaches the app-activation fallback (got '$out')" ;;
  esac
else
  fail 'focus_agent.sh can be sourced as a library'
fi

# Root cause 3: claude-code must NOT activate the desktop Claude chat app;
# Claude Code is a CLI hosted in a terminal surface.
check_sh 'claude-code no longer targets the desktop Claude app' \
  '! "$SCRIPT" --source claude-code --name cc --cwd /tmp --dry-run | grep -q '\''application "Claude"'\'''

check_sh 'dry-run states that a non-running terminal is never launched' \
  '"$SCRIPT" --source codex-cli --name cx --cwd /tmp --dry-run | grep -qi "no new window is ever launched"'

# Root cause 4: cmux is the documented host, so it must be preferred over
# Terminal.app when choosing which app to raise.
check_sh 'cmux is preferred over Terminal when both are running' \
  'printf "#!/bin/sh\nexit 0\n" > "$TMP_DIR/bin/pgrep"; chmod +x "$TMP_DIR/bin/pgrep";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH"; [ "$(terminal_app_name)" = cmux ]'

check_sh 'a non-running app is never activated' \
  'printf "#!/bin/sh\nexit 1\n" > "$TMP_DIR/bin/pgrep"; chmod +x "$TMP_DIR/bin/pgrep";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH"; ! app_is_running Terminal'

# Root cause 6: focus_terminal_app runs `activate`, which LAUNCHES a quit app.
# process_focus reaches it directly, bypassing last_resort_focus, so a press
# could still open a fresh login-zsh Terminal window. It must refuse first, and
# it must not even reach osascript.
check_sh 'a tty match never launches a quit terminal app' \
  'printf "#!/bin/sh\nexit 1\n" > "$TMP_DIR/bin/pgrep"; chmod +x "$TMP_DIR/bin/pgrep";
   printf "#!/bin/sh\ntouch \"$TMP_DIR/osascript-ran\"\n" > "$TMP_DIR/bin/osascript";
   chmod +x "$TMP_DIR/bin/osascript";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   ! focus_terminal_app ttys016 Terminal;
   [ ! -e "$TMP_DIR/osascript-ran" ]'

check_sh 'refusing a quit terminal app says so' \
  'printf "#!/bin/sh\nexit 1\n" > "$TMP_DIR/bin/pgrep"; chmod +x "$TMP_DIR/bin/pgrep";
   printf "#!/bin/sh\nexit 0\n" > "$TMP_DIR/bin/osascript"; chmod +x "$TMP_DIR/bin/osascript";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   focus_terminal_app ttys016 Terminal 2>&1 | grep -qi "not running"'

# process_focus guesses the host app from a pid, which for a cmux-hosted agent
# can resolve to Terminal.app and open a new window. It must decline outright.
check_sh 'process_focus declines for cmux-hosted sources' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   SOURCE=claude-code; CWD=/tmp; ! process_focus 2>/dev/null'

check_sh 'process_focus still runs for non-cmux sources' \
  'printf "#!/bin/sh\nexit 1\n" > "$TMP_DIR/bin/pgrep"; chmod +x "$TMP_DIR/bin/pgrep";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   SOURCE=other; CWD=/tmp;
   ! process_focus 2>&1 | grep -qi "cmux-hosted"'

# Root cause 5: hermes-ssh used to fall through to raising an arbitrary
# terminal when no ssh pane matched, which surfaced the wrong window.
check_sh 'hermes-ssh reports failure instead of raising a random terminal' \
  '! "$SCRIPT" --source hermes-ssh --name x 2>&1 | grep -qi "no ssh pane or cmux surface"; true'

check_sh 'hermes-ssh dry-run no longer promises to raise a terminal app' \
  '! "$SCRIPT" --source hermes-ssh --name x --dry-run | grep -q "fall back to raising"'

# Root cause 6: a Discord agent key opened a Chrome tab showing the web
# client instead of the desktop app, which the user called unusable.
check_sh 'https discord channel URL becomes a discord:// deep link' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   [ "$(discord_deep_link https://discord.com/channels/1/2/3)" = "discord://-/channels/1/2/3" ]'

check_sh 'deep link keeps the required - placeholder host' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   discord_deep_link https://discord.com/channels/1/2 | grep -q "^discord://-/channels/"'

check_sh 'a channel URL without a message id still converts' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   [ "$(discord_deep_link https://discord.com/channels/9/8)" = "discord://-/channels/9/8" ]'

check_sh 'the legacy discordapp.com host converts too' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   [ "$(discord_deep_link https://discordapp.com/channels/1/2)" = "discord://-/channels/1/2" ]'

check_sh 'an already-deep link is left alone' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   [ "$(discord_deep_link discord://-/channels/1/2)" = "discord://-/channels/1/2" ]'

check_sh 'a non-channel URL is passed through rather than guessed at' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   [ "$(discord_deep_link https://example.com/x)" = "https://example.com/x" ]'

check_sh 'Discord deep links explicitly activate the desktop app' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   mkdir -p "$TMP_DIR/Discord.app";
   printf "#!/bin/sh\nprintf \"%%s\\n\" \"\$*\" > \"$TMP_DIR/opened\"\n" > "$TMP_DIR/bin/open";
   chmod +x "$TMP_DIR/bin/open";
   PATH="$TMP_DIR/bin:$PATH" DISCORD_APP_PATH="$TMP_DIR/Discord.app" \
     open_discord_url https://discord.com/channels/1/2/3;
   [ "$(cat "$TMP_DIR/opened")" = "-a Discord discord://-/channels/1/2/3" ]'

if [ -d /Applications/Discord.app ]; then
  # This is the target Mac, not an isolated Linux fixture; the hard-coded
  # system application path is intentionally authoritative and cannot be
  # hidden with PATH/HOME stubs.
  pass 'installed Discord app is detected on the target Mac'
else
  check_sh 'no Discord app means the browser, not a dead scheme' \
    'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
     printf "#!/bin/sh\necho \"OPENED $*\" >> \"$TMP_DIR/opened\"\n" > "$TMP_DIR/bin/open";
     chmod +x "$TMP_DIR/bin/open";
     printf "#!/bin/sh\nexit 1\n" > "$TMP_DIR/bin/pgrep"; chmod +x "$TMP_DIR/bin/pgrep";
     PATH="$TMP_DIR/bin:$PATH"; HOME="$TMP_DIR"; DISCORD_APP_PATH=;
     ! discord_app_present'
fi

# Root cause 7: a desktop Claude/Codex session has no tty and no cmux surface,
# and a hook older than --app recorded no app either, so the key did nothing.
check_sh 'claude-code maps to the Claude desktop app' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   [ "$(desktop_app_for_source claude-code)" = "Claude" ]'

check_sh 'codex-cli maps to ChatGPT, the real installed bundle' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   [ "$(desktop_app_for_source codex-cli)" = "ChatGPT" ]'

check_sh 'cursor-agent maps to the installed Cursor bundle' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   [ "$(desktop_app_for_source cursor-agent)" = "Cursor" ]'

# Cursor's Agents window is not an ordinary editor workspace: in current Glass
# builds `windowsState.openedWindows` is empty even with several agents open.
# Cursor does expose an exact agent selector, keyed by the hook's
# `conversation_id`.  Validate that id against Cursor's local search database
# before invoking the scheme, then verify Cursor persisted the same selected id.
check_sh 'a Cursor conversation UUID becomes an exact local-agent deep link' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   [ "$(deep_link_for_agent Cursor acfaf394-68ae-40ae-91de-d6ec2b5d7774)" = \
     "cursor://anysphere.cursor-deeplink/background-agent?bcId=acfaf394-68ae-40ae-91de-d6ec2b5d7774" ]'

check_sh 'Cursor refuses a path-like conversation id' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   ! deep_link_for_agent Cursor "../../etc/passwd"'

check_sh 'Cursor local conversation evidence accepts one live exact id' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   db="$TMP_DIR/cursor-conversations.db";
   sqlite3 "$db" "CREATE TABLE conversations(source TEXT, scope TEXT, id TEXT, is_archived INTEGER); INSERT INTO conversations VALUES('"'"'local'"'"','"'"''"'"','"'"'acfaf394-68ae-40ae-91de-d6ec2b5d7774'"'"',0);";
   CURSOR_CONVERSATION_DB="$db" cursor_local_agent_is_live acfaf394-68ae-40ae-91de-d6ec2b5d7774'

check_sh 'Cursor local conversation evidence refuses a missing id' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   db="$TMP_DIR/cursor-missing.db";
   sqlite3 "$db" "CREATE TABLE conversations(source TEXT, scope TEXT, id TEXT, is_archived INTEGER);";
   ! CURSOR_CONVERSATION_DB="$db" cursor_local_agent_is_live acfaf394-68ae-40ae-91de-d6ec2b5d7774'

check_sh 'Cursor local conversation evidence refuses duplicate identities' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   db="$TMP_DIR/cursor-duplicate.db";
   sqlite3 "$db" "CREATE TABLE conversations(source TEXT, scope TEXT, id TEXT, is_archived INTEGER); INSERT INTO conversations VALUES('"'"'local'"'"','"'"''"'"','"'"'acfaf394-68ae-40ae-91de-d6ec2b5d7774'"'"',0),('"'"'local'"'"','"'"''"'"','"'"'acfaf394-68ae-40ae-91de-d6ec2b5d7774'"'"',0);";
   ! CURSOR_CONVERSATION_DB="$db" cursor_local_agent_is_live acfaf394-68ae-40ae-91de-d6ec2b5d7774'

check_sh 'Cursor local conversation evidence refuses archived agents' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   db="$TMP_DIR/cursor-archived.db";
   sqlite3 "$db" "CREATE TABLE conversations(source TEXT, scope TEXT, id TEXT, is_archived INTEGER); INSERT INTO conversations VALUES('"'"'local'"'"','"'"''"'"','"'"'acfaf394-68ae-40ae-91de-d6ec2b5d7774'"'"',1);";
   ! CURSOR_CONVERSATION_DB="$db" cursor_local_agent_is_live acfaf394-68ae-40ae-91de-d6ec2b5d7774'

check_sh 'Cursor focus opens and verifies the exact selected agent' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   app_is_running() { return 0; };
   cursor_local_agent_is_live() { return 0; };
   cursor_selected_agent_id() { printf "%s\n" acfaf394-68ae-40ae-91de-d6ec2b5d7774; };
   app_is_frontmost() { return 0; };
   printf '"'"'#!/bin/sh\nprintf "%%s\\n" "$*" > "%s/cursor-open"\n'"'"' "$TMP_DIR" > "$TMP_DIR/bin/open";
   chmod +x "$TMP_DIR/bin/open"; PATH="$TMP_DIR/bin:$PATH";
   SESSION=acfaf394-68ae-40ae-91de-d6ec2b5d7774;
   cursor_agent_focus;
   [ "$(cat "$TMP_DIR/cursor-open")" = "cursor://anysphere.cursor-deeplink/background-agent?bcId=$SESSION" ]'

check_sh 'Cursor focus never opens a missing local agent' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   app_is_running() { return 0; };
   cursor_local_agent_is_live() { return 1; };
   printf '"'"'#!/bin/sh\ntouch "%s/cursor-opened-missing"\n'"'"' "$TMP_DIR" > "$TMP_DIR/bin/open";
   chmod +x "$TMP_DIR/bin/open"; PATH="$TMP_DIR/bin:$PATH";
   SESSION=acfaf394-68ae-40ae-91de-d6ec2b5d7774;
   ! cursor_agent_focus;
   [ ! -e "$TMP_DIR/cursor-opened-missing" ]'

check_sh 'Cursor focus reports failure when selected-agent readback disagrees' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   app_is_running() { return 0; };
   cursor_local_agent_is_live() { return 0; };
   cursor_selected_agent_id() { printf "%s\n" 550e8400-e29b-41d4-a716-446655440000; };
   app_is_frontmost() { return 0; };
   printf '"'"'#!/bin/sh\nexit 0\n'"'"' > "$TMP_DIR/bin/open";
   chmod +x "$TMP_DIR/bin/open"; PATH="$TMP_DIR/bin:$PATH";
   CURSOR_FOCUS_POLLS=1 SESSION=acfaf394-68ae-40ae-91de-d6ec2b5d7774;
   ! cursor_agent_focus'

check_sh 'cursor-agent uses exact conversation focus before generic app activation' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   cursor_agent_focus() { touch "$TMP_DIR/cursor-agent-focused"; return 0; };
   app_focus() { touch "$TMP_DIR/generic-app-focused"; return 0; };
   SOURCE=cursor-agent; NAME=x; CWD=/Users/example/project; APP_HINT=Cursor;
   SESSION=acfaf394-68ae-40ae-91de-d6ec2b5d7774; TTY_HINT=; SURFACE_HINT=; HERDR_PANE_HINT=;
   focus_agent;
   [ -e "$TMP_DIR/cursor-agent-focused" ] && [ ! -e "$TMP_DIR/generic-app-focused" ]'

check_sh 'cursor-agent refuses weaker fallbacks when exact conversation is missing' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   cursor_agent_focus() { return 1; };
   app_focus() { touch "$TMP_DIR/generic-app-focused-after-cursor-miss"; return 0; };
   cmux_focus() { touch "$TMP_DIR/cmux-focused-after-cursor-miss"; return 0; };
   SOURCE=cursor-agent; NAME=x; CWD=/Users/example/project; APP_HINT=Cursor;
   SESSION=acfaf394-68ae-40ae-91de-d6ec2b5d7774; TTY_HINT=; SURFACE_HINT=; HERDR_PANE_HINT=;
   ! focus_agent;
   [ ! -e "$TMP_DIR/generic-app-focused-after-cursor-miss" ] && [ ! -e "$TMP_DIR/cmux-focused-after-cursor-miss" ]'

check_sh 'a source with no desktop app returns failure, not a guess' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   ! desktop_app_for_source cmux'

check_sh 'the desktop fallback still refuses to launch a quit app' \
  'printf "#!/bin/sh\nexit 1\n" > "$TMP_DIR/bin/pgrep"; chmod +x "$TMP_DIR/bin/pgrep";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   ! app_is_running Claude'

# AppleScript `activate` returning zero is not evidence that anything became
# visible.  A headless/stale app process can accept it while cmux stays in
# front, which is the same false-success class this whole resolver prevents.
check_sh 'desktop app focus refuses when post-action frontmost read-back misses' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   app_is_running() { return 0; };
   run_osascript_timeout() { return 0; };
   app_is_frontmost() { return 1; };
   APP_HINT=Claude;
   ! app_focus'

# The desktop Claude app is NOT Claude Code.  A terminal session HAS a tty, so
# the source guess must not rescue it by raising an unrelated chat app.
check_sh 'a session with a tty never falls back to the desktop app guess' \
  'printf "#!/bin/sh\nexit 0\n" > "$TMP_DIR/bin/pgrep"; chmod +x "$TMP_DIR/bin/pgrep";
   printf "#!/bin/sh\nexit 1\n" > "$TMP_DIR/bin/osascript"; chmod +x "$TMP_DIR/bin/osascript";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   SOURCE=claude-code; NAME=x; CWD=/tmp; APP_HINT=; TTY_HINT=ttys009;
   ! focus_agent 2>&1 | grep -q "guessed from source"'

# Root cause 8: "the app opened, but not the tab I pressed".  Both desktop apps
# register a scheme that names a specific conversation.  The id must be that
# app's own id, though: a wrong id makes both apps fall back to a recent-chats
# list, so the press LOOKS successful while showing the wrong thing.
check_sh 'a Claude conversation UUID becomes a claude:// deep link' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   [ "$(deep_link_for_agent Claude 550e8400-e29b-41d4-a716-446655440000)" \
     = "claude://claude.ai/chat/550e8400-e29b-41d4-a716-446655440000" ]'

check_sh 'a Claude Code desktop session becomes an exact local deep link' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   [ "$(deep_link_for_agent Claude local_550e8400-e29b-41d4-a716-446655440000)" \
     = "claude://claude.ai/epitaxy/local_550e8400-e29b-41d4-a716-446655440000" ]'

check_sh 'Claude refuses a malformed local desktop session id' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   ! deep_link_for_agent Claude local_not-a-uuid'

check_sh 'a Codex thread id becomes a codex:// deep link' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   [ "$(deep_link_for_agent ChatGPT thread_01HXYZ)" = "codex://threads/thread_01HXYZ" ]'

# A Claude Code CLI session id is not a claude.ai conversation id.  Sending one
# would open the wrong conversation and report success.
check_sh 'a non-UUID session id is refused rather than guessed at' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   ! deep_link_for_agent Claude sess-01HXYZ'

check_sh 'a uuid-SHAPED but non-hex id is refused' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   ! deep_link_for_agent Claude zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz'

check_sh 'an empty session id yields no deep link' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   ! deep_link_for_agent Claude ""'

check_sh 'a path-like Codex id is refused' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   ! deep_link_for_agent ChatGPT "../../etc/passwd"'

check_sh 'an app with no known scheme gets no deep link' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   ! deep_link_for_agent cmux 550e8400-e29b-41d4-a716-446655440000'

check_sh 'Claude deep focus refuses success when selected-session read-back disagrees' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   app_is_running() { return 0; };
   app_selected_url() { printf "%s\n" "https://claude.ai/epitaxy/local_84c74384-2ac1-4efe-9135-b28a04b66266"; };
   app_is_frontmost() { return 0; };
   printf '"'"'#!/bin/sh\nexit 0\n'"'"' > "$TMP_DIR/bin/open";
   chmod +x "$TMP_DIR/bin/open"; PATH="$TMP_DIR/bin:$PATH";
   APP_HINT=Claude; SESSION=local_550e8400-e29b-41d4-a716-446655440000;
   APP_FOCUS_POLLS=1;
   ! app_focus_deep'

check_sh 'Codex deep focus succeeds only after exact selected-thread read-back' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   app_is_running() { return 0; };
   app_selected_url() { printf "%s\n" "app://codex/local/019fd98b-90b7-73b3-a804-8c3a496257f8"; };
   app_is_frontmost() { return 0; };
   printf '"'"'#!/bin/sh\nprintf "%%s\\n" "$*" > "%s/codex-open"\n'"'"' "$TMP_DIR" > "$TMP_DIR/bin/open";
   chmod +x "$TMP_DIR/bin/open"; PATH="$TMP_DIR/bin:$PATH";
   APP_HINT=ChatGPT; SESSION=019fd98b-90b7-73b3-a804-8c3a496257f8;
   app_focus_deep;
   [ "$(cat "$TMP_DIR/codex-open")" = "codex://threads/$SESSION" ]'

check_sh 'unavailable desktop read-back fails fast instead of polling for seconds' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   app_is_running() { return 0; };
   app_selected_url() {
     count=0; [ ! -e "$TMP_DIR/blank-read-count" ] || count=$(cat "$TMP_DIR/blank-read-count");
     count=$((count + 1)); printf "%s\n" "$count" > "$TMP_DIR/blank-read-count";
   };
   printf '"'"'#!/bin/sh\nexit 0\n'"'"' > "$TMP_DIR/bin/open";
   chmod +x "$TMP_DIR/bin/open"; PATH="$TMP_DIR/bin:$PATH";
   SOURCE=codex-cli; APP_HINT=ChatGPT;
   SESSION=019fd98b-90b7-73b3-a804-8c3a496257f8;
   APP_FOCUS_POLLS=40; APP_FOCUS_BLANK_POLLS=2;
   ! app_focus_deep;
   [ "$(cat "$TMP_DIR/blank-read-count")" -eq 2 ]'

check_sh 'desktop-hosted Claude Code UUID selects the exact local session route' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   app_is_running() { return 0; };
   app_selected_url() { printf "%s\n" "https://claude.ai/epitaxy/local_05734cca-ee0e-4590-9921-ae7d0ffa1d9c"; };
   app_is_frontmost() { return 0; };
   printf '"'"'#!/bin/sh\nprintf "%%s\\n" "$*" > "%s/claude-local-open"\n'"'"' "$TMP_DIR" > "$TMP_DIR/bin/open";
   chmod +x "$TMP_DIR/bin/open"; PATH="$TMP_DIR/bin:$PATH";
   SOURCE=claude-code; APP_HINT=Claude;
   SESSION=05734cca-ee0e-4590-9921-ae7d0ffa1d9c;
   APP_FOCUS_POLLS=1;
   app_focus_deep;
   [ "$(cat "$TMP_DIR/claude-local-open")" = "claude://claude.ai/epitaxy/local_$SESSION" ]'

# Root cause 9: a cmux-hosted Claude/Codex tab raised the NATIVE app.  The hook
# recorded no tty, and "no tty" was read as "must be desktop-hosted" -- absence
# of evidence treated as evidence of absence.  A live cmux tree says otherwise.
check_sh 'a live cmux tree blocks the desktop-app guess' \
  'printf "#!/bin/sh\necho %s\n" "'"'"'{\"surfaces\":[{\"ref\":\"surface:1\"}]}'"'"'" > "$TMP_DIR/bin/cmux";
   chmod +x "$TMP_DIR/bin/cmux";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   cmux_has_surfaces'

check_sh 'an empty cmux tree does not block the guess' \
  'printf "#!/bin/sh\necho \"{}\"\n" > "$TMP_DIR/bin/cmux"; chmod +x "$TMP_DIR/bin/cmux";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   ! cmux_has_surfaces'

check_sh 'a live cmux tree blocks an unrecorded running desktop app guess' \
  'printf "#!/bin/sh\necho %s\n" "'"'"'{\"surfaces\":[{\"ref\":\"surface:1\"}]}'"'"'" > "$TMP_DIR/bin/cmux";
   chmod +x "$TMP_DIR/bin/cmux";
   printf "#!/bin/sh\nexit 0\n" > "$TMP_DIR/bin/pgrep"; chmod +x "$TMP_DIR/bin/pgrep";
   printf "#!/bin/sh\nexit 0\n" > "$TMP_DIR/bin/osascript"; chmod +x "$TMP_DIR/bin/osascript";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   SOURCE=claude-code; NAME=x; CWD=/nope; APP_HINT=; TTY_HINT=;
   ! focus_agent 2>&1 | grep -q "guessed from source"'

# ...but a QUIT app plus a live cmux is the cmux case, and must not silently
# succeed by activating something unrelated.
check_sh 'a quit app with a live cmux reports failure instead of guessing' \
  'printf "#!/bin/sh\necho %s\n" "'"'"'{\"surfaces\":[{\"ref\":\"surface:1\"}]}'"'"'" > "$TMP_DIR/bin/cmux";
   chmod +x "$TMP_DIR/bin/cmux";
   printf "#!/bin/sh\nexit 1\n" > "$TMP_DIR/bin/pgrep"; chmod +x "$TMP_DIR/bin/pgrep";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   SOURCE=claude-code; NAME=x; CWD=/nope; APP_HINT=; TTY_HINT=;
   focus_agent 2>&1 | grep -q "cmux has live surfaces"'

# Root cause 10: "claude and codex buttons only open cmux". A cwd is not an
# identity. When the hook records no tty, the resolver fell back to matching a
# directory, and every tab open in that repo matches -- so the press focused an
# arbitrary cmux tab and reported success. The surface id fixes this at the
# source: the agent names its own tab. Current cmux still needs the tab's
# workspace/window context to resolve a short surface ref globally.
check_sh 'a recorded short surface is focused with its resolved ancestry' \
  'cat > "$TMP_DIR/bin/cmux" <<CMUXEOF
#!/bin/sh
echo "\$@" >> "$TMP_DIR/calls"
if [ "\$1" = tree ]; then
  echo '\''{"active":{"surface_ref":"surface:29"},"windows":[{"ref":"window:7","workspaces":[{"ref":"workspace:8","panes":[{"ref":"pane:9","surfaces":[{"ref":"surface:29"}]}]}]}]}'\''
fi
exit 0
CMUXEOF
   chmod +x "$TMP_DIR/bin/cmux";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   SURFACE_HINT=surface:29; SESSION=; CWD=/w; TTY_HINT=; NAME=x; SOURCE=claude-code;
   cmux_focus >/dev/null 2>&1 &&
   grep -q "focus-panel --panel surface:29 --workspace workspace:8 --window window:7" "$TMP_DIR/calls"'

check_sh 'a malformed surface hint is ignored rather than sent to cmux' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   SURFACE_HINT="not-a-ref"; ! is_cmux_ref "$SURFACE_HINT"'

# `tree --all` is the global view, while `list-panels` only describes the
# selected workspace on current cmux builds.  Once the global view says a cwd
# is ambiguous, retrying against the partial view makes the current surface
# look uniquely identified and turns a refusal into a false success.
check_sh 'an ambiguous global tree is not rescued by partial list-panels output' \
  'cat > "$TMP_DIR/bin/cmux" <<CMUXEOF
#!/bin/sh
if [ "\$1 \$2 \$3" = "tree --all --json" ]; then
  echo '\''{"windows":[{"workspaces":[{"panes":[{"surfaces":[{"ref":"surface:1","cwd":"/Users/example/shared"},{"ref":"surface:2","cwd":"/Users/example/shared"}]}]}]}]}'\''
elif [ "\$1 \$2" = "list-panels --json" ]; then
  echo '\''{"surfaces":[{"ref":"surface:1","cwd":"/Users/example/shared"}]}'\''
elif [ "\$1" = "focus-panel" ]; then
  touch "$TMP_DIR/partial-focus-ran"
fi
exit 0
CMUXEOF
   chmod +x "$TMP_DIR/bin/cmux";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   SURFACE_HINT=; SESSION=; CWD=/Users/example/shared; TTY_HINT=; NAME=x; SOURCE=claude-code;
   ! cmux_focus; [ ! -e "$TMP_DIR/partial-focus-ran" ]'

# A recorded desktop host is authoritative.  If it has quit, the session is
# gone; falling through to cmux can focus an unrelated tab with the same cwd.
check_sh 'a quit recorded desktop app stops before terminal resolution' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   app_focus_deep() { return 1; };
   app_focus() { error "not running"; return 1; };
   cmux_focus() { touch "$TMP_DIR/quit-app-cmux-ran"; return 0; };
   SOURCE=claude-code; NAME=x; CWD=/Users/example/shared;
   APP_HINT=ThisAppDoesNotExist; TTY_HINT=; SESSION=; SURFACE_HINT=;
   ! focus_agent; [ ! -e "$TMP_DIR/quit-app-cmux-ran" ]'

check_sh 'workspace selection carries the resolved window context' \
  'printf "#!/bin/sh\necho \"\$@\" > \"$TMP_DIR/workspace-call\"\nexit 0\n" > "$TMP_DIR/bin/cmux";
   chmod +x "$TMP_DIR/bin/cmux";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   cmux_select_workspace workspace:13 window:2;
   grep -q "select-workspace --workspace workspace:13 --window window:2" "$TMP_DIR/workspace-call"'

check_sh 'panel focus carries workspace and window context' \
  'printf "#!/bin/sh\necho \"\$@\" > \"$TMP_DIR/panel-call\"\nexit 0\n" > "$TMP_DIR/bin/cmux";
   chmod +x "$TMP_DIR/bin/cmux";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   cmux_focus_panel_in_context surface:31 workspace:13 window:2;
   grep -q "focus-panel --panel surface:31 --workspace workspace:13 --window window:2" "$TMP_DIR/panel-call"'

check_sh 'Herdr focus succeeds only after current-pane read-back matches' \
  'cat > "$TMP_DIR/bin/herdr" <<HERDREOF
#!/bin/sh
case "\$1 \$2" in
  "pane get") echo '\''{"result":{"pane":{"pane_id":"w1:p7"}}}'\'' ;;
  "agent focus") exit 0 ;;
  "pane current") echo '\''{"result":{"pane":{"pane_id":"w1:p7"}}}'\'' ;;
esac
HERDREOF
   chmod +x "$TMP_DIR/bin/herdr";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   activate_herdr_terminal() { return 0; };
   PATH="$TMP_DIR/bin:$PATH"; HERDR_PANE_HINT=w1:p7;
   herdr_focus'

check_sh 'Herdr focus refuses a wrong current pane despite exit zero' \
  'cat > "$TMP_DIR/bin/herdr" <<HERDREOF
#!/bin/sh
case "\$1 \$2" in
  "pane get") echo '\''{"result":{"pane":{"pane_id":"w1:p7"}}}'\'' ;;
  "agent focus") exit 0 ;;
  "pane current") echo '\''{"result":{"pane":{"pane_id":"w1:p2"}}}'\'' ;;
esac
HERDREOF
   chmod +x "$TMP_DIR/bin/herdr";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH"; HERDR_PANE_HINT=w1:p7;
   ! herdr_focus'

# A remote agent viewed through an SSH shell is a real Herdr pane, but Herdr
# does not classify that local shell as an agent.  `agent focus` therefore
# returns agent_not_found even though the pane exists.  Focus its exact
# workspace and tab, then require the same current-pane read-back as usual.
check_sh 'a mapped Hermes SSH pane focuses through exact workspace and tab' \
  'cat > "$TMP_DIR/bin/herdr" <<HERDREOF
#!/bin/sh
case "\$1 \$2" in
  "pane get") echo '\''{"result":{"pane":{"pane_id":"w4:p1","workspace_id":"w4","tab_id":"w4:t1"}}}'\'' ;;
  "agent focus") echo '\''{"error":{"code":"agent_not_found"}}'\'' ;;
  "workspace focus") [ "\$3" = w4 ] && touch "$TMP_DIR/hermes-workspace-focused" ;;
  "tab focus") [ "\$3" = w4:t1 ] && touch "$TMP_DIR/hermes-tab-focused" ;;
  "pane current")
    if [ -e "$TMP_DIR/hermes-workspace-focused" ] && [ -e "$TMP_DIR/hermes-tab-focused" ]; then
      echo '\''{"result":{"pane":{"pane_id":"w4:p1"}}}'\''
    else
      echo '\''{"result":{"pane":{"pane_id":"w5:p1"}}}'\''
    fi ;;
esac
HERDREOF
   chmod +x "$TMP_DIR/bin/herdr";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   activate_herdr_terminal() { return 0; };
   PATH="$TMP_DIR/bin:$PATH"; HERDR_PANE_HINT=w4:p1;
   herdr_focus && [ -e "$TMP_DIR/hermes-workspace-focused" ] && [ -e "$TMP_DIR/hermes-tab-focused" ]'

check_sh 'hermes-ssh main routing prefers a recorded Herdr pane over legacy host lookup' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   herdr_focus() { touch "$TMP_DIR/hermes-recorded-herdr-ran"; return 0; };
   focus_hermes_ssh() { touch "$TMP_DIR/hermes-legacy-ssh-ran"; return 0; };
   main --source hermes-ssh --name remote --herdr-pane w4:p1;
   [ -e "$TMP_DIR/hermes-recorded-herdr-ran" ] && [ ! -e "$TMP_DIR/hermes-legacy-ssh-ran" ]'

# A live pane can outlast the Terminal client that was viewing it.  Selecting
# the pane in Herdr's server is not enough: without a client, nothing becomes
# visible and the press looks dead.  Starting a viewer is safe here because
# herdr_focus has already proved the exact pane exists; this is not a generic
# "open a terminal and hope" fallback.
check_sh 'a verified Herdr pane starts one default-Terminal viewer when no client exists' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   herdr_client_tty() { [ -e "$TMP_DIR/herdr-viewer-started" ] && printf "ttys003\n"; };
   launch_herdr_terminal_client() { touch "$TMP_DIR/herdr-viewer-started"; };
   select_herdr_terminal_tty() { [ "$1" = ttys003 ] && touch "$TMP_DIR/herdr-viewer-selected"; };
   HERDR_CLIENT_POLLS=1 activate_herdr_terminal;
   [ -e "$TMP_DIR/herdr-viewer-started" ] && [ -e "$TMP_DIR/herdr-viewer-selected" ]'

# A process can move from a Herdr-hosted shell into cmux while retaining the
# old HERDR_PANE environment variable.  A second inherited variable can be
# stale too, so an existing surface UUID is not enough: its tty must be the
# exact tty the agent hook recorded.  The positive case preserves the useful
# recovery; the negative case replays the live 2026-08-07 failure, where the
# UUID named a Herdr tab on ttys005 while the live Codex process was on ttys000.
check_sh 'a missing Herdr pane falls through to a recorded cmux surface on the same tty' \
  'cat > "$TMP_DIR/bin/cmux" <<CMUXEOF
#!/bin/sh
echo '\''{"windows":[{"workspaces":[{"panes":[{"surfaces":[{"ref":"surface:8","id":"B854DA82-6647-4ED2-AC3E-0F679082354D","tty":"ttys007"}]}]}]}]}'\''
CMUXEOF
   chmod +x "$TMP_DIR/bin/cmux";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   herdr_focus() { return 2; };
   cmux_focus() { touch "$TMP_DIR/stale-herdr-cmux-ran"; return 0; };
   SOURCE=codex-cli; NAME=x; CWD=/Users/example/project;
   APP_HINT=; TTY_HINT=ttys007; SESSION=;
   SURFACE_HINT=B854DA82-6647-4ED2-AC3E-0F679082354D;
   HERDR_PANE_HINT=w1:p1;
   focus_agent; [ -e "$TMP_DIR/stale-herdr-cmux-ran" ]'

check_sh 'a missing Herdr pane refuses a recorded cmux surface on another tty' \
  'cat > "$TMP_DIR/bin/cmux" <<CMUXEOF
#!/bin/sh
echo '\''{"windows":[{"workspaces":[{"panes":[{"surfaces":[{"ref":"surface:1","id":"B854DA82-6647-4ED2-AC3E-0F679082354D","tty":"ttys005"}]}]}]}]}'\''
CMUXEOF
   chmod +x "$TMP_DIR/bin/cmux";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH";
   herdr_focus() { return 2; };
   cmux_focus() { touch "$TMP_DIR/wrong-stale-herdr-cmux-ran"; return 0; };
   cmux_focus_recorded_tty() { return 1; };
   SOURCE=codex-cli; NAME=x; CWD=/Users/example/project;
   APP_HINT=; TTY_HINT=ttys000; SESSION=;
   SURFACE_HINT=B854DA82-6647-4ED2-AC3E-0F679082354D;
   HERDR_PANE_HINT=w1:p1;
   ! focus_agent; [ ! -e "$TMP_DIR/wrong-stale-herdr-cmux-ran" ]'

# A session can move from Herdr into cmux while inheriting BOTH obsolete host
# hints.  The tty is a live kernel identity and is stronger than either stale
# hint: when it maps uniquely, the press must use it instead of refusing or
# focusing the reused surface UUID now owned by another tab.
check_sh 'a missing Herdr pane recovers through its exact live tty despite a stale surface UUID' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   herdr_focus() { return 2; };
   cmux_surface_matches_tty() { return 1; };
   cmux_focus_recorded_tty() { touch "$TMP_DIR/stale-host-tty-ran"; return 0; };
   SOURCE=codex-cli; NAME=x; CWD=/Users/example/project;
   APP_HINT=cmux; TTY_HINT=ttys000; SESSION=019fd98b-90b7-73b3-a804-8c3a496257f8;
   SURFACE_HINT=B854DA82-6647-4ED2-AC3E-0F679082354D;
   HERDR_PANE_HINT=w1:p1;
   focus_agent; [ -e "$TMP_DIR/stale-host-tty-ran" ]'

check_sh 'an orphaned Codex terminal thread recovers in the exact Codex app tab' \
  'FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   herdr_focus() { return 2; };
   cmux_surface_matches_tty() { return 1; };
   cmux_focus_recorded_tty() { return 1; };
   codex_thread_is_local() { return 0; };
   app_focus_deep() {
     [ "$APP_HINT" = ChatGPT ] && touch "$TMP_DIR/codex-app-recovery";
   };
   SOURCE=codex-cli; NAME=cx; CWD=/tmp; APP_HINT=cmux;
   SESSION=019fd98b-90b7-73b3-a804-8c3a496257f8;
   TTY_HINT=ttys000; SURFACE_HINT=stale-surface; HERDR_PANE_HINT=w1:p1;
   focus_agent;
   [ -e "$TMP_DIR/codex-app-recovery" ]'

check_sh 'Herdr focus raises the Terminal tab hosting the client' \
  'cat > "$TMP_DIR/bin/herdr" <<HERDREOF
#!/bin/sh
case "\$1 \$2" in
  "pane get") echo '\''{"result":{"pane":{"pane_id":"w1:p7"}}}'\'' ;;
  "agent focus") exit 0 ;;
  "pane current") echo '\''{"result":{"pane":{"pane_id":"w1:p7"}}}'\'' ;;
esac
HERDREOF
   cat > "$TMP_DIR/bin/ps" <<PSEOF
#!/bin/sh
printf '\''%s\n'\'' '\''?? /opt/homebrew/opt/herdr/bin/herdr server'\'' '\''ttys007 /opt/homebrew/bin/herdr'\''
PSEOF
   cat > "$TMP_DIR/bin/osascript" <<OSAEOF
#!/bin/sh
printf '\''%s\n'\'' "\$*" > "$TMP_DIR/osascript-args"
cat >/dev/null
OSAEOF
   chmod +x "$TMP_DIR/bin/herdr" "$TMP_DIR/bin/ps" "$TMP_DIR/bin/osascript";
   FOCUS_AGENT_LIB_ONLY=1 . "$SCRIPT";
   PATH="$TMP_DIR/bin:$PATH"; HERDR_PANE_HINT=w1:p7;
   herdr_focus && grep -q -- "- ttys007" "$TMP_DIR/osascript-args"'

printf '%d/%d passed\n' "$passed" "$total"
[ "$passed" -eq "$total" ]
