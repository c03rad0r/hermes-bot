#!/usr/bin/env python3
"""zai-quota-gate.py — hard preflight gate for LLM-driven crons.

Checks if z.ai quota is healthy enough to run an LLM-driven task.
Returns JSON status + exit code 0 (ok) or 1 (blocked).

Usage:
  python3 zai-quota-gate.py
  → {"allowed": true, "reason": "friend_key_available", "active_key": "friend"}
  
  python3 zai-quota-gate.py --reason
  → Allows but explains the quota situation

Cron scripts should call this first and exit 0 if blocked:
  if ! python3 /path/to/zai-quota-gate.py >/dev/null 2>&1; then exit 0; fi
"""
import json, os, sys, urllib.request

def check_proxy_quota():
    """Check quota via proxy's /quota endpoint (preferred, freshest data)."""
    try:
        req = urllib.request.Request("http://localhost:9099/quota",
                                      method="GET", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        
        ours = data.get("ours", {})
        friend = data.get("friend", {})
        
        ours_locked = ours.get("locked", True)
        friend_locked = friend.get("locked", True)
        
        # Get max pct for reasoning
        ours_max = max((w.get("used_pct", 0) for w in ours.get("windows", [])), default=100)
        friend_max = max((w.get("used_pct", 0) for w in friend.get("windows", [])), default=100)
        
        if not ours_locked or not friend_locked:
            active = "friend" if not friend_locked else "ours"
            return {"allowed": True, "reason": f"{active}_key_available",
                    "active_key": active,
                    "ours_pct": ours_max, "friend_pct": friend_max}
        else:
            return {"allowed": False, "reason": "both_keys_locked",
                    "ours_pct": ours_max, "friend_pct": friend_max}
    except Exception as e:
        return None  # Fallback

def check_state_file():
    """Fallback: read zai_state.json."""
    state_file = os.path.expanduser("~/.hermes/bot/zai_state.json")
    try:
        with open(state_file) as f:
            d = json.load(f)
        
        if d.get("quota_pause", False):
            return {"allowed": False, "reason": "quota_pause"}
        if d.get("throttle", False):
            return {"allowed": False, "reason": "throttled"}
        return {"allowed": True, "reason": "state_file_ok"}
    except Exception:
        return None

def main():
    # Try proxy first
    result = check_proxy_quota()
    if result is None:
        result = check_state_file()
    if result is None:
        result = {"allowed": True, "reason": "no_data_optimistic"}
    
    print(json.dumps(result))
    return 0 if result["allowed"] else 1

if __name__ == "__main__":
    sys.exit(main())
