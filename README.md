# Conty

A YAML-driven browser dashboard for controlling your Mac over your local network.

Milestone 2 implements configuration validation, pairing, a protected HTTP API, ordered execution and a phone-friendly dashboard. PowerPoint, generic media playback, system volume and configured processes use native host adapters. The owner reported verifying the Milestone 1 hardware proof; the new server/helper path still needs its separate MacBook/iPhone acceptance run.

## Setup

Requires Python 3.11+ and a logged-in macOS desktop session. Run as your normal user, not with `sudo`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[macos,test]'
.venv/bin/conty doctor
```

`doctor` requests a macOS Accessibility permission prompt if access is missing. Use the dialog's System Settings button to enable the launching terminal/editor or host, then rerun `doctor`. macOS may suppress repeat prompts; the report also includes a command to open the permission pane manually. Use `conty doctor --no-prompt` for passive checks. Neither mode sends keys, changes playback, or writes audio properties. The report checks keyboard, generic media and audio capabilities; Apple Music does not need to be running or authorized. It exits nonzero if any native capability is unavailable, including while consent is pending. It reports the Python executable and launch-method caveat for permission troubleshooting.

Keyboard input and generic media use optional PyObjC Cocoa/Quartz bridges, with ApplicationServices for keyboard permission checks. Core Audio and direct process execution use the standard library. Only the legacy explicitly targeted Apple Music adapter uses `osascript`. Process execution and fake tests also work without the macOS extra. Tested dependency versions are recorded in the [host-control guide](documentation/milestone-1-host-control.md).

## Start a dashboard

```sh
.venv/bin/conty validate examples/conty.yaml
.venv/bin/conty serve examples/conty.yaml
```

Open the printed URL and enter the temporary code printed in the terminal. The reference configuration includes Presentation and Media pages. When `server.bind` is omitted, Conty automatically detects the host's IPv4 address from its active network route and uses it for the listener, printed URL and QR code. Remove the example's existing `bind` line to enable this:

```yaml
server:
  port: 8765
```

An explicit `bind` always wins: use `bind: 127.0.0.1` for same-computer access, or a specific IP to choose an interface. Detection sends no packets and needs no internet connection. If no usable IPv4 route is available, Conty falls back to `127.0.0.1`. With multiple interfaces or a VPN, override `bind` if the detected address is not reachable by your phone. Restart after changing networks; the address is selected once per launch. Wildcard binding (`0.0.0.0` or `::`) still requires explicit `allowed_hosts`.

Keep the terminal open and connect the phone to the same private network. Scan the printed QR code or open the printed LAN URL. Each pairing code lasts two minutes and is replaced after use. Ctrl-C stops the service and revokes all sessions.

By default HTTP traffic is unencrypted; this prototype is for a trusted private LAN. Use `serve --ssl-certfile /absolute/cert.pem --ssl-keyfile /absolute/key.pem` to enable HTTPS with a certificate trusted by your clients. Pairing grants access to every configured action. Configuration is trusted executable input. Configuration changes require a service restart in Milestone 2.

For a harmless process-only example, use `examples/script.yaml`. See the [Milestone 2 guide](documentation/milestone-2-dashboard.md) for schema details, limits, request semantics and acceptance checks.

The dashboard is a compact grid of labelled symbols. Set `position: {row: 1, column: 2}` and `size: {width: 1, height: 2}` to place and size a tile in cells. Pages can declare `layout`/`widgets` directly; existing sections and span fields still work. Sliders support `style: standard` (the original track and thumb, also the default) or `style: ios` (a broad filled control inspired by iOS Control Centre). The Media example uses the iOS style. Both read actual system volume and apply changes live while dragging: immediate preview, up to 20 updates/second, and only the latest value waiting behind one in-flight update. Releasing preserves the final selection; cancelling discards unsent values but cannot undo already applied changes. Keyboard controls work the same way. Activity stays collapsed until opened.

Native actions use a warmed supervised worker to avoid Python/framework startup on each tap, and the browser receives completion feedback through immediate batched status reads. Read-only health probes run separately from action dispatch. See the [latency checks](documentation/milestone-2-dashboard.md#latency-regression-checks) for repeatable benchmarks.

The Media page sends system-wide previous/play-pause/next keys, so it can control Spotify and other players that handle macOS media keys. Start playback in your preferred player first. macOS decides which player handles each key; Conty neither selects Spotify specifically nor confirms playback state, and the system may resume or launch its default player when none is active. No Spotify account/API integration or Apple Music Automation permission is required. YAML media actions use `target: system` (also the default if omitted). Existing explicit `target: {app_id: com.apple.Music}` actions retain their original Apple Music-only behavior; change them to `target: system` to opt into generic routing.

## PowerPoint carousel widget (experimental)

Open PowerPoint and save the intended deck to disk at least once, then explicitly request Automation permission locally with `.venv/bin/conty powerpoint authorize` from the same launcher and Python environment used to serve Conty. Start `.venv/bin/conty serve examples/conty.yaml` and pair the phone. The Presentation page contains a normal grid carousel (`type: powerpoint`), spanning three columns and two rows, with separate Previous, Next, Blackout and Open PowerPoint YAML buttons below it. It has no `action`; `label` is optional and defaults to `PowerPoint`. Standard `position` and `size` (or position spans) apply, in either a direct dashboard grid or a section. A sole available deck is selected automatically; choose explicitly when several are available. While the selected deck is in **editing view**, the client automatically attempts preview export once per deck revision. It never automatically exports during a slideshow; wait for previews before starting the show. The native backend exports a temporary PDF, renders PNG previews with macOS Quartz, retains only the PNG cache in memory and deletes the PDF. No Office extension, CDN or new package is needed beyond the existing macOS extra. HTTPS remains optional; default trusted-LAN HTTP is unencrypted. Normal service and presentation HTTP calls never request Automation/Accessibility consent or launch/activate PowerPoint. PowerPoint may separately ask to grant access to `~/Library/Application Support/Conty/PowerPoint Previews/` on the first export. Conty reuses that private folder and `preview.pdf` filename across decks and restarts, while deleting the PDF after processing, so each export no longer introduces a new folder grant. The picker shows scan progress and errors, with **Scan presentations** to try again; background discovery and preview generation do not block the existing keyboard buttons.

Refresh previews manually after content-only edits that do not change the observed deck revision; automatic export is not continuous regeneration or an automatic retry loop. Slide titles and hidden-slide badges are snapshot metadata, not individually reread on each poll or jump. Navigation skips a redundant full scan but still freshly checks identity, show ownership, slide IDs/order/numbering and focus. Read-only profiling of an 81-slide deck measured warm scans at about 1.4–1.9 seconds versus 6.7 seconds cold; this is not a live-jump latency measurement. Animations and video remain live-PowerPoint-only. Cached preview images do not expire during slow live-state scans. Downloads are paced and retried independently; **Retry failed previews** reloads images without re-exporting the PDF or replaying a command. Start the slideshow yourself and keep the chosen show active and frontmost. Swipe through previews to select a slide without navigating the live show; double-click or double-tap a preview to jump to that slide. The horizontal carousel has no internal Next/Previous buttons; the separate YAML Previous and Next buttons remain available. These number-and-Return jumps need separate Accessibility permission via `conty doctor` and support only standard all-slides shows numbered from one. The current-slide highlight is polled separately from the selected thumbnail, not measured audience output. Distinct decks with the same filename have separate identities; multiple windows of one presentation are deduplicated. Existing keyboard tiles remain application-wide, outside these deck-bound safeguards.

Read-only discovery was verified against the installed Mac PowerPoint **16.103.1**, including its runtime requirement for exact saved-path targeting when declared window IDs are unavailable. Do not open, close or Save As decks during an operation; reopening the same saved file within one process lifetime cannot be distinguished. Export and navigation are still **not audience-hardware-tested**. Presentation/slide titles and slide content are sensitive and visible to paired clients, which receive access to all configured actions; filesystem paths and private native handles are not published. Native timeouts, uncertain export/restoration or partial key sequences disable control until deliberate local recovery and a Conty restart—do not retry an uncertain command. See [PowerPoint setup, security, limitations and hardware acceptance](documentation/powerpoint.md) before relying on it in a presentation.

## Explicit local tests

**These commands have real desktop effects.** Open the intended applications and read the [permission/setup and acceptance guide](documentation/milestone-1-host-control.md) first.

```sh
# No app ID: snapshot the current frontmost running app; never activate it.
# Arrange for a harmless test app to be frontmost when the command starts.
.venv/bin/conty keyboard a
.venv/bin/conty keyboard left_bracket

# PowerPoint reference example: it must already be running with a test slideshow.
.venv/bin/conty keyboard right --app-id com.microsoft.Powerpoint
.venv/bin/conty keyboard left --app-id com.microsoft.Powerpoint
.venv/bin/conty keyboard b --app-id com.microsoft.Powerpoint

# Explicit target, but never activate it with this policy.
.venv/bin/conty keyboard right --app-id com.microsoft.Powerpoint --focus require_frontmost

# Generic media keys: macOS routes them to the selected player, e.g. Spotify.
.venv/bin/conty media play_pause
.venv/bin/conty media previous_track
.venv/bin/conty media next_track

# Current default output device must support writable master volume AND mute.
.venv/bin/conty volume adjust --delta 5
.venv/bin/conty volume adjust --delta -5
.venv/bin/conty volume toggle_mute

# Absolute executable, literal arguments, no implicit shell.
.venv/bin/conty process --timeout 5 -- /bin/echo 'hello from Conty'
```

Keyboard actions use physical ANSI key names, not Unicode text typing; output depends on the active keyboard layout and application. The public `SUPPORTED_KEYS` tuple lists the supported vocabulary: `a`–`z`, `0`–`9`; `minus`, `equal`, `left_bracket`, `right_bracket`, `backslash`, `semicolon`, `quote`, `grave`, `comma`, `period`, `slash`; `return`, `tab`, `space`, `backspace`, `escape`, `delete`, `home`, `end`, `page_up`, `page_down`, `left`, `right`, `up`, `down`; `f1`–`f20`; `keypad_0`–`keypad_9`, `keypad_decimal`, `keypad_multiply`, `keypad_plus`, `keypad_clear`, `keypad_divide`, `keypad_enter`, `keypad_minus`, `keypad_equal`. Optional modifiers are `shift`, `control`, `option`, `command`.

`app_id` defaults to `None`: capture the current frontmost running application and send only to that PID, without activation or retargeting. Termination and focus are rechecked immediately before dispatch; failure is fail-closed, with no global-input fallback. With explicit `--app-id`, the target must already be running and the default policy is `activate`; `--focus require_frontmost` checks without activation. Adapter/CLI `focus` defaults to `None`, selecting the appropriate implicit policy. Explicitly specifying a focus policy without an app ID returns `invalid_action`.

Commands emit JSON and exit 0 on success, 1 on adapter failure, or 2 for CLI syntax errors. Errors distinguish permissions, unavailable targets/capabilities, focus failure, timeouts, and execution failure. `effects_possible` means that failure does **not** prove nothing happened. Keyboard/media success means **dispatched**, not observed slide/playback state. The legacy `conty music ...` command remains explicitly Apple Music-only and requires its existing Automation setup.

Process output is bounded to 16 KiB per stream, decoded as UTF-8 with replacement, and printed **only to this local CLI**. Treat that output as potentially sensitive; do not use the internal result object as a future public API response. The child inherits only `PATH=/usr/bin:/bin:/usr/sbin:/sbin` and `LANG=en_US.UTF-8`, plus explicit `--env-from CHILD_NAME=HOST_NAME` mappings. Add `HOME` or other variables explicitly when an executable requires them. `--cwd` must be absolute. Timeouts start at child execution, attempt TERM then KILL for the process group, and reap the child. Detached descendants/external effects cannot be undone. An `/usr/bin/open` success does not establish application readiness.

## Tests

```sh
.venv/bin/python -m pytest -q
```

Tests use fake native bridges and real harmless child processes; they never send input or change playback/audio. CLI boundary tests use fake adapters. Browser tests run against a real loopback server with injected adapters. Install their browser once with `.venv/bin/python -m playwright install chromium`; without it, browser tests report a skip. See the [manual checklist](documentation/milestone-2-dashboard.md#hardware-acceptance) for server-path acceptance.

### Development checks

Install the development tools alongside the test dependencies:

```sh
.venv/bin/python -m pip install -e '.[test,dev]'
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m pytest -q
```

Use `.venv/bin/python -m ruff format src tests` to apply formatting. Ruff checks basic Python errors and import ordering; it does not enforce a full static-typing policy.

## Design and next milestones

- [Draft specification](documentation/conty-draft-spec.md)
- [Technical implementation plan](documentation/conty-technical-overview.md)
- [Milestone 1 decisions, permissions, limitations, and acceptance evidence](documentation/milestone-1-host-control.md)
- [Milestone 2 schema, operation, limits, and acceptance](documentation/milestone-2-dashboard.md)

Milestone 3 will add atomic configuration reload, request-ID recovery, individual session management and distribution packaging. Milestone 2 already bounds resources and prevents recognized duplicate requests from executing twice within its retention window.
