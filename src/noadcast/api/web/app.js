(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const apiRoot = new URL("api/v1/", location.href);
  const healthUrl = new URL("health", location.href);
  const number = new Intl.NumberFormat();
  const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 4 });
  const state = { token: sessionStorage.getItem("noadcastToken") || "", authRequired: true, view: "overview", podcasts: [], settings: null, episodeOffset: 0, episodeTotal: 0, episodeRequest: 0, podcastRequest: 0, analysisRequest: 0, usageRequest: 0, sessionGeneration: 0, busy: new Set(), settingsBusy: false, timer: null };
  const pageSize = 50;

  function put(id, value) { $(id).textContent = value; }
  function fmt(value) { return number.format(Number(value) || 0); }
  function dollars(value) { return money.format(Number(value) || 0); }
  function bytes(value) {
    if (!Number.isFinite(Number(value))) return "—";
    let size = Number(value), unit = 0;
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    if (size >= 1000) { size /= 1000; unit = 1; }
    if (size >= 1000) { size /= 1000; unit = 2; }
    if (size >= 1000) { size /= 1000; unit = 3; }
    if (size >= 1000) { size /= 1000; unit = 4; }
    if (size >= 1000) { size /= 1000; unit = 5; }
    return `${size.toFixed(unit === 0 ? 0 : size < 10 ? 1 : 0)} ${units[unit]}`;
  }
  function date(value) {
    if (!value) return "—";
    const d = new Date(value);
    return Number.isNaN(d.valueOf()) ? "—" : new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" }).format(d);
  }
  function shortDate(value) {
    if (!value) return "—";
    const d = new Date(value);
    return Number.isNaN(d.valueOf()) ? "—" : new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }).format(d);
  }
  function label(value) { return String(value || "—").replaceAll("_", " "); }
  function el(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }
  function showError(message) {
    put("page-error-text", message);
    $("page-error").hidden = false;
  }
  function clearError() { $("page-error").hidden = true; }
  function messageFor(error) { return error instanceof Error ? error.message : "Could not reach the server."; }
  function showLogin(message = "") {
    stopPolling();
    $("connecting").hidden = true;
    state.sessionGeneration++;
    state.episodeRequest++; state.podcastRequest++; state.analysisRequest++; state.usageRequest++;
    state.token = "";
    state.podcasts = []; state.settings = null; state.episodeOffset = 0; state.episodeTotal = 0;
    sessionStorage.removeItem("noadcastToken");
    $("app").hidden = true;
    $("login").hidden = false;
    put("login-error", message);
    $("login-error").hidden = !message;
    if (message) $("token").focus();
  }
  function showApp() {
    state.sessionGeneration++;
    $("connecting").hidden = true;
    $("login").hidden = true;
    $("app").hidden = false;
    $("sign-out").hidden = !state.authRequired;
    $("token").value = "";
    clearError();
    selectView("overview");
    loadPodcasts().catch((error) => showError(messageFor(error)));
    startPolling();
  }
  async function request(path, options = {}) {
    const generation = state.sessionGeneration;
    const headers = { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) };
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    return timedFetch(new URL(path, apiRoot), { ...options, headers: { ...headers, ...options.headers }, cache: "no-store" }, async (response) => {
      if (generation !== state.sessionGeneration) throw new Error("Session changed.");
      if (response.status === 401) {
        showLogin("Your session expired. Enter the API token again.");
        throw new Error("Authentication required.");
      }
      if (!response.ok) {
        let detail = "";
        try { const body = await response.json(); detail = body.message || body.detail || body.error?.message || ""; } catch (error) { if (error?.name === "AbortError") throw error; }
        throw new Error(detail || `Server returned ${response.status}.`);
      }
      const body = response.status === 204 ? null : await response.json();
      if (generation !== state.sessionGeneration) throw new Error("Session changed.");
      return body;
    });
  }
  async function timedFetch(url, options, read) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try { return await read(await fetch(url, { ...options, signal: controller.signal })); }
    catch (error) { if (error?.name === "AbortError") throw new Error("Request timed out."); throw error; }
    finally { clearTimeout(timer); }
  }
  async function connect(token) {
    const generation = state.sessionGeneration;
    await timedFetch(new URL("session", apiRoot), { headers: token ? { Authorization: `Bearer ${token}` } : {}, cache: "no-store" }, async (response) => {
      if (!response.ok) throw new Error(response.status === 401 ? "That token was not accepted." : `Server returned ${response.status}.`);
      await response.json();
      if (generation !== state.sessionGeneration) throw new Error("Session changed.");
    });
    state.token = token;
    if (token) sessionStorage.setItem("noadcastToken", token);
    showApp();
  }
  async function boot() {
    try {
      const health = await timedFetch(healthUrl, { cache: "no-store" }, async (response) => {
        if (!response.ok) throw new Error(`Health check returned ${response.status}.`);
        return response.json();
      });
      state.authRequired = health.authRequired !== false;
      put("server-label", `Server ${health.version || "connected"}`);
      if (!state.authRequired || state.token) {
        try { await connect(state.authRequired ? state.token : ""); return; }
        catch (error) { if (state.authRequired && messageFor(error).includes("token")) { showLogin(messageFor(error)); return; } throw error; }
      }
      showLogin();
    } catch (error) { showLogin(`Could not connect: ${messageFor(error)}`); }
  }
  function stopPolling() { if (state.timer) clearInterval(state.timer); state.timer = null; }
  function startPolling() {
    stopPolling();
    state.timer = setInterval(() => {
      if (!document.hidden && state.view === "overview" && !$("app").hidden) loadOverview();
    }, 30000);
  }
  function selectView(name) {
    state.view = name;
    document.querySelectorAll(".nav-item").forEach((button) => {
      const active = button.dataset.view === name;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    document.querySelectorAll(".view").forEach((view) => { view.hidden = view.id !== `view-${name}`; });
    clearError();
    refreshView();
    $("main-content").focus?.();
  }
  function refreshView() {
    if (state.view === "overview") loadOverview();
    else if (state.view === "episodes") loadEpisodes();
    else if (state.view === "usage") loadUsage();
    else if (state.view === "analysis") loadAnalysis();
  }
  async function loadOverview() {
    try {
      const data = await request("admin/stats");
      const counts = data.episodesByState || {};
      put("stat-episodes", fmt(Object.values(counts).reduce((a, b) => a + Number(b || 0), 0)));
      put("stat-episodes-detail", `${fmt(counts.ready)} ready · ${fmt(counts.failed)} failed`);
      put("stat-podcasts", fmt(data.podcasts));
      put("stat-audio", bytes(data.audio?.storedBytes));
      const audioCounts = data.audio?.byState || {};
      put("stat-audio-detail", `${fmt(audioCounts.present)} files available`);
      put("stat-cost", dollars((data.spend30d || []).reduce((sum, row) => sum + Number(row.costUsd || 0), 0)));
      put("storage-used", bytes(data.disk?.usedBytes));
      put("storage-total", `used of ${bytes(data.disk?.totalBytes)} total volume`);
      put("storage-audio", bytes(data.audio?.storedBytes));
      put("storage-free", bytes(data.disk?.freeBytes));
      const total = Number(data.disk?.totalBytes || 0), used = Number(data.disk?.usedBytes || 0);
      const percentage = total > 0 ? Math.max(0, Math.min(100, used / total * 100)) : 0;
      $("storage-meter").setAttribute("aria-valuenow", String(Math.round(percentage)));
      $("storage-meter").querySelector("span").style.width = `${percentage}%`;
      renderQueues(data.queues || [], data.pool);
      renderFailures(data.recentFailures || []);
      put("last-updated", `Updated ${shortDate(data.serverTime)}`);
      clearError();
    } catch (error) { if (!$("app").hidden) showError(`Overview: ${messageFor(error)}`); }
  }
  function renderQueues(queues, pool) {
    const holder = $("queues"); holder.replaceChildren();
    if (!queues.length && !pool) holder.append(el("p", "empty", "No processing queues reported."));
    queues.forEach((queue) => {
      const row = el("div", "queue-row");
      row.append(el("span", "queue-name", label(queue.kind)), el("span", "queue-numbers", `${fmt(queue.running)} running · ${fmt(queue.pending)} pending`), el("span", "queue-count", `${fmt(queue.available)} ready`));
      holder.append(row);
    });
    if (pool) {
      const row = el("div", "queue-row");
      row.append(el("span", "queue-name", "Transcription workers"), el("span", "queue-numbers", `${fmt(pool.busy)} busy · ${fmt(pool.ready)} ready`), el("span", "queue-count", `${fmt(pool.alive)}/${fmt(pool.workers)}`));
      holder.append(row);
    }
  }
  function renderFailures(failures) {
    const holder = $("failures"); holder.replaceChildren();
    if (!failures.length) { holder.append(el("p", "empty", "No recent failures.")); return; }
    failures.slice(0, 5).forEach((failure) => {
      const row = el("div", "failure-row"), info = el("div");
      info.append(el("strong", "", `${label(failure.kind)} · job ${failure.jobId}`), el("p", "", failure.error || "Unknown error"));
      row.append(info, el("time", "", shortDate(failure.at)));
      holder.append(row);
    });
  }
  async function loadPodcasts() {
    if (state.busy.size) return;
    const serial = ++state.podcastRequest;
    const data = await request("podcasts");
    if (serial !== state.podcastRequest) return;
    state.podcasts = data.items || [];
    const current = $("episode-podcast").value;
    $("episode-podcast").replaceChildren(new Option("All podcasts", ""));
    state.podcasts.slice().sort((a, b) => a.title.localeCompare(b.title)).forEach((podcast) => $("episode-podcast").add(new Option(podcast.title, podcast.id)));
    $("episode-podcast").value = current;
    renderPodcasts();
  }
  async function loadEpisodes() {
    const serial = ++state.episodeRequest;
    const params = new URLSearchParams({ limit: String(pageSize), offset: String(state.episodeOffset) });
    const query = $("episode-search").value.trim(), podcast = $("episode-podcast").value, status = $("episode-state").value;
    if (query) params.set("q", query);
    if (podcast) params.set("podcastId", podcast);
    if (status) params.set("state", status);
    try {
      const data = await request(`episodes?${params}`);
      if (serial !== state.episodeRequest) return;
      state.episodeTotal = Number(data.total || 0);
      renderEpisodes(data.items || []);
      clearError();
    } catch (error) { if (serial === state.episodeRequest && !$("app").hidden) showError(`Episodes: ${messageFor(error)}`); }
  }
  function renderEpisodes(items) {
    const holder = $("episode-rows"); holder.replaceChildren();
    put("episode-count", `${fmt(state.episodeTotal)} episodes`);
    if (!items.length) {
      const row = el("tr"), cell = el("td", "empty", "No episodes match these filters."); cell.colSpan = 5; row.append(cell); holder.append(row);
    }
    items.forEach((episode) => {
      const row = el("tr"), first = el("td"), title = el("span", "title", episode.title || "Untitled episode");
      const podcast = state.podcasts.find((item) => item.id === episode.podcastId);
      first.append(title, el("span", "subline", podcast?.title || `Podcast ${episode.podcastId}`));
      const status = el("span", `status-pill ${episode.state === "ready" ? "ready" : episode.state === "failed" ? "failed" : ["downloading", "transcribing", "classifying"].includes(episode.state) ? "active" : ""}`, label(episode.state));
      const statusCell = el("td"); statusCell.append(status);
      row.append(first, el("td", "", date(episode.publishedAt)), statusCell, el("td", "", label(episode.audioState)), el("td", "", fmt(episode.adMarkers?.length || 0)));
      holder.append(row);
    });
    const first = state.episodeTotal ? state.episodeOffset + 1 : 0;
    put("episode-range", `${fmt(first)}–${fmt(state.episodeOffset + items.length)} of ${fmt(state.episodeTotal)}`);
    $("episodes-prev").disabled = state.episodeOffset === 0;
    $("episodes-next").disabled = state.episodeOffset + pageSize >= state.episodeTotal;
  }
  async function loadUsage() {
    const serial = ++state.usageRequest;
    try {
      const data = await request(`usage?days=${$("usage-period").value}`);
      if (serial !== state.usageRequest) return;
      const totals = data.totals || {};
      put("usage-calls", fmt(totals.calls)); put("usage-input", fmt(totals.inputTokens));
      put("usage-output", fmt(Number(totals.thoughtTokens || 0) + Number(totals.outputTokens || 0)));
      put("usage-cost", dollars(totals.costUsd));
      renderUsageRows("usage-days", (data.days || []).slice().sort((a, b) => b.date.localeCompare(a.date)), "No usage during this period.", (row) => row.date);
      renderUsageRows("usage-models", data.byModel || [], "No model usage during this period.", (row) => row.model || row.provider || "Unknown model");
      clearError();
    } catch (error) { if (serial === state.usageRequest && !$("app").hidden) showError(`Usage: ${messageFor(error)}`); }
  }
  function renderUsageRows(id, items, empty, firstValue) {
    const holder = $(id); holder.replaceChildren();
    if (!items.length) { const row = el("tr"), cell = el("td", "empty", empty); cell.colSpan = 6; row.append(cell); holder.append(row); return; }
    items.forEach((item) => {
      const row = el("tr"); row.append(el("td", "", firstValue(item)));
      for (const key of ["calls", "inputTokens", "thoughtTokens", "outputTokens"]) row.append(el("td", "num", fmt(item[key])));
      row.append(el("td", "num", dollars(item.costUsd))); holder.append(row);
    });
  }
  async function loadAnalysis() {
    if (state.settingsBusy || state.busy.size) return;
    const serial = ++state.analysisRequest;
    try {
      const [settings] = await Promise.all([request("settings"), loadPodcasts()]);
      if (serial !== state.analysisRequest) return;
      state.settings = settings;
      $("global-analysis").checked = !!settings.adAnalysisEnabled;
      $("global-analysis").disabled = false;
      put("global-status", settings.adAnalysisEnabled ? "Enabled for future analysis." : "Paused for all podcasts.");
      $("model-select").value = settings.classifierModel;
      $("model-select").disabled = false;
      $("save-model").disabled = true;
      put("model-status", "Current model saved.");
      clearError();
    } catch (error) { if (!$("app").hidden) showError(`Analysis settings: ${messageFor(error)}`); }
  }
  function renderPodcasts() {
    const holder = $("podcast-list"), query = $("podcast-search").value.trim().toLocaleLowerCase(), filter = $("podcast-filter").value;
    const excluded = state.podcasts.filter((podcast) => podcast.adAnalysisEnabled === false);
    put("excluded-count", `${fmt(excluded.length)} excluded`);
    const visible = state.podcasts.filter((podcast) => {
      if (filter === "excluded" && podcast.adAnalysisEnabled) return false;
      if (filter === "included" && !podcast.adAnalysisEnabled) return false;
      return !query || `${podcast.title} ${podcast.author || ""}`.toLocaleLowerCase().includes(query);
    }).sort((a, b) => a.title.localeCompare(b.title));
    holder.replaceChildren();
    if (!visible.length) { holder.append(el("p", "empty", "No podcasts match this filter.")); return; }
    visible.forEach((podcast) => {
      const row = el("div", "podcast-row"), info = el("div"), control = el("label", "switch-control"), checkbox = el("input"), track = el("span", "switch-track"), status = el("p", "podcast-status");
      checkbox.type = "checkbox"; checkbox.checked = !podcast.adAnalysisEnabled; checkbox.disabled = state.busy.has(podcast.id);
      checkbox.setAttribute("aria-label", `Exclude ${podcast.title} from ad detection`);
      checkbox.addEventListener("change", () => savePodcast(podcast, checkbox, status));
      status.hidden = true;
      info.append(el("div", "podcast-title", podcast.title), el("div", "podcast-meta", `${fmt(podcast.episodeCount)} episodes${podcast.author ? ` · ${podcast.author}` : ""}`), status);
      control.append(checkbox, track); row.append(info, control); holder.append(row);
    });
  }
  async function savePodcast(podcast, checkbox, status) {
    const excluded = checkbox.checked;
    state.podcastRequest++; state.analysisRequest++;
    checkbox.disabled = true; state.busy.add(podcast.id);
    status.hidden = false; status.classList.remove("error"); status.textContent = "Saving…";
    try {
      const updated = await request(`podcasts/${podcast.id}`, { method: "PATCH", body: JSON.stringify({ adAnalysisEnabled: !excluded }) });
      const index = state.podcasts.findIndex((item) => item.id === podcast.id);
      if (index >= 0) state.podcasts[index] = updated;
      state.busy.delete(podcast.id);
      renderPodcasts();
    } catch (error) {
      checkbox.checked = !excluded;
      status.textContent = `Could not save: ${messageFor(error)}`;
      status.classList.add("error");
    } finally { state.busy.delete(podcast.id); checkbox.disabled = false; }
  }
  async function saveGlobal() {
    const checkbox = $("global-analysis"), enabled = checkbox.checked;
    state.analysisRequest++;
    state.settingsBusy = true;
    $("model-select").disabled = true; $("save-model").disabled = true;
    checkbox.disabled = true; put("global-status", "Saving…"); $("global-status").classList.remove("error");
    try {
      state.settings = await request("settings", { method: "PATCH", body: JSON.stringify({ adAnalysisEnabled: enabled }) });
      checkbox.checked = !!state.settings.adAnalysisEnabled;
      put("global-status", checkbox.checked ? "Enabled for future analysis." : "Paused for all podcasts.");
    } catch (error) {
      checkbox.checked = !enabled;
      put("global-status", `Could not save: ${messageFor(error)}`); $("global-status").classList.add("error");
    } finally { checkbox.disabled = false; $("model-select").disabled = false; $("save-model").disabled = $("model-select").value === state.settings?.classifierModel; state.settingsBusy = false; }
  }
  async function saveModel() {
    const selected = $("model-select").value;
    state.analysisRequest++;
    state.settingsBusy = true;
    $("global-analysis").disabled = true;
    $("save-model").disabled = true; $("model-select").disabled = true;
    put("model-status", "Saving…"); $("model-status").classList.remove("error");
    try {
      state.settings = await request("settings", { method: "PATCH", body: JSON.stringify({ classifierModel: selected }) });
      $("model-select").value = state.settings.classifierModel;
      put("model-status", "Current model saved.");
    } catch (error) {
      $("model-select").value = state.settings?.classifierModel || selected;
      put("model-status", `Could not save: ${messageFor(error)}`); $("model-status").classList.add("error");
    } finally { $("global-analysis").disabled = false; $("model-select").disabled = false; $("save-model").disabled = $("model-select").value === state.settings?.classifierModel; state.settingsBusy = false; }
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("login-form").querySelector("button"); button.disabled = true;
    try { await connect($("token").value.trim()); }
    catch (error) { put("login-error", messageFor(error)); $("login-error").hidden = false; }
    finally { button.disabled = false; }
  });
  $("sign-out").addEventListener("click", () => showLogin());
  $("refresh").addEventListener("click", refreshView);
  $("retry").addEventListener("click", refreshView);
  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => selectView(button.dataset.view)));
  let searchTimer;
  $("episode-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.episodeOffset = 0; loadEpisodes(); }, 300); });
  for (const id of ["episode-podcast", "episode-state"]) $(id).addEventListener("change", () => { state.episodeOffset = 0; loadEpisodes(); });
  $("episodes-prev").addEventListener("click", () => { state.episodeOffset = Math.max(0, state.episodeOffset - pageSize); loadEpisodes(); });
  $("episodes-next").addEventListener("click", () => { state.episodeOffset += pageSize; loadEpisodes(); });
  $("usage-period").addEventListener("change", loadUsage);
  $("global-analysis").addEventListener("change", saveGlobal);
  $("model-select").addEventListener("change", () => { $("save-model").disabled = $("model-select").value === state.settings?.classifierModel; });
  $("save-model").addEventListener("click", saveModel);
  $("podcast-search").addEventListener("input", renderPodcasts);
  $("podcast-filter").addEventListener("change", renderPodcasts);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.view === "overview" && !$("app").hidden) loadOverview(); });
  boot();
})();
