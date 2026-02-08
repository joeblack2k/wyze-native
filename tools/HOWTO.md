# Tools How-To (Scrape + Diff for PID/Switch/Sensor Support)

This folder contains optional utilities to help reverse-engineer which Wyze cloud keys and **property_list PIDs** correspond to camera settings. The goal is to safely add new Home Assistant entities (switches/sensors) **only when the camera actually supports them**.

These tools are for contributors. You do not need them for normal use of the integration.

## What These Tools Do

- <code>wyze_scrape.py</code>
  - Logs into Wyze cloud and dumps a **small** set of cloud API payloads used by the integration.
  - Can optionally fetch per-device <code>property_list</code> (PIDs) and/or a tiny sample of recent events.
- <code>wyze_diff_scrapes.py</code>
  - Compares a “before” vs “after” scrape and prints what changed.
  - This is the recommended method to map a Wyze app toggle to a PID/key.

## Security / Privacy

Scrapes can include:
- Device MACs
- Local IP addresses
- SSIDs
- Public IP addresses
- Signed thumbnail URLs (time-limited)

Do **not** publish your full scrape JSON publicly.

When filing issues, **redact** sensitive fields (see below).

## Prerequisites

You need Wyze developer credentials:
- Wyze account email + password
- Wyze “Key ID” + “API Key” (from Wyze developer portal)

Python:
- <code>python3</code>
- <code>aiohttp</code> (and optionally <code>certifi</code>)

Install dependencies:

<code>
python3 -m pip install --user aiohttp certifi
</code>

## Running a Basic Scrape

From the repo root:

<code>
WYZE_EMAIL="you@example.com" \\
WYZE_PASSWORD="your-password" \\
WYZE_KEY_ID="your-key-id" \\
WYZE_API_KEY="your-api-key" \\
  python3 tools/wyze_scrape.py --out wyze_scrape.json
</code>

Notes:
- This calls the same cloud endpoints as the integration uses (async, no Docker).
- It is rate-limit aware. If Wyze rate limits you, wait and retry later.

## Scraping PIDs (property_list)

To also call <code>/v2/device/get_device_Info</code> per camera and include <code>property_list</code>:

<code>
python3 tools/wyze_scrape.py --out before.json
</code>

This is the default behavior unless you pass <code>--no-device-info</code>.

If you have many cameras, reduce concurrency (less bursty, less likely to rate limit):

<code>
python3 tools/wyze_scrape.py --max-concurrent 1 --out before.json
</code>

## Mapping a Wyze App Toggle to a PID (Recommended Workflow)

The key rule: **change ONE setting only** between scrapes.

1) Create a baseline:

<code>
python3 tools/wyze_scrape.py --out before.json
</code>

2) In the Wyze app, change exactly one setting on exactly one camera.

Examples of good single changes:
- Status light on/off
- Show timestamp on/off
- Show Wyze logo on/off
- IR lights on/off
- Night vision mode
- Motion recording / sound recording toggles

3) Create the “after” snapshot:

<code>
python3 tools/wyze_scrape.py --out after.json
</code>

4) Diff them:

<code>
python3 tools/wyze_diff_scrapes.py before.json after.json --mac YOUR_CAMERA_MAC
</code>

This will print changes across:
- <code>raw.*</code> keys
- <code>device_params.*</code> keys
- <code>property_list</code> PID changes (<code>pid.P####</code>)

If you do not know the MAC, open the scrape JSON and look at <code>devices[].mac</code>.

## Optional: Event Sampling

To also include a very small event list sample per camera:

<code>
python3 tools/wyze_scrape.py --include-events --out wyze_scrape_events.json
</code>

This can help confirm whether the account/camera is returning cloud events at all (relevant for snapshots).

## What Information Helps Add New HA Entities

To add a new switch or sensor, we generally need:
- The PID/key name that changes when you toggle the setting
- The range of values (0/1, 1/2/3, strings, etc.)
- Confirmation that the PID is present on your camera model
- Device model + firmware version (behavior varies)

## How to File a Helpful Issue (Please Do This)

Open an issue at:
- https://github.com/joeblack2k/wyze-native/issues

Include:

1) Camera identification (redacted):
- Camera nickname
- Product model (example: <code>HL_PAN3</code>)
- Firmware version (example: <code>4.50.15.4800</code>)
- MAC (only last 4 chars) (example: <code>...8D4A</code>)

2) Which setting you changed in the Wyze app:
- “Show Timestamp: off -> on”

3) The diff output for that camera:
- Copy/paste the relevant section from:
  - <code>python3 tools/wyze_diff_scrapes.py before.json after.json --mac ...</code>
- The most important part is:
  - <code>== PROPERTY_LIST PID CHANGES ==</code>
  - plus any relevant <code>device_params</code> changes

4) Whether it worked in the app immediately and whether the change reverted.

### Redaction Guidelines

Before posting anything:
- Remove/replace:
  - full MACs (keep last 4 chars)
  - IP addresses
  - SSIDs
  - signed thumbnail URLs
  - access/refresh tokens (if any appear)

If you paste only the diff output (not the full JSON), that’s usually enough and safer.

## Rate Limiting Tips

Wyze rate limits are real. To reduce issues:
- Avoid repeated runs back-to-back
- Use <code>--max-concurrent 1</code> if you have many cameras
- Do not enable <code>--include-events</code> unless needed

