#!/usr/bin/env python3
"""model_tier_router — determines best model tier given quota state + peak hours + task tier.

Called by kanban dispatcher BEFORE spawning a worker.
Returns JSON: {"tier": "air", "model": "glm-4.5-air", "reason": "quota=PLENTYFUL_peak"}

Usage:
  python3 model_tier_router.py --task-tier mid
  python3 model_tier_router.py --task-tier heavy --quota-state CRITICAL
"""

from __future__ import annotations
import json
import sys
import urllib.request
import datetime

QUOTA_ENDPOINT = "http://localhost:9099/quota"

MODEL_MAP = {
    "flash": "glm-4.5-flash",
    "air": "glm-4.5-air",
    "mid": "glm-4.5",
    "heavy": "glm-5.2",
}

TIER_ORDER = ["flash", "air", "mid", "heavy"]

QUOTA_STATE_TIERS = {
    "PLENTYFUL": ["flash", "air", "mid", "heavy"],
    "MODERATE":  ["flash", "air", "mid"],
    "TIGHT":     ["flash", "air"],
    "CRITICAL":  ["flash"],
}


def determine_quota_state(data: dict) -> tuple[str, str]:
    """Determine quota state from proxy /quota data.
    
    Returns (state, reason) where reason is like "friend_moderate".
    Falls back to CRITICAL if no key available.
    """
    # Get the best (unlocked) key with the most headroom
    best_hours = -1
    active_key = None
    windows = None
    
    for name in ["friend", "ours"]:  # prefer friend first
        k = data.get(name, {})
        if k.get("locked", True):
            continue
        active_key = name
        windows = k.get("windows", [])
        # Find max hours_left across windows
        for w in windows:
            hl = w.get("hours_left")
            if hl is not None and hl > best_hours:
                best_hours = hl
    
    if not active_key or not windows:
        return "CRITICAL", "no_keys_available"
    
    will_exhaust = any(w.get("will_exhaust", False) for w in windows)
    
    if best_hours > 48 and not will_exhaust:
        return "PLENTYFUL", f"{active_key}_plentyful"
    elif best_hours > 12:
        return "MODERATE", f"{active_key}_moderate"
    elif best_hours > 2:
        return "TIGHT", f"{active_key}_tight"
    else:
        return "CRITICAL", f"{active_key}_critical_or_exhausted"


def is_peak_hours() -> tuple[bool, str]:
    """Check if current UTC hour is within peak window (06:00-10:00)."""
    hour = datetime.datetime.utcnow().hour
    peak = 6 <= hour < 10
    return peak, "peak" if peak else "off_peak"


def select_tier(required_tier: str, quota_state: str,
                peak: bool, peak_reason: str) -> dict:
    """Select the cheapest viable model tier.
    
    Args:
        required_tier: Minimum tier the task needs (flash/air/mid/heavy)
        quota_state: PLENTYFUL/MODERATE/TIGHT/CRITICAL
        peak: Whether current time is peak hours
    
    Returns:
        dict with tier, model, reason
    """
    allowed = QUOTA_STATE_TIERS.get(quota_state, ["flash"])
    max_tier = "air" if peak else "heavy"
    
    req_idx = TIER_ORDER.index(required_tier) if required_tier in TIER_ORDER else 0
    max_idx = TIER_ORDER.index(max_tier) if max_tier in TIER_ORDER else 3
    
    # Pick cheapest (lowest index) tier meeting requirements
    best = None
    best_idx = None
    for i, tier in enumerate(TIER_ORDER):
        if tier not in allowed:
            continue
        if i > max_idx:
            continue
        if i >= req_idx:
            if best_idx is None or i < best_idx:
                best = tier
                best_idx = i
    
    if best:
        return {
            "tier": best,
            "model": MODEL_MAP[best],
            "reason": f"quota={quota_state}_{peak_reason}"
        }
    else:
        return {
            "tier": None,
            "model": None,
            "reason": f"no_tier req={required_tier} state={quota_state} peak={peak}"
        }


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Select cheapest viable model tier for a kanban task")
    parser.add_argument("--task-tier", default="flash",
                        choices=["flash", "air", "mid", "heavy"],
                        help="Required model tier for the task (default: flash)")
    parser.add_argument("--quota-state", choices=list(QUOTA_STATE_TIERS.keys()),
                        help="Override quota state (skip proxy query)")
    args = parser.parse_args()
    
    quota_state = args.quota_state
    reason = "override"
    
    if not quota_state:
        try:
            resp = urllib.request.urlopen(QUOTA_ENDPOINT, timeout=5)
            data = json.loads(resp.read())
        except Exception as e:
            # Fall back to flash on quota unreachable
            result = {
                "tier": "flash",
                "model": "glm-4.5-flash",
                "reason": f"quota_unreachable_{e}"
            }
            print(json.dumps(result))
            sys.exit(0)
        
        quota_state, reason = determine_quota_state(data)
    
    peak, peak_reason = is_peak_hours()
    result = select_tier(args.task_tier, quota_state, peak, peak_reason)
    result["quota_state"] = quota_state
    result["peak_hours"] = peak
    print(json.dumps(result))


if __name__ == "__main__":
    main()
