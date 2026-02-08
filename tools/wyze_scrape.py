#!/usr/bin/env python3
"""Scrape Wyze cloud API payloads for reverse-engineering.

This helper logs into Wyze and dumps the raw payloads we can fetch via the
same cloud endpoints used by the Home Assistant `wyze_native` integration:

- /v2/home_page/get_object_list (device list + device_params)
- /v2/device/get_device_Info (extended info incl. property_list PIDs)

It does NOT use any P2P/TUTK features (so it cannot discover purely-local
settings like RTSP credentials, timestamp overlay toggles, etc).

Example:
  WYZE_EMAIL="you@example.com" \\
  WYZE_PASSWORD="..." \\
  WYZE_KEY_ID="..." \\
  WYZE_API_KEY="..." \\
    python3 tools/wyze_scrape.py --out wyze_scrape.json

On macOS, Python installs sometimes lack system CA certs; this script defaults
to using `certifi` if installed.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import sys
import time
from typing import Any

try:
    import aiohttp
except ModuleNotFoundError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]


DEFAULT_WYZE_API_PATH = (
    "custom_components/wyze_native/wyze_api.py"
)


def _load_wyze_api_module(path: Path):
    """Load wyze_api.py directly (without importing the HA integration package)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("wyze_native_wyze_api", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed creating module spec for {path}")

    module = importlib.util.module_from_spec(spec)
    # dataclasses (py3.13+) may look up the module in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ssl_context(*, insecure: bool) -> ssl.SSLContext | bool:
    if insecure:
        return False
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _sleep_until(ts: int | None, *, fallback_seconds: int = 60) -> None:
    if not ts:
        await asyncio.sleep(fallback_seconds)
        return
    delay = max(5, int(ts - time.time()) + 5)
    await asyncio.sleep(delay)


def _flatten_property_list(prop_list: Any) -> dict[str, Any]:
    """Convert Wyze property_list items into pid->value."""
    if not isinstance(prop_list, list):
        return {}
    out: dict[str, Any] = {}
    for item in prop_list:
        if not isinstance(item, dict):
            continue
        pid = item.get("pid")
        if not pid:
            continue
        # Wyze uses {"value": ...} in get_device_Info.
        out[str(pid)] = item.get("value", item.get("pvalue"))
    return out


def _json_default(o: Any):  # noqa: ANN001
    # Helpful for WyzeCredential dataclass and unknown objects.
    if hasattr(o, "__dict__"):
        return o.__dict__
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Wyze cloud API payloads.")
    parser.add_argument(
        "--wyze-api",
        default=DEFAULT_WYZE_API_PATH,
        help=f"Path to wyze_api.py (default: {DEFAULT_WYZE_API_PATH})",
    )
    parser.add_argument(
        "--ha-config-entries",
        default="",
        help="Optional path to Home Assistant .storage/core.config_entries to reuse an existing wyze_native config entry.",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Write JSON report to this path (default: wyze_scrape_<ts>.json)",
    )
    parser.add_argument(
        "--no-device-info",
        action="store_true",
        help="Skip per-camera get_device_Info calls (fewer API calls).",
    )
    parser.add_argument(
        "--include-events",
        action="store_true",
        help="Fetch a small recent event list for each camera (more API calls).",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=2,
        help="Max concurrent get_device_Info calls (default: 2).",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification (NOT recommended).",
    )
    args = parser.parse_args()

    if aiohttp is None:
        print(
            "Missing dependency: aiohttp\n"
            "Install with: python3 -m pip install aiohttp certifi\n"
            "Or run this script using the Home Assistant Python environment.",
            file=sys.stderr,
        )
        return 2

    email = os.environ.get("WYZE_EMAIL", "").strip()
    password = os.environ.get("WYZE_PASSWORD", "")
    key_id = os.environ.get("WYZE_KEY_ID", "").strip()
    api_key = os.environ.get("WYZE_API_KEY", "").strip()
    stored: dict[str, Any] = {}

    if not (email and password and key_id and api_key) and args.ha_config_entries:
        cfg_path = Path(args.ha_config_entries).expanduser().resolve()
        if not cfg_path.exists():
            print(f"HA core.config_entries not found: {cfg_path}", file=sys.stderr)
            return 2
        try:
            cfg = json.loads(cfg_path.read_text())
            entries = cfg.get("data", {}).get("entries", [])
            entry = next(
                (e for e in entries if isinstance(e, dict) and e.get("domain") == "wyze_native"),
                None,
            )
            stored = dict(entry.get("data") or {}) if isinstance(entry, dict) else {}
        except Exception as err:  # noqa: BLE001
            print(f"Failed reading HA config entries: {err}", file=sys.stderr)
            return 2

        email = (stored.get("email") or "").strip()
        password = stored.get("password") or ""
        key_id = (stored.get("key_id") or "").strip()
        api_key = (stored.get("api_key") or "").strip()

    if not (email and password and key_id and api_key):
        print(
            "Missing credentials. Either set env vars (WYZE_EMAIL, WYZE_PASSWORD, WYZE_KEY_ID, WYZE_API_KEY)\n"
            "or pass --ha-config-entries /path/to/.storage/core.config_entries",
            file=sys.stderr,
        )
        return 2

    wyze_api_path = Path(args.wyze_api).expanduser().resolve()
    if not wyze_api_path.exists():
        print(f"wyze_api.py not found: {wyze_api_path}", file=sys.stderr)
        return 2

    wyze_api = _load_wyze_api_module(wyze_api_path)

    ssl_ctx = _ssl_context(insecure=bool(args.insecure))
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    timeout = aiohttp.ClientTimeout(total=30)

    report: dict[str, Any] = {
        "generated_at": _utc_now_iso(),
        "wyze_api_path": str(wyze_api_path),
        "devices": [],
        "device_info_by_mac": {},
        "events_by_mac": {},
        "summary": {},
    }

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        client = wyze_api.WyzeApiClient(
            session,
            email=email,
            password=password,
            key_id=key_id,
            api_key=api_key,
            phone_id=stored.get("phone_id") if stored else None,
            access_token=stored.get("access_token") if stored else None,
            refresh_token=stored.get("refresh_token") if stored else None,
            user_id=stored.get("user_id") if stored else None,
        )

        # Only login if we don't already have an access token.
        if not getattr(client, "access_token", None):
            while True:
                try:
                    cred = await client.login()
                    report["credential"] = asdict(cred)
                    report["credential"].pop("password", None)
                    for k in ("access_token", "refresh_token"):
                        if report["credential"].get(k):
                            report["credential"][k] = "REDACTED"
                    break
                except getattr(wyze_api, "WyzeRateLimitError") as err:
                    print(f"Rate limited during login: {err}", file=sys.stderr)
                    await _sleep_until(getattr(err, "reset_by", None))

        # Pull devices.
        while True:
            try:
                devices = await client.get_devices()
                break
            except getattr(wyze_api, "WyzeRateLimitError") as err:
                print(f"Rate limited during get_devices: {err}", file=sys.stderr)
                await _sleep_until(getattr(err, "reset_by", None))

        # Normalize output (TypedDict -> plain dict).
        report["devices"] = [dict(d) for d in devices]

        if args.include_events:
            while True:
                try:
                    # Small, per-camera event sampling. This is primarily useful to
                    # confirm whether a camera has recent events/thumbnails at all.
                    for dev in report["devices"]:
                        mac = str(dev.get("mac") or "")
                        if not mac:
                            continue
                        try:
                            events = await client.get_event_list([mac], count=5, order_by=1, last_ts=0)
                        except getattr(wyze_api, "WyzeRateLimitError"):
                            raise
                        except Exception as err:  # noqa: BLE001
                            report["events_by_mac"][mac] = {"error": str(err)}
                            continue
                        report["events_by_mac"][mac] = events
                    break
                except getattr(wyze_api, "WyzeRateLimitError") as err:
                    print(f"Rate limited during get_event_list: {err}", file=sys.stderr)
                    await _sleep_until(getattr(err, "reset_by", None))

        if not args.no_device_info:
            sem = asyncio.Semaphore(max(1, int(args.max_concurrent)))

            async def fetch_info(dev: dict[str, Any]) -> None:
                mac = str(dev.get("mac") or "")
                model = str(dev.get("product_model") or "")
                if not mac or not model:
                    return

                async with sem:
                    while True:
                        try:
                            info = await client.get_device_info(mac, model)
                            report["device_info_by_mac"][mac] = info
                            return
                        except getattr(wyze_api, "WyzeRateLimitError") as err:
                            print(
                                f"Rate limited during get_device_info({mac}): {err}",
                                file=sys.stderr,
                            )
                            await _sleep_until(getattr(err, "reset_by", None))

            await asyncio.gather(*(fetch_info(d) for d in report["devices"]))

    # Build summary.
    raw_keys: set[str] = set()
    params_keys: set[str] = set()
    info_keys: set[str] = set()
    pids: set[str] = set()

    key_samples: dict[str, list[Any]] = defaultdict(list)

    for dev in report["devices"]:
        raw = dev.get("raw") or {}
        if isinstance(raw, dict):
            raw_keys.update(map(str, raw.keys()))
            for k, v in raw.items():
                if len(key_samples[f"raw.{k}"]) < 5:
                    key_samples[f"raw.{k}"].append(v)

        params = dev.get("device_params") or {}
        if isinstance(params, dict):
            params_keys.update(map(str, params.keys()))
            for k, v in params.items():
                if len(key_samples[f"device_params.{k}"]) < 5:
                    key_samples[f"device_params.{k}"].append(v)

        info = report["device_info_by_mac"].get(str(dev.get("mac") or ""), {})
        if isinstance(info, dict):
            info_keys.update(map(str, info.keys()))
            for k, v in info.items():
                if len(key_samples[f"device_info.{k}"]) < 5:
                    key_samples[f"device_info.{k}"].append(v)

            prop_list = info.get("property_list") or []
            flat = _flatten_property_list(prop_list)
            pids.update(map(str, flat.keys()))

    report["summary"] = {
        "raw_keys": sorted(raw_keys),
        "device_param_keys": sorted(params_keys),
        "device_info_keys": sorted(info_keys),
        "property_pids": sorted(pids),
        "key_samples": dict(key_samples),
    }

    out_path = Path(args.out) if args.out else Path(f"wyze_scrape_{int(time.time())}.json")
    out_path = out_path.expanduser().resolve()
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=_json_default))

    print(f"Wrote: {out_path}")
    print(f"Cameras: {len(report['devices'])}")
    print(f"device_params keys: {len(report['summary']['device_param_keys'])}")
    print(f"raw keys: {len(report['summary']['raw_keys'])}")
    print(f"property_pids: {len(report['summary']['property_pids'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
