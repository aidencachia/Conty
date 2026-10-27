# Milestone 2 — minimum usable dashboard

## Status and startup

The YAML pipeline, same-origin server, pairing, supervised FIFO executor, polling API and local browser UI are implemented. Milestone 1 hardware proof was reported complete by the owner during planning on 19 October 2026. A real MacBook/iPhone check of the new server and native-helper launch context remains required; automated tests use fake native adapters and harmless subprocesses.

```sh
.venv/bin/python -m pip install -e '.[macos,test,dev]'
.venv/bin/conty validate examples/conty.yaml
.venv/bin/conty serve examples/conty.yaml
```

Run as the logged-in user in the same terminal/desktop context used to grant macOS permissions. The server and validation never request native permission prompts. Use the existing local `doctor` setup flow deliberately when needed; `doctor --no-prompt` is passive.

`validate` exits 0 for valid configuration, 1 for a configuration error and 2 for CLI syntax errors. It checks schema and OS support without executing actions or probing live applications/devices. `serve` exits on invalid startup configuration; edits do not alter a running snapshot. Only one service is allowed per user, using a lock in an owner-only directory under the system temporary directory. A port conflict also prevents startup.

The service prints connection URLs, a QR code containing only the first URL, and a six-digit pairing code. Codes expire after two minutes and are consumed on successful pairing. A fresh code is displayed after expiry or use. Multiple browsers can pair; each gets a separate eight-hour session. Ctrl-C stops admission, cancels queued work, permits up to five seconds for active execution and then attempts bounded cleanup. Restart revokes all sessions and forgets invocation history.

## Configuration version 1

See [the reference configuration](../examples/conty.yaml) for Presentation and Media pages and [the script example](../examples/script.yaml) for a process-only dashboard.

- Require integer `version: 1`; reject unknown fields, coercion from strings/booleans, duplicate keys and unsupported action types.
- Default server: auto-detected IPv4 `bind`, `port: 8765`, `auth: {mode: pairing_code}`. If `bind` is omitted (including when the entire server section is omitted), the host routing table selects the address without sending packets or requiring internet access. No usable IPv4 route falls back to `127.0.0.1`. Explicit `bind` must be a numeric IP and always bypasses discovery. Wildcard binding requires `allowed_hosts`, a list of usable IPs or plain DNS names without schemes, ports or wildcards. Loopback binding also accepts `localhost`, `127.0.0.1` and `::1` at the configured port.
- To use a phone, omit `bind` and open the printed URL on the same trusted network. The detected address alone is automatically accepted by Host validation; discovery does not allow arbitrary hosts. Use an explicit address for multi-interface/VPN routing, or `127.0.0.1` for local-only access. Restart after network/DHCP changes; detection is once per launch. Existing explicit IPs remain unchanged. Approving a hostname does not configure DNS or mDNS.
- Default `defaults.action_timeout_seconds: 5`; any action can override it with `timeout_seconds`. Values must be finite, positive and at most 300 seconds. A timeout begins when execution starts.
- IDs use letters, digits, underscores and hyphens, start with a letter or underscore, and have at most 64 characters. Action/dashboard IDs are global, section IDs are scoped to a dashboard, and widget IDs to a section. Titles/labels contain 1–256 characters.
- A dashboard may declare `layout` and `widgets` directly for a single grid, or use the existing `sections` form for grouped grids. Mixing the forms is rejected. `layout.rows` and `layout.columns` are integers from 1–1000. Each tile has a one-based `position.row` and `position.column`. Optional `size: {width: 1, height: 2}` expresses size in columns and rows; the existing `position.row_span`/`column_span` remain supported. Conflicting size/span values are rejected. Rectangles must fit and cannot overlap; empty cells stay empty.
- Buttons show a `label` and optional `symbol` (1–16 characters, default `•`). Both are plain text. Button `appearance` is `default`, `primary` or `danger`. Slider tiles support system volume only. Tiles preserve placement and spans on narrow screens, using horizontal scrolling if necessary. The UI is a compact page selector and grid; activity is collapsed and failures appear in a short notice.
- Read at most 1 MiB of UTF-8 YAML; allow at most 32 collection levels and 10,000 scalar/collection nodes. Reject aliases, merge keys, explicit tags, non-string mapping keys and multiple YAML documents. Errors identify field paths and source locations where available.

### Actions and paths

Keyboard actions use `key`, optional unique `modifiers` and optional `target: {app_id, focus}`. Keys share the existing physical `SUPPORTED_KEYS` vocabulary. Omitting the app ID snapshots the frontmost application when execution starts, with no activation or retargeting. Explicit focus requires an app ID. An explicit target defaults to activation and must already be running.

Media actions require `target: {app_id: com.apple.Music}` and `command: previous_track`, `play_pause` or `next_track`. Volume actions require `target: system`, with `operation: adjust` plus an integer `delta` from −100 to 100, `operation: toggle_mute` with no delta, or `operation: set` with a bounded runtime value supplied by a slider. Readiness failures do not invalidate an otherwise supported configuration.

### Minimal grid and volume slider

```yaml
actions:
  volume_level:
    type: volume
    target: system
    operation: set
dashboards:
  - id: media
    title: Media
    layout: {rows: 2, columns: 1}
    widgets:
      - id: volume
        type: slider
        style: ios # Or standard for the original track and thumb (the default).
        label: Volume
        symbol: "♫"
        action: volume_level
        position: {row: 1, column: 1}
        size: {width: 1, height: 2}
```

Include `version: 1` at the root as usual. A slider must reference a `volume`/`set` action; a button cannot invoke that dynamic action. A tile taller than it is wide uses a vertical slider; a wider tile uses a horizontal slider. The example uses one column and two rows. The thumb and percentage preview update immediately, and integer values 0–100 are applied live while dragging. The first changed value is sent immediately; subsequent updates are spaced by at least 50 ms (up to 20/second), with at most one accepted/in-flight volume update and one latest unsent value per browser. Intermediate unsent values are replaced, including across different slider styles, so a slow host cannot accumulate a stale slider backlog. Release preserves the latest value; cancelling or hiding the tab drops unsent values without undoing already accepted changes. Keyboard changes are supported. Volume writes preserve actual mute state.

Slider `style` accepts `standard` (the existing track and thumb, default when omitted) or `ios` (a rounded full-tile control with a light fill, inspired by iOS Control Centre). The iOS fill grows upward for vertical tiles and rightward for horizontal tiles. The whole surface accepts touch/mouse dragging and tapping, including exact 0/100 endpoints. Both styles retain a labelled native range for keyboard and assistive access, show the same observed host volume, and support the same cell positions/sizes. Different styles can appear on the same dashboard.

The slider reads actual host volume before enabling, refreshes every two seconds while visible, and reads back after completion. Unknown/unsupported state disables only the slider and shows “Unavailable.” Reads use bounded read-only probes, share concurrent requests and cache observations/errors for 250 ms. Writes invalidate that cache. An edited preview is not overwritten by a background read. Missing permissions, output changes and unsupported devices fail honestly; no guessed starting percentage or live toggle state is implied.

Processes use `argv`, optional `cwd`, `env_from` and `timeout_seconds`. The executable at `argv[0]` is an absolute path. Ordinary string arguments remain literal; mark paths explicitly:

```yaml
type: process
argv: [/bin/sh, {path: scripts/hello.sh}, "a literal argument"]
cwd: work
env_from: {CHILD_NAME: HOST_NAME}
timeout_seconds: 10
```

Resolve `{path: ...}` and relative `cwd` against the configuration directory. If `cwd` is omitted, use that directory. Script paths stay relative to the configuration even when `cwd` differs. There is no implicit shell, interpolation, PATH executable lookup or interpretation of ordinary arguments as filenames. An explicitly configured interpreter or shell is trusted local input. Environment mappings read host variables when execution starts. Missing variables fail execution without disclosing values. Process output is retained at most 16 KiB per stream and never returned to browsers.

## Execution and request semantics

The coordinator owns one immutable startup revision and executes one invocation at a time across all browsers. A long process delays slide navigation. Each accepted invocation captures its configured action. Session expiry cancels that session's work that has not started; an already running action finishes normally.

Native keyboard, Music and audio work runs one action at a time in a persistent supervised Python worker, with Music's `osascript` in the same process group. The service preloads configured keyboard/audio bridges once; each invocation still resolves current targets/devices and checks live permissions. Cocoa application-state reads pump the main run loop without waiting so cached frontmost/termination information stays fresh, as required by [Apple's NSRunningApplication contract](https://developer.apple.com/documentation/appkit/nsrunningapplication). Each request gets its own [PyObjC autorelease pool](https://pyobjc.readthedocs.io/en/latest/api/module-objc.html) when Cocoa is loaded.

Blocking native work stays off the HTTP loop. The worker receives the remaining action budget and checks deadlines before dispatch; audio also checks before writing and after readback. The parent enforces a hard outer deadline of the configured timeout plus 1.5 seconds for cleanup. A hard timeout, unexpected worker exit or malformed protocol frame terminates the group, attempts to reap the worker, disables further execution and cancels queued work. Restart locally after checking the host. A hard timeout cannot establish that a key was released or undo an external effect. Read-only readiness checks run in separate bounded probe processes every 30 seconds and cannot occupy the action worker or hold up its queue.

Processes use the existing asynchronous adapter. A normal process timeout attempts TERM, waits 200 ms, sends KILL and allows up to one second to drain/reap; confirmed cleanup permits the next invocation. Unconfirmed cleanup disables execution. Detached descendants and external effects can remain.

States are `queued`, `running`, `succeeded`, `failed`, `timed_out` and `cancelled`. Keyboard/media success confirms dispatch, not slide/playback state. Volume writes are checked by their adapter; the slider additionally reads observed host volume after editing ends and pending writes finish. Failure results include `effects_possible` when changes may already have occurred.

| Resource | Policy |
| --- | --- |
| Execution | One active plus 64 queued invocations |
| New submissions | Per session: buttons burst 20/refill 10 per second; volume-set sliders have an independent burst 30/refill 20 per second; shared host burst 100/refill 50 per second |
| Request records | At most 10,000; keep nonterminal records and terminal records for 15 minutes after completion |
| Local history | Most recent 1,000 records, including bounded adapter diagnostics; no HTTP history endpoint |
| Sessions | At most 32, eight-hour fixed lifetime |
| Pairing attempts | Per source: burst 5, refill 5/minute; host: burst 20, refill 20/minute |
| HTTP requests | Per source: burst 80, refill 40/second; host: burst 400, refill 200/second |
| HTTP bodies | 4 KiB; five-second read deadline |
| Browser submission queue | At most 32 unsent items; one POST awaiting acknowledgement. Slider updates additionally permit only one accepted/in-flight update and one latest unsent value. |

The same session/request ID and action/revision return the existing invocation; conflicting reuse returns 409. Duplicate checks precede stale-revision and capacity checks. Unexpired records are never evicted to make room. Capacity/rate rejections accept no work. This is a bounded deduplication guarantee, not exactly-once delivery across restart or expiry.

The browser starts one batched status read immediately after submission acknowledgement. Each read watches up to 64 known invocation IDs, waits at most two seconds, and wakes as soon as any watched action changes. There is no fixed 500 ms feedback delay and only one connection is occupied by action-status reads. Overall status is checked every two seconds; background tabs reduce activity and failures trigger backoff. The browser never retries an action POST. On connection loss, it discards unsent taps and checks known invocation IDs after reconnecting. A lost POST response without an invocation ID shows **outcome unknown**; request-ID recovery is Milestone 3. Reloading the browser loses this page's invocation tracking and never replays actions.

For live sliders the browser uses `POST /api/invocations?wait=0.25`: the server may return completion in the same response, avoiding extra polling round trips for quick native writes. Waiting is bounded, rechecks authentication and never cancels admitted work on timeout/disconnect. Slower actions return a nonterminal record and use the existing batched observer. All accepted writes retain FIFO, request-ID deduplication, rate/capacity limits and native deadlines; they never bypass the execution worker. Actual Wi-Fi, output-device and host load still determine physical latency.

## HTTP and security

All assets are local. Unpaired clients see only the static pairing page. Pairing authorizes every action in the configuration; there are no per-action roles.

| Route | Behavior |
| --- | --- |
| `POST /api/pair` | `{code}` establishes a session cookie and returns `csrf_token` |
| `GET /api/dashboard` | Presentation-only schema and active revision |
| `GET /api/status` | Revision, executor availability, cached coarse readiness and this session's CSRF token |
| `POST /api/invocations` | `{request_id, action_id, revision}`; configured volume-set actions additionally require integer `value: 0..100`. Optional query `wait` is 0–0.25 seconds and waits for completion or the deadline. 202 for new admission, 200 for a duplicate, whether queued or already terminal. |
| `GET /api/invocations/{id}` | Sanitized record belonging to the current session; otherwise 404 |
| `GET /api/invocations?ids=ID&ids=ID&wait=2` | Up to 64 owned records; returns on the next state change or within two seconds. Response: `{invocations: [...], missing_ids: [...]}`; foreign IDs appear missing. |
| `GET /api/volume` | Authenticated observation `{value: 0..100, muted: boolean}` when a volume-set action is configured; no device IDs or raw diagnostics. |

Request IDs are 1–128 URL-safe letters/digits/underscores/hyphens. Unknown request fields are rejected. Errors use `{error: {code, message, correlation_id}}`; 401 requests pairing, 403 rejects origin/CSRF/code, 409 identifies conflicts, 422 invalid fields, 429 throttling, and 503 unavailable execution/capacity.

Validate Host against approved authorities, require exact same-origin mutation requests, and require a session-bound `X-CSRF-Token` for authenticated mutations. Pairing is JSON-only and throttled. Cookies are opaque, hashed in memory, `HttpOnly`, `SameSite=Strict`, path `/`, and have no Domain attribute; HTTPS contexts also set `Secure`. CORS and public interactive API documentation are disabled. Forwarded proxy headers are ignored by the supported launcher. Responses disable caching and supply a restrictive content security policy.

This launcher supports trusted-LAN HTTP, which does not encrypt codes, session credentials or commands. Untrusted networks, reverse-proxy deployment and TLS setup remain unsupported in this milestone. Native permission prompts remain local. Raw configuration, executable paths, arguments, environment mappings, native diagnostics and stdout/stderr never appear in API results. Local admission/completion logs contain IDs, result categories and timing only; no persistent log files are created. Pairing codes are deliberately printed only in the owner's terminal.

## Verification

```sh
.venv/bin/python -m playwright install chromium
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m pytest -q
.venv/bin/python -m pip wheel --no-deps --wheel-dir dist .
```

Configuration, auth, FIFO/deduplication/capacity, sanitized API results, process/native-helper cleanup, lock ownership and browser behavior have automated coverage. Browser tests run a real loopback server with fake adapters and include lost-response behavior, explicit grids, empty trailing columns, spans, focus order and mobile/desktop rendering. They skip with an explicit installation message if Chromium is unavailable. Python 3.11 is the exercised runtime; other supported Python versions are not independently validated here.

Validation on 19 October 2026: **246 tests and 176 subtests passed**, including all four Chromium browser tests. Ruff lint/format checks, JavaScript syntax and `git diff --check` passed. A wheel was built, installed into a separate temporary directory and imported outside the source tree; packaged HTML/CSS/JS/SVG, pairing, invocation polling, a real harmless script and output redaction passed the installed-wheel smoke test. Mobile and desktop screenshots were inspected. No key, playback or audio-changing native actions were executed.

Exercised versions: Python 3.11.1, FastAPI 0.146.0, Starlette 1.9.0, Pydantic 2.14.1, ruamel.yaml 0.18.17, Uvicorn 0.54.0, qrcode 8.2, httpx 0.28.1, Playwright 1.64.0 with Chromium 156, pytest 9.1.1 and Ruff 0.14.14. The suite emits one upstream Starlette warning about future TestClient/httpx compatibility; it does not affect the current checks.

### Hardware acceptance

Use harmless content and the same terminal/permission identity intended for daily use. Record macOS, PowerPoint, Music, Safari, Python, display and output-device versions alongside results.

| Check | Expected result | Evidence |
| --- | --- | --- |
| iPhone and Mac on private Wi-Fi with internet disconnected | QR URL loads, pairing succeeds, both pages work without external assets | Pending server-path run |
| Actual PowerPoint slideshow/display | Previous, next, blackout/restore work through the native helper | Pending server-path run |
| Rapid intentional slide taps, including a second paired browser | Effects follow host admission order | Automated with fakes; hardware pending |
| Apple Music playing in the background | Previous/play-pause/next affect Music without focus change | Pending server-path run |
| Selected output at moderate volume | ±5 percentage points preserve mute; mute preserves stored volume | Pending server-path run |
| Harmless process/script | Runs from the configured directory; browser receives no output | Automated; actual launch-context check pending |
| Missing permissions, absent app or unsupported output | Clear failure and no unrelated target receives input | Automated with fakes; hardware pending |
| Disconnect/reconnect, tab backgrounding and service restart | No automatic POST retries; unknown outcomes remain honest; restart requires pairing | Chromium automated; iPhone pending |

Atomic reload, per-session owner management, request-ID recovery, persistent state, additional state bindings/toggles and distributable macOS packaging remain Milestone 3 or later. The bounded system-volume slider is implemented.

## Latency regression checks

Run `.venv/bin/python tools/benchmark_latency.py` to compare interpreter-per-request overhead with the warmed persistent protocol. It sends expired requests without a key, so it cannot dispatch native input. Run `.venv/bin/python -m pytest -q -s tests/test_browser.py::test_tap_feedback_latency` for a real-browser tap-to-feedback measurement with a fixed 60 ms fake action.

On this Mac, 50 protocol samples measured a median of **39.689 ms** for one-shot helpers versus **0.108 ms** for the persistent worker (p95 **47.006 ms** versus **0.148 ms**). The real-browser benchmark dropped from a median of **503.5 ms** to **65.0 ms**, including the same 60 ms simulated action. These are local protocol/browser measurements; Wi-Fi delay, actual native dispatch, PowerPoint response and the per-command Music `osascript` cost are additional. The warmed worker removes Python/framework startup from repeated actions. Terminal-record expiry uses a heap, so admission/status reads no longer scan every retained record.

Latency follow-up validation: **262 tests and 176 subtests passed**, including six real-browser tests. Coverage includes worker reuse, ordinary errors, idle exit, corrupt/oversized frames, hard timeout/reaping, independent probe timeouts, live permission checks, non-waiting Cocoa run-loop refresh, immediate batch wakeups, ownership/redaction, cancellation and network recovery without action replay. Ruff lint/format, JavaScript syntax and diff checks passed. Restart the host and refresh already-open dashboards to activate the updated protocol and assets.

Simplified-grid follow-up validation: **287 tests and 176 subtests passed**, including seven Chromium browser tests. Coverage includes explicit cell sizes, direct dashboard grids, slider/action binding, strict 0–100 values, observed-volume redaction, coalesced reads, write-cache invalidation and one commit per completed drag. Mobile and desktop screenshots were inspected. The browser feedback benchmark remains **65.6 ms median**, including the 60 ms fake action. Ruff lint/format, JavaScript syntax and diff checks passed. No native key, playback or audio-changing actions were executed; actual-device slider acceptance remains pending.

Two-style slider validation on 27 October 2026: **315 tests and 176 subtests passed**, including twelve Chromium browser cases. Both styles were checked for typed configuration, observed state and single commits. The iOS-style control additionally has vertical/horizontal mouse-drag, touch, cancellation, keyboard endpoint, shared readback and unavailable/recovery coverage. Both orientations were visually inspected. Tap-to-feedback measured **64.1 ms median**, including the same 60 ms fake action. Ruff lint/format, JavaScript syntax and diff checks passed. No native host controls were changed by these tests; physical iPhone/Safari acceptance remains pending.

Live-slider follow-up validation: **331 tests and 176 subtests passed**, including nineteen Chromium browser cases. The live behavior replaces the release-only commits described in earlier validation notes. Checks cover updates before release in both styles, immediate preview, 50 ms pacing on a fast fake host, latest-only coalescing on a slow host, final-value delivery, cancellation without rollback, cancellation preserving queued buttons, no replay after a lost response, independent bounded slider rate limits, completion-wait deadlines/cancellation and session expiry. Median input-to-confirmation was **67.7 ms** for standard and **68.2 ms** for iOS style, including a 60 ms fake action and without releasing the slider. Ruff lint/format, JavaScript syntax and diff checks passed. Actual Wi-Fi/device latency and physical iPhone/Safari acceptance remain unverified; no native host controls were changed by these tests.
