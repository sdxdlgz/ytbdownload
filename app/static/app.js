// deno-lint-ignore-file no-window no-window-prefix -- this script runs only in a browser window.
"use strict";

const API_ROOT = "/api/v1";
const ACTIVE_STATES = new Set(["queued", "running", "postprocessing", "cancelling"]);
const TERMINAL_STATES = new Set(["completed", "failed", "cancelled", "expired"]);

const state = {
  config: null,
  session: null,
  analysis: null,
  selectedChoice: null,
  activeTab: "recommended",
  jobs: [],
  activeJob: null,
  analysisGeneration: 0,
  jobGeneration: 0,
  loadingTimer: null,
  previousFocus: null,
  systemHealth: null,
};

const elements = {};

class ApiError extends Error {
  constructor(error, status) {
    super(error?.message || "请求失败，请稍后重试。");
    this.name = "ApiError";
    this.code = error?.code || "REQUEST_FAILED";
    this.status = status;
    this.details = error?.details || {};
    this.requestId = error?.request_id || "";
  }
}

document.addEventListener("DOMContentLoaded", init);

async function init() {
  collectElements();
  bindEvents();
  setOnlineStatus(navigator.onLine);

  try {
    const [config, session] = await Promise.all([
      api("/config", { authPrompt: false }),
      api("/session", { authPrompt: false }),
    ]);
    state.config = config;
    state.session = session;
    applyConfig(config);
    applySession(session);
    void checkHealth();
    if (session.auth_required && !session.authenticated) {
      openAuthDialog(true);
      return;
    }
    await bootAuthenticatedView();
  } catch (error) {
    setSystemState("offline", "OFFLINE");
    showToast("初始化失败", readableError(error), "error");
  }
}

function collectElements() {
  const ids = [
    "system-status", "system-status-label", "history-button", "history-count",
    "auth-control", "auth-control-label", "url-form", "url-input", "paste-button",
    "playlist-toggle", "playlist-limit-note", "url-error", "analyze-button", "download-console",
    "extractor-count", "workspace", "workspace-title", "reset-button", "analysis-loading",
    "loading-code", "loading-message", "analysis-error", "analysis-error-code",
    "analysis-error-message", "retry-button", "media-result", "cover-frame", "media-cover",
    "cover-fallback", "media-platform", "media-kind", "source-chip", "restriction-chip",
    "media-title", "media-uploader", "media-duration", "media-quality", "media-format-count",
    "media-subtitle-count", "media-description", "playlist-entries-toggle", "playlist-entries",
    "playlist-entry-count", "playlist-entry-list", "choice-list", "choice-empty",
    "advanced-options", "embed-metadata", "subtitle-options", "subtitle-list",
    "selection-label", "selection-description", "download-button", "active-transfer",
    "transfer-title", "transfer-status", "transfer-index", "transfer-platform",
    "transfer-media-title", "transfer-choice", "progress-number", "transfer-progress",
    "transfer-phase", "transfer-bytes", "transfer-speed", "transfer-eta", "transfer-error",
    "artifact-list", "cancel-job-button", "dismiss-job-button", "footer-version",
    "drawer-backdrop", "history-drawer", "history-close", "refresh-history", "history-list",
    "history-empty", "auth-dialog", "auth-form", "token-input", "toggle-token", "auth-error",
    "auth-submit", "toast-region", "choice-template", "artifact-template", "history-template",
    "drop-hint",
  ];
  for (const id of ids) {
    elements[toCamel(id)] = document.getElementById(id);
  }
  elements.tabs = [...document.querySelectorAll("[data-choice-tab]")];
}

function bindEvents() {
  elements.urlForm.addEventListener("submit", handleAnalysisSubmit);
  elements.urlInput.addEventListener("input", () => {
    elements.urlError.textContent = "";
  });
  elements.pasteButton.addEventListener("click", pasteFromClipboard);
  elements.retryButton.addEventListener("click", () => void analyzeCurrentUrl());
  elements.resetButton.addEventListener("click", resetAnalysis);
  elements.downloadButton.addEventListener("click", startDownload);
  elements.cancelJobButton.addEventListener("click", cancelActiveJob);
  elements.dismissJobButton.addEventListener("click", dismissActiveJob);
  elements.playlistEntriesToggle.addEventListener("click", togglePlaylistEntries);
  elements.historyButton.addEventListener("click", openHistory);
  elements.historyClose.addEventListener("click", closeHistory);
  elements.drawerBackdrop.addEventListener("click", closeHistory);
  elements.refreshHistory.addEventListener("click", () => void loadJobs(true));
  elements.authForm.addEventListener("submit", login);
  elements.toggleToken.addEventListener("click", toggleTokenVisibility);
  elements.authControl.addEventListener("click", handleAuthControl);
  elements.systemStatus.addEventListener("click", describeSystemStatus);
  elements.tabs.forEach((tab) => {
    tab.addEventListener("click", () => selectTab(tab.dataset.choiceTab));
    tab.addEventListener("keydown", handleTabKeydown);
  });

  window.addEventListener("online", () => {
    setOnlineStatus(true);
    void checkHealth();
  });
  window.addEventListener("offline", () => setOnlineStatus(false));
  window.addEventListener("keydown", handleGlobalKeydown);

  let dragDepth = 0;
  window.addEventListener("dragenter", (event) => {
    if (!hasTextTransfer(event.dataTransfer)) return;
    event.preventDefault();
    dragDepth += 1;
    elements.downloadConsole.classList.add("is-dragging");
  });
  window.addEventListener("dragover", (event) => {
    if (!hasTextTransfer(event.dataTransfer)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  });
  window.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) elements.downloadConsole.classList.remove("is-dragging");
  });
  window.addEventListener("drop", (event) => {
    if (!hasTextTransfer(event.dataTransfer)) return;
    event.preventDefault();
    dragDepth = 0;
    elements.downloadConsole.classList.remove("is-dragging");
    const raw = event.dataTransfer.getData("text/uri-list") || event.dataTransfer.getData("text/plain");
    const url = extractFirstUrl(raw);
    if (!url) {
      showToast("没有找到链接", "请拖入包含 http:// 或 https:// 的文本。", "error");
      return;
    }
    elements.urlInput.value = url;
    elements.urlInput.focus();
    showToast("链接已放入", "确认播放列表选项后即可解析。", "success");
  });

  elements.mediaCover.addEventListener("load", () => {
    elements.mediaCover.hidden = false;
    elements.coverFallback.hidden = true;
  });
  elements.mediaCover.addEventListener("error", () => {
    elements.mediaCover.hidden = true;
    elements.coverFallback.hidden = false;
  });

  elements.authDialog.addEventListener("cancel", (event) => {
    if (state.session?.auth_required && !state.session?.authenticated) {
      event.preventDefault();
    }
  });
}

async function bootAuthenticatedView() {
  try {
    await loadJobs(false);
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) {
      showToast("记录读取失败", readableError(error), "error");
    }
  }
}

function applyConfig(config) {
  const count = config?.capabilities?.extractor_count;
  elements.extractorCount.textContent = count
    ? `${new Intl.NumberFormat("zh-CN").format(count)} EXTRACTORS`
    : "1,000+ EXTRACTORS";
  elements.playlistLimitNote.textContent = `最多 ${config.limits.max_playlist_items} 项`;
  elements.footerVersion.textContent = `WEB / ${config.version}`;

  const platformNames = (config.featured_platforms || []).filter((name) => !name.startsWith("以及"));
  if (platformNames.length) {
    const track = document.getElementById("platform-ticker");
    track.replaceChildren();
    for (let round = 0; round < 2; round += 1) {
      for (const name of platformNames) {
        const label = document.createElement("span");
        label.textContent = name.toUpperCase();
        if (round) label.setAttribute("aria-hidden", "true");
        const star = document.createElement("i");
        star.textContent = "✳";
        if (round) star.setAttribute("aria-hidden", "true");
        track.append(label, star);
      }
    }
  }
}

function applySession(session) {
  state.session = session;
  elements.authControl.hidden = !session.auth_required;
  if (session.auth_required) {
    elements.authControlLabel.textContent = session.authenticated ? "退出" : "访问令牌";
    elements.authControl.setAttribute(
      "aria-label",
      session.authenticated ? "退出当前访问会话" : "输入访问令牌",
    );
  }
}

async function checkHealth() {
  if (!navigator.onLine) return;
  try {
    const health = await api("/health/ready", { authPrompt: false, allowErrorBody: true });
    state.systemHealth = health;
    if (health.status === "ok") {
      setSystemState("ready", "READY");
    } else {
      setSystemState("degraded", "LIMITED");
    }
  } catch {
    state.systemHealth = null;
    setSystemState("offline", "OFFLINE");
  }
}

function setOnlineStatus(online) {
  if (!online) {
    setSystemState("offline", "OFFLINE");
  }
}

function setSystemState(kind, label) {
  elements.systemStatus.classList.toggle("is-degraded", kind === "degraded");
  elements.systemStatus.classList.toggle("is-offline", kind === "offline");
  elements.systemStatusLabel.textContent = label;
}

function describeSystemStatus() {
  const health = state.systemHealth;
  if (!health) {
    showToast("服务不可达", "浏览器暂时无法连接后端。", "error");
    return;
  }
  const js = health.checks?.javascript_runtime;
  const ffmpeg = health.checks?.ffmpeg;
  if (health.status === "ok") {
    showToast(
      "下载引擎就绪",
      `yt-dlp ${health.checks?.yt_dlp?.version || "ready"} · ffmpeg 可用 · ${js?.name || "JS"} 可用`,
      "success",
    );
  } else if (!ffmpeg?.ok) {
    showToast("ffmpeg 不可用", "视频合并、音频和封面转换会失败，请检查部署。", "error");
  } else if (!js?.ok) {
    showToast("JavaScript 运行时缺失", `建议安装 ${js?.name || "Deno"}，否则 YouTube 格式可能不完整。`, "error");
  } else {
    showToast("服务降级", "部分运行能力未就绪，请查看 VPS 健康检查。", "error");
  }
}

async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    const url = extractFirstUrl(text);
    if (!url) throw new Error("剪贴板中没有 HTTP(S) 链接。");
    elements.urlInput.value = url;
    elements.urlError.textContent = "";
    elements.urlInput.focus();
  } catch (error) {
    elements.urlInput.focus();
    showToast("无法读取剪贴板", error.message || "请按 Ctrl/Cmd + V 手动粘贴。", "error");
  }
}

function handleAnalysisSubmit(event) {
  event.preventDefault();
  void analyzeCurrentUrl();
}

async function analyzeCurrentUrl() {
  const rawUrl = elements.urlInput.value.trim();
  const validation = validateUrl(rawUrl);
  if (validation) {
    elements.urlError.textContent = validation;
    elements.urlInput.focus();
    return;
  }

  state.analysisGeneration += 1;
  const generation = state.analysisGeneration;
  state.analysis = null;
  state.selectedChoice = null;
  elements.urlError.textContent = "";
  showAnalysisState("loading");
  elements.workspace.hidden = false;
  elements.analyzeButton.disabled = true;
  elements.analyzeButton.classList.add("is-loading");
  startLoadingMessages();
  requestAnimationFrame(() => {
    elements.workspace.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  try {
    const record = await api("/analyses", {
      method: "POST",
      body: {
        url: rawUrl,
        playlist: elements.playlistToggle.checked,
      },
    });
    if (generation !== state.analysisGeneration) return;
    state.analysis = record;
    await pollAnalysis(record.id, generation);
  } catch (error) {
    if (generation !== state.analysisGeneration) return;
    renderAnalysisError(error);
  } finally {
    if (generation === state.analysisGeneration) {
      stopLoadingMessages();
      elements.analyzeButton.disabled = false;
      elements.analyzeButton.classList.remove("is-loading");
    }
  }
}

async function pollAnalysis(id, generation) {
  let attempts = 0;
  while (generation === state.analysisGeneration) {
    const record = attempts === 0 && state.analysis?.id === id
      ? state.analysis
      : await api(`/analyses/${id}`);
    if (generation !== state.analysisGeneration) return;
    state.analysis = record;
    if (record.status === "completed") {
      renderAnalysis(record.result);
      return;
    }
    if (record.status === "failed" || record.status === "expired") {
      throw new ApiError(record.error || {
        code: record.status === "expired" ? "ANALYSIS_EXPIRED" : "ANALYSIS_FAILED",
        message: record.status === "expired" ? "分析结果已过期，请重新分析。" : "媒体分析失败。",
      }, 422);
    }
    attempts += 1;
    elements.loadingCode.textContent = `EXTRACT / ${String(attempts).padStart(3, "0")}`;
    await delay(Math.min(2200, 700 + attempts * 130));
  }
}

function startLoadingMessages() {
  stopLoadingMessages();
  const messages = [
    "读取标题、封面和可用画质。不同站点可能需要几秒钟。",
    "正在检查视频流、音频流与容器兼容性。",
    "正在整理安全的下载预设，不会接受任意命令参数。",
    "来源站点响应较慢，任务仍在继续。",
  ];
  let index = 0;
  elements.loadingMessage.textContent = messages[index];
  state.loadingTimer = window.setInterval(() => {
    index = Math.min(index + 1, messages.length - 1);
    elements.loadingMessage.textContent = messages[index];
  }, 4200);
}

function stopLoadingMessages() {
  if (state.loadingTimer) window.clearInterval(state.loadingTimer);
  state.loadingTimer = null;
}

function showAnalysisState(mode) {
  elements.analysisLoading.hidden = mode !== "loading";
  elements.analysisError.hidden = mode !== "error";
  elements.mediaResult.hidden = mode !== "result";
}

function renderAnalysisError(error) {
  showAnalysisState("error");
  const normalized = normalizeError(error);
  elements.analysisErrorCode.textContent = normalized.code;
  elements.analysisErrorMessage.textContent = normalized.requestId
    ? `${normalized.message}（请求 ${normalized.requestId.slice(0, 8)}）`
    : normalized.message;
}

function renderAnalysis(result) {
  if (!result) {
    renderAnalysisError(new ApiError({ code: "EMPTY_RESULT", message: "分析结果为空。" }, 502));
    return;
  }
  showAnalysisState("result");
  elements.mediaTitle.textContent = result.title || "未命名媒体";
  elements.mediaUploader.textContent = result.uploader || result.webpage_domain || "未知发布者";
  elements.mediaPlatform.textContent = (result.platform || result.extractor || "SOURCE").toUpperCase();
  elements.mediaKind.textContent = result.kind === "playlist" ? "PLAYLIST" : "SINGLE";
  elements.sourceChip.textContent = `ROUTE / ${(result.platform || result.extractor || "UNKNOWN").toUpperCase()}`;
  elements.mediaDuration.textContent = result.kind === "playlist"
    ? `${result.entry_count || 0} 项`
    : formatDuration(result.duration);

  const heights = (result.formats || []).map((item) => item.height).filter(Number.isFinite);
  elements.mediaQuality.textContent = heights.length ? `${Math.max(...heights)}p` : (result.kind === "playlist" ? "AUTO" : "自适应");
  elements.mediaFormatCount.textContent = result.formats?.length
    ? `${result.formats.length} 个`
    : (result.kind === "playlist" ? "预设" : "直连");
  elements.mediaSubtitleCount.textContent = result.subtitles?.length ? `${result.subtitles.length} 种` : "—";
  elements.mediaDescription.textContent = result.description || "";
  elements.mediaDescription.hidden = !result.description;

  const restriction = result.restriction;
  elements.restrictionChip.hidden = !restriction;
  if (restriction) {
    elements.restrictionChip.textContent = restriction.message || "媒体受限";
    elements.restrictionChip.title = restriction.code || "RESTRICTED";
  }

  renderCover(result.thumbnail, result.title);
  renderPlaylistEntries(result);
  renderSubtitles(result.subtitles || []);

  const availableTabs = tabAvailability(result.choices || []);
  for (const tab of elements.tabs) {
    const tabName = tab.dataset.choiceTab;
    tab.disabled = !availableTabs[tabName];
    tab.hidden = !availableTabs[tabName];
  }
  const preferred = availableTabs.recommended
    ? "recommended"
    : availableTabs.audio
      ? "audio"
      : availableTabs.thumbnail
        ? "thumbnail"
        : "formats";
  selectTab(preferred, true);
}

function renderCover(thumbnail, title) {
  if (thumbnail?.url) {
    elements.mediaCover.alt = `${title || "媒体"}的封面`;
    elements.mediaCover.hidden = false;
    elements.coverFallback.hidden = true;
    elements.mediaCover.src = thumbnail.url;
  } else {
    elements.mediaCover.removeAttribute("src");
    elements.mediaCover.alt = "";
    elements.mediaCover.hidden = true;
    elements.coverFallback.hidden = false;
  }
}

function renderPlaylistEntries(result) {
  const entries = result.kind === "playlist" ? (result.entries || []) : [];
  elements.playlistEntriesToggle.hidden = !entries.length;
  elements.playlistEntries.hidden = true;
  elements.playlistEntriesToggle.setAttribute("aria-expanded", "false");
  elements.playlistEntryList.replaceChildren();
  elements.playlistEntryCount.textContent = `${entries.length} ITEMS`;
  for (const entry of entries) {
    const item = document.createElement("li");
    const title = document.createElement("strong");
    const duration = document.createElement("span");
    title.textContent = entry.title || `项目 ${entry.index || ""}`;
    duration.textContent = formatDuration(entry.duration);
    item.append(title, duration);
    elements.playlistEntryList.append(item);
  }
}

function togglePlaylistEntries() {
  const willOpen = elements.playlistEntries.hidden;
  elements.playlistEntries.hidden = !willOpen;
  elements.playlistEntriesToggle.setAttribute("aria-expanded", String(willOpen));
  elements.playlistEntriesToggle.querySelector("span").textContent = willOpen
    ? "收起播放列表项目"
    : "查看播放列表项目";
}

function renderSubtitles(subtitles) {
  elements.subtitleList.replaceChildren();
  elements.subtitleOptions.hidden = !subtitles.length;
  for (const subtitle of subtitles) {
    const label = document.createElement("label");
    label.className = "subtitle-chip";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = subtitle.code;
    input.dataset.kind = subtitle.kind || "manual";
    input.addEventListener("change", enforceSubtitleLimit);
    const copy = document.createElement("span");
    const name = document.createTextNode(subtitle.name || subtitle.code);
    const kind = document.createElement("i");
    kind.textContent = subtitle.kind === "automatic" ? "AUTO" : "MAN";
    copy.append(name, kind);
    label.append(input, copy);
    elements.subtitleList.append(label);
  }
}

function enforceSubtitleLimit(event) {
  const checked = [...elements.subtitleList.querySelectorAll("input:checked")];
  if (checked.length > 5) {
    event.target.checked = false;
    showToast("最多 5 种字幕", "减少字幕语言后再开始传输。", "error");
  }
}

function tabAvailability(choices) {
  return {
    recommended: choices.some((choice) => choice.kind === "video" && !choice.technical),
    formats: choices.some((choice) => choice.kind === "video" && choice.technical),
    audio: choices.some((choice) => choice.kind === "audio"),
    thumbnail: choices.some((choice) => choice.kind === "thumbnail"),
  };
}

function handleTabKeydown(event) {
  const navigationKeys = ["ArrowLeft", "ArrowRight", "Home", "End"];
  if (!navigationKeys.includes(event.key)) return;
  const available = elements.tabs.filter((tab) => !tab.hidden && !tab.disabled);
  if (!available.length) return;
  const current = Math.max(0, available.indexOf(event.currentTarget));
  let target = current;
  if (event.key === "ArrowRight") target = (current + 1) % available.length;
  if (event.key === "ArrowLeft") target = (current - 1 + available.length) % available.length;
  if (event.key === "Home") target = 0;
  if (event.key === "End") target = available.length - 1;
  event.preventDefault();
  available[target].focus();
  selectTab(available[target].dataset.choiceTab);
}


function selectTab(tabName, forceSelection = false) {
  if (!state.analysis?.result) return;
  state.activeTab = tabName;
  for (const tab of elements.tabs) {
    const active = tab.dataset.choiceTab === tabName;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  }
  const choices = filterChoices(state.analysis.result.choices || [], tabName);
  if (forceSelection || !choices.some((choice) => choice.id === state.selectedChoice?.id)) {
    state.selectedChoice = choices[0] || null;
  }
  renderChoices(choices);
}

function filterChoices(choices, tabName) {
  switch (tabName) {
    case "recommended":
      return choices.filter((choice) => choice.kind === "video" && !choice.technical);
    case "formats":
      return choices.filter((choice) => choice.kind === "video" && choice.technical);
    case "audio":
      return choices.filter((choice) => choice.kind === "audio");
    case "thumbnail":
      return choices.filter((choice) => choice.kind === "thumbnail");
    default:
      return [];
  }
}

function renderChoices(choices) {
  elements.choiceList.replaceChildren();
  elements.choiceEmpty.hidden = Boolean(choices.length);
  for (const choice of choices) {
    const fragment = elements.choiceTemplate.content.cloneNode(true);
    const card = fragment.querySelector(".choice-card");
    const input = fragment.querySelector("input");
    const title = fragment.querySelector(".choice-copy strong");
    const description = fragment.querySelector(".choice-copy small");
    const badge = fragment.querySelector(".choice-meta em");
    const size = fragment.querySelector(".choice-meta b");
    input.value = choice.id;
    input.checked = choice.id === state.selectedChoice?.id;
    title.textContent = choice.label || "输出选项";
    description.textContent = choice.description || "由服务器安全选择格式。";
    badge.textContent = choice.badge || choice.ext?.toUpperCase() || choice.kind.toUpperCase();
    size.textContent = choice.expected_size ? formatBytes(choice.expected_size) : choice.height ? `${choice.height}P` : "AUTO";
    card.dataset.choiceId = choice.id;
    input.addEventListener("change", () => {
      state.selectedChoice = choice;
      updateSelectionSummary();
    });
    elements.choiceList.append(fragment);
  }
  updateSelectionSummary();
}

function updateSelectionSummary() {
  const choice = state.selectedChoice;
  elements.downloadButton.disabled = !choice;
  elements.selectionLabel.textContent = choice?.label || "尚未选择";
  elements.selectionDescription.textContent = choice?.description || "请选择一个输出线路";
  const mediaChoice = choice && ["video", "audio"].includes(choice.kind);
  elements.advancedOptions.hidden = !choice || choice.kind === "thumbnail";
  elements.embedMetadata.closest("label").hidden = !mediaChoice;
  elements.subtitleOptions.hidden = !(
    choice?.kind === "video" && state.analysis?.result?.subtitles?.length
  );
}

async function startDownload() {
  if (!state.analysis?.id || !state.selectedChoice) return;
  const subtitles = [...elements.subtitleList.querySelectorAll("input:checked")];
  const subtitleLanguages = subtitles.map((input) => input.value);
  const includeAutomatic = subtitles.some((input) => input.dataset.kind === "automatic");
  const request = {
    analysis_id: state.analysis.id,
    choice_id: state.selectedChoice.id,
    subtitle_languages: state.selectedChoice.kind === "video" ? subtitleLanguages : [],
    include_auto_subtitles: state.selectedChoice.kind === "video" && includeAutomatic,
    embed_metadata: elements.embedMetadata.checked,
  };

  elements.downloadButton.disabled = true;
  elements.downloadButton.classList.add("is-loading");
  try {
    const job = await api("/jobs", {
      method: "POST",
      headers: { "Idempotency-Key": createIdempotencyKey() },
      body: request,
    });
    state.activeJob = job;
    upsertJob(job);
    renderActiveJob(job);
    renderHistory();
    showToast("任务已进入队列", `${job.choice?.label || "输出"}将由服务器处理。`, "success");
    requestAnimationFrame(() => {
      elements.activeTransfer.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    void pollJob(job.id);
  } catch (error) {
    showToast("无法创建任务", readableError(error), "error");
  } finally {
    elements.downloadButton.classList.remove("is-loading");
    elements.downloadButton.disabled = !state.selectedChoice;
  }
}

async function pollJob(jobId) {
  state.jobGeneration += 1;
  const generation = state.jobGeneration;
  while (generation === state.jobGeneration && state.activeJob?.id === jobId) {
    try {
      const job = await api(`/jobs/${jobId}`);
      if (generation !== state.jobGeneration) return;
      const previousStatus = state.activeJob?.status;
      state.activeJob = job;
      upsertJob(job);
      renderActiveJob(job);
      renderHistory();
      if (TERMINAL_STATES.has(job.status)) {
        if (job.status === "completed" && previousStatus !== "completed") {
          showToast("传输完成", `${job.artifacts.length} 个文件已经可以下载。`, "success");
        } else if (job.status === "failed" && previousStatus !== "failed") {
          showToast("任务失败", job.error?.message || "下载器未能生成文件。", "error");
        }
        return;
      }
      await delay(document.hidden ? 2500 : 900);
    } catch (error) {
      if (generation !== state.jobGeneration) return;
      if (error instanceof ApiError && error.status === 404) {
        state.activeJob = null;
        elements.activeTransfer.hidden = true;
        return;
      }
      await delay(2200);
    }
  }
}

function renderActiveJob(job) {
  if (!job) {
    elements.activeTransfer.hidden = true;
    return;
  }
  elements.workspace.hidden = false;
  elements.activeTransfer.hidden = false;
  elements.transferStatus.className = "transfer-status";
  if (["failed", "cancelled", "expired"].includes(job.status)) {
    elements.transferStatus.classList.add("is-error");
  } else if (job.status === "completed") {
    elements.transferStatus.classList.add("is-complete");
  }
  elements.transferStatus.textContent = job.status.toUpperCase();
  elements.transferIndex.textContent = `JOB / ${job.id.slice(0, 4).toUpperCase()}`;
  elements.transferPlatform.textContent = (job.platform || "SOURCE").toUpperCase();
  elements.transferMediaTitle.textContent = job.title || "未命名任务";
  elements.transferChoice.textContent = job.choice?.label || job.choice?.kind || "输出";
  const progress = Math.round(Number(job.progress) || 0);
  elements.progressNumber.textContent = `${progress}%`;
  elements.transferProgress.value = progress;
  elements.transferProgress.textContent = `${progress}%`;
  elements.transferPhase.textContent = phaseLabel(job.phase, job.status);
  elements.transferBytes.textContent = job.downloaded_bytes ? formatBytes(job.downloaded_bytes) : "—";
  elements.transferSpeed.textContent = job.speed ? `${formatBytes(job.speed)}/s` : "—";
  elements.transferEta.textContent = Number.isFinite(job.eta) ? formatEta(job.eta) : "—";

  elements.transferError.hidden = !job.error;
  elements.transferError.textContent = job.error?.message || "";
  renderArtifacts(job.artifacts || []);
  elements.cancelJobButton.hidden = !ACTIVE_STATES.has(job.status);
  elements.cancelJobButton.disabled = job.status === "cancelling";
  elements.dismissJobButton.hidden = ACTIVE_STATES.has(job.status);
  elements.transferTitle.textContent = job.status === "completed"
    ? "文件已经落地。"
    : job.status === "failed"
      ? "传输没有完成。"
      : job.status === "cancelled"
        ? "任务已取消。"
        : "正在搬运字节。";
}

function renderArtifacts(artifacts) {
  elements.artifactList.replaceChildren();
  elements.artifactList.hidden = !artifacts.length;
  for (const artifact of artifacts) {
    const fragment = elements.artifactTemplate.content.cloneNode(true);
    const link = fragment.querySelector(".artifact-item");
    const title = fragment.querySelector(".artifact-copy strong");
    const detail = fragment.querySelector(".artifact-copy small");
    link.href = artifact.download_url;
    link.setAttribute("download", "");
    title.textContent = artifact.filename;
    detail.textContent = `${formatBytes(artifact.size)} · ${artifact.media_type}${artifact.primary ? " · PRIMARY" : ""}`;
    elements.artifactList.append(fragment);
  }
}

async function cancelActiveJob() {
  const job = state.activeJob;
  if (!job || !ACTIVE_STATES.has(job.status)) return;
  elements.cancelJobButton.disabled = true;
  try {
    const updated = await api(`/jobs/${job.id}`, { method: "DELETE" });
    state.activeJob = updated;
    upsertJob(updated);
    renderActiveJob(updated);
    renderHistory();
    if (ACTIVE_STATES.has(updated.status)) void pollJob(updated.id);
  } catch (error) {
    elements.cancelJobButton.disabled = false;
    showToast("取消失败", readableError(error), "error");
  }
}

function dismissActiveJob() {
  state.jobGeneration += 1;
  state.activeJob = null;
  elements.activeTransfer.hidden = true;
}

async function loadJobs(notify = false) {
  const result = await api("/jobs?limit=50");
  state.jobs = result.items || [];
  elements.historyCount.textContent = String(result.total || 0);
  renderHistory();
  const active = state.jobs.find((job) => ACTIVE_STATES.has(job.status));
  if (active && !state.activeJob) {
    state.activeJob = active;
    renderActiveJob(active);
    void pollJob(active.id);
  }
  if (notify) showToast("记录已刷新", `找到 ${result.total || 0} 个任务。`, "success");
}

function upsertJob(job) {
  const index = state.jobs.findIndex((item) => item.id === job.id);
  if (index >= 0) state.jobs[index] = job;
  else state.jobs.unshift(job);
  state.jobs.sort((a, b) => new Date(b.created_at) - new Date(a.created_at));
  elements.historyCount.textContent = String(state.jobs.length);
}

function renderHistory() {
  elements.historyList.replaceChildren();
  elements.historyEmpty.hidden = state.jobs.length > 0;
  elements.historyList.hidden = state.jobs.length === 0;
  for (const job of state.jobs) {
    const fragment = elements.historyTemplate.content.cloneNode(true);
    const card = fragment.querySelector(".history-item");
    const status = fragment.querySelector(".history-state");
    const time = fragment.querySelector("time");
    const title = fragment.querySelector("h3");
    const choice = fragment.querySelector(".history-choice");
    const progress = fragment.querySelector("progress");
    const detail = fragment.querySelector(".history-detail");
    const actions = fragment.querySelector(".history-actions");
    status.textContent = job.status.toUpperCase();
    status.classList.toggle("is-error", ["failed", "cancelled", "expired"].includes(job.status));
    time.dateTime = job.created_at;
    time.textContent = formatDate(job.created_at);
    title.textContent = job.title || "未命名任务";
    choice.textContent = `${job.platform || "SOURCE"} / ${job.choice?.label || "OUTPUT"}`;
    progress.value = Math.round(job.progress || 0);
    detail.textContent = job.status === "completed"
      ? `${job.artifacts?.length || 0} FILES`
      : phaseLabel(job.phase, job.status).toUpperCase();

    if (ACTIVE_STATES.has(job.status)) {
      const view = actionButton("查看", () => {
        state.activeJob = job;
        renderActiveJob(job);
        closeHistory();
        void pollJob(job.id);
        elements.activeTransfer.scrollIntoView({ behavior: "smooth" });
      });
      actions.append(view);
    }
    const primary = job.artifacts?.find((artifact) => artifact.primary) || job.artifacts?.[0];
    if (job.status === "completed" && primary) {
      const download = document.createElement("a");
      download.href = primary.download_url;
      download.download = "";
      download.textContent = "下载";
      actions.append(download);
    }
    if (TERMINAL_STATES.has(job.status) && job.status !== "expired") {
      const purge = actionButton("清理", () => void purgeJob(job.id));
      purge.title = "立即删除服务器上的临时文件";
      actions.append(purge);
    }
    card.dataset.jobId = job.id;
    elements.historyList.append(fragment);
  }
}

function actionButton(label, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

async function purgeJob(jobId) {
  try {
    const updated = await api(`/jobs/${jobId}`, { method: "DELETE" });
    upsertJob(updated);
    if (state.activeJob?.id === jobId) {
      state.activeJob = updated;
      renderActiveJob(updated);
    }
    renderHistory();
    showToast("临时文件已清理", "任务记录已标记为过期。", "success");
  } catch (error) {
    showToast("清理失败", readableError(error), "error");
  }
}

function openHistory() {
  state.previousFocus = document.activeElement;
  elements.drawerBackdrop.hidden = false;
  elements.historyDrawer.hidden = false;
  document.body.classList.add("drawer-open");
  elements.historyClose.focus();
  void loadJobs(false).catch((error) => {
    showToast("记录读取失败", readableError(error), "error");
  });
}

function closeHistory() {
  if (elements.historyDrawer.hidden) return;
  elements.drawerBackdrop.hidden = true;
  elements.historyDrawer.hidden = true;
  document.body.classList.remove("drawer-open");
  if (state.previousFocus instanceof HTMLElement) state.previousFocus.focus();
}

function handleGlobalKeydown(event) {
  if (event.key === "Escape" && !elements.historyDrawer.hidden) {
    closeHistory();
    return;
  }
  if (event.key === "Tab" && !elements.historyDrawer.hidden) {
    trapDrawerFocus(event);
  }
}

function trapDrawerFocus(event) {
  const focusable = [...elements.historyDrawer.querySelectorAll(
    "button:not(:disabled), a[href], input:not(:disabled), [tabindex]:not([tabindex='-1'])",
  )].filter((item) => !item.hidden);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function openAuthDialog(required = false) {
  if (elements.authDialog.open) return;
  elements.authDialog.dataset.required = String(required);
  elements.authError.textContent = "";
  elements.tokenInput.value = "";
  elements.tokenInput.type = "password";
  elements.authDialog.showModal();
  requestAnimationFrame(() => elements.tokenInput.focus());
}

async function login(event) {
  event.preventDefault();
  const token = elements.tokenInput.value;
  if (!token) {
    elements.authError.textContent = "请输入访问令牌。";
    return;
  }
  elements.authSubmit.disabled = true;
  elements.authSubmit.classList.add("is-loading");
  try {
    const session = await api("/auth/session", {
      method: "POST",
      body: { token },
      authPrompt: false,
    });
    applySession(session);
    elements.authDialog.close();
    elements.tokenInput.value = "";
    showToast("工作台已解锁", "会话保存在 HttpOnly Cookie 中。", "success");
    await bootAuthenticatedView();
  } catch (error) {
    elements.authError.textContent = readableError(error);
    elements.tokenInput.select();
  } finally {
    elements.authSubmit.disabled = false;
    elements.authSubmit.classList.remove("is-loading");
  }
}

async function handleAuthControl() {
  if (!state.session?.auth_required) return;
  if (!state.session.authenticated) {
    openAuthDialog(true);
    return;
  }
  try {
    const session = await api("/auth/session", { method: "DELETE", authPrompt: false });
    applySession(session);
    state.jobs = [];
    state.activeJob = null;
    state.analysis = null;
    state.analysisGeneration += 1;
    state.jobGeneration += 1;
    elements.workspace.hidden = true;
    renderHistory();
    openAuthDialog(true);
  } catch (error) {
    showToast("退出失败", readableError(error), "error");
  }
}

function toggleTokenVisibility() {
  const visible = elements.tokenInput.type === "text";
  elements.tokenInput.type = visible ? "password" : "text";
  elements.toggleToken.setAttribute("aria-label", visible ? "显示访问令牌" : "隐藏访问令牌");
}

function resetAnalysis() {
  state.analysisGeneration += 1;
  stopLoadingMessages();
  elements.analyzeButton.disabled = false;
  elements.analyzeButton.classList.remove("is-loading");
  state.analysis = null;
  state.selectedChoice = null;
  showAnalysisState("none");
  if (!state.activeJob) elements.workspace.hidden = true;
  elements.urlInput.focus();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function api(path, options = {}) {
  const {
    method = "GET",
    body,
    headers = {},
    authPrompt = true,
    allowErrorBody = false,
  } = options;
  const requestHeaders = new Headers(headers);
  requestHeaders.set("Accept", "application/json");
  const init = {
    method,
    headers: requestHeaders,
    credentials: "same-origin",
  };
  if (body !== undefined) {
    requestHeaders.set("Content-Type", "application/json");
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(`${API_ROOT}${path}`, init);
  } catch (cause) {
    throw new ApiError({ code: "NETWORK_ERROR", message: "无法连接下载服务器。" }, 0, { cause });
  }

  const type = response.headers.get("content-type") || "";
  let payload = null;
  if (type.includes("application/json")) {
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    const error = new ApiError(payload?.error, response.status);
    if (response.status === 401 && authPrompt && state.config?.auth_required) {
      applySession({ auth_required: true, authenticated: false });
      openAuthDialog(true);
    }
    if (allowErrorBody && payload && response.status === 503) return payload;
    throw error;
  }
  return payload;
}

function validateUrl(raw) {
  if (!raw) return "请先粘贴媒体链接。";
  if (raw.length > 16384) return "链接过长。";
  try {
    const parsed = new URL(raw);
    if (!["http:", "https:"].includes(parsed.protocol)) return "只支持 HTTP(S) 链接。";
    if (parsed.username || parsed.password) return "链接不能包含用户名或密码。";
  } catch {
    return "链接格式不正确，请包含 https://。";
  }
  return "";
}

function extractFirstUrl(text) {
  if (!text) return "";
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter((line) => line && !line.startsWith("#"));
  for (const line of lines) {
    const match = line.match(/https?:\/\/[^\s<>"']+/i);
    if (match) return match[0];
  }
  return "";
}

function hasTextTransfer(transfer) {
  if (!transfer) return false;
  return [...transfer.types].some((type) => ["text/plain", "text/uri-list"].includes(type));
}

function phaseLabel(phase, status) {
  if (status === "completed") return "已就绪";
  if (status === "failed") return "失败";
  if (status === "cancelled") return "已取消";
  if (status === "expired") return "已过期";
  const normalized = (phase || status || "queued").split(":")[0];
  const labels = {
    queued: "排队中",
    extracting: "重新确认媒体",
    downloading: "接收数据",
    postprocessing: "ffmpeg 后处理",
    finalizing: "校验并登记文件",
    cancelling: "正在取消",
    ready: "已就绪",
  };
  return labels[normalized] || normalized;
}

function formatDuration(seconds) {
  if (!Number.isFinite(Number(seconds))) return "—";
  const value = Math.max(0, Math.round(Number(seconds)));
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const secs = value % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${minutes}:${String(secs).padStart(2, "0")}`;
}

function formatEta(seconds) {
  const value = Math.max(0, Math.round(Number(seconds)));
  if (value < 60) return `${value} 秒`;
  if (value < 3600) return `${Math.floor(value / 60)}分 ${value % 60}秒`;
  return `${Math.floor(value / 3600)}时 ${Math.floor((value % 3600) / 60)}分`;
}

function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  const digits = index === 0 || size >= 100 ? 0 : 1;
  return `${size.toFixed(digits)} ${units[index]}`;
}

function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function createIdempotencyKey() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const random = crypto.getRandomValues(new Uint32Array(4));
  return `web-${[...random].map((part) => part.toString(16)).join("-")}`;
}

function normalizeError(error) {
  if (error instanceof ApiError) return error;
  return new ApiError({ code: "UNEXPECTED_ERROR", message: error?.message || "发生未知错误。" }, 0);
}

function readableError(error) {
  return normalizeError(error).message;
}

function showToast(title, message, type = "success") {
  const toast = document.createElement("div");
  toast.className = `toast${type === "error" ? " is-error" : ""}`;
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  const copy = document.createElement("div");
  const heading = document.createElement("strong");
  const body = document.createElement("span");
  heading.textContent = title;
  body.textContent = message;
  copy.append(heading, body);
  toast.append(copy);
  elements.toastRegion.append(toast);
  window.setTimeout(() => {
    toast.remove();
  }, type === "error" ? 6500 : 4200);
}


function delay(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function toCamel(value) {
  return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}
