import base64
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections import deque
from pathlib import Path
from urllib.parse import urlencode, urlparse

from flask import Flask, Response, abort, g, jsonify, request, send_file
from ytmusicapi import YTMusic

YTDLP_BIN = os.environ.get("YTDLP_BIN", "/opt/yt-dlp/bin/yt-dlp")
VERSION_FILE = Path(os.environ.get("YTDLP_VERSION_FILE", "/opt/yt-dlp/VERSION.json"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "/config/cookies.txt"))
MAX_CONCURRENT = max(1, int(os.environ.get("MAX_CONCURRENT_JOBS", "2")))
MAX_QUEUED = max(1, int(os.environ.get("MAX_QUEUED_JOBS", "20")))
MAX_ACTIVE_PER_CLIENT = max(1, int(os.environ.get("MAX_ACTIVE_PER_CLIENT", "5")))
MAX_PLAYLIST_ITEMS = max(1, int(os.environ.get("MAX_PLAYLIST_ITEMS", "200")))
RETENTION_SECONDS = float(os.environ.get("RETENTION_HOURS", "24")) * 3600
JOB_TIMEOUT = float(os.environ.get("JOB_TIMEOUT_MINUTES", "180")) * 60
SEARCH_LIMIT = min(50, max(5, int(os.environ.get("SEARCH_LIMIT", "20"))))
SEARCH_CACHE_SECONDS = float(os.environ.get("SEARCH_CACHE_MINUTES", "10")) * 60
AUTH_USER = os.environ.get("AUTH_USER", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")

if not (AUTH_USER and AUTH_PASSWORD) and os.environ.get("ALLOW_NO_AUTH") != "1":
    raise SystemExit("AUTH_USER et AUTH_PASSWORD sont obligatoires "
                     "(ou ALLOW_NO_AUTH=1 si un proxy authentifiant protège déjà l'accès).")

AUDIO_FORMATS = {"mp3": "mp3", "m4a": "m4a", "wav": "wav", "flac": "flac", "opus": "opus", "ogg": "vorbis"}
VIDEO_FORMATS = {"mp4", "mkv", "webm"}
AUDIO_QUALITIES = {"0", "2", "5"}
VIDEO_QUALITIES = {"best", "2160", "1440", "1080", "720", "480", "360"}
SKIP_SUFFIXES = {".part", ".ytdl", ".tmp", ".temp", ".jpg", ".jpeg", ".png", ".webp"}
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
CLIENT_COOKIE = "ytdlp_client"
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
SEP = "\x1f"
PROGRESS_TEMPLATE = SEP.join([
    "download:PROG", "%(progress._percent_str)s", "%(info.playlist_index)s",
    "%(info.n_entries)s", "%(info.playlist_title)s", "%(info.title)s",
])

logging.basicConfig(level=logging.INFO, format="%(asctime)s [web] %(levelname)s %(message)s")
log = logging.getLogger("ytdlp-web")

app = Flask(__name__, static_folder="static", static_url_path="/static")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
slots = threading.Semaphore(MAX_CONCURRENT)
PUBLIC_FIELDS = ("id", "url", "kind", "fmt", "quality", "playlist", "status", "progress",
                 "title", "item", "items", "error", "warning", "filename", "size", "created")


def _authorized() -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    ok_user = hmac.compare_digest(user.encode(), AUTH_USER.encode())
    ok_pwd = hmac.compare_digest(pwd.encode(), AUTH_PASSWORD.encode())
    return ok_user and ok_pwd


@app.before_request
def guard():
    if request.path == "/healthz":
        return None
    if AUTH_USER and not _authorized():
        return Response("Authentification requise.", 401,
                        {"WWW-Authenticate": 'Basic realm="Telechargeur", charset="UTF-8"'})
    if request.method in ("POST", "DELETE") and request.headers.get("X-Requested-With") != "fetch":
        abort(403)
    header_id = request.headers.get("X-Client-Id", "")
    cookie_id = request.cookies.get(CLIENT_COOKIE, "")
    if CLIENT_ID_RE.match(header_id):
        g.client_id, g.new_client = header_id, False
    elif CLIENT_ID_RE.match(cookie_id):
        g.client_id, g.new_client = cookie_id, False
    else:
        g.client_id, g.new_client = secrets.token_urlsafe(32), True
    return None


@app.after_request
def security_headers(resp):
    if g.get("new_client"):
        secure = request.is_secure or request.headers.get("X-Forwarded-Proto", "") == "https"
        resp.set_cookie(CLIENT_COOKIE, g.client_id, max_age=365 * 24 * 3600, httponly=True,
                        secure=secure, samesite="Lax", path="/")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data: https://i.ytimg.com https://*.googleusercontent.com https://*.ggpht.com; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    return resp


def public(job: dict) -> dict:
    return {k: job.get(k) for k in PUBLIC_FIELDS}


def update(job: dict, **fields) -> None:
    with jobs_lock:
        job.update(fields)


def safe_name(text: str, fallback: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text or "").strip(" .")
    return (text[:120] or fallback)


def build_command(job: dict, workdir: Path) -> list[str]:
    cmd = [
        sys.executable, YTDLP_BIN,
        "--ignore-config", "--newline", "--no-colors", "--progress",
        "--progress-template", PROGRESS_TEMPLATE,
        "--no-mtime", "--no-overwrites", "--windows-filenames", "--embed-metadata",
        "--retries", "5", "--fragment-retries", "10",
        "-P", str(workdir),
        "-o", "%(playlist_index&{} - |)s%(title).150B [%(id)s].%(ext)s",
    ]
    if job["playlist"]:
        cmd += ["--yes-playlist", "--ignore-errors", "--playlist-items", f"1:{MAX_PLAYLIST_ITEMS}"]
    else:
        cmd += ["--no-playlist"]

    if COOKIES_FILE.is_file():
        copy = workdir / ".cookies.txt"
        shutil.copyfile(COOKIES_FILE, copy)
        cmd += ["--cookies", str(copy)]

    if job["kind"] == "audio":
        cmd += ["-f", "ba/b", "-x", "--audio-format", AUDIO_FORMATS[job["fmt"]]]
        if job["fmt"] not in ("wav", "flac"):
            cmd += ["--audio-quality", job["quality"]]
        if job["fmt"] in ("mp3", "m4a"):
            cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]
    else:
        fmt, q = job["fmt"], job["quality"]
        res = "res" if q == "best" else f"res:{q}"
        codecs = {"mp4": "vcodec:h264,acodec:m4a", "mkv": "vcodec,acodec", "webm": "vcodec:vp9,acodec:opus"}[fmt]
        cmd += ["-f", "bv*+ba/b", "-S", f"{res},{codecs}", "--merge-output-format", fmt]
        cmd += ["--recode-video", fmt] if fmt == "webm" else ["--remux-video", fmt]

    cmd += ["--", job["url"]]
    return cmd


def parse_progress(line: str, job: dict) -> None:
    parts = line.split(SEP, 5)
    if len(parts) < 6:
        return
    _, pct_s, idx_s, n_s, ptitle, title = parts
    try:
        pct = float(re.sub(r"[^0-9.]", "", pct_s) or 0)
    except ValueError:
        pct = 0.0
    idx = int(idx_s) if idx_s.isdigit() else 1
    n = int(n_s) if n_s.isdigit() else 1
    overall = ((idx - 1) + pct / 100) / max(n, 1) * 100
    display = ptitle if (job["playlist"] and n > 1 and ptitle not in ("NA", "")) else title
    update(job, status="running", progress=round(min(overall, 99.5), 1),
           item=idx, items=n, title=display if display != "NA" else job["title"])


def collect_outputs(workdir: Path) -> list[Path]:
    return sorted(p for p in workdir.iterdir()
                  if p.is_file() and not p.name.startswith(".") and p.suffix.lower() not in SKIP_SUFFIXES)


def kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass


def run_job(job_id: str) -> None:
    job = jobs[job_id]
    with slots:
        if job["status"] == "cancelled":
            return
        workdir = DOWNLOAD_DIR / job_id
        workdir.mkdir(parents=True, exist_ok=True)
        update(job, status="running", progress=0)
        tail: deque[str] = deque(maxlen=40)
        try:
            if not Path(YTDLP_BIN).is_file():
                raise RuntimeError("yt-dlp n'est pas encore installé, réessayez dans une minute.")
            proc = subprocess.Popen(
                build_command(job, workdir), cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True,
            )
            job["_proc"] = proc
            deadline = time.monotonic() + JOB_TIMEOUT
            timer = threading.Timer(JOB_TIMEOUT, kill, args=(proc,))
            timer.daemon = True
            timer.start()
            for raw in proc.stdout:
                line = raw.rstrip()
                if line.startswith("PROG" + SEP):
                    parse_progress(line, job)
                    continue
                tail.append(line)
                if line.startswith(("[ExtractAudio]", "[Merger]", "[VideoRemuxer]", "[VideoConvertor]",
                                    "[EmbedThumbnail]", "[Metadata]")):
                    update(job, status="processing")
            rc = proc.wait()
            timer.cancel()
            timed_out = time.monotonic() >= deadline

            if job["status"] == "cancelled":
                shutil.rmtree(workdir, ignore_errors=True)
                return

            outputs = collect_outputs(workdir)
            if not outputs:
                errors = [l for l in tail if l.startswith("ERROR")]
                msg = "Délai maximal dépassé." if timed_out else (errors[-1][6:].strip() if errors else
                                                                  "yt-dlp n'a produit aucun fichier.")
                raise RuntimeError(msg)

            warning = None
            if rc != 0:
                warning = "Certains éléments n'ont pas pu être téléchargés."

            if len(outputs) == 1:
                result = outputs[0]
            else:
                result = workdir / f".{safe_name(job['title'], 'playlist')}.zip"
                update(job, status="processing")
                with zipfile.ZipFile(result, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                    for f in outputs:
                        zf.write(f, f.name)
                for f in outputs:
                    f.unlink(missing_ok=True)
            download_name = result.name.lstrip(".")
            update(job, status="done", progress=100, warning=warning, _path=str(result),
                   filename=download_name, size=result.stat().st_size, finished=time.time())
            log.info("Tâche %s terminée : %s", job_id, download_name)
        except Exception as exc:
            log.warning("Tâche %s en erreur : %s", job_id, exc)
            update(job, status="error", error=str(exc)[:500], finished=time.time())
            shutil.rmtree(workdir, ignore_errors=True)
        finally:
            job.pop("_proc", None)


def janitor() -> None:
    while True:
        time.sleep(600)
        limit = time.time() - RETENTION_SECONDS
        with jobs_lock:
            expired = [jid for jid, j in jobs.items()
                       if j["status"] in ("done", "error", "cancelled") and j.get("finished", j["created"]) < limit]
            for jid in expired:
                jobs.pop(jid, None)
        for jid in expired:
            shutil.rmtree(DOWNLOAD_DIR / jid, ignore_errors=True)


def startup_cleanup() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for child in DOWNLOAD_DIR.iterdir():
        if child.is_dir() and JOB_ID_RE.match(child.name):
            shutil.rmtree(child, ignore_errors=True)


startup_cleanup()
threading.Thread(target=janitor, daemon=True).start()


SEARCH_TYPES = {"videos", "playlists", "songs", "albums"}
_search_cache: dict[tuple, tuple[float, list]] = {}
_search_cache_lock = threading.Lock()
_search_slots = threading.Semaphore(4)
_ytm_local = threading.local()


def _ytmusic() -> YTMusic:
    if not hasattr(_ytm_local, "client"):
        _ytm_local.client = YTMusic(location="FR")
    return _ytm_local.client


def _thumb(thumbnails, target: int = 320) -> str | None:
    thumbs = [t for t in (thumbnails or []) if isinstance(t, dict) and t.get("url")]
    if not thumbs:
        return None
    thumbs.sort(key=lambda t: t.get("width") or 0)
    for t in thumbs:
        if (t.get("width") or 0) >= target:
            return t["url"]
    return thumbs[-1]["url"]


def _names(items) -> str:
    return ", ".join(a.get("name", "") for a in (items or []) if isinstance(a, dict) and a.get("name"))


def _search_music(query: str, kind: str, limit: int) -> list[dict]:
    raw = _ytmusic().search(query, filter=kind, limit=limit)
    results = []
    for r in raw:
        if kind == "songs":
            vid = r.get("videoId")
            if not vid or r.get("isAvailable") is False:
                continue
            album = (r.get("album") or {}).get("name")
            results.append({
                "type": "song", "id": vid, "title": r.get("title") or "",
                "subtitle": ", ".join(x for x in (_names(r.get("artists")), album) if x),
                "duration": r.get("duration_seconds"), "count": None,
                "thumbnail": _thumb(r.get("thumbnails"), 120),
                "url": f"https://music.youtube.com/watch?v={vid}", "playlist": False,
            })
        elif kind == "albums":
            pid, bid = r.get("playlistId"), r.get("browseId")
            if not (pid or bid):
                continue
            url = (f"https://music.youtube.com/playlist?list={pid}" if pid
                   else f"https://music.youtube.com/browse/{bid}")
            results.append({
                "type": "album", "id": pid or bid, "title": r.get("title") or "",
                "subtitle": ", ".join(x for x in (_names(r.get("artists")), r.get("type"), r.get("year")) if x),
                "duration": None, "count": None,
                "thumbnail": _thumb(r.get("thumbnails"), 226),
                "url": url, "playlist": True,
            })
    return results


def _search_youtube(query: str, kind: str, limit: int) -> list[dict]:
    if not Path(YTDLP_BIN).is_file():
        raise RuntimeError("yt-dlp n'est pas encore installé.")
    if kind == "videos":
        target = f"ytsearch{limit}:{query}"
    else:
        target = "https://www.youtube.com/results?" + urlencode({"search_query": query, "sp": "EgIQAw=="})
    cmd = [sys.executable, YTDLP_BIN, "--ignore-config", "--no-warnings", "--flat-playlist",
           "--playlist-end", str(limit), "-J", "--", target]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    if proc.returncode != 0:
        err = [l for l in proc.stderr.splitlines() if l.startswith("ERROR")]
        raise RuntimeError(err[-1][6:].strip() if err else "La recherche YouTube a échoué.")
    data = json.loads(proc.stdout or "{}")
    results = []
    for e in data.get("entries") or []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        url_in = e.get("url") or ""
        is_playlist = "/playlist?list=" in url_in
        is_video = not is_playlist and (e.get("ie_key") == "Youtube" or "/watch?v=" in url_in or "/shorts/" in url_in)
        if (kind == "playlists" and not is_playlist) or (kind == "videos" and not is_video):
            continue
        if is_playlist:
            url = e.get("url") or f"https://www.youtube.com/playlist?list={e['id']}"
        else:
            url = f"https://www.youtube.com/watch?v={e['id']}"
        results.append({
            "type": "playlist" if is_playlist else "video", "id": e["id"], "title": e.get("title") or "",
            "subtitle": e.get("channel") or e.get("uploader") or "",
            "duration": None if is_playlist else e.get("duration"),
            "count": e.get("playlist_count"),
            "thumbnail": _thumb(e.get("thumbnails")),
            "url": url, "playlist": is_playlist,
        })
    return results


def run_search(query: str, kind: str, limit: int) -> list[dict]:
    key = (kind, query.casefold(), limit)
    now = time.time()
    with _search_cache_lock:
        hit = _search_cache.get(key)
        if hit and now - hit[0] < SEARCH_CACHE_SECONDS:
            return hit[1]
    with _search_slots:
        results = (_search_music(query, kind, limit) if kind in ("songs", "albums")
                   else _search_youtube(query, kind, limit))[:limit]
    with _search_cache_lock:
        if len(_search_cache) > 300:
            _search_cache.clear()
        _search_cache[key] = (now, results)
    return results


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/docs")
def docs():
    return app.send_static_file("docs.html")


API_ERRORS = {
    403: "Requête refusée : l'en-tête X-Requested-With: fetch est obligatoire pour POST et DELETE.",
    404: "Ressource introuvable.",
    405: "Méthode non autorisée sur cette route.",
}


@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(405)
def api_error(exc):
    if request.path.startswith("/api/"):
        return jsonify(error=API_ERRORS.get(exc.code, exc.description)), exc.code
    return exc


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/info")
def info():
    try:
        version = json.loads(VERSION_FILE.read_text("utf-8"))
    except Exception:
        version = {}
    return jsonify({
        "ytdlp": {k: version.get(k) for k in ("version", "channel", "repo", "updated_at", "checked_at", "release_url")},
        "retention_hours": RETENTION_SECONDS / 3600,
        "max_playlist_items": MAX_PLAYLIST_ITEMS,
        "search_limit": SEARCH_LIMIT,
    })


@app.get("/api/search")
def search():
    query = " ".join(str(request.args.get("q", "")).split())
    kind = request.args.get("type", "videos")
    if not 2 <= len(query) <= 200:
        return jsonify(error="Saisissez entre 2 et 200 caractères."), 400
    if kind not in SEARCH_TYPES:
        return jsonify(error="Type de recherche inconnu (videos, playlists, songs ou albums)."), 400
    try:
        limit = min(SEARCH_LIMIT, max(1, int(request.args.get("limit", "10"))))
    except ValueError:
        limit = 10
    try:
        results = run_search(query, kind, limit)
    except subprocess.TimeoutExpired:
        return jsonify(error="YouTube met trop de temps à répondre. Réessayez."), 504
    except Exception as exc:
        log.warning("Recherche « %s » (%s) en échec : %s", query, kind, exc)
        return jsonify(error=f"La recherche a échoué : {str(exc)[:200]}"), 502
    return jsonify({"query": query, "type": kind, "results": results})


@app.get("/api/jobs")
def list_jobs():
    with jobs_lock:
        items = sorted((public(j) for j in jobs.values() if j["owner"] == g.client_id),
                       key=lambda j: j["created"], reverse=True)
    return jsonify(items)


@app.post("/api/jobs")
def create_job():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    fmt = str(data.get("format", "")).lower()
    quality = str(data.get("quality", ""))
    playlist = bool(data.get("playlist"))

    parsed = urlparse(url)
    if len(url) > 2048 or parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify(error="Le lien doit commencer par http:// ou https://."), 400
    if fmt in AUDIO_FORMATS:
        kind = "audio"
        quality = quality if quality in AUDIO_QUALITIES else "0"
    elif fmt in VIDEO_FORMATS:
        kind = "video"
        quality = quality if quality in VIDEO_QUALITIES else "best"
    else:
        return jsonify(error="Format non pris en charge."), 400

    with jobs_lock:
        active = sum(1 for j in jobs.values() if j["status"] in ("queued", "running", "processing"))
        mine = sum(1 for j in jobs.values()
                   if j["owner"] == g.client_id and j["status"] in ("queued", "running", "processing"))
        if mine >= MAX_ACTIVE_PER_CLIENT:
            return jsonify(error=f"Vous avez déjà {mine} téléchargements en cours. Attendez qu'un d'eux se termine."), 429
        if active >= MAX_QUEUED:
            return jsonify(error="Le serveur traite déjà beaucoup de téléchargements. Réessayez dans quelques minutes."), 429
        job_id = uuid.uuid4().hex
        jobs[job_id] = {
            "id": job_id, "owner": g.client_id, "url": url, "kind": kind, "fmt": fmt, "quality": quality, "playlist": playlist,
            "status": "queued", "progress": 0, "title": url, "item": None, "items": None,
            "error": None, "warning": None, "filename": None, "size": None, "created": time.time(),
        }
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    return jsonify(public(jobs[job_id])), 201


def get_job_or_404(job_id: str) -> dict:
    job = jobs.get(job_id) if JOB_ID_RE.match(job_id) else None
    if job is None or not hmac.compare_digest(job["owner"], g.client_id):
        abort(404)
    return job


@app.get("/api/jobs/<job_id>")
def get_job(job_id):
    return jsonify(public(get_job_or_404(job_id)))


@app.get("/api/jobs/<job_id>/file")
def download(job_id):
    job = get_job_or_404(job_id)
    path = job.get("_path")
    if job["status"] != "done" or not path or not Path(path).is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=job["filename"], conditional=True)


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id):
    job = get_job_or_404(job_id)
    if job["status"] in ("queued", "running", "processing"):
        update(job, status="cancelled", finished=time.time())
        proc = job.get("_proc")
        if proc:
            kill(proc)
        return jsonify(public(job))
    with jobs_lock:
        jobs.pop(job_id, None)
    shutil.rmtree(DOWNLOAD_DIR / job_id, ignore_errors=True)
    return "", 204
