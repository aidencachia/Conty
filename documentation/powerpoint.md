# Native PowerPoint slide previews and control (experimental)

## Scope and verification status

Conty uses the already-running Mac PowerPoint application through native Apple Events, exports a temporary PDF, and renders PNG previews with macOS Quartz. There is no browser extension, Office task pane, CDN dependency, cloud upload or alternate renderer. No new package is needed: Quartz is already part of Conty's `macos` extra. Run as your normal logged-in desktop user, never with `sudo`.

The installed **PowerPoint for Mac 16.103.1** scripting definition (SDEF) was **statically verified** for the native implementation. This is not an audience-hardware-tested integration, a claim of compatibility with every PowerPoint release, or proof that a command changed the projector output. Complete the manual acceptance gate below before relying on it.

## Setup: explicit local permissions

1. Install the existing dependencies if needed:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -e '.[macos,test]'
   ```

2. Open PowerPoint yourself. It must already be running before authorization. Conty never launches or activates it.
3. From the **same launcher and Python environment** that will run the service, explicitly request Automation access:

   ```sh
   .venv/bin/conty powerpoint authorize
   ```

   This owner-local command awaits `NativePowerPoint.authorize()` and closes the adapter in `finally`, including on failure. Success prints only a JSON `available` boolean, not native handles, executable paths or document metadata. Adapter failures use the existing CLI JSON error format and exit 1. A successful permission check does not authorize Accessibility, send input, export a document or prove slideshow readiness.

   The actual Apple Event sender is the supervised **child Python helper**, not an Office or browser component. macOS permission attribution depends on the launch chain: it may appear under the terminal/editor or Python identity. Use the same terminal/editor or service launcher and the same `.venv` Python for authorization and serving; consent from a different launcher/interpreter may not apply. Check **System Settings > Privacy & Security > Automation** for the attributed sender's PowerPoint entry after a denial or suppressed repeat prompt.

4. For **double-click/double-tap slide navigation**, separately request Accessibility access:

   ```sh
   .venv/bin/conty doctor
   # Passive diagnostics instead:
   .venv/bin/conty doctor --no-prompt
   ```

   Accessibility is used for the carousel's targeted number-and-Return key sequence, not PDF export or slide observation. Native next/previous Apple Events remain separate backend operations, not carousel buttons. `doctor` remains the existing keyboard/media/audio diagnostic; it does not request PowerPoint Automation. The selected show's `can_control` check requires active/frontmost status, not Accessibility trust, so enabled navigation does not establish permission for number jumps.

**Normal `serve` startup and presentation HTTP requests never request Automation or Accessibility consent.** PowerPoint may independently show its **Grant File Access** dialog during the first preview export to a new folder; that is its sandbox file permission, not an Automation prompt. A remote client cannot request Automation or Accessibility consent. Missing consent fails closed; return to these local setup steps rather than trying to trigger permission through the phone. Neither authorization nor normal presentation operations launch/activate PowerPoint, start a slideshow or switch applications automatically.

## PowerPoint's Grant File Access dialog

PDF exports reuse this dedicated location for all presentations:

```text
~/Library/Application Support/Conty/PowerPoint Previews/preview.pdf
```

The **PowerPoint Previews** folder remains in place across preview loads, Conty restarts and different decks, so PowerPoint can reuse its folder grant instead of being asked about a new random temporary folder every time. Persistence of that grant is controlled by PowerPoint/macOS and still needs testing across Office restarts or permission resets; Conty does not bypass the sandbox or guarantee that Office will never ask again.

After updating, cancel any old `conty-powerpoint-…` temporary-folder prompt that is still open, inspect that no old export is pending, restart Conty and reload the remote. Load previews and, if PowerPoint asks for file access, press **Select…** and approve **PowerPoint Previews**. If needed, press **Command–Shift–G** in the file chooser and enter the folder path above (without `/preview.pdf`). Conty creates it on the first preview export. Do not grant the whole system temporary directory or enable Full Disk Access for this feature.

A private lock prevents simultaneous exports from sharing the filename. The folder and empty lock file are retained, but the PDF is deleted after rendering or attempted failure cleanup. A stale PDF left by a crash is checked and removed before the next export. Deleting the folder can invalidate PowerPoint's remembered grant and cause another prompt.

## Configure the grid widget

`type: powerpoint` is a normal dashboard grid element, not an action button or a separate page. It requires an `id` and `position`, has no `action` field, and accepts an optional `label` (default: `PowerPoint`). Standard `size` and `position.row_span`/`column_span` normalization, bounds and overlap checks apply. When both size and spans are specified they must agree. It works in direct dashboard `layout`/`widgets` grids and in section grids, alongside buttons and sliders. No PowerPoint action definition is needed; a carousel-only configuration can use `actions: {}`. Configuration validation does not probe PowerPoint or request permissions.

The reference Presentation page reserves three columns and two rows for the carousel, with unrelated controls in the remaining rows:

```yaml
layout: {rows: 5, columns: 3}
widgets:
  - id: powerpoint
    type: powerpoint
    position: {row: 1, column: 1}
    size: {width: 3, height: 2}
  - id: previous
    type: button
    label: Previous
    action: previous_slide
    position: {row: 3, column: 1}
  - id: next
    type: button
    label: Next
    action: next_slide
    position: {row: 3, column: 3}
  - id: blackout
    type: button
    label: Blackout / restore
    action: blackout
    position: {row: 4, column: 1}
    size: {width: 3, height: 1}
  - id: launch
    type: button
    label: Open PowerPoint
    action: open_powerpoint
    position: {row: 5, column: 1}
    size: {width: 3, height: 1}
```

The buttons reference actions defined in `examples/conty.yaml`; the carousel does not. The public dashboard preserves `type: powerpoint`, the default/custom label and normalized spans without inventing an action reference.

## Start and use the phone carousel

```sh
.venv/bin/conty validate examples/conty.yaml
.venv/bin/conty serve examples/conty.yaml
```

Connect the phone to the same trusted private LAN, open the printed dashboard URL or QR code and pair with the current terminal code. Save the intended presentation to disk at least once. Open the Presentation dashboard containing the carousel. A sole available deck is selected automatically; when multiple decks are available, explicitly choose the intended presentation. Discovery runs in the background and displays **Looking for open PowerPoint presentations…** rather than leaving an unexplained placeholder. Use **Scan presentations** to scan again; permission or native errors appear next to the picker. Scanning never exports a PDF.

While the selected deck is **in editing view**, the client automatically attempts preview export **once per deck revision**. This guard is client-side, not a service-wide export schedule or a guarantee across browser reloads or separate clients. Automatic export never runs while the deck is in slideshow view. Wait for the preview command to finish before starting a slideshow. A failed attempt must not become a repeated background export loop; use the manual preview control to deliberately retry after resolving a safe failure. After content-only edits that do not change the observed revision, stop the slideshow and explicitly press **Refresh previews** to replace the snapshot. Previews are not continuously regenerated.

Start the chosen slideshow yourself and keep it **active and frontmost** on the Mac. Dismiss menus/dialogs and focus the slideshow canvas, not presenter notes or another input control. Number jumps also require a verifiable Accessibility focus hierarchy; unsupported hierarchies fail closed with an unsafe-focus error. Swipe horizontally through the carousel previews to select a slide without changing the live show. Double-click or double-tap a preview to navigate directly to that slide by dispatching its number and Return. Selecting or swiping alone does not navigate; selection status explains when the show must be started or focused, and command errors remain visible independently. There are **no internal Next/Previous buttons** in the carousel; the separate Previous and Next YAML buttons remain available for ordinary keyboard navigation. The active slide is centered initially and whenever a new active slide is observed. Browsing can move away from it without changing PowerPoint; arrow keys also browse, and Enter activates the focused preview.

Number jumps support only a **standard all-slides show numbered from one**. Custom shows, restricted slide ranges and nonstandard numbering are rejected with an explicit unsupported-show error rather than guessed mappings. Hidden slides are included in preview mapping and marked in the panel; test the resulting jump behavior on your real deck before use.

While the widget's dashboard is active and the browser page is visible, it polls discovery approximately every **two seconds**. Pending command status is polled every **250 milliseconds**, without starting discovery while a command is pending. The current-slide highlight comes from PowerPoint's actual slide ID via Apple Events and is separate from the thumbnail selected for a future jump. It is not inferred from the last button press, an acknowledgement of posted keys or a measurement of audience output. Stale/unavailable observations disable live controls. Recently completed scan results are published before another scan starts, and a changed native observation timestamp is accepted even if a subsequent scan is already running; repeated unchanged cached timestamps do not renew live state.

PDF previews are static, revision-bound snapshots and **do not expire just because live-state polling is slow**. Each image read still requires an active paired session, the current preview revision and an available cached slide. Observed deck closure or mapping changes invalidate the cache; an unobserved closure does not immediately revoke content already cached or received by a client. Animation, video and transition playback remain live-PowerPoint-only.

Thumbnail downloads are lazy, paced and limited to three concurrent requests. A failed image GET is retried at most twice automatically. **Retry failed previews** retries only those cached PNG downloads—it does not export another PDF or replay navigation. Image-loading progress and normal command feedback are announced to assistive technology without adding persistent visible text. Errors remain visible; successful commands are also recorded in Activity.

Existing YAML keyboard tiles are separate application-wide controls and do not inherit this panel's presentation-bound safeguards. Discovery, read-only observations and PDF export do not hold the keyboard executor's input lock, so displaying the carousel or loading previews does not queue ordinary keyboard tiles such as Blackout behind a slow scan. Presentation navigation shares the input lock only while dispatching mutations. Navigation retains a ten-second admission/input-lock-wait deadline, then a separate bounded thirty-second native execution budget. Number jumps validate presentation identity, show ownership, slide order/numbering and show settings once before sending the number and Return. Between key pairs, only process lifetime, Accessibility canvas focus, keyboard permission and frontmost checks run; there are no PowerPoint Apple Event reads until all keys have been sent.

### Navigation latency and metadata snapshots

Navigation does not run a separate full observation before dispatch. Its native preflight freshly verifies presentation/process identity, show ownership, slide IDs, order and numbering, permissions and focus. For a double-click/double-tap slide jump, all key events are prepared before this preflight, then the complete number and Return are sent with lightweight process/focus/permission guards between pairs. The full deck checks are not repeated per digit. The current slide is still read from PowerPoint after dispatch, not assumed from the requested jump. Navigation remains serialized with native discovery, so a scan already in progress can still delay dispatch.

**Fast-jump trade-off:** presentation membership, slide order/numbering, selected show and show settings are not revalidated between digits. Do not edit, reorder, close, switch or Save As presentations during a jump. A concurrent change can cause the complete number and Return to reach a different slide or show before postflight checks identity, mapping and activity. Postflight does not revalidate show settings or detect transient changes that have already reverted; final observation is not rollback or delivery confirmation. Process lifetime and Accessibility input-safety checks still run between pairs, and key-up is always attempted after key-down. The jump is one serialized helper operation, not atomic keyboard delivery.

Warm discovery and navigation reuse slide titles and hidden-slide badges from the metadata snapshot rather than reading every slide's transition settings individually. Refresh previews explicitly after changing these properties; an unchanged slide mapping does not make those badges live. A changed ID/order/number mapping triggers a full discovery read and invalidates the previous preview mapping. Explicit full observations and preview refresh still read hidden flags afresh.

Read-only profiling on the installed PowerPoint 16.103.1 with an 81-slide deck measured about **6.7 seconds cold** versus **1.4–1.9 seconds on warm scans**; warm scans eliminated all 82 hidden-flag reads. These are scan timings, **not measured tap-to-slide latency**. The optimized live jump still needs a user timing check; no live input was sent during performance profiling.

## Preview export and document safety

- The backend uses the persistent **PowerPoint Previews** folder above with mode **0700**, exports `preview.pdf` there and renders it locally using Quartz. The completed PDF is restricted to **0600** before rendering. The PNG preview cache stays in service memory; PDF cleanup is attempted on success and failure, while the folder and its **0600** export lock remain for grant reuse. Existing unsafe permissions, symbolic links, hard links or foreign-owned artifacts are rejected rather than followed or silently changed. No unrelated files are swept. A cleanup failure cannot hide an earlier uncertain native result. A forced host termination can still leave a private PDF behind; this is not secure erasure or an encrypted-disk guarantee.
- Before export, it captures print settings, sets **slides**, **print all**, and **include hidden slides = true**, and verifies those options. It attempts to restore every captured setting in `finally`, including when export fails; restoration is checked and bounded.
- PDF page count must equal the full slide count, including hidden slides. Ordered page-to-slide-ID mapping, slide order, numbering, hidden flags and post-render metadata are checked before publishing previews. A mismatch fails rather than offering a potentially mis-targeted thumbnail. These checks do not visually prove PDF content fidelity; manual comparison is required.
- Export must leave the original document's Save As/full-name identity unchanged or fail. Conty does not repair a renamed document, force-save the original or close the deck. Unsaved edits are not a reason to force a save. Back up a disposable test deck before exercising export on hardware.
- A native hang/timeout can prevent restoration from completing. An error with possible effects is not proof that no setting or input changed. Inspect PowerPoint locally before recovery.

## Presentation and slideshow identity

Private deck identity binds the running **PID** and its **launch time** to the selected presentation. Builds that expose usable native window IDs can use those IDs. PowerPoint 16.103.1 on the tested Mac declares inherited IDs in its scripting dictionary but does not supply usable IDs at runtime; on that build Conty identifies a presentation by its **unique full saved path**, never just its filename. It re-enumerates open paths and verifies the exact full name before each operation. Short-lived root indices are transport selectors, not cached identities; detected membership changes reject an in-flight operation, but during fast number jumps detection may occur only after all keys have been sent. Unsaved or ambiguous full-path targets cannot be selected by guessing. Distinct saved files with the same basename remain distinguishable, and multiple windows of one presentation are deduplicated.

Saved-path checks are not atomic. Do not open, close, reorder or Save As presentations while loading previews or dispatching navigation. Reopening the same saved file within the same process lifetime is indistinguishable on this build; a saved-path key identifies the file, not a provably unique opening session. A detected change after effects causes an uncertain result and requires local recovery.

The selected slideshow is bound through its associated presentation: usable document-window IDs where available, or an exact full-path ownership check with exactly one associated show for saved-path targeting. It is not chosen merely because it is globally active. Commands recheck the exact process lifetime, selected-show relationship, slide ordering, active show and frontmost application. Before each number-jump key pair, captured-PID Accessibility checks reject menus, modal windows, sheets and editable controls; they do not assume that AppleScript and Accessibility window IDs are interchangeable. Number-and-Return dispatch is serialized to the captured PID, with guards before each key-down/up pair and after the final pair; release is attempted even if key-down fails. There is no global System Events keystroke fallback and no automatic activation. Detected process/focus loss aborts rather than sending further key-downs. Deck/show identity and mapping changes during the fast key sequence are checked afterward and may be reported only after Return has been sent. Focus can still change between a check and posting: PID-directed input is not atomic window delivery, and key-up cleanup remains directed to the captured PID.

## Network and privacy

HTTPS is **optional**, not a PowerPoint prerequisite. The default trusted-LAN HTTP warning remains important: traffic, pairing and slide content are **unencrypted**. Do not expose this prototype to the internet or an untrusted network. If you have a certificate trusted by your browser clients, the existing TLS flags still work:

```sh
.venv/bin/conty serve examples/conty.yaml --ssl-certfile /absolute/cert.pem --ssl-keyfile /absolute/key.pem
```

Use both flags together. The server validates the certificate/key before advertising a URL. Only browser clients need to trust the chosen HTTPS certificate; PowerPoint is not an HTTPS client.

Filesystem paths, original full names, temporary PDF names and private native handles are not published to remote clients. **Presentation titles, slide titles and preview content are sensitive** and intentionally visible to paired clients; a presentation title can itself contain a filename or other private text. Pairing grants access to all configured actions and presentation content, not only one chosen deck. There is no per-deck privacy boundary between paired clients. Only pair devices you trust, and remember that clients can retain content they have received. Ctrl-C stops the host and revokes sessions; it cannot retract screenshots or undo dispatched effects.

## Fail-closed recovery

Native timeouts, uncertain export/print-setting restoration, and partial number-key sequences **poison control**. Affected controls stay disabled until deliberate local inspection and a **Conty service restart**. Rerunning a command, refreshing the page, reselecting a deck or recreating an adapter is not recovery. Do not retry an uncertain command: the earlier operation may already have taken effect.

If a response is lost, the panel checks the existing command status rather than blindly resubmitting. Unknown outcomes require local recovery. On shutdown the service bounds helper cleanup and clears its in-memory preview cache and sessions; externally applied effects remain possible.

## Manual Mac/iPhone/audience acceptance gate

This checklist is **pending hardware verification**, not a record of passed tests. Use disposable decks and watch the real audience output, not only the phone or presenter view.

- **Multiple decks/windows:** open two different presentations with the same filename and another deck with multiple document windows. Verify separate keys for distinct decks, deduplication for one presentation, automatic sole-deck selection versus explicit selection for multiple decks, and no retargeting when a window closes or PowerPoint restarts. Move focus between shows/apps during dispatch, and open menus/dialogs before a jump and between its digits and Return. Verify refusal before dispatch or an uncertain result after partial input, with no remainder/retry. Exercise the actual slideshow and Presenter View Accessibility hierarchies; failure to verify them must refuse number jumps.
- **Hidden slides and PDF content:** edit without saving; include hidden slides, titles, images, charts and unusual layouts. Set non-default print ranges/handouts/hidden options first. Verify automatic preview export occurs only in editing view and at most once per deck revision on a client, with deliberate manual refresh/retry and no automatic slideshow export or repeated failure loop. Compare every PDF-derived thumbnail with PowerPoint, including hidden-slide order and page count. Check print settings are restored and the original full name/save destination and unsaved edits are unchanged; no forced save or deck close.
- **Slide numbers and show binding:** test standard all-slides numbering from one, multi-digit jumps, hidden-slide targets, swipe-only selection, double-click/double-tap navigation, and two simultaneously open decks/shows. Confirm the carousel has no Next/Previous buttons. Test custom shows, restricted ranges and numbering offsets for clear rejection. Separately verify selected-thumbnail versus actual-current-ID highlighting after manual navigation.
- **Permissions:** with PowerPoint already running, test allow/deny/revoked Automation using the explicit local command from the real service launcher and Python environment. Test missing Accessibility separately: preview/read must not depend on it, while double-click/double-tap number jumps fail safely. Startup, panel polling and remote commands must never prompt, launch or activate an app.
- **Snapshot fidelity:** edit text, reorder/add/remove slides and change hidden flags. Verify stale mappings are rejected and manual refresh replaces static previews. Confirm that animations/video are not represented as live phone content.
- **Uncertainty:** interrupt export, change focus during a multi-digit jump, revoke permission, or make PowerPoint unresponsive. Verify partial/uncertain effects disable control until local recovery and service restart, without automatically retrying. Check restored print options locally after failures where restoration can complete.
- **Network and shutdown:** test real phone pairing over trusted-LAN HTTP and optional trusted HTTPS, disconnect/reconnect during export and navigation, hide/close the panel, stop Conty and relaunch. Verify no duplicate dispatch, stale active controls, surviving helper, retained service preview cache or usable old session. Inspect for temporary-file leftovers after forced termination.

## Automated checks (no desktop effects)

The local CLI tests replace the native import and await fake authorization/cleanup methods; startup tests replace the server runner, application factory, TLS loader and instance lock. They never authorize real PowerPoint or send keys. Native protocol, snapshot, security and browser tests use injected adapters. `test_powerpoint_render.py` additionally generates a harmless synthetic PDF and exercises real Quartz/ImageIO rendering; descriptor tests inspect native Apple Event data types without sending events. Browser tests start a loopback listener with fake adapters.

Read-only checks against the installed PowerPoint found and corrected native selector byte order and count-event encoding that static dictionary tests did not catch. A subsequent public-adapter scan discovered one presentation with 81 slides in approximately 3.2 seconds; timing varies. No permission prompts, real exports, navigation or input were performed. Complete the manual export/navigation acceptance gate before relying on this integration.

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
node --check src/conty/web/presentations.js
```
