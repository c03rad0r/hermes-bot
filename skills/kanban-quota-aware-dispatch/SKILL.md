---
name: kanban-quota-aware-dispatch
description: "Route kanban tasks to appropriate model tiers based on Kalman-predicted quota headroom, peak-hour windows, and task complexity metadata. Keeps workers on the local proxy (no PPQ fallback) by downgrading models instead."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [kanban, quota, dispatch, kalman, model-selection, scheduling]
    related_skills: [zai-proxy-management, kalman-convergence-check, kanban-worker-management]
---

# Quota-Aware Kanban Dispatch

## Problem

Workers all use `glm-5.2` regardless of task complexity. During peak hours
(06:00–10:00 UTC) or when quota is tight this:
- Burns 3× quota per token on heavy models during peak
- Forces PPQ fallback (real money) when z.ai keys exhaust
- Blocks dispatch entirely when both keys hit thresholds

## Solution: Four-Levers

### 1. Model Tiering (price relative to glm-5.2)

| Tier | Model | Relative cost | Peak cost | Best for |
|------|-------|--------------|-----------|----------|
| `flash` | glm-4.5-flash | 0.11× | 0.33× | Formatting, simple edits, grep/search, test runs |
| `air` | glm-4.5-air | 0.22× | 0.66× | Boilerplate, mid-complexity |
| `mid` | glm-4.5 | 0.33× | 1.0× | Refactoring, moderate coding |
| `heavy` | glm-5.2 | 1.0× | 3.0× | Complex reasoning, architecture, debugging |

A `flash` task during peak costs **0.33×** vs `heavy` at **3.0×** = **9× savings**.

### 2. Quota State (from Kalman)

The Kalman filter (`burn_predictor.py`) outputs `hours_left`, `will_exhaust`,
`burn_rate_tph`. Map to quota state:

| State | Condition | Allowed tiers |
|-------|-----------|---------------|
| `PLENTYFUL` | `hours_left > 48` AND `!will_exhaust` | All |
| `MODERATE` | `hours_left > 12` | `flash`, `air`, `mid` |
| `TIGHT` | `hours_left > 2` | `flash`, `air` |
| `CRITICAL` | `hours_left < 2` OR `will_exhaust` | `flash` only |

Query via:
```bash
curl -s localhost:9099/quota | python3 -c "
import sys, json
d = json.load(sys.stdin)
# Get the active key's headroom
for key in ['ours', 'friend']:
    k = d.get(key, {})
    if not k.get('locked', True):
        print(f'active_key={key}')
        for w in k.get('windows', []):
            print(f"  {w['name']}: {w.get('used_pct','?')}%")
        break
"
```

### 3. Peak Hours

**06:00–10:00 UTC** — glm-5.2 burns 3× quota.
Rule: during peak, cap max tier to `air` regardless of quota state.

Check with:
```bash
HOUR=$(date -u +%H)
if [ "$HOUR" -ge 6 ] && [ "$HOUR" -lt 10 ]; then echo "PEAK"; else echo "OFF_PEAK"; fi
```

### 4. Kanban Task Model Tier Metadata

Every kanban task SHOULD carry a `model_tier` field:

| Value | Meaning | Example tasks |
|-------|---------|--------------|
| `flash` | Default. Simple/mechanical. | File moves, grep, test runs, formatting |
| `air` | Mid complexity. | Boilerplate generation, simple refactors |
| `mid` | Moderate. | Feature implementation, moderate debugging |
| `heavy` | Complex reasoning. | Architecture, code review, design docs |

When omitted, dispatcher assumes `flash` (conservative default).

## Implementation

### Layer 1: Proxy — X-Model-Tier header rewrite

The proxy at localhost:9099 should accept an `X-Model-Tier` HTTP header.
When present, the proxy picks the cheapest model in that tier that the
current quota state permits, instead of forwarding the client's `model`
field verbatim.

```python
# Pseudo-code for proxy rewrite:
MODEL_TIER_MAP = {
    'flash': ['glm-4.5-flash'],
    'air':   ['glm-4.5-air', 'glm-4.5-flash'],
    'mid':   ['glm-4.5', 'glm-4.5-air', 'glm-4.5-flash'],
    'heavy': ['glm-5.2', 'glm-4.5', 'glm-4.5-air', 'glm-4.5-flash'],
}

def pick_model(tier: str, quota_state: str) -> str:
    candidates = MODEL_TIER_MAP[tier]
    for model in candidates:
        if is_allowed(model, quota_state):
            return model
    return candidates[-1]  # safe fallback
```

This is already designed (see `zai-proxy-management` → `references/tiered-model-selection-design.md`)
but NOT YET IMPLEMENTED.

### Layer 2: Kalman — Quota state endpoint

Add a `quota_state` method to `burn_predictor.py` that returns the
enum from §2 above. Make it callable from the proxy and from dispatch
scripts.

```bash
python3 ~/.hermes/bot/burn_predictor.py --quota-state
# Returns: "TIGHT" or "PLENTYFUL"
```

### Layer 3: Dispatcher — Smart routing

Before spawning a worker, the kanban dispatcher:

1. Reads the task's `model_tier` (default: `flash`)
2. Checks `peak_hours` → if peak, cap max tier to `air`
3. Queries Kalman `quota_state` → determines allowed tiers
4. Picks the cheapest allowed tier that meets the task's required tier
5. Passes `model_tier` or a specific model down to the worker

```python
# Pseudo-code for dispatcher:
def select_model_for_task(task, quota_state, peak_hours):
    required_tier = task.get('model_tier', 'flash')
    max_tier = 'air' if peak_hours else 'heavy'
    allowed = get_allowed_tiers(quota_state)
    selected = max(allowed & {required_tier, max_tier})  # pick cheapest that fits
    return selected
```

### Layer 4: Cron — Off-peak scheduling

Tasks with `model_tier: heavy` should NOT be dispatched during peak
hours or when quota is tight. Instead, they get a `scheduled_at` field
and a cron picks them up at the next off-peak window.

```bash
# Example cron schedule for heavy tasks:
# 0 10 * * *  — dispatch any queued heavy tasks (just after peak ends)
# 0 22 * * *  — dispatch heavy tasks in cheap evening hours
```

## Verification

```bash
# Check quota state
python3 ~/.hermes/bot/burn_predictor.py --quota-state

# Check peak hours
date -u +%H  # if 06-10 → peak

# Check what model a task would get
./dispatch_simulate.py --task model_tier=mid

# Test proxy header rewrite
curl -s -X POST localhost:9099/v1/chat/completions \
  -H "X-Model-Tier: flash" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
# Should actually use glm-4.5-flash
```

## Pitfalls

- **Don't over-engineer model tier assignment.** Most tasks are `flash`.
  Only tasks that genuinely need reasoning get `heavy`. Start conservative.
- **Kalman convergence is unhealthy** as of 2026-07-07 (MAPE ~118M%).
  The quota_state heuristic works with any Kalman output — fall back to
  `hours_left` from the proxy's raw quota data when Kalman is unavailable.
- **Two-layer retry problem still applies.** The proxy retries 50× but
  Hermes agent has `api_max_retries`. Bumping to 15+ helped. With
  model downgrade, 429s should be rarer.
- **Model tier is a hint, not a hard constraint.** A `flash` task CAN run on
  glm-5.2 if quota is plentiful — no harm done. The tier prevents
  expensive models when budget is tight, not the reverse.
- **Keep the skill in sync** with `zai-proxy-management` skill — if the
  proxy's model list changes, update both.
