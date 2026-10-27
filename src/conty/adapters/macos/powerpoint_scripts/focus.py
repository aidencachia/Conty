"""Read-only, PID-scoped AX preflight for slideshow number-jump key pairs.

No system-wide AX element, input, activation, permission request, or inferred
AX-to-PowerPoint window-ID mapping is used. Required/advertised queries must
succeed; optional attributes are omitted only when enumeration proves absence.
The container allowlist is deliberately narrow. Real PowerPoint AX hierarchies
need manual hardware acceptance before enabling number jumps in production.
Even a successful check cannot eliminate the check-to-post focus/lifetime race.
"""

from __future__ import annotations

import time

_CONTAINER_ROLES = {
    "AXWindow",
    "AXGroup",
    "AXSplitGroup",
    "AXLayoutArea",
    "AXScrollArea",
    "AXImage",
    "AXUnknown",
}
_WINDOW_SUBROLES = {"AXStandardWindow", "AXUnknown"}
_CONTAINER_SUBROLES = _WINDOW_SUBROLES | {"AXHostingView"}
_UNSAFE_ROLES = {
    "AXMenu",
    "AXMenuItem",
    "AXMenuBar",
    "AXMenuBarItem",
    "AXSheet",
    "AXDialog",
    "AXSystemDialog",
    "AXPopover",
}
_UNSAFE_SUBROLES = {"AXDialog", "AXSystemDialog", "AXSheet", "AXFloatingWindow"}


class _Unsafe(Exception):
    pass


class JumpFocusGuard:
    def __init__(self, ax, *, deadline):
        self.ax = ax
        self.deadline = min(deadline, time.monotonic() + 2)
        self.pid = None

    def _tick(self):
        if time.monotonic() >= self.deadline:
            raise _Unsafe()

    def _owner(self, element):
        self._tick()
        if element is None:
            raise _Unsafe()
        error, pid = self.ax.AXUIElementGetPid(element, None)
        if error != 0 or pid != self.pid:
            raise _Unsafe()

    def _read(self, element, name):
        self._owner(element)
        error, value = self.ax.AXUIElementCopyAttributeValue(element, name, None)
        if error != 0 or value is None:
            raise _Unsafe()
        return value

    @staticmethod
    def _items(value):
        if isinstance(value, (str, bytes, dict)):
            raise _Unsafe()
        items = list(value)
        if len(items) > 128:
            raise _Unsafe()
        return items

    def _attrs(self, element):
        self._owner(element)
        error, names = self.ax.AXUIElementCopyAttributeNames(element, None)
        if error != 0 or names is None:
            raise _Unsafe()
        names = self._items(names)
        if any(not isinstance(name, str) for name in names):
            raise _Unsafe()
        return set(names)

    def _role(self, element):
        role = self._read(element, "AXRole")
        if not isinstance(role, str) or not role.startswith("AX"):
            raise _Unsafe()
        return role

    def _false(self, element, name):
        value = self._read(element, name)
        if type(value) is not bool or value:
            raise _Unsafe()

    def _same(self, first, second):
        self._tick()
        return bool(self.ax.CFEqual(first, second))

    def _container(self, element, *, window=False):
        if self._role(element) not in (_CONTAINER_ROLES if not window else {"AXWindow"}):
            raise _Unsafe()
        attrs = self._attrs(element)
        if window or "AXSubrole" in attrs:
            # PowerPoint's AppKit canvas ancestors include an AXGroup hosting
            # view. It is a container, not a dialog or an editable control.
            allowed_subroles = _WINDOW_SUBROLES if window else _CONTAINER_SUBROLES
            if self._read(element, "AXSubrole") not in allowed_subroles:
                raise _Unsafe()
        if window or "AXModal" in attrs:
            self._false(element, "AXModal")
        return attrs

    def _window_without_sheets(self, window, attrs):
        if "AXSheets" in attrs and self._items(self._read(window, "AXSheets")):
            raise _Unsafe()
        # Even if AXSheets is absent, attached modal/sheet children must not be
        # ignored. Unfocused ordinary controls are not themselves input targets.
        for child in self._items(self._read(window, "AXChildren")):
            if self._role(child) in _UNSAFE_ROLES:
                raise _Unsafe()
            child_attrs = self._attrs(child)
            if "AXSubrole" in child_attrs:
                if self._read(child, "AXSubrole") in _UNSAFE_SUBROLES:
                    raise _Unsafe()
            if "AXModal" in child_attrs:
                self._false(child, "AXModal")

    def _menus_closed(self, app):
        bar = self._read(app, "AXMenuBar")
        if self._role(bar) != "AXMenuBar":
            raise _Unsafe()
        attrs = self._attrs(bar)
        bar_selection_known = "AXSelectedChildren" in attrs
        if bar_selection_known and self._items(self._read(bar, "AXSelectedChildren")):
            raise _Unsafe()
        for item in self._items(self._read(bar, "AXChildren")):
            if self._role(item) != "AXMenuBarItem":
                raise _Unsafe()
            item_attrs = self._attrs(item)
            indicator_known = bar_selection_known
            expanded_known = False
            for name in ("AXSelected", "AXExpanded"):
                if name in item_attrs:
                    self._false(item, name)
                    indicator_known = True
                    expanded_known |= name == "AXExpanded"
            if "AXSelectedChildren" in item_attrs:
                if self._items(self._read(item, "AXSelectedChildren")):
                    raise _Unsafe()
                indicator_known = True
            if not indicator_known:
                raise _Unsafe()
            if "AXChildren" in item_attrs:
                for menu in self._items(self._read(item, "AXChildren")):
                    if self._role(menu) != "AXMenu":
                        raise _Unsafe()
                    menu_attrs = self._attrs(menu)
                    if "AXVisible" in menu_attrs:
                        self._false(menu, "AXVisible")
                    elif not (expanded_known or bar_selection_known):
                        # PowerPoint exposes closed AXMenu children without
                        # AXVisible/AXExpanded. An explicitly empty selection
                        # on their menu bar also proves no bar menu is active.
                        # Missing/failed queries alone are still not evidence.
                        raise _Unsafe()
                    if "AXSelectedChildren" in menu_attrs:
                        if self._items(self._read(menu, "AXSelectedChildren")):
                            raise _Unsafe()

    def safe(self, pid):
        self.pid = pid
        try:
            app = self.ax.AXUIElementCreateApplication(pid)
            window = self._read(app, "AXFocusedWindow")
            focused = self._read(app, "AXFocusedUIElement")
            attrs = self._container(window, window=True)
            self._window_without_sheets(window, attrs)
            self._menus_closed(app)
            node, seen = focused, []
            for _ in range(32):
                if any(self._same(node, previous) for previous in seen):
                    raise _Unsafe()
                seen.append(node)
                self._container(node, window=self._same(node, window))
                if self._same(node, window):
                    break
                # Both the AXWindow relationship AND a safe ancestor chain
                # must lead to this focused window, all owned by captured PID.
                if not self._same(self._read(node, "AXWindow"), window):
                    raise _Unsafe()
                node = self._read(node, "AXParent")
            else:
                raise _Unsafe()
            if not self._same(self._read(app, "AXFocusedWindow"), window):
                raise _Unsafe()
            if not self._same(self._read(app, "AXFocusedUIElement"), focused):
                raise _Unsafe()
            self._menus_closed(app)
            return True
        except Exception:
            # Includes missing bridge APIs, malformed values, unsupported AX
            # attributes and AX messaging failures; none is evidence of safety.
            return False
