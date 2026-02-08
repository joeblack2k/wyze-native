# Wyze Native (Home Assistant)

Native (cloud) Home Assistant integration for Wyze cameras and related devices.

This project ports the relevant cloud API logic from `docker-wyze-bridge` into a Home Assistant custom component. It focuses on safe, async, pure-Python operation inside Home Assistant.

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=joeblack2k&repository=wyze-native&category=integration)

## What You Get

- `switch`:
  - Camera power (privacy mode behavior depends on model; implemented as cloud power switch)
  - A few PID-backed recording toggles (only exposed when the camera reports the PID)
- `sensor`:
  - Connection, WiFi details, firmware version, and other diagnostics
- `binary_sensor`:
  - Best-effort motion (based on thumbnail timestamps)
- `camera`:
  - Snapshot/thumbnail (no native P2P streaming)
  - Optional stream source via a user-provided RTSP/Go2RTC URL template

## Important Limitations

- This integration does not implement Wyze native streaming (P2P/TUTK/Wyze WebRTC signaling).
- Snapshot images are only available when Wyze provides a thumbnail URL (typically event-based). If a camera has no events/thumbnails, Home Assistant will show a placeholder image.
- Firmware update checks and remote firmware upgrades are not supported via the public cloud endpoints used here.

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant.
2. Add this repository as a custom repository:
   - Repository: `joeblack2k/wyze-native`
   - Category: `Integration`
3. Install **Wyze Native**.
4. Restart Home Assistant.

### Manual

1. Copy `custom_components/wyze_native` into your Home Assistant `config/custom_components/`.
2. Restart Home Assistant.

## Configuration

1. Home Assistant → Settings → Devices & services → Add integration → **Wyze Native**
2. Enter:
   - Email
   - Password
   - Key ID
   - API Key

### Optional Streaming (RTSP -> WebRTC)

If your camera firmware supports RTSP and you enabled RTSP in the Wyze app, you can provide a stream URL template:

Home Assistant → Wyze Native → Options → `Stream URL template`

Supported placeholders:
- `{mac}`: device MAC
- `{nickname}`: Wyze nickname
- `{name}`: slugified nickname (URL-safe)
- `{model}`: product model
- `{ip}`: local IP as reported by Wyze cloud

Examples:

- Direct RTSP:
  - `rtsp://USER:PASS@{ip}/live`
- Via go2rtc (recommended for WebRTC dashboards):
  - `rtsp://127.0.0.1:8554/{name}`

For WebRTC dashboards, use go2rtc plus a WebRTC dashboard card.

## Debug / Reverse Engineering Tools

The `tools/` folder includes scripts to scrape and diff Wyze cloud payloads for research:

- `tools/wyze_scrape.py`
- `tools/wyze_diff_scrapes.py`
- `tools/HOWTO.md` (recommended workflow + how to file issues with PID diffs)

These tools require your Wyze developer keys and will query Wyze cloud endpoints. Do not publish scrape output publicly.

## Credits

- Wyze cloud API behavior referenced from [docker-wyze-bridge](https://github.com/akeslo/docker-wyze-bridge)
