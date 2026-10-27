import datetime as dt
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPOS = {
    "stable": "yt-dlp/yt-dlp",
    "nightly": "yt-dlp/yt-dlp-nightly-builds",
    "master": "yt-dlp/yt-dlp-master-builds",
}
CHANNEL = os.environ.get("YTDLP_CHANNEL", "stable").strip().lower()
REPO = os.environ.get("YTDLP_REPO") or REPOS.get(CHANNEL, REPOS["stable"])
INTERVAL = max(0.25, float(os.environ.get("UPDATE_INTERVAL_HOURS", "6"))) * 3600
RETRY_DELAY = 15 * 60
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

BASE = Path(os.environ.get("YTDLP_HOME", "/opt/yt-dlp"))
BIN = BASE / "bin" / "yt-dlp"
META = BASE / "VERSION.json"
ASSET = "yt-dlp"
SUMS = "SHA2-256SUMS"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [updater] %(levelname)s %(message)s")
log = logging.getLogger()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def http_get(url: str, api: bool = False, timeout: int = 120) -> bytes:
    headers = {"User-Agent": "ytdlp-web-updater"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        if TOKEN:
            headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def load_meta() -> dict:
    try:
        return json.loads(META.read_text("utf-8"))
    except Exception:
        return {}


def save_meta(meta: dict) -> None:
    tmp = META.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), "utf-8")
    os.replace(tmp, META)


def expected_hash(sums_text: str) -> str:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == ASSET:
            return parts[0].lower()
    raise RuntimeError(f"Empreinte de « {ASSET} » absente de {SUMS}")


def latest_release() -> dict:
    try:
        release = json.loads(http_get(f"https://api.github.com/repos/{REPO}/releases/latest", api=True, timeout=30))
        release["_assets"] = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
        return release
    except urllib.error.HTTPError as exc:
        if exc.code not in (403, 429):
            raise
        log.warning("API GitHub indisponible (%s), repli sur github.com", exc.code)
    req = urllib.request.Request(f"https://github.com/{REPO}/releases/latest",
                                 headers={"User-Agent": "ytdlp-web-updater"}, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as resp:
        final = resp.geturl()
    if "/releases/tag/" not in final:
        raise RuntimeError(f"Impossible de déterminer la dernière release ({final})")
    tag = urllib.parse.unquote(final.rsplit("/releases/tag/", 1)[1])
    base = f"https://github.com/{REPO}/releases/download/{urllib.parse.quote(tag)}"
    return {"tag_name": tag, "html_url": final,
            "_assets": {ASSET: f"{base}/{ASSET}", SUMS: f"{base}/{SUMS}"}}


def check_once() -> None:
    release = latest_release()
    tag = release["tag_name"]
    meta = load_meta()

    if meta.get("version") == tag and meta.get("repo") == REPO and BIN.is_file():
        meta["checked_at"] = now()
        save_meta(meta)
        log.info("yt-dlp %s (%s) est à jour", tag, REPO)
        return

    log.info("Nouvelle version disponible : %s -> %s (%s)", meta.get("version", "aucune"), tag, REPO)
    assets = release["_assets"]
    if ASSET not in assets or SUMS not in assets:
        raise RuntimeError(f"Release {tag} incomplète (assets {ASSET}/{SUMS} manquants)")

    want = expected_hash(http_get(assets[SUMS]).decode("utf-8", "replace"))
    data = http_get(assets[ASSET], timeout=300)
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        raise RuntimeError(f"Empreinte invalide : attendu {want}, obtenu {got}")

    BIN.parent.mkdir(parents=True, exist_ok=True)
    tmp = BIN.with_name("yt-dlp.new")
    tmp.write_bytes(data)
    tmp.chmod(0o755)

    out = subprocess.run([sys.executable, str(tmp), "--version"], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Le nouveau binaire ne démarre pas : {out.stderr.strip()[:300]}")

    os.replace(tmp, BIN)
    save_meta({
        "version": tag,
        "reported_version": out.stdout.strip(),
        "repo": REPO,
        "channel": CHANNEL,
        "published_at": release.get("published_at"),
        "release_url": release.get("html_url"),
        "sha256": got,
        "updated_at": now(),
        "checked_at": now(),
    })
    log.info("yt-dlp mis à jour en %s", out.stdout.strip())


def main() -> None:
    log.info("Canal %s, dépôt %s, vérification toutes les %.1f h", CHANNEL, REPO, INTERVAL / 3600)
    while True:
        try:
            check_once()
            delay = INTERVAL
        except Exception as exc:
            log.error("Échec de la vérification : %s", exc)
            delay = RETRY_DELAY if BIN.is_file() else 60
        time.sleep(delay)


if __name__ == "__main__":
    main()
