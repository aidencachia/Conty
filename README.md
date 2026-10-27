# Conty

A YAML-driven browser dashboard for controlling your Mac over your local network.

Milestone 2 implements configuration validation, pairing, a protected HTTP API, ordered execution and a phone-friendly dashboard. PowerPoint, Apple Music, system volume and configured processes use the existing host adapters. The owner reported verifying the Milestone 1 hardware proof; the new server/helper path still needs its separate MacBook/iPhone acceptance run.

## Setup

Requires Python 3.11+ and a logged-in macOS desktop session. Run as your normal user, not with `sudo`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[macos,test]'
.venv/bin/conty doctor
```

`doctor` requests a macOS Accessibility permission prompt if access is missing. Use the dialog's System Settings button to enable the launching terminal/editor or host, then rerun `doctor`. macOS may suppress repeat prompts; the report also includes a command to open the permission pane manually. Use `conty doctor --no-prompt` for passive checks. Neither mode sends keys, changes playback, or writes audio properties; Music Automation checks remain non-prompting. It exits nonzero if any native capability is unavailable, including while consent is pending. It reports the Python executable and launch-method caveat for permission troubleshooting.

Keyboard input uses optional PyObjC Cocoa, Quartz and ApplicationServices bridges. Music uses the system `osascript` JXA bridge; Core Audio and direct process execution use the standard library. Process execution and fake tests also work without the macOS extra. Tested dependency versions are recorded in the [host-control guide](documentation/milestone-1-host-control.md).

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

HTTP traffic is unencrypted; this prototype is for a trusted private LAN. Pairing grants access to every configured action. Configuration is trusted executable input. Configuration changes require a service restart in Milestone 2.

For a harmless process-only example, use `examples/script.yaml`. See the [Milestone 2 guide](documentation/milestone-2-dashboard.md) for schema details, limits, request semantics and acceptance checks.

The dashboard is a compact grid of labelled symbols. Set `position: {row: 1, column: 2}` and `size: {width: 1, height: 2}` to place and size a tile in cells. Pages can declare `layout`/`widgets` directly; existing sections and span fields still work. Sliders support `style: standard` (the original track and thumb, also the default) or `style: ios` (a broad filled control inspired by iOS Control Centre). The Media example uses the iOS style. Both read actual system volume and apply changes live while dragging: immediate preview, up to 20 updates/second, and only the latest value waiting behind one in-flight update. Releasing preserves the final selection; cancelling discards unsent values but cannot undo already applied changes. Keyboard controls work the same way. Activity stays collapsed until opened.

Native actions use a warmed supervised worker to avoid Python/framework startup on each tap, and the browser receives completion feedback through immediate batched status reads. Read-only health probes run separately from action dispatch. See the [latency checks](documentation/milestone-2-dashboard.md#latency-regression-checks) for repeatable benchmarks.

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

# Apple Music must already be running and Automation permission granted.
.venv/bin/conty music play_pause
.venv/bin/conty music previous_track
.venv/bin/conty music next_track

# Current default output device must support writable master volume AND mute.
.venv/bin/conty volume adjust --delta 5
.venv/bin/conty volume adjust --delta -5
.venv/bin/conty volume toggle_mute

# Absolute executable, literal arguments, no implicit shell.
.venv/bin/conty process --timeout 5 -- /bin/echo 'hello from Conty'
```

Keyboard actions use physical ANSI key names, not Unicode text typing; output depends on the active keyboard layout and application. The public `SUPPORTED_KEYS` tuple lists the supported vocabulary: `a`–`z`, `0`–`9`; `minus`, `equal`, `left_bracket`, `right_bracket`, `backslash`, `semicolon`, `quote`, `grave`, `comma`, `period`, `slash`; `return`, `tab`, `space`, `backspace`, `escape`, `delete`, `home`, `end`, `page_up`, `page_down`, `left`, `right`, `up`, `down`; `f1`–`f20`; `keypad_0`–`keypad_9`, `keypad_decimal`, `keypad_multiply`, `keypad_plus`, `keypad_clear`, `keypad_divide`, `keypad_enter`, `keypad_minus`, `keypad_equal`. Optional modifiers are `shift`, `control`, `option`, `command`.

`app_id` defaults to `None`: capture the current frontmost running application and send only to that PID, without activation or retargeting. Termination and focus are rechecked immediately before dispatch; failure is fail-closed, with no global-input fallback. With explicit `--app-id`, the target must already be running and the default policy is `activate`; `--focus require_frontmost` checks without activation. Adapter/CLI `focus` defaults to `None`, selecting the appropriate implicit policy. Explicitly specifying a focus policy without an app ID returns `invalid_action`.

Commands emit JSON and exit 0 on success, 1 on adapter failure, or 2 for CLI syntax errors. Errors distinguish permissions, unavailable targets/capabilities, focus failure, timeouts, and execution failure. `effects_possible` means that failure does **not** prove nothing happened. Keyboard/Music success means **dispatched**, not observed slide/playback state.

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
