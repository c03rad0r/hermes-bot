---
name: kanban-quota-aware-dispatch
description: "Route kanban workers to cheaper GLM models during peak hours or tight quota, driven by Kalman filter predictions. 10/80/10 target distribution (heavy/mid/lower). Dynamic percentile thresholds auto-adjusted from usage history. Urgency-aware dispatch — urgent tasks always flow, low tasks queue for off-peak."
version: 3.0.0
author: Hermes Agent
tags: [kanban, dispatch, quota, kalman, model-tiering, urgency, peak-hours, dynamic-thresholds]
---

# Quota-Aware Kanban Dispatch — v3

## Target Distribution

From historical analysis of 1584 Kalman samples (friend/5-hour window):

| Tier | Model | Target | 90th %ile threshold | Best for |
|------|-------|--------|---------------------|----------|
| `heavy` | glm-5.2 | **10%** | `hours_left > 10.6h` | Architecture, debugging, complex reasoning |
| `mid` | glm-4.5 | **80%** | `0.1h < hours_left ≤ 10.6h` | Refactoring, coding, most tasks |
| `air` | glm-4.5-air | ~5% | `hours_left ≤ 0.1h` | Boilerplate |
| `flash` | glm-4.5-flash | ~5% | `hours_left ≤ 0.1h` | Formatting, simple edits |

**Peak hours (06:00-10:00 UTC):** cap at `air` regardless. glm-5.2 costs 3× during peak.

## Dynamic Thresholds

Thresholds auto-adjust every 1h via `threshold_tracker.py` (PID controller reading `model_decisions` from `zai_usage.db`):

- If heavy usage > 13% → raise `heavy_min_hours` 5%
- If heavy usage < 7% → lower `heavy_min_hours` 5%  
- Same logic for lower tiers
- Dead zone: ±3% to prevent oscillation
- Min: 4h, Max: 48h

## Urgency-Aware Dispatch

Each kanban task carries `urgency` alongside `model_tier`:

| Urgency | Behavior |
|---------|----------|
| `urgent` | Always dispatched regardless of quota/peak. Peak cap removed. Uses best available tier. |
| `normal` | Standard dispatch rules (default). Blocked in CRITICAL state. |
| `low` | Only dispatched in PLENTYFUL state (hours_left > 10h). Queued for off-peak otherwise. |

This means:
- **Urgent production issues** → always get glm-5.2 if needed
- **Routine development** → glm-4.5 most of the time (80% target)
- **Cleanup/refactor tickets** → queue for weekends or plentiful quota windows

## Implementation Files

### `~/.hermes/bot/model_tier_router.py`
CLI + proxy import. Two entry points:

**CLI (dispatcher calls this):**
```bash
# Standard dispatch (80% case — glm-4.5)
python3 model_tier_router.py --task-tier mid
# {"tier": "mid", "model": "glm-4.5", "reason": "quota=MODERATE_off_peak_urg=normal", ...}

# Urgent task during peak — overrides cap
python3 model_tier_router.py --task-tier heavy --urgency urgent
# {"tier": "heavy", "model": "glm-5.2", "reason": ..., "hours_left": 4.2}

# Low urgency in tight quota — deferred
python3 model_tier_router.py --task-tier mid --urgency low --quota-state MODERATE
# {"tier": null, "reason": "urgency=low blocked in MODERATE"}

# Show current thresholds
python3 model_tier_router.py --stats
```

**Proxy import (`compute_tier(chosen_key, tier_hint)`):**
Called by `zai_proxy.py` on every proxied request. Returns `model: None` when no downgrade needed.

### `~/.hermes/bot/zai_proxy.py`
X-Model-Tier header rewrite. When header present, rewrites body's `model` field.

### `~/.hermes/bot/quota_gate.py`
Preflight check before spawning workers. Returns exit code 0 (GO) or 1 (BLOCKED). Blocks only when both keys truly exhausted (used_pct >= 95%).

### `~/.hermes/bot/threshold_tracker.py`
PID controller. Reads `model_decisions` table, adjusts thresholds toward 10/80/10 target. Cronned every 1h.

## Kanban Task Metadata

```json
{
  "id": "t_abc123",
  "title": "Fix race condition in payment channel",
  "model_tier": "heavy",
  "urgency": "urgent",
  "status": "ready"
}
```

Recommended tiers by task type:
- `flash` + `normal`: typo fixes, CI tweaks, formatting
- `air` + `normal`: config changes, simple documentation
- `mid` + `normal`: refactoring, endpoints, tests (80% of tasks)
- `mid` + `low`: backlog cleanup, tech debt (queue for off-peak)
- `heavy` + `urgent`: production incidents, race conditions
- `heavy` + `normal`: architecture, protocol design

## Verification

```bash
# Show current thresholds + historical data
python3 ~/.hermes/bot/model_tier_router.py --stats

# Test dispatch gate
python3 ~/.hermes/bot/quota_gate.py -v

# Test tier selection
python3 ~/.hermes/bot/model_tier_router.py --task-tier mid --urgency urgent

# Check proxy rewrite
curl -s -X POST localhost:9099/v1/chat/completions \
  -H "X-Model-Tier: flash" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
```

## Related

- `tollgate-rs/docs/design/infra/quota-aware-dispatch.md` — full design doc
- `zai-proxy-management` skill — proxy architecture
- `kalman-convergence-check` skill — Kalman filter health
- `kanban-worker-management` skill — dispatch lifecycle
- `references/locked-vs-exhausted.md` — two-layer quota model
