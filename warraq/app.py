"""Desktop launcher: runs the local server and opens the GUI in a native window.

Order of preference:
1. a native window via pywebview (WebView2 on Windows, WebKit on macOS),
2. a chromeless Edge/Chrome "app" window,
3. the default web browser.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser

from . import APP_NAME, __version__, paths

log = logging.getLogger("warraq")


def _setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        fh = logging.handlers.RotatingFileHandler(paths.log_dir() / "warraq.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:  # pragma: no cover
        pass
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def free_port(preferred: int = 0) -> int:
    if preferred:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int):
    import uvicorn

    from .server.api import AppState, create_app

    state = AppState()
    app = create_app(state)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False, timeout_keep_alive=30)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="warraq-server", daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return server, state


def _browser_app_window(url: str) -> bool:
    """Open a chromeless Edge/Chrome window (looks like a native app)."""
    candidates = []
    if sys.platform == "win32":
        pf = [os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), os.environ.get("PROGRAMFILES", r"C:\Program Files"), os.environ.get("LOCALAPPDATA", "")]
        for base in pf:
            candidates += [
                os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
            ]
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    else:
        candidates += [shutil.which(n) or "" for n in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")]
    profile = paths.data_dir() / "browser-profile"
    for exe in candidates:
        if exe and os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1440,920", "--no-first-run"])
                return True
            except Exception:
                continue
    return False


def main(browser: bool = False, port: int = 0) -> int:
    _setup_logging()
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    port = free_port(port or int(os.environ.get("WARRAQ_PORT", "0") or 0))
    server, state = start_server(port)
    url = f"http://127.0.0.1:{port}/"
    log.info("%s %s running at %s", APP_NAME, __version__, url)
    if not browser and os.environ.get("WARRAQ_BROWSER") != "1":
        try:
            import webview  # pywebview

            window = webview.create_window(
                APP_NAME,
                url,
                width=1440,
                height=920,
                min_size=(980, 640),
                background_color="#0c0c0f",
                text_select=True,
            )
            state.window = window
            webview.start(private_mode=False, storage_path=str(paths.data_dir() / "webview"))
            server.should_exit = True
            return 0
        except Exception as exc:
            log.warning("native window unavailable (%s); falling back to a browser window", exc)
    if not _browser_app_window(url):
        webbrowser.open(url)
    print(f"{APP_NAME} is running at {url}  (press Ctrl+C to quit)")
    try:
        while not server.should_exit:
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
