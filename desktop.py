"""
Video to Vertical — desktop app.

Horizontal video (YouTube) -> vertical 9:16 clip that follows the speaker,
with noise cancellation. The window is a small local web page (ui/index.html)
shown in the system's own web view (WebKit on macOS, Edge WebView2 on Windows).

    python desktop.py               # open the app
    python desktop.py --selftest F  # headless check used by the build; writes report to F
"""
import json
import os
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import core

APP_NAME = "Video to Vertical"
OUTPUT_DIR = Path.home() / "Vertical Clips"


def ui_path(name: str = "index.html") -> str:
    return os.path.join(core.resource_dir(), "ui", name)


def open_path(path: str) -> None:
    if core.IS_WIN:
        os.startfile(path)  # noqa
    elif core.IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def reveal_path(path: str) -> None:
    if core.IS_WIN:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif core.IS_MAC:
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])


def read_clipboard() -> str:
    try:
        if core.IS_MAC:
            return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3).stdout
        if core.IS_WIN:
            import ctypes
            from ctypes import wintypes
            CF_UNICODETEXT = 13
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            u32.GetClipboardData.restype = wintypes.HANDLE
            k32.GlobalLock.restype = ctypes.c_void_p
            k32.GlobalLock.argtypes = [wintypes.HANDLE]
            k32.GlobalUnlock.argtypes = [wintypes.HANDLE]
            if not u32.OpenClipboard(None):
                return ""
            try:
                h = u32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return ""
                p = k32.GlobalLock(h)
                try:
                    return ctypes.wstring_at(p) if p else ""
                finally:
                    k32.GlobalUnlock(h)
            finally:
                u32.CloseClipboard()
        return subprocess.run(["xclip", "-o", "-selection", "clipboard"],
                              capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Bridge exposed to the page as window.pywebview.api
# --------------------------------------------------------------------------
class Api:
    def __init__(self):
        self._window = None  # set after the window is created (not exposed to JS)
        self._busy = False
        self._last = None

    def attach(self, window):
        self._window = window

    # -- calls into the page --
    def _js(self, fn: str, *args):
        if self._window is not None:
            try:
                self._window.evaluate_js(f"{fn}({', '.join(json.dumps(a) for a in args)})")
            except Exception:
                pass

    # -- called from the page --
    def app_info(self):
        return {"out_dir": str(OUTPUT_DIR).replace(str(Path.home()), "~"), "name": APP_NAME}

    def paste(self):
        return read_clipboard()

    def preview_source(self, url):
        try:
            return core.preview_sources(str(url), log=lambda m: self._js("onLog", m))
        except Exception as e:
            msg = str(e).splitlines()[0][:300]
            self._js("onLog", msg)
            return {"error": msg}

    def js_log(self, msg):
        self._js("onLog", str(msg)[:500])

    def open_url(self, url):
        if isinstance(url, str) and url.startswith("https://"):
            webbrowser.open(url)

    def open_folder(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        open_path(str(OUTPUT_DIR))

    def open_clip(self):
        if self._last and os.path.exists(self._last):
            open_path(self._last)

    def reveal_clip(self):
        if self._last and os.path.exists(self._last):
            reveal_path(self._last)

    def make_clip(self, p):
        if self._busy:
            return {"error": "A clip is already being made."}
        try:
            url = str(p.get("url", "")).strip()
            if not url:
                return {"error": "Paste a YouTube link first."}
            core.validate_range(str(p.get("start", "")), str(p.get("end", "")))
            settings = core.ClipSettings(
                lock_in=float(p.get("lock_in", 100)),
                noise_reduction=float(p.get("noise", 20)),
                normalize_audio=bool(p.get("normalize", True)),
            )
            quality = int(p.get("quality", 2160))
        except Exception as e:
            return {"error": str(e)}

        self._busy = True

        def work():
            try:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                out = core.make_clip(url, str(p["start"]), str(p["end"]), settings, str(OUTPUT_DIR),
                                     max_height=quality,
                                     progress=lambda f, m: self._js("onProgress", f, m),
                                     log=lambda m: self._js("onLog", m))
                self._last = out
                self._js("onLog", f"Saved to {out}")
                self._js("onDone", out, os.path.basename(out))
            except Exception as e:
                self._js("onLog", traceback.format_exc(limit=3))
                self._js("onError", str(e))
            finally:
                self._busy = False

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}


# --------------------------------------------------------------------------
# Self-test (run by the build pipeline on the packaged app)
# --------------------------------------------------------------------------
def selftest(report_path: str) -> int:
    import tempfile
    lines, ok = [], True

    def say(m):
        lines.append(str(m))

    try:
        ff = core.ffmpeg_exe()
        say(f"ffmpeg: {ff}")
        det = core.SpeakerDetector(log=say)
        say(f"face model loaded: {det.yunet is not None}")
        ok = ok and det.yunet is not None
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src.mp4")
            subprocess.run([ff, "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=3",
                            "-f", "lavfi", "-i", "sine=f=300:d=3",
                            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", src],
                           check=True, **core._quiet())
            out = os.path.join(tmp, "out.mp4")
            core.render_vertical(src, out, core.ClipSettings(noise_reduction=50), log=say)
            size = os.path.getsize(out)
            say(f"rendered vertical clip: {size} bytes")
            ok = ok and size > 10000
        for f in ("index.html", "icon.png"):
            exists = os.path.exists(ui_path(f))
            say(f"ui/{f}: {'ok' if exists else 'MISSING'}")
            ok = ok and exists
        import webview  # noqa: F401
        say(f"pywebview: ok ({getattr(webview, '__version__', '?')})")
        api = Api()
        say(f"api check: {api.make_clip({'url': '', 'start': '0:00', 'end': '1:00'})}")
    except Exception:
        ok = False
        say(traceback.format_exc())
    say("SELFTEST " + ("PASSED" if ok else "FAILED"))
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
def run_app() -> None:
    import webview

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    api = Api()
    window = webview.create_window(
        APP_NAME, url=ui_path(), js_api=api,
        width=600, height=940, min_size=(460, 640),
        background_color="#F6F6F8", text_select=True,
    )
    api.attach(window)

    def warmup():
        # Fetch helper tools on first launch (and check yt-dlp updates daily) in the background.
        try:
            first = not core.tools_ready()
            if first:
                api._js("onLog", "First launch: setting up helper tools…")
            core.ensure_tools(log=lambda m: api._js("onLog", m))
            if first:
                api._js("onLog", "Setup complete.")
        except Exception as e:
            api._js("onLog", f"Setup will retry when you make a clip ({e}).")

    webview.start(warmup, http_server=True, private_mode=False,
                  storage_path=os.path.join(core.data_dir(), "webview"))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2]))
    run_app()
