#!/usr/bin/env bash
set -euo pipefail

# Usage: ./scripts/init_repo.sh <git_remote_url>
# Example: ./scripts/init_repo.sh https://github.com/you/upstox-trading-bot.git

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 <git_remote_url>"
  exit 1
fi

REMOTE_URL="$1"

echo "Initializing git repository..."
git init
git add .
git commit -m "Initial trading bot setup"

echo "Adding remote: $REMOTE_URL"
git remote add origin "$REMOTE_URL"

echo "Creating main branch and pushing..."
git branch -M main
git push -u origin main

echo "Done. Repository pushed to $REMOTE_URL"