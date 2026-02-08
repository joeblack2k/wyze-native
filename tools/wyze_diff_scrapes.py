#!/usr/bin/env python3
"""Diff two wyze_scrape.json files produced by tools/wyze_scrape.py.

This is useful to map Wyze app toggles to cloud properties:
1. Run wyze_scrape.py -> before.json
2. Change ONE setting in the Wyze app (status light, timestamp, etc)
3. Run wyze_scrape.py -> after.json
4. Run this script to see which keys/PIDs changed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _index_devices(scrape: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for d in scrape.get("devices") or []:
        if not isinstance(d, dict):
            continue
        mac = d.get("mac")
        if isinstance(mac, str) and mac:
            out[mac] = d
    return out


def _props_by_pid(info: dict[str, Any]) -> dict[str, Any]:
    pl = info.get("property_list") or []
    if not isinstance(pl, list):
        return {}
    out: dict[str, Any] = {}
    for item in pl:
        if not isinstance(item, dict):
            continue
        pid = item.get("pid")
        if not pid:
            continue
        out[str(pid)] = item.get("value", item.get("pvalue"))
    return out


def _print_changes(label: str, changes: list[str]) -> None:
    if not changes:
        return
    print(f"\n== {label} ==")
    for line in changes:
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="Diff two Wyze scrape JSON files.")
    parser.add_argument("before", help="Path to BEFORE scrape JSON")
    parser.add_argument("after", help="Path to AFTER scrape JSON")
    parser.add_argument("--mac", default="", help="Limit output to a single device MAC")
    args = parser.parse_args()

    before = _load(Path(args.before).expanduser().resolve())
    after = _load(Path(args.after).expanduser().resolve())

    b_devs = _index_devices(before)
    a_devs = _index_devices(after)

    macs = sorted(set(b_devs) | set(a_devs))
    if args.mac:
        macs = [m for m in macs if m.lower() == args.mac.strip().lower()]
        if not macs:
            print(f"No matching MAC found in either file: {args.mac}")
            return 2

    for mac in macs:
        b = b_devs.get(mac) or {}
        a = a_devs.get(mac) or {}

        name = a.get("nickname") or b.get("nickname") or mac
        model = a.get("product_model") or b.get("product_model") or ""
        print(f"\n#############################\n{mac}  {name}  {model}\n#############################")

        # Diff raw keys (top-level, excluding device_params).
        raw_b = (b.get("raw") or {}) if isinstance(b.get("raw"), dict) else {}
        raw_a = (a.get("raw") or {}) if isinstance(a.get("raw"), dict) else {}
        keys = sorted(set(raw_b) | set(raw_a))
        raw_changes: list[str] = []
        for k in keys:
            if k == "device_params":
                continue
            if raw_b.get(k) != raw_a.get(k):
                raw_changes.append(f"- raw.{k}: {raw_b.get(k)!r} -> {raw_a.get(k)!r}")
        _print_changes("RAW CHANGES", raw_changes)

        # Diff device_params.
        dp_b = (b.get("device_params") or {}) if isinstance(b.get("device_params"), dict) else {}
        dp_a = (a.get("device_params") or {}) if isinstance(a.get("device_params"), dict) else {}
        keys = sorted(set(dp_b) | set(dp_a))
        dp_changes: list[str] = []
        for k in keys:
            if dp_b.get(k) != dp_a.get(k):
                dp_changes.append(f"- device_params.{k}: {dp_b.get(k)!r} -> {dp_a.get(k)!r}")
        _print_changes("DEVICE_PARAMS CHANGES", dp_changes)

        # Diff device_info + property_list PIDs if present.
        bi = (before.get("device_info_by_mac") or {}).get(mac) or {}
        ai = (after.get("device_info_by_mac") or {}).get(mac) or {}
        if isinstance(bi, dict) and isinstance(ai, dict):
            bp = _props_by_pid(bi)
            ap = _props_by_pid(ai)
            pids = sorted(set(bp) | set(ap))
            pid_changes: list[str] = []
            for pid in pids:
                if bp.get(pid) != ap.get(pid):
                    pid_changes.append(f"- pid.{pid}: {bp.get(pid)!r} -> {ap.get(pid)!r}")
            _print_changes("PROPERTY_LIST PID CHANGES", pid_changes)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
