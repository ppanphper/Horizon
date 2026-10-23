"use strict";

const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const PROMPTS = ["match", "analysis", "enrichment"];
const RUN_LABELS = {
  idle: "Idle",
  running: "Running…",
  succeeded: "Finished",
  failed: "Failed",
};
const state = { current: null, config: null, runPoll: null, lastRunState: null };

const $ = (id) => document.getElementById(id);

function element(tag, properties = {}, children = []) {
  const node = document.createElement(tag);
  Object.assign(node, properties);
  node.append(...children);
  return node;
}

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (method !== "GET") {
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRF-Token"] = csrfToken;
    options.body = JSON.stringify(body || {});
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.details = data.details || [];
    throw error;
  }
  return data;
}

function showMessage(text, { error = false, details = [] } = {}) {
  const box = $("message");
  box.className = error ? "message error" : "message";
  box.replaceChildren(error ? element("strong", { textContent: "Error: " }) : "", text);
  if (details.length) {
    box.append(element("ul", {}, details.map((detail) => element("li", { textContent: detail }))));
  }
  box.hidden = false;
}

function reportError(error) {
  showMessage(error.message, { error: true, details: error.details || [] });
}

function guarded(handler) {
  return (event) => {
    if (event) event.preventDefault();
    Promise.resolve(handler(event)).catch(reportError);
  };
}

function showView(name) {
  for (const button of document.querySelectorAll("nav button")) {
    if (button.dataset.view === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  for (const panel of document.querySelectorAll("[data-panel]")) panel.hidden = panel.id !== name;
  $("message").hidden = true;
  if (name === "profiles") return loadProfiles(state.current?.id);
  if (name === "routing") return loadConfig();
  return Promise.all([refreshRun(), loadSummaries()]);
}

/* Profiles */

async function loadProfiles(selectedId) {
  const { profiles } = await api("GET", "/api/profiles");
  const list = $("profile-list");
  list.replaceChildren();
  for (const profile of profiles) {
    const notes = [profile.default ? "default" : "", profile.editable ? "" : "read-only"].filter(Boolean);
    const button = element("button", { type: "button", textContent: profile.id });
    if (notes.length) button.append(element("span", { className: "note", textContent: notes.join(" · ") }));
    if (profile.id === selectedId) button.setAttribute("aria-current", "true");
    button.addEventListener("click", guarded(() => openProfile(profile.id)));
    list.append(element("li", {}, [button]));
  }
  if (selectedId && profiles.some((profile) => profile.id === selectedId) && state.current?.mode !== "new") {
    await openProfile(selectedId);
  }
}

async function openProfile(profileId) {
  const detail = await api("GET", `/api/profiles/${encodeURIComponent(profileId)}`);
  state.current = { mode: detail.editable ? "edit" : "view", id: detail.id, revision: detail.revision };
  for (const button of $("profile-list").querySelectorAll("button")) {
    if (button.firstChild.textContent === detail.id) button.setAttribute("aria-current", "true");
    else button.removeAttribute("aria-current");
  }
  renderEditor(`${detail.name} (${detail.id})`, detail.definition, detail.prompts, detail.references);
}

function renderEditor(title, definition, prompts, references = []) {
  const { mode } = state.current;
  const readOnly = mode === "view";
  $("profile-editor").hidden = false;
  $("profile-title").textContent = title;
  const status = [];
  if (readOnly) status.push("Read-only here. Copy it to make changes.");
  if (references.length) status.push(`Used by ${references.join(", ")}.`);
  $("profile-status").textContent = status.join(" ");
  $("profile-status").hidden = !status.length;
  $("definition").value = JSON.stringify(definition, null, 2);
  $("definition").readOnly = readOnly;
  for (const name of PROMPTS) {
    $(`prompt-${name}`).value = prompts[name];
    $(`prompt-${name}`).readOnly = readOnly;
  }
  $("save-profile").hidden = readOnly;
  $("delete-profile").hidden = mode !== "edit";
  $("copy-form").hidden = mode === "new";
}

function startNewProfile() {
  const input = $("new-profile-id");
  if (!input.reportValidity()) return;
  const profileId = input.value.trim();
  state.current = { mode: "new", id: profileId, revision: null };
  const definition = {
    id: profileId,
    name: profileId,
    match: "match.md",
    analysis: "analysis.md",
    enrichment: {
      prompt: "enrichment.md",
      blocks: [{ id: "summary", type: "section", tools: [], primary: true }],
    },
  };
  renderEditor(`New profile (${profileId})`, definition, { match: "", analysis: "", enrichment: "" });
  $("new-profile-form").reset();
  $("prompt-match").focus();
}

async function saveProfile() {
  let definition;
  try {
    definition = JSON.parse($("definition").value);
  } catch (error) {
    showMessage("profile.json is not valid JSON.", { error: true, details: [error.message] });
    return;
  }
  const prompts = Object.fromEntries(PROMPTS.map((name) => [name, $(`prompt-${name}`).value]));
  const { mode, id, revision } = state.current;
  const saved = mode === "new"
    ? await api("POST", "/api/profiles", { id, definition, prompts })
    : await api("PUT", `/api/profiles/${encodeURIComponent(id)}`, { revision, definition, prompts });
  state.current = { mode: "edit", id: saved.id, revision: saved.revision };
  await loadProfiles(saved.id);
  showMessage(`Saved ${saved.id}. The next pipeline run uses it.`);
}

async function copyProfile() {
  const input = $("copy-profile-id");
  if (!input.reportValidity()) return;
  const copied = await api("POST", `/api/profiles/${encodeURIComponent(state.current.id)}/copy`, {
    id: input.value.trim(),
  });
  $("copy-form").reset();
  state.current = { mode: "edit", id: copied.id, revision: copied.revision };
  await loadProfiles(copied.id);
  showMessage(`Copied to ${copied.id}.`);
}

async function deleteProfile() {
  const { id, revision } = state.current;
  if (!window.confirm(`Delete profile ${id} and its files?`)) return;
  await api("DELETE", `/api/profiles/${encodeURIComponent(id)}`, { revision });
  state.current = null;
  $("profile-editor").hidden = true;
  await loadProfiles();
  showMessage(`Deleted ${id}.`);
}

/* Routing */

function option(value, label, selected = false) {
  return element("option", { value, textContent: label, selected });
}

function profileSelect(profiles, selected, label) {
  const ids = profiles.includes(selected) || !selected ? profiles : [...profiles, selected];
  const select = element("select", { className: "route-fixed" }, ids.map((id) => option(id, id, id === selected)));
  select.setAttribute("aria-label", label);
  return select;
}

function sourceRow(source, config) {
  const name = [source.source, source.label].filter(Boolean).join(" · ");
  const route = source.route;
  const mode = route === null ? "auto" : Array.isArray(route) ? "candidates" : "fixed";
  const modeSelect = element("select", { className: "route-mode" }, [
    option("auto", "Automatic", mode === "auto"),
    option("fixed", "Fixed profile", mode === "fixed"),
    option("candidates", "Candidates", mode === "candidates"),
  ]);
  modeSelect.setAttribute("aria-label", `Routing for ${name}`);
  const fixed = profileSelect(config.profiles, typeof route === "string" ? route : config.default_profile, `Profile for ${name}`);
  const candidates = element("fieldset", { className: "route-candidates" }, [
    element("legend", { className: "visually-hidden", textContent: `Candidate profiles for ${name}` }),
    ...config.profiles.map((id) => element("label", {}, [
      element("input", { type: "checkbox", value: id, checked: Array.isArray(route) && route.includes(id) }),
      id,
    ])),
  ]);
  const sync = () => {
    fixed.hidden = modeSelect.value !== "fixed";
    candidates.hidden = modeSelect.value !== "candidates";
  };
  modeSelect.addEventListener("change", sync);
  sync();
  const header = element("th", { scope: "row", textContent: name });
  if (!source.enabled) header.append(element("span", { className: "note", textContent: "disabled" }));
  const row = element("tr", {}, [header, element("td", {}, [modeSelect]), element("td", {}, [fixed, candidates])]);
  row.dataset.key = source.key;
  return row;
}

function settingsRow(profileId, settings) {
  const threshold = element("input", {
    type: "number", min: 0, max: 10, step: "any", className: "threshold",
    value: settings.threshold ?? "", placeholder: "No filter",
  });
  threshold.setAttribute("aria-label", `Threshold for ${profileId}`);
  const dedup = element("input", { type: "checkbox", className: "topic-dedup", checked: settings.topic_dedup });
  dedup.setAttribute("aria-label", `Topic deduplication for ${profileId}`);
  const row = element("tr", {}, [
    element("th", { scope: "row", textContent: profileId }),
    element("td", {}, [threshold]),
    element("td", {}, [dedup]),
  ]);
  row.dataset.profile = profileId;
  return row;
}

function renderConfig(config) {
  state.config = config;
  $("default-profile").replaceChildren(...config.profiles.map((id) => option(id, id, id === config.default_profile)));
  $("settings-rows").replaceChildren(...config.profiles.map((id) => settingsRow(
    id, config.profile_settings[id] || { threshold: null, topic_dedup: true },
  )));
  const sources = config.sources.map((source) => sourceRow(source, config));
  if (!sources.length) {
    sources.push(element("tr", {}, [element("td", { colSpan: 3, textContent: "No configured sources." })]));
  }
  $("source-rows").replaceChildren(...sources);
}

async function loadConfig() {
  renderConfig(await api("GET", "/api/config"));
}

async function saveConfig() {
  const profileSettings = {};
  for (const row of $("settings-rows").querySelectorAll("tr[data-profile]")) {
    const threshold = row.querySelector(".threshold").value.trim();
    profileSettings[row.dataset.profile] = {
      threshold: threshold === "" ? null : Number(threshold),
      topic_dedup: row.querySelector(".topic-dedup").checked,
    };
  }
  const routes = {};
  for (const row of $("source-rows").querySelectorAll("tr[data-key]")) {
    const mode = row.querySelector(".route-mode").value;
    if (mode === "auto") routes[row.dataset.key] = null;
    else if (mode === "fixed") routes[row.dataset.key] = row.querySelector(".route-fixed").value;
    else routes[row.dataset.key] = [...row.querySelectorAll(".route-candidates input:checked")].map((input) => input.value);
  }
  const config = await api("PUT", "/api/config", {
    revision: state.config.revision,
    default_profile: $("default-profile").value,
    profile_settings: profileSettings,
    routes,
  });
  renderConfig(config);
  showMessage("Routing saved. The next pipeline run uses it.");
}

/* Run */

function renderRun(status) {
  let label = RUN_LABELS[status.state] || status.state;
  if (status.state === "failed" && status.exit_code !== null) label += ` (exit code ${status.exit_code})`;
  $("run-state").textContent = label;
  $("start-run").disabled = status.state === "running";
  const log = $("run-log");
  log.textContent = status.log.join("\n");
  log.hidden = !status.log.length;
  log.scrollTop = log.scrollHeight;
}

async function refreshRun() {
  clearTimeout(state.runPoll);
  const status = await api("GET", "/api/run");
  renderRun(status);
  if (status.state === "running") {
    state.runPoll = setTimeout(() => refreshRun().catch(reportError), 2000);
  } else if (state.lastRunState === "running") {
    await loadSummaries();
  }
  state.lastRunState = status.state;
}

async function startRun() {
  renderRun(await api("POST", "/api/run", {}));
  state.lastRunState = "running";
  state.runPoll = setTimeout(() => refreshRun().catch(reportError), 2000);
}

async function loadSummaries() {
  const { summaries } = await api("GET", "/api/summaries");
  const items = summaries.map((name) => {
    const button = element("button", { type: "button", textContent: name });
    button.addEventListener("click", guarded(() => openSummary(name)));
    return element("li", {}, [button]);
  });
  if (!items.length) items.push(element("li", { className: "status", textContent: "No summaries yet." }));
  $("summary-list").replaceChildren(...items);
}

async function openSummary(name) {
  const { markdown } = await api("GET", `/api/summaries/${encodeURIComponent(name)}`);
  $("summary-view").textContent = markdown;
  $("summary-view").hidden = false;
}

for (const button of document.querySelectorAll("nav button")) {
  button.addEventListener("click", guarded(() => showView(button.dataset.view)));
}
$("new-profile-form").addEventListener("submit", guarded(startNewProfile));
$("copy-form").addEventListener("submit", guarded(copyProfile));
$("save-profile").addEventListener("click", guarded(saveProfile));
$("delete-profile").addEventListener("click", guarded(deleteProfile));
$("routing-form").addEventListener("submit", guarded(saveConfig));
$("start-run").addEventListener("click", guarded(startRun));
loadProfiles().catch(reportError);
