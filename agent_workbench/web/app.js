"use strict";

(() => {
  const $ = (selector, parent = document) => parent.querySelector(selector);
  const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
  const state = { bootstrap: null, tasks: [], trash: [], bin: "active", trashBusy: false, selected: null, detail: null, view: "overview", filter: "all", taskSearch: "", taskStatus: "all", tab: "report", listBusy: false, detailBusy: false, submitting: false, reviewing: false, connection: false, backendMismatch: false, listError: "", detailError: "", storage: null, storageBusy: false, storageError: "", cases: [], casesBusy: false, casesError: "", caseSearch: "", caseVerified: "all", purgeTarget: null, purging: false, reviewDrafts: new Map(), materialDirty: {ops:false,release:false}, materialLoading: {ops:false,release:false} };
  const ACTIVE = new Set(["queued", "running"]);
  const STATUS = {
    queued: ["排队中", "amber-badge"], running: ["处理中", "teal-badge"], waiting_human: ["待人工复核", "amber-badge"],
    completed: ["报告已生成", "teal-badge"], review_acknowledged: ["已确认阅读", "teal-badge"], blocked: ["存在阻断", "red-badge"],
    incomplete: ["需要补充", "amber-badge"], failed: ["运行失败", "red-badge"], interrupted: ["已中断", "amber-badge"],
    rejected: ["已拒绝", "red-badge"], changes_requested: ["要求修改", "amber-badge"]
  };
  const AGENT_NAMES = { ops: "故障诊断", release: "发布评审" };
  const VIEW_NAMES = { overview: "总览", ops: "故障诊断", release: "发布评审", tasks: "任务与报告", cases: "案例库" };
  const OLD_BACKEND_MESSAGE = "当前后台仍是旧版，请在旧启动窗口按Ctrl+C关闭，再重新启动工作台并刷新页面。";
  const MIXED_BACKEND_MESSAGE = "网页与后台版本不一致，或同一端口仍有重复服务。请关闭旧启动窗口，重新启动工作台并刷新页面。";
  const numberFormat = new Intl.NumberFormat("zh-CN");
  let toastTimer;
  const recognizedMaterials = {ops: "", release: ""};

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function text(selector, value) { const node = $(selector); if (node) node.textContent = String(value); }
  function count(value) { return Number.isFinite(value) && value >= 0 ? numberFormat.format(value) : "未知"; }
  function date(value, short = false) {
    const parsed = new Date(value);
    if (!value || Number.isNaN(parsed.getTime())) return "时间未知";
    return new Intl.DateTimeFormat("zh-CN", { ...(short ? {} : { year: "numeric" }), month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(parsed);
  }
  function badge(status) {
    const [label, color] = STATUS[status] || ["状态未知", ""];
    const node = el("span", `badge ${color}${status === "running" ? " running" : ""}`);
    node.append(el("span", "badge-dot"), document.createTextNode(label));
    return node;
  }
  function toast(message) {
    const node = $("#toast-region");
    node.textContent = message;
    node.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove("visible"), 3800);
  }
  function supportsFeature(feature) { return !state.backendMismatch && state.bootstrap?.capabilities?.[feature] === true; }
  function compatibilityMessage() {
    if (state.backendMismatch) return MIXED_BACKEND_MESSAGE;
    return state.bootstrap && (!supportsFeature("trash") || !supportsFeature("custom_materials")) ? OLD_BACKEND_MESSAGE : "";
  }
  function updateBackendControls() {
    const button = $("#tasks-trash");
    button.disabled = !supportsFeature("trash");
    button.title = button.disabled ? compatibilityMessage() : "查看可恢复的报告";
  }
  function setConnection(online, message = "") {
    state.connection = online;
    $("#connection-dot").className = `connection-dot ${online ? "online" : "offline"}`;
    text("#connection-text", online ? "本机服务已连接" : "本机连接待恢复");
    const notice = online ? compatibilityMessage() || message : message || "暂时无法连接本机服务，已保留当前页面。请确认工作台仍在运行。";
    $("#global-notice").hidden = online && !notice;
    text("#global-notice-text", notice);
  }
  async function api(path, options = {}) {
    const { timeoutMs = 15000, ...requestOptions } = options;
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), timeoutMs);
    try {
      const response = await fetch(path, { credentials: "same-origin", cache: "no-store", redirect: "error", signal: abort.signal, ...requestOptions,
        headers: { "Accept": "application/json", ...(requestOptions.body ? { "Content-Type": "application/json", "X-Workbench-Token": state.bootstrap?.csrf_token || "" } : {}), ...requestOptions.headers } });
      let payload;
      try { payload = await response.json(); } catch (_) { throw new Error("本机服务返回了无法读取的结果，请刷新连接后重试。"); }
      const routeMissing = payload?.error === "提交入口不存在。" || payload?.error === "页面或接口不存在。";
      if (response.status === 404 && routeMissing && (path === "/api/trash" || /^\/api\/tasks\/[^/]+\/(trash|restore)$/.test(path))) {
        state.backendMismatch = true;
        updateBackendControls();
        setConnection(true);
        throw new Error(MIXED_BACKEND_MESSAGE);
      }
      if (!response.ok) throw new Error(typeof payload?.error === "string" ? payload.error : `请求未完成（${response.status}），请稍后重试。`);
      return payload;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("请求等待超时，后台任务可能仍在运行，请刷新任务状态。");
      if (error instanceof TypeError) throw new Error("无法连接本机服务，请确认工作台仍在运行。");
      throw error;
    } finally { clearTimeout(timer); }
  }

  function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]); }
  function safeMarkdownUrl(value) {
    const decoded = value.replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">");
    try {
      const url = new URL(decoded, window.location.href);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
      return escapeHtml(url.href);
    } catch (_) { return null; }
  }
  function inlineMarkdown(raw) {
    // Escape untrusted text first; only these three fixed markup forms return
    // HTML. Raw HTML, image tags, event handlers and executable links stay inert.
    return escapeHtml(raw).replace(/`([^`\n]+)`|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\(([^)\s]+)\)/g, (match, code, bold, label, url) => {
      if (code !== undefined) return `<code>${code}</code>`;
      if (bold !== undefined) return `<strong>${bold}</strong>`;
      const safe = safeMarkdownUrl(url);
      return safe ? `<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
    });
  }
  function markdown(raw) {
    const lines = String(raw ?? "").replace(/\r\n?/g, "\n").split("\n");
    const result = [];
    let index = 0;
    const cells = line => line.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, "|"));
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index++; continue; }
      if (/^\s*```/.test(line)) {
        const code = [];
        index++;
        while (index < lines.length && !/^\s*```/.test(lines[index])) code.push(lines[index++]);
        if (index < lines.length) index++;
        result.push(`<details class="report-code"><summary>查看结构化记录</summary><pre><code>${escapeHtml(code.join("\n"))}</code></pre></details>`);
        continue;
      }
      if (line.includes("|") && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1]) && cells(lines[index + 1]).every(cell => /^:?-{3,}:?$/.test(cell))) {
        const header = cells(line);
        const rows = [];
        index += 2;
        while (index < lines.length && lines[index].trim() && lines[index].includes("|")) rows.push(cells(lines[index++]));
        result.push(`<div class="table-wrap"><table><thead><tr>${header.map(cell => `<th>${inlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${header.map((_, position) => `<td>${inlineMarkdown(row[position] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }
      const heading = line.match(/^(#{1,4})\s+(.+)$/);
      if (heading) { result.push(`<h${heading[1].length}>${inlineMarkdown(heading[2])}</h${heading[1].length}>`); index++; continue; }
      if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { result.push("<hr>"); index++; continue; }
      if (/^\s*>\s?/.test(line)) { result.push(`<blockquote>${inlineMarkdown(line.replace(/^\s*>\s?/, ""))}</blockquote>`); index++; continue; }
      if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]\s+/.test(line);
        const pattern = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*+]\s+/;
        const items = [];
        while (index < lines.length && pattern.test(lines[index])) items.push(`<li>${inlineMarkdown(lines[index++].replace(pattern, ""))}</li>`);
        result.push(`<${ordered ? "ol" : "ul"}>${items.join("")}</${ordered ? "ol" : "ul"}>`);
        continue;
      }
      result.push(`<p>${inlineMarkdown(line)}</p>`);
      index++;
    }
    return result.join("\n");
  }

  function toLocalInput(value) {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "";
    return new Date(parsed.getTime() - parsed.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  }
  function ensureOption(input, value) { if (typeof value === "string") input.value = value; }
  const KIND_NAMES = { metrics: "指标", logs: "日志", traces: "链路", changes: "变更", runbook: "手册", runbooks: "手册", release_snapshot: "发布快照", config_diff: "配置差异", code_diff: "代码差异", ci_checks: "测试检查", dependency_budget: "依赖容量", incident_case: "历史案例", incident_cases: "历史案例" };
  const MATERIAL_NAMES = { complete: "类别已覆盖", partial: "类别有缺项", empty: "无当前资料", unknown: "类别覆盖待核对" };
  function coverageSummary(value) {
    const coverage = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    const current = Number.isInteger(coverage.current_count) && coverage.current_count >= 0 ? coverage.current_count : null;
    const missing = Array.isArray(coverage.missing_kinds) ? [...new Set(coverage.missing_kinds.filter(item => typeof item === "string"))] : [];
    const counts = Object.entries(coverage.counts || {}).filter(([, value]) => Number.isInteger(value) && value >= 0);
    const status = current === 0 ? "empty" : missing.length ? "partial" : current === null ? "unknown" : "complete";
    return { current, missing, counts, status, first: coverage.first_observation, last: coverage.last_observation };
  }
  function hasProfiles(agent) { return Array.isArray(state.bootstrap?.profiles?.[agent]); }
  function profilesFor(agent) { return (hasProfiles(agent) ? state.bootstrap.profiles[agent] : []).filter(item => item && typeof item.id === "string" && item.params && typeof item.params === "object" && !Array.isArray(item.params)); }
  function isCustom(agent) { return $(`#${agent}-profile`).value === "custom"; }
  function selectedProfile(agent) { return profilesFor(agent).find(item => item.id === $(`#${agent}-profile`).value) || null; }
  function modelLabel(mode) { return ["api", "model_api"].includes(mode) ? "真实模型 API" : ["demo", "scripted_demo"].includes(mode) ? "离线脚本" : "方式未记录"; }
  function sourceLabel(task) {
    if (typeof task.source_label === "string" && task.source_label) return task.source_label.replace(/^材料[：:]\s*/, "");
    const data = dataOf(task);
    if (task.source === "synthetic" || data.source === "synthetic" || (Array.isArray(data.evidence) && data.evidence.length && data.evidence.every(row => row.synthetic === true))) return "模拟演示";
    return "历史资料（来源待核对）";
  }
  function profileTimeState(profile, start, end) {
    const first = typeof profile?.coverage?.first_observation === "string" ? new Date(profile.coverage.first_observation).getTime() : NaN;
    const last = typeof profile?.coverage?.last_observation === "string" ? new Date(profile.coverage.last_observation).getTime() : NaN;
    const from = new Date(start).getTime(), to = new Date(end).getTime();
    const valid = [from, to].every(Number.isFinite) && from <= to;
    const edited = valid && (from !== new Date(profile?.params?.start).getTime() || to !== new Date(profile?.params?.end).getTime());
    return { edited, outside: valid && [first, last].every(Number.isFinite) && (to < first || from > last) };
  }
  function currentProfileTime(profile) { return profileTimeState(profile, $("#ops-start").value, $("#ops-end").value); }
  function profileProblem(agent) {
    if (isCustom(agent)) return supportsFeature("custom_materials") ? "" : compatibilityMessage() || OLD_BACKEND_MESSAGE;
    if (!hasProfiles(agent)) return "";
    const profile = selectedProfile(agent);
    if (!profile) return "请先选择一个可用演示场景。";
    if (profile.ready === false || coverageSummary(profile.coverage).current === 0) return "本场景没有可运行的当前资料，请选择其他演示场景。";
    if (agent === "ops" && currentProfileTime(profile).outside) return "当前时间窗不包含本场景的观测资料，请调整时间后再运行。";
    return "";
  }
  function coveragePanel(coverage, options = {}) {
    const info = coverageSummary(coverage);
    const panel = el("section", `coverage-card ${options.outside ? "empty" : info.status}`);
    const top = el("div", "coverage-heading");
    top.append(el("h3", "", options.title || "演示资料预检"), el("span", "material-state", MATERIAL_NAMES[options.outside ? "empty" : info.status]));
    panel.append(top);
    if (info.counts.length) {
      const counts = el("div", "coverage-counts");
      info.counts.forEach(([kind, value]) => counts.append(el("span", "", `${KIND_NAMES[kind] || kind} ${value}`)));
      panel.append(counts);
    }
    panel.append(el("p", "", info.current === null ? "尚未提供覆盖预检；任务提交后仍由后台检查实际资料。" : `当前资料 ${info.current} 条；手册和历史案例不替代当前观测。即使类别已覆盖，仍可能需要补查。`));
    if (info.first || info.last) panel.append(el("p", "", `材料观测时间：${date(info.first)} — ${date(info.last)}。这是样本范围，不表示连续覆盖。`));
    if (info.missing.length) panel.append(el("p", "coverage-warning", `缺少：${info.missing.map(kind => KIND_NAMES[kind] || kind).join("、")}。可以演示缺材料流程，结果会保留资料缺口。`));
    if (options.outside) panel.append(el("p", "coverage-warning", "当前时间窗没有覆盖上述观测，暂不能运行；以上计数为场景预设资料。"));
    else if (options.edited) panel.append(el("p", "coverage-warning", "时间窗已调整；以上为预设统计，后台会按本次时间窗重新检查，实际条数可能减少。"));
    return panel;
  }
  function renderFormCoverage(agent) {
    const profile = selectedProfile(agent);
    const host = $(`#${agent}-coverage`);
    if (isCustom(agent)) {
      const note = el("section", "coverage-card unknown");
      note.append(el("h3", "", "使用自己提供的材料"), el("p", "", "运行前会按服务、环境和时间检查材料。空项会保留为缺口；只修改服务名称不会自动获得该服务的数据。"));
      host.replaceChildren(note); return;
    }
    if (!profile) { host.replaceChildren(coveragePanel(null)); return; }
    const timing = agent === "ops" ? currentProfileTime(profile) : {};
    host.replaceChildren(coveragePanel(profile.coverage, timing));
  }
  function applyProfile(agent) {
    if (isCustom(agent)) {
      $(`#${agent}-material-editor`).hidden = false;
      $(`#${agent}-edit-materials`).hidden = true;
      text(`#${agent}-profile-description`, "直接输入或粘贴材料。服务、环境与变更可自行填写；材料中的内容须属于本次范围。");
      const observed = $(`#${agent}-material-observed`);
      if (!observed.value) observed.value = agent === "ops" && $("#ops-end").value || toLocalInput(new Date().toISOString());
      renderFormCoverage(agent); updateMaterialMode(agent); updateAgentState(agent); return;
    }
    const profile = selectedProfile(agent);
    if (!profile) return;
    $(`#${agent}-material-editor`).hidden = true;
    $(`#${agent}-edit-materials`).hidden = false;
    const params = profile.params;
    if (agent === "ops") {
      for (const key of ["service", "environment"]) {
        $(`#ops-${key}`).value = params[key] || "";
      }
      $("#ops-question").value = typeof params.question === "string" ? params.question : "";
      $("#ops-start").value = toLocalInput(params.start);
      $("#ops-end").value = toLocalInput(params.end);
      $("#ops-workflow").value = ["auto", "single", "swarm"].includes(params.workflow) ? params.workflow : "auto";
    } else {
      $("#release-change").value = params.change_id || "";
      $("#release-service").value = params.service || "order-service";
      $("#release-environment").value = params.environment || "staging";
    }
    const owner = typeof profile.owner === "string" ? `演示负责人：${profile.owner}。` : "";
    const expected = { completed: "生成演示报告", complete: "生成演示报告", blocked: "展示规则阻断", incomplete: "展示材料缺口", partial: "展示材料缺口", healthy: "正常样本对照", waiting_human: "暂停等待演示复核", review_acknowledged: "手动记录演示阅读意见" }[profile.expected_status];
    text(`#${agent}-profile-description`, [profile.description, owner, expected ? `演示目标：${expected}。` : ""].filter(Boolean).join(" "));
    $(`#${agent}-error`).hidden = true;
    updateMaterialMode(agent);
    renderFormCoverage(agent);
    if (agent === "release") updateChange();
    else updateAgentState(agent);
  }
  function populateProfiles(agent, firstLoad) {
    const select = $(`#${agent}-profile`);
    const previous = select.value;
    const available = profilesFor(agent);
    $(`#${agent}-profile-field`).hidden = false;
    select.replaceChildren();
    for (const profile of available) select.append(new Option(profile.label || profile.id, profile.id));
    select.append(new Option("自己输入材料", "custom"));
    if (previous === "custom") { select.value = "custom"; applyProfile(agent); return; }
    const preferred = state.bootstrap?.defaults?.[agent]?.profile_id;
    const choice = available.find(item => item.id === previous) || available.find(item => item.id === preferred) || available.find(item => item.id === "complete-demo") || available[0];
    if (choice) {
      select.value = choice.id;
      if (firstLoad || previous !== choice.id) applyProfile(agent);
      else renderFormCoverage(agent);
    } else {
      select.value = "custom";
      applyProfile(agent);
    }
  }
  function selectedMode(agent) { return $(`input[name="${agent}-mode"]:checked`)?.value || "demo"; }
  function updateAgentState(agent) {
    const config = state.bootstrap?.agents?.[agent];
    const mode = selectedMode(agent);
    const note = $(`#${agent}-model-note`);
    if (!config) return;
    const unavailable = !config.available;
    const ready = mode === "demo" || config.api_key_configured;
    note.classList.toggle("warning", unavailable || !ready);
    const problem = profileProblem(agent);
    text(`#${agent}-model-label`, `模型：${modelLabel(mode)}`);
    if (unavailable) note.textContent = config.config_message || "暂时找不到该助手，请检查本机项目配置。";
    else if (problem) note.textContent = problem;
    else if (mode === "demo") note.textContent = "离线脚本读取本次材料，不发送模型请求。结果用于体验流程，不代表真实模型分析效果。";
    else if (!ready) note.textContent = config.config_message || "本机尚未配置可用模型密钥，请在本机项目配置后重新连接。此页面不接收密钥。";
    else note.textContent = `真实模型 API：${config.model || "已配置"}。本次选定材料会发送至已配置接口并产生实际用量，请先检查其中是否含敏感信息。`;
    note.classList.toggle("warning", unavailable || !ready || Boolean(problem));
    $(`#${agent}-submit`).disabled = state.submitting || state.materialLoading[agent] || unavailable || !ready || Boolean(problem) || (agent === "release" && !$("#release-change").value);
    text(`#overview-${agent}-status`, unavailable ? "暂不可用" : "已就绪");
    updateSourceLabel(agent);
  }
  function updateChange() {
    const profile = selectedProfile("release");
    const change = (state.bootstrap?.changes || []).find(item => item.id === $("#release-change").value);
    text("#change-description", isCustom("release") ? "填写本次变更的材料。相同编号的新运行也会保存独立输入和报告。" : profile?.description || change?.description || "选择本次要检查的材料。");
    updateAgentState("release");
  }

  const MATERIAL_FIELDS = {
    ops: [
      ["metrics", "指标记录", "每行：指标名=数值 单位。例如 request_error_rate=0.183 ratio；也可粘贴标准记录JSON数组。"],
      ["logs", "日志原文", "每行一条日志，粘贴实际原文；请先移除密钥等敏感内容。"],
      ["traces", "调用链路", "每行：操作|状态|总耗时毫秒|子步骤:耗时。例如 POST /orders|error|3036|db.acquire:3001"],
      ["changes", "变更记录", "每行一条发布或配置变化说明；保留具体前后值。"],
      ["runbooks", "参考手册（可选）", "粘贴与本次问题相关的操作手册。手册不能替代当前观测。"]
    ],
    release: [
      ["release_purpose", "发布目的", "说明为什么发布。"], ["change_summary", "变更说明", "说明版本、功能或配置发生了哪些变化。"],
      ["owner", "材料中的负责人", "填写实际负责人或明确的演示角色。"], ["rollback_plan", "回退计划", "填写回退条件、目标版本和步骤；计划不代表已经执行。"],
      ["validation_plan", "验证计划", "填写验证安排和验收标准。"], ["config_before", "变更前连接池上限", "没有该数据就留空。", "number"],
      ["config_after", "变更后连接池上限", "没有该数据就留空。", "number"], ["code_diff", "代码差异", "粘贴相关代码diff或明确的变更说明。"],
      ["ci_status", "测试检查状态", "", "select"], ["ci_details", "测试检查说明", "粘贴测试结果、时间、版本等信息。"],
      ["replicas", "目标实例数量", "例如 8；无记录则留空。", "number"], ["pool_max", "每个实例的连接池上限", "用于计算目标连接需求。", "number"],
      ["db_max_connections", "数据库连接总上限", "用于计算可用连接预算。", "number"], ["other_connections", "其他服务占用连接数", "只填其他服务占用，不含本次目标实例。", "number"]
    ]
  };
  function materialField(agent, key, label, placeholder, kind = "textarea") {
    const wrapper = el("div", "field");
    const title = el("label", "", label); title.htmlFor = `${agent}-material-${key}`;
    const input = el(kind === "select" ? "select" : kind === "number" ? "input" : "textarea");
    input.id = title.htmlFor; input.dataset.materialKey = key;
    if (kind === "select") {
      for (const [value, text] of [["unknown", "尚未提供"], ["passed", "已有通过记录"], ["failed", "已有失败记录"]]) input.append(new Option(text, value));
    } else if (kind === "number") { input.type = "number"; input.min = "0"; input.step = "any"; input.placeholder = placeholder; }
    else { input.rows = key === "logs" || key === "code_diff" ? 5 : 3; input.maxLength = 60000; input.placeholder = placeholder; }
    wrapper.append(title, input);
    if (kind === "number") wrapper.append(el("p", "field-help", "可选；留空表示未知，不会自动填0。"));
    return wrapper;
  }
  function createMaterialEditor(agent) {
    const host = $(`#${agent}-material-editor`);
    host.append(el("h3", "", "输入本次材料"), el("p", "material-intro", "只填已经掌握的内容。未提供的数字和材料不会自动补齐；每次提交会保存独立输入快照。"));
    const toolbar = el("div", "field-grid");
    const sourceWrap = el("div", "field");
    const sourceLabel = el("label", "", "材料来源声明"); sourceLabel.htmlFor = `${agent}-material-source`;
    const source = el("select"); source.id = sourceLabel.htmlFor;
    source.append(new Option("用户提供的资料（未经系统核实）", "user"), new Option("模拟演示资料", "synthetic"));
    sourceWrap.append(sourceLabel, source);
    const formatWrap = el("div", "field");
    const formatLabel = el("label", "", "输入方式"); formatLabel.htmlFor = `${agent}-material-format`;
    const format = el("select"); format.id = formatLabel.htmlFor;
    format.append(new Option("简易表单", "fields"), new Option("JSON材料 / 文件导入", "json"));
    formatWrap.append(formatLabel, format); toolbar.append(sourceWrap, formatWrap); host.append(toolbar);
    const fields = el("div", "material-fields"); fields.id = `${agent}-material-fields`;
    const observedWrap = el("div", "field"); const observedLabel = el("label", "", "本次材料观测 / 评审时间"); observedLabel.htmlFor = `${agent}-material-observed`;
    const observed = el("input"); observed.type = "datetime-local"; observed.step = "1"; observed.id = observedLabel.htmlFor;
    observedWrap.append(observedLabel, observed, el("p", "field-help", "简易表单的记录使用此时间；不确定时间时，请先核实。带独立时间的原始记录可用JSON输入。")); fields.append(observedWrap);
    const fieldGrid = el("div", "material-field-grid");
    MATERIAL_FIELDS[agent].forEach(([key, label, placeholder, kind]) => fieldGrid.append(materialField(agent, key, label, placeholder, kind)));
    fields.append(fieldGrid); host.append(fields);
    const advanced = el("div", "material-json"); advanced.id = `${agent}-material-json`; advanced.hidden = true;
    const jsonLabel = el("label", "field-label", "JSON材料内容"); jsonLabel.htmlFor = `${agent}-material-data`;
    const json = el("textarea"); json.id = jsonLabel.htmlFor; json.rows = 13; json.spellcheck = false; json.maxLength = 524288; json.placeholder = "粘贴原生材料JSON；可先载入场景示例查看结构。";
    const fileWrap = el("div", "material-import"); const fileLabel = el("label", "", "导入 JSON，自动填写范围"); fileLabel.htmlFor = `${agent}-material-file`;
    const file = el("input"); file.id = fileLabel.htmlFor; file.type = "file"; file.accept = ".json,application/json";
    const importStatus = el("p", "field-help", "选择材料文件后，自动识别服务、环境和资料来源。故障材料还会填写时间，发布材料会填写变更编号。");
    importStatus.id = `${agent}-import-status`; importStatus.setAttribute("role", "status");
    const importError = el("p", "form-error"); importError.id = `${agent}-import-error`; importError.hidden = true; importError.setAttribute("role", "alert");
    file.addEventListener("change", () => importMaterialFile(agent, file)); fileWrap.append(fileLabel, file, importStatus, importError);
    $(`#${agent}-import-slot`).append(fileWrap);
    const recognize = el("button", "button button-secondary", "识别 JSON 并填写范围"); recognize.type = "button";
    recognize.addEventListener("click", () => recognizeMaterial(agent));
    const rebind = el("button", "button button-secondary", "将演示材料绑定到当前范围"); rebind.type = "button"; rebind.id = `${agent}-material-rebind`; rebind.addEventListener("click", () => rebindMaterialScope(agent));
    const actions = el("div", "material-actions"); actions.append(recognize, rebind);
    const details = el("details", "material-json-details"); details.id = `${agent}-json-details`; details.open = true;
    details.append(el("summary", "", "展开粘贴或编辑 JSON 材料"), jsonLabel, json, actions,
      el("p", "field-help", "粘贴 JSON 后点击识别。调整模拟材料的范围标签时，请核对表单后再绑定。"));
    advanced.append(details); host.append(advanced);
    const clear = el("button", "text-button", "清空材料草稿"); clear.type = "button";
    clear.addEventListener("click", () => {
      $$('[data-material-key]', fields).forEach(input => input.value = input.tagName === "SELECT" ? "unknown" : ""); json.value = ""; file.value = "";
      state.materialDirty[agent] = false; recognizedMaterials[agent] = ""; text(`#${agent}-import-status`, "材料草稿已清空，可重新选择 JSON 文件。"); toast("材料草稿已清空。");
    });
    host.append(clear);
    host.addEventListener("input", () => { state.materialDirty[agent] = true; });
    source.addEventListener("change", () => { state.materialDirty[agent] = true; updateMaterialMode(agent); updateAgentState(agent); });
    format.addEventListener("change", () => updateMaterialMode(agent));
  }
  function updateSourceLabel(agent) {
    const synthetic = !isCustom(agent) || $(`#${agent}-material-source`).value === "synthetic";
    text(`#${agent}-source-label`, `材料：${synthetic ? "模拟演示" : "用户提供（未核实）"}`);
  }
  function updateMaterialMode(agent) {
    const json = $(`#${agent}-material-format`).value === "json";
    $(`#${agent}-material-json`).hidden = !json;
    $(`#${agent}-material-fields`).hidden = json;
    $(`#${agent}-material-observed`).required = isCustom(agent) && !json;
    $$('input,select,textarea', $(`#${agent}-material-fields`)).forEach(input => input.disabled = !isCustom(agent) || json);
    $$('input,textarea', $(`#${agent}-material-json`)).forEach(input => input.disabled = !isCustom(agent) || !json);
    $(`#${agent}-material-rebind`).disabled = $(`#${agent}-material-source`).value !== "synthetic";
    updateSourceLabel(agent);
  }
  function enterCustom(agent) {
    if (!isCustom(agent)) { $(`#${agent}-profile`).value = "custom"; applyProfile(agent); toast("已切换为自填材料，请提供当前范围的实际记录。"); }
    if (agent === "release") updateChange();
  }
  async function loadMaterialTemplate(agent) {
    const profile = selectedProfile(agent);
    if (!profile || state.materialLoading[agent]) return;
    const errorNode = $(`#${agent}-error`);
    if (!supportsFeature("custom_materials")) { errorNode.textContent = compatibilityMessage() || OLD_BACKEND_MESSAGE; errorNode.hidden = false; return; }
    if (state.materialDirty[agent]) { errorNode.textContent = "已有材料草稿，已为你保留。先选择“自己输入材料”继续编辑，或清空草稿后再载入其他示例。"; errorNode.hidden = false; return; }
    state.materialLoading[agent] = true;
    const button = $(`#${agent}-edit-materials`); button.disabled = true; button.textContent = "正在载入材料…";
    try {
      const payload = await api(`/api/material-template?agent=${agent}&profile_id=${encodeURIComponent(profile.id)}`);
      if (payload.materials?.format !== "json" || !payload.materials.data || typeof payload.materials.data !== "object") throw new Error("示例材料格式不完整，请刷新后再试。");
      // A slow template response must never overwrite text entered meanwhile.
      if (state.materialDirty[agent] || selectedProfile(agent)?.id !== profile.id) throw new Error("页面已修改，未覆盖当前材料草稿。请重新选择操作。");
      $(`#${agent}-material-data`).value = JSON.stringify(payload.materials.data, null, 2);
      $(`#${agent}-material-format`).value = "json"; $(`#${agent}-material-source`).value = payload.materials.synthetic === true ? "synthetic" : "user";
      const params = payload.params || {};
      for (const key of ["service", "environment"]) if (params[key]) $(`#${agent}-${key}`).value = params[key];
      if (agent === "release" && params.change_id) $("#release-change").value = params.change_id;
      $(`#${agent}-profile`).value = "custom"; applyProfile(agent); state.materialDirty[agent] = true;
      errorNode.hidden = true; toast("示例已复制为可编辑材料；原场景和已有报告不受影响。");
    } catch (failure) { errorNode.textContent = failure.message; errorNode.hidden = false; }
    finally { state.materialLoading[agent] = false; button.disabled = false; button.textContent = "载入此场景材料并编辑"; }
  }
  async function importMaterialFile(agent, fileInput) {
    const file = fileInput.files?.[0]; if (!file) return;
    if (state.materialLoading[agent]) return;
    await recognizeMaterial(agent, file);
    fileInput.value = "";
  }
  async function recognizeMaterial(agent, file = null) {
    if (state.materialLoading[agent]) return false;
    const errorNode = $(`#${agent}-import-error`);
    const editor = $(`#${agent}-material-data`);
    const previous = editor.value;
    const scopeBefore = ["service", "environment", ...(agent === "ops" ? ["start", "end"] : ["change"])].map(key => $(`#${agent}-${key}`).value).join("|");
    const profileBefore = $(`#${agent}-profile`).value;
    const previousProfile = selectedProfile(agent);
    state.materialLoading[agent] = true; updateAgentState(agent);
    errorNode.hidden = true;
    text(`#${agent}-import-status`, "正在校验材料并识别范围…");
    try {
      if (file && file.size > (state.bootstrap?.limits?.max_material_bytes || 524288)) throw new Error("材料文件超过512KiB，请缩小材料范围。");
      const raw = file ? await file.text() : previous;
      const document = JSON.parse(raw.replace(/^\uFEFF/, ""));
      const result = await api("/api/material-preview", {method:"POST", body:JSON.stringify({agent, document})});
      const scopeNow = ["service", "environment", ...(agent === "ops" ? ["start", "end"] : ["change"])].map(key => $(`#${agent}-${key}`).value).join("|");
      if (previous !== editor.value || scopeBefore !== scopeNow || profileBefore !== $(`#${agent}-profile`).value) throw new Error("识别期间表单发生修改，请再次导入或识别。");
      editor.value = JSON.stringify(result.materials.data, null, 2);
      recognizedMaterials[agent] = editor.value;
      $(`#${agent}-material-format`).value = "json";
      $(`#${agent}-material-source`).value = result.materials.synthetic ? "synthetic" : "user";
      for (const key of ["service", "environment"]) $(`#${agent}-${key}`).value = result.params[key];
      if (agent === "ops") {
        $("#ops-start").value = toLocalInput(result.params.start); $("#ops-end").value = toLocalInput(result.params.end);
        $("#ops-material-observed").value = toLocalInput(result.params.end);
        const question = $("#ops-question");
        if (!question.value.trim() || question.value === previousProfile?.params?.question || question.value === state.bootstrap?.defaults?.ops?.question) {
          question.value = `请根据本次材料分析 ${result.params.service} 的异常，引用证据并说明候选原因、待验证项和下一步排查。`;
        }
      } else $("#release-change").value = result.params.change_id;
      $(`#${agent}-profile`).value = "custom"; state.materialDirty[agent] = true; applyProfile(agent);
      $(`#${agent}-json-details`).open = false;
      const missing = result.coverage.missing_kinds.map(kind => KIND_NAMES[kind] || kind);
      const status = `${file ? file.name + " · " : ""}已识别 ${result.coverage.current_count} 条当前材料 · ${result.params.service} / ${result.params.environment} · ${result.source_label}${missing.length ? " · 缺少" + missing.join("、") : ""}。请核对下方范围后开始。`;
      text(`#${agent}-import-status`, status);
      $(`#${agent}-coverage`).replaceChildren(coveragePanel(result.coverage));
      $(`#${agent}-error`).hidden = true;
      toast("材料已校验，范围已自动填写。");
      return true;
    } catch (failure) {
      errorNode.textContent = failure instanceof SyntaxError ? "JSON格式无效，请检查括号、引号和逗号。" : failure.message;
      errorNode.hidden = false; text(`#${agent}-import-status`, "导入未完成，请查看下方原因。"); return false;
    } finally { state.materialLoading[agent] = false; updateAgentState(agent); }
  }
  function rebindJsonScope(agent, data, scope) {
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("材料JSON必须为对象。");
    const result = JSON.parse(JSON.stringify(data));
    if (agent === "ops") {
      for (const key of ["metrics", "logs", "traces", "changes", "runbooks"]) for (const row of result[key] || []) { row.service = scope.service; row.environment = scope.environment; row.synthetic = true; }
    } else {
      const entries = Object.values(result.changes || {});
      if (entries.length !== 1) throw new Error("仅能绑定包含一个变更的演示材料，请先保留本次变更。");
      const change = entries[0]; change.scope = {...change.scope, service:scope.service, environment:scope.environment, change_id:scope.change_id};
      for (const rows of Object.values(change.evidence || {})) for (const row of rows) { row.service = scope.service; row.environment = scope.environment; row.change_id = scope.change_id; row.synthetic = true; }
      result.changes = {[scope.change_id]:change};
    }
    return result;
  }
  function rebindMaterialScope(agent) {
    const errorNode = $(`#${agent}-error`);
    try {
      if ($(`#${agent}-material-source`).value !== "synthetic") throw new Error("此操作仅适用于明确标注的模拟材料。");
      const scope = {service:$(`#${agent}-service`).value.trim(),environment:$(`#${agent}-environment`).value.trim(),change_id:$("#release-change").value.trim()};
      if (![scope.service, scope.environment, ...(agent === "release" ? [scope.change_id] : [])].every(value => /^[A-Za-z0-9_-]{1,80}$/.test(value))) throw new Error("请先填写有效的服务、环境和变更编号。");
      const result = rebindJsonScope(agent, JSON.parse($(`#${agent}-material-data`).value), scope);
      $(`#${agent}-material-data`).value = JSON.stringify(result, null, 2); state.materialDirty[agent] = true; errorNode.hidden = true;
      toast("模拟资料已绑定当前范围；原始内容仍是演示设定，未采集真实数据。");
    } catch (failure) { errorNode.textContent = failure instanceof SyntaxError ? "请先填写有效JSON。" : failure.message; errorNode.hidden = false; }
  }
  function collectMaterials(agent) {
    const format = $(`#${agent}-material-format`).value;
    const raw = {format, synthetic:$(`#${agent}-material-source`).value === "synthetic"};
    if (format === "json") {
      try { raw.data = JSON.parse($(`#${agent}-material-data`).value); } catch (_) { throw new Error("材料JSON格式无效，请检查后重试。"); }
      if (!raw.data || typeof raw.data !== "object" || Array.isArray(raw.data)) throw new Error("材料JSON必须为对象。");
    } else {
      const stamp = new Date($(`#${agent}-material-observed`).value);
      if (Number.isNaN(stamp.getTime())) throw new Error("请填写材料观测或评审时间。");
      raw.observed_at = stamp.toISOString();
      for (const [key,,,kind] of MATERIAL_FIELDS[agent]) {
        const value = $(`#${agent}-material-${key}`).value;
        raw[key] = kind === "number" ? value.trim() === "" ? null : Number(value) : value;
      }
      const useful = MATERIAL_FIELDS[agent].some(([key,,,kind]) => kind === "number" ? raw[key] !== null : kind === "select" ? raw[key] !== "unknown" : raw[key].trim());
      if (!useful) throw new Error("请至少输入一项当前材料；未知数字可留空。");
    }
    if (new TextEncoder().encode(JSON.stringify(raw)).length > (state.bootstrap?.limits?.max_material_bytes || 524288)) throw new Error("材料超过512KiB，请缩小范围再提交。");
    return raw;
  }
  async function bootstrap() {
    $("#retry-connect").disabled = true;
    try {
      const payload = await api("/api/bootstrap", { timeoutMs: 40000 });
      if (!payload?.csrf_token || !payload.agents) throw new Error("工作台初始化信息不完整，请重新启动本机服务。");
      const firstLoad = !state.bootstrap;
      state.bootstrap = payload;
      state.backendMismatch = false;
      updateBackendControls();
      if (firstLoad && !hasProfiles("ops")) {
        const defaults = payload.defaults?.ops || {};
        text("#ops-question", defaults.question || "");
        $("#ops-question").value = defaults.question || "";
        for (const key of ["service", "environment"]) if (typeof defaults[key] === "string") $("#ops-" + key).value = defaults[key];
        $("#ops-start").value = toLocalInput(defaults.start);
        $("#ops-end").value = toLocalInput(defaults.end);
      }
      $("#change-options").replaceChildren();
      for (const change of payload.changes || []) if (typeof change.id === "string") $("#change-options").append(new Option(change.label || change.id, change.id));
      if (!hasProfiles("release") && !$("#release-change").value) $("#release-change").value = payload.changes?.[0]?.id || "";
      populateProfiles("ops", firstLoad);
      populateProfiles("release", firstLoad);
      updateAgentState("ops");
      updateChange();
      setConnection(true);
      await loadTasks();
      await loadStorage();
      if (state.view === "cases") await loadCases();
    } catch (error) { setConnection(false, error.message); }
    finally { $("#retry-connect").disabled = false; }
  }

  function recentRow(task) {
    const button = el("button", "recent-row");
    button.type = "button";
    button.append(el("span", `task-kind-icon ${task.agent === "release" ? "release" : ""}`, task.agent === "release" ? "✓" : "⌁"));
    const main = el("span", "recent-task-main");
    main.append(el("span", "recent-task-title", task.title || AGENT_NAMES[task.agent] || "任务"), el("span", "recent-task-meta", `${AGENT_NAMES[task.agent] || "任务"} · ${sourceLabel(task)} · ${modelLabel(task.mode)} · ${date(task.created_at, true)}`));
    button.append(main, badge(task.status), el("span", "chevron", "›"));
    button.addEventListener("click", () => selectTask(task.id));
    return button;
  }
  function storageCard(title, status, detail, tone = "unknown") {
    const card = el("div", `storage-card ${tone}`);
    card.append(el("span", "storage-label", title), el("b", "storage-state", status), el("p", "", detail));
    return card;
  }
  function renderStorage() {
    const host = $("#storage-cards"); host.replaceChildren();
    if (state.storageError) {
      text("#storage-mode", "状态暂不可用"); text("#storage-note", state.storageError + " 请刷新重试，未确认连接成功。");
      text("#report-storage-label", "报告存储待确认");
      host.append(storageCard("Java 服务", "未确认", "未取得有效状态"), storageCard("SQLite", "未确认", "未取得有效状态"), storageCard("Redis", "未确认", "未取得有效状态"), storageCard("任务队列", "未确认", "未取得有效状态"));
      return;
    }
    const info = state.storage;
    if (!info) { host.append(el("div", "empty-inline", "正在核对存储状态…")); return; }
    const javaMode = info.mode === "java";
    text("#storage-mode", javaMode ? "Java 管理业务存储" : "兼容模式 · 本地存储");
    const javaReady = info.java?.available === true, sqliteReady = info.sqlite?.available === true, redisReady = info.redis?.available === true;
    host.append(storageCard("Java 服务", javaMode ? javaReady ? "已连接" : "未连接" : "未启用", javaMode ? "管理任务、案例和报告存储" : "当前使用兼容模式", javaMode ? javaReady ? "ready" : "caution" : "unknown"));
    host.append(storageCard("SQLite", sqliteReady ? "可用" : "未确认", info.sqlite?.owner === "java" ? "Java 管理任务与案例记录" : "本地任务与案例记录", sqliteReady ? "ready" : "caution"));
    const countText = Number.isInteger(info.redis?.report_count) && info.redis.report_count >= 0 ? `；报告 ${count(info.redis.report_count)} 份` : "";
    const persistence = redisReady && info.redis?.durability === "AOF" ? "；AOF 持久化" : "";
    host.append(storageCard("Redis", redisReady ? "已连接" : info.reports === "redis" ? "未连接" : "未用于报告", info.reports === "redis" ? `保存报告正文${countText}${persistence}` : "当前报告保存在文件中", redisReady ? "ready" : info.reports === "redis" ? "caution" : "unknown"));
    const queue = info.queue;
    if (queue?.mode === "kafka") {
      host.append(storageCard("Kafka 任务队列", queue.available ? "可用" : "等待连接", queue.available ? "后台执行进程从 Kafka 领取任务" : "已提交的待发送任务会保存在 SQLite", queue.available ? "ready" : "caution"));
    } else {
      host.append(storageCard("任务队列", queue?.mode === "local" ? "本机排队" : "未确认", queue?.mode === "local" ? "由当前工作台启动执行进程" : "当前后台未提供队列状态", "unknown"));
    }
    text("#report-storage-label", info.reports === "redis" ? redisReady ? "报告：Redis" : "报告：Redis 待恢复" : info.reports === "files" ? "报告：本地文件" : "报告存储待确认");
    text("#storage-note", (typeof info.message === "string" && info.message ? info.message + " " : "") + `状态核对时间：${date(new Date().toISOString())}。资料来源与模型运行方式另行记录。`);
  }
  async function loadStorage() {
    if (!state.bootstrap || state.storageBusy) return;
    state.storageBusy = true; $("#refresh-storage").disabled = true;
    try {
      const payload = await api("/api/storage-status");
      if (!payload || !["java", "legacy"].includes(payload.mode) || !payload.java || !payload.sqlite || !payload.redis) throw new Error("后台尚未提供有效的存储状态。");
      state.storage = payload; state.storageError = "";
    } catch (failure) { state.storageError = failure.message; }
    finally { state.storageBusy = false; $("#refresh-storage").disabled = false; renderStorage(); }
  }
  function matchesCase(item) {
    if (state.caseVerified === "verified" && item.verified !== true) return false;
    if (state.caseVerified === "unverified" && item.verified === true) return false;
    const query = state.caseSearch.trim().toLocaleLowerCase();
    return !query || [item.id, item.service, item.environment, item.summary, item.source_job_id].some(value => typeof value === "string" && value.toLocaleLowerCase().includes(query));
  }
  function renderCases() {
    const host = $("#case-list"); host.replaceChildren();
    const items = state.cases.filter(matchesCase);
    text("#case-match-count", `显示 ${items.length} / ${state.cases.length} 项`);
    $("#refresh-cases").disabled = state.casesBusy;
    if (state.casesError) {
      const issue = el("section", "quality-panel error-panel"); issue.append(el("h3", "", "案例读取提示"), el("p", "", state.casesError)); host.append(issue);
    }
    if (state.casesBusy && !state.cases.length) { host.append(el("div", "card empty-inline", "正在读取案例…")); return; }
    if (!items.length) {
      const empty = el("section", "card empty-detail");
      empty.append(el("h2", "", state.casesError ? "案例暂不可用" : state.caseSearch.trim() || state.caseVerified !== "all" ? "没有符合条件的案例" : "案例库为空"), el("p", "", state.casesError ? "请检查存储服务或刷新重试。" : "Agent运行后保存的案例会显示在这里；内置演示场景不属于本列表。"));
      host.append(empty); return;
    }
    for (const item of items) {
      const card = el("article", "card case-card");
      const top = el("div", "case-card-top");
      top.append(el("code", "", item.id), el("span", `badge ${item.verified === true ? "teal-badge" : "amber-badge"}`, item.verified === true ? "人工已确认" : "尚未确认"));
      card.append(top, el("h2", "", `${item.service || "服务未记录"} / ${item.environment || "环境未记录"}`), el("p", "case-summary", typeof item.summary === "string" && item.summary ? item.summary : "未提供案例摘要。"));
      const meta = el("div", "case-meta");
      meta.append(el("span", "", item.source === "java" ? "来源：Java案例存储" : item.source === "ops" ? "来源：故障诊断记忆" : "来源：按案例记录判断"));
      if (item.source_job_id) meta.append(el("span", "", "关联任务：" + item.source_job_id));
      const actions = el("div", "case-actions"); const purge = el("button", "button button-danger", "永久删除案例"); purge.type = "button";
      purge.disabled = state.purging || state.casesBusy || Boolean(state.casesError) || !supportsFeature("cases") || !supportsFeature("permanent_delete");
      if (!supportsFeature("permanent_delete")) purge.title = "当前后台尚不支持永久删除，请更新并重新启动。";
      purge.addEventListener("click", () => openPurgeDialog("case", item)); actions.append(purge);
      card.append(meta, actions); host.append(card);
    }
  }
  async function loadCases() {
    if (!state.bootstrap || state.casesBusy) return;
    if (!supportsFeature("cases")) { state.cases = []; state.casesError = "当前后台尚不支持案例库，请更新工作台后重新启动。"; renderCases(); return; }
    state.casesBusy = true; renderCases();
    try {
      const payload = await api("/api/cases");
      if (!Array.isArray(payload.cases)) throw new Error("案例列表格式不完整。");
      state.cases = payload.cases.filter(item => item && typeof item.id === "string"); state.casesError = "";
    } catch (failure) { state.casesError = failure.message; }
    finally { state.casesBusy = false; renderCases(); }
  }
  function canConfirmPurge(value, target) { return Boolean(target && ["case", "task"].includes(target.kind) && typeof target.id === "string" && target.id.length > 0 && value === target.id); }
  function openPurgeDialog(kind, item) {
    if (state.purging || state.reviewing) return;
    if (!supportsFeature("permanent_delete") || kind === "case" && !supportsFeature("cases")) { toast("当前后台尚不支持永久删除，请更新并重新启动工作台。"); return; }
    if (kind === "task" && (!isTrashed(item) || ACTIVE.has(item.status))) { toast("请先将已结束的任务移到回收站，再选择永久删除。"); return; }
    state.purgeTarget = {kind, id:item.id};
    text("#purge-title", kind === "case" ? "永久删除案例" : "永久删除报告与任务");
    text("#purge-description", kind === "case" ? "此案例将从案例库永久删除，无法恢复。内置演示场景不受影响；关联报告按后台规则分别管理。" : "此回收站记录及对应报告将永久删除，无法恢复。请确认编号，取消可保留现有记录。");
    text("#purge-target", item.id);
    $("#purge-confirmation").value = ""; $("#purge-submit").disabled = true; $("#purge-submit").textContent = "永久删除"; $("#purge-cancel").disabled = false; $("#purge-error").hidden = true;
    const dialog = $("#purge-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
    $("#purge-confirmation").focus();
  }
  function closePurgeDialog() {
    if (state.purging) return;
    const dialog = $("#purge-dialog");
    if (typeof dialog.close === "function") dialog.close(); else dialog.removeAttribute("open");
    state.purgeTarget = null;
  }
  async function submitPurge(event) {
    event.preventDefault();
    const target = state.purgeTarget;
    if (state.purging || !canConfirmPurge($("#purge-confirmation").value, target)) return;
    if (!supportsFeature("permanent_delete")) { text("#purge-error", "后台版本不支持永久删除，请取消后重新连接。"); $("#purge-error").hidden = false; return; }
    state.purging = true; $("#purge-submit").disabled = true; $("#purge-cancel").disabled = true; $("#purge-submit").textContent = "正在永久删除…"; $("#purge-error").hidden = true;
    try {
      const path = target.kind === "case" ? `/api/cases/${encodeURIComponent(target.id)}/purge` : `/api/tasks/${encodeURIComponent(target.id)}/purge`;
      await api(path, {method:"POST",body:JSON.stringify({confirmation:target.id})});
    } catch (failure) {
      text("#purge-error", failure.message); $("#purge-error").hidden = false;
      state.purging = false; $("#purge-cancel").disabled = false; $("#purge-submit").textContent = "永久删除"; $("#purge-submit").disabled = !canConfirmPurge($("#purge-confirmation").value, state.purgeTarget); return;
    }
    state.purging = false; closePurgeDialog();
    if (target.kind === "case") { state.cases = state.cases.filter(item => item.id !== target.id); renderCases(); await loadCases(); }
    else {
      state.tasks = state.tasks.filter(item => item.id !== target.id); state.trash = state.trash.filter(item => item.id !== target.id); state.reviewDrafts.delete(target.id);
      if (state.selected === target.id) { state.selected = null; state.detail = null; emptyTaskDetail("该记录已永久删除"); }
      renderTasks(); await loadTasks(); await loadTrash();
    }
    toast(target.kind === "case" ? "案例已永久删除。" : "报告与任务已永久删除。"); loadStorage();
  }
  function matchesTask(task) {
    if (state.filter !== "all" && task.agent !== state.filter) return false;
    if (state.taskStatus !== "all" && (state.taskStatus === "active" ? !ACTIVE.has(task.status) : task.status !== state.taskStatus)) return false;
    const query = state.taskSearch.trim().toLocaleLowerCase();
    return !query || [task.id, task.title, task.review_id, task.service, task.environment, task.params?.service, task.params?.environment, task.profile_label].some(value => typeof value === "string" && value.toLocaleLowerCase().includes(query));
  }
  function renderTasks() {
    const tasks = state.tasks;
    text("#stat-active", tasks.filter(task => ACTIVE.has(task.status)).length);
    text("#stat-review", tasks.filter(task => task.status === "waiting_human").length);
    text("#stat-reports", tasks.filter(task => task.report_available).length);
    const listing = state.bin === "trash" ? state.trash : tasks;
    text("#task-total", listing.length);
    text("#task-list-title", state.bin === "trash" ? "回收站" : "全部任务");
    const active = tasks.filter(task => ACTIVE.has(task.status) || task.status === "waiting_human").length;
    text("#nav-task-count", active);
    $("#nav-task-count").hidden = !active;
    const recent = $("#recent-tasks");
    recent.replaceChildren();
    if (!tasks.length) recent.append(el("div", "empty-inline", state.listError || "还没有任务，从上方选择一位助手开始。"));
    else tasks.slice(0, 4).forEach(task => recent.append(recentRow(task)));
    const list = $("#task-list");
    const scroll = list.scrollTop;
    list.replaceChildren();
    const filtered = listing.filter(matchesTask);
    text("#task-match-count", `显示 ${filtered.length} / ${listing.length} 项`);
    if (!filtered.length) list.append(el("div", "empty-inline", state.listError || (state.taskSearch.trim() || state.taskStatus !== "all" ? "没有符合搜索或状态条件的任务。" : state.bin === "trash" ? "回收站为空。移入的报告可在这里恢复。" : "这个分类下还没有任务。")));
    for (const task of filtered) {
      const button = el("button", `task-list-item${task.id === state.selected ? " selected" : ""}`);
      button.type = "button";
      button.setAttribute("aria-pressed", String(task.id === state.selected));
      const top = el("span", "list-item-top");
      top.append(el("span", "list-item-kind", AGENT_NAMES[task.agent] || "任务"), badge(task.status));
      button.append(top, el("span", "list-item-title", task.title || "未命名任务"), el("span", "list-item-date", `${date(task.created_at, true)} · ${modelLabel(task.mode)} · ${sourceLabel(task)}`));
      button.addEventListener("click", () => selectTask(task.id));
      list.append(button);
    }
    list.scrollTop = scroll;
  }
  async function loadTasks(manual = false) {
    if (state.listBusy || !state.bootstrap) return;
    state.listBusy = true;
    try {
      const payload = await api("/api/tasks");
      if (!Array.isArray(payload.tasks)) throw new Error("任务列表暂时无法读取。");
      state.tasks = payload.tasks.filter(task => task && typeof task.id === "string").sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
      state.listError = "";
      setConnection(true);
      if (state.bin === "trash") await loadTrash();
      renderTasks();
      const selected = state.tasks.find(task => task.id === state.selected);
      if (!manual && state.view === "tasks" && selected && state.detail && selected.status !== state.detail.status) loadDetail();
      if (manual) { if (state.selected) await loadDetail(); toast("任务状态已刷新"); }
    } catch (error) { state.listError = error.message; setConnection(false, error.message); if (manual) toast(error.message); }
    finally { state.listBusy = false; if (state.listError) renderTasks(); }
  }
  async function loadTrash() {
    if (state.trashBusy || !state.bootstrap) return;
    if (!supportsFeature("trash")) { state.trash = []; renderTasks(); return; }
    state.trashBusy = true;
    try {
      const payload = await api("/api/trash");
      if (!Array.isArray(payload.tasks)) throw new Error("回收站暂时无法读取。");
      state.trash = payload.tasks.filter(task => task && typeof task.id === "string");
      renderTasks();
    } catch (failure) { toast(failure.message); }
    finally { state.trashBusy = false; }
  }
  function isTrashed(task) { return task.deleted === true || Boolean(task.deleted_at) || state.trash.some(item => item.id === task.id); }
  function emptyTaskDetail(message) {
    const panel = el("div", "card empty-detail"); panel.append(el("h2", "", message), el("p", "", "选择左侧任务查看报告；移入回收站的记录可以恢复。"));
    $("#task-detail").replaceChildren(panel);
  }
  async function setBin(bin) {
    if (bin === "trash" && !supportsFeature("trash")) { toast(compatibilityMessage() || OLD_BACKEND_MESSAGE); setConnection(state.connection); return; }
    state.bin = bin; state.selected = null; state.detail = null;
    ["active", "trash"].forEach(key => { const button = $(`#tasks-${key}`); button.classList.toggle("active", key === bin); button.setAttribute("aria-pressed", String(key === bin)); });
    emptyTaskDetail(bin === "trash" ? "回收站中的报告可恢复" : "选择一项任务");
    renderTasks(); if (bin === "trash") await loadTrash();
  }
  async function changeTaskTrash(task, restore, button) {
    if (state.reviewing || state.trashBusy) return;
    if (!supportsFeature("trash")) { toast(compatibilityMessage() || OLD_BACKEND_MESSAGE); setConnection(state.connection); return; }
    button.disabled = true;
    const label = button.textContent; button.textContent = restore ? "正在恢复…" : "正在移入…";
    try {
      await api(`/api/tasks/${encodeURIComponent(task.id)}/${restore ? "restore" : "trash"}`, {method:"POST",body:"{}"});
      state.selected = null; state.detail = null; state.detailError = "";
      await loadTasks(); await loadTrash();
      emptyTaskDetail(restore ? "报告已恢复到任务列表" : "报告已移到回收站");
      toast(restore ? "已恢复，原报告和评审状态保持。" : "已移到回收站，可随时恢复。");
    } catch (failure) { toast(failure.message); button.disabled = !supportsFeature("trash"); button.textContent = label; }
  }
  function setRoute(view) {
    state.view = VIEW_NAMES[view] ? view : "overview";
    $$("[data-view]").forEach(section => section.hidden = section.dataset.view !== state.view);
    $$("[data-nav]").forEach(link => { const active = link.dataset.nav === state.view; link.classList.toggle("active", active); if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); });
    text("#breadcrumb-page", VIEW_NAMES[state.view]);
    document.title = `${VIEW_NAMES[state.view]} · 运维工作台`;
    if (state.view === "tasks" && state.selected) loadDetail();
    if (state.view === "cases" && state.bootstrap) loadCases();
    if (state.view === "overview" && state.bootstrap) loadStorage();
  }
  function selectTask(id) {
    if (typeof id !== "string") return;
    if (id !== state.selected) { state.detail = null; state.tab = "report"; state.detailError = ""; }
    state.selected = id;
    renderTasks();
    if (window.location.hash !== "#tasks") window.location.hash = "tasks";
    else setRoute("tasks");
    if (!state.detail) {
      const loading = el("div", "card empty-detail");
      loading.append(el("h2", "", "正在读取任务"), el("p", "", "进度与报告即将显示。"));
      $("#task-detail").replaceChildren(loading);
    }
    loadDetail();
  }
  async function submitTask(agent, event) {
    event.preventDefault();
    if (state.submitting || !state.bootstrap) return;
    if (state.materialLoading[agent]) return;
    if (isCustom(agent) && $(`#${agent}-material-format`).value === "json" && recognizedMaterials[agent] !== $(`#${agent}-material-data`).value) {
      if (!await recognizeMaterial(agent)) return;
    }
    const form = $(`#${agent}-form`);
    if (!form.reportValidity()) return;
    const mode = selectedMode(agent);
    const config = state.bootstrap.agents[agent];
    const errorNode = $(`#${agent}-error`);
    errorNode.hidden = true;
    if (!config?.available || (mode === "api" && !config.api_key_configured)) { errorNode.textContent = "请先完成本机助手与模型配置，然后重新连接工作台。"; errorNode.hidden = false; return; }
    const problem = profileProblem(agent);
    if (problem) { errorNode.textContent = problem; errorNode.hidden = false; return; }
    let params;
    if (agent === "ops") {
      const start = new Date($("#ops-start").value);
      const end = new Date($("#ops-end").value);
      if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || end < start) { errorNode.textContent = "请检查时间窗，结束时间应不早于开始时间。"; errorNode.hidden = false; return; }
      params = { question: $("#ops-question").value.trim(), service: $("#ops-service").value, environment: $("#ops-environment").value, start: start.toISOString(), end: end.toISOString(), workflow: $("#ops-workflow").value };
      if (!params.question) { errorNode.textContent = "请填写希望排查的问题。"; errorNode.hidden = false; return; }
    } else {
      params = { change_id: $("#release-change").value.trim() };
      if (isCustom(agent)) Object.assign(params,{service:$("#release-service").value.trim(),environment:$("#release-environment").value.trim()});
    }
    const profile = selectedProfile(agent);
    if (isCustom(agent)) params.profile_id = "custom";
    else if (hasProfiles(agent) && profile) params.profile_id = profile.id;
    let materials;
    try { if (isCustom(agent)) materials = collectMaterials(agent); }
    catch (failure) { errorNode.textContent = failure.message; errorNode.hidden = false; return; }
    state.submitting = true;
    const submit = $(`#${agent}-submit`);
    submit.textContent = "正在提交…";
    updateAgentState("ops"); updateAgentState("release");
    try {
      const payload = await api("/api/tasks", { method: "POST", body: JSON.stringify({ agent, mode, params, ...(materials ? {materials} : {}) }) });
      if (!payload.task?.id) throw new Error("未取得任务编号，请先刷新任务列表确认是否已创建。");
      state.tasks = [payload.task, ...state.tasks.filter(task => task.id !== payload.task.id)];
      if (state.bin !== "active") await setBin("active");
      selectTask(payload.task.id);
      toast("任务已提交，可在这里查看进度");
      loadTasks();
    } catch (error) { errorNode.textContent = error.message; errorNode.hidden = false; }
    finally { state.submitting = false; submit.textContent = agent === "ops" ? "开始诊断 →" : "开始评审 →"; updateAgentState("ops"); updateAgentState("release"); }
  }

  function issueText(issue) {
    if (typeof issue === "string") return issue;
    if (!issue || typeof issue !== "object") return "有一项内容需要人工核对。";
    return [issue.message || issue.description || issue.title || "有一项内容需要人工核对。", issue.path ? `（${issue.path}）` : ""].join("");
  }
  function issuePanel(title, issues, error = false) {
    const panel = el("section", `quality-panel${error ? " error-panel" : ""}`);
    panel.append(el("h3", "", title));
    const list = el("ul");
    issues.forEach(issue => list.append(el("li", "", issueText(issue))));
    panel.append(list);
    return panel;
  }
  function downloadLink(path, label, task, extension) {
    if (typeof path !== "string") return null;
    try {
      const url = new URL(path, window.location.origin);
      const expected = `/api/tasks/${encodeURIComponent(task.id)}/report.${extension}`;
      if (url.origin !== window.location.origin || url.pathname !== expected || url.search || url.hash) return null;
      const link = el("a", "button button-secondary button-small", label);
      link.href = url.pathname;
      link.download = `${task.id}.${extension}`;
      return link;
    } catch (_) { return null; }
  }
  function statusNote(task) {
    const notes = { queued: "任务已进入队列，等待本机助手开始处理。", running: "助手正在处理。当前显示后台已提供的进度，完成后会自动更新报告。", waiting_human: "材料已整理，请先阅读报告、证据和质量提示，再记录复核意见。确认阅读不构成发布批准。", blocked: "报告存在规则阻断。确认阅读不会消除阻断，也不会执行发布。", incomplete: "部分材料或分析尚未通过检查，请查看质量提示与已有证据。", interrupted: "任务已中断。已保存的报告和日志仍可查看。", failed: "本次任务未完成，请查看错误提示与执行记录。", review_acknowledged: "已记录人工阅读意见。该状态不构成生产发布授权。", changes_requested: "已记录修改意见，材料更新后需要重新评审。", rejected: "已记录拒绝意见，原报告和依据仍保留。", completed: "报告已生成。诊断结论与处置建议仍需结合实际情况核对。" };
    return notes[task.status] || "请结合报告与证据检查本次任务结果。";
  }
  function dataOf(task) { const data = task.report?.data; return data && typeof data === "object" && !Array.isArray(data) ? data : {}; }
  function warningsOf(data) {
    const issues = Array.isArray(data.quality_issues) ? [...data.quality_issues] : [];
    if (Array.isArray(data.warnings)) issues.push(...data.warnings);
    return issues;
  }
  function taskCoverage(task) {
    const data = dataOf(task);
    return task.coverage || data.coverage || task.profile?.coverage || data.profile?.coverage || null;
  }
  function taskAssessment(task) {
    const data = dataOf(task);
    const assessment = task.assessment || data.assessment || {};
    const material = assessment.material_status || coverageSummary(taskCoverage(task)).status;
    const generation = assessment.generation_status || (ACTIVE.has(task.status) ? task.status : task.report ? (data.complete === false || warningsOf(data).length ? "incomplete" : "generated") : "unknown");
    const quality = assessment.quality_status || (warningsOf(data).length ? "issues" : task.report && Array.isArray(data.quality_issues) ? "passed" : "unknown");
    return { material, generation, quality, model: assessment.model_status || task.mode, error: assessment.error };
  }
  function assessmentPanel(task) {
    const info = taskAssessment(task);
    const generationNames = { complete: "报告已生成", completed: "报告已生成", generated: "报告已生成", success: "报告已生成", incomplete: "生成待复核", quality_failed: "生成待复核", failed: "生成失败", error: "生成失败", running: "生成中", queued: "排队中", waiting_human: "报告待阅读", unknown: "生成状态待核对" };
    const qualityNames = { passed: "限定检查通过", complete: "限定检查通过", valid: "限定检查通过", issues: "需要复核", failed: "需要复核", invalid: "需要复核", error: "需要复核", unknown: "待检查", pending: "待检查" };
    const panel = el("section", "assessment-panel");
    const grid = el("div", "assessment-grid");
      for (const [label, value] of [["生成状态", generationNames[info.generation] || info.generation], ["资料状态", MATERIAL_NAMES[info.material] || "类别覆盖待核对"], ["质量检查", qualityNames[info.quality] || info.quality]]) {
      const caution = label === "资料状态" && ["partial", "empty"].includes(info.material) || label === "质量检查" && ["issues", "failed", "invalid", "error"].includes(info.quality);
      const cell = el("div", `assessment-cell${caution ? " caution" : ""}`);
      cell.append(el("span", "", label), el("b", "", value));
      grid.append(cell);
    }
    panel.append(grid, el("p", "assessment-note", "报告生成、材料类别覆盖与证据是否充分应分别判断；类别已覆盖仍可能需要补查。"));
    if (info.error) panel.append(el("p", "coverage-warning", typeof info.error === "string" ? info.error : issueText(info.error)));
    const coverage = taskCoverage(task);
    if (coverage) panel.append(coveragePanel(coverage, { title: "本次实际资料覆盖" }));
    return panel;
  }
  function reportContent(task) {
    const container = el("div", "detail-content");
    const data = dataOf(task);
    const issues = warningsOf(data);
    if (task.error) container.append(issuePanel("运行提示", [task.error], true));
    if (issues.length) container.append(issuePanel(task.status === "waiting_human" ? "等待复核，但本次仍有质量问题" : "需要留意的内容", issues, task.status === "waiting_human"));
    if (task.agent === "release" && task.report && data.analysis == null) container.append(issuePanel("本次没有可采纳的模型分析", ["仍可查看规则检查和实际取得的证据。没有模型分析不代表发布安全。"]));
    const body = typeof task.report?.markdown === "string" ? task.report.markdown : typeof data.answer === "string" ? data.answer : "";
    if (body) {
      const article = el("article", "markdown");
      // This is the only untrusted-content HTML insertion. markdown() always
      // escapes source text before emitting a small fixed set of safe tags.
      article.innerHTML = markdown(body);
      container.append(article);
    } else container.append(el("p", "log-placeholder", ACTIVE.has(task.status) ? "报告正在准备中，可先查看执行记录。" : "暂未生成可读报告，请查看执行记录和任务状态。"));
    if (task.agent === "release") appendReview(container, task, data);
    return container;
  }
  function normalizedUsage(data, agent) {
    if (agent === "ops") return data.execution?.usage || {};
    const calls = Array.isArray(data.model_calls) ? data.model_calls : [];
    const keys = ["prompt_tokens", "completion_tokens", "total_tokens"];
    const result = {};
    for (const key of keys) {
      const values = calls.map(call => call?.usage?.[key]).filter(value => typeof value === "number" && Number.isFinite(value) && value >= 0);
      result[key] = values.length ? values.reduce((total, value) => total + value, 0) : null;
    }
    return result;
  }
  function showJsonDetails(parent, title, value) {
    const details = el("details", "evidence-details");
    details.append(el("summary", "", title), el("pre", "", JSON.stringify(value, null, 2)));
    parent.append(details);
  }
  function evidenceContent(task) {
    const data = dataOf(task);
    const container = el("div", "detail-content");
    const usage = normalizedUsage(data, task.agent);
    container.append(el("h3", "detail-section-heading", "实际模型用量"));
    const grid = el("div", "token-grid");
    for (const [key, label] of [["prompt_tokens", "输入 Token"], ["completion_tokens", "输出 Token"], ["total_tokens", "总 Token"]]) { const card = el("div", "token-card"); card.append(el("span", "", label), el("b", "", count(usage[key]))); grid.append(card); }
    container.append(grid);
    let note = "仅展示模型接口实际报告的计数；未知不代表零。未计算费用。";
    if (task.mode === "demo" || data.demo === true) note = "当前为离线演示，不产生真实模型接口用量；未报告的计数显示为未知。";
    else if (data.execution?.usage_complete === false) note += " 本次用量记录不完整，显示的是已报告部分。";
    else if (task.agent === "release") {
      const calls = Array.isArray(data.model_calls) ? data.model_calls : [];
      if (!calls.length || calls.some(call => ["prompt_tokens", "completion_tokens", "total_tokens"].some(key => typeof call?.usage?.[key] !== "number"))) note += " 部分调用可能未提供完整计数。";
    }
    container.append(el("p", "metrics-note", note));
    const findings = Array.isArray(data.policy_findings) ? data.policy_findings : [];
    if (task.agent === "release") {
      container.append(el("h3", "detail-section-heading", "独立规则检查"));
      if (!findings.length) container.append(el("p", "metrics-note", "本次记录中没有规则发现，不代表所有风险都不存在。"));
      findings.forEach(finding => {
        const blocked = finding.severity === "blocker";
        const card = el("div", `finding-card${blocked ? " blocker" : ""}`);
        const top = el("div", "finding-header");
        const label = el("span", `badge ${blocked ? "red-badge" : "amber-badge"}`, blocked ? "阻断" : "提示");
        top.append(label, el("h4", "", finding.title || finding.code || "规则发现"));
        card.append(top, el("p", "", finding.detail || finding.message || finding.description || finding.explanation || ""));
        if (Array.isArray(finding.evidence_ids)) card.append(el("div", "finding-ids", `依据：${finding.evidence_ids.join(" · ")}`));
        container.append(card);
      });
    }
    const evidence = Array.isArray(data.evidence) ? data.evidence : [];
    container.append(el("h3", "detail-section-heading", `证据来源 · ${evidence.length} 条`));
    if (!evidence.length) container.append(el("p", "metrics-note", "当前没有可展示的证据记录。"));
    evidence.forEach(row => {
      const labels = [row.id || "未编号证据", row.kind || row.evidence_type, row.environment, row.timestamp ? date(row.timestamp) : ""].filter(Boolean);
      showJsonDetails(container, labels.join(" · "), row);
    });
    const issues = warningsOf(data);
    if (issues.length) container.append(issuePanel("质量与范围提示", issues));
    const calls = Array.isArray(data.model_calls) ? data.model_calls : data.execution?.model_calls;
    if (Array.isArray(calls) && calls.length) { container.append(el("h3", "detail-section-heading", "逐次调用记录")); calls.forEach((call, index) => showJsonDetails(container, `第 ${index + 1} 次 · ${call.success === false ? "未成功" : "已记录"} · ${typeof call.latency_ms === "number" ? `${(call.latency_ms / 1000).toFixed(3)} 秒` : "耗时未知"}`, call)); }
    if (data.execution) showJsonDetails(container, "查看执行统计", data.execution);
    return container;
  }
  function logsContent(task) {
    const container = el("div", "detail-content");
    const logs = Array.isArray(task.logs) ? task.logs.map(line => typeof line === "string" ? line : JSON.stringify(line)) : [];
    const toolbar = el("div", "log-toolbar");
    toolbar.append(el("span", "", `${logs.length} 条后台记录`));
    const bottom = el("button", "text-button", "滚动到最新");
    bottom.type = "button";
    toolbar.append(bottom);
    const console = el("pre", "log-console", logs.length ? logs.join("\n") : "尚无执行记录。");
    console.id = "active-log-console";
    console.setAttribute("tabindex", "0");
    console.setAttribute("aria-label", "后台执行记录");
    bottom.addEventListener("click", () => console.scrollTop = console.scrollHeight);
    container.append(toolbar, console);
    return container;
  }
  function validReviewRecord(record) {
    return Boolean(record && typeof record === "object" && !Array.isArray(record)
      && typeof record.reviewer === "string" && record.reviewer.trim()
      && ["acknowledge", "request_changes", "reject"].includes(record.decision));
  }
  function reviewDefaults(task, data, draft) {
    const demo = task.mode === "demo" || (!task.mode && (data.demo === true || data.run_mode === "scripted_demo"));
    const matching = draft && draft.digest === data.review_digest;
    const automatic = matching ? draft.automatic_demo === true : demo;
    return {
      demo,
      reviewer: matching && !(automatic && !demo) ? draft.reviewer || "" : demo ? "演示评审员" : "",
      decision: matching && ["acknowledge", "request_changes", "reject"].includes(draft.decision) ? draft.decision : "acknowledge",
      comment: matching ? draft.comment || "" : "",
      automatic_demo: automatic && demo
    };
  }
  function appendReview(container, task, data) {
    const record = data.review_record;
    if (validReviewRecord(record)) {
      state.reviewDrafts.delete(task.id);
      const panel = el("section", "review-record");
      const decisions = { acknowledge: "确认已阅读", request_changes: "要求修改", reject: "拒绝" };
      panel.append(el("h3", "", "人工意见已记录"), el("p", "", `署名：${record.reviewer || "未提供"} · ${decisions[record.decision] || "已记录"}`));
      if (record.comment) panel.append(el("p", "", record.comment));
      panel.append(el("p", "", "记录仅用于本次评审，不构成生产发布授权。"));
      container.append(panel);
      return;
    }
    if (isTrashed(task)) { container.append(el("p", "metrics-note", "此报告已移到回收站；恢复后可继续原评审流程。")); return; }
    if (task.status !== "waiting_human") return;
    if (typeof data.review_digest !== "string" || !data.review_digest) { container.append(issuePanel("暂时无法提交人工意见", ["报告缺少当前材料标识，请刷新任务后再试。"])); return; }
    const form = el("form", "review-form");
    form.id = "human-review-form";
    const defaults = reviewDefaults(task, data, state.reviewDrafts.get(task.id));
    form.append(el("h3", "", "阅读完成后，记录复核意见"), el("p", "", defaults.demo ? "当前为离线演示，预填的是测试署名“演示评审员”，可以修改。请阅读报告后再手动提交；确认阅读不等于批准上线。" : "请核对上方报告、质量提示和阻断项，并填写本次实际复核署名。确认阅读不等于批准上线。"));
    const grid = el("div", "field-grid");
    const nameField = el("div", "field");
    const nameLabel = el("label", "", "复核人姓名"); nameLabel.htmlFor = "reviewer-name";
    const name = el("input"); name.id = "reviewer-name"; name.name = "reviewer"; name.required = true; name.maxLength = 80; name.autocomplete = "name"; name.placeholder = "填写本次复核署名";
    nameField.append(nameLabel, name);
    const decisionField = el("div", "field");
    const decisionLabel = el("label", "", "复核意见"); decisionLabel.htmlFor = "reviewer-decision";
    const decision = el("select"); decision.id = "reviewer-decision"; decision.name = "decision";
    for (const [value, label] of [["acknowledge", "确认已阅读（不构成上线批准）"], ["request_changes", "要求修改或补充"], ["reject", "拒绝本次提交"]]) decision.append(new Option(label, value));
    decisionField.append(decisionLabel, decision); grid.append(nameField, decisionField);
    const commentField = el("div", "field");
    const commentLabel = el("label", "", "备注"); commentLabel.htmlFor = "reviewer-comment";
    const comment = el("textarea"); comment.id = "reviewer-comment"; comment.name = "comment"; comment.maxLength = 2000; comment.rows = 3; comment.placeholder = "记录需要补充的材料、修改要求或阅读说明。";
    name.value = defaults.reviewer; decision.value = defaults.decision; comment.value = defaults.comment;
    let automaticDemoName = defaults.automatic_demo;
    name.addEventListener("input", () => { automaticDemoName = false; });
    const rememberDraft = () => state.reviewDrafts.set(task.id, { digest: data.review_digest, reviewer: name.value, decision: decision.value, comment: comment.value, automatic_demo: automaticDemoName });
    [name, decision, comment].forEach(field => field.addEventListener("input", rememberDraft));
    decision.addEventListener("change", rememberDraft);
    commentField.append(commentLabel, comment);
    const error = el("div", "form-error"); error.id = "review-error"; error.hidden = true; error.setAttribute("role", "alert");
    const footer = el("div", "form-footer");
    const submit = el("button", "button button-primary", "提交复核意见"); submit.type = "submit";
    footer.append(el("span", "muted small", "意见绑定当前这份评审材料。"), submit);
    form.append(grid, commentField, error, footer);
    form.addEventListener("submit", event => submitReview(event, task.id, data.review_digest));
    container.append(form);
  }
  async function submitReview(event, id, digest) {
    event.preventDefault();
    if (state.reviewing) return;
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    const reviewer = $("#reviewer-name", form).value.trim();
    const comment = $("#reviewer-comment", form).value;
    const decision = $("#reviewer-decision", form).value;
    const error = $("#review-error", form);
    if (!reviewer) { error.textContent = "请填写复核人姓名。"; error.hidden = false; return; }
    state.reviewing = true;
    const button = $("button[type=submit]", form);
    button.disabled = true; button.textContent = "正在记录…"; error.hidden = true;
    try {
      await api(`/api/tasks/${encodeURIComponent(id)}/review`, { method: "POST", body: JSON.stringify({ reviewer, decision, comment, review_digest: digest }) });
      toast("复核意见已提交，正在后台记录。请留意任务状态。");
      await loadTasks();
      state.reviewing = false;
      await loadDetail(true);
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { state.reviewing = false; if (button.isConnected) { button.disabled = false; button.textContent = "提交复核意见"; } }
  }

  function renderDetail() {
    const task = state.detail;
    if (!task) return;
    const previousConsole = $("#active-log-console");
    const atBottom = !previousConsole || previousConsole.scrollHeight - previousConsole.scrollTop - previousConsole.clientHeight < 35;
    const oldLogScroll = previousConsole?.scrollTop || 0;
    const panel = el("section", "card");
    const header = el("header", "detail-header");
    const top = el("div", "detail-title-top");
    top.append(el("span", "detail-eyebrow", AGENT_NAMES[task.agent] || "后台任务"), badge(task.status));
    const meta = el("div", "detail-meta");
    meta.append(el("span", "", date(task.created_at)), el("code", "", task.id));
    if (task.review_id) meta.append(el("span", "", `评审 ${task.review_id}`));
    const provenance = el("div", "source-split detail-source");
    const actualModel = taskAssessment(task).model;
    provenance.append(el("span", "source-pill", `材料：${sourceLabel(task)}`), el("span", "model-pill", actualModel === "not_called" ? `模型：未调用（所选${modelLabel(task.mode)}）` : `模型：${modelLabel(actualModel)}`));
    if (task.profile_label) provenance.append(el("span", "profile-pill", `场景：${task.profile_label}`));
    header.append(top, el("h2", "", task.title || AGENT_NAMES[task.agent] || "任务详情"), provenance, meta, el("p", "detail-status-note", statusNote(task)), assessmentPanel(task));
    const quality = warningsOf(dataOf(task));
    if (task.status === "waiting_human" && quality.length) header.append(issuePanel("报告待阅读，质量问题尚未解决", quality, true));
    if (ACTIVE.has(task.status)) {
      const progress = el("div", "task-progress");
      progress.setAttribute("role", "status");
      const caption = el("div", "progress-caption");
      caption.append(el("span", "", task.status === "queued" ? "等待本机助手" : "后台处理中"), el("span", "", "状态自动更新"));
      progress.append(el("div", "progress-track"), caption); header.append(progress);
    }
    const actions = el("div", "detail-actions");
    if (task.report) {
      const md = downloadLink(task.report.download_md, "↓ 下载 Markdown", task, "md");
      const json = downloadLink(task.report.download_json, "↓ 下载 JSON", task, "json");
      if (md) actions.append(md);
      if (json) actions.append(json);
    }
    const restore = isTrashed(task);
    const trash = el("button", `button ${restore ? "button-secondary" : "button-danger"}`, restore ? "恢复报告" : "移到回收站");
    trash.type = "button"; trash.disabled = ACTIVE.has(task.status) || state.reviewing || !supportsFeature("trash");
    if (ACTIVE.has(task.status)) trash.title = "运行中的任务暂不能移入回收站，请等待结束。";
    else if (!supportsFeature("trash")) trash.title = compatibilityMessage() || OLD_BACKEND_MESSAGE;
    trash.addEventListener("click", () => changeTaskTrash(task, restore, trash)); actions.append(trash); header.append(actions);
    if (restore) {
      const purge = el("button", "button button-danger permanent-action", "永久删除"); purge.type = "button";
      purge.disabled = ACTIVE.has(task.status) || state.reviewing || state.purging || !supportsFeature("permanent_delete");
      if (!supportsFeature("permanent_delete")) purge.title = "当前后台尚不支持永久删除，请更新并重新启动。";
      purge.addEventListener("click", () => openPurgeDialog("task", task)); actions.append(purge);
    }
    if (restore) header.append(el("p", "detail-status-note", "此记录位于回收站，报告文件和原状态仍保留。恢复后可继续查看和复核。"));
    panel.append(header);
    const tabs = el("div", "detail-tabs"); tabs.setAttribute("role", "tablist"); tabs.setAttribute("aria-label", "任务详情视图");
    for (const [id, label] of [["report", "可读报告"], ["evidence", "证据与用量"], ["logs", "执行记录"]]) {
      const button = el("button", id === state.tab ? "active" : "", label);
      button.type = "button"; button.setAttribute("role", "tab"); button.setAttribute("aria-selected", String(id === state.tab)); button.id = `detail-tab-${id}`; button.setAttribute("aria-controls", "detail-tab-panel");
      button.addEventListener("click", () => { state.tab = id; renderDetail(); });
      button.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const order = ["report", "evidence", "logs"];
        const position = order.indexOf(id);
        state.tab = event.key === "Home" ? order[0] : event.key === "End" ? order[2] : order[(position + (event.key === "ArrowRight" ? 1 : 2)) % 3];
        renderDetail();
        $(`#detail-tab-${state.tab}`).focus();
      });
      tabs.append(button);
    }
    const body = el("div", "detail-body"); body.id = "detail-tab-panel"; body.setAttribute("role", "tabpanel"); body.setAttribute("aria-labelledby", `detail-tab-${state.tab}`);
    if (state.detailError) body.append(issuePanel("状态刷新提示", [state.detailError]));
    body.append(state.tab === "report" ? reportContent(task) : state.tab === "evidence" ? evidenceContent(task) : logsContent(task));
    panel.append(tabs, body);
    $("#task-detail").replaceChildren(panel);
    const console = $("#active-log-console");
    if (console) console.scrollTop = atBottom ? console.scrollHeight : oldLogScroll;
  }
  async function loadDetail(force = false) {
    const id = state.selected;
    if (!id || state.detailBusy || (state.reviewing && !force)) return;
    state.detailBusy = true;
    try {
      const payload = await api(`/api/tasks/${encodeURIComponent(id)}`);
      if (!payload.task || payload.task.id !== id) throw new Error("这项任务的详情暂时无法读取。");
      if (state.selected !== id) return;
      const wasActive = state.detail && ACTIVE.has(state.detail.status);
      state.detail = payload.task;
      state.detailError = "";
      setConnection(true);
      renderDetail();
      if (wasActive && !ACTIVE.has(payload.task.status)) { loadTasks(); toast(payload.task.status === "waiting_human" ? "评审材料已生成，等待人工复核" : "任务状态已更新"); }
    } catch (error) {
      if (state.selected !== id) return;
      state.detailError = error.message;
      setConnection(false, error.message);
      if (state.detail) renderDetail();
      else { const panel = el("div", "card empty-detail"); panel.append(el("h2", "", "暂时无法读取任务"), el("p", "", error.message)); const retry = el("button", "button button-secondary", "重试"); retry.type = "button"; retry.addEventListener("click", () => loadDetail()); panel.append(retry); $("#task-detail").replaceChildren(panel); }
    } finally { state.detailBusy = false; if (state.selected !== id) loadDetail(); }
  }

  ["ops", "release"].forEach(agent => {
    createMaterialEditor(agent);
    $(`#${agent}-edit-materials`).addEventListener("click", () => loadMaterialTemplate(agent));
    ["service", "environment", ...(agent === "release" ? ["change"] : [])].forEach(key => $(`#${agent}-${key}`).addEventListener("input", () => enterCustom(agent)));
  });
  $("#tasks-active").addEventListener("click", () => setBin("active"));
  $("#tasks-trash").addEventListener("click", () => setBin("trash"));
  $("#task-search").addEventListener("input", event => { state.taskSearch = event.target.value; renderTasks(); });
  $("#task-status-filter").addEventListener("change", event => { state.taskStatus = event.target.value; renderTasks(); });
  $("#case-search").addEventListener("input", event => { state.caseSearch = event.target.value; renderCases(); });
  $("#case-verified").addEventListener("change", event => { state.caseVerified = event.target.value; renderCases(); });
  $("#refresh-cases").addEventListener("click", loadCases);
  $("#refresh-storage").addEventListener("click", loadStorage);
  $("#purge-confirmation").addEventListener("input", event => { $("#purge-submit").disabled = state.purging || !canConfirmPurge(event.target.value, state.purgeTarget); });
  $("#purge-cancel").addEventListener("click", closePurgeDialog);
  $("#purge-form").addEventListener("submit", submitPurge);
  $("#purge-dialog").addEventListener("cancel", event => { if (state.purging) event.preventDefault(); else state.purgeTarget = null; });
  $("#ops-form").addEventListener("submit", event => submitTask("ops", event));
  $("#release-form").addEventListener("submit", event => submitTask("release", event));
  $("#ops-profile").addEventListener("change", () => applyProfile("ops"));
  $("#release-profile").addEventListener("change", () => applyProfile("release"));
  ["#ops-start", "#ops-end"].forEach(selector => $(selector).addEventListener("input", () => { renderFormCoverage("ops"); updateAgentState("ops"); }));
  $("#release-change").addEventListener("change", updateChange);
  $$("input[name=ops-mode]").forEach(radio => radio.addEventListener("change", () => updateAgentState("ops")));
  $$("input[name=release-mode]").forEach(radio => radio.addEventListener("change", () => updateAgentState("release")));
  $$(".refresh-tasks").forEach(button => button.addEventListener("click", () => loadTasks(true)));
  $$("[data-filter]").forEach(button => button.addEventListener("click", () => { state.filter = button.dataset.filter; $$("[data-filter]").forEach(item => { item.classList.toggle("active", item === button); item.setAttribute("aria-pressed", String(item === button)); }); renderTasks(); }));
  $("#retry-connect").addEventListener("click", bootstrap);
  window.addEventListener("hashchange", () => setRoute(window.location.hash.slice(1)));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { loadTasks(); if (state.view === "tasks" && state.selected) loadDetail(); } });
  text("#today-label", new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "short" }).format(new Date()));
  setRoute(window.location.hash.slice(1) || "overview");
  bootstrap();
  setInterval(() => { if (!document.hidden && state.bootstrap) loadTasks(); }, 5000);
  setInterval(() => { if (!document.hidden && state.bootstrap) { if (state.view === "overview") loadStorage(); if (state.view === "cases") loadCases(); } }, 30000);
  setInterval(() => { if (!document.hidden && state.view === "tasks" && state.selected && (!state.detail || ACTIVE.has(state.detail.status))) loadDetail(); }, 1500);
})();
