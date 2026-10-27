"use strict";

// Presentation commands have their own deck-bound lane, not the native keyboard queue.
function createPowerPointWidget() {
  const panel = document.getElementById("powerpoint-template").content.firstElementChild.cloneNode(true);
  const elements = new Map();
  for (const node of panel.querySelectorAll("[id]")) {
    elements.set(node.id, node);
    node.dataset.role = node.id;
    node.removeAttribute("id");
  }
  const $ = (id) => elements.get(id);
  const active = () => panel.isConnected && !panel.closest("[hidden]");
  const events = new AbortController();
  const automaticPreviews = new Set();
  let centeredSlide = null;
  const picker = $("presentation-select");
  const scan = $("presentation-scan");
  const discoveryStatus = $("presentation-discovery-status");
  const slides = $("presentation-slides");
  const info = $("presentation-info");
  const feedback = $("presentation-message");
  const selectionStatus = $("presentation-selection-status");
  const previewStatus = $("presentation-preview-status");
  const retryPreviews = $("presentation-retry-previews");
  const show = { disabled: true };
  const refresh = $("presentation-refresh");

  const finished = new Set(["succeeded", "failed", "cancelled", "outcome_unknown"]);
  const labels = { refresh: "PDF previews", show_slide: "Show slide" };
  const recovery = "Restart Conty locally before sending another command to this instance.";
  const uncertain = new Set();
  const observations = new Map();
  let decks = [];
  let selected = "";
  let selectedSlide = null;
  let selectedRevision = null;
  let cards = new Map();
  let optionsSignature = "";
  let generation = state.generation;
  let busy = null;
  let sending = null;
  let pending = null;
  let loadError = "";
  let listHealthy = false;
  let listLoaded = false;
  let listLoading = false;
  let discovering = false;
  let imageEpoch = 0;
  let imageQueue = [];
  let imageActive = 0;
  let imageLastStarted = -Infinity;
  let imagePumpTimer = null;
  let imageObserver = null;

  function current() {
    return decks.find((deck) => deck.id === selected);
  }

  function imageProgress() {
    const images = [...cards.values()].filter((card) => card.imageUrl);
    const loaded = images.filter((card) => card.imageState === "loaded").length;
    const failed = images.filter((card) => card.imageState === "failed").length;
    previewStatus.textContent = images.length
      ? `${loaded}/${images.length} preview images loaded${failed ? ` · ${failed} unavailable` : ""}. Scroll to load more.`
      : "";
    retryPreviews.hidden = failed === 0;
    retryPreviews.disabled = !state.connected;
  }

  function queueImage(card) {
    if (!state.connected || cards.get(card.button.dataset.slideId) !== card || !card.imageUrl ||
        ["queued", "loading", "loaded"].includes(card.imageState)) return;
    card.imageState = "queued";
    imageQueue.push(card);
    pumpImages();
  }

  function pumpImages() {
    if (imagePumpTimer || imageActive >= 3 || !imageQueue.length || !state.connected ||
        !active() || document.hidden) return;
    // Pace starts as well as limiting concurrency: even tiny cached PNGs must
    // not exhaust the host's per-client request bucket when a large grid opens.
    const delay = Math.max(0, 65 - (performance.now() - imageLastStarted));
    if (delay > 0) {
      imagePumpTimer = setTimeout(() => { imagePumpTimer = null; pumpImages(); }, delay);
      return;
    }
    const card = imageQueue.shift();
    if (cards.get(card.button.dataset.slideId) !== card || card.imageState !== "queued") {
      pumpImages();
      return;
    }
    const epoch = imageEpoch;
    const token = card.imageToken;
    const image = document.createElement("img");
    const live = () => epoch === imageEpoch && card.imageToken === token &&
      cards.get(card.button.dataset.slideId) === card;
    card.imageState = "loading";
    card.imageAttempts++;
    card.imageRound++;
    imageActive++;
    imageLastStarted = performance.now();
    let done = false;
    let timeout;
    function finish(ok, cancelled = false) {
      if (done) return;
      done = true;
      clearTimeout(timeout);
      image.onload = image.onerror = null;
      if (!ok) image.removeAttribute("src");
      card.cancelImage = null;
      if (epoch === imageEpoch) imageActive--;
      if (live() && !cancelled) {
        if (ok) card.imageState = "loaded";
        else if (card.imageAttempts < 3) {
          card.imageState = "retrying";
          card.preview.textContent = "Retrying preview…";
          card.imageTimer = setTimeout(() => {
            card.imageTimer = null;
            if (live()) queueImage(card);
          }, card.imageAttempts === 1 ? 500 : 1500);
        } else {
          card.imageState = "failed";
          card.preview.textContent = "—";
          card.preview.title = "Preview unavailable. Retry using the refresh icon.";
        }
        imageProgress();
      }
      pumpImages();
    }
    card.cancelImage = () => { finish(false, true); image.removeAttribute("src"); };
    image.alt = card.imageAlt;
    image.onload = () => finish(true);
    image.onerror = () => finish(false);
    timeout = setTimeout(() => finish(false), 8000);
    card.preview.replaceChildren(image);
    // Only safe image GETs are retried. A failed thumbnail never resubmits
    // navigation or regenerates the PowerPoint/PDF snapshot.
    image.src = card.imageUrl + (card.imageRound > 1 ? `&preview_attempt=${card.imageRound}` : "");
    pumpImages();
  }

  function prepareImage(card, url, number) {
    card.cancelImage?.();
    clearTimeout(card.imageTimer);
    imageObserver?.unobserve(card.button);
    card.imageUrl = url;
    card.imageToken = {};
    card.imageAttempts = card.imageRound = 0;
    card.imageState = "waiting";
    card.imageAlt = `Preview of slide ${number}`;
    card.preview.textContent = url ? "…" : "—";
    if (!url) return;
    if (typeof IntersectionObserver === "function") {
      if (!imageObserver) imageObserver = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          const visible = cards.get(entry.target.dataset.slideId);
          if (entry.isIntersecting && visible?.button === entry.target) {
            imageObserver.unobserve(entry.target);
            queueImage(visible);
          }
        }
      }, { root: slides, rootMargin: "300px" });
      imageObserver.observe(card.button);
    } else queueImage(card);
  }

  function clearCards() {
    imageEpoch++;
    clearTimeout(imagePumpTimer);
    imagePumpTimer = null;
    imageQueue = [];
    imageObserver?.disconnect();
    imageObserver = null;
    for (const card of cards.values()) {
      clearTimeout(card.imageTimer);
      card.cancelImage?.();
    }
    imageActive = 0;
    cards.clear();
    slides.replaceChildren();
    centeredSlide = null;
    imageProgress();
  }

  function clear() {
    decks = [];
    selected = "";
    selectedSlide = selectedRevision = null;
    pending = busy = sending = null;
    observations.clear();
    automaticPreviews.clear();
    clearCards();
    optionsSignature = "";
    loadError = feedback.textContent = "";
    listHealthy = listLoaded = listLoading = discovering = false;
    render();
  }

  function fresh(deck) {
    const observation = observations.get(deck.id);
    // observed_at is a host clock, not a browser timestamp. Expire unchanged observations locally.
    return listHealthy && active() && !document.hidden && observation &&
      performance.now() - observation.received < 6000;
  }

  function render() {
    const deck = current();
    const loading = listLoading || discovering;
    const placeholderText = decks.length ? "Choose a running presentation"
      : loading ? "Looking…"
        : loadError ? "Unable to load presentations"
          : listLoaded ? "No presentations found" : "Choose a running presentation";
    const options = JSON.stringify([placeholderText, selected, decks.map((item) => [item.id, item.title, item.connected])]);
    if (options !== optionsSignature) {
      optionsSignature = options;
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = placeholderText;
      const entries = decks.map((item) => {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = `${item.title || "Untitled"} · ${item.id.slice(0, 8)}${item.connected ? "" : " · Offline"}`;
        return option;
      });
      if (selected && !deck) {
        const missing = document.createElement("option");
        missing.value = selected;
        missing.textContent = "Previously selected presentation · Unavailable";
        entries.push(missing);
      }
      picker.replaceChildren(placeholder, ...entries);
      picker.value = selected;
    }
    picker.hidden = decks.length === 1 && !!deck;
    picker.disabled = !state.connected || (loading && !listLoaded && !decks.length);
    picker.setAttribute("aria-busy", String(loading));
    scan.disabled = !state.connected || !!busy || discovering || !!sending || pending !== null;
    discoveryStatus.textContent = loading && !listLoaded
      ? "Looking for open PowerPoint presentations…"
      : loadError || (listLoaded && !decks.length ? "No presentations found. Open PowerPoint and scan again." : "");
    if (deck && selectedRevision !== deck.revision) {
      selectedRevision = deck.revision;
      selectedSlide = null;
      clearCards();
    }
    const online = state.connected && deck?.connected;
    const blocked = !!sending || pending !== null || uncertain.has(selected);
    const available = online && fresh(deck) && !blocked;
    const navigation = available && deck.view === "read" && deck.can_control;
    const target = deck?.slides.find((slide) => slide.id === selectedSlide);
    refresh.title = deck?.needs_refresh ? "Load previews" : "Refresh previews";
    refresh.disabled = !available || deck.view !== "edit";
    show.disabled = !navigation || deck.needs_refresh || !target?.preview_url || !Number.isInteger(target?.page);
    selectionStatus.textContent = !target ? "" : `Slide ${target.number}. Double-click or double-tap to show.`;
    panel.setAttribute("aria-busy", String(!!sending || pending !== null));
    if (!deck) {
      clearCards();
      info.textContent = selected ? "Presentation unavailable" : "";
      return;
    }
    info.textContent = !online ? "Presentation disconnected"
      : deck.view === "read" && deck.needs_refresh ? "Stop slideshow to load previews"
        : deck.view === "read" && !deck.can_control ? "Focus slideshow in PowerPoint" : "";
    if (!online) {
      clearCards();
      return;
    }
    const live = fresh(deck) && deck.view === "read" ? deck.current_slide_id : null;
    for (const slide of deck.slides) {
      let card = cards.get(slide.id);
      if (!card) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "slide-card";
        button.dataset.slideId = slide.id;
        const preview = document.createElement("span");
        preview.className = "slide-preview";
        const label = document.createElement("span");
        const badge = document.createElement("span");
        badge.className = "slide-badge";
        badge.textContent = "◌";
        badge.title = "Hidden slide";
        const liveLabel = document.createElement("span");
        liveLabel.className = "slide-live-label";
        liveLabel.textContent = "●";
        liveLabel.setAttribute("aria-label", "Active slide");
        button.append(preview, label, badge, liveLabel);
        button.addEventListener("click", () => {
          selectedSlide = slide.id;
          render();
        });
        const activate = () => {
          selectedSlide = slide.id;
          void submit("show_slide");
        };
        button.addEventListener("dblclick", activate);
        let lastTap = 0;
        let touchStart = null;
        button.addEventListener("pointerdown", (event) => {
          if (event.pointerType === "touch") touchStart = { x: event.clientX, y: event.clientY };
        });
        button.addEventListener("pointercancel", () => { touchStart = null; lastTap = 0; });
        button.addEventListener("pointerup", (event) => {
          if (event.pointerType !== "touch" || !touchStart) return;
          const moved = Math.hypot(event.clientX - touchStart.x, event.clientY - touchStart.y);
          touchStart = null;
          if (moved > 12) { lastTap = 0; return; }
          const now = performance.now();
          if (lastTap && now - lastTap < 350) { event.preventDefault(); activate(); lastTap = 0; }
          else lastTap = now;
        });
        button.addEventListener("keydown", (event) => {
          if (event.key === "Enter") { event.preventDefault(); activate(); }
        });
        card = { button, preview, label, badge, liveLabel, imageUrl: null };
        cards.set(slide.id, card);
        slides.append(button);
      }
      card.label.textContent = String(slide.number);
      card.button.setAttribute("aria-label", `Slide ${slide.number}${slide.title ? `: ${slide.title}` : ""}`);
      card.badge.hidden = !slide.hidden;
      card.button.dataset.page = slide.page ?? "";
      card.button.setAttribute("aria-pressed", String(slide.id === selectedSlide));
      card.button.dataset.live = String(slide.id === live);
      card.button.classList.toggle("is-live", slide.id === live);
      card.liveLabel.hidden = slide.id !== live;
      if (slide.id === live) card.button.setAttribute("aria-current", "true");
      else card.button.removeAttribute("aria-current");
      card.button.title = "Double-click or double-tap to show";
      if (card.imageUrl !== slide.preview_url || !card.preview.childNodes.length)
        prepareImage(card, slide.preview_url, slide.number);
    }
    const center = live || centeredSlide || deck.slides[0]?.id;
    if (active() && center && centeredSlide !== center) {
      centeredSlide = center;
      requestAnimationFrame(() => {
        if (centeredSlide === center && active()) centerCard(cards.get(center)?.button);
      });
    }
    imageProgress();
    pumpImages();
  }

  function centerCard(button) {
    if (!button) return;
    const bounds = button.getBoundingClientRect();
    const viewport = slides.getBoundingClientRect();
    slides.scrollTo({ left: slides.scrollLeft + bounds.left - viewport.left + bounds.width / 2 - viewport.width / 2,
      behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
  }

  function autoPreview() {
    const deck = current();
    if (!active() || !deck?.needs_refresh || refresh.disabled || busy || document.hidden) return;
    const key = `${deck.id}/${deck.revision}`;
    if (automaticPreviews.has(key)) return;
    // Never loop on failed exports or retry an uncertain mutation automatically.
    automaticPreviews.add(key);
    void submit("refresh");
  }

  function unknown(command, text) {
    uncertain.add(command.deck);
    pending = null;
    feedback.textContent = `${text} ${recovery}`;
    feedback.classList.remove("sr-only");
  }

  function applyRecord(command, record) {
    if (!finished.has(record.state)) {
      feedback.classList.add("sr-only");
      feedback.textContent = record.operation === "refresh"
        ? `${record.state === "running" ? "Loading PDF previews" : "PDF preview load queued"} — Waiting for PowerPoint (up to 90 seconds).`
        : `${record.state === "running" ? "Running" : "Queued"} — Waiting for PowerPoint. Navigation completion does not verify the audience display.`;
      return false;
    }
    const text = record.state === "succeeded" && command.operation === "refresh"
      ? "PDF previews ready"
      : record.message;
    feedback.textContent = text;
    feedback.classList.toggle("sr-only", record.state === "succeeded");
    addActivity(command.label, text);
    pending = null;
    if (record.state === "outcome_unknown") unknown(command, text);
    if (record.state === "succeeded" && command.operation === "refresh" && selected === command.deck) {
      selectedSlide = null;
      clearCards();
    }
    return true;
  }

  async function poll() {
    if (busy || sending || !state.connected || !state.csrf || !active() || document.hidden) return;
    const token = {};
    busy = token;
    const started = state.generation;
    render();
    try {
      if (pending) {
        const command = pending;
        if (performance.now() - command.created >= 120000) {
          unknown(command, "Command outcome is no longer available. The command was not retried.");
        } else {
          try {
            const record = await api(`/api/presentations/${command.deck}/commands/${command.id}`);
            if (started !== state.generation || pending !== command) return;
            const done = applyRecord(command, record);
            // Native commands hold the service lock. Poll cheap status, not discovery.
            if (!done) return;
          } catch (error) {
            if (started !== state.generation) return;
            if (error.status === 404) unknown(command, "Command outcome is no longer available. The command was not retried.");
            else throw error;
          }
        }
      }
      listLoading = true;
      loadError = "";
      render();
      const result = await api("/api/presentations");
      if (started !== state.generation || !state.connected) return;
      decks = result.presentations;
      discovering = result.discovering === true;
      loadError = "";
      if (!discovering) listHealthy = listLoaded = true;
      // Another scan may already be running when a completed observation reaches
      // this phone. Only a changed host timestamp renews it; repeated cached
      // responses never make an unchanged observation look live again.
      if (listLoaded) for (const deck of decks) {
        if (deck.observed_at != null && observations.get(deck.id)?.value !== deck.observed_at) {
          observations.set(deck.id, { value: deck.observed_at, received: performance.now() });
        }
      }
      // Never replace a missing selected instance with another deck of the same name.
      if (!selected && decks.filter((deck) => deck.connected).length === 1 && listLoaded) {
        selected = decks.find((deck) => deck.connected).id;
      }
    } catch (error) {
      if (started !== state.generation) return;
      discovering = false;
      if (error.status === 401) requirePairing();
      else {
        listHealthy = false;
        loadError = error instanceof APIError
          ? error.message
          : "Unable to load presentations right now. Selection retained; commands are not retried.";
      }
    } finally {
      if (busy === token) {
        busy = null;
        listLoading = false;
        render();
        autoPreview();
      }
    }
  }

  async function submit(operation) {
    render();
    const button = { refresh, show_slide: show }[operation];
    const deck = current();
    if (!deck || button.disabled) return;
    const started = state.generation;
    const payload = { request_id: requestId(), revision: deck.revision, operation };
    if (operation === "show_slide") payload.slide_id = selectedSlide;
    const token = {};
    sending = token;
    const command = { deck: deck.id, operation, label: `${deck.title} · ${labels[operation]}`, created: performance.now() };
    feedback.classList.add("sr-only");
    feedback.textContent = "Submitting to the selected presentation…";
    render();
    try {
      const record = await api(`/api/presentations/${deck.id}/commands`, payload);
      if (started !== state.generation) return;
      command.id = record.id;
      pending = command;
      applyRecord(command, record);
    } catch (error) {
      if (started !== state.generation) return;
      if (error.status === 401) requirePairing();
      else if (error instanceof APIError && (error.status < 500 || (error.status === 503 && error.code))) {
        feedback.textContent = error.message;
        feedback.classList.remove("sr-only");
        if (["executor_unavailable", "timed_out"].includes(error.code)) unknown(command, error.message);
      } else {
        unknown(command, "Submission outcome unknown. The command was not retried.");
      }
    } finally {
      if (sending === token) sending = null;
      render();
      if (started === state.generation) void poll();
    }
  }

  picker.addEventListener("change", () => {
    selected = picker.value;
    selectedSlide = selectedRevision = null;
    clearCards();
    feedback.classList.toggle("sr-only", !uncertain.has(selected));
    feedback.textContent = uncertain.has(selected)
      ? `Command outcome unknown. ${recovery}`
      : "";
    render();
    autoPreview();
  });
  retryPreviews.addEventListener("click", () => {
    for (const card of cards.values()) if (card.imageState === "failed") {
      card.imageAttempts = 0;
      queueImage(card);
    }
    imageProgress();
  });
  scan.addEventListener("click", () => { if (!scan.disabled) void poll(); });
  refresh.addEventListener("click", () => void submit("refresh"));
  slides.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const buttons = [...cards.values()].map((card) => card.button);
    if (!buttons.length) return;
    const index = buttons.indexOf(document.activeElement);
    const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
      : Math.max(0, Math.min(buttons.length - 1, index + (event.key === "ArrowLeft" ? -1 : 1)));
    buttons[nextIndex].focus({ preventScroll: true });
    centerCard(buttons[nextIndex]);
  });
  document.addEventListener("conty:connection", () => {
    if (generation !== state.generation) {
      generation = state.generation;
      clear();
    }
    if (!state.connected) listHealthy = false;
    render();
    if (state.connected) void poll();
  }, { signal: events.signal });
  document.addEventListener("visibilitychange", () => {
    render();
    if (!document.hidden) void poll();
  }, { signal: events.signal });
  const resizeObserver = new ResizeObserver(() => {
    if (!active() || !slides.clientWidth || !slides.clientHeight) return;
    const width = Math.max(32, Math.min(slides.clientWidth * 0.72, (slides.clientHeight - 28) * 16 / 9));
    slides.style.setProperty("--slide-width", `${width}px`);
    centerCard(cards.get(centeredSlide)?.button);
  });
  resizeObserver.observe(slides);
  const idleTimer = setInterval(() => { render(); void poll(); }, 2000);
  const commandTimer = setInterval(() => { if (pending) void poll(); }, 250);
  panel.dispose = () => {
    events.abort();
    resizeObserver.disconnect();
    clearInterval(idleTimer);
    clearInterval(commandTimer);
    clearCards();
    generation = -1;
  };
  render();
  return panel;
}
