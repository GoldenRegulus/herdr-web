#!/bin/bash
nohup uv run herdr-web "$@" > output.log 2>&1 &
echo $! > herdr-web.pid
echo "Started server with PID: $!"
