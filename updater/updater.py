#!/usr/bin/env python3
"""Invoice intake agent - updater and launcher.

Double-click update.bat (Windows) or update.command (Mac). A small page opens in
the browser showing the installed version and the latest one. From there the
app is installed or updated in one click (the data folder with the settings,
the Gmail token and the invoices is kept), an older version can be installed,
and the app is started - the Start button runs run.bat / run.sh for you.

Needs Python 3.9 or newer and nothing else. The only outside calls are to
GitHub, to read the version list and download the zip. The app is installed
in the "app" folder next to this file.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = "OttomanLabsAI/agent-invoice-data-extraction"
BRANCH = os.environ.get("INVOICE_AGENT_BRANCH", "main")  # the build "Update" installs
HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("INVOICE_AGENT_UPDATER_PORT", "8766"))
APP_HOST, APP_PORT = "127.0.0.1", int(os.environ.get("INVOICE_AGENT_PORT", "8765"))
APP_URL = f"http://localhost:{APP_PORT}"
KEEP = ("data", ".venv")  # carried over from the old version to the new one
USER_AGENT = "invoice-agent-updater"
CACHE_SECONDS = 600


# --------------------------------------------------------------------------- versions and GitHub


def version_key(text: str | None):
    """'v3.2', '3.2' or '3.2.0' -> (3, 2); None when there is no version in the text."""
    m = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def version_label(text: str | None) -> str:
    key = version_key(text)
    return f"v{key[0]}.{key[1]}" if key else ""


def valid_tag(tag: str | None) -> bool:
    return bool(tag) and (tag == "latest" or re.fullmatch(r"v?\d+\.\d+(\.\d+)?", tag) is not None)


def fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def download(url: str, dest: Path, progress=None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as response, open(dest, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)


def app_running() -> bool:
    """True when something answers on the app's port."""
    try:
        with socket.create_connection((APP_HOST, APP_PORT), timeout=1):
            return True
    except OSError:
        return False


def remove_tree(path: Path) -> None:
    """rmtree that copes with read-only files and Windows letting go of handles late."""

    def writable(func, target, _exc):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    for attempt in range(6):
        if not path.exists():
            return
        shutil.rmtree(path, onerror=writable)
        if not path.exists():
            return
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Could not remove {path}. Close any window showing that folder and try again.")


# --------------------------------------------------------------------------- the manager


class Manager:
    """Knows the installed app, asks GitHub what exists, installs, starts."""

    def __init__(self, base: Path):
        self.base = Path(base)
        self.lock = threading.Lock()
        self.busy = False
        self.log: list[str] = []
        self.error = ""
        self.progress = None
        self.cache: dict = {}
        self.starting_since: float | None = None

    def say(self, text: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {text}")
        print(text, flush=True)

    # ---- the installed app

    def app_dir(self) -> Path | None:
        preferred = self.base / "app"
        if (preferred / "app.py").exists():
            return preferred
        for child in sorted(self.base.iterdir()):
            if child.is_dir() and child.name not in ("app.new", "app.old") and (child / "app.py").exists() and (child / "agent").is_dir():
                return child
        return None

    def installed_version(self, folder: Path | None = None) -> str:
        folder = folder or self.app_dir()
        if not folder:
            return ""
        version_file = folder / "VERSION"
        if version_file.exists():
            return version_label(version_file.read_text(encoding="utf-8")) or "unknown"
        package = folder / "package.json"  # builds before the VERSION file carried the version here
        if package.exists():
            try:
                return version_label(json.loads(package.read_text(encoding="utf-8")).get("version", "")) or "unknown"
            except ValueError:
                pass
        return "unknown"

    # ---- GitHub

    def latest_version(self) -> str:
        for name in ("VERSION", "package.json"):
            try:
                text = fetch(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{name}").decode("utf-8")
                label = version_label(json.loads(text).get("version", "") if name.endswith(".json") else text)
                if label:
                    return label
            except Exception:  # noqa: BLE001 - offline, missing file, bad JSON: try the next
                continue
        return ""

    def versions(self) -> list[dict] | None:
        """Published versions newest first, or None when GitHub cannot be reached."""
        try:
            items = []
            for release in json.loads(fetch(f"https://api.github.com/repos/{REPO}/releases?per_page=100").decode("utf-8")):
                if release.get("draft") or not release.get("tag_name"):
                    continue
                label = release["tag_name"]
                if release.get("name") and release["name"] != release["tag_name"]:
                    label += " - " + release["name"]
                if release.get("published_at"):
                    label += f" ({release['published_at'][:10]})"
                items.append({"tag": release["tag_name"], "label": label})
            if not items:
                tags = json.loads(fetch(f"https://api.github.com/repos/{REPO}/tags?per_page=100").decode("utf-8"))
                items = [{"tag": t["name"], "label": t["name"]} for t in tags if t.get("name")]
        except Exception:  # noqa: BLE001
            return None
        items = [i for i in items if version_key(i["tag"])]
        items.sort(key=lambda i: version_key(i["tag"]), reverse=True)
        for item in items:
            item["zip"] = f"https://github.com/{REPO}/archive/refs/tags/{item['tag']}.zip"
        return items

    def status(self, refresh: bool = False) -> dict:
        now = time.time()
        if refresh or now - self.cache.get("at", 0) > CACHE_SECONDS:
            self.cache = {"at": now, "latest": self.latest_version(), "versions": self.versions()}
        folder = self.app_dir()
        installed = self.installed_version(folder)
        latest = self.cache["latest"]
        installed_key, latest_key = version_key(installed), version_key(latest)
        if not folder:
            state = "not_installed"
        elif not latest:
            state = "unknown_latest"
        elif installed_key and latest_key and installed_key >= latest_key:
            state = "up_to_date"
        else:
            state = "update_available"
        running = app_running()
        if self.starting_since is not None and running:
            self.starting_since = None
            threading.Thread(target=webbrowser.open, args=(APP_URL,), daemon=True).start()
        if self.starting_since is not None and now - self.starting_since > 300:
            self.starting_since = None
        return {
            "base": str(self.base), "folder": str(folder) if folder else "", "installed": installed, "latest": latest, "state": state,
            "versions": self.cache["versions"], "github_ok": self.cache["versions"] is not None and bool(latest),
            "latest_zip": f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip", "branch": BRANCH,
            "app_running": running, "app_url": APP_URL, "starting": self.starting_since is not None,
            "busy": self.busy, "log": self.log[-80:], "error": self.error, "progress": self.progress,
            "platform": "windows" if os.name == "nt" else ("mac" if sys.platform == "darwin" else "linux"),
        }

    # ---- install / update

    def install(self, tag: str) -> None:
        """Download a version and make it the installed app. Safe to call from a thread; one at a time."""
        with self.lock:
            if self.busy:
                return
            self.busy, self.log, self.error, self.progress = True, [], "", None
        try:
            self._install(tag)
        except Exception as exc:  # noqa: BLE001 - shown on the page
            self.error = str(exc)
            self.say(f"Stopped: {exc}")
        finally:
            self.busy, self.progress = False, None
            self.cache["at"] = 0  # re-read the versions next time

    def _install(self, tag: str) -> None:
        if not valid_tag(tag):
            raise RuntimeError("That is not a version I know how to install.")
        if app_running():
            raise RuntimeError("The app is running. Close its window (the one that opened when it started), then try again.")
        if tag == "latest":
            url, what = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip", "the latest version"
        else:
            url, what = f"https://github.com/{REPO}/archive/refs/tags/{tag}.zip", tag
        zip_path, new_dir, old_dir, target = self.base / "download.zip", self.base / "app.new", self.base / "app.old", self.base / "app"
        for leftover in (new_dir, old_dir):
            if leftover.exists():
                remove_tree(leftover)

        self.say(f"Downloading {what}...")
        download(url, zip_path, progress=lambda done, total: setattr(self, "progress", [done, total]))
        self.say(f"Downloaded {zip_path.stat().st_size // 1024} KB.")
        self.say("Unpacking...")
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(new_dir)
        inner = next((p for p in new_dir.iterdir() if p.is_dir() and (p / "app.py").exists()), None)
        if inner is None:
            raise RuntimeError("The download did not contain the app.")

        current = self.app_dir()
        if current:
            for name in KEEP:
                source = current / name
                if source.exists():
                    self.say(f"Keeping {name}...")
                    shutil.move(str(source), str(inner / name))
            self.say(f"Removing the old version ({self.installed_version(current)})...")
            current.rename(old_dir)
            remove_tree(old_dir)
        inner.rename(target)
        remove_tree(new_dir)
        zip_path.unlink(missing_ok=True)
        self.say("Deleted the zip.")
        if (target / ".venv").exists():
            self.refresh_venv(target)
        self.say(f"Installed {self.installed_version(target)} in {target}.")

    def refresh_venv(self, app_dir: Path) -> None:
        python = app_dir / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
        if not python.exists():
            remove_tree(app_dir / ".venv")
            return
        self.say("Checking the app's parts (pip)...")
        try:
            result = subprocess.run([str(python), "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                                    cwd=str(app_dir), capture_output=True, text=True, timeout=900)
            ok = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            self.say("Could not refresh the parts - the app will set itself up again on its first start.")
            remove_tree(app_dir / ".venv")

    # ---- start

    def start_app(self) -> str:
        folder = self.app_dir()
        if not folder:
            return "Install the app first."
        if app_running():
            return "The app is already running."
        if os.name == "nt":
            subprocess.Popen(["cmd", "/c", "start", "Invoice intake agent", "run.bat"], cwd=str(folder))
        else:
            log = open(self.base / "app.log", "ab")  # noqa: SIM115 - handed to the child process
            subprocess.Popen(["bash", "run.sh"], cwd=str(folder), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.starting_since = time.time()
        return "Starting the app. The first start sets up its parts and takes a minute or two; it opens in the browser by itself when ready."


# --------------------------------------------------------------------------- the local page


class Handler(BaseHTTPRequestHandler):
    manager: Manager
    token: str
    page_path: Path
    server_ref: ThreadingHTTPServer

    def log_message(self, *args) -> None:  # keep the console for the manager's own lines
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        if path == "/":
            page = self.page_path.read_text(encoding="utf-8") if self.page_path.exists() else "<p>updater.html is missing next to updater.py.</p>"
            self._send(200, page.replace("__TOKEN__", self.token).encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(200, self.manager.status(refresh="refresh=1" in self.path))
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.headers.get("X-Updater-Token") != self.token:
            self._json(403, {"ok": False, "message": "Open the page from update.bat / update.command."})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        path = self.path.split("?", 1)[0]
        if path == "/api/install":
            tag = body.get("version") or "latest"
            if not valid_tag(tag):
                self._json(400, {"ok": False, "message": "That is not a version I know how to install."})
                return
            if self.manager.busy:
                self._json(409, {"ok": False, "message": "Already busy."})
                return
            threading.Thread(target=self.manager.install, args=(tag,), daemon=True).start()
            self._json(200, {"ok": True})
        elif path == "/api/start":
            self._json(200, {"ok": True, "message": self.manager.start_app()})
        elif path == "/api/quit":
            self._json(200, {"ok": True})
            threading.Timer(0.3, self.server_ref.shutdown).start()
        else:
            self._json(404, {"ok": False, "message": "not found"})


def make_server(manager: Manager, port: int = PORT, page_path: Path | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    Handler.manager = manager
    Handler.token = secrets.token_urlsafe(16)
    Handler.page_path = page_path or (HERE / "updater.html")
    Handler.server_ref = server
    return server


def main() -> None:
    url = f"http://127.0.0.1:{PORT}/"
    try:
        server = make_server(Manager(HERE))
    except OSError:
        print(f"The updater page is already open at {url} - opening it again.")
        webbrowser.open(url)
        return
    print(f"Invoice intake agent updater. Opening {url} in your browser.")
    print("Leave this window open while you use the page; the app itself keeps running after it is closed.")
    threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("Updater closed.")


if __name__ == "__main__":
    main()
