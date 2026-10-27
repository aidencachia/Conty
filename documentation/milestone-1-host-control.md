# Milestone 1 — host control implementation and acceptance

## Status

The adapters, local CLI and automated tests are implemented. The owner reported completing the Milestone 1 hardware proof during Milestone 2 planning on 19 October 2026. Per-case results and application/display versions were not supplied, so the checklist below preserves its historical pending entries rather than inventing evidence. The separate [Milestone 2 server/helper acceptance](milestone-2-dashboard.md#hardware-acceptance) remains to be recorded.

Milestone 2 now defaults media actions to generic system media keys, including Spotify support through macOS routing. Use `conty media ...`; `doctor` checks generic `media` capability instead of Apple Music. The Apple Music-specific details below remain historical/legacy behavior for `conty music ...` or explicitly targeted Music actions, not requirements for the generic Media page.

Implementation environment: Python 3.11.1; PyObjC 12.2.2; pytest 9.1.1. Dependency ranges live in `pyproject.toml`; these are the versions used for this validation, not a claim of testing every compatible version. Application, macOS and presentation-display versions remain to be recorded during acceptance.

## Local entry point

Install from the repository as described in the README. `conty --help` and each subcommand's `--help` describe all arguments. `python -m conty` is an equivalent entry point. Local diagnostic commands do not listen on the network and no native action runs on import. Milestone 2 adds an explicit `serve` command.

Use a stable terminal and Python installation throughout the proof. Run in the logged-in user's desktop session; never elevate permissions. The future packaged host may have a different TCC identity and must be separately validated.

### Accessibility setup

1. Run `conty doctor` from the same terminal/launch method you will use for testing. If Accessibility access is missing, it requests the macOS permission dialog. Use the dialog's System Settings button to navigate to the permission pane. The request is asynchronous: enable access and rerun `doctor`; it can still report `permission_denied` while you are granting consent. macOS may suppress repeat prompts, so use the manual steps below if no dialog appears.
2. Open **Apple menu > System Settings > Privacy & Security**, then **scroll down in the right-hand pane to Accessibility**. This is the app-permission list, **not** the main Accessibility page in the settings sidebar. On macOS Monterey 12 and earlier, use **System Preferences > Security & Privacy > Privacy > Accessibility** instead.
3. Enable the app used to launch Conty, such as Terminal, iTerm, or Zed when using its integrated terminal. If it is absent, click **+**, select that application, and click **Open**, then enable it. Authenticate if macOS asks. `doctor` reports the Python path, but macOS can attribute permission to the responsible terminal/editor rather than Python itself; authorize the identity shown by macOS for your launch method, not every example app.
4. Restart the host/terminal if required by macOS and rerun `doctor` from the same app.
5. If using Zed's sandboxed execution gives different results, repeat from your normal terminal. A sandbox failure does not establish the desktop session's actual capability.

To navigate directly to the permission pane, run this command yourself:

```sh
/usr/bin/open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
```

This only opens settings; it does not grant permission. If your macOS version does not honor the deep link, use the manual path above. See [Apple's Accessibility permission guide](https://support.apple.com/guide/mac-help/allow-accessibility-apps-to-access-your-mac-mh43185/mac).

Only `doctor` requests Accessibility consent by default; `doctor --no-prompt` performs passive checks. Keyboard commands never request consent. Revoking permission must yield `permission_denied` before input dispatch.

### Apple Music Automation setup

Music commands deliberately **never request consent**. Their helper checks permission for the exact event with `AEDeterminePermissionToAutomateTarget(..., false)`, then sends to a running PID with `kAEDoNotPromptForUserConsent`. On a fresh installation permission may therefore remain denied until you deliberately authorize automation locally.

Open Music yourself first. To deliberately request initial consent from the same terminal/launch context, run this separate local setup command and approve only the identity you expect:

```sh
/usr/bin/osascript -e 'tell application id "com.apple.Music" to get player state'
```

This is a setup instruction, not a command run by Conty or its tests. It requests a read, not a playback change; unlike Conty's PID-directed path, application-addressed AppleScript can launch Music, so open Music first. Inspect System Settings > Privacy & Security > Automation, then rerun `conty doctor`. If consent is denied or the responsible identity differs, diagnose it locally; Conty does not retry prompts or redirect commands. Terminal, editor, service and packaged-app identities can differ. Do not reset global privacy settings as a routine fix.

`doctor` checks all three event permissions without sending them. The JXA native bridge intentionally fails closed with `capability_unavailable` if it cannot be established. Fake tests cannot prove the bridge or its permission attribution works on a given OS version.

## Implementation decisions

### Generalized keyboard input

- `app_id` is optional and defaults to `None`. Without an app ID, snapshot the current frontmost running application and its PID when the action starts. Never activate, launch, or retarget to a newly focused application.
- An explicit `--app-id` selects a running application by bundle identifier, not an executable path. Resolve exactly one running application; never launch it automatically. PowerPoint is only a reference example: `conty keyboard right --app-id com.microsoft.Powerpoint`.
- Adapter/CLI `focus` defaults to `None`, selecting the implicit policy appropriate to targeting: no activation for a frontmost snapshot; `activate` for an explicit app ID. Explicitly specifying either focus policy without an app ID is `invalid_action`.
- With an explicit target, `activate` requests activation then polls for frontmost confirmation within the deadline; `require_frontmost` never activates. For both explicit and snapshot targeting, recheck target PID/termination, permission, focus and deadline immediately before dispatch and fail closed if a check fails.
- The public `SUPPORTED_KEYS` tuple defines the physical ANSI vocabulary: `a`–`z`, `0`–`9`; `minus`, `equal`, `left_bracket`, `right_bracket`, `backslash`, `semicolon`, `quote`, `grave`, `comma`, `period`, `slash`; `return`, `tab`, `space`, `backspace`, `escape`, `delete`, `home`, `end`, `page_up`, `page_down`, `left`, `right`, `up`, `down`; `f1`–`f20`; `keypad_0`–`keypad_9`, `keypad_decimal`, `keypad_multiply`, `keypad_plus`, `keypad_clear`, `keypad_divide`, `keypad_enter`, `keypad_minus`, `keypad_equal`. Optional modifiers are `shift`, `control`, `option`, `command`.
- Keys identify physical positions, not Unicode characters or text typing. Results depend on the active keyboard layout and application; a letter or punctuation name does not promise the corresponding character on every layout.
- Deliver paired Quartz events with `CGEventPostToPid`, not global event posting. Modifiers are event flags, not separately held physical keys; key-up clears synthetic flags. Always attempt key-up even if down posting raises.
- PID targeting avoids intentionally routing input to another frontmost app. It does not prove an application accepts synthetic events (including PowerPoint in slideshow mode), nor eliminate lifecycle/focus races. A successful result means input was dispatched only. Do not replace it with global input as a silent fallback if PowerPoint fails the gate.
- Focus polling is bounded; individual synchronous PyObjC calls cannot be interrupted by a Python deadline. The local CLI executes one action per invocation. **Do not run concurrent CLI actions when ordering matters.** A future server must serialize work and supervise native operations, not abandon timed-out threads that may later send input.

### Apple Music

- Only `com.apple.Music`, with fixed previous/play-pause/next event templates. No user text becomes scripting source.
- The short-lived `osascript` helper resolves the already-running PID, checks Automation from the sending process, and sends directly to that PID without activation or global media-key fallback.
- Parent bounds execution with process-group termination/reaping. Cleanup may add up to 1.2 seconds after the requested deadline. Failure to confirm cleanup disables that adapter instance until recovery.
- Event success is dispatch confirmation, not an observed playback-state binding.

### Core Audio

- Resolve the default output device per operation. Require **both** writable scalar volume and mute on output scope, main/master element 0. No channel-gain fallback or emulated mute.
- Read actual volume/mute; clamp volume plus integer percentage-point delta to 0–100. Adjustment writes only volume, toggle writes only mute.
- Check route/aliveness around writes and read back both properties; report route loss or mismatched readback as failure, with possible effects after a write attempt.
- Channel-only, fixed-volume, mute-less and quantized devices that cannot meet the mapping are rejected honestly. This may exclude HDMI, USB or Bluetooth devices; test the real presentation output.
- External system controls can race read/modify/write. No system-wide transaction, rollback or persistent state cache is claimed. Native Core Audio calls are synchronous and not independently cancellable.

### Direct process invocation

- Absolute executable and argument vector, no implicit shell. A local owner can explicitly invoke an interpreter/shell; this is trusted local executable input.
- Minimal inherited environment plus explicit name-to-host-variable mappings; separate cwd support.
- Drain both pipes concurrently, retain at most 16 KiB each, reject nonzero exits with the exit code.
- Timeout/cancellation attempts process-group TERM, waits 200 ms, attempts KILL, then bounds drain/reap to one second. Unconfirmed cleanup makes the adapter unavailable. Normal success only confirms leader exit and pipe completion, not completion of detached work. Side effects and detached descendants may remain.
- Output is for local diagnostics, never a future browser projection. Environment values are not included in error messages.

## Hardware acceptance checklist

Run with harmless content, moderate speaker volume and no sensitive application focused. Keep a local recovery path. Record actual results rather than assuming success from a JSON response. For no-app-ID tests, use a local delayed invocation (for example, `sleep 3; conty keyboard a`) and focus the intended scratch app before Conty starts; otherwise the launching terminal/editor may be the snapshot target. Use disposable content for destructive keys and test function/keypad behavior only where safe. Race checks may require controlled instrumentation; mark them pending if they cannot be reproduced manually.

| Check | Expected result | Status |
| --- | --- | --- |
| Record macOS, PowerPoint, Music versions, Python path, terminal identity and display/audio setup | Reproducible environment | Pending |
| Confirm installed PowerPoint bundle ID | Matches configured target | Pending |
| No `--app-id`, harmless non-PowerPoint app frontmost when the action starts | Key reaches only the snapshotted PID; no activation or focus change | Pending |
| Non-PowerPoint scratch document: letters, digits, named punctuation, navigation, function and keypad keys from `SUPPORTED_KEYS` | Physical key behavior matches the app/layout; record app, layout and each tested key, including unsupported app behavior | Pending |
| Repeat letter/punctuation tests with another keyboard layout | Physical positions remain the contract; Unicode output is not assumed | Pending |
| No `--app-id`: switch focus or terminate the snapshotted app before dispatch | Fail closed; no activation, retargeting or global fallback | Pending |
| Specify `--focus activate` or `--focus require_frontmost` without `--app-id` | `invalid_action`, no key or activation | Pending |
| PowerPoint right/left/B with `--app-id com.microsoft.Powerpoint`, on actual presentation display | Next/previous/blackout then restore visibly work | Pending |
| Explicit `--app-id com.microsoft.Powerpoint`, omitted focus or `--focus activate`, from another app | PowerPoint becomes frontmost before input; other app receives no key | Pending |
| `--app-id com.microsoft.Powerpoint --focus require_frontmost` while another app is active | Focus error, no key anywhere | Pending |
| Close target or switch focus during activation | Honest unavailable/focus failure; no global fallback | Pending |
| Revoke Accessibility, run keyboard test, restore permission | Permission error, no key elsewhere | Pending |
| Music playing in background; previous/play-pause/next | Only Music responds; focus stays unchanged | Pending |
| Quit Music and invoke a command | Target unavailable; Music stays closed | Pending |
| Deny Music Automation | Permission failure without repeated prompts | Pending |
| Adjust ±5 at several levels including near 0 and 100 | Actual volume changes/clamps; mute unchanged | Pending |
| Toggle mute twice, including with nonzero stored volume | Mute toggles; stored volume unchanged | Pending |
| Switch default output between actions | Each action resolves the selected device | Pending |
| Unsupported output / disconnect / sleep-wake | Honest capability/target error, no unrelated gains changed | Pending |
| Direct process success/nonzero/bounded output/timeout | Structured outcomes, no hanging executor | Automated; manual launch-context check pending |

## Validation evidence

Historical validation before the generalized keyboard contract: automated fake-native and harmless subprocess tests passed: **61 tests, 47 subtests**. These counts are not validation of the generalized keyboard changes. Coverage includes focus/permission changes, missing targets, release attempts, Music helper cleanup, unsupported devices, actual-state audio semantics, direct argument handling, environment restrictions, bounded pipe draining, nonzero exits, cancellation and timeouts with a TERM-resistant descendant.

In that historical validation, a passive `conty doctor` run in the editor tool session reported:

- Keyboard: `permission_denied` (Accessibility).
- Music: `target_unavailable` (no running Music process observed).
- Audio: `target_unavailable` (no default output device observed).

These are safe diagnostic outcomes, **not a completed hardware acceptance run** and not proof those capabilities are unavailable outside the execution sandbox. No keyboard, playback or audio-changing commands were run. Git status inspection was blocked by the machine's unaccepted Xcode license; no commits or branches were created.

### Pre-Milestone 2 cleanup validation (2026-10-19)

After the generalized keyboard implementation and cleanup, `.venv/bin/python -m pytest -q` passed: **144 tests, 176 subtests**. This supersedes the historical automated counts above, not the pending hardware acceptance gate. Some CLI subtests were migrated to parameterized pytest cases, so subtest counts are not directly comparable.

The cleanup adds dedicated fake-backed CLI boundary coverage (argument forwarding, focus defaults, doctor prompting, JSON/exit-code behavior, syntax rejection, literal process arguments and environment mappings), shared timeout validation, explicit keyboard bridge injection and public entry-point annotations. Keyboard bridge tests verify injection avoids native loading and that the default remains lazy. Ruff 0.14.14 provides a formatting and basic lint/import-order baseline; development commands are in the README.

No input, playback or audio-changing native actions were run. Serialized execution/native-hang supervision and the public response projection remain Milestone 2 work, not guarantees supplied by this cleanup.

## Milestone 2 boundary

The diagnostic CLI remains owner-local. Milestone 2 now supplies the strict configuration pipeline, public projection, pairing/security, bounded serialized dispatcher, native helper supervision and API feedback. See the [Milestone 2 guide](milestone-2-dashboard.md) for the implemented boundary and remaining server-path hardware acceptance.
