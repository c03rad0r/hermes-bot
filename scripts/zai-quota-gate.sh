#!/usr/bin/env bash
# zai-quota-gate.sh — hard preflight gate for LLM-driven crons.
# Returns 0 if z.ai quota is healthy enough to run.
# Returns 1 if both keys are exhausted — caller should abort silently.
#
# Usage: in cron script header:
#   if ! ~/.hermes/profiles/manager/scripts/zai-quota-gate.sh; then exit 0; fi

STATE_FILE="$HOME/.hermes/bot/zai_state.json"
QUOTA_URL="http://localhost:9099/quota"

# Try proxy quota endpoint first (fresher data)
if result=$(curl -sf --connect-timeout 3 "$QUOTA_URL" 2>/dev/null); then
    friend_locked=$(echo "$result" | python3 -c "
import sys, json
d = json.load(sys.stdin)
f = d.get('friend', {})
# Only blocked when BOTH keys locked
if f.get('locked', True):
    print('YES')
else:
    print('NO')
" 2>/dev/null)
    
    ours_locked=$(echo "$result" | python3 -c "
import sys, json
d = json.load(sys.stdin)
o = d.get('ours', {})
if o.get('locked', True):
    print('YES')
else:
    print('NO')
" 2>/dev/null)

    if [ "$ours_locked" = "YES" ] && [ "$friend_locked" = "YES" ]; then
        exit 1  # Both locked — wait
    fi
    exit 0  # At least one key available
fi

# Fallback: read zai_state.json
if [ -f "$STATE_FILE" ]; then
    blocked=$(python3 -c "
import json
d = json.load(open('$STATE_FILE'))
# quota_pause = both keys exhausted
if d.get('quota_pause', False):
    print('YES')
elif d.get('throttle', False):
    print('YES')
else:
    print('NO')
" 2>/dev/null)
    
    if [ "$blocked" = "YES" ]; then
        exit 1
    fi
    exit 0
fi

# No data at all — allow (optimistic)
exit 0
