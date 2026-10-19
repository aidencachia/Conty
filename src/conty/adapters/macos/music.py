"""Apple Music only, without activation, launch, global media keys or prompts.

A fixed JXA script runs in /usr/bin/osascript. Inside that SAME process it resolves
an already-running Music PID, calls AEDeterminePermissionToAutomateTarget with
askUserIfNeeded=false for the exact event, then sends to a PID descriptor with
kAEDoNotPromptForUserConsent. This avoids checking Python's TCC identity and then
sending from a different identity. No application-by-name scripting proxy is
created: that could auto-launch a player after a preflight race.

The native bridge is deliberately fail-closed if the API/signature is missing.
An unapproved host needs deliberate local Automation setup; check()/execute()
never request approval. Terminal and packaged-host permission identities can
differ. Real macOS/JXA/TCC acceptance is required; fake tests cannot prove it.

This API is synchronous: run on a dedicated serialized executor, not the HTTP
event loop. A new helper session is terminated, killed if necessary and reaped
on timeout. Cleanup may take up to 1.2 seconds beyond the action deadline and
cannot undo an event already delivered. Unconfirmed cleanup poisons the adapter
instance until local recovery/restart. No playback state is inferred.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from conty.adapters.base import ActionResult, AdapterError, validate_timeout

# Music's fixed 'hook' suite: previous track, playpause, next track.
_EVENTS = {"previous_track": "Prev", "play_pause": "PlPs", "next_track": "Next"}
_ERRORS = {
    "permission_denied": "Grant Automation access to Apple Music for the controlling host in System Settings > Privacy & Security > Automation; no prompt was requested",
    "target_unavailable": "Expected one running Apple Music process; Music was not launched",
    "capability_unavailable": "The native no-prompt Apple Event bridge is unavailable",
    "timed_out": "Apple Music did not reply before the deadline",
    "execution_failed": "Apple Music rejected the Apple Event",
}

# No user-provided text is interpolated into this source. Native four-character
# codes are converted to uint32 values rather than interpreted as script source.
_PREAMBLE = r"""
ObjC.import('AppKit');
ObjC.import('Carbon');
function fourCC(s) {
    return ((s.charCodeAt(0) << 24) | (s.charCodeAt(1) << 16) |
            (s.charCodeAt(2) << 8) | s.charCodeAt(3)) >>> 0;
}
function status(code) {
    if (code === -1743 || code === -1744) return 'permission_denied';
    if (code === -600 || code === -609) return 'target_unavailable';
    if (code === -1712) return 'timed_out';
    return 'execution_failed';
}
function run() {
    var dispatched = false;
    try {
        if (typeof $.AEDeterminePermissionToAutomateTarget === 'undefined') {
            ObjC.bindFunction('AEDeterminePermissionToAutomateTarget',
                ['int', ['void *', 'unsigned int', 'unsigned int', 'bool']]);
        }
        var apps = $.NSRunningApplication.runningApplicationsWithBundleIdentifier('com.apple.Music');
        if (Number(apps.count) !== 1) return 'preflight:target_unavailable';
        var app = apps.objectAtIndex(0);
        if (app.terminated) return 'preflight:target_unavailable';
        var pid = Number(app.processIdentifier);
        if (pid <= 0) return 'preflight:target_unavailable';
        var target = $.NSAppleEventDescriptor.descriptorWithProcessIdentifier(pid);
        var suite = fourCC('hook');
"""
_CHECK_BODY = r"""
        var names = ['Prev', 'PlPs', 'Next'];
        for (var i = 0; i < names.length; i++) {
            var permission = Number($.AEDeterminePermissionToAutomateTarget(
                target.aeDesc, suite, fourCC(names[i]), false));
            if (permission !== 0) return 'preflight:' + status(permission);
        }
        return 'ready';
"""
_SEND_BODY = r"""
        var eventID = fourCC('EVENT_ID');
        var permission = Number($.AEDeterminePermissionToAutomateTarget(
            target.aeDesc, suite, eventID, false));
        if (permission !== 0) return 'preflight:' + status(permission);
        if (app.terminated) return 'preflight:target_unavailable';
        var event = $.NSAppleEventDescriptor.appleEventWithEventClassEventIDTargetDescriptorReturnIDTransactionID(
            suite, eventID, target, -1, 0);
        var error = Ref();
        // kAEWaitReply | kAENeverInteract | kAEDoNotPromptForUserConsent.
        // The parent enforces the caller's deadline, including helper startup.
        dispatched = true;
                var reply = event.sendEventWithOptionsTimeoutError(0x00080013, 5, error);
        if (!reply || reply.isNil()) {
            var nativeError = error[0];
            if (nativeError && !nativeError.isNil()) return status(Number(nativeError.code));
            return 'execution_failed';
        }
        // JXA can wrap Objective-C nil as a truthy object. Check isNil rather
        // than treating any wrapper as a successful reply/error descriptor.
        var failure = reply.paramDescriptorForKeyword(fourCC('errn'));
        if (failure && !failure.isNil() && Number(failure.int32Value) !== 0)
            return status(Number(failure.int32Value));
        return 'dispatched';
"""
_END = r"""
    } catch (e) {
        return (dispatched ? '' : 'preflight:') + 'capability_unavailable';
    }
}
"""
_CHECK_SCRIPT = _PREAMBLE + _CHECK_BODY + _END
_SCRIPTS = {
    name: _PREAMBLE + _SEND_BODY.replace("EVENT_ID", event) + _END
    for name, event in _EVENTS.items()
}


def _stop(process):
    """Terminate the helper's session and drain/reap, including on timeout."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=0.2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=1)


def _run(script: str, timeout: float, *, effects_possible: bool) -> str:
    if sys.platform != "darwin":
        raise AdapterError("capability_unavailable", "Apple Music control requires macOS")
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
        )
    except OSError as exc:
        raise AdapterError(
            "capability_unavailable", "Could not start the Apple Event helper"
        ) from exc
    try:
        stdout, _stderr = process.communicate(timeout=max(0, deadline - time.monotonic()))
    except BaseException as exc:
        try:
            _stop(process)
        except (OSError, subprocess.TimeoutExpired) as cleanup_error:
            raise AdapterError(
                "executor_unavailable",
                "Apple Event helper cleanup could not be confirmed; do not dispatch further actions",
                effects_possible=effects_possible,
            ) from cleanup_error
        if isinstance(exc, subprocess.TimeoutExpired):
            raise AdapterError(
                "timed_out",
                "Apple Event helper timed out; process group terminated and helper reaped; external effects may remain",
                effects_possible=effects_possible,
            ) from exc
        raise
    if process.returncode != 0:
        # Do not leak raw scripting diagnostics or ask callers to parse them.
        raise AdapterError(
            "capability_unavailable",
            "Apple Event helper failed; the native scripting bridge could not be established",
            effects_possible=effects_possible,
        )
    token = stdout.decode("utf-8", errors="replace").strip()
    preflight = token.startswith("preflight:")
    if preflight:
        token = token.removeprefix("preflight:")
    if token in _ERRORS:
        raise AdapterError(
            token, _ERRORS[token], effects_possible=effects_possible and not preflight
        )
    if token not in ("ready", "dispatched"):
        raise AdapterError(
            "execution_failed",
            "Unexpected response from Apple Event helper",
            effects_possible=effects_possible,
        )
    return token


class MusicAdapter:
    def __init__(self) -> None:
        self._unavailable = False

    def _invoke(self, script, timeout, *, effects_possible):
        if self._unavailable:
            raise AdapterError(
                "executor_unavailable",
                "Previous helper cleanup was not confirmed; restart the executor after local recovery",
            )
        try:
            return _run(script, timeout, effects_possible=effects_possible)
        except AdapterError as exc:
            if exc.code == "executor_unavailable":
                self._unavailable = True
            raise

    def check(self) -> dict:
        """Check all three permissions without launching/activating Music."""
        try:
            token = self._invoke(_CHECK_SCRIPT, 5, effects_possible=False)
            if token != "ready":
                raise AdapterError("execution_failed", "Unexpected Apple Music readiness result")
            return {
                "available": True,
                "app_id": "com.apple.Music",
                "permission": "automation",
                "commands": list(_EVENTS),
                "effect_confirmation": "dispatch_only",
            }
        except AdapterError as exc:
            return {"available": False, "code": exc.code, "message": str(exc)}

    def execute(self, command: str, timeout: float = 5) -> ActionResult:
        if not isinstance(command, str) or command not in _SCRIPTS:
            raise AdapterError(
                "invalid_action",
                "Supported Music commands are previous_track, play_pause and next_track",
            )
        validate_timeout(timeout)
        token = self._invoke(_SCRIPTS[command], timeout, effects_possible=True)
        if token != "dispatched":
            raise AdapterError(
                "execution_failed",
                "Music command dispatch was not confirmed",
                effects_possible=True,
            )
        return ActionResult(
            "Apple Music command dispatched",
            details={
                "app_id": "com.apple.Music",
                "command": command,
                "effect_confirmation": "dispatch_only",
                "cleanup": "helper_reaped",
            },
        )
