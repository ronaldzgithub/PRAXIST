"use strict";

const controlToken = document.querySelector('meta[name="praxist-control-token"]')?.content || "";
const bootstrapReadOnly = document.querySelector('meta[name="praxist-read-only"]')?.content === "true";

const app = {
  overview: null,
  detail: null,
  actions: [],
  selectedKey: null,
  filter: "all",
  search: "",
  readOnly: bootstrapReadOnly,
  refreshing: false,
  timer: null,
  actionStates: new Map(),
  confirmCallback: null,
};

const byId = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");
const display = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : String(value);
const clamp = (value, low, high) => Math.max(low, Math.min(high, Number(value) || 0));
const stateClass = (state) => String(state || "unknown").toLowerCase().replaceAll(/[^a-z0-9_-]/g, "-");

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    headers["X-Praxist-Control"] = controlToken;
  }
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) {
    throw new Error(payload?.error || `Dashboard request failed (${response.status})`);
  }
  return payload;
}

function setConnection(status, subtitle) {
  const dot = byId("connection-dot");
  dot.className = `pulse-dot ${status}`;
  byId("connection-label").textContent = status === "online" ? "Live sampler" : status === "offline" ? "Disconnected" : "Connecting";
  byId("last-sync").textContent = subtitle;
}

async function refresh({ forceDetail = false } = {}) {
  if (app.refreshing) return;
  app.refreshing = true;
  try {
    const [overview, actions] = await Promise.all([
      api("/api/v1/overview"),
      api("/api/v1/actions"),
    ]);
    app.overview = overview;
    app.actions = actions;
    app.readOnly = Boolean(overview.control?.read_only);
    selectDefaultRun();
    renderOverview();
    renderActions();
    if (app.selectedKey && (forceDetail || !app.detail || app.detail.run?.key !== app.selectedKey)) {
      await refreshDetail();
    } else if (app.selectedKey) {
      await refreshDetail();
    }
    setConnection("online", `Synced ${formatClock(overview.generated_at)}`);
    scheduleRefresh(overview.refresh_after_ms || 2000);
  } catch (error) {
    setConnection("offline", error.message);
    showToast("Dashboard disconnected", error.message, true);
    scheduleRefresh(4000);
  } finally {
    app.refreshing = false;
  }
}

function scheduleRefresh(milliseconds) {
  clearTimeout(app.timer);
  app.timer = setTimeout(() => refresh(), clamp(milliseconds, 1000, 15000));
}

async function refreshDetail() {
  if (!app.selectedKey) return;
  const selected = app.selectedKey;
  try {
    const detail = await api(`/api/v1/runs/${encodeURIComponent(selected)}`);
    if (selected !== app.selectedKey) return;
    app.detail = detail;
    renderDetail();
  } catch (error) {
    if (selected === app.selectedKey) {
      app.detail = null;
      renderDetail();
      showToast("Run detail unavailable", error.message, true);
    }
  }
}

function selectDefaultRun() {
  const runs = app.overview?.runs || [];
  if (app.selectedKey && runs.some((run) => run.key === app.selectedKey)) return;
  const active = runs.find((run) => isActive(run));
  app.selectedKey = (active || runs[0])?.key || null;
  app.detail = null;
}

function renderOverview() {
  const overview = app.overview;
  if (!overview) return;
  const counts = overview.counts || {};
  byId("metric-active").textContent = display(counts.active, "0");
  byId("metric-total").textContent = `${display(counts.total, "0")} known`;
  byId("metric-generations").textContent = display(counts.committed_generations, "0");
  byId("metric-findings").textContent = compactNumber(counts.findings || 0);
  byId("metric-attention").textContent = display(counts.attention, "0");
  document.querySelector(".attention-card")?.classList.toggle("has-attention", Number(counts.attention) > 0);
  byId("host-line").textContent = `${overview.host?.hostname || "local host"} · PID ${overview.host?.pid || "—"} · ${overview.host?.cpu_count || "?"} logical CPUs`;
  byId("run-count-badge").textContent = display(counts.total, "0");
  renderMode();
  renderWarnings();
  renderRunList();
  renderHost();
}

function renderMode() {
  const pill = byId("mode-pill");
  pill.classList.toggle("read-only", app.readOnly);
  pill.innerHTML = `<span></span>${app.readOnly ? "Read-only session" : "Control enabled"}`;
  document.querySelectorAll(".control-only").forEach((element) => {
    element.classList.toggle("hidden", app.readOnly);
  });
}

function renderWarnings() {
  const warnings = app.overview?.warnings || [];
  const strip = byId("warning-strip");
  if (!warnings.length) {
    strip.classList.add("hidden");
    return;
  }
  byId("warning-text").textContent = warnings.slice(0, 3).join(" · ");
  strip.classList.remove("hidden");
}

function filteredRuns() {
  const query = app.search.toLowerCase();
  return (app.overview?.runs || []).filter((run) => {
    const haystack = [run.run_id, run.task_name, run.task_path, run.model, run.model_provider_ref, run.state, run.phase].join(" ").toLowerCase();
    if (query && !haystack.includes(query)) return false;
    if (app.filter === "active") return isActive(run);
    if (app.filter === "attention") return Boolean(run.warnings?.length);
    if (app.filter === "offline") return !isActive(run);
    return true;
  });
}

function renderRunList() {
  const runs = filteredRuns();
  const list = byId("run-list");
  if (!runs.length) {
    list.innerHTML = `<div class="list-empty">No runs match this view.<br>Registry, process-only, stale, and remote rows all appear here when detected.</div>`;
    return;
  }
  list.innerHTML = runs.map((run) => {
    const progress = run.progress || {};
    const percent = clamp(progress.percent ?? 0, 0, 100);
    const title = run.task_name || basename(run.task_path) || run.run_id || `PID ${run.pid}`;
    const model = run.model || run.model_provider_ref || run.source;
    const generation = progress.max_generations
      ? `gen ${display(progress.current_generation, "?")} / ${progress.max_generations}`
      : `gen ${display(progress.current_generation, "?")}`;
    return `
      <button type="button" class="run-card ${run.key === app.selectedKey ? "selected" : ""}" data-run-key="${esc(run.key)}">
        <span class="run-card-head">
          <i class="state-dot ${stateClass(run.state)}"></i>
          <strong class="run-card-title">${esc(title)}</strong>
          <small class="run-card-state">${esc(run.state)}</small>
        </span>
        <span class="run-card-meta"><span>${esc(run.run_id || `PID ${run.pid}`)}</span><span>${esc(generation)}</span></span>
        <span class="run-progress"><span style="width:${percent}%"></span></span>
        <span class="run-card-foot"><span>${esc(model)}</span>${peerPips(run.peer_health_summary)}</span>
      </button>`;
  }).join("");
}

function peerPips(summary) {
  if (!summary || typeof summary !== "object") return `<span class="peer-pips"><i></i></span>`;
  const pips = [];
  for (const color of ["red", "yellow", "green", "gray"]) {
    const count = Math.min(5, Number(summary[color] || 0));
    for (let index = 0; index < count; index += 1) pips.push(`<i class="${esc(color)}"></i>`);
  }
  return `<span class="peer-pips">${pips.join("") || "<i></i>"}</span>`;
}

function renderHost() {
  const host = app.overview?.host || {};
  byId("host-load").textContent = display(host.loadavg);
  byId("host-memory").textContent = display(host.memory);
  byId("host-status").textContent = "live";
  byId("host-status").classList.add("healthy");
  const loads = String(host.loadavg || "").split(/\s+/).map(Number).filter(Number.isFinite);
  document.querySelectorAll("#load-bars i").forEach((bar, index) => {
    const load = loads[index] || 0;
    const cpu = Math.max(1, Number(host.cpu_count || 1));
    bar.style.height = `${Math.max(10, Math.min(100, load * 100 / cpu))}%`;
  });
  const gpus = host.gpus || [];
  byId("gpu-list").innerHTML = gpus.length
    ? gpus.map((gpu) => `<div class="gpu-row">${esc(gpu)}</div>`).join("")
    : `<div class="gpu-row">No accelerator telemetry exposed</div>`;
}

function renderDetail() {
  const detail = app.detail;
  byId("empty-detail").classList.toggle("hidden", Boolean(detail));
  byId("detail-content").classList.toggle("hidden", !detail);
  if (!detail) return;
  const run = detail.run || {};
  const progress = run.progress || {};
  const title = run.task_name || basename(run.task_path) || run.run_id || `PID ${run.pid}`;
  byId("detail-state").textContent = display(run.state);
  byId("detail-state-dot").className = `state-dot ${stateClass(run.state)}`;
  byId("detail-source").textContent = display(run.source);
  byId("detail-title").textContent = title;
  byId("detail-path").textContent = run.run_dir || run.task_path || "Path unavailable";
  byId("detail-path").dataset.copy = run.run_dir || run.task_path || "";
  byId("detail-phase").textContent = display(run.phase);
  const current = display(progress.current_generation, "?");
  const maximum = progress.max_generations ? ` of ${progress.max_generations}` : "";
  byId("detail-generation").textContent = `Generation ${current}${maximum}`;
  byId("progress-fill").style.width = `${clamp(progress.percent ?? 0, 0, 100)}%`;
  byId("boundary-count").textContent = `${display(progress.committed_boundaries, "0")} committed`;
  markBoundary("boundary-stop", progress.boundary?.stop_signal);
  markBoundary("boundary-results", progress.boundary?.generation_results);
  markBoundary("boundary-commit", progress.boundary?.committed);
  const warnings = [...(run.warnings || []), ...(detail.warnings || [])];
  const warning = byId("detail-warning");
  warning.textContent = [...new Set(warnings)].join(" · ");
  warning.classList.toggle("hidden", !warning.textContent);
  renderDetailMetrics(run);
  renderPeers(detail.peers || []);
  renderEvidence(detail.frontier || {}, detail.gems || {}, run);
  renderScheduler(detail.resource_scheduler || {});
  renderLogs(detail.recent_logs || []);
  renderArtifacts(detail);
  const capabilities = run.capabilities || {};
  byId("stop-button").classList.toggle("hidden", app.readOnly || !capabilities.can_stop);
  byId("resume-button").classList.toggle("hidden", app.readOnly || !capabilities.can_resume);
  byId("copy-monitor-button").disabled = !capabilities.can_monitor;
}

function markBoundary(id, done) { byId(id).classList.toggle("done", Boolean(done)); }

function renderDetailMetrics(run) {
  const progress = run.progress || {};
  const peers = run.peer_health_summary || {};
  const best = run.best_mature_result || {};
  const cards = [
    ["PEERS", sumObject(peers), healthSummary(peers)],
    ["FINDINGS", display(progress.findings_total, "0"), `${display(progress.variants_total, "0")} variants`],
    ["FRONTIER", display(progress.frontier_candidates, "0"), `${display(progress.variants_above_baseline, "0")} above baseline`],
    ["GEMS", display(progress.gems_count, "0"), `strategy ${display(progress.strategy, "—")}`],
    [best.metric_name || "BEST MATURE", display(best.metric_value), best.variant_name || "no mature result"],
  ];
  byId("detail-metrics").innerHTML = cards.map(([label, value, note]) => `
    <article class="detail-metric"><small>${esc(label)}</small><strong title="${esc(value)}">${esc(value)}</strong><em title="${esc(note)}">${esc(note)}</em></article>
  `).join("");
}

function renderPeers(peers) {
  const target = byId("tab-peers");
  if (!peers.length) {
    target.innerHTML = `<div class="data-empty">No bounded peer health rows are available for this run.</div>`;
    return;
  }
  target.innerHTML = `<table class="peer-table"><thead><tr><th>Peer</th><th>Health</th><th>State</th><th>Active variant</th><th>Best metric</th><th>Updated</th></tr></thead><tbody>${peers.map((peer) => `
    <tr>
      <td><span class="peer-name"><i class="state-dot ${stateClass(peer.health)}"></i>${esc(peer.peer_id || peer.peer_name || "peer")}</span></td>
      <td class="health-text ${stateClass(peer.health)}">${esc(display(peer.health))}</td>
      <td>${esc(display(peer.research_state))}</td>
      <td class="table-truncate" title="${esc(peer.active_variant || "")}">${esc(display(peer.active_variant))}</td>
      <td>${esc(display(peer.best_metric_value))}</td>
      <td>${esc(relativeTime(peer.last_updated_utc))}</td>
    </tr>`).join("")}</tbody></table>`;
}

function renderEvidence(frontier, gems, run) {
  const lanes = frontier.lanes || {};
  const laneCards = Object.entries(lanes).map(([lane, value]) => evidenceCard(lane, value.entries || [], value.count || 0));
  const gemCard = evidenceCard("Gems", gems.entries || [], gems.count || 0);
  const validation = run.best_validation_signal || {};
  const validationCard = Object.keys(validation).length ? `
    <article class="evidence-card"><header><h3>Validation signal</h3><span>follow-up only</span></header>
      <div class="evidence-entry"><strong>${esc(validation.variant_name || "unknown")}</strong><small>${esc(display(validation.metric_name))} ${esc(display(validation.metric_value))} · ${esc(display(validation.validation_reason))}</small></div>
    </article>` : "";
  byId("tab-evidence").innerHTML = laneCards.length || (gems.entries || []).length || validationCard
    ? `<div class="evidence-grid">${laneCards.join("")}${gemCard}${validationCard}</div>`
    : `<div class="data-empty">No canonical frontier or committed Gems entries are available yet.</div>`;
}

function evidenceCard(name, entries, count) {
  return `<article class="evidence-card"><header><h3>${esc(name)}</h3><span>${esc(count)} entries</span></header>${entries.length ? entries.map((entry) => `
    <div class="evidence-entry"><strong>${esc(entry.variant_name || entry.finding_id || "unnamed")}</strong><small>${esc(display(entry.metric_name))} ${esc(display(entry.metric_value))} · gen ${esc(display(entry.generation_id))} · ${esc(display(entry.evidence_stage))}</small></div>
  `).join("") : `<div class="evidence-entry"><small>No materialized entries</small></div>`}</article>`;
}

function renderScheduler(scheduler) {
  const target = byId("tab-scheduler");
  if (!Object.keys(scheduler).length) {
    target.innerHTML = `<div class="data-empty">No resource scheduler status is materialized for this run.</div>`;
    return;
  }
  const cards = [
    ["Queue", { queued: scheduler.queued, running: scheduler.running, completed: scheduler.completed, failed: scheduler.failed, rejected: scheduler.rejected }],
    ["Capacity", { concurrency_limit: scheduler.concurrency_limit, admission_closed: scheduler.admission_closed, peer_capacity_blocked: scheduler.peer_capacity_blocked, release_pending: scheduler.release_pending }],
    ["Activity", scheduler.running_activity || scheduler.work_class_mix || {}],
    ["Supply", scheduler.supply_stats || scheduler.queue_blocked_reasons || {}],
  ];
  target.innerHTML = cards.map(([title, value]) => jsonCard(title, value)).join("");
}

function renderLogs(lines) {
  const target = byId("tab-logs");
  target.innerHTML = `<pre class="log-view"></pre>`;
  target.querySelector("pre").textContent = lines.length ? lines.join("\n") : "No recent bounded log tail is available.";
}

function renderArtifacts(detail) {
  const target = byId("tab-artifacts");
  const resume = detail.resume_plan || {};
  target.innerHTML = [
    jsonCard("Runtime summary", detail.runtime_summary || {}),
    jsonCard("Resume plan", resume),
    jsonCard("Orchestrator projection", compactObject(detail.orchestrator_status || {}, 24)),
  ].join("");
}

function jsonCard(title, value) {
  return `<article class="json-card"><h3>${esc(title)}</h3><pre></pre></article>`;
}

function hydrateJsonCards(container = document) {
  container.querySelectorAll(".json-card").forEach((card) => {
    const title = card.querySelector("h3")?.textContent;
    let value = {};
    if (title === "Runtime summary") value = app.detail?.runtime_summary || {};
    else if (title === "Resume plan") value = app.detail?.resume_plan || {};
    else if (title === "Orchestrator projection") value = compactObject(app.detail?.orchestrator_status || {}, 24);
    else if (title === "Queue") {
      const s = app.detail?.resource_scheduler || {};
      value = { queued: s.queued, running: s.running, completed: s.completed, failed: s.failed, rejected: s.rejected };
    } else if (title === "Capacity") {
      const s = app.detail?.resource_scheduler || {};
      value = { concurrency_limit: s.concurrency_limit, admission_closed: s.admission_closed, peer_capacity_blocked: s.peer_capacity_blocked, release_pending: s.release_pending };
    } else if (title === "Activity") value = app.detail?.resource_scheduler?.running_activity || app.detail?.resource_scheduler?.work_class_mix || {};
    else if (title === "Supply") value = app.detail?.resource_scheduler?.supply_stats || app.detail?.resource_scheduler?.queue_blocked_reasons || {};
    card.querySelector("pre").textContent = JSON.stringify(value, null, 2);
  });
}

function renderActions() {
  const list = byId("action-list");
  const active = app.actions.filter((action) => ["queued", "running"].includes(action.status)).length;
  byId("action-running").textContent = active;
  for (const action of app.actions) {
    const previous = app.actionStates.get(action.action_id);
    if (previous && previous !== action.status && ["succeeded", "failed"].includes(action.status)) {
      showToast(action.status === "succeeded" ? "Action completed" : "Action failed", action.label + (action.error ? ` · ${action.error}` : ""), action.status === "failed");
    }
    app.actionStates.set(action.action_id, action.status);
  }
  list.innerHTML = app.actions.length ? app.actions.slice(0, 20).map((action) => `
    <article class="action-item">
      <i class="action-status ${stateClass(action.status)}"></i>
      <div class="action-copy"><strong>${esc(action.label)}</strong><small>${esc(action.status)} · ${esc(relativeTime(action.created_at))}</small>${action.error ? `<em>${esc(action.error)}</em>` : ""}</div>
    </article>`).join("") : `<div class="data-empty">No dashboard control actions yet.</div>`;
}

async function submitAction(kind, payload) {
  try {
    const record = await api(`/api/v1/actions/${encodeURIComponent(kind)}`, { method: "POST", body: JSON.stringify(payload) });
    app.actions.unshift(record);
    app.actionStates.set(record.action_id, record.status);
    renderActions();
    showToast("Action queued", record.label, false);
    scheduleRefresh(500);
    return true;
  } catch (error) {
    showToast("Action rejected", error.message, true);
    return false;
  }
}

function launchPayload(form) {
  const data = new FormData(form);
  const payload = {};
  for (const key of ["task_path", "agent_system", "runtime", "model_provider", "model", "strategy", "config_file", "run_dir"]) {
    const value = String(data.get(key) || "").trim();
    if (value) payload[key] = value;
  }
  for (const key of ["cohort", "generations", "startup_timeout"]) {
    const value = String(data.get(key) || "").trim();
    if (value) payload[key] = Number(value);
  }
  payload.codex_native = data.get("codex_native") === "on";
  payload.server = data.get("server") === "on";
  return payload;
}

function openConfirmation({ title, eyebrow = "LIFECYCLE ACTION", message, phrase, note = "", submitLabel = "Confirm", callback }) {
  byId("confirm-title").textContent = title;
  byId("confirm-eyebrow").textContent = eyebrow;
  byId("confirm-message").textContent = message;
  byId("confirm-phrase").textContent = phrase;
  byId("confirm-note").textContent = note;
  byId("confirm-submit").textContent = submitLabel;
  byId("confirm-input").value = "";
  app.confirmCallback = async () => callback(byId("confirm-input").value);
  byId("confirm-dialog").showModal();
  setTimeout(() => byId("confirm-input").focus(), 50);
}

function selectedRun() { return app.detail?.run || null; }

function wireEvents() {
  byId("refresh-button").addEventListener("click", () => refresh({ forceDetail: true }));
  byId("warning-dismiss").addEventListener("click", () => byId("warning-strip").classList.add("hidden"));
  byId("run-search").addEventListener("input", (event) => { app.search = event.target.value; renderRunList(); });
  document.querySelectorAll(".filter").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll(".filter").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    app.filter = button.dataset.filter;
    renderRunList();
  }));
  byId("run-list").addEventListener("click", async (event) => {
    const card = event.target.closest("[data-run-key]");
    if (!card) return;
    app.selectedKey = card.dataset.runKey;
    app.detail = null;
    renderRunList();
    renderDetail();
    await refreshDetail();
  });
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
    document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${tab.dataset.tab}`));
    hydrateJsonCards(byId(`tab-${tab.dataset.tab}`));
  }));
  document.querySelectorAll("[data-close-dialog]").forEach((button) => button.addEventListener("click", () => byId(button.dataset.closeDialog).close()));
  byId("launch-button").addEventListener("click", () => byId("launch-dialog").showModal());
  byId("launch-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const ok = await submitAction("start", launchPayload(event.currentTarget));
    if (ok) byId("launch-dialog").close();
  });
  byId("validate-button").addEventListener("click", async () => {
    const payload = launchPayload(byId("launch-form"));
    if (!payload.task_path) { showToast("Task path required", "Enter an absolute task project path first.", true); return; }
    const ok = await submitAction("resolve", payload);
    if (ok) byId("launch-dialog").close();
  });
  byId("detail-path").addEventListener("click", () => copyText(byId("detail-path").dataset.copy, "Run path copied"));
  byId("copy-monitor-button").addEventListener("click", () => {
    const run = selectedRun();
    if (!run) return;
    const command = run.run_id ? `praxist --monitor --run-id ${shellQuote(run.run_id)}` : `praxist --monitor --run-dir ${shellQuote(run.run_dir)}`;
    copyText(command, "Monitor command copied");
  });
  byId("stop-button").addEventListener("click", () => {
    const run = selectedRun();
    if (!run?.run_id) return;
    openConfirmation({
      title: `Stop ${run.run_id}`,
      message: "Praxist will close new admission, signal only identity-verified run-owned processes, wait for a clean drain, and escalate only if needed.",
      phrase: run.run_id,
      note: "The dashboard and other runs remain active.",
      submitLabel: "Stop this run",
      callback: async (typed) => {
        if (typed !== run.run_id) { showToast("Confirmation mismatch", "Type the exact run id.", true); return false; }
        return submitAction("stop", { run_id: run.run_id, confirm_run_id: typed, grace: 300 });
      },
    });
  });
  byId("resume-button").addEventListener("click", () => {
    const run = selectedRun();
    if (!run) return;
    const target = run.run_id || run.run_dir;
    const resume = app.detail?.resume_plan || {};
    const note = resume.available
      ? `Resume plan: ${display(resume.completed_generations, "?")} completed; next generation ${display(resume.start_generation, "?")}.`
      : `Resume plan unavailable: ${display(resume.warning, "public resume checks still apply")}.`;
    openConfirmation({
      title: `Resume ${target}`,
      message: "Resume uses the persisted task identity and the completed-generation recovery policy. It never overrides a verified live controller.",
      phrase: target,
      note,
      submitLabel: "Resume safely",
      callback: async (typed) => {
        if (typed !== target) { showToast("Confirmation mismatch", "Type the exact resume target.", true); return false; }
        return submitAction("resume", { target, confirm_target: typed, startup_timeout: 30 });
      },
    });
  });
  byId("stop-all-button").addEventListener("click", () => openConfirmation({
    title: "Stop every detected Praxist run",
    eyebrow: "HOST-WIDE DESTRUCTIVE CONTROL",
    message: "This targets the union of registry-managed and Praxist controller processes found by the bounded process scan. Unrelated processes are not selected by name alone.",
    phrase: "STOP ALL PRAXIST RUNS",
    note: "Use only when every run on this host is intentionally being stopped.",
    submitLabel: "Stop all runs",
    callback: async (typed) => {
      if (typed !== "STOP ALL PRAXIST RUNS") { showToast("Confirmation mismatch", "Type the complete stop-all phrase.", true); return false; }
      return submitAction("stop-all", { confirmation: typed, scope: "all", grace: 300 });
    },
  }));
  byId("cleanup-button").addEventListener("click", () => openConfirmation({
    title: "Clean stale registry entries",
    message: "This removes only entries whose recorded process is gone or whose identity no longer matches. It sends no signals.",
    phrase: "GC STALE",
    note: "Run artifacts are not removed.",
    submitLabel: "Clean registry",
    callback: async (typed) => {
      if (typed !== "GC STALE") { showToast("Confirmation mismatch", "Type the registry cleanup phrase.", true); return false; }
      return submitAction("gc", { confirmation: typed, dry_run: false });
    },
  }));
  byId("confirm-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!app.confirmCallback) return;
    const ok = await app.confirmCallback();
    if (ok) byId("confirm-dialog").close();
  });
}

function showToast(title, message, error = false) {
  const toast = document.createElement("article");
  toast.className = `toast${error ? " error" : ""}`;
  const dot = document.createElement("i");
  const copy = document.createElement("div");
  const strong = document.createElement("strong");
  const paragraph = document.createElement("p");
  strong.textContent = title;
  paragraph.textContent = message;
  copy.append(strong, paragraph);
  toast.append(dot, copy);
  byId("toast-region").append(toast);
  setTimeout(() => toast.remove(), 5200);
}

async function copyText(value, success) {
  if (!value) return;
  try { await navigator.clipboard.writeText(value); showToast(success, value); }
  catch { showToast("Copy unavailable", "Select and copy the displayed value manually.", true); }
}

function isActive(run) { return ["running", "starting", "status_inconsistent", "unknown"].includes(String(run.state)); }
function basename(value) { const parts = String(value || "").split(/[\\/]/).filter(Boolean); return parts.at(-1) || ""; }
function compactNumber(value) { const number = Number(value) || 0; return number >= 1_000_000 ? `${(number / 1_000_000).toFixed(1)}m` : number >= 1_000 ? `${(number / 1_000).toFixed(1)}k` : String(number); }
function sumObject(value) { return Object.values(value || {}).reduce((sum, item) => sum + (Number(item) || 0), 0); }
function healthSummary(value) { return ["red", "yellow", "green"].filter((key) => value?.[key]).map((key) => `${value[key]} ${key}`).join(" · ") || "no peer health"; }
function compactObject(value, limit) { return Object.fromEntries(Object.entries(value || {}).slice(0, limit)); }
function formatClock(value) { const date = new Date(value); return Number.isNaN(date.valueOf()) ? "now" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
function relativeTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return display(value);
  const seconds = Math.round((date.valueOf() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 48) return formatter.format(hours, "hour");
  return formatter.format(Math.round(hours / 24), "day");
}
function shellQuote(value) { const text = String(value || ""); return `'${text.replaceAll("'", `'"'"'`)}'`; }

wireEvents();
renderMode();
refresh();
