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
    """Don't flash console windows on Windows when running helpers."""
    if IS_WIN:
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


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
def _fmt_time(sec: float) -> str:
    sec = max(0.0, sec)
    return f"{int(sec // 3600)}:{int(sec % 3600 // 60):02d}:{sec % 60:06.3f}"


def download_section(url: str, start: float, end: float, workdir: str,
                     max_height: int = 2160, progress=None, log=print) -> str:
    ytdlp, deno = ensure_tools(progress=progress, log=log)
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(deno) + os.pathsep + env.get("PATH", "")
    out_tmpl = os.path.join(workdir, "source.%(ext)s")
    cmd = [
        ytdlp, "--no-playlist", "--newline", "--no-colors", "--no-mtime",
        "-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "--merge-output-format", "mp4",
        "--download-sections", f"*{_fmt_time(start)}-{_fmt_time(end)}",
        "--force-keyframes-at-cuts",
        "--ffmpeg-location", ffmpeg_exe(),
        "--js-runtimes", f"deno:{deno}",
        "-o", out_tmpl, url,
    ]
    log("Downloading section from YouTube…")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env, **_quiet())
    tail = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        tail = (tail + [line])[-15:]
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m and progress:
            progress(0.02 + 0.18 * float(m.group(1)) / 100, "Downloading from YouTube")
        elif "ERROR" in line or "WARNING" in line:
            log(line)
    proc.wait()
    files = [f for f in os.listdir(workdir) if f.startswith("source.") and not f.endswith(".part")]
    if proc.returncode != 0 or not files:
        hint = "\n".join(tail[-5:])
        raise RuntimeError("YouTube download failed. Check the link (is the video public or unlisted?).\n" + hint)
    files.sort(key=lambda f: (not f.endswith(".mp4"), f))
    return os.path.join(workdir, files[0])


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
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300, **_quiet()).stdout
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
# Main pipeline
# --------------------------------------------------------------------------
@dataclass
class ClipSettings:
    lock_in: float = 50
    noise_reduction: float = 40
    normalize_audio: bool = True
    detect_every: int = 3        # run detector every N frames
    crf: int = 20


def render_vertical(src_path: str, out_path: str, settings: ClipSettings,
                    progress=None, log=print) -> str:
    cap = cv2.VideoCapture(src_path)
    if not cap.isOpened():
        raise RuntimeError("Couldn't open the downloaded video.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1

    crop_w = int(round(src_h * 9 / 16)) // 2 * 2
    crop_h = src_h
    if crop_w > src_w:  # already narrow; crop height instead
        crop_w = src_w // 2 * 2
        crop_h = int(round(src_w * 16 / 9)) // 2 * 2

    # Pass 1: find the speaker.
    log(f"Tracking speaker ({src_w}x{src_h} @ {fps:.2f}fps)…")
    det = SpeakerDetector(log=log)
    raw = []
    last = None
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % settings.detect_every == 0:
            x = det.detect(frame, last)
            if x is not None:
                last = x
            raw.append(np.nan if x is None else x)
        else:
            raw.append(np.nan)
        i += 1
        if progress and i % 15 == 0:
            progress(0.20 + 0.35 * min(i / total, 1.0), "Tracking speaker")
    cap.release()
    n = len(raw)
    if n == 0:
        raise RuntimeError("The video has no frames.")
    found = int(np.sum(~np.isnan(raw)))
    log(f"Speaker found in {found} of {(n + settings.detect_every - 1) // settings.detect_every} checks.")
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
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, **_quiet())
    cap = cv2.VideoCapture(src_path)
    interp = cv2.INTER_AREA if crop_h > OUT_H else cv2.INTER_CUBIC
    try:
        for j in range(n):
            ok, frame = cap.read()
            if not ok:
                break
            x0 = int(round(lefts[j]))
            crop = frame[top:top + crop_h, x0:x0 + crop_w]
            proc.stdin.write(cv2.resize(crop, (OUT_W, OUT_H), interpolation=interp).tobytes())
            if progress and j % 15 == 0:
                progress(0.55 + 0.44 * (j / n), "Rendering vertical clip")
    except BrokenPipeError:
        pass
    finally:
        cap.release()
        if proc.stdin:
            proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{err[-1500:]}")
    if progress:
        progress(1.0, "Done")
    return out_path


def make_clip(url: str, start: str, end: str, settings: ClipSettings,
              out_dir: str, max_height: int = 2160, progress=None, log=print) -> str:
    s, e = validate_range(start, end)
    os.makedirs(out_dir, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        if progress:
            progress(0.01, "Downloading")
        src = download_section(url, s, e, tmp, max_height=max_height, progress=progress, log=log)
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
