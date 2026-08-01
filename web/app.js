"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const app = {
  episodes: [],
  counts: {},
  filter: "all",
  query: "",
  selectedId: null,
  detail: null,
  series: null,
  currentTime: 0,
  playing: false,
  animation: null,
  playbackMaster: null,
  selectionToken: 0,
  activeJob: null,
  preloadGeneration: 0,
  preloadControllers: new Set(),
};

const cameraVideos = [
  { name: "head", element: $("#videoHead") },
  { name: "left_wrist", element: $("#videoLeft") },
  { name: "right_wrist", element: $("#videoRight") },
];

const mediaCache = new Map();
const PRELOAD_AHEAD = 5;
const KEEP_BEHIND = 2;
const STORAGE_VERSION = 1;

function storageKey(kind, rawRoot = rootValue()) {
  return `dataprocess:${kind}:v${STORAGE_VERSION}:${rawRoot}`;
}

function mediaKey(episodeId, camera, rawRoot = rootValue(), cacheVersion = "0") {
  return `${rawRoot}|${episodeId}|${camera}|${cacheVersion}`;
}

function mediaUrl(episodeId, kind, camera, rawRoot, cacheVersion = "0") {
  return `/api/episodes/${episodeId}/${kind}/${camera}?raw_root=${encodeURIComponent(rawRoot)}&v=${encodeURIComponent(cacheVersion)}`;
}

function saveEpisodeSnapshot() {
  if (!app.episodes.length) return;
  try {
    localStorage.setItem(storageKey("episodes"), JSON.stringify({
      savedAt: Date.now(),
      episodes: app.episodes,
      counts: app.counts,
    }));
  } catch (error) {
    console.warn("Unable to persist episode index", error);
  }
}

function restoreEpisodeSnapshot() {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey("episodes")) || "null");
    if (!value || !Array.isArray(value.episodes) || !value.counts) return false;
    app.episodes = value.episodes;
    app.counts = value.counts;
    const lastEpisode = localStorage.getItem(storageKey("selected"));
    app.selectedId = app.episodes.some((episode) => episode.id === lastEpisode) ? lastEpisode : null;
    renderSummary();
    renderEpisodeList();
    setPreloadStatus("已恢复本地索引 · 后台校验中", "running");
    return true;
  } catch (error) {
    console.warn("Unable to restore episode index", error);
    return false;
  }
}

function setPreloadStatus(text, state = "idle") {
  $("#preloadText").textContent = text;
  $("#preloadStatus").dataset.state = state;
}

async function api(path, options = {}) {
  const request = { ...options, headers: { ...(options.headers || {}) } };
  if (request.body && typeof request.body !== "string") {
    request.headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(request.body);
  }
  const response = await fetch(path, request);
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    payload = { error: `HTTP ${response.status}` };
  }
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function rootValue() {
  return $("#rawRoot").value.trim();
}

function rootQuery() {
  return `raw_root=${encodeURIComponent(rootValue())}`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function toast(message, kind = "normal", duration = 3200) {
  const node = document.createElement("div");
  node.className = `toast ${kind === "error" ? "error" : ""}`;
  node.textContent = message;
  $("#toastStack").appendChild(node);
  window.setTimeout(() => node.remove(), duration);
}

function formatTime(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(value / 60);
  const remainder = value - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${remainder.toFixed(3).padStart(6, "0")}`;
}

function formatDuration(seconds) {
  if (!seconds) return "—";
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    $("#healthDot").className = "online";
    $("#healthText").textContent = `服务在线 · v${health.version}`;
    if (!$("#rawRoot").value.trim()) $("#rawRoot").value = health.default_raw_root;
  } catch (error) {
    $("#healthDot").className = "offline";
    $("#healthText").textContent = "服务不可用";
    toast(error.message, "error");
  }
}

async function scan(refresh = false, selectCurrent = true) {
  const button = $("#scanButton");
  button.classList.add("loading");
  button.disabled = true;
  try {
    const payload = refresh
      ? await api("/api/scan", { method: "POST", body: { raw_root: rootValue() } })
      : await api(`/api/episodes?${rootQuery()}`);
    app.episodes = payload.episodes;
    app.counts = payload.counts;
    saveEpisodeSnapshot();
    renderSummary();
    renderEpisodeList();
    if (app.selectedId) {
      const stillExists = app.episodes.some((episode) => episode.id === app.selectedId);
      if (!stillExists) clearSelection();
      else if (selectCurrent) await selectEpisode(app.selectedId);
    }
    if (refresh) toast(`扫描完成：${payload.counts.total} 个 episode`);
  } catch (error) {
    if (app.episodes.length) {
      renderSummary();
      renderEpisodeList();
      setPreloadStatus("后台校验失败 · 继续使用本地索引", "error");
      toast(`后台校验失败，继续使用本地缓存：${error.message}`, "error", 6000);
    } else {
      app.episodes = [];
      renderEpisodeList(error.message);
      toast(error.message, "error", 6000);
    }
  } finally {
    button.classList.remove("loading");
    button.disabled = false;
  }
}

function renderSummary() {
  $("#countTotal").textContent = app.counts.total ?? 0;
  $("#countUnprocessed").textContent = app.counts.unprocessed ?? 0;
  $("#countPending").textContent = app.counts.pending_export ?? 0;
  $("#countExported").textContent = app.counts.exported ?? 0;
  $("#countFailed").textContent = app.counts.failed ?? 0;
}

function episodeMatches(episode) {
  if (app.query && !episode.id.toLowerCase().includes(app.query.toLowerCase())) return false;
  if (["unprocessed", "processed", "exported", "failed"].includes(app.filter)) {
    return episode.workflow_status === app.filter;
  }
  return true;
}

function statusClass(episode) {
  return episode.workflow_status || "unprocessed";
}

function renderEpisodeList(error = "") {
  const list = $("#episodeList");
  if (error) {
    list.innerHTML = `<div class="list-empty">读取失败<br>${escapeHtml(error)}</div>`;
    return;
  }
  const filtered = app.episodes.filter(episodeMatches);
  if (!filtered.length) {
    list.innerHTML = '<div class="list-empty">当前筛选下没有 episode</div>';
    return;
  }
  list.innerHTML = filtered
    .map((episode) => {
      const status = statusClass(episode);
      const workflowLabel = {
        unprocessed: "未处理 · 默认成功",
        processed: "已处理 · 待导出",
        exported: "已导出",
        failed: episode.reason,
      }[status];
      const meta = status === "failed"
        ? workflowLabel
        : `${workflowLabel} · ${episode.state_frames} 帧`;
      return `<button class="episode-item ${status} ${episode.id === app.selectedId ? "active" : ""}" data-id="${episode.id}">
        <span class="status-dot"></span>
        <span class="episode-item-main"><span class="episode-item-title">${episode.id}</span><span class="episode-item-meta">${escapeHtml(meta)}</span></span>
        <span class="episode-item-duration">${formatDuration(episode.duration_s)}</span>
      </button>`;
    })
    .join("");
  $$(".episode-item").forEach((button) => {
    button.addEventListener("click", () => selectEpisode(button.dataset.id));
  });
}

function clearSelection() {
  pausePlayback();
  cancelPreload();
  app.selectedId = null;
  app.detail = null;
  app.series = null;
  $("#emptyWorkspace").classList.remove("hidden");
  $("#reviewWorkspace").classList.add("hidden");
  renderEpisodeList();
}

async function selectEpisode(episodeId) {
  pausePlayback();
  cancelPreload();
  app.selectedId = episodeId;
  try { localStorage.setItem(storageKey("selected"), episodeId); } catch (_error) { /* storage disabled */ }
  app.currentTime = 0;
  app.series = null;
  const token = ++app.selectionToken;
  renderEpisodeList();
  $("#emptyWorkspace").classList.add("hidden");
  $("#reviewWorkspace").classList.remove("hidden");
  $("#episodeTitle").textContent = episodeId;
  $("#episodeSummary").textContent = "读取元数据…";
  resetVideos("读取视频索引…");
  try {
    const detail = await api(`/api/episodes/${episodeId}?${rootQuery()}`);
    if (token !== app.selectionToken) return;
    app.detail = detail;
    renderDetail();
    if (detail.ready) {
      setupVideos();
      const series = await api(`/api/episodes/${episodeId}/series?${rootQuery()}&max_points=900`);
      if (token !== app.selectionToken) return;
      app.series = series;
      renderJointOptions();
      drawChart();
    } else {
      resetVideos(detail.reason);
      app.series = null;
      renderJointOptions();
      drawChart();
    }
  } catch (error) {
    if (token !== app.selectionToken) return;
    $("#episodeSummary").textContent = error.message;
    toast(error.message, "error", 6000);
  }
}

function renderDetail() {
  const detail = app.detail;
  if (!detail) return;
  const status = $("#episodeStatus");
  status.className = "status-badge";
  if (detail.workflow_status === "failed") {
    status.classList.add("failed");
    status.textContent = detail.ready ? "人工标记失败" : "采集失败";
  } else if (detail.workflow_status === "exported") {
    status.classList.add("exported");
    status.textContent = "已导出";
  } else if (detail.workflow_status === "processed") {
    status.textContent = "已处理 · 待导出";
  } else {
    status.classList.add("unprocessed");
    status.textContent = "未处理 · 默认成功";
  }
  const warningText = detail.warnings?.length ? ` · ${detail.warnings.join("；")}` : "";
  const workflowHint = {
    unprocessed: "保存审阅后进入待导出队列",
    processed: "将在下一次导出中包含",
    exported: `已写入 ${detail.last_export?.output_root || "GR00T 数据集"}`,
    failed: "不会导出",
  }[detail.workflow_status] || "";
  $("#episodeSummary").textContent = `${detail.reason} · ${workflowHint} · ${formatDuration(detail.duration_s)}${warningText}`;
  $("#metaStateFrames").textContent = detail.state_frames.toLocaleString();
  $("#metaActionFrames").textContent = detail.action_frames.toLocaleString();
  $("#metaDimensions").textContent = detail.dimensions || "—";
  $("#metaManifest").textContent = detail.manifest_id || "—";
  $("#reviewNote").value = detail.note || "";
  $("#toggleExclude").textContent = detail.excluded && detail.ready ? "恢复为正常" : "标记为失败";
  $("#toggleExclude").disabled = !detail.ready;
  $("#autoTrim").disabled = !detail.ready;
  $("#saveReview").disabled = !detail.ready;

  const duration = Math.max(0, detail.duration_s || 0);
  for (const id of ["#seekBar", "#trimStart", "#trimEnd"]) $(id).max = duration;
  $("#trimStartNumber").max = duration;
  $("#trimEndNumber").max = duration;
  $("#trimStart").value = detail.trim_start_s || 0;
  $("#trimEnd").value = detail.trim_end_s ?? duration;
  $("#trimStartNumber").value = Number(detail.trim_start_s || 0).toFixed(2);
  $("#trimEndNumber").value = Number(detail.trim_end_s ?? duration).toFixed(2);
  $("#seekBar").value = 0;
  $("#totalTime").textContent = formatTime(duration);
  updateTrimUI();
  updatePlaybackUI(0);
}

function resetVideos(message = "相机不可用") {
  cameraVideos.forEach(({ element }) => {
    if (element._loadController) {
      element._loadController.abort();
      element._loadController = null;
    }
    element.pause();
    element._motionFrame = null;
    element._motionStillCount = 0;
    element._motionLastSample = 0;
    element._visuallyStatic = false;
    element.removeAttribute("src");
    element.removeAttribute("poster");
    const cached = element.dataset.cacheKey ? mediaCache.get(element.dataset.cacheKey) : null;
    if (element.dataset.objectUrl && cached?.objectUrl !== element.dataset.objectUrl) {
      URL.revokeObjectURL(element.dataset.objectUrl);
    }
    delete element.dataset.objectUrl;
    delete element.dataset.cacheKey;
    element.load();
    const card = element.closest(".video-card");
    card.classList.remove("loaded");
    card.classList.toggle("unavailable", message !== "读取视频索引…");
    card.querySelector(".video-loading").lastChild.textContent = message;
    setVideoState(element, message === "读取视频索引…" ? "读取索引" : message, message === "读取视频索引…" ? "loading" : "paused");
  });
}

function setVideoState(element, text, state = "ready") {
  const node = element.closest(".video-card")?.querySelector(".video-state");
  if (!node) return;
  if (node.textContent !== text) node.textContent = text;
  node.dataset.state = state;
}

function setupVideos() {
  const token = app.selectionToken;
  const episodeId = app.selectedId;
  const loads = [];
  cameraVideos.forEach(({ name, element }) => {
    const card = element.closest(".video-card");
    const camera = app.detail.cameras[name];
    card.classList.remove("loaded", "unavailable");
    card.querySelector(".video-loading").lastChild.textContent = "首次打开正在生成预览";
    setVideoState(element, "生成预览", "loading");
    if (!camera) {
      card.classList.add("unavailable");
      card.querySelector(".video-loading").lastChild.textContent = "未选择此机位";
      setVideoState(element, "未启用", "paused");
      return;
    }
    element.dataset.offset = camera.offset_s || 0;
    element.poster = mediaUrl(app.selectedId, "poster", name, rootValue(), camera.cache_version);
    element.playbackRate = Number($("#playbackSpeed").value);
    element.onloadedmetadata = async () => {
      card.classList.add("loaded");
      seekVideo(element, app.currentTime);
      setVideoState(element, "就绪 · 本地缓存", "ready");
      if (app.playing) {
        element.playbackRate = Number($("#playbackSpeed").value);
        try {
          await element.play();
          if (!app.playbackMaster) app.playbackMaster = element;
        } catch (error) {
          setVideoState(element, `播放失败`, "error");
          toast(`${name} 无法加入同步播放：${error.message}`, "error", 5000);
        }
      }
    };
    element.onerror = () => {
      card.classList.add("unavailable");
      card.querySelector(".video-loading").lastChild.textContent = "预览生成失败";
      setVideoState(element, "视频错误", "error");
    };
    element.onplaying = () => setVideoState(element, `本地播放 ${formatTime(element.currentTime)}`, "ready");
    element.onwaiting = () => setVideoState(element, "本地解码中", "buffering");
    element.onstalled = () => setVideoState(element, "浏览器解码停滞", "buffering");
    element.onpause = () => {
      if (!app.playing) setVideoState(element, "已暂停 · 本地缓存", "paused");
    };
    element.onended = () => setVideoState(element, "播放结束", "paused");
    loads.push(loadVideoBlob(episodeId, name, element, token, camera.cache_version));
  });
  Promise.allSettled(loads).then(() => {
    if (token === app.selectionToken && episodeId === app.selectedId) {
      schedulePreload(episodeId);
    }
  });
}

function attachCachedVideo(element, entry) {
  element.dataset.objectUrl = entry.objectUrl;
  element.dataset.cacheKey = entry.key;
  element.src = entry.objectUrl;
  element.load();
  entry.lastUsed = Date.now();
}

function storeMediaBlob(key, episodeId, camera, rawRoot, blob) {
  const existing = mediaCache.get(key);
  if (existing) {
    existing.lastUsed = Date.now();
    return existing;
  }
  const entry = {
    key,
    episodeId,
    camera,
    rawRoot,
    blob,
    objectUrl: URL.createObjectURL(blob),
    size: blob.size,
    lastUsed: Date.now(),
  };
  mediaCache.set(key, entry);
  return entry;
}

async function loadVideoBlob(episodeId, name, element, token, cacheVersion = "0") {
  const card = element.closest(".video-card");
  const rawRoot = rootValue();
  const key = mediaKey(episodeId, name, rawRoot, cacheVersion);
  const cached = mediaCache.get(key);
  if (cached) {
    attachCachedVideo(element, cached);
    return;
  }
  const controller = new AbortController();
  element._loadController = controller;
  const url = mediaUrl(episodeId, "video", name, rawRoot, cacheVersion);
  try {
    // Fetching the compact preview as one Blob is more reliable than letting
    // different browsers issue dozens of competing byte-range requests.
    const response = await fetch(url, { signal: controller.signal, cache: "force-cache" });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try { message = (await response.json()).error || message; } catch (_error) { /* MP4 response */ }
      throw new Error(message);
    }
    const blob = await response.blob();
    if (controller.signal.aborted) return;
    const entry = storeMediaBlob(key, episodeId, name, rawRoot, blob);
    if (token !== app.selectionToken || episodeId !== app.selectedId) return;
    attachCachedVideo(element, entry);
  } catch (error) {
    if (error.name === "AbortError") return;
    card.classList.add("unavailable");
    card.querySelector(".video-loading").lastChild.textContent = `加载失败：${error.message}`;
    setVideoState(element, "加载失败", "error");
    toast(`${name} 视频加载失败：${error.message}`, "error", 6000);
  } finally {
    if (element._loadController === controller) element._loadController = null;
  }
}

function cancelPreload() {
  app.preloadGeneration += 1;
  app.preloadControllers.forEach((controller) => controller.abort());
  app.preloadControllers.clear();
  setPreloadStatus("预加载等待当前 episode 就绪", "idle");
}

async function prefetchVideo(episodeId, camera, rawRoot, generation, cacheVersion = "0") {
  const key = mediaKey(episodeId, camera, rawRoot, cacheVersion);
  if (mediaCache.has(key)) return true;
  const controller = new AbortController();
  app.preloadControllers.add(controller);
  try {
    const url = mediaUrl(episodeId, "video", camera, rawRoot, cacheVersion);
    const response = await fetch(url, { signal: controller.signal, cache: "force-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    if (generation !== app.preloadGeneration || controller.signal.aborted) return false;
    storeMediaBlob(key, episodeId, camera, rawRoot, blob);
    return true;
  } finally {
    app.preloadControllers.delete(controller);
  }
}

function cleanupMediaCache(keepEpisodeIds, rawRoot) {
  for (const [key, entry] of mediaCache.entries()) {
    if (entry.rawRoot !== rawRoot || !keepEpisodeIds.has(entry.episodeId)) {
      URL.revokeObjectURL(entry.objectUrl);
      mediaCache.delete(key);
    }
  }
}

function clearMediaCache() {
  for (const entry of mediaCache.values()) URL.revokeObjectURL(entry.objectUrl);
  mediaCache.clear();
}

async function schedulePreload(currentEpisodeId) {
  const currentIndex = app.episodes.findIndex((episode) => episode.id === currentEpisodeId);
  if (currentIndex < 0) return;
  const following = app.episodes
    .slice(currentIndex + 1)
    .filter((episode) => episode.ready)
    .slice(0, PRELOAD_AHEAD);
  const previous = app.episodes
    .slice(Math.max(0, currentIndex - KEEP_BEHIND), currentIndex)
    .filter((episode) => episode.ready);
  const keepIds = new Set([currentEpisodeId, ...previous.map((item) => item.id), ...following.map((item) => item.id)]);
  const rawRoot = rootValue();
  cleanupMediaCache(keepIds, rawRoot);
  if (!following.length) {
    setPreloadStatus("已到数据集末尾", "done");
    return;
  }

  const generation = ++app.preloadGeneration;
  let completed = 0;
  let failures = 0;
  for (const episode of following) {
    if (generation !== app.preloadGeneration) return;
    setPreloadStatus(`预加载 ${completed + 1}/${following.length} · ${episode.id}`, "running");
    const cameras = Object.keys(episode.cameras || {}).filter((camera) => ["head", "left_wrist", "right_wrist"].includes(camera));
    const results = await Promise.allSettled(
      cameras.map((camera) => prefetchVideo(
        episode.id,
        camera,
        rawRoot,
        generation,
        episode.cameras[camera]?.cache_version,
      )),
    );
    if (generation !== app.preloadGeneration) return;
    failures += results.filter((result) => result.status === "rejected").length;
    completed += 1;
  }
  const bytes = [...mediaCache.values()]
    .filter((entry) => keepIds.has(entry.episodeId) && entry.rawRoot === rawRoot)
    .reduce((sum, entry) => sum + entry.size, 0);
  const memoryMb = (bytes / 1024 / 1024).toFixed(0);
  if (failures) {
    setPreloadStatus(`已预加载 ${completed} 个，${failures} 路失败 · ${memoryMb} MB`, "error");
  } else {
    setPreloadStatus(`后续 ${completed} 个已预加载 · ${memoryMb} MB`, "done");
  }
}

function videoOffset(video) {
  return Number(video.dataset.offset || 0);
}

function seekVideo(video, globalTime) {
  if (!Number.isFinite(video.duration)) return;
  const local = Math.max(0, Math.min(video.duration - 0.001, globalTime - videoOffset(video)));
  if (Math.abs(video.currentTime - local) > 0.01) video.currentTime = local;
}

function seek(globalTime) {
  if (!app.detail) return;
  app.currentTime = Math.max(0, Math.min(app.detail.duration_s, Number(globalTime) || 0));
  cameraVideos.forEach(({ element }) => seekVideo(element, app.currentTime));
  updatePlaybackUI(app.currentTime);
}

function playableVideos() {
  return cameraVideos.map((item) => item.element).filter((video) => Number.isFinite(video.duration));
}

async function togglePlayback() {
  if (app.playing) {
    pausePlayback();
    return;
  }
  if (!app.detail?.ready) return;
  const videos = playableVideos();
  if (!videos.length) {
    toast("视频仍在生成，请稍候", "error");
    return;
  }
  const trimStart = Number($("#trimStart").value);
  const trimEnd = Number($("#trimEnd").value);
  if (app.currentTime < trimStart || app.currentTime >= trimEnd - 0.02) seek(trimStart);
  videos.forEach((video) => {
    video.playbackRate = Number($("#playbackSpeed").value);
    seekVideo(video, app.currentTime);
  });
  app.playing = true;
  app.playbackMaster = videos.find((video) => video.id === "videoHead") || videos[0];
  $("#playPause").classList.add("playing");
  const results = await Promise.allSettled(videos.map((video) => video.play()));
  results.forEach((result, index) => {
    if (result.status === "rejected") {
      setVideoState(videos[index], "播放被浏览器阻止", "error");
    }
  });
  if (!videos.some((video) => !video.paused)) {
    pausePlayback();
    toast("浏览器未允许视频播放，请再次点击播放", "error");
    return;
  }
  tickPlayback();
}

function pausePlayback() {
  app.playing = false;
  if (app.animation) cancelAnimationFrame(app.animation);
  app.animation = null;
  app.playbackMaster = null;
  cameraVideos.forEach(({ element }) => element.pause());
  $("#playPause").classList.remove("playing");
}

function tickPlayback() {
  if (!app.playing || !app.detail) return;
  const videos = playableVideos();
  let master = app.playbackMaster;
  if (!master || !videos.includes(master) || master.ended) {
    master = videos.find((video) => !video.paused) || videos[0];
    app.playbackMaster = master;
  }
  if (!master) return pausePlayback();
  app.currentTime = master.currentTime + videoOffset(master);
  const trimEnd = Number($("#trimEnd").value);
  if (app.currentTime >= trimEnd) {
    seek(trimEnd);
    pausePlayback();
    return;
  }
  videos.forEach((video) => {
    const desired = app.currentTime - videoOffset(video);
    if (video !== master && Number.isFinite(video.duration) && Math.abs(video.currentTime - desired) > 0.075) {
      video.currentTime = Math.max(0, Math.min(video.duration - 0.001, desired));
    }
    if (video.paused && !video.ended && video.readyState >= 2 && !video._resumePending) {
      video._resumePending = true;
      video.play().catch((error) => {
        setVideoState(video, "同步恢复失败", "error");
        console.warn("resume video failed", error);
      }).finally(() => { video._resumePending = false; });
    }
    sampleVisualMotion(video);
    if (video._visuallyStatic) {
      setVideoState(video, `源画面静止 ${formatTime(video.currentTime)}`, "frozen");
    } else if (!video.paused) {
      setVideoState(video, `本地播放 ${formatTime(video.currentTime)}`, "ready");
    }
  });
  updatePlaybackUI(app.currentTime);
  app.animation = requestAnimationFrame(tickPlayback);
}

function sampleVisualMotion(video) {
  const now = performance.now();
  if (video.readyState < 2 || now - (video._motionLastSample || 0) < 450) return;
  video._motionLastSample = now;
  if (!video._motionCanvas) {
    video._motionCanvas = document.createElement("canvas");
    video._motionCanvas.width = 32;
    video._motionCanvas.height = 24;
  }
  const context = video._motionCanvas.getContext("2d", { willReadFrequently: true });
  try {
    context.drawImage(video, 0, 0, 32, 24);
    const current = context.getImageData(0, 0, 32, 24).data;
    if (video._motionFrame) {
      let difference = 0;
      for (let index = 0; index < current.length; index += 4) {
        difference += Math.abs(current[index] - video._motionFrame[index]);
        difference += Math.abs(current[index + 1] - video._motionFrame[index + 1]);
        difference += Math.abs(current[index + 2] - video._motionFrame[index + 2]);
      }
      const meanDifference = difference / (32 * 24 * 3);
      video._motionStillCount = meanDifference < 0.8 ? (video._motionStillCount || 0) + 1 : 0;
      video._visuallyStatic = video._motionStillCount >= 3;
    }
    video._motionFrame = new Uint8ClampedArray(current);
  } catch (_error) {
    video._visuallyStatic = false;
  }
}

function updatePlaybackUI(time) {
  $("#currentTime").textContent = formatTime(time);
  $("#seekBar").value = time;
  drawChart();
}

function trimValues() {
  return {
    start: Number($("#trimStart").value),
    end: Number($("#trimEnd").value),
  };
}

function setTrim(which, value, seekToValue = true) {
  if (!app.detail?.ready) return;
  const duration = app.detail.duration_s;
  let { start, end } = trimValues();
  if (which === "start") start = Math.max(0, Math.min(Number(value), end - 0.25));
  else end = Math.min(duration, Math.max(Number(value), start + 0.25));
  $("#trimStart").value = start;
  $("#trimEnd").value = end;
  $("#trimStartNumber").value = start.toFixed(2);
  $("#trimEndNumber").value = end.toFixed(2);
  updateTrimUI();
  if (seekToValue) seek(which === "start" ? start : end);
}

function updateTrimUI() {
  if (!app.detail) return;
  const duration = Math.max(app.detail.duration_s, 0.001);
  const { start, end } = trimValues();
  const left = (start / duration) * 100;
  const right = 100 - (end / duration) * 100;
  $("#trimValid").style.left = `${left}%`;
  $("#trimValid").style.right = `${right}%`;
  $("#trimDuration").textContent = formatDuration(Math.max(0, end - start));
  $("#retainedPercent").textContent = `${Math.round(((end - start) / duration) * 100)}%`;
  drawChart();
}

async function saveReview() {
  if (!app.detail?.ready) return;
  const { start, end } = trimValues();
  try {
    const detail = await api(`/api/episodes/${app.selectedId}/review`, {
      method: "POST",
      body: {
        raw_root: rootValue(),
        excluded: app.detail.excluded,
        trim_start_s: start,
        trim_end_s: end,
        note: $("#reviewNote").value,
      },
    });
    app.detail = detail;
    mergeEpisode(detail);
    renderDetail();
    renderEpisodeList();
    renderSummaryFromEpisodes();
    toast("审阅结果已保存");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function toggleExclude() {
  if (!app.detail?.ready) return;
  const { start, end } = trimValues();
  try {
    const detail = await api(`/api/episodes/${app.selectedId}/review`, {
      method: "POST",
      body: {
        raw_root: rootValue(),
        excluded: !app.detail.excluded,
        trim_start_s: start,
        trim_end_s: end,
        note: $("#reviewNote").value,
        reason: !app.detail.excluded ? "人工标记为失败" : "人工复核通过",
      },
    });
    app.detail = detail;
    mergeEpisode(detail);
    renderDetail();
    renderEpisodeList();
    renderSummaryFromEpisodes();
    toast(detail.excluded ? "已从导出列表排除" : "已恢复到导出列表");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function autoTrim() {
  if (!app.detail?.ready) return;
  $("#autoTrim").disabled = true;
  try {
    const detail = await api(`/api/episodes/${app.selectedId}/auto-trim`, {
      method: "POST",
      body: { raw_root: rootValue(), padding_s: 0.6 },
    });
    app.detail = detail;
    mergeEpisode(detail);
    renderDetail();
    renderEpisodeList();
    seek(detail.trim_start_s);
    toast("已按关节运动区间自动裁剪，请回放确认");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    $("#autoTrim").disabled = !app.detail?.ready;
  }
}

function mergeEpisode(detail) {
  const index = app.episodes.findIndex((episode) => episode.id === detail.id);
  if (index >= 0) app.episodes[index] = { ...app.episodes[index], ...detail };
  saveEpisodeSnapshot();
}

function renderSummaryFromEpisodes() {
  app.counts.total = app.episodes.length;
  app.counts.unprocessed = app.episodes.filter((episode) => episode.workflow_status === "unprocessed").length;
  app.counts.processed = app.episodes.filter((episode) => episode.workflow_status === "processed").length;
  app.counts.pending_export = app.episodes.filter((episode) => episode.export_eligible).length;
  app.counts.exported = app.episodes.filter((episode) => episode.workflow_status === "exported").length;
  app.counts.failed = app.episodes.filter((episode) => episode.workflow_status === "failed").length;
  app.counts.exportable = app.counts.pending_export;
  renderSummary();
}

function renderJointOptions() {
  const select = $("#jointSelect");
  const names = app.series?.names || [];
  if (!names.length) {
    select.innerHTML = '<option value="0">无轨迹数据</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  const previous = Number(select.value) || 0;
  select.innerHTML = names
    .map((name, index) => `<option value="${index}" ${index === previous ? "selected" : ""}>${escapeHtml(name)}</option>`)
    .join("");
}

function drawChart() {
  const canvas = $("#seriesChart");
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const ratio = window.devicePixelRatio || 1;
  const width = Math.round(rect.width * ratio);
  const height = Math.round(rect.height * ratio);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  const points = app.series?.points || [];
  const duration = app.detail?.duration_s || 1;
  const pad = { left: 7, right: 7, top: 7, bottom: 20 };
  const plotWidth = rect.width - pad.left - pad.right;
  const plotHeight = rect.height - pad.top - pad.bottom;

  ctx.strokeStyle = "rgba(200,230,221,.08)";
  ctx.lineWidth = 1;
  for (let row = 0; row <= 3; row += 1) {
    const y = pad.top + (plotHeight * row) / 3;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(rect.width - pad.right, y); ctx.stroke();
  }
  for (let column = 0; column <= 5; column += 1) {
    const x = pad.left + (plotWidth * column) / 5;
    ctx.beginPath(); ctx.moveTo(x, pad.top); ctx.lineTo(x, pad.top + plotHeight); ctx.stroke();
  }
  if (!points.length) {
    ctx.fillStyle = "#60726d";
    ctx.font = "10px ui-monospace, monospace";
    ctx.fillText("NO SERIES DATA", pad.left + 6, pad.top + 18);
    return;
  }
  const dimension = Number($("#jointSelect").value) || 0;
  const values = points.flatMap((point) => [point.state[dimension], point.action[dimension]]).filter(Number.isFinite);
  let min = Math.min(...values);
  let max = Math.max(...values);
  const margin = Math.max((max - min) * 0.08, 0.001);
  min -= margin; max += margin;
  const xOf = (time) => pad.left + (time / duration) * plotWidth;
  const yOf = (value) => pad.top + (1 - (value - min) / (max - min)) * plotHeight;

  const { start, end } = trimValues();
  ctx.fillStyle = "rgba(3,8,7,.57)";
  ctx.fillRect(pad.left, pad.top, Math.max(0, xOf(start) - pad.left), plotHeight);
  ctx.fillRect(xOf(end), pad.top, Math.max(0, rect.width - pad.right - xOf(end)), plotHeight);

  function line(key, color) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.35;
    ctx.beginPath();
    points.forEach((point, index) => {
      const x = xOf(point.t);
      const y = yOf(point[key][dimension]);
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  line("state", "#65f0b5");
  line("action", "#58cadd");

  const cursorX = xOf(app.currentTime);
  ctx.strokeStyle = "rgba(255,255,255,.72)";
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(cursorX, pad.top); ctx.lineTo(cursorX, pad.top + plotHeight); ctx.stroke();
  ctx.fillStyle = "#81938e";
  ctx.font = "9px ui-monospace, monospace";
  ctx.fillText(min.toFixed(2), pad.left, rect.height - 4);
  const maxText = max.toFixed(2);
  ctx.fillText(maxText, rect.width - pad.right - ctx.measureText(maxText).width, rect.height - 4);
}

function updateExportSummary() {
  const selected = app.episodes.filter((episode) => episode.export_eligible);
  const seconds = selected.reduce(
    (sum, episode) => sum + Math.max(0, (episode.trim_end_s ?? episode.duration_s) - (episode.trim_start_s || 0)),
    0,
  );
  $("#exportEpisodes").textContent = selected.length;
  $("#exportSeconds").textContent = Math.round(seconds).toLocaleString();
}

function openExportDialog() {
  updateExportSummary();
  $("#jobProgress").classList.add("hidden");
  $("#jobError").textContent = "";
  $("#startExport").disabled = false;
  $("#exportDialog").showModal();
}

async function startExport() {
  const task = $("#taskText").value.trim();
  const output = $("#outputRoot").value.trim();
  const cameras = $$('input[name="camera"]:checked').map((node) => node.value);
  if (!task || !output) return toast("请填写输出目录和任务描述", "error");
  if (!cameras.length) return toast("请至少选择一个相机", "error");
  const episodeIds = app.episodes
    .filter((episode) => episode.export_eligible)
    .map((episode) => episode.id);
  if (!episodeIds.length) return toast("没有可导出的 episode", "error");
  $("#startExport").disabled = true;
  $("#jobProgress").classList.remove("hidden");
  $("#jobError").textContent = "";
  try {
    const job = await api("/api/jobs/convert", {
      method: "POST",
      body: {
        raw_root: rootValue(),
        output_root: output,
        task,
        fps: Number($("#exportFps").value),
        layout: $("#exportLayout").value,
        cameras,
        episode_ids: episodeIds,
        overwrite: $("#overwriteOutput").checked,
      },
    });
    app.activeJob = job.id;
    pollJob(job.id);
  } catch (error) {
    $("#startExport").disabled = false;
    $("#jobError").textContent = error.message;
    toast(error.message, "error");
  }
}

async function pollJob(jobId) {
  if (app.activeJob !== jobId) return;
  try {
    const job = await api(`/api/jobs/${jobId}`);
    const percent = Math.round(job.progress * 100);
    $("#jobMessage").textContent = job.message;
    $("#jobPercent").textContent = `${percent}%`;
    $("#jobProgressBar").style.width = `${percent}%`;
    if (job.status === "completed") {
      $("#startExport").disabled = false;
      $("#startExport").textContent = "再次导出";
      toast(`转换完成：${job.result.output_root}`, "normal", 7000);
      await scan(false);
      updateExportSummary();
      return;
    }
    if (job.status === "failed") {
      $("#startExport").disabled = false;
      $("#jobError").textContent = job.error || "转换失败";
      toast("转换失败，请查看任务日志", "error", 6000);
      return;
    }
    window.setTimeout(() => pollJob(jobId), 1000);
  } catch (error) {
    $("#jobError").textContent = error.message;
    window.setTimeout(() => pollJob(jobId), 2000);
  }
}

function bindEvents() {
  $("#scanButton").addEventListener("click", () => scan(true));
  $("#rawRoot").addEventListener("keydown", (event) => {
    if (event.key === "Enter") scan(true);
  });
  $("#searchInput").addEventListener("input", (event) => {
    app.query = event.target.value.trim();
    renderEpisodeList();
  });
  $$("#statusFilter button").forEach((button) => {
    button.addEventListener("click", () => {
      $$("#statusFilter button").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      app.filter = button.dataset.filter;
      renderEpisodeList();
    });
  });
  $("#playPause").addEventListener("click", togglePlayback);
  $("#stepBack").addEventListener("click", () => { pausePlayback(); seek(app.currentTime - 1 / 20); });
  $("#stepForward").addEventListener("click", () => { pausePlayback(); seek(app.currentTime + 1 / 20); });
  $("#seekBar").addEventListener("input", (event) => { pausePlayback(); seek(event.target.value); });
  $("#playbackSpeed").addEventListener("change", (event) => {
    cameraVideos.forEach(({ element }) => { element.playbackRate = Number(event.target.value); });
  });
  $("#trimStart").addEventListener("input", (event) => setTrim("start", event.target.value));
  $("#trimEnd").addEventListener("input", (event) => setTrim("end", event.target.value));
  $("#trimStartNumber").addEventListener("change", (event) => setTrim("start", event.target.value));
  $("#trimEndNumber").addEventListener("change", (event) => setTrim("end", event.target.value));
  $("#saveReview").addEventListener("click", saveReview);
  $("#toggleExclude").addEventListener("click", toggleExclude);
  $("#autoTrim").addEventListener("click", autoTrim);
  $("#jointSelect").addEventListener("change", drawChart);
  $("#seriesChart").addEventListener("click", (event) => {
    if (!app.detail) return;
    const rect = event.currentTarget.getBoundingClientRect();
    seek(((event.clientX - rect.left) / rect.width) * app.detail.duration_s);
  });
  $("#openExport").addEventListener("click", openExportDialog);
  $("#startExport").addEventListener("click", startExport);
  window.addEventListener("resize", drawChart);
  window.addEventListener("beforeunload", () => {
    app.preloadControllers.forEach((controller) => controller.abort());
    clearMediaCache();
  });
  window.addEventListener("keydown", (event) => {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
    if (event.code === "Space") { event.preventDefault(); togglePlayback(); }
    if (event.code === "ArrowLeft") { pausePlayback(); seek(app.currentTime - 1 / 20); }
    if (event.code === "ArrowRight") { pausePlayback(); seek(app.currentTime + 1 / 20); }
  });
}

async function init() {
  bindEvents();
  const restored = restoreEpisodeSnapshot();
  await loadHealth();
  const restoredSelection = restored && app.selectedId
    ? selectEpisode(app.selectedId)
    : null;
  await scan(false, !restoredSelection);
  if (restoredSelection) await restoredSelection;
}

init();
