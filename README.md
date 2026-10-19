# Conty

A local macOS host-control prototype for a future phone-based remote dashboard.

**Implemented scope: Milestone 1 adapters and local diagnostics.** There is no HTTP server, LAN exposure, pairing, YAML loader, or dashboard yet. Native hardware acceptance is still pending; automated tests do not establish that a PowerPoint slideshow responds correctly.

## Setup

Requires Python 3.11+ and a logged-in macOS desktop session. Run as your normal user, not with `sudo`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[macos,test]'
.venv/bin/conty doctor
```

`doctor` requests a macOS Accessibility permission prompt if access is missing. Use the dialog's System Settings button to enable the launching terminal/editor or host, then rerun `doctor`. macOS may suppress repeat prompts; the report also includes a command to open the permission pane manually. Use `conty doctor --no-prompt` for passive checks. Neither mode sends keys, changes playback, or writes audio properties; Music Automation checks remain non-prompting. It exits nonzero if any native capability is unavailable, including while consent is pending. It reports the Python executable and launch-method caveat for permission troubleshooting.

Keyboard input uses optional PyObjC Cocoa, Quartz and ApplicationServices bridges. Music uses the system `osascript` JXA bridge; Core Audio and direct process execution use the standard library. Process execution and fake tests also work without the macOS extra. Tested dependency versions are recorded in the [host-control guide](documentation/milestone-1-host-control.md).

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

Tests use fake native bridges and real harmless child processes; they never send input or change playback/audio. CLI boundary tests use fake adapters and do not start child processes. See the [manual checklist](documentation/milestone-1-host-control.md#hardware-acceptance-checklist) for the separate Milestone 1 hardware gate.

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

Milestone 2 adds configuration, pairing, ordered dispatch, the API and browser UI. Do not expose this diagnostic CLI as an unauthenticated remote execution endpoint.
