"use strict";

const $ = (id) => document.getElementById(id);
const clone = (value) => JSON.parse(JSON.stringify(value));
let state, draft, dirty = false, jsonDirty = false, page = "overview", busy = false;
let lastRunStatus = "idle";
let displayedAI = {};
const titles = {overview: "概览与日报", model: "AI 模型", sources: "信息源", rules: "日报规则", profiles: "Profile 管理", advanced: "高级配置", reader: "日报阅读"};
let currentDigest = null, readerRequest = 0, readerMode = "formatted", readerObserver = null;

async function api(path, body, method = "POST") {
  const response = await fetch(path, body === undefined ? {} : {
    method, headers: {"Content-Type": "application/json", "X-Horizon-Token": state.csrf_token},
    body: JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}
function message(text, success = false) {
  $("message").textContent = text;
  $("message").className = success ? "message success" : "message";
  $("message").hidden = false;
}
function markDirty() {
  dirty = true;
  $("save-status").textContent = "有未保存修改";
  $("save-status").className = "save-status dirty";
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function profileSelect(selected, auto = true) {
  const select = element("select");
  if (auto) select.add(new Option("自动匹配", "auto"));
  state.profiles.forEach(p => select.add(new Option(p.name, p.id)));
  if (Array.isArray(selected)) {
    select.add(new Option("保留现有候选 Profile", "__keep__"));
    select.value = "__keep__";
  } else select.value = selected || (auto ? "auto" : state.profiles[0]?.id);
  return select;
}
function setProfiles(id, selected, auto = true) {
  const existing = $(id), select = profileSelect(selected, auto);
  existing.replaceChildren(...select.childNodes);
  existing.value = select.value || (selected || "auto");
  if (Array.isArray(selected)) existing.value = "__keep__";
}
function field(label, value, onChange, type = "text", placeholder = "") {
  const wrap = element("label", label), input = element("input");
  input.type = type; input.value = value ?? ""; input.placeholder = placeholder;
  input.addEventListener("input", () => { onChange(input.value); markDirty(); });
  wrap.append(input); return wrap;
}
function sourceCard(title, source, remove) {
  const card = element("div", undefined, "source-item"), top = element("div", undefined, "source-top");
  top.append(element("span", title, "source-title"));
  const actions = element("div", undefined, "source-meta"), enabledLabel = element("label", undefined, "checkbox-row");
  const enabled = element("input"); enabled.type = "checkbox"; enabled.checked = source.enabled !== false;
  enabled.addEventListener("change", () => { source.enabled = enabled.checked; markDirty(); });
  enabledLabel.append(enabled, document.createTextNode("启用"));
  const del = element("button", "移除", "delete");
  del.addEventListener("click", () => { remove(); markDirty(); renderSources(); });
  actions.append(enabledLabel, del); top.append(actions); card.append(top);
  const fields = element("div", undefined, "source-fields"); card.append(fields);
  return [card, fields];
}
function profileField(source) {
  const label = element("label", "阅读方式"), select = profileSelect(source.profile);
  select.addEventListener("change", () => { if (select.value !== "__keep__") source.profile = select.value; markDirty(); });
  label.append(select); return label;
}
function renderSources() {
  const rss = draft.sources.rss || [], github = draft.sources.github || [];
  $("rss-list").replaceChildren(); $("github-list").replaceChildren();
  if (!rss.length) $("rss-list").append(element("p", "还没有订阅。点击右上角添加你的第一个信息源。", "hint"));
  rss.forEach((source, index) => {
    const [card, fields] = sourceCard(`订阅 ${String(index + 1).padStart(2, "0")}`, source, () => rss.splice(index, 1));
    fields.append(field("名称", source.name, v => source.name = v, "text", "例如：技术博客"),
      field("RSS / Atom 地址", source.url, v => source.url = v, "url", "https://example.com/feed.xml"),
      field("分类", source.category, v => source.category = v || null, "text", "例如：ai-tools"), profileField(source));
    $("rss-list").append(card);
  });
  if (!github.length) $("github-list").append(element("p", "还没有 GitHub 来源。可以关注某个仓库的版本更新。", "hint"));
  github.forEach((source, index) => {
    const [card, fields] = sourceCard(`GitHub ${String(index + 1).padStart(2, "0")}`, source, () => github.splice(index, 1));
    const typeLabel = element("label", "动态类型"), type = element("select");
    type.add(new Option("仓库 Release", "repo_releases")); type.add(new Option("用户动态", "user_events")); type.value = source.type;
    type.addEventListener("change", () => { source.type = type.value; markDirty(); renderSources(); });
    typeLabel.append(type); fields.append(typeLabel, profileField(source));
    if (source.type === "user_events") fields.append(field("GitHub 用户名", source.username, v => source.username = v));
    else fields.append(field("仓库所有者", source.owner, v => source.owner = v, "text", "astral-sh"), field("仓库名称", source.repo, v => source.repo = v, "text", "uv"));
    $("github-list").append(card);
  });
}
function renderRules() {
  $("profile-rules").replaceChildren();
  state.profiles.forEach(profile => {
    const setting = draft.processing?.profile_settings?.[profile.id] || {};
    const row = element("div", undefined, "rule-row"), name = element("div", profile.name, "rule-name");
    name.append(element("small", profile.id));
    const label = element("label", "最低评分"), input = element("input");
    input.id = `threshold-${profile.id}`; input.type = "number"; input.min = 0; input.max = 10; input.step = .5;
    input.placeholder = "不设门槛"; input.value = setting.threshold ?? ""; label.append(input);
    const dedupLabel = element("label", undefined, "checkbox-row"), checkbox = element("input");
    checkbox.id = `dedup-${profile.id}`; checkbox.type = "checkbox"; checkbox.checked = setting.topic_dedup !== false;
    dedupLabel.append(checkbox, document.createTextNode("合并重复主题")); row.append(name, label, dedupLabel);
    $("profile-rules").append(row);
  });
}
function updateProtocol() {
  const openai = $("provider").value === "openai";
  $("protocol-field").hidden = !openai;
  if (!openai) $("api-protocol").value = "chat_completions";
  $("max-tokens").min = openai && $("api-protocol").value === "responses" ? "16" : "1";
  $("azure-note").hidden = $("provider").value !== "azure";
}
function fillForm() {
  const ai = draft.ai || {}, hn = draft.sources.hackernews || {}, rules = draft.processing || {}, digest = draft.digest || {};
  $("provider").value = ai.provider || "openai";
  $("api-protocol").value = ai.api_protocol || "chat_completions";
  updateProtocol();
  $("model").value = state.ai.model || ""; $("base-url").value = state.ai.base_url || "";
  displayedAI = {model: $("model").value, base_url: $("base-url").value};
  $("api-key").value = ""; $("clear-key").checked = false;
  $("key-env").value = ai.api_key_env ?? (ai.provider === "openai-chatgpt" ? "" : "HORIZON_AI_API_KEY");
  $("max-tokens").value = ai.max_tokens ?? 4096; $("temperature").value = ai.temperature ?? .3;
  $("concurrency").value = ai.analysis_concurrency ?? 1;
  $("key-badge").textContent = state.ai.key_configured ? "密钥已配置" : "未配置密钥";
  $("key-badge").className = state.ai.key_configured ? "badge good" : "badge";
  $("key-hint").textContent = state.ai.key_configured ? "已有密钥，留空不会修改。填写新值后点击保存即可替换。" : "填写你自己的密钥，然后点击右上角保存。";
  $("hn-enabled").checked = hn.enabled !== false; $("hn-count").value = hn.fetch_top_stories ?? 30; $("hn-score").value = hn.min_score ?? 100;
  setProfiles("hn-profile", hn.profile); setProfiles("default-profile", rules.default_profile || "tech-news", false);
  $("max-items").value = digest.max_items ?? ""; $("hours").value = draft.collection?.time_window_hours ?? 24;
  $("lang-zh").checked = (ai.languages || ["en"]).includes("zh"); $("lang-en").checked = (ai.languages || ["en"]).includes("en");
  $("azure-note").hidden = ai.provider !== "azure";
  renderSources(); renderRules(); updateOverview();
}
function number(id, fallback = null) {
  const value = $(id).value.trim();
  if (!value) return fallback;
  const n = Number(value); if (!Number.isFinite(n)) throw new Error("请填写有效数字"); return n;
}
function collect() {
  const config = clone(draft);
  config.ai = {...config.ai, provider: $("provider").value, api_protocol: $("provider").value === "openai" ? $("api-protocol").value : "chat_completions", model: $("model").value === displayedAI.model ? draft.ai.model : $("model").value.trim(), base_url: $("base-url").value === displayedAI.base_url ? draft.ai.base_url : $("base-url").value.trim() || null,
    api_key_env: $("key-env").value.trim(), max_tokens: number("max-tokens", 4096), temperature: number("temperature", .3), analysis_concurrency: number("concurrency", 1),
    languages: [...(draft.ai.languages || []).filter(l => !["zh", "en"].includes(l)), ...[$("lang-zh").checked ? "zh" : null, $("lang-en").checked ? "en" : null].filter(Boolean)]};
  config.sources.hackernews = {...config.sources.hackernews, enabled: $("hn-enabled").checked, fetch_top_stories: number("hn-count", 30), min_score: number("hn-score", 100)};
  if ($("hn-profile").value !== "__keep__") config.sources.hackernews.profile = $("hn-profile").value;
  config.digest = {...config.digest, max_items: number("max-items")};
  config.collection = {...config.collection, time_window_hours: number("hours", 24)};
  config.processing = {...config.processing, default_profile: $("default-profile").value, profile_settings: {...config.processing?.profile_settings}};
  state.profiles.forEach(p => {
    const setting = config.processing.profile_settings[p.id] || {};
    const threshold = number(`threshold-${p.id}`), dedup = $(`dedup-${p.id}`).checked;
    if (threshold !== (setting.threshold ?? null)) setting.threshold = threshold;
    if (dedup !== (setting.topic_dedup !== false)) setting.topic_dedup = dedup;
    if (Object.keys(setting).length) config.processing.profile_settings[p.id] = setting;
  });
  return config;
}
function updateOverview() {
  const sources = draft.sources;
  let count = (sources.rss || []).filter(s => s.enabled !== false).length + (sources.github || []).filter(s => s.enabled !== false).length;
  Object.entries(sources).forEach(([key, value]) => { if (!["rss", "github"].includes(key) && value?.enabled) count++; });
  $("stat-sources").textContent = count;
  $("stat-limit").textContent = draft.digest?.max_items ?? "不限";
  $("readiness").textContent = state.missing.length ? "还需配置：" + state.missing.join("、") : "已具备运行所需配置。点击生成后会使用你的 AI API。";
  renderDigests(state.summaries); renderRun(state.run);
}
function renderDigests(items) {
  state.summaries = items; $("stat-digests").textContent = items.length;
  const list = $("digest-list"); list.replaceChildren();
  if (!items.length) {
    const empty = element("div", undefined, "empty");
    empty.append(element("span", "▤", "empty-symbol"), element("strong", "第一份日报，从这里开始"), element("span", "完成模型配置后，点击「生成今日新闻」。")); list.append(empty); return;
  }
  items.forEach(item => {
    const button = element("button", undefined, "digest-row"), title = element("span", digestLabel(item.name));
    title.append(element("small", `${(item.size / 1024).toFixed(1)} KB · 阅读日报`));
    button.append(title, element("span", "↗", "digest-arrow"));
    button.addEventListener("click", () => openDigest(item.name)); list.append(button);
  });
}
function digestLabel(name) {
  const match = name.match(/^horizon-(\d{4}-\d{2}-\d{2})-(.+)\.md$/);
  if (!match) return name;
  return `${match[1]} · ${{zh: "中文日报", en: "English"}[match[2]] || match[2]}`;
}
function setReaderMode(mode) {
  readerMode = mode;
  $("reader-layout").hidden = mode !== "formatted" || !currentDigest;
  $("reader-raw").hidden = mode !== "raw" || !currentDigest;
  $("reader-formatted").setAttribute("aria-pressed", String(mode === "formatted"));
  $("reader-original").setAttribute("aria-pressed", String(mode === "raw"));
}
function readerJump(id) {
  const target = document.getElementById(id);
  if (!target || !$("reader-article").contains(target)) return;
  target.scrollIntoView({behavior: "auto", block: "start"});
  const heading = target.matches("h2,h3") ? target : target.nextElementSibling;
  if (heading?.matches("h2,h3")) heading.focus({preventScroll:true});
}
function fillReader(data) {
  currentDigest = data;
  document.title = data.title + " · Horizon";
  $("reader-title").textContent = data.title;
  $("reader-meta").textContent = `${data.article_count ? data.article_count + " 条精选 · " : ""}约 ${data.reading_minutes} 分钟阅读 · ${digestLabel(data.name)}`;
  // HTML is sanitized and namespaced by src.web.reader, never raw Markdown HTML.
  $("reader-article").innerHTML = data.html;
  $("reader-raw").textContent = data.markdown;
  $("reader-toc").replaceChildren();
  data.toc.forEach(entry => {
    const link = element("a", entry.title, entry.level === 2 ? "toc-category" : "toc-article");
    link.href = "#" + entry.id; link.dataset.heading = entry.id;
    link.addEventListener("click", event => { event.preventDefault(); readerJump(entry.id); });
    $("reader-toc").append(link);
  });
  $("reader-toc-count").textContent = data.article_count ? String(data.article_count).padStart(2, "0") : "";
  $("reader-toc-panel").hidden = !data.toc.length;
  $("reader-layout").classList.toggle("no-toc", !data.toc.length);
  $("reader-toc-panel").open = window.matchMedia("(min-width: 901px)").matches;
  $("reader-download").href = "/api/summaries/" + encodeURIComponent(data.name) + "/download";
  $("reader-download").download = data.name;
  $("reader-download").removeAttribute("aria-disabled");
  $("reader-loading").hidden = true;
  setReaderMode("formatted");
  readerObserver?.disconnect();
  readerObserver = new IntersectionObserver(entries => {
    const active = entries.find(entry => entry.isIntersecting);
    if (!active) return;
    $("reader-toc").querySelectorAll("a").forEach(link => {
      const selected = link.dataset.heading === active.target.id;
      link.classList.toggle("current", selected);
      if (selected) link.setAttribute("aria-current", "location");
      else link.removeAttribute("aria-current");
    });
  }, {rootMargin: "-90px 0px -55% 0px"});
  $("reader-article").querySelectorAll("h2,h3").forEach(heading => readerObserver.observe(heading));
}
async function openDigest(name, historyMode = "push") {
  const request = ++readerRequest;
  currentDigest = null; readerObserver?.disconnect();
  navigate("reader", false);
  if (page !== "reader") return;
  history[historyMode === "push" ? "pushState" : "replaceState"](null, "", "#reader/" + encodeURIComponent(name));
  $("reader-title").textContent = digestLabel(name);
  $("reader-meta").textContent = "";
  $("reader-error").hidden = true; $("reader-loading").hidden = false;
  $("reader-download").removeAttribute("href"); $("reader-download").setAttribute("aria-disabled", "true");
  setReaderMode("formatted");
  $("reader-article").replaceChildren(); $("reader-raw").textContent = "";
  $("reader-select").replaceChildren();
  const available = state?.summaries || [];
  available.forEach(item => $("reader-select").add(new Option(digestLabel(item.name), item.name)));
  if (!available.some(item => item.name === name)) $("reader-select").add(new Option(digestLabel(name), name));
  $("reader-select").value = name;
  window.scrollTo({top:0,behavior:"auto"});
  try {
    const data = await api("/api/summaries/" + encodeURIComponent(name));
    if (request !== readerRequest || page !== "reader") return;
    fillReader(data);
  } catch (error) {
    if (request !== readerRequest || page !== "reader") return;
    $("reader-loading").hidden = true;
    $("reader-error").textContent = error.message;
    $("reader-error").hidden = false;
  }
}
function renderRun(run) {
  const active = ["running", "stopping"].includes(run.status);
  const labels = {idle: "待开始", running: "运行中", stopping: "正在停止", stopped: "已停止", success: "已完成", failed: "运行失败"};
  $("run-badge").textContent = labels[run.status] || run.status;
  $("run-badge").className = "badge" + (run.status === "success" ? " good" : run.status === "failed" ? " bad" : "");
  $("run").disabled = active || busy; $("stop").hidden = !active; $("stop").disabled = run.status === "stopping";
  $("save").disabled = active || busy; $("run-detail").textContent = run.started_at ? "开始于 " + new Date(run.started_at).toLocaleString("zh-CN") : "尚未运行";
  const log = $("logs"), text = run.logs?.length ? run.logs.join("\n") : "运行日志会显示在这里。";
  if (log.textContent !== text) { const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 45; log.textContent = text; if (atBottom) log.scrollTop = log.scrollHeight; }
  lastRunStatus = run.status;
}
async function load() {
  busy = true; $("save").disabled = true;
  try {
    state = await api("/api/state"); draft = clone(state.config); dirty = false; jsonDirty = false;
    $("loading").hidden = true; $("content").hidden = false; $("message").hidden = true;
    fillForm(); $("save-status").textContent = "已与本地同步"; $("save-status").className = "save-status";
    $("json-editor").value = JSON.stringify(collect(), null, 2);
  } catch (error) { $("loading").textContent = "配置加载失败，可以点击「重新加载」重试。"; message(error.message); }
  finally { busy = false; if (state) renderRun(state.run); }
}
async function save() {
  try {
    if (jsonDirty) throw new Error("高级 JSON 有未应用的修改，请先点击「应用到表单」。");
    const invalid = [...document.querySelectorAll("input")].find(input => !input.checkValidity());
    if (invalid) { navigate(invalid.closest(".page").id.replace("page-", "")); invalid.reportValidity(); return; }
    busy = true; $("save").disabled = true;
    state = await api("/api/config", {config: collect(), revision: state.revision, api_key: $("api-key").value, clear_key: $("clear-key").checked});
    draft = clone(state.config); dirty = false; fillForm();
    $("save-status").textContent = "已保存到本地"; $("save-status").className = "save-status";
    $("json-editor").value = JSON.stringify(collect(), null, 2);
    message("配置已保存。下次生成日报时会使用这份配置。", true);
  } catch (error) { message(error.message); }
  finally { busy = false; renderRun(state.run); }
}
function navigate(next, updateUrl = true) {
  if (!(next in titles)) next = "overview";
  if (page === "advanced" && jsonDirty && next !== "advanced") { message("请先应用高级 JSON 修改，或重新加载以放弃修改。"); return; }
  page = next; $("page-title").textContent = titles[page];
  document.querySelector(".top-actions").hidden = page === "reader";
  if (page !== "reader") { readerRequest++; readerObserver?.disconnect(); document.title = "Horizon · 我的每日新闻"; }
  document.querySelectorAll(".page").forEach(p => p.hidden = p.id !== "page-" + page);
  document.querySelectorAll("nav button[data-page]").forEach(b => { const active = b.dataset.page === (page === "reader" ? "overview" : page); b.classList.toggle("active", active); b.setAttribute("aria-current", active ? "page" : "false"); });
  if (page === "advanced" && state && !jsonDirty) $("json-editor").value = JSON.stringify(collect(), null, 2);
  if (page === "profiles" && state) loadProfiles().catch(error => message(error.message));
  if (updateUrl) history.replaceState(null, "", "#" + page);
  $("message").hidden = true;
}

document.querySelectorAll("nav button").forEach(b => b.addEventListener("click", () => navigate(b.dataset.page)));
document.querySelector(".brand").addEventListener("click", event => { event.preventDefault(); navigate("overview"); });
$("content").addEventListener("input", event => { if (!event.target.closest("#page-profiles") && event.target.matches("input,select,textarea")) markDirty(); });
$("content").addEventListener("change", event => { if (!event.target.closest("#page-profiles") && event.target.matches("input,select")) markDirty(); });
$("save").addEventListener("click", save);
$("reload").addEventListener("click", () => { if (!dirty || confirm("放弃未保存的修改，并重新读取本地配置？")) load(); });
$("go-model").addEventListener("click", () => navigate("model"));
$("provider").addEventListener("change", updateProtocol);
$("api-protocol").addEventListener("change", updateProtocol);
$("add-rss").addEventListener("click", () => {
  draft.sources.rss ||= []; draft.sources.rss.push({name: "", url: "", enabled: true, category: "", profile: "tech-news"});
  renderSources(); markDirty(); $("rss-list").lastElementChild.querySelector('input[type="text"]').focus();
});
$("add-github").addEventListener("click", () => {
  draft.sources.github ||= []; draft.sources.github.push({type: "repo_releases", owner: "", repo: "", enabled: true, profile: "tech-news"});
  renderSources(); markDirty();
});
$("json-editor").addEventListener("input", () => jsonDirty = true);
$("apply-json").addEventListener("click", async () => {
  try {
    const next = JSON.parse($("json-editor").value);
    if (!next || typeof next !== "object" || Array.isArray(next) || !next.ai || !next.sources) throw new Error("配置必须包含 ai 和 sources 对象");
    const key = $("api-key").value, clear = $("clear-key").checked;
    const ai = await api("/api/validate", {config: next});
    draft = next; state.ai = ai; fillForm(); $("api-key").value = key; $("clear-key").checked = clear;
    jsonDirty = false; markDirty(); message("JSON 已应用到表单，点击「保存配置」后写入本地。", true);
  } catch (error) { message("JSON 无法应用：" + error.message); }
});
$("run").addEventListener("click", async () => {
  if (dirty) { message("请先保存配置，再生成日报。"); return; }
  try { busy = true; $("run").disabled = true; state.run = await api("/api/run", {}); $("message").hidden = true; }
  catch (error) { message(error.message); }
  finally { busy = false; renderRun(state.run); }
});
$("stop").addEventListener("click", async () => {
  try { state.run = await api("/api/run/stop", {}); renderRun(state.run); } catch (error) { message(error.message); }
});
$("refresh-digests").addEventListener("click", async () => { try { renderDigests(await api("/api/summaries")); } catch (error) { message(error.message); } });
$("reader-back").addEventListener("click", () => { navigate("overview"); window.scrollTo({top:0,behavior:"auto"}); });
$("reader-select").addEventListener("change", () => openDigest($("reader-select").value, "replace"));
$("reader-formatted").addEventListener("click", () => { setReaderMode("formatted"); if (currentDigest) $("reader-article").scrollIntoView({block:"start"}); });
$("reader-original").addEventListener("click", () => { setReaderMode("raw"); if (currentDigest) $("reader-raw").scrollIntoView({block:"start"}); });
$("reader-top").addEventListener("click", () => window.scrollTo({top:0,behavior:"auto"}));
$("reader-article").addEventListener("click", event => {
  const link = event.target.closest("a[href]");
  if (link?.getAttribute("href").startsWith("#")) {
    event.preventDefault(); readerJump(link.getAttribute("href").slice(1));
  }
});
function route() {
  const hash = location.hash.slice(1);
  if (hash.startsWith("reader/")) {
    try { openDigest(decodeURIComponent(hash.slice(7)), "replace"); }
    catch (_) { navigate("overview"); message("日报链接格式不正确"); }
  } else navigate(hash || "overview");
}
window.addEventListener("popstate", route);
window.addEventListener("beforeunload", event => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
setInterval(async () => {
  if (!state || busy || document.hidden) return;
  try {
    const previous = lastRunStatus; state.run = await api("/api/run"); renderRun(state.run);
    if (["running", "stopping"].includes(previous) && !["running", "stopping"].includes(state.run.status)) renderDigests(await api("/api/summaries"));
  } catch (_) { $("run-detail").textContent = "服务连接中断，请确认容器正在运行"; }
}, 2500);
let profileDetail = null, routingDetail = null;
async function selectProfile() {
  profileDetail = await api("/api/profiles/" + encodeURIComponent($("profile-picker").value));
  $("profile-definition").value = JSON.stringify(profileDetail.definition, null, 2);
  for (const name of ["match", "analysis", "enrichment"]) {
    $("profile-" + name).value = profileDetail.prompts[name] || "";
    $("profile-" + name).readOnly = !profileDetail.editable;
  }
  $("profile-definition").readOnly = !profileDetail.editable;
  $("profile-save").disabled = !profileDetail.editable;
  $("profile-delete").disabled = !profileDetail.editable;
  $("profile-info").textContent = (profileDetail.editable ? "可编辑" : "内置 / 只读") + " · 引用：" + (profileDetail.references.join("、") || "无");
}
async function loadProfiles(selected) {
  const listing = await api("/api/profiles");
  $("profile-picker").replaceChildren(...listing.profiles.map(p => new Option(p.name + " · " + p.id, p.id)));
  if (selected) $("profile-picker").value = selected;
  routingDetail = await api("/api/profile-routing");
  $("profile-routing").value = JSON.stringify({default_profile: routingDetail.default_profile, profile_settings: routingDetail.profile_settings,
    routes: Object.fromEntries(routingDetail.sources.map(source => [source.key, source.route]))}, null, 2);
  await selectProfile();
}
async function profileAction(action) {
  try {
    if (dirty || jsonDirty) throw new Error("请先保存其他配置，或重新加载放弃未保存修改。");
    if (action === "routing") {
      await api("/api/profile-routing", {...JSON.parse($("profile-routing").value), revision: routingDetail.revision}, "PUT");
    } else {
      const path = "/api/profiles/" + encodeURIComponent(profileDetail.id);
      const body = {definition: JSON.parse($("profile-definition").value), prompts: Object.fromEntries(["match", "analysis", "enrichment"].map(name => [name, $("profile-" + name).value])), revision: profileDetail.revision};
      if (action === "copy") await api(path + "/copy", {id: $("profile-new-id").value.trim()});
      if (action === "create") {
        const id = $("profile-new-id").value.trim();
        body.definition = {...body.definition, id, match: "match.md", analysis: "analysis.md",
          enrichment: {...body.definition.enrichment, prompt: "enrichment.md"}};
        await api("/api/profiles", {...body, id});
      }
      if (action === "save") await api(path, body, "PUT");
      if (action === "delete") {
        if (!confirm("删除此 Profile？此操作不可撤销。")) return;
        await api(path, {revision: profileDetail.revision}, "DELETE");
      }
    }
    await load(); await loadProfiles(); message("Profile 配置已保存。", true);
  } catch (error) { message(error.message); }
}
$("profile-picker").addEventListener("change", () => selectProfile().catch(error => message(error.message)));
$("profiles-refresh").addEventListener("click", () => loadProfiles().catch(error => message(error.message)));
for (const action of ["copy", "create", "save", "delete"]) $("profile-" + action).addEventListener("click", () => profileAction(action));
$("profile-routing-save").addEventListener("click", () => profileAction("routing"));
load().then(route);
