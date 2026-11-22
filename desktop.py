"""
Video to Vertical — desktop app.

Horizontal video (YouTube) -> vertical 9:16 clip that follows the speaker,
with noise cancellation. Packaged with PyInstaller into a standalone app.

    python desktop.py               # open the app
    python desktop.py --selftest F  # headless check used by the build; writes report to F
"""
import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import core

APP_NAME = "Video to Vertical"
OUTPUT_DIR = Path.home() / "Vertical Clips"
QUALITY = {
    "Best available (up to 4K, sharpest)": 2160,
    "1080p (faster download)": 1080,
    "720p (fastest)": 720,
}


def open_path(path: str) -> None:
    if core.IS_WIN:
        os.startfile(path)  # noqa
    elif core.IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


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
        if det.yunet is None:
            ok = False
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
        import tkinter  # noqa: F401  (the window toolkit must be bundled)
        say("tkinter: ok")
    except Exception:
        ok = False
        say(traceback.format_exc())
    say("SELFTEST " + ("PASSED" if ok else "FAILED"))
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------
def run_app() -> None:
    import tkinter as tk
    from tkinter import ttk, messagebox
    from tkinter.scrolledtext import ScrolledText

    root = tk.Tk()
    root.title(APP_NAME)
    root.minsize(560, 640)
    try:
        ttk.Style().theme_use("aqua" if core.IS_MAC else "vista" if core.IS_WIN else "clam")
    except tk.TclError:
        pass

    events: "queue.Queue[tuple]" = queue.Queue()
    state = {"busy": False, "last": None}

    pad = {"padx": 14, "pady": 4}
    frm = ttk.Frame(root, padding=10)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)

    ttk.Label(frm, text="📱  Video to Vertical", font=("", 18, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
    ttk.Label(frm, text="Horizontal video → vertical clip that follows the speaker").grid(
        row=1, column=0, columnspan=2, sticky="w", padx=14)

    ttk.Label(frm, text="YouTube link").grid(row=2, column=0, columnspan=2, sticky="w", padx=14, pady=(14, 0))
    url = tk.StringVar()
    url_entry = ttk.Entry(frm, textvariable=url)
    url_entry.grid(row=3, column=0, columnspan=2, sticky="ew", **pad)

    ttk.Label(frm, text="Start (mm:ss or h:mm:ss)").grid(row=4, column=0, sticky="w", padx=14, pady=(8, 0))
    ttk.Label(frm, text="End (max 5 min after start)").grid(row=4, column=1, sticky="w", padx=14, pady=(8, 0))
    start, end = tk.StringVar(value="0:00"), tk.StringVar(value="1:00")
    ttk.Entry(frm, textvariable=start, width=12).grid(row=5, column=0, sticky="ew", **pad)
    ttk.Entry(frm, textvariable=end, width=12).grid(row=5, column=1, sticky="ew", **pad)

    def slider(row, title, left, right, initial):
        var = tk.DoubleVar(value=initial)
        head = ttk.Frame(frm)
        head.grid(row=row, column=0, columnspan=2, sticky="ew", padx=14, pady=(12, 0))
        ttk.Label(head, text=title, font=("", 12, "bold")).pack(side="left")
        val = ttk.Label(head, text=str(initial))
        val.pack(side="right")
        ttk.Scale(frm, from_=0, to=100, variable=var,
                  command=lambda v: val.config(text=str(int(float(v))))).grid(
            row=row + 1, column=0, columnspan=2, sticky="ew", padx=14)
        ends = ttk.Frame(frm)
        ends.grid(row=row + 2, column=0, columnspan=2, sticky="ew", padx=14)
        ttk.Label(ends, text=left, foreground="gray").pack(side="left")
        ttk.Label(ends, text=right, foreground="gray").pack(side="right")
        return var

    lock = slider(6, "Speaker tracking", "Smooth, calm camera", "Locked on speaker", 55)
    noise = slider(9, "Noise cancellation", "Off", "Strongest", 40)

    normalize = tk.BooleanVar(value=True)
    ttk.Checkbutton(frm, text="Even out volume for social media", variable=normalize).grid(
        row=12, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 0))

    ttk.Label(frm, text="Download quality").grid(row=13, column=0, sticky="w", padx=14, pady=(8, 0))
    quality = tk.StringVar(value=list(QUALITY)[0])
    ttk.Combobox(frm, textvariable=quality, values=list(QUALITY), state="readonly").grid(
        row=14, column=0, columnspan=2, sticky="ew", **pad)

    go = ttk.Button(frm, text="Make clip")
    go.grid(row=15, column=0, columnspan=2, sticky="ew", padx=14, pady=(14, 4))
    bar = ttk.Progressbar(frm, maximum=1000)
    bar.grid(row=16, column=0, columnspan=2, sticky="ew", **pad)
    status = ttk.Label(frm, text="Ready.")
    status.grid(row=17, column=0, columnspan=2, sticky="w", padx=14)

    logbox = ScrolledText(frm, height=7, wrap="word", font=("Menlo" if core.IS_MAC else "Consolas", 10))
    logbox.grid(row=18, column=0, columnspan=2, sticky="nsew", padx=14, pady=(8, 4))
    logbox.configure(state="disabled")
    frm.rowconfigure(18, weight=1)

    btns = ttk.Frame(frm)
    btns.grid(row=19, column=0, columnspan=2, sticky="ew", padx=14, pady=(4, 6))
    play = ttk.Button(btns, text="▶ Play last clip", state="disabled",
                      command=lambda: state["last"] and open_path(state["last"]))
    play.pack(side="left")
    ttk.Button(btns, text="Open clips folder",
               command=lambda: (OUTPUT_DIR.mkdir(parents=True, exist_ok=True), open_path(str(OUTPUT_DIR)))).pack(side="right")

    def log(msg):
        events.put(("log", msg))

    def prog(frac, msg):
        events.put(("progress", frac, msg))

    def worker(args):
        try:
            out = core.make_clip(*args, progress=prog, log=log)
            events.put(("done", out))
        except Exception as e:
            log(traceback.format_exc(limit=2))
            events.put(("error", str(e)))

    def start_job():
        if state["busy"]:
            return
        if not url.get().strip():
            messagebox.showwarning(APP_NAME, "Paste a YouTube link first.")
            return
        try:
            core.validate_range(start.get(), end.get())
        except ValueError as e:
            messagebox.showwarning(APP_NAME, str(e))
            return
        settings = core.ClipSettings(lock_in=lock.get(), noise_reduction=noise.get(),
                                     normalize_audio=normalize.get())
        state["busy"] = True
        go.config(state="disabled", text="Working…")
        bar["value"] = 0
        status.config(text="Starting…")
        args = (url.get().strip(), start.get(), end.get(), settings, str(OUTPUT_DIR),
                QUALITY.get(quality.get(), 2160))
        threading.Thread(target=worker, args=(args,), daemon=True).start()

    go.config(command=start_job)

    def pump():
        try:
            while True:
                ev = events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    logbox.configure(state="normal")
                    logbox.insert("end", ev[1] + "\n")
                    logbox.see("end")
                    logbox.configure(state="disabled")
                elif kind == "progress":
                    bar["value"] = max(0, min(1000, ev[1] * 1000))
                    status.config(text=ev[2] + "…")
                elif kind in ("done", "error"):
                    state["busy"] = False
                    go.config(state="normal", text="Make clip")
                    if kind == "done":
                        state["last"] = ev[1]
                        bar["value"] = 1000
                        status.config(text=f"Done! Saved {os.path.basename(ev[1])}")
                        play.config(state="normal")
                        log(f"Saved to {ev[1]}")
                    else:
                        status.config(text="Something went wrong — see log.")
                        messagebox.showerror(APP_NAME, ev[1])
        except queue.Empty:
            pass
        root.after(100, pump)

    root.after(100, pump)

    # First launch: fetch helpers in the background so the first clip starts faster.
    def warmup():
        try:
            if not core.tools_ready():
                core.ensure_tools(progress=prog, log=log)
                events.put(("progress", 0, "Ready"))
            else:
                core.ensure_tools(log=lambda m: None)  # daily yt-dlp update check
        except Exception as e:
            log(f"Setup will retry when you make a clip ({e}).")

    threading.Thread(target=warmup, daemon=True).start()
    url_entry.focus_set()
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2]))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    run_app()
