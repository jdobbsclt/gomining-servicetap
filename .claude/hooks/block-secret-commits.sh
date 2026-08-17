#!/bin/bash
set -euo pipefail

input=$(cat)

# tool_name is a plain identifier (no special chars to escape) -- safe to
# pull out with grep/sed. Avoids depending on jq or python, neither of
# which can be assumed present (this machine has no working python on
# PATH -- only a non-functional Windows Store stub).
tool_name=$(echo "$input" | grep -o '"tool_name"[[:space:]]*:[[:space:]]*"[^"]*"' | sed 's/.*"\([^"]*\)"$/\1/' || true)

if [[ "$tool_name" != "Bash" ]]; then
  exit 0
fi

# Don't bother precisely parsing tool_input.command out of the JSON --
# just check the raw payload for the substring. JSON-escaping only
# touches quotes/backslashes, not plain words, so this is reliable enough
# for a yes/no check.
if [[ "$input" != *"git commit"* ]]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

staged=$(git diff --cached 2>/dev/null || true)

if [[ -z "$staged" ]]; then
  exit 0
fi

if echo "$staged" | grep -qE 'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'; then
  echo '{"hookSpecificOutput":{"permissionDecision":"deny"},"systemMessage":"Blocked: staged changes contain what looks like a JWT (e.g. a GoMining access_token). Never commit session cookies -- they belong only in GitHub Secrets. Run git reset to unstage if this is a mistake."}'
  exit 0
fi

if echo "$staged" | grep -qE '"(access_token|refresh_token|cf_clearance)"'; then
  echo '{"hookSpecificOutput":{"permissionDecision":"deny"},"systemMessage":"Blocked: staged changes contain a known GoMining cookie field name (access_token/refresh_token/cf_clearance). These must never be committed -- they belong only in GitHub Secrets. Run git reset to unstage if this is a mistake."}'
  exit 0
fi

exit 0
