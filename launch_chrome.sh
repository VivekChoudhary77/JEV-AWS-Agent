#!/usr/bin/env bash
# Chrome ignores remote debugging on the default profile, so this uses a dedicated one.
set -euo pipefail

echo "Log in as the agent IAM user in this window, then run the agent."

exec google-chrome \
  --remote-debugging-port=9222 \
  --remote-debugging-address=127.0.0.1 \
  --user-data-dir="$HOME/.jev-agent-chrome" \
  --no-first-run \
  "https://us-east-1.console.aws.amazon.com/console/home?region=us-east-1"
