#!/usr/bin/env bash
# Start a fresh Herdr session for the end-to-end tests, apart from the default
# session: w1:t1 holds panes w1:p1 | w1:p2, and w1:t2 holds w1:p3.
set -euo pipefail
SESSION=${HERDR_E2E_SESSION:-herdr-web-e2e}
WORKDIR=$(mktemp -d)

herdr session stop "$SESSION" >/dev/null 2>&1 || true
herdr session delete "$SESSION" >/dev/null 2>&1 || true
nohup herdr --session "$SESSION" server >/dev/null 2>&1 &
for _ in $(seq 40); do
  herdr --session "$SESSION" status 2>/dev/null | grep -q 'status: running' && break
  sleep 0.25
done

herdr --session "$SESSION" workspace create --label test --cwd "$WORKDIR" --focus >/dev/null
herdr --session "$SESSION" pane split w1:p1 --direction right --no-focus >/dev/null
herdr --session "$SESSION" tab create --workspace w1 --cwd "$WORKDIR" --no-focus >/dev/null
sleep 1
# The checks expect bash's line editor in UTF-8, whatever the user's own shell
# and prompt are.
for pane in w1:p1 w1:p2 w1:p3; do
  herdr --session "$SESSION" pane run "$pane" \
    "exec env LANG=C.UTF-8 LC_ALL=C.UTF-8 PS1='\$ ' bash --norc --noprofile" >/dev/null
done
sleep 1
for pane in w1:p1 w1:p2 w1:p3; do
  herdr --session "$SESSION" pane run "$pane" clear >/dev/null
done
echo "Herdr session $SESSION is ready"
