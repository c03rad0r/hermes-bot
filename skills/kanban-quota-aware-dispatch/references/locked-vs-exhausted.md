# Locked vs Exhausted: Two-Layer Quota Model

The proxy's `is_key_locked()` returns `locked=True` when ANY window hits its
per-window threshold (e.g. friend/5-hour at 80%). This is conservative — the
key still has headroom but the proxy prefers the other one.

The `model_tier_router.py` ignores the proxy's `locked` flag and uses actual
`hours_left` data instead. This means:

- Friend key at 80% on 5-hour with 1h remaining → proxy says locked, but
  tier router still considers it available. Normal dispatch proceeds.
- Both keys at 95%+ with <0.1h → truly exhausted. Dispatch blocks.

This prevents the common scenario where the proxy marks both keys "locked"
(at 80% friend/5-hour + 100% ours/weekly) but the friend key still has
usable headroom.
