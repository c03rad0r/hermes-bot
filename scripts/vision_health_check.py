#!/usr/bin/env python3
"""Vision health watchdog — prevents silent vision failures.

Checks three layers:
1. CONFIG INTEGRITY: vision provider/model/base_url in config.yaml haven't drifted
2. PROXY HEALTH: z.ai proxy at :9099 is responding
3. END-TO-END SMOKE TEST: actual vision API call with a tiny test image

Exit codes:
  0 = healthy (or legitimate skip — no vision configured)
  1 = BROKEN — prints diagnostic to stdout

Watchdog pattern: --no-agent cron, deliver=local. Wraps with LLM cron for
investigation + escalation. SILENT when healthy.

Incident this prevents (2025-07-05):
  - Config had provider=ppq, model=gemini-3-flash-preview → PPQ key was revoked
  - Fallback to zai used hardcoded model glm-5v-turbo → NOT in subscription plan
  - Result: vision_analyze returned "Connection error" for HOURS, silently
"""
from __future__ import annotations

import base64
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

# ── Expected values (the known-good config after the fix) ──────────────────
EXPECTED_PROVIDER = "zai"
EXPECTED_MODEL = "glm-4.6v"           # MUST NOT be glm-5v-turbo (not in plan)
BAD_MODELS = {"glm-5v-turbo"}         # models known to fail on z.ai plan
EXPECTED_BASE_URL = "http://127.0.0.1:9099"
PROXY_HEALTH_URL = "http://127.0.0.1:9099/health"  # quick proxy liveness check

# Config file locations
CONFIG_PATHS = [
    Path.home() / ".hermes" / "profiles" / "manager" / "config.yaml",
    Path.home() / ".hermes" / "config.yaml",
]

# State file for other crons to read
STATE_PATH = Path.home() / ".hermes" / "bot" / "vision_health.json"


def _make_tiny_png() -> str:
    """Create a 1×1 red pixel PNG and return as base64 data URL."""
    width, height = 1, 1
    sig = b"\x89PNG\r\n\x1a\n"

    # IHDR chunk
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", ihdr_crc)

    # IDAT chunk (1 red pixel: filter byte 0 + RGB)
    raw = b"\x00\xff\x00\x00"
    compressed = zlib.compress(raw)
    idat_crc = zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF
    idat = struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", idat_crc)

    # IEND chunk
    iend_crc = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)

    png_bytes = sig + ihdr + idat + iend
    b64 = base64.b64encode(png_bytes).decode()
    return f"data:image/png;base64,{b64}"


def load_vision_config() -> dict | None:
    """Read auxiliary.vision from config.yaml."""
    try:
        import yaml
    except ImportError:
        # Fallback: grep the config manually
        for cfg_path in CONFIG_PATHS:
            if not cfg_path.is_file():
                continue
            text = cfg_path.read_text(errors="ignore")
            # Very simple extraction
            result = {}
            in_vision = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("vision:"):
                    in_vision = True
                    continue
                if in_vision and stripped and not stripped.startswith("-"):
                    if not line.startswith(" ") or (len(line) - len(line.lstrip()) <= 4 and ":" in stripped):
                        break  # dedented to a new top-level key
                    if ":" in stripped:
                        key, _, val = stripped.partition(":")
                        result[key.strip()] = val.strip().strip("\"'")
            if result:
                return result
        return None

    for cfg_path in CONFIG_PATHS:
        if not cfg_path.is_file():
            continue
        with open(cfg_path) as f:
            config = yaml.safe_load(f)
        if config and "auxiliary" in config and "vision" in config["auxiliary"]:
            return config["auxiliary"]["vision"]
    return None


def check_config(vision_cfg: dict) -> list[str]:
    """Layer 1: Verify config values. Returns list of issues (empty = OK)."""
    issues = []

    provider = vision_cfg.get("provider", "")
    model = vision_cfg.get("model", "")
    base_url = vision_cfg.get("base_url", "")

    if provider != EXPECTED_PROVIDER:
        issues.append(
            f"CONFIG DRIFT: vision.provider='{provider}' "
            f"(expected '{EXPECTED_PROVIDER}')"
        )

    if model in BAD_MODELS:
        issues.append(
            f"BANNED MODEL: vision.model='{model}' is known to fail "
            f"(not in z.ai subscription plan, error 1311)"
        )
    elif model != EXPECTED_MODEL and provider == EXPECTED_PROVIDER:
        issues.append(
            f"CONFIG DRIFT: vision.model='{model}' "
            f"(expected '{EXPECTED_MODEL}')"
        )

    if EXPECTED_BASE_URL not in base_url and provider == EXPECTED_PROVIDER:
        issues.append(
            f"CONFIG DRIFT: vision.base_url='{base_url}' "
            f"(expected to contain '{EXPECTED_BASE_URL}')"
        )

    return issues


def check_proxy() -> list[str]:
    """Layer 2: Check z.ai proxy at :9099 is alive."""
    issues = []
    try:
        req = urllib.request.Request(PROXY_HEALTH_URL, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                issues.append(f"PROXY UNHEALTHY: :9099 returned HTTP {resp.status}")
    except urllib.error.URLError as e:
        issues.append(f"PROXY DOWN: cannot reach :9099 — {e.reason}")
    except Exception as e:
        issues.append(f"PROXY ERROR: {type(e).__name__}: {e}")
    return issues


def check_smoke_test(vision_cfg: dict) -> list[str]:
    """Layer 3: End-to-end vision API call with a tiny test image."""
    issues = []

    model = vision_cfg.get("model", EXPECTED_MODEL)
    base_url = vision_cfg.get("base_url", EXPECTED_BASE_URL)
    # Normalize endpoint (proxy uses /chat/completions, NOT /v1/chat/completions)
    endpoint = base_url.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3]
    endpoint = endpoint + "/chat/completions"

    tiny_png = _make_tiny_png()

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What color is this image? One word."},
                    {"type": "image_url", "image_url": {"url": tiny_png}},
                ],
            }
        ],
        "max_tokens": 5,
    }

    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            # Check for API-level errors
            if "error" in body:
                err = body["error"]
                code = err.get("code", "unknown")
                msg = err.get("message", str(err))
                issues.append(
                    f"VISION API ERROR (code={code}): {msg}. "
                    f"Model={model}, endpoint={endpoint}"
                )
            elif "choices" not in body:
                issues.append(
                    f"VISION UNEXPECTED RESPONSE: no 'choices' in response. "
                    f"Body: {json.dumps(body)[:300]}"
                )
            # else: success — vision is working
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:300]
        except Exception:
            pass
        issues.append(
            f"VISION HTTP {e.code}: {e.reason}. "
            f"Model={model}, endpoint={endpoint}. "
            f"Body: {err_body}"
        )
    except Exception as e:
        issues.append(
            f"VISION SMOKE TEST FAILED: {type(e).__name__}: {e}. "
            f"Model={model}, endpoint={endpoint}"
        )

    return issues


def write_state(ok: bool, issues: list[str]):
    """Write state file for other crons to consult."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "ts": int(time.time()),
        "ok": ok,
        "issues": issues,
    }
    STATE_PATH.write_text(json.dumps(state, indent=2))


def main() -> int:
    # Load vision config
    vision_cfg = load_vision_config()

    if not vision_cfg:
        # Vision not configured at all — this is a legitimate skip
        write_state(True, ["vision not configured (skip)"])
        return 0

    all_issues: list[str] = []

    # Layer 1: Config integrity
    all_issues.extend(check_config(vision_cfg))

    # Layer 2: Proxy health
    all_issues.extend(check_proxy())

    # Layer 3: End-to-end smoke test (only if config+proxy look OK)
    if not all_issues:
        all_issues.extend(check_smoke_test(vision_cfg))

    ok = len(all_issues) == 0
    write_state(ok, all_issues)

    if ok:
        # Silent — healthy
        return 0
    else:
        # Print diagnostics for the LLM cron to read
        print("VISION HEALTH CHECK FAILED")
        print(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
        print(f"Issues found: {len(all_issues)}")
        for i, issue in enumerate(all_issues, 1):
            print(f"  {i}. {issue}")
        print()
        print("Config values:")
        for k, v in sorted(vision_cfg.items()):
            print(f"  vision.{k} = {v}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
