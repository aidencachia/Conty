# Conty — draft project specification
Draft v0.2 · 19 October 2026 · project name: Conty

## 1. Purpose

Build Conty, a self-hosted web control panel engine that runs on a computer and exposes a browser interface for controlling that same computer. A locally edited YAML file defines the interface and the actions behind its controls.

The first use case is controlling PowerPoint on a MacBook from an iPhone: next slide, previous slide and blackout. A second reference dashboard controls Apple Music playback and the host's system output volume. The architecture should also support dashboards for launching applications, running scripts and performing development tasks.

Initial assumptions: one host per server instance; one trusted local owner edits the configuration; one or more paired browser clients can use the dashboard. macOS is the first supported platform. Windows and Linux are future adapter targets.

## 2. Goals and initial boundaries

The initial release must:

- Generate a usable dashboard entirely from YAML, with no frontend code required for each dashboard.
- Work over the local network without an internet connection or a phone app.
- Support multiple dashboard pages and grouped buttons with explicit grid dimensions and button positions.
- Execute configured keyboard actions, processes, media playback commands and system volume operations on the host.
- Give clients clear connection and execution feedback.
- Support PowerPoint and media/volume remotes as reference configurations.

Later releases may add sequences, toggles, sliders, status widgets and additional application-specific integrations.

A visual dashboard editor, public internet hosting, a central account service, screen streaming, arbitrary remote terminal access and controlling other computers are outside the initial release. Conty is a configurable control panel engine; YAML remains the source of truth.

## 3. User workflow

1. The owner installs the host service and writes a YAML configuration.
2. A validation command checks the configuration and reports useful errors.
3. The owner starts the service with that configuration and enables LAN access.
4. The service displays its local URL, a QR code for that URL and a temporary pairing code.
5. A phone opens the URL, enters the code and receives a revocable session.
6. The dashboard renders from the active configuration. Tapping a control invokes its configured action on the host.
7. The owner edits the YAML and reloads it. Valid changes become active atomically; invalid changes leave the existing dashboard running.

Editing configuration and starting the service are local operations in the first release.

## 4. System architecture

| Component | Responsibility |
| --- | --- |
| Configuration loader | Parse YAML, validate the schema and action references, and produce an immutable configuration revision. |
| Web server | Serve the dashboard, pair clients, authenticate requests and expose action/status endpoints. |
| Dashboard renderer | Convert the public dashboard definition into responsive browser controls. |
| Action dispatcher | Resolve action IDs, enforce execution policy and manage invocation status. |
| Host adapters | Implement keyboard input, process execution, media playback and system volume operations, including platform-specific permissions and capability checks. |
| Runtime store | Track sessions, active configuration, queued/running invocations and bounded recent history. |

The browser sends an action ID, request ID and configuration revision. The host resolves the implementation from its own validated configuration. Executable paths, scripts and environment values stay on the host.

The web server and executor can live in one process initially. Platform adapters should have a narrow interface so adding another operating system does not require changing dashboard rendering.

## 5. YAML model

This is the proposed Milestone 2 configuration model. Milestone 1 provides adapters and a local CLI only; no YAML loader is implemented yet.

Use a versioned, declarative schema with four main areas:

- **Server:** listening address, port and pairing policy.
- **Defaults:** execution timeout and shared behavior.
- **Actions:** reusable named operations.
- **Dashboards:** pages, sections, layout and widgets that reference actions.

A dashboard is a named control page. A section is a labelled group with its own explicitly sized grid. A button is a widget placed at a particular grid coordinate that invokes one action. IDs are stable identifiers; labels can change without changing identity.

Example configuration, illustrating the proposed schema:

```yaml
version: 1

server:
  bind: "0.0.0.0"
  port: 8765
  auth:
    mode: pairing_code

defaults:
  action_timeout_seconds: 5

actions:
  next_slide:
    type: keyboard
    target:
      app_id: "com.microsoft.Powerpoint"
      focus: activate
    key: right

  previous_slide:
    type: keyboard
    target:
      app_id: "com.microsoft.Powerpoint"
      focus: activate
    key: left

  toggle_blackout:
    type: keyboard
    target:
      app_id: "com.microsoft.Powerpoint"
      focus: activate
    key: b

  open_powerpoint:
    type: process
    argv: ["/usr/bin/open", "-a", "Microsoft PowerPoint"]

  previous_track:
    type: media
    target:
      app_id: "com.apple.Music"
    command: previous_track

  play_pause:
    type: media
    target:
      app_id: "com.apple.Music"
    command: play_pause

  next_track:
    type: media
    target:
      app_id: "com.apple.Music"
    command: next_track

  volume_down:
    type: volume
    target: system
    operation: adjust
    delta: -5

  toggle_mute:
    type: volume
    target: system
    operation: toggle_mute

  volume_up:
    type: volume
    target: system
    operation: adjust
    delta: 5

dashboards:
  - id: presentation
    title: "Presentation"
    sections:
      - id: slides
        title: "Slides"
        layout:
          rows: 2
          columns: 2
        widgets:
          - id: previous
            type: button
            label: "Previous"
            action: previous_slide
            position: { row: 1, column: 1 }

          - id: next
            type: button
            label: "Next"
            action: next_slide
            appearance: primary
            position: { row: 1, column: 2 }

          - id: blackout
            type: button
            label: "Blackout / restore"
            action: toggle_blackout
            position:
              row: 2
              column: 1
              column_span: 2

      - id: applications
        title: "Applications"
        layout:
          rows: 1
          columns: 1
        widgets:
          - id: launch
            type: button
            label: "Open PowerPoint"
            action: open_powerpoint
            position: { row: 1, column: 1 }

  - id: media
    title: "Media"
    sections:
      - id: player
        title: "Playback and volume"
        layout:
          rows: 2
          columns: 3
        widgets:
          - id: previous_track
            type: button
            label: "Previous"
            action: previous_track
            position: { row: 1, column: 1 }

          - id: play_pause
            type: button
            label: "Play / pause"
            action: play_pause
            appearance: primary
            position: { row: 1, column: 2 }

          - id: next_track
            type: button
            label: "Next"
            action: next_track
            position: { row: 1, column: 3 }

          - id: volume_down
            type: button
            label: "Volume −5%"
            action: volume_down
            position: { row: 2, column: 1 }

          - id: mute
            type: button
            label: "Mute / unmute"
            action: toggle_mute
            position: { row: 2, column: 2 }

          - id: volume_up
            type: button
            label: "Volume +5%"
            action: volume_up
            position: { row: 2, column: 3 }
```

### Explicit layout for the MVP

- Every section declares positive integer `layout.rows` and `layout.columns`.
- Every widget declares positive integer `position.row` and `position.column`. Coordinates start at **1** in the top-left corner and are relative to the containing section.
- Optional `position.row_span` and `position.column_span` default to 1 and must be positive integers.
- A widget occupies all cells in its rectangular span. Its occupied cells must remain inside the declared grid and must not overlap another widget.
- Empty cells are allowed and remain empty. The MVP does not auto-place widgets.
- Sections appear vertically in configuration order. Row/column positions apply within each section, not across the entire page.
- Preserve row/column positions, gaps and spans on every screen size. Size cells to the viewport where possible; when minimum usable button size cannot be maintained, allow horizontal scrolling rather than rearranging controls. Adaptive breakpoint layouts can be added later.
- Render and keyboard-navigation order follows row, then column, independently of widget declaration order.

The presentation grid places Previous and Next in row 1; Blackout spans both columns of row 2. The media grid has two rows and three columns:

| Row / column | 1 | 2 | 3 |
| --- | --- | --- | --- |
| 1 | Previous | Play / pause | Next |
| 2 | Volume down | Mute / unmute | Volume up |

The media and volume action types below are proposed Conty built-ins; they must be implemented by host adapters. This example targets Apple Music playback explicitly and the currently selected system audio output for volume.

Schema rules:

- Require a supported integer schema version; reject unknown fields, duplicate YAML keys, duplicate IDs, unresolved references, out-of-bounds grid positions and overlapping widget spans.
- Use safe YAML parsing with bounded file size and nesting; reject executable/custom tags.
- Give errors a configuration path, such as `dashboards[0].sections[0].widgets[1].action`, and a line/column where available.
- Define key names and modifiers independently of the underlying OS key codes.
- Resolve relative script paths and working directories against the configuration directory.
- Require process arguments as an array; do not implicitly run them through a shell.
- Allow optional `cwd`, `env_from` and per-action timeout for process actions. `env_from` maps child environment names to existing host environment names; values are never returned to clients.
- Keep persistent configuration separate from execution history and live device state.
- Fail validation for action types unsupported on the current host.

The examples' application bundle IDs, key delivery and Apple Music playback behavior must be verified on the MacBook during the macOS prototype.

## 6. Initial actions

| Action type | Configuration | Required behavior |
| --- | --- | --- |
| Keyboard | Physical key, optional modifiers and optional target application/focus policy. | Deliver one key press and release to the resolved running application's PID through the host adapter. |
| Process | Executable and argument array, optional working directory/environment references and timeout. | Start the configured process as the current user; capture bounded output and its exit result. |
| Media | Target application ID and command: `play_pause`, `previous_track` or `next_track`. | Send the command to the named player's supported adapter without requiring it to be frontmost. |
| Volume | `target: system`; operation `adjust` with integer `delta`, or `toggle_mute` without a delta. | Adjust the current system output volume or toggle its mute state. |

Media playback initially requires an Apple Music adapter on macOS. Unsupported players must fail validation; an unavailable player must fail execution clearly. Do not redirect commands to a different player. Other players and global media-key routing are later extensions.

A volume delta is a signed number of percentage points on a 0–100 scale, bounded to −100 through 100. Read the actual current output volume before each adjustment and clamp the result to 0–100. An adjustment preserves mute state; `toggle_mute` reads and inverts the actual mute state without changing the stored volume. Volume operations target the output device selected when the action executes. If software volume/mute control is unavailable, return a capability error rather than reporting success.

For the MVP, volume uses buttons. A continuous slider and a displayed live percentage require a state binding and remain later features. Play/pause and mute buttons do not imply that their current state is known to the browser.

Scripts can be invoked using a configured interpreter and script path. Browser clients cannot supply arbitrary arguments, executable names or script contents in the first release.

Keyboard actions are generalized, not PowerPoint-specific. With `app_id` omitted (`None` in the adapter), snapshot the current frontmost running application and PID when execution starts; never activate or retarget it. With an explicit app ID, the application must already be running and the implicit focus policy is `activate`; `require_frontmost` instead fails if it is not already active. Adapter/CLI `focus` defaults to `None` to select the appropriate implicit policy. Explicitly specifying a focus policy without an app ID is `invalid_action`. Recheck termination, focus, permission and deadline immediately before PID-directed dispatch; fail closed without global input or another target if a check fails.

The public `SUPPORTED_KEYS` tuple defines physical ANSI key names: `a`–`z`, `0`–`9`; `minus`, `equal`, `left_bracket`, `right_bracket`, `backslash`, `semicolon`, `quote`, `grave`, `comma`, `period`, `slash`; `return`, `tab`, `space`, `backspace`, `escape`, `delete`, `home`, `end`, `page_up`, `page_down`, `left`, `right`, `up`, `down`; `f1`–`f20`; `keypad_0`–`keypad_9`, `keypad_decimal`, `keypad_multiply`, `keypad_plus`, `keypad_clear`, `keypad_divide`, `keypad_enter`, `keypad_minus`, `keypad_equal`. Modifiers are `shift`, `control`, `option`, `command`. This is physical key input, not Unicode text typing; output depends on keyboard layout and application. The proposed YAML validator should use the same vocabulary and targeting rules.

An OS adapter can confirm that it dispatched input; it cannot necessarily confirm that the application performed the intended action. For example, sending B does not prove that PowerPoint is showing a black screen.

A later `sequence` action may invoke existing actions in order with bounded delays and stop on failure. Its design must specify cycle detection, timeouts and partial completion before it is added.

## 7. Dashboard behavior

- Work comfortably on iPhone Safari, with large touch targets and portrait/landscape layouts.
- Render pages and explicit section grids from YAML; preserve configured rows, columns, gaps and spans across screen sizes, using scrolling when needed.
- Provide labelled controls, accessible focus states and sufficient contrast.
- Show connection state separately from action state.
- Acknowledge accepted invocations immediately; show queued, running, succeeded, failed or timed-out status.
- Keep navigation buttons usable for intentional rapid presses while preserving command order.
- Keep all initial assets local, including icons and fonts.
- Provide a full-screen-friendly layout and an optional keep-awake feature where browser capabilities and deployment allow it.

Do not render a stateful toggle merely because a command was sent. A future toggle or slider needs an explicit state source and an unknown/unavailable state. Blackout is therefore a button labelled “Blackout / restore” in the initial release.

## 8. Execution and connection semantics

Use a bounded FIFO queue with one active invocation per host initially. All clients share this queue. Apply per-client rate limits and reject requests when the queue is full rather than silently dropping commands.

Each invocation has a client-generated request ID, server invocation ID, action ID, configuration revision and timestamps. Retain request IDs for a bounded window so retransmitting the same request cannot execute the action twice within that window. Retention duration and limits must be documented; this is not an exactly-once guarantee across restarts.

Clients must not automatically retry commands after a lost connection. They should reconnect, retrieve known invocation status and show “outcome unknown” when the host cannot establish what happened.

Enforce action timeouts. For processes, attempt to terminate the process group started by the action and report the result; external effects and detached descendants may remain. Cancellation is best effort and cannot undo a keystroke or completed command.

A valid reload creates a new revision. Allow the active invocation to finish against its original configuration, cancel queued invocations that have not started, and reject subsequent requests using the old revision. Browser clients then fetch the new dashboard.

## 9. Host access and permissions

Bind to loopback by default. LAN access is an explicit configuration choice, as in the example. Require pairing for control access; generate temporary codes at runtime, limit pairing attempts and allow the owner to revoke sessions.

Authorize every action/status request. Validate request origins and accepted Host values, and protect cookie-authenticated mutations against cross-site requests. Configuration files are trusted executable input: editing them is equivalent to granting access to the host user's capabilities.

Run as the current user without automatic privilege elevation. Record action IDs, timestamps, duration and results; redact secrets and avoid returning command output to phones by default. Keep history bounded.

For a trusted private LAN, HTTP can be an explicitly documented initial deployment mode; it does not encrypt session credentials or command traffic. Provide a TLS deployment path before supporting untrusted networks.

On macOS, the setup flow must explain and diagnose the Accessibility/Automation permissions required by the chosen adapter. Missing permissions should produce a clear failure.

## 10. Proposed interfaces

Conceptual API; exact routes remain an implementation decision:

| Endpoint | Purpose |
| --- | --- |
| `POST /api/pair` | Exchange a temporary code for a session. |
| `GET /api/dashboard` | Retrieve the public dashboard schema and active revision. |
| `POST /api/invocations` | Submit an action ID, request ID and configuration revision. |
| `GET /api/invocations/{id}` | Retrieve execution state for the current session. |
| `GET /api/status` | Retrieve authenticated connection, capability and permission status. |

The public dashboard definition includes presentation fields and action references, with executable details removed. HTTP polling is sufficient initially. Event streaming or WebSockets can be introduced if live status needs justify them.

Conceptual CLI:

```text
conty validate conty.yaml
conty serve conty.yaml
```

These are proposed Conty CLI commands, not an implemented interface.

## 11. Implementation direction

Keep the schema language-independent. One reasonable prototype is a Python host with a small HTTP API, a plain HTML/CSS/JavaScript renderer and macOS adapters for input, media playback and system volume. Choose the exact framework and keyboard automation method after testing focus and permission handling on the MacBook.

Avoid requiring a database for the first prototype. Store configuration as YAML and bounded runtime history in memory. If sessions are persisted, store session credentials securely and provide expiration/revocation.

Before building the complete renderer, prove that the host can reliably activate PowerPoint, advance/reverse slides and toggle blackout while the Mac is connected to the actual presentation display. Also prove that it can control Apple Music and adjust/mute the selected system audio output.

## 12. Milestones and acceptance criteria

**Milestone 1 — host control proof:** implement the host adapters and verify PowerPoint next/previous/blackout, process invocation, Apple Music playback, system volume/mute and permission failures on the MacBook.

**Milestone 2 — minimum usable dashboard:** YAML validation, explicit grid placement and spans, page/section/button rendering, LAN serving, pairing, ordered dispatch and action feedback.

**Milestone 3 — dependable daily use:** configuration reload, duplicate-request handling, bounded history, reconnect behavior, setup instructions and packaging.

**Later:** sequences, additional media/application integrations, observed state, toggles/sliders with bounded typed inputs, reusable profiles, and Windows/Linux adapters. A visual editor should write the same YAML schema.

The initial release is accepted when:

1. An iPhone on the same LAN controls a PowerPoint slideshow on the MacBook without internet access or a phone app.
2. A media dashboard controls Apple Music previous/play-pause/next and adjusts system output volume in 5-percentage-point steps, with mute/unmute.
3. A configured process or script runs without changing frontend code.
4. Invalid YAML reports the failing field and preserves the active configuration.
5. Missing permissions and unavailable target apps fail clearly without sending input to another app.
6. Rapid deliberate navigation presses execute in accepted order.
7. Repeating a request ID within the retention window does not repeat the action; unknown outcomes after disconnect are shown honestly.
8. Unpaired clients cannot inspect dashboard/action data or invoke commands.
9. A process timeout leaves the service responsive; unrelated execution history and secrets are not exposed to clients.
10. Every button appears at its configured row and column, with its configured span, in phone and desktop browsers. Empty cells stay empty; invalid or overlapping placements fail validation without replacing the active configuration.
11. Unsupported volume devices and unavailable media targets return clear errors.

## 13. Decisions to revisit after the prototype

- Whether Python is sufficient for the distributed app, or a native/compiled host would make installation easier.
- Whether different command types eventually need separate execution queues.
- Whether users need interactive controls and observed state immediately after the button-based release.
- How much app-specific automation is needed beyond keyboard input.
- Whether HTTPS should become the default and how certificates will be installed/trusted on phones.

## Platform references

- Apple: [Allow apps to control other apps on Mac](https://support.apple.com/en-ie/guide/mac-help/mchl07817563/27/mac/27).
- Apple: [Audio Devices window of Audio MIDI Setup](https://support.apple.com/en-euro/guide/audio-midi-setup/ams1010/mac). Software volume controls depend on output-device capabilities.
- Apple: [Apple Events entitlement](https://developer.apple.com/documentation/BundleResources/Entitlements/com.apple.security.automation.apple-events).
- MDN: [WakeLock](https://developer.mozilla.org/en-US/docs/Web/API/WakeLock). Wake-lock support requires a secure context, so an ordinary HTTP LAN page cannot promise automatic screen wakefulness.

