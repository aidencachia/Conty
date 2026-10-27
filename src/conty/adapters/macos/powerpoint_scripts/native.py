"""Fixed Python Apple Event worker; JSON data arrives through argv, never source.

The target is an already-running PID descriptor. Every event is permission
preflighted in THIS sending process and uses never-interact/do-not-prompt flags.
There are no application scripting proxies, System Events, or ScriptingBridge.
This module is import-safe: framework imports and native activity are lazy.

Keyboard preflight cannot make focus/lifetime checks atomic with event posting,
nor acknowledge delivery. Each key pair is preflighted, then its key-up is
attempted unconditionally to the captured PID, even after focus loss or a failed
key-down. No cleanup resolves a replacement application or starts an extra
Escape/Return key pair.
An exiting process (including the tiny PID-reuse race) or a killed/hung helper
can still prevent a safe/confirmed release; uncertain effects poison control.
Number jumps additionally require a nonmodal AX container focus in the focused
window and positively closed menus. No AX/Apple Event window-ID equivalence is
assumed. Unsupported AX hierarchies fail closed; manual hardware acceptance of
PowerPoint's actual slideshow focus tree is still required before production.

On versions without window IDs, identity is a unique full saved path within the
captured PID/lifetime. Root indices are freshly resolved transport selectors,
never persistent identities. Membership and full paths are checked before/after
effects, but those checks cannot be atomic with Apple Events or key posting.
Do not open, close, reorder or Save As presentations during an operation. Reopening
the same saved file within that process lifetime is indistinguishable.
"""

from __future__ import annotations

import ctypes
import json
import signal
import sys
import time
from pathlib import Path

from conty.adapters.base import AdapterError
from conty.adapters.macos.powerpoint import (
    MAX_CACHED_DECKS_BYTES,
    MAX_DECKS,
    MAX_PDF,
    MAX_SLIDES,
    _error,
    _handle,
    _slides,
)

BUNDLE = "com.microsoft.Powerpoint"
NO_PROMPT = 0x00080013  # wait reply | never interact | do not prompt for user consent
PRINT_SETTINGS = ("POpT", "SRgT", "POpH")
PRINT_SLIDES = 0x00D80001
PRINT_ALL = 0x00F70001
SHOW_ALL = 0x00D60001
SAVE_PDF = 0x00CC000E
TITLE_TYPES = {0x00DB0001, 0x00DB0003, 0x00DB0005}


def fourcc(value):
    return int.from_bytes(value.encode("mac_roman"), "big")


class _AEDesc(ctypes.Structure):
    _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]


class AppleEvents:
    """Small raw-AE bridge. Only this class touches native frameworks."""

    def __init__(self, *, timeout=25):
        from conty.adapters.macos.keyboard import _Native

        self.keyboard = _Native()
        self.apps = self.keyboard.apps
        self.D = self.apps.NSAppleEventDescriptor
        apps = [
            app
            for app in self.apps.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
                BUNDLE
            )
            if not app.isTerminated()
        ]
        if not apps:
            raise AdapterError(
                "target_unavailable", "PowerPoint is not running", details={"not_running": True}
            )
        if len(apps) != 1:
            raise _error("target_unavailable")
        self.app = apps[0]
        self.pid = self.keyboard.pid(self.app)
        self.lifetime = self._lifetime(self.app)
        self.target = self.D.descriptorWithProcessIdentifier_(self.pid)
        self.root = self.D.nullDescriptor()
        self.deadline = time.monotonic() + timeout
        self.effects = False
        self.unknown = False
        self.carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        self.carbon.AECreateDesc.argtypes = [
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.POINTER(_AEDesc),
        ]
        self.carbon.AECreateDesc.restype = ctypes.c_int32
        self.carbon.AEDisposeDesc.argtypes = [ctypes.POINTER(_AEDesc)]
        self.carbon.AEDisposeDesc.restype = ctypes.c_int32
        self.carbon.AEDeterminePermissionToAutomateTarget.argtypes = [
            ctypes.POINTER(_AEDesc),
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_bool,
        ]
        self.carbon.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32

    @staticmethod
    def _lifetime(app):
        date = app.launchDate()
        if date is None:
            raise _error("target_unavailable")
        return format(float(date.timeIntervalSince1970()), ".9f")

    def guard(self):
        if time.monotonic() >= self.deadline:
            self.fail(-1712)
        if not self.keyboard.is_running(self.app):
            raise _error("target_unavailable")
        current = self.apps.NSRunningApplication.runningApplicationWithProcessIdentifier_(self.pid)
        if (
            current is None
            or current.isTerminated()
            or str(current.bundleIdentifier()) != BUNDLE
            or self._lifetime(current) != self.lifetime
            or self.keyboard.pid(current) != self.pid
        ):
            raise _error("target_unavailable")

    def fail(self, status):
        if status in (-1743, -1744):
            code = "permission_denied"
        elif status in (-600, -609, -1728):
            code = "target_unavailable"
        elif status == -1712:
            code = "timed_out"
            self.unknown = True
        else:
            code = "execution_failed"
        error = _error(code, effects=self.effects)
        error.details["native_code"] = status
        raise error

    def permission(self, suite, event_id, *, ask=False):
        self.guard()
        # Build a C AEDesc rather than assuming PyObjC's opaque aeDesc pointer
        # can be passed to ctypes. Both descriptors address exactly the same PID.
        target = _AEDesc()
        pid = ctypes.c_int32(self.pid)
        status = self.carbon.AECreateDesc(
            fourcc("kpid"), ctypes.byref(pid), ctypes.sizeof(pid), ctypes.byref(target)
        )
        if status:
            self.fail(status)
        try:
            status = self.carbon.AEDeterminePermissionToAutomateTarget(
                ctypes.byref(target), fourcc(suite), fourcc(event_id), ask
            )
            if status:
                self.fail(status)
        finally:
            self.carbon.AEDisposeDesc(ctypes.byref(target))
        self.guard()

    def enum(self, value):
        return self.D.descriptorWithEnumCode_(value)

    def integer(self, value):
        return self.D.descriptorWithInt32_(value)

    def boolean(self, value):
        return self.D.descriptorWithBoolean_(value)

    def specifier(self, desired, form, key, container=None):
        record = self.D.recordDescriptor()
        fields = {
            "want": self.D.descriptorWithTypeCode_(fourcc(desired)),
            "form": self.enum(fourcc(form)),
            "seld": key,
            "from": self.root if container is None else container,
        }
        for name, value in fields.items():
            record.setDescriptor_forKeyword_(value, fourcc(name))
        result = record.coerceToDescriptorType_(fourcc("obj "))
        if result is None:
            raise _error("capability_unavailable")
        return result

    def prop(self, code, container):
        return self.specifier(
            "prop", "prop", self.D.descriptorWithTypeCode_(fourcc(code)), container
        )

    def all(self, cls, container=None):
        # Cocoa stores OSType payloads in native byte order. Preserve its enum
        # bytes but supply typeAbsoluteOrdinal (enum -> abso coercion fails).
        selector = self.D.descriptorWithDescriptorType_bytes_length_(
            fourcc("abso"), self.enum(fourcc("all ")).data(), 4
        )
        if selector is None:
            raise _error("capability_unavailable")
        return self.specifier(cls, "indx", selector, container)

    def at(self, cls, index, container=None):
        return self.specifier(cls, "indx", self.integer(index), container)

    def by_id(self, cls, identity):
        return self.specifier(cls, "ID  ", self.integer(identity))

    def send(self, suite, event_id, direct, parameters=None, *, mutation=False):
        self.permission(suite, event_id)  # ask=False, in the actual sending process
        event = self.D.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_(
            fourcc(suite), fourcc(event_id), self.target, -1, 0
        )
        event.setParamDescriptor_forKeyword_(direct, fourcc("----"))
        for name, value in (parameters or {}).items():
            event.setParamDescriptor_forKeyword_(value, fourcc(name))
        self.guard()  # No application-by-name resolution between this check and send.
        if mutation:
            self.effects = True
        reply, error = event.sendEventWithOptions_timeout_error_(
            NO_PROMPT, min(35, max(0.001, self.deadline - time.monotonic())), None
        )
        if reply is None:
            if error is not None:
                self.fail(int(error.code()))
            self.unknown = True
            raise _error("execution_failed", effects=self.effects)
        failure = reply.paramDescriptorForKeyword_(fourcc("errn"))
        if failure is not None and failure.int32Value():
            self.fail(int(failure.int32Value()))
        return reply.paramDescriptorForKeyword_(fourcc("----"))

    def get(self, specifier):
        result = self.send("core", "getd", specifier)
        if result is None:
            raise _error("execution_failed")
        return result

    def value(self, descriptor):
        kind = int(descriptor.descriptorType())
        if kind == fourcc("list"):
            return [
                self.value(descriptor.descriptorAtIndex_(i))
                for i in range(1, descriptor.numberOfItems() + 1)
            ]
        if kind in (fourcc("bool"), fourcc("true"), fourcc("fals")):
            return bool(descriptor.booleanValue())
        if kind == fourcc("enum"):
            return int(descriptor.enumCodeValue())
        if kind == fourcc("type"):
            return int(descriptor.typeCodeValue())
        if kind in (fourcc("long"), fourcc("shor")):
            return int(descriptor.int32Value())
        if kind == fourcc("comp"):
            data = bytes(descriptor.data())
            if len(data) != 8:
                raise _error("execution_failed")
            return int.from_bytes(data, sys.byteorder, signed=True)
        text = descriptor.stringValue()
        if text is None:
            raise _error("execution_failed")
        return str(text)

    def read(self, code, container):
        return self.value(self.get(self.prop(code, container)))

    def count(self, cls, container=None):
        return self.value(
            self.send(
                "core",
                "cnte",
                self.root if container is None else container,
                {"kocl": self.D.descriptorWithTypeCode_(fourcc(cls))},
            )
        )

    def references(self, cls, container=None, *, limit=64):
        count = self.count(cls, container)
        if type(count) is not int or not 0 <= count <= limit:
            raise _error("limit_exceeded")
        if not count:
            return []
        result = self.get(self.all(cls, container))
        if result.descriptorType() != fourcc("list") or result.numberOfItems() != count:
            raise _error("target_unavailable")
        return [result.descriptorAtIndex_(i) for i in range(1, count + 1)]

    def reference_key(self, reference, cls, form):
        if reference.descriptorType() != fourcc("obj "):
            return None
        fields = {
            key: reference.descriptorForKeyword_(fourcc(key))
            for key in ("want", "form", "seld", "from")
        }
        if any(value is None for value in fields.values()):
            return None
        if (
            fields["want"].descriptorType() != fourcc("type")
            or fields["want"].typeCodeValue() != fourcc(cls)
            or fields["form"].descriptorType() != fourcc("enum")
            or fields["form"].enumCodeValue() != fourcc(form)
            or fields["from"].descriptorType() != fourcc("null")
        ):
            return None
        key = fields["seld"]
        if form == "ID  " and key.descriptorType() not in (fourcc("long"), fourcc("shor")):
            return None
        if form == "name" and key.descriptorType() not in (fourcc("utxt"), fourcc("TEXT")):
            return None
        return self.value(key)

    def presentation_paths(self):
        count = self.count("pptP")
        if type(count) is not int or not 0 <= count <= 64:
            raise _error("limit_exceeded")
        if not count:
            return []
        paths = self.read("fNam", self.all("pptP"))
        if not isinstance(paths, list) or len(paths) != count:
            raise _error("target_unavailable")
        return [path if isinstance(path, str) else None for path in paths]

    def ids(self, cls, container=None, *, limit=64):
        count = self.count(cls, container)
        if type(count) is not int or not 0 <= count <= limit:
            raise _error("limit_exceeded")
        if not count:
            return []
        # Some versions return stable unique-ID selectors even when the
        # inherited ID property is absent. Prefer those actual object references.
        refs = self.references(cls, container, limit=limit)
        ids = [self.reference_key(ref, cls, "ID  ") for ref in refs]
        if not all(type(identity) is int for identity in ids):
            try:
                descriptor = self.get(self.prop("ID  ", self.all(cls, container)))
                items = (
                    [descriptor.descriptorAtIndex_(i) for i in range(1, count + 1)]
                    if descriptor.descriptorType() == fourcc("list")
                    and descriptor.numberOfItems() == count
                    else [descriptor]
                )
                if any(
                    item is not None
                    and item.descriptorType() == fourcc("type")
                    and item.typeCodeValue() == fourcc("msng")
                    for item in items
                ):
                    raise _error("unsupported_identity")
                ids = self.value(descriptor)
            except AdapterError as exc:
                if exc.details.get("native_code") in (-1728, -1708):
                    raise _error("unsupported_identity") from None
                raise
        if (
            not isinstance(ids, list)
            or len(ids) != count
            or any(type(i) is not int for i in ids)
            or len(set(ids)) != count
        ):
            raise _error("target_unavailable")
        return ids

    def set(self, specifier, value):
        self.send("core", "setd", specifier, {"data": value}, mutation=True)

    def export(self, presentation, path):
        url = self.apps.NSURL.fileURLWithPath_(str(path))
        self.send(
            "core",
            "save",
            presentation,
            {
                "kfil": self.D.descriptorWithFileURL_(url),
                "fltp": self.enum(SAVE_PDF),
            },
            mutation=True,
        )

    def jump_focus_safe(self, pid):
        from conty.adapters.macos.powerpoint_scripts.focus import JumpFocusGuard

        self.guard()
        if pid != self.pid:
            return False
        safe = JumpFocusGuard(self.keyboard.ax, deadline=self.deadline).safe(pid)
        self.guard()
        return safe

    def post(self, event):
        self.effects = True  # Posting can raise after dispatch; never claim no effects.
        self.keyboard.post(self.pid, event)


class DeckSession:
    """Identity and transaction logic, independently testable with a fake bridge."""

    def __init__(self, bridge):
        self.b = bridge

    def resolve(self, handle, *, allow_renamed=False):
        b = self.b
        b.guard()
        if b.pid != handle["pid"] or b.lifetime != handle["lifetime"]:
            raise _error("target_unavailable")
        if handle.get("identity_kind") == "saved_path":
            return self.resolve_saved(handle)
        ids = b.ids("pDWn")
        identity = handle["window_id"]
        if ids.count(identity) != 1:
            raise _error("target_unavailable")
        window = b.by_id("pDWn", identity)
        # This is a PROPERTY OF THE ID-ANCHORED WINDOW, not an indexed/active
        # presentation returned by PowerPoint which could later retarget.
        presentation = b.prop("pptP", window)
        members = b.ids("pDWn", presentation)
        if members.count(identity) != 1 or not set(members).issubset(ids):
            raise _error("target_unavailable")
        fullname = b.read("fNam", presentation)
        if fullname != handle["fullname"] and not allow_renamed:
            raise _error("target_unavailable")
        return presentation, sorted(members)

    def resolve_saved(self, handle):
        b = self.b
        fullname = handle["fullname"]
        if not Path(fullname).is_absolute():
            raise _error("unsupported_identity")
        paths = b.presentation_paths()
        matches = [index for index, path in enumerate(paths, 1) if path == fullname]
        if len(matches) > 1:
            raise _error("unsupported_identity")
        if not matches:
            raise _error("target_unavailable")
        index = matches[0]
        presentation = b.at("pptP", index)
        signature = {"kind": "saved_path", "paths": paths, "index": index, "fullname": fullname}
        # This root index is short-lived transport, not identity. In particular,
        # never use name selectors or indices within filtered collections: this
        # PowerPoint build can ignore their path/container and target another file.
        self.verify_saved(presentation, signature)
        return presentation, signature

    def verify_saved(self, presentation, signature):
        b = self.b
        if b.read("fNam", presentation) != signature["fullname"]:
            raise _error("target_unavailable")
        b.guard()

    def effect_target(self, handle, signature):
        presentation, current = self.resolve(handle)
        if current != signature:
            raise _error("target_unavailable")
        return presentation

    def saved_show(self, presentation, signature):
        b = self.b
        count = b.count("pSSw")
        if type(count) is not int or not 0 <= count <= MAX_DECKS:
            raise _error("limit_exceeded")
        owners = [b.read("fNam", b.prop("pptP", b.at("pSSw", i))) for i in range(1, count + 1)]
        matches = [i for i, path in enumerate(owners, 1) if path == signature["fullname"]]
        if len(matches) > 1:
            raise _error("target_unavailable")
        if not matches:
            return None, None
        selected = b.prop("pSSw", presentation)
        if b.read("fNam", b.prop("pptP", selected)) != signature["fullname"]:
            raise _error("target_unavailable")
        self.verify_saved(presentation, signature)
        # A relationship/membership token, NOT a persistent slideshow/window ID.
        token = {"kind": "saved_path_show", "owners": owners, "index": matches[0]}
        return token, selected

    def title(self, slide, number):
        b = self.b
        try:
            count = b.count("cD05", slide)
            if not 0 < count <= 32:
                return f"Slide {number}"
            kinds = b.read("PHtY", b.all("cD05", slide))
            if not isinstance(kinds, list) or len(kinds) != count:
                return f"Slide {number}"
            for index, kind in enumerate(kinds, 1):
                if kind in TITLE_TYPES:
                    shape = b.at("cD05", index, slide)
                    text = b.read("pTdT", b.prop("TObj", b.prop("TxTF", shape)))
                    return " ".join(str(text).split())[:256] or f"Slide {number}"
        except AdapterError as exc:
            # Missing title/unsupported placeholder collection is not an identity
            # failure. Permission, native timeouts and all other errors propagate.
            if exc.details.get("native_code") not in (-1728, -1708):
                raise
        return f"Slide {number}"

    def manifest(self, presentation, *, titles=False):
        b = self.b
        count = b.count("pSLD", presentation)
        if type(count) is not int or not 0 <= count <= MAX_SLIDES:
            raise _error("limit_exceeded")
        if not count:
            return []
        slides = b.all("pSLD", presentation)
        ids = b.read("SlID", slides)
        indices = b.read("SIdX", slides)
        numbers = b.read("SldN", slides)
        hidden = b.read("Thid", b.prop("PSsT", slides))
        if isinstance(hidden, list) and len(hidden) == count:
            # Current PowerPoint resolves the nested bulk property to missing
            # values, although individual transitions expose real booleans.
            # Fall back only for that observed result, never invent visibility.
            hidden = [
                b.read("Thid", b.prop("PSsT", b.at("pSLD", index, presentation)))
                if type(value) is int and value == fourcc("msng")
                else value
                for index, value in enumerate(hidden, 1)
            ]
        if (
            any(not isinstance(c, list) or len(c) != count for c in (ids, indices, numbers, hidden))
            or indices != list(range(1, count + 1))
            or len(set(ids)) != count
            or any(type(v) is not int or v <= 0 for v in ids)
            or any(type(v) is not int for v in numbers)
            or any(type(v) is not bool for v in hidden)
        ):
            raise _error("stale_snapshot")
        result = []
        for index, (identity, number, invisible) in enumerate(zip(ids, numbers, hidden), 1):
            title = (
                self.title(b.at("pSLD", index, presentation), number)
                if titles
                else f"Slide {number}"
            )
            result.append(
                {"id": str(identity), "number": number, "title": title, "hidden": invisible}
            )
        return result

    def selected_show(self, presentation, members):
        if isinstance(members, dict) and members.get("kind") == "saved_path":
            return self.saved_show(presentation, members)
        b = self.b
        matches = []
        for identity in b.ids("pSSw", limit=MAX_DECKS):
            show = b.by_id("pSSw", identity)
            owner = b.prop("pptP", show)
            # Match the presentation relationship via document-window identities,
            # never via filename (two unsaved presentations can share that name).
            if sorted(b.ids("pDWn", owner)) == members:
                if b.read("ID  ", b.prop("pSSw", presentation)) != identity:
                    raise _error("target_unavailable")
                matches.append((identity, show))
        if len(matches) > 1:
            raise _error("target_unavailable")
        return matches[0] if matches else (None, None)

    def cached_manifest(self, presentation, slides):
        """Reuse static preview metadata only with freshly verified order/numbering."""
        observed = self.navigation_manifest(presentation)
        if [(s["id"], s["number"]) for s in slides] != [(s["id"], s["number"]) for s in observed]:
            raise _error("stale_snapshot")
        return [dict(slide) for slide in slides]

    def snapshot(self, handle, *, titles=False, verify=True, snapshot_slides=None):
        b = self.b
        presentation, members = self.resolve(handle)
        manifest = (
            self.manifest(presentation, titles=titles)
            if snapshot_slides is None
            else self.cached_manifest(presentation, snapshot_slides)
        )
        saved_path = handle.get("identity_kind") == "saved_path"
        identity, show = self.selected_show(presentation, members)
        current, active = None, False
        if show is not None:
            view = b.prop("PSSv", show)
            current = str(b.read("SlID", b.prop("pSLD", view)))
            if current not in {s["id"] for s in manifest}:
                raise _error("stale_snapshot")
            active = bool(b.read("Actv", show)) and b.keyboard.frontmost(b.pid)
        # Re-read lightweight metadata and identity to reject concurrent edits.
        verified_presentation, verified_members = self.resolve(handle)
        if saved_path and verified_members != members:
            raise _error("target_unavailable")
        if verify:
            verified_manifest = (
                self.manifest(verified_presentation, titles=False)
                if snapshot_slides is None
                else self.cached_manifest(verified_presentation, snapshot_slides)
            )
            if self.signature(manifest) != self.signature(verified_manifest):
                raise _error("stale_snapshot")
            if saved_path:
                self.effect_target(handle, members)
        return {
            "slides": manifest,
            "view": "read" if show is not None else "edit",
            "current_slide_id": current,
            "can_control": active,
            "show_id": None if saved_path else identity,
            **({"show_token": identity, "target_signature": members} if saved_path else {}),
        }

    @staticmethod
    def signature(slides):
        return [(s["id"], s["number"], s["hidden"]) for s in slides]

    def discovery_snapshot(self, handle, cached_decks, *, verify=True):
        # Exact private identity, including the original full path. Never resolve
        # a cached handle as a replacement for the freshly discovered target.
        for entry in cached_decks or []:
            if not isinstance(entry, dict) or entry.get("handle") != handle:
                continue
            slides = _slides(entry.get("slides"))
            try:
                return self.snapshot(handle, verify=verify, snapshot_slides=slides)
            except AdapterError as exc:
                if exc.code != "stale_snapshot":
                    raise
                break
        return self.snapshot(handle, verify=verify)

    def discover(self, cached_decks=None):
        if cached_decks is not None and (
            not isinstance(cached_decks, list)
            or len(cached_decks) > MAX_DECKS
            or len(json.dumps(cached_decks, ensure_ascii=True, separators=(",", ":")).encode())
            > MAX_CACHED_DECKS_BYTES
        ):
            raise _error("limit_exceeded")
        b = self.b
        result, seen = [], set()
        try:
            identities = b.ids("pDWn")
        except AdapterError as exc:
            if exc.code != "unsupported_identity":
                raise
            return self.discover_saved(cached_decks)
        for identity in sorted(identities):
            presentation = b.prop("pptP", b.by_id("pDWn", identity))
            members = tuple(sorted(b.ids("pDWn", presentation)))
            if identity not in members:
                raise _error("target_unavailable")
            if members in seen:
                continue
            seen.add(members)
            if len(result) >= MAX_DECKS:
                raise _error("limit_exceeded")
            handle = {
                "pid": b.pid,
                "lifetime": b.lifetime,
                "window_id": identity,
                "fullname": b.read("fNam", presentation),
            }
            handle = _handle(handle)
            result.append(
                {
                    "handle": handle,
                    "title": str(b.read("pnam", presentation))[:256],
                    **self.discovery_snapshot(handle, cached_decks),
                }
            )
        return result

    def discover_saved(self, cached_decks=None):
        b = self.b
        paths = b.presentation_paths()
        candidates = [
            path
            for path in paths
            if isinstance(path, str) and Path(path).is_absolute() and paths.count(path) == 1
        ]
        if len(candidates) > MAX_DECKS:
            raise _error("limit_exceeded")
        names = b.read("pnam", b.all("pptP")) if paths else []
        if not isinstance(names, list) or len(names) != len(paths):
            raise _error("target_unavailable")
        result = []
        for fullname in candidates:
            handle = _handle(
                {
                    "pid": b.pid,
                    "lifetime": b.lifetime,
                    "identity_kind": "saved_path",
                    "fullname": fullname,
                }
            )
            result.append(
                {
                    "handle": handle,
                    "title": str(names[paths.index(fullname)])[:256],
                    **self.discovery_snapshot(handle, cached_decks, verify=False),
                }
            )
        if not result:
            raise _error("unsupported_identity")
        return result

    def export(self, handle, path):
        if handle.get("identity_kind") == "saved_path":
            return self.export_saved(handle, path)
        b = self.b
        before = self.snapshot(handle)
        if before["view"] != "edit":
            raise _error("invalid_action")
        if not before["slides"]:
            raise _error("export_failed")
        presentation, original_members = self.resolve(handle)
        options = b.prop("PtPo", presentation)
        saved = {code: b.get(b.prop(code, options)) for code in PRINT_SETTINGS}
        changed = False
        deadline = b.deadline
        try:
            # Set all three even if the deck currently prints handouts, notes,
            # a custom range, or hides slides. PDF pages then map to deck order.
            for code, value in zip(
                PRINT_SETTINGS, (b.enum(PRINT_SLIDES), b.enum(PRINT_ALL), b.boolean(True))
            ):
                self.resolve(handle)
                changed = True  # set may fail after its event was delivered
                b.set(b.prop(code, options), value)
            for code, value in zip(PRINT_SETTINGS, (PRINT_SLIDES, PRINT_ALL, True)):
                if b.read(code, options) != value:
                    raise _error("export_failed")
            verified = self.snapshot(handle)
            if verified["view"] != "edit" or verified["slides"] != before["slides"]:
                raise _error("stale_snapshot")
            b.export(presentation, path)
        finally:
            if changed:
                # Restoration gets a separate bounded grace period. Each option
                # is attempted even if an earlier restore fails. A killed/hung
                # worker cannot promise restoration; the parent poisons control.
                b.deadline = time.monotonic() + 4
                failures = False
                for code in reversed(PRINT_SETTINGS):
                    try:
                        # A failed Save As may rename the same presentation.
                        # Still restore the captured ID-anchored document, never
                        # a replacement filename match. The normal postflight
                        # rejects any rename; it is NOT repaired/force-saved.
                        _, members = self.resolve(handle, allow_renamed=True)
                        if members != original_members:
                            raise _error("target_unavailable")
                        b.set(b.prop(code, options), saved[code])
                        if b.read(code, options) != b.value(saved[code]):
                            raise _error("export_failed")
                    except Exception:
                        failures = True
                if failures:
                    b.unknown = True
                    raise _error("executor_unavailable", effects=True)
                b.deadline = deadline
        after = self.snapshot(handle)
        if after["view"] != "edit" or before["slides"] != after["slides"]:
            raise _error("stale_snapshot", effects=True)
        if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= MAX_PDF:
            raise _error("export_failed", effects=True)
        return after

    def export_saved(self, handle, path):
        b = self.b
        before = self.snapshot(handle)
        if before["view"] != "edit":
            raise _error("invalid_action")
        if not before["slides"]:
            raise _error("export_failed")
        signature = before["target_signature"]
        presentation = self.effect_target(handle, signature)
        options = b.prop("PtPo", presentation)
        saved = {code: b.get(b.prop(code, options)) for code in PRINT_SETTINGS}
        self.effect_target(handle, signature)
        changed = False
        deadline = b.deadline
        try:
            for code, value in zip(
                PRINT_SETTINGS, (b.enum(PRINT_SLIDES), b.enum(PRINT_ALL), b.boolean(True))
            ):
                presentation = self.effect_target(handle, signature)
                changed = True
                b.set(b.prop(code, b.prop("PtPo", presentation)), value)
                self.effect_target(handle, signature)
            for code, value in zip(PRINT_SETTINGS, (PRINT_SLIDES, PRINT_ALL, True)):
                presentation = self.effect_target(handle, signature)
                if b.read(code, b.prop("PtPo", presentation)) != value:
                    raise _error("export_failed")
            verified = self.snapshot(handle)
            if verified["view"] != "edit" or verified["slides"] != before["slides"]:
                raise _error("stale_snapshot")
            presentation = self.effect_target(handle, signature)
            b.export(presentation, path)
            self.effect_target(handle, signature)
        finally:
            if changed:
                b.deadline = time.monotonic() + 4
                failures = False
                for code in reversed(PRINT_SETTINGS):
                    try:
                        # Unlike ID handles, saved-path handles CANNOT follow a
                        # renamed presentation. Refuse restoration rather than
                        # guessing another file or reusing a captured index.
                        presentation = self.effect_target(handle, signature)
                        options = b.prop("PtPo", presentation)
                        b.set(b.prop(code, options), saved[code])
                        presentation = self.effect_target(handle, signature)
                        if b.read(code, b.prop("PtPo", presentation)) != b.value(saved[code]):
                            raise _error("export_failed")
                    except Exception:
                        failures = True
                if failures:
                    b.unknown = True
                    raise _error("executor_unavailable", effects=True)
                b.deadline = deadline
        self.effect_target(handle, signature)
        after = self.snapshot(handle)
        if (
            after["target_signature"] != signature
            or after["view"] != "edit"
            or after["slides"] != before["slides"]
        ):
            raise _error("stale_snapshot", effects=True)
        if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= MAX_PDF:
            raise _error("export_failed", effects=True)
        return after

    def jump_allowed(self, presentation, show, manifest):
        b = self.b
        settings = b.prop("SSSt", presentation)
        if (
            b.read("SinS", b.prop("PSSv", show))
            or b.read("SRgT", settings) != SHOW_ALL
            or b.read("PsF#", b.prop("pPgS", presentation)) != 1
            or any(slide["number"] != i for i, slide in enumerate(manifest, 1))
        ):
            raise _error("unsupported_show")

    def navigation_manifest(self, presentation):
        """Bulk numbering/order checks; never traverse each slide's transition per key."""
        b = self.b
        count = b.count("pSLD", presentation)
        if type(count) is not int or not 0 <= count <= MAX_SLIDES:
            raise _error("limit_exceeded")
        if not count:
            return []
        slides = b.all("pSLD", presentation)
        identities = b.read("SlID", slides)
        indices = b.read("SIdX", slides)
        numbers = b.read("SldN", slides)
        if (
            any(
                not isinstance(values, list) or len(values) != count
                for values in (identities, indices, numbers)
            )
            or indices != list(range(1, count + 1))
            or any(type(value) is not int or value <= 0 for value in identities)
            or len(set(identities)) != count
            or any(type(value) is not int for value in numbers)
        ):
            raise _error("stale_snapshot")
        return [
            {"id": str(identity), "number": number} for identity, number in zip(identities, numbers)
        ]

    def command_guard(
        self, handle, show_id, expected, *, jump=False, target_signature=None, expected_numbers=None
    ):
        b = self.b
        b.guard()
        presentation, members = self.resolve(handle)
        if target_signature is not None and members != target_signature:
            raise _error("target_unavailable")
        identity, show = self.selected_show(presentation, members)
        if identity is None or identity != show_id:
            raise _error("target_unavailable")
        if not b.read("Actv", show) or not b.keyboard.frontmost(b.pid):
            raise _error("target_not_frontmost")
        manifest = self.navigation_manifest(presentation)
        if [s["id"] for s in manifest] != expected or (
            expected_numbers is not None and [s["number"] for s in manifest] != expected_numbers
        ):
            raise _error("stale_snapshot")
        if jump:
            self.jump_allowed(presentation, show, manifest)
            if not b.keyboard.trusted():
                raise _error("permission_denied")
        # Focus and lifetime are checked again after all potentially slow reads.
        if target_signature is None:
            if not b.read("Actv", show) or not b.keyboard.frontmost(b.pid):
                raise _error("target_not_frontmost")
        if target_signature is not None:
            presentation = self.effect_target(handle, target_signature)
            identity, show = self.selected_show(presentation, target_signature)
            if identity != show_id:
                raise _error("target_unavailable")
            if not b.read("Actv", show) or not b.keyboard.frontmost(b.pid):
                raise _error("target_not_frontmost")
            self.verify_saved(presentation, target_signature)
        if jump:
            self.jump_input_guard(handle)
        b.guard()
        return show

    def jump_input_guard(self, handle):
        """Recheck input safety without round trips through PowerPoint's Apple Events."""
        b = self.b
        b.guard()
        if b.pid != handle["pid"] or b.lifetime != handle["lifetime"]:
            raise _error("target_unavailable")
        if not b.jump_focus_safe(b.pid):
            raise _error("unsafe_focus")
        if not b.keyboard.trusted():
            raise _error("permission_denied")
        if not b.keyboard.frontmost(b.pid):
            raise _error("target_not_frontmost")
        b.guard()

    def command(self, handle, operation, slide_id=None, expected_slides=None, snapshot_slides=None):
        b = self.b
        cached_metadata = snapshot_slides is not None
        if cached_metadata:
            snapshot_slides = _slides(snapshot_slides)
            ids = [s["id"] for s in snapshot_slides]
            if expected_slides != ids:
                raise _error("stale_snapshot")
            # Capture only target identity here. The per-dispatch guard freshly
            # validates mapping, numbering and focus; an initial full snapshot
            # repeats those reads and fetches a current position we never use.
            presentation, members = self.resolve(handle)
            show_token, _ = self.selected_show(presentation, members)
            target_signature = members if handle.get("identity_kind") == "saved_path" else None
        else:
            # ID-only callers still need the full metadata fallback.
            before = self.snapshot(handle, verify=False)
            snapshot_slides = before["slides"]
            ids = [slide["id"] for slide in snapshot_slides]
            if expected_slides is not None and expected_slides != ids:
                raise _error("stale_snapshot")
            show_token = before.get("show_token", before["show_id"])
            target_signature = before.get("target_signature")
        guard_options = (
            {"target_signature": target_signature} if target_signature is not None else {}
        )
        if cached_metadata:
            guard_options["expected_numbers"] = [s["number"] for s in snapshot_slides]
        if show_token is None:
            raise _error("target_not_frontmost")
        if operation == "show_slide":
            if expected_slides is None or slide_id not in ids:
                raise _error("invalid_action")
            number = ids.index(slide_id) + 1
            # Build events before the final full preflight, then keep the entire
            # serialized sequence pinned to the captured process. Deck identity,
            # order and show settings are intentionally not queried between keys:
            # concurrent deck changes are detected only by the final snapshot.
            pairs = [b.keyboard.events(key, ()) for key in [*str(number), "return"]]
            self.command_guard(handle, show_token, ids, jump=True, **guard_options)
            for index, (down, up) in enumerate(pairs):
                if index:
                    self.jump_input_guard(handle)
                try:
                    b.post(down)
                finally:
                    # Match KeyboardAdapter: release to the captured PID even
                    # if down raised, focus/permission changed, or time expired.
                    # A slow guard here could leave the synthetic key held.
                    b.post(up)
            # Detect uncertainty during the final pair too, but only AFTER its
            # release was attempted. Earlier losses abort before the next pair.
            self.jump_input_guard(handle)
            confirmation = "dispatch_only"
        elif operation in ("next_slide", "previous_slide"):
            show = self.command_guard(handle, show_token, ids, **guard_options)
            b.send(
                "sPPT",
                "SnxS" if operation == "next_slide" else "SpvS",
                b.prop("PSSv", show),
                mutation=True,
            )
            confirmation = "dispatch_only"
        else:
            raise _error("invalid_action")
        # This snapshot freshly resolves both presentation and show. Compare its
        # identity below instead of resolving them a second time just beforehand.
        after = self.snapshot(handle, verify=False, snapshot_slides=snapshot_slides)
        if after.get("show_token", after["show_id"]) != show_token or (
            target_signature is not None and after["target_signature"] != target_signature
        ):
            raise _error("target_unavailable", effects=True)
        if not after["can_control"]:
            raise _error("target_not_frontmost", effects=True)
        if [slide["id"] for slide in after["slides"]] != ids:
            raise _error("stale_snapshot", effects=True)
        # PowerPoint may process posted keys asynchronously. Never claim that
        # the observed current slide is an acknowledgement of key delivery.
        return {**after, "effect_confirmation": confirmation}


def execute(request, *, bridge_factory=AppleEvents):
    operation = request.get("operation")
    b = None
    try:
        try:
            b = bridge_factory(timeout=40 if operation == "export" else 25)
        except AdapterError as exc:
            if operation == "discover" and exc.details.get("not_running") is True:
                return {"ok": True, "result": []}
            raise
        if operation == "authorize":
            for suite, event in (
                ("core", "getd"),
                ("core", "cnte"),
                ("core", "setd"),
                ("core", "save"),
                ("sPPT", "SnxS"),
                ("sPPT", "SpvS"),
            ):
                b.permission(suite, event, ask=True)
            result = {"available": True, "permission": "automation", "prompt_requested": True}
        else:
            session = DeckSession(b)
            if operation == "discover":
                result = session.discover(request.get("cached_decks"))
            else:
                handle = _handle(request.get("handle"))
                if operation == "observe":
                    result = session.snapshot(handle)
                elif operation == "export":
                    result = session.export(handle, Path(request["pdf"]))
                else:
                    result = session.command(
                        handle,
                        operation,
                        request.get("slide_id"),
                        request.get("expected_slides"),
                        request.get("snapshot_slides"),
                    )
        return {"ok": True, "result": result}
    except AdapterError as exc:
        effects = bool(b and b.effects) or exc.effects_possible
        return {
            "ok": False,
            "code": exc.code,
            "effects_possible": effects,
            "poison": bool(b and b.unknown) or effects or exc.code == "timed_out",
        }
    except BaseException:
        return {
            "ok": False,
            "code": "execution_failed",
            "effects_possible": bool(b and b.effects),
            "poison": bool(b and (b.effects or b.unknown)),
        }


def main():
    # TERM interrupts Python code so export's finally can try restoration. A
    # native hang may delay delivery; the supervisor still has a hard kill bound.
    def terminate(_signal, _frame):
        raise InterruptedError()

    signal.signal(signal.SIGTERM, terminate)
    try:
        result = execute(json.loads(sys.argv[1]))
    except BaseException:
        result = {"ok": False, "code": "execution_failed", "poison": True, "effects_possible": True}
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
