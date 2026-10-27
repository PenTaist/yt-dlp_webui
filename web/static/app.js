"use strict";

const $ = (s) => document.querySelector(s);
const form = $("#form"), urlInput = $("#url"), quality = $("#quality"), qualityNote = $("#quality-note");
const go = $("#go"), formError = $("#form-error"), list = $("#jobs"), empty = $("#empty");

const QUALITIES = {
  audio: [["0", "Meilleure"], ["2", "Haute"], ["5", "Moyenne, fichiers plus légers"]],
  video: [["best", "Meilleure disponible"], ["2160", "4K (2160p) maximum"], ["1440", "1440p maximum"],
          ["1080", "1080p maximum"], ["720", "720p maximum"], ["480", "480p maximum"], ["360", "360p maximum"]],
};
const FORMAT_LABEL = { mp3: "MP3", m4a: "M4A", opus: "Opus", ogg: "OGG", flac: "FLAC", wav: "WAV", mp4: "MP4", mkv: "MKV", webm: "WebM" };
const nf = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 });
const rows = new Map();
let pollTimer = null;
let currentKind = null;

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "X-Requested-With": "fetch", "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Le serveur a répondu ${res.status}.`);
  return data;
}

function selectedFormat() {
  const input = form.querySelector('input[name="fmt"]:checked');
  return { fmt: input.value, kind: input.dataset.kind };
}

function refreshQuality() {
  const { fmt, kind } = selectedFormat();
  if (kind !== currentKind) {
    const previous = quality.value;
    quality.replaceChildren(...QUALITIES[kind].map(([v, label]) => new Option(label, v)));
    if ([...quality.options].some((o) => o.value === previous)) quality.value = previous;
    currentKind = kind;
  }
  const lossless = fmt === "wav" || fmt === "flac";
  quality.disabled = lossless;
  qualityNote.textContent = lossless
    ? "Format sans perte : la qualité de la source est conservée telle quelle."
    : kind === "video" ? "Si la résolution demandée n’existe pas, la plus proche en dessous est utilisée." : "";
}

function formatSize(bytes) {
  if (!bytes) return "";
  const units = ["octets", "Ko", "Mo", "Go"];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${nf.format(v)}\u00a0${units[i]}`;
}

function statusText(job) {
  switch (job.status) {
    case "queued": return "En attente d’un emplacement libre";
    case "running": {
      const pct = `${nf.format(job.progress || 0)}\u00a0%`;
      return job.items > 1 ? `Téléchargement ${pct}, élément ${job.item} sur ${job.items}` : `Téléchargement ${pct}`;
    }
    case "processing": return "Conversion en cours";
    case "done": return `Prêt, ${formatSize(job.size)}`;
    case "error": return job.error || "Échec du téléchargement.";
    case "cancelled": return "Annulé";
    default: return job.status;
  }
}

function metaText(job) {
  const q = QUALITIES[job.kind].find(([v]) => v === job.quality);
  const parts = [FORMAT_LABEL[job.fmt] || job.fmt];
  if (q && !["wav", "flac"].includes(job.fmt)) parts.push(q[1].toLowerCase());
  if (job.playlist) parts.push("playlist");
  return parts.join(", ");
}

function buildRow(job) {
  const li = document.createElement("li");
  li.className = "job";
  li.innerHTML = `
    <div class="job-head">
      <div><p class="job-title"></p><p class="job-meta"></p></div>
      <div class="job-actions"></div>
    </div>
    <div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="100"><span></span></div>
    <p class="status"></p>
    <p class="warn" hidden></p>`;
  rows.set(job.id, li);
  return li;
}

function renderRow(li, job) {
  li.dataset.status = job.status;
  li.querySelector(".job-title").textContent = job.title || job.url;
  li.querySelector(".job-meta").textContent = metaText(job);
  li.querySelector(".status").textContent = statusText(job);
  const bar = li.querySelector(".bar");
  bar.setAttribute("aria-valuenow", String(Math.round(job.progress || 0)));
  bar.firstElementChild.style.width = `${job.progress || 0}%`;
  const warn = li.querySelector(".warn");
  warn.hidden = !job.warning;
  warn.textContent = job.warning || "";

  const actions = li.querySelector(".job-actions");
  const signature = job.status;
  if (actions.dataset.for === signature) return;
  actions.dataset.for = signature;
  actions.replaceChildren();
  const active = ["queued", "running", "processing"].includes(job.status);
  if (job.status === "done") {
    const a = document.createElement("a");
    a.className = "save";
    a.href = `/api/jobs/${job.id}/file`;
    a.textContent = "Enregistrer le fichier";
    a.setAttribute("download", job.filename || "");
    actions.append(a);
  }
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ghost";
  btn.textContent = active ? "Annuler" : "Retirer";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      await api(`/api/jobs/${job.id}`, { method: "DELETE" });
      await refreshJobs();
    } catch (e) { btn.disabled = false; alert(e.message); }
  });
  actions.append(btn);
}

function renderJobs(items) {
  const seen = new Set();
  items.forEach((job, index) => {
    seen.add(job.id);
    const li = rows.get(job.id) || buildRow(job);
    renderRow(li, job);
    if (list.children[index] !== li) list.insertBefore(li, list.children[index] || null);
  });
  for (const [id, li] of rows) if (!seen.has(id)) { li.remove(); rows.delete(id); }
  empty.hidden = items.length > 0;
  list.hidden = items.length === 0;
  return items.some((j) => ["queued", "running", "processing"].includes(j.status));
}

async function refreshJobs() {
  try {
    const active = renderJobs(await api("/api/jobs"));
    schedulePoll(active ? 1500 : 15000);
  } catch {
    schedulePoll(5000);
  }
}

function schedulePoll(ms) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refreshJobs, ms);
}

async function loadInfo() {
  try {
    const info = await api("/api/info");
    const v = info.ytdlp || {};
    $("#engine").textContent = v.version ? `yt-dlp ${v.version}` : "yt-dlp en cours d’installation";
    if (v.release_url) $("#engine").href = v.release_url;
    $("#retention").textContent = `${nf.format(info.retention_hours)}\u00a0h`;
    $("#pl-limit").textContent = `(jusqu’à ${info.max_playlist_items} éléments)`;
    const checked = v.checked_at ? new Date(v.checked_at).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }) : null;
    $("#foot").textContent = v.version
      ? `Canal de mise à jour ${v.channel || "stable"}. Dernière vérification ${checked}.`
      : "";
  } catch { }
}

const searchForm = $("#search-form"), q = $("#q"), panel = $("#search-panel");
const searchStatus = $("#search-status"), results = $("#results");
const SEARCH_LABEL = { videos: "vidéos", playlists: "playlists", songs: "titres", albums: "albums" };
const SEARCH_COUNT = 10;
const DEBOUNCE_MS = { songs: 250, albums: 250, videos: 450, playlists: 450 };
const searchCache = new Map();
let searchSeq = 0, searchTimer = null, searchCtrl = null, activeIndex = -1, shownKey = "", shownItems = [];

function fmtDuration(sec) {
  if (!sec && sec !== 0) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = String(sec % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

function quickLabel() {
  return `Télécharger en .${selectedFormat().fmt}`;
}

function refreshQuickLabels() {
  results.querySelectorAll(".quick:not([data-sent])").forEach((b) => { b.textContent = quickLabel(); });
}

function searchKind() {
  return searchForm.querySelector('input[name="stype"]:checked').value;
}

function setSearchStatus(text, isError = false) {
  searchStatus.textContent = text;
  searchStatus.hidden = !text;
  searchStatus.classList.toggle("is-error", isError);
}

function openPanel() {
  panel.hidden = false;
  q.setAttribute("aria-expanded", "true");
}

function closePanel() {
  panel.hidden = true;
  q.setAttribute("aria-expanded", "false");
  setActive(-1);
}

function setActive(index) {
  const items = results.children;
  if (activeIndex >= 0 && items[activeIndex]) items[activeIndex].classList.remove("is-active");
  activeIndex = index;
  if (index >= 0 && items[index]) {
    items[index].classList.add("is-active");
    items[index].scrollIntoView({ block: "nearest" });
    q.setAttribute("aria-activedescendant", items[index].id);
  } else {
    q.removeAttribute("aria-activedescendant");
  }
}

function buildResult(item, index) {
  const li = document.createElement("li");
  li.className = "result";
  li.id = `result-${index}`;
  li.dataset.type = item.type;
  li.addEventListener("mouseenter", () => setActive(index));

  const thumb = document.createElement("div");
  thumb.className = "thumb";
  if (item.thumbnail) {
    const img = document.createElement("img");
    img.src = item.thumbnail;
    img.alt = "";
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    thumb.append(img);
  }
  const badgeText = item.duration ? fmtDuration(item.duration)
    : item.type === "playlist" ? (item.count ? `${item.count} vidéos` : "Playlist")
    : item.type === "album" ? "Album" : "";
  if (badgeText) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = badgeText;
    thumb.append(badge);
  }

  const text = document.createElement("div");
  const title = document.createElement("p");
  title.className = "result-title";
  title.textContent = item.title || item.url;
  title.title = item.title || "";
  const sub = document.createElement("p");
  sub.className = "result-sub";
  sub.textContent = item.subtitle || "";
  text.append(title, sub);

  const actions = document.createElement("div");
  actions.className = "result-actions";
  const quick = document.createElement("button");
  quick.type = "button";
  quick.className = "quick";
  quick.textContent = quickLabel();
  quick.addEventListener("click", () => quickDownload(item, quick));
  const use = document.createElement("button");
  use.type = "button";
  use.className = "ghost";
  use.textContent = "Utiliser ce lien";
  use.addEventListener("click", () => useLink(item));
  actions.append(quick, use);

  li.append(thumb, text, actions);
  return li;
}

function renderResults(items, kind, key) {
  shownKey = key;
  shownItems = items;
  activeIndex = -1;
  results.replaceChildren(...items.map(buildResult));
  setSearchStatus(items.length ? "" : `Aucun résultat dans les ${SEARCH_LABEL[kind]}. Essayez d’autres mots ou un autre filtre.`);
  openPanel();
}

function scheduleSearch(immediate = false) {
  clearTimeout(searchTimer);
  const query = q.value.trim().replace(/\s+/g, " ");
  if (query.length < 2) {
    searchSeq++;
    if (searchCtrl) searchCtrl.abort();
    panel.classList.remove("is-loading");
    results.replaceChildren();
    shownKey = "";
    closePanel();
    return;
  }
  const kind = searchKind();
  const key = `${kind}|${query.toLowerCase()}`;
  if (key === shownKey) {
    openPanel();
    return;
  }
  if (searchCache.has(key)) {
    searchSeq++;
    if (searchCtrl) searchCtrl.abort();
    panel.classList.remove("is-loading");
    renderResults(searchCache.get(key), kind, key);
    return;
  }
  searchTimer = setTimeout(() => runSearch(query, kind, key), immediate ? 0 : DEBOUNCE_MS[kind]);
}

async function runSearch(query, kind, key) {
  if (searchCtrl) searchCtrl.abort();
  searchCtrl = new AbortController();
  const seq = ++searchSeq;
  panel.classList.add("is-loading");
  if (!results.children.length) setSearchStatus("Recherche…");
  openPanel();
  try {
    const params = new URLSearchParams({ q: query, type: kind, limit: String(SEARCH_COUNT) });
    const data = await api(`/api/search?${params}`, { signal: searchCtrl.signal });
    if (seq !== searchSeq) return;
    const items = (data.results || []).slice(0, SEARCH_COUNT);
    searchCache.set(key, items);
    if (searchCache.size > 100) searchCache.delete(searchCache.keys().next().value);
    renderResults(items, kind, key);
  } catch (err) {
    if (err.name === "AbortError" || seq !== searchSeq) return;
    results.replaceChildren();
    shownKey = "";
    setSearchStatus(err.message, true);
    openPanel();
  } finally {
    if (seq === searchSeq) panel.classList.remove("is-loading");
  }
}

async function quickDownload(item, button) {
  const { fmt } = selectedFormat();
  button.disabled = true;
  button.textContent = "Envoi…";
  try {
    await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ url: item.url, format: fmt, quality: quality.value, playlist: item.playlist }),
    });
    button.dataset.sent = "1";
    button.textContent = "Ajouté aux téléchargements";
    await refreshJobs();
  } catch (err) {
    button.disabled = false;
    button.textContent = quickLabel();
    setSearchStatus(err.message, true);
  }
}

function useLink(item) {
  closePanel();
  urlInput.value = item.url;
  $("#playlist").checked = !!item.playlist;
  formError.textContent = "";
  urlInput.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
  urlInput.focus({ preventScroll: true });
}

q.addEventListener("input", () => scheduleSearch());
q.addEventListener("focus", () => { if (results.children.length || !searchStatus.hidden) scheduleSearch(true); });
q.addEventListener("keydown", (e) => {
  const count = results.children.length;
  if (e.key === "ArrowDown" && count) {
    e.preventDefault();
    openPanel();
    setActive(Math.min(activeIndex + 1, count - 1));
  } else if (e.key === "ArrowUp" && count) {
    e.preventDefault();
    setActive(Math.max(activeIndex - 1, 0));
  } else if (e.key === "Enter") {
    e.preventDefault();
    if (!panel.hidden && activeIndex >= 0 && shownItems[activeIndex]) useLink(shownItems[activeIndex]);
    else scheduleSearch(true);
  } else if (e.key === "Escape" && !panel.hidden) {
    e.preventDefault();
    closePanel();
  }
});
searchForm.addEventListener("submit", (e) => { e.preventDefault(); scheduleSearch(true); });
searchForm.addEventListener("change", (e) => {
  if (e.target.name !== "stype") return;
  shownKey = "";
  q.focus();
  scheduleSearch(true);
});
document.addEventListener("pointerdown", (e) => {
  if (!panel.hidden && !e.target.closest(".search-box")) closePanel();
});

form.addEventListener("change", (e) => { if (e.target.name === "fmt") { refreshQuality(); refreshQuickLabels(); } });

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  formError.textContent = "";
  const url = urlInput.value.trim();
  if (!/^https?:\/\/\S+$/i.test(url)) {
    formError.textContent = "Collez un lien complet qui commence par https://.";
    urlInput.focus();
    return;
  }
  const { fmt } = selectedFormat();
  go.disabled = true;
  go.textContent = "Envoi…";
  try {
    await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({ url, format: fmt, quality: quality.value, playlist: $("#playlist").checked }),
    });
    urlInput.value = "";
    await refreshJobs();
  } catch (err) {
    formError.textContent = err.message;
  } finally {
    go.disabled = false;
    go.textContent = "Lancer le téléchargement";
  }
});

if (navigator.clipboard && window.isSecureContext) {
  const paste = $("#paste");
  paste.hidden = false;
  paste.addEventListener("click", async () => {
    try { urlInput.value = (await navigator.clipboard.readText()).trim(); urlInput.focus(); }
    catch { formError.textContent = "Le navigateur a refusé l’accès au presse-papiers. Collez le lien avec Ctrl+V."; }
  });
}

refreshQuality();
loadInfo();
refreshJobs();
setInterval(loadInfo, 10 * 60 * 1000);
