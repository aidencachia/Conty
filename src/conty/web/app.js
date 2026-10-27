"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  csrf: null,
  revision: null,
  definition: null,
  selected: null,
  connected: false,
  available: true,
  queue: [],
  submitting: false,
  outstanding: new Map(),
  feedback: new Map(),
  controls: new Map(),
  activity: [],
  statusBusy: false,
  pollBusy: false,
  failures: 0,
  generation: 0,
  volume: null,
  volumeBusy: false,
  volumeVersion: 0,
  volumeDesired: null,
  volumeActive: null,
  volumeTimer: null,
  volumeLastSent: -Infinity,
};
const SLIDER_INTERVAL_MS = 50;
const terminal = new Set(["succeeded", "failed", "timed_out", "cancelled"]);

class APIError extends Error {
  constructor(status, data) {
    super(data?.error?.message || "The host could not complete this request.");
    this.status = status;
    this.code = data?.error?.code;
  }
}

async function api(path, payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const options = {
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    };
    if (payload !== undefined) {
      options.method = "POST";
      options.headers = {
        "Content-Type": "application/json",
        "X-CSRF-Token": state.csrf || "",
      };
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new APIError(response.status, data);
    return data;
  } finally {
    clearTimeout(timer);
  }
}

function connection(text, kind) {
  if ($("connection").textContent !== text) $("connection").textContent = text;
  $("connection-dot").className = `dot ${kind || ""}`;
}
function notice(text, kind = "action") {
  $("notice").hidden = !text;
  $("notice").dataset.kind = kind;
  if ($("notice").textContent !== (text || ""))
    $("notice").textContent = text || "";
}
function addActivity(label, text) {
  state.activity.unshift({ label, text });
  state.activity = state.activity.slice(0, 20);
  const nodes = state.activity.map((item) => {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = item.label;
    const status = document.createElement("span");
    status.textContent = item.text;
    li.append(name, status);
    return li;
  });
  $("activity-list").replaceChildren(...nodes);
}
function discardUnsent() {
  clearTimeout(state.volumeTimer);
  state.volumeTimer = null;
  state.volumeDesired = null;
  for (const control of state.controls.values())
    if (control.input) control.input.dataset.editing = "false";
  for (const item of state.queue)
    state.feedback.set(item.key, "Not submitted · connection changed");
  state.queue.length = 0;
}
function disconnected() {
  state.connected = false;
  discardUnsent();
  connection("Reconnecting", "offline");
  notice(
    "Connection lost. Unsent taps were discarded. Accepted actions may still run; checking known outcomes.",
    "connection",
  );
  updateControls();
}
function requirePairing() {
  discardUnsent();
  state.generation++;
  for (const item of state.outstanding.values())
    addActivity(item.label, "Outcome unknown · session ended");
  state.outstanding.clear();
  state.volumeActive = null;
  state.csrf = null;
  state.volume = null;
  state.volumeVersion++;
  state.connected = false;
  state.definition = null;
  state.revision = null;
  $("dashboard").hidden = true;
  $("pairing").hidden = false;
  connection("Pairing required");
}

function updateControls() {
  let pending = state.queue.length + state.outstanding.size;
  if (state.submitting) pending++;
  $("pending-count").textContent = pending ? `${pending} pending` : "";
  for (const [key, control] of state.controls) {
    const count =
      state.queue.filter((item) => item.key === key).length +
      [...state.outstanding.values()].filter((item) => item.key === key).length;
    control.button.disabled =
      !state.connected ||
      !state.available ||
      state.queue.length >= 32 ||
      (control.input && state.volume === null);
    if (control.input) {
      control.input.disabled = control.button.disabled;
      control.button.dataset.disabled = String(control.input.disabled);
    }
    control.button.dataset.pending = String(count > 0);
    control.status.textContent = count
      ? `${count} pending`
      : state.feedback.get(key) || "Ready";
  }
}

function renderPage() {
  const dashboard = state.definition.dashboards.find(
    (page) => page.id === state.selected,
  );
  state.controls.clear();
  $("pages").replaceChildren(
    ...state.definition.dashboards.map((page) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = page.title;
      button.setAttribute("aria-pressed", String(page.id === dashboard.id));
      button.addEventListener("click", () => {
        state.selected = page.id;
        renderPage();
      });
      return button;
    }),
  );
  $("dashboard-title").textContent = dashboard.title;
  $("sections").replaceChildren(
    ...dashboard.sections.map((section, index) => {
      const wrapper = document.createElement("section");
      wrapper.className = "control-section";
      const heading = document.createElement("h2");
      heading.className = "section-heading";
      heading.textContent = section.title;
      heading.id = `section-${index}`;
      heading.hidden = dashboard.sections.length === 1;
      wrapper.setAttribute("aria-label", section.title);
      const scroll = document.createElement("div");
      scroll.className = "grid-scroll";
      scroll.setAttribute("role", "region");
      scroll.setAttribute("aria-label", `${section.title} controls`);
      scroll.tabIndex = 0;
      const grid = document.createElement("div");
      grid.className = "control-grid";
      // Empty trailing columns must contribute to scroll width too.
      grid.style.minWidth = `${section.layout.columns * 88 + (section.layout.columns - 1) * 8}px`;
      grid.style.gridTemplateColumns = `repeat(${section.layout.columns}, minmax(88px, 1fr))`;
      grid.style.gridTemplateRows = `repeat(${section.layout.rows}, 112px)`;
      for (const widget of section.widgets) {
        const key = `${dashboard.id}/${section.id}/${widget.id}`;
        const slider = widget.type === "slider";
        const button = document.createElement(slider ? "div" : "button");
        if (!slider) button.type = "button";
        if (!slider) button.setAttribute("aria-label", widget.label);
        button.className = `control ${widget.appearance || ""}`;
        button.dataset.widget = widget.id;
        button.dataset.kind = widget.type;
        button.style.gridRow = `${widget.position.row} / span ${widget.position.row_span}`;
        button.style.gridColumn = `${widget.position.column} / span ${widget.position.column_span}`;
        const symbol = document.createElement("span");
        symbol.className = "control-symbol";
        symbol.textContent = widget.symbol || "•";
        symbol.setAttribute("aria-hidden", "true");
        const label = document.createElement("span");
        label.className = "control-label";
        label.textContent = widget.label;
        const status = document.createElement("span");
        status.className = "control-status sr-only";
        button.append(symbol, label);
        let input, output;
        if (slider) {
          button.classList.add("slider-control");
          const filled = widget.style === "ios";
          if (filled) {
            button.classList.add("slider-ios");
            const fill = document.createElement("span");
            fill.className = "slider-fill";
            fill.setAttribute("aria-hidden", "true");
            button.prepend(fill);
          }
          const vertical =
            widget.position.row_span > widget.position.column_span;
          if (vertical) button.classList.add("vertical");
          input = document.createElement("input");
          input.type = "range";
          input.min = "0";
          input.max = "100";
          input.step = "1";
          input.setAttribute("aria-label", widget.label);
          input.setAttribute("aria-orientation", vertical ? "vertical" : "horizontal");
          output = document.createElement("output");
          output.textContent = "—";
          if (state.volume !== null) {
            setSliderValue({ button, input, output }, state.volume.value);
          }
          input.disabled = true;
          input.addEventListener("input", () => {
            const changed = output.textContent !== `${input.value}%`;
            state.volumeVersion++;
            input.dataset.editing = "true";
            setSliderValue({ button, input, output }, input.value);
            if (changed) enqueue(widget, key, Number(input.value));
          });
          input.addEventListener("change", () => {
            input.dataset.editing = "false";
            state.volumeVersion++;
            enqueue(widget, key, Number(input.value));
            void refreshVolume();
          });
          for (const event of ["blur", "pointercancel"])
            input.addEventListener(event, () => {
              if (input.dataset.editing === "true") cancelSlider(key, input);
              else void refreshVolume();
            });
          button.append(input, output);
          if (filled) bindFilledSlider(input, vertical, () => cancelSlider(key, input));
        } else {
          button.addEventListener("click", () => enqueue(widget, key));
        }
        button.append(status);
        button.addEventListener("focus", () =>
          button.scrollIntoView({ block: "nearest", inline: "nearest" }),
        );
        state.controls.set(key, { button, status, input, output });
        grid.append(button);
      }
      scroll.append(grid);
      wrapper.append(heading, scroll);
      return wrapper;
    }),
  );
  updateControls();
  void refreshVolume();
}

function setSliderValue(control, value) {
  control.input.value = String(value);
  control.output.textContent = `${value}%`;
  control.button.style.setProperty("--slider-value", `${value}%`);
}

function cancelSlider(key, input) {
  input.dataset.editing = "false";
  if (state.volumeDesired?.key === key) {
    state.queue = state.queue.filter((item) => item.value === undefined);
    state.volumeDesired = null;
    clearTimeout(state.volumeTimer);
    state.volumeTimer = null;
  }
  // Already accepted live changes cannot be undone. Read the actual host value
  // after any in-flight write, rather than pretending to restore the drag start.
  void drain();
  void refreshVolume();
}

function bindFilledSlider(input, vertical, onCancel) {
  // Keep the native range for keyboard/assistive access. Pointer handling uses
  // the whole tile, with exact endpoints and coalesced live updates.
  let drag = null;
  function preview(event) {
    const bounds = input.getBoundingClientRect();
    const fraction = vertical
      ? (bounds.bottom - event.clientY) / bounds.height
      : (event.clientX - bounds.left) / bounds.width;
    input.value = String(Math.round(Math.max(0, Math.min(1, fraction)) * 100));
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function cancel() {
    if (!drag) return;
    const previous = drag;
    drag = null;
    if (input.hasPointerCapture(previous.id)) input.releasePointerCapture(previous.id);
    onCancel();
  }
  input.addEventListener("pointerdown", (event) => {
    if (input.disabled || drag || !event.isPrimary || event.button !== 0) return;
    event.preventDefault();
    input.focus({ preventScroll: true });
    drag = { id: event.pointerId, value: input.value };
    input.setPointerCapture(event.pointerId);
    preview(event);
  });
  input.addEventListener("pointermove", (event) => {
    if (!drag || drag.id !== event.pointerId) return;
    if (input.disabled) return cancel();
    preview(event);
  });
  input.addEventListener("pointerup", (event) => {
    if (!drag || drag.id !== event.pointerId) return;
    if (input.disabled) return cancel();
    preview(event);
    const previous = drag;
    drag = null;
    input.releasePointerCapture(event.pointerId);
    input.dataset.editing = "false";
    if (input.value !== previous.value)
      input.dispatchEvent(new Event("change", { bubbles: true }));
    else void refreshVolume();
  });
  for (const event of ["pointercancel", "lostpointercapture", "blur"])
    input.addEventListener(event, cancel);
}

async function refreshVolume() {
  if (state.volumeBusy || !state.connected || document.hidden) return;
  const sliders = [...state.controls.values()].filter(
    (control) => control.input,
  );
  if (
    !sliders.length ||
    sliders.some((c) => c.input.dataset.editing === "true")
  )
    return;
  if (
    state.queue.some((item) => item.value !== undefined) ||
    state.volumeActive ||
    [...state.outstanding.values()].some((item) => item.value !== undefined)
  )
    return;
  state.volumeBusy = true;
  const version = state.volumeVersion,
    generation = state.generation;
  try {
    const volume = await api("/api/volume");
    if (version !== state.volumeVersion || generation !== state.generation)
      return;
    state.volume = volume;
    state.volumeDesired = null;
    for (const control of state.controls.values())
      if (control.input) {
        setSliderValue(control, volume.value);
      }
  } catch (error) {
    if (generation === state.generation && version === state.volumeVersion) {
      state.volume = null;
      for (const control of state.controls.values())
        if (control.input) control.output.textContent = "Unavailable";
    }
    if (error.status === 401) requirePairing();
  } finally {
    state.volumeBusy = false;
    updateControls();
  }
}

async function refreshDashboard() {
  const definition = await api("/api/dashboard");
  discardUnsent();
  state.generation++;
  state.definition = definition;
  state.revision = definition.revision;
  if (!definition.dashboards.some((page) => page.id === state.selected))
    state.selected = definition.dashboards[0].id;
  $("pairing").hidden = true;
  $("dashboard").hidden = false;
  renderPage();
}
function requestId() {
  // getRandomValues also works on ordinary HTTP LAN pages; randomUUID may not.
  return [...crypto.getRandomValues(new Uint8Array(24))]
    .map((n) => n.toString(16).padStart(2, "0"))
    .join("");
}
function enqueue(widget, key, value) {
  if (!state.connected || !state.available || state.queue.length >= 32) return;
  if (value !== undefined) {
    // One latest unsent value for system volume, even across slider styles.
    if (state.volumeDesired?.value === value) return;
    state.queue = state.queue.filter((item) => item.value === undefined);
    if (state.volumeActive?.value === value) {
      state.volumeDesired = state.volumeActive;
      updateControls();
      void drain();
      return;
    }
  }
  const item = {
    key,
    label: widget.label,
    action_id: widget.action,
    request_id: requestId(),
    revision: state.revision,
    generation: state.generation,
    value,
  };
  if (value !== undefined) state.volumeDesired = item;
  state.queue.push(item);
  updateControls();
  void drain();
}
async function drain() {
  if (state.submitting || !state.connected) return;
  state.submitting = true;
  try {
    while (state.queue.length && state.connected) {
      if (state.queue[0].value !== undefined) {
        if (state.volumeActive) break;
        const delay = SLIDER_INTERVAL_MS - (performance.now() - state.volumeLastSent);
        if (delay > 0) {
          if (state.volumeTimer === null)
            state.volumeTimer = setTimeout(() => {
              state.volumeTimer = null;
              void drain();
            }, Math.ceil(delay));
          break;
        }
      }
      const item = state.queue.shift();
      if (item.value !== undefined) {
        state.volumeActive = item;
        state.volumeLastSent = performance.now();
      }
      updateControls();
      try {
        const record = await api(item.value === undefined
          ? "/api/invocations" : "/api/invocations?wait=0.25", {
          request_id: item.request_id,
          action_id: item.action_id,
          revision: item.revision,
          ...(item.value !== undefined ? { value: item.value } : {}),
        });
        if (item.generation !== state.generation) {
          if (state.volumeActive === item) state.volumeActive = null;
          addActivity(item.label, "Outcome unknown · connection changed");
          continue;
        }
        state.outstanding.set(record.id, item);
        state.feedback.set(item.key, record.state);
        applyRecord(record, item);
        void pollInvocations();
      } catch (error) {
        if (state.volumeActive === item) {
          state.volumeActive = null;
          state.volumeDesired = null;
          state.queue = state.queue.filter((queued) => queued.value === undefined);
        }
        if (error instanceof APIError) {
          const outcome = item.value !== undefined && error.status === 401
            ? "Outcome unknown · session ended" : `Not accepted · ${error.message}`;
          state.feedback.set(item.key, outcome);
          addActivity(item.label, outcome);
          notice(error.message);
          if (error.status === 401) requirePairing();
          else if (error.code === "stale_revision") {
            discardUnsent();
            await refreshDashboard();
          } else if (error.status === 429 || error.status === 503) {
            discardUnsent();
            break;
          }
        } else {
          state.feedback.set(
            item.key,
            "Outcome unknown · submission response lost",
          );
          addActivity(item.label, "Outcome unknown · submission response lost");
          disconnected();
          notice(
            "Outcome unknown. Check the host before repeating this action.",
          );
        }
      }
      updateControls();
    }
  } finally {
    state.submitting = false;
    updateControls();
  }
}
function applyRecord(record, item) {
  if (terminal.has(record.state)) {
    state.outstanding.delete(record.id);
    if (state.volumeActive === item) state.volumeActive = null;
    const success = record.state === "succeeded";
    const text = success
      ? record.result.message
      : `${record.state.replaceAll("_", " ")} · ${record.result?.message || "Check the host"}`;
    state.feedback.set(item.key, text);
    addActivity(
      item.label,
      text +
        (!success && record.result?.effects_possible
          ? " · effects possible"
          : ""),
    );
    if (!success)
      notice(`${item.label}: ${record.result?.message || "Action failed"}`);
    if (item.value !== undefined) {
      if (!success) {
        state.volumeDesired = null;
        state.queue = state.queue.filter((queued) => queued.value === undefined);
      }
      void drain();
      void refreshVolume();
    }
  } else
    state.feedback.set(
      item.key,
      record.state === "running" ? "Running on host" : "Queued on host",
    );
}

async function pollInvocations() {
  if (
    state.pollBusy ||
    !state.connected ||
    !state.outstanding.size ||
    document.hidden
  )
    return;
  state.pollBusy = true;
  try {
    while (state.connected && state.outstanding.size && !document.hidden) {
      const generation = state.generation;
      const query = new URLSearchParams({ wait: "2" });
      for (const id of [...state.outstanding.keys()].slice(0, 64))
        query.append("ids", id);
      // One bounded read wakes as soon as any watched action changes. No timer
      // delays feedback, and only one browser connection is occupied.
      const result = await api(`/api/invocations?${query}`);
      if (generation !== state.generation) continue;
      for (const record of result.invocations) {
        const item = state.outstanding.get(record.id);
        if (item) applyRecord(record, item);
      }
      for (const id of result.missing_ids) {
        const item = state.outstanding.get(id);
        if (item) {
          state.outstanding.delete(id);
          if (state.volumeActive === item) {
            state.volumeActive = null;
            state.volumeDesired = null;
            state.queue = state.queue.filter((queued) => queued.value === undefined);
            void drain();
          }
          state.feedback.set(item.key, "Outcome unknown");
          addActivity(item.label, "Outcome unknown · record unavailable");
          notice(`Outcome unknown: ${item.label}.`);
        }
      }
      updateControls();
    }
  } catch (error) {
    if (error.status === 401) requirePairing();
    else disconnected();
  } finally {
    state.pollBusy = false;
    updateControls();
  }
}

async function checkStatus(schedule = true) {
  if (!state.statusBusy) {
    state.statusBusy = true;
    try {
      const status = await api("/api/status");
      state.csrf = status.csrf_token;
      state.available = status.executor_available;
      if (!state.definition || state.revision !== status.revision)
        await refreshDashboard();
      state.connected = true;
      state.failures = 0;
      connection(
        state.available ? "Connected" : "Host needs attention",
        state.available ? "connected" : "offline",
      );
      if (!state.available)
        notice(
          "Execution is unavailable. Check the computer and restart Conty locally.",
          "executor",
        );
      else if (["connection", "executor"].includes($("notice").dataset.kind))
        notice("");
      updateControls();
      void pollInvocations();
      void refreshVolume();
    } catch (error) {
      if (error.status === 401) requirePairing();
      else {
        state.failures++;
        disconnected();
      }
    } finally {
      state.statusBusy = false;
    }
  }
  const delay = state.failures
    ? Math.min(15000, 2000 * 2 ** Math.min(state.failures, 3)) +
      Math.random() * 400
    : document.hidden
      ? 10000
      : 2000;
  if (schedule) setTimeout(() => void checkStatus(), delay);
}

$("pair-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("pair-submit").disabled = true;
  $("pair-message").classList.remove("error");
  $("pair-message").textContent = "Connecting to your computer…";
  try {
    const result = await api("/api/pair", { code: $("code").value });
    state.csrf = result.csrf_token;
    state.connected = true;
    state.failures = 0;
    $("code").value = "";
    await refreshDashboard();
    connection("Connected", "connected");
    updateControls();
  } catch (error) {
    $("pair-message").classList.add("error");
    $("pair-message").textContent =
      error instanceof APIError
        ? error.message
        : "The computer is unreachable. Check your network and try again.";
  } finally {
    $("pair-submit").disabled = false;
  }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) void checkStatusOnce();
  else {
    for (const [key, control] of state.controls)
      if (control.input) cancelSlider(key, control.input);
  }
});
async function checkStatusOnce() {
  // The scheduled checker owns the only recurring status timer.
  if (!state.statusBusy && state.csrf) await checkStatus(false);
}
window.addEventListener("offline", disconnected);
window.addEventListener("online", () => void checkStatus(false));
void checkStatus();
void pollInvocations();
