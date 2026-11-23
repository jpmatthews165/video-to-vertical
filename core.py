"""
Video to Vertical core: download a YouTube section, track the speaker,
crop to vertical 9:16, and clean up the audio.

UI-free so it can be tested on its own (see app.py for the interface).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import tempfile
from dataclasses import dataclass

import cv2
import numpy as np

MAX_CLIP_SECONDS = 5 * 60
OUT_W, OUT_H = 1080, 1920


# --------------------------------------------------------------------------
# Where things live (works both from source and inside the packaged app)
# --------------------------------------------------------------------------
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
EXE = ".exe" if IS_WIN else ""


def resource_dir() -> str:
    """Folder holding bundled files (bin/, models/)."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    """Per-user folder for downloaded helper tools."""
    if IS_WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, "Video to Vertical")
    os.makedirs(d, exist_ok=True)
    return d


def _quiet() -> dict:
    """Don't flash console windows on Windows; run helpers in their own process group
    so Cancel can stop them together with anything they start."""
    if IS_WIN:
        return {"creationflags": 0x08000000 | 0x00000200}  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
    return {"start_new_session": True}


def _kill_tree(proc) -> None:
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=0x08000000)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


class Cancelled(Exception):
    """Raised when the user presses Cancel."""


_cancel = threading.Event()
_procs: set = set()
_procs_lock = threading.Lock()


def reset_cancel() -> None:
    _cancel.clear()


def cancel() -> None:
    """Stop the current job: flag it and kill any helper processes it started."""
    _cancel.set()
    with _procs_lock:
        procs = list(_procs)
    for p in procs:
        _kill_tree(p)


def check_cancel() -> None:
    if _cancel.is_set():
        raise Cancelled()


def _track(proc):
    with _procs_lock:
        _procs.add(proc)
    if _cancel.is_set():
        _kill_tree(proc)
    return proc


def _untrack(proc) -> None:
    with _procs_lock:
        _procs.discard(proc)


def ffmpeg_exe() -> str:
    bundled = os.path.join(resource_dir(), "bin", "ffmpeg" + EXE)
    if os.path.exists(bundled):
        return bundled
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        found = shutil.which("ffmpeg")
        if not found:
            raise RuntimeError("ffmpeg not found.")
        return found


def _ssl_context():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _download(url: str, dest: str, label: str, progress=None) -> None:
    import urllib.request
    tmp = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": "VideoToVertical"})
    with urllib.request.urlopen(req, context=_ssl_context(), timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            check_cancel()
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total, f"{label} ({done >> 20} of {total >> 20} MB)")
    os.replace(tmp, dest)


YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/" + (
    "yt-dlp.exe" if IS_WIN else "yt-dlp_macos" if IS_MAC else "yt-dlp")


def _deno_url() -> str:
    import platform
    m = platform.machine().lower()
    arch = "aarch64" if m in ("arm64", "aarch64") else "x86_64"
    target = ("pc-windows-msvc" if IS_WIN else "apple-darwin" if IS_MAC else "unknown-linux-gnu")
    return f"https://github.com/denoland/deno/releases/latest/download/deno-{arch}-{target}.zip"


def tool_paths() -> tuple[str, str]:
    d = data_dir()
    return os.path.join(d, "yt-dlp" + EXE), os.path.join(d, "deno" + EXE)


def tools_ready() -> bool:
    return all(os.path.exists(p) for p in tool_paths())


_tools_lock = threading.Lock()


def ensure_tools(progress=None, log=print) -> tuple[str, str]:
    """Download yt-dlp and Deno into the user folder on first run (~60 MB),
    and keep yt-dlp up to date (YouTube changes often)."""
    with _tools_lock:
        return _ensure_tools(progress, log)


def _ensure_tools(progress, log) -> tuple[str, str]:
    import time
    import zipfile
    ytdlp, deno = tool_paths()
    if not os.path.exists(deno):
        log("One-time setup: downloading Deno (helps read YouTube)…")
        z = deno + ".zip"
        _download(_deno_url(), z, "Downloading Deno", progress)
        with zipfile.ZipFile(z) as zf:
            name = next(n for n in zf.namelist() if os.path.basename(n).startswith("deno"))
            with zf.open(name) as src, open(deno, "wb") as dst:
                shutil.copyfileobj(src, dst)
        os.remove(z)
        os.chmod(deno, 0o755)
    if not os.path.exists(ytdlp):
        log("One-time setup: downloading the YouTube downloader…")
        _download(YTDLP_URL, ytdlp, "Downloading yt-dlp", progress)
        os.chmod(ytdlp, 0o755)
        _touch_update_stamp()
    else:
        stamp = os.path.join(data_dir(), "ytdlp-updated")
        if not os.path.exists(stamp) or time.time() - os.path.getmtime(stamp) > 86400:
            update_ytdlp(log)
    return ytdlp, deno


def _touch_update_stamp():
    with open(os.path.join(data_dir(), "ytdlp-updated"), "w") as f:
        f.write("ok")


def update_ytdlp(log=print) -> None:
    ytdlp, _ = tool_paths()
    try:
        r = subprocess.run([ytdlp, "-U"], capture_output=True, text=True, timeout=180, **_quiet())
        msg = (r.stdout + r.stderr).strip().splitlines()
        if msg:
            log("yt-dlp: " + msg[-1])
        _touch_update_stamp()
    except Exception as ex:
        log(f"Couldn't check for yt-dlp updates ({ex}).")


# --------------------------------------------------------------------------
# Timestamps
# --------------------------------------------------------------------------
def parse_timestamp(text: str) -> float:
    """Accepts 90, 1:30, 01:02:03, 1:30.5"""
    text = (text or "").strip()
    if not text:
        raise ValueError("Timestamp is empty.")
    if not re.fullmatch(r"\d+(:\d{1,2}){0,2}(\.\d+)?", text):
        raise ValueError(f"Can't read timestamp '{text}'. Use mm:ss or hh:mm:ss.")
    parts = [float(p) for p in text.split(":")]
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def validate_range(start: str, end: str) -> tuple[float, float]:
    s, e = parse_timestamp(start), parse_timestamp(end)
    if e <= s:
        raise ValueError("End time must be after start time.")
    if e - s > MAX_CLIP_SECONDS:
        raise ValueError(f"Clip is {e - s:.0f}s long. The limit is 5 minutes (300s).")
    return s, e


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------
def ytdlp_common(deno: str) -> list[str]:
    """Options that make yt-dlp tolerate slow or flaky connections to YouTube."""
    return [
        "--js-runtimes", f"deno:{deno}",
        "--socket-timeout", "30",          # default 20s was timing out fetching YouTube's player code
        "--retries", "5", "--fragment-retries", "5", "--extractor-retries", "3",
        "--retry-sleep", "exp=1:8",
        "--cache-dir", os.path.join(data_dir(), "yt-dlp-cache"),  # keep solved player code between runs
    ]


class DownloadIssue:
    """Notes what went wrong in one yt-dlp run, for retries and friendly messages."""
    def __init__(self):
        self.challenge_failed = False
        self.network = False

    def scan(self, line: str):
        low = line.lower()
        if "challenge solving failed" in low or "signature solving failed" in low:
            self.challenge_failed = True
        if any(k in low for k in ("timed out", "unexpected_eof", "connection reset", "temporary failure",
                                  "network is unreachable", "remote end closed")):
            self.network = True


def _fmt_time(sec: float) -> str:
    sec = max(0.0, sec)
    return f"{int(sec // 3600)}:{int(sec % 3600 // 60):02d}:{sec % 60:06.3f}"


STALL_SECONDS = 90


def _hms(text: str) -> float:
    h, m, sec = text.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def download_section(url: str, start: float, end: float, workdir: str,
                     max_height: int = 2160, progress=None, log=print,
                     issue: "DownloadIssue | None" = None) -> str:
    import queue as _queue
    import time as _time
    ytdlp, deno = ensure_tools(progress=progress, log=log)
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(deno) + os.pathsep + env.get("PATH", "")
    out_tmpl = os.path.join(workdir, "source.%(ext)s")
    want = end - start
    cmd = [
        ytdlp, "--no-playlist", "--newline", "--no-colors", "--no-mtime",
        "-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "--merge-output-format", "mp4",
        # Copy the section as-is (no re-encode here: the clip is re-encoded once, later).
        "--download-sections", f"*{_fmt_time(start)}-{_fmt_time(end)}",
        "--ffmpeg-location", ffmpeg_exe(),
        *ytdlp_common(deno),
        # If YouTube drops the connection mid-download, resume instead of stopping early.
        "--downloader-args", "ffmpeg_i:-reconnect 1 -reconnect_on_network_error 1 -reconnect_delay_max 5",
        "-o", out_tmpl, url,
    ]
    log(f"Downloading section from YouTube (up to {max_height}p)…")
    check_cancel()
    proc = _track(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", env=env, **_quiet()))

    lines: "_queue.Queue[str | None]" = _queue.Queue()

    def reader():
        for raw in proc.stdout:
            for part in raw.replace("\r", "\n").split("\n"):
                if part.strip():
                    lines.put(part.strip())
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    tail: list[str] = []
    last_activity = _time.time()
    last_logged = 0.0
    stalled = False
    while True:
        try:
            line = lines.get(timeout=2)
        except _queue.Empty:
            if _cancel.is_set():
                break
            if _time.time() - last_activity > STALL_SECONDS:
                stalled = True
                log(f"No download progress for {STALL_SECONDS}s — restarting the download.")
                _kill_tree(proc)
                break
            continue
        if line is None:
            break
        last_activity = _time.time()
        tail = (tail + [line])[-15:]
        if issue is not None:
            issue.scan(line)
        frac = None
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            frac = float(m.group(1)) / 100
        else:
            m = re.search(r"time=\s*(\d+:\d{2}:\d{2}(?:\.\d+)?)", line)   # ffmpeg progress
            if m and want > 0:
                frac = min(1.0, _hms(m.group(1)) / want)
        if frac is not None:
            speed = re.search(r"\bat\s+(~?\s*[\d.]+\s*\w+/s)", line) or re.search(r"speed=\s*([\d.]+x)", line)
            sp = ""
            if speed:
                sp = speed.group(1).replace(" ", "")
                sp = f" · {sp[:-1]}× real-time" if sp.endswith("x") else f" · {sp}"
            label = f"Downloading from YouTube · {round(frac * 100)}%{sp}"
            if progress:
                progress(0.02 + 0.18 * frac, label)
            if _time.time() - last_logged > 15:          # a heartbeat in Logs
                last_logged = _time.time()
                log(label)
        elif "ERROR" in line or "WARNING" in line or "Downloading 1 format" in line:
            log(line)
    proc.wait()
    _untrack(proc)
    check_cancel()
    if stalled:
        if issue is not None:
            issue.network = True
        raise RuntimeError("The download stalled.")
    files = [f for f in os.listdir(workdir) if f.startswith("source.") and not f.endswith(".part")
             and not f.endswith(".ytdl")]
    if proc.returncode != 0 or not files:
        hint = "\n".join(tail[-5:])
        raise RuntimeError("YouTube download failed. Check the link (is the video public or unlisted?).\n" + hint)
    files.sort(key=lambda f: (not f.endswith(".mp4"), f))
    return os.path.join(workdir, files[0])


_preview_cache: dict = {}


def preview_sources(url: str, log=print) -> dict:
    """Direct, low-res stream URLs for the in-app preview player.

    Returns {"video": url, "audio": url-or-None}. Uses a single 360p file with sound
    when YouTube offers one, otherwise a small video-only stream plus an audio stream.
    """
    key = url.strip()
    if key in _preview_cache:
        return _preview_cache[key]
    ytdlp, deno = ensure_tools(log=log)
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(deno) + os.pathsep + env.get("PATH", "")
    base = [ytdlp, "--no-playlist", "--no-warnings", "-g", *ytdlp_common(deno)]
    tries = [
        "18",                                                           # 360p mp4 with sound
        "bv*[height<=480][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]",
        "b[height<=720][vcodec^=avc1][acodec!=none]/b[height<=720]",
    ]
    last_err = ""
    for fmt in tries:
        try:
            r = subprocess.run(base + ["-f", fmt, key], capture_output=True, text=True,
                               timeout=90, env=env, **_quiet())
        except subprocess.TimeoutExpired:
            last_err = "timed out"
            continue
        urls = [u for u in r.stdout.split() if u.startswith("http")]
        if r.returncode == 0 and urls:
            res = {"video": urls[0], "audio": urls[1] if len(urls) > 1 else None}
            _preview_cache[key] = res
            return res
        last_err = (r.stderr.strip().splitlines() or ["no playable format"])[-1]
    raise RuntimeError(f"Preview unavailable: {last_err}")


# --------------------------------------------------------------------------
# Speaker detection
# --------------------------------------------------------------------------
class SpeakerDetector:
    """Finds the speaker's horizontal position in a frame.

    Uses OpenCV's bundled face detectors (frontal + profile) and falls back to
    a full-body person detector when the face is too small or turned away.
    Picks the candidate closest to where the speaker was last seen, so people
    in the background don't steal the frame.
    """

    YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
                 "face_detection_yunet/face_detection_yunet_2023mar.onnx")
    YUNET_NAME = "face_detection_yunet_2023mar.onnx"

    def __init__(self, log=print):
        self.yunet = None
        self.yunet_size = None
        self.frontal = self.profile = None

        # 1) YuNet: small, accurate neural face detector (downloaded once, ~230 KB).
        if hasattr(cv2, "FaceDetectorYN"):
            try:
                model = os.path.join(resource_dir(), "models", self.YUNET_NAME)
                if not os.path.exists(model):
                    model = os.path.join(data_dir(), self.YUNET_NAME)
                    if not os.path.exists(model):
                        log("Downloading face model (one time)…")
                        _download(self.YUNET_URL, model, "Downloading face model")
                self.yunet = cv2.FaceDetectorYN.create(model, "", (320, 320), 0.6, 0.3, 50)
            except Exception as ex:
                log(f"Neural face model unavailable ({ex}); using basic detector.")
                self.yunet = None

        # 2) Classic Haar face detectors (bundled with opencv-python 4.x).
        if self.yunet is None:
            base = getattr(getattr(cv2, "data", None), "haarcascades", "")
            for attr, fname in (("frontal", "haarcascade_frontalface_default.xml"),
                                ("profile", "haarcascade_profileface.xml")):
                path = os.path.join(base, fname)
                if os.path.exists(path):
                    c = cv2.CascadeClassifier(path)
                    if not c.empty():
                        setattr(self, attr, c)

        # 3) Full-body person detector as last resort.
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    def _faces(self, small: np.ndarray, scale: float) -> list[tuple[float, float]]:
        cands = []
        sh, sw = small.shape[:2]
        if self.yunet is not None:
            if self.yunet_size != (sw, sh):
                self.yunet.setInputSize((sw, sh))
                self.yunet_size = (sw, sh)
            _, faces = self.yunet.detect(small)
            if faces is not None:
                for f in faces:
                    x, y, fw, fh, conf = f[0], f[1], f[2], f[3], f[-1]
                    cands.append(((x + fw / 2) / scale, fw * fh * conf))
            return cands
        gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        min_face = max(18, sw // 60)
        if self.frontal is not None:
            for (x, y, fw, fh) in self.frontal.detectMultiScale(gray, 1.1, 5, minSize=(min_face, min_face)):
                cands.append(((x + fw / 2) / scale, fw * fh))
        if self.profile is not None:  # profile cascade sees one side; flip for the other
            for (x, y, fw, fh) in self.profile.detectMultiScale(gray, 1.1, 5, minSize=(min_face, min_face)):
                cands.append(((x + fw / 2) / scale, fw * fh))
            for (x, y, fw, fh) in self.profile.detectMultiScale(cv2.flip(gray, 1), 1.1, 5, minSize=(min_face, min_face)):
                cands.append(((sw - (x + fw / 2)) / scale, fw * fh))
        return cands

    def detect(self, frame: np.ndarray, last_x: float | None) -> float | None:
        h, w = frame.shape[:2]
        scale = 1280.0 / w if w > 1280 else 1.0
        small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale != 1.0 else frame

        cands = self._faces(small, scale)  # (center_x in source px, size weight)

        if not cands:
            hs = 640.0 / w if w > 640 else 1.0
            tiny = cv2.resize(frame, None, fx=hs, fy=hs, interpolation=cv2.INTER_AREA) if hs != 1.0 else frame
            rects, weights = self.hog.detectMultiScale(tiny, winStride=(8, 8), padding=(8, 8), scale=1.05)
            for (x, y, bw, bh), wgt in zip(rects, np.ravel(weights) if len(rects) else []):
                if wgt > 0.5:
                    cands.append(((x + bw / 2) / hs, bw * bh * 0.05))

        if not cands:
            return None
        if last_x is None:
            return max(cands, key=lambda c: c[1])[0]
        # Prefer near the previous position; size breaks ties.
        def score(c):
            dist = abs(c[0] - last_x) / w
            return dist * 4.0 - np.log1p(c[1]) * 0.05
        return min(cands, key=score)[0]


# --------------------------------------------------------------------------
# Path smoothing (the "lock-in vs smooth" slider)
# --------------------------------------------------------------------------
def tracking_params(lock_in: float) -> dict:
    """lock_in: 0 = very smooth/lazy camera, 100 = tightly locked on speaker."""
    t = float(np.clip(lock_in, 0, 100)) / 100.0
    return {
        # How far the speaker can drift from center before the camera follows,
        # as a fraction of the crop width.
        "dead_zone": 0.28 * (1 - t) + 0.02 * t,
        # Gaussian smoothing window, seconds.
        "sigma_s": 1.6 * (1 - t) + 0.12 * t,
        # Max pan speed in crop-widths per second.
        "max_speed": 0.35 * (1 - t) + 2.5 * t,
    }


def build_camera_path(raw_x: np.ndarray, fps: float, crop_w: int, src_w: int,
                      lock_in: float) -> np.ndarray:
    """raw_x: per-frame speaker x (NaN where not found). Returns crop left edges."""
    n = len(raw_x)
    p = tracking_params(lock_in)
    idx = np.arange(n)
    valid = ~np.isnan(raw_x)
    if not valid.any():
        x = np.full(n, src_w / 2.0)
    else:
        x = np.interp(idx, idx[valid], raw_x[valid])  # fill gaps, hold ends

    # Dead zone: camera target only moves once the speaker leaves the zone.
    dz = p["dead_zone"] * crop_w
    target = np.empty(n)
    cur = x[0]
    for i in range(n):
        if x[i] > cur + dz:
            cur = x[i] - dz
        elif x[i] < cur - dz:
            cur = x[i] + dz
        target[i] = cur

    # Zero-phase Gaussian smoothing (no lag, since we process offline).
    sigma = max(1.0, p["sigma_s"] * fps)
    radius = int(3 * sigma)
    k = np.exp(-0.5 * (np.arange(-radius, radius + 1) / sigma) ** 2)
    k /= k.sum()
    padded = np.pad(target, radius, mode="edge")
    smooth = np.convolve(padded, k, mode="valid")

    # Speed limit, applied forward then backward so it stays centered.
    vmax = p["max_speed"] * crop_w / fps
    for arr in (smooth, smooth[::-1]):
        for i in range(1, n):
            d = arr[i] - arr[i - 1]
            if abs(d) > vmax:
                arr[i] = arr[i - 1] + np.sign(d) * vmax

    left = smooth - crop_w / 2.0
    return np.clip(left, 0, src_w - crop_w)


# --------------------------------------------------------------------------
# Audio
# --------------------------------------------------------------------------
def measure_noise_floor(src_path: str) -> float:
    """Estimate background noise level (dBFS) from the quietest 10% of 0.1s windows."""
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostats", "-i", src_path, "-vn", "-ac", "1", "-ar", "48000",
           "-af", "asetnsamples=n=4800,astats=metadata=1:reset=1,"
                  "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
           "-f", "null", "-"]
    try:
        check_cancel()
        p = _track(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, **_quiet()))
        try:
            out, _ = p.communicate(timeout=300)
        finally:
            _untrack(p)
        check_cancel()
        vals = [float(v) for v in re.findall(r"RMS_level=(-?[\d.]+)", out)]
        vals = [v for v in vals if v > -120]
        if len(vals) >= 5:
            return float(np.percentile(vals, 10))
    except Exception:
        pass
    return -50.0


def audio_filter(noise_reduction: float, normalize: bool = True,
                 noise_floor_db: float = -50.0) -> str:
    """noise_reduction: 0 = off, 100 = strongest."""
    t = float(np.clip(noise_reduction, 0, 100)) / 100.0
    filters = []
    if t > 0:
        nf = float(np.clip(noise_floor_db + 3, -80, -20))
        filters.append(f"highpass=f={int(60 + 40 * t)}")             # rumble / HVAC
        filters.append(f"afftdn=nr={4 + 41 * t:.1f}:nf={nf:.1f}:tn=1")  # adaptive spectral denoise
    if normalize:
        filters.append("loudnorm=I=-14:TP=-1.5:LRA=11")  # social-media loudness
    return ",".join(filters) if filters else "anull"



# --------------------------------------------------------------------------
# Reading video with ffmpeg (more robust than OpenCV's decoder)
# --------------------------------------------------------------------------
@dataclass
class VideoInfo:
    width: int
    height: int
    fps: float
    duration: float | None
    has_audio: bool
    codec: str


def probe(path: str) -> VideoInfo:
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", path],
                       capture_output=True, text=True, errors="replace", **_quiet())
    err = r.stderr
    dur = None
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    vline = next((l for l in err.splitlines() if "Video:" in l), "")
    if not vline:
        raise RuntimeError("The downloaded file has no video.")
    wh = re.search(r"[ ,](\d{2,5})x(\d{2,5})[ ,\]]", vline + " ")
    if not wh:
        raise RuntimeError("Couldn't read the video size.")
    fm = re.search(r"([\d.]+) fps", vline) or re.search(r"([\d.]+)k? tbr", vline)
    fps = float(fm.group(1)) if fm else 30.0
    if not (1 <= fps <= 240):
        fps = 30.0
    codec = (re.search(r"Video: (\w+)", vline) or [None, "?"])[1]
    return VideoInfo(int(wh.group(1)), int(wh.group(2)), fps, dur, "Audio:" in err, codec)


def iter_frames(path: str, info: VideoInfo, width: int | None = None):
    """Yield BGR frames at a constant frame rate, optionally scaled to `width`."""
    w, h = info.width, info.height
    if width and width < w:
        w, h = width // 2 * 2, int(round(info.height * width / info.width)) // 2 * 2
    vf = f"fps={info.fps:g}" + (f",scale={w}:{h}:flags=area" if (w, h) != (info.width, info.height) else "")
    cmd = [ffmpeg_exe(), "-v", "error", "-nostdin", "-i", path, "-map", "0:v:0", "-an", "-sn",
           "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = _track(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   bufsize=w * h * 3 * 2, **_quiet()))
    size = w * h * 3
    try:
        while True:
            check_cancel()
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
        _untrack(proc)
    check_cancel()


# --------------------------------------------------------------------------
# Main pipeline
# --------------------------------------------------------------------------
@dataclass
class ClipSettings:
    lock_in: float = 100
    noise_reduction: float = 20
    normalize_audio: bool = True
    detect_every: int = 3        # run detector every N frames
    crf: int = 20


def render_vertical(src_path: str, out_path: str, settings: ClipSettings,
                    progress=None, log=print) -> str:
    info = probe(src_path)
    fps, src_w, src_h = info.fps, info.width, info.height
    total = int((info.duration or 1) * fps) or 1

    crop_w = int(round(src_h * 9 / 16)) // 2 * 2
    crop_h = src_h
    if crop_w > src_w:  # already narrow; crop height instead
        crop_w = src_w // 2 * 2
        crop_h = int(round(src_w * 16 / 9)) // 2 * 2

    # Pass 1: find the speaker (on a smaller copy of each frame, for speed).
    dur_txt = f", {info.duration:.1f}s" if info.duration else ""
    log(f"Source: {src_w}x{src_h} @ {fps:g}fps, {info.codec}{dur_txt}")
    log("Tracking speaker…")
    det = SpeakerDetector(log=log)
    det_w = min(src_w, 1280)
    back = src_w / det_w
    raw = []
    last = None
    for i, frame in enumerate(iter_frames(src_path, info, det_w)):
        if i % settings.detect_every == 0:
            x = det.detect(frame, None if last is None else last / back)
            if x is not None:
                x *= back
                last = x
            raw.append(np.nan if x is None else x)
        else:
            raw.append(np.nan)
        if progress and i % 15 == 0:
            progress(0.20 + 0.35 * min(i / total, 1.0), "Tracking speaker")
    n = len(raw)
    if n == 0:
        raise RuntimeError("Couldn't read any video frames from the download.")
    found = int(np.sum(~np.isnan(raw)))
    checks = (n + settings.detect_every - 1) // settings.detect_every
    log(f"Speaker found in {found} of {checks} checks.")
    lefts = build_camera_path(np.array(raw, dtype=float), fps, crop_w, src_w, settings.lock_in)

    # Pass 2: crop + encode, piping frames into ffmpeg with the cleaned audio.
    floor = measure_noise_floor(src_path) if settings.noise_reduction > 0 else -50.0
    if settings.noise_reduction > 0:
        log(f"Background noise measured at {floor:.0f} dB.")
    log("Rendering vertical clip…")
    top = (src_h - crop_h) // 2
    cmd = [
        ffmpeg_exe(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT_W}x{OUT_H}", "-r", f"{fps}", "-i", "-",
        "-i", src_path,
        "-map", "0:v:0", "-map", "1:a:0?",
        "-af", audio_filter(settings.noise_reduction, settings.normalize_audio, floor),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(settings.crf), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", "-shortest", out_path,
    ]
    proc = _track(subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, **_quiet()))
    interp = cv2.INTER_AREA if crop_h > OUT_H else cv2.INTER_CUBIC
    written = 0
    try:
        for j, frame in enumerate(iter_frames(src_path, info)):
            if j >= n:
                break
            x0 = int(round(lefts[j]))
            crop = frame[top:top + crop_h, x0:x0 + crop_w]
            proc.stdin.write(cv2.resize(crop, (OUT_W, OUT_H), interpolation=interp).tobytes())
            written += 1
            if progress and j % 15 == 0:
                progress(0.55 + 0.44 * (j / n), "Rendering vertical clip")
    except (BrokenPipeError, OSError):
        pass
    except Cancelled:
        proc.kill()
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        proc.wait()
        _untrack(proc)
    if _cancel.is_set():
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise Cancelled()
    log(f"Rendered {written} frames ({written / fps:.1f}s).")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{err[-1500:]}")
    if progress:
        progress(1.0, "Done")
    return out_path


def make_clip(url: str, start: str, end: str, settings: ClipSettings,
              out_dir: str, max_height: int = 2160, progress=None, log=print) -> str:
    s, e = validate_range(start, end)
    reset_cancel()
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        if progress:
            progress(0.01, "Downloading")
        want = e - s
        src = None
        best_len = -1.0
        attempts = [(max_height, ""), (max_height, "")]
        for h, lbl in ((1080, " at 1080p"), (720, " at 720p")):
            if h < max_height:
                attempts.append((h, lbl))
        warned_network = False
        skip = set()
        for k, (height, label) in enumerate(attempts):
            if k in skip:
                continue
            sub = os.path.join(tmp, f"try{k}")
            os.makedirs(sub)
            issue = DownloadIssue()
            try:
                path = download_section(url, s, e, sub, max_height=height, progress=progress,
                                        log=log, issue=issue)
                got = probe(path).duration or 0.0
                log(f"Downloaded {got:.1f}s of {want:.0f}s{label}.")
            except RuntimeError as ex:
                path, got = None, 0.0
                log(str(ex).splitlines()[0])
                if src is None and all(j in skip or j <= k for j in range(len(attempts))):
                    raise
            if path and got > best_len:
                src, best_len = path, got
            if got >= want * 0.95 - 0.5:
                break
            if issue.network and not warned_network:
                warned_network = True
                log("Your connection to YouTube is slow or dropping. If this keeps happening, "
                    "try another network or turn off any VPN or web filter.")
            if k + 1 < len(attempts):
                # A failed YouTube "challenge" usually means a one-off timeout: retry the same quality
                # first; otherwise step down to a smaller stream.
                nxt_k = k + 1
                if attempts[nxt_k][0] == height and not (issue.challenge_failed or issue.network):
                    skip.add(nxt_k)
                    nxt_k += 1
                if nxt_k < len(attempts):
                    log(f"Didn't get the full section — retrying{attempts[nxt_k][1]}…")
        if best_len < want * 0.95 - 0.5:
            log(f"WARNING: could only get {best_len:.0f}s of the {want:.0f}s section from YouTube.")
        def stamp(t):
            t = int(t)
            return (f"{t // 3600}h" if t >= 3600 else "") + f"{t % 3600 // 60:02d}m{t % 60:02d}s"
        name = f"clip_{stamp(s)}-{stamp(e)}.mp4"
        out = os.path.join(out_dir, name)
        k = 1
        while os.path.exists(out):
            out = os.path.join(out_dir, name.replace(".mp4", f"_{k}.mp4"))
            k += 1
        return render_vertical(src, out, settings, progress=progress, log=log)
