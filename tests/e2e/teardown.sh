#!/usr/bin/env bash
# Stop and remove the end-to-end test session.
SESSION=${HERDR_E2E_SESSION:-herdr-web-e2e}
herdr session stop "$SESSION" >/dev/null 2>&1
herdr session delete "$SESSION" >/dev/null 2>&1
echo "Herdr session $SESSION removed"
