"""
Build the standalone Video to Vertical app for the computer this runs on.

    pip install -r requirements.txt
    python build.py

Output (in dist/):
  macOS   -> VideoToVertical-mac-<AppleSilicon|Intel>.zip   (contains "Video to Vertical.app")
  Windows -> VideoToVertical-windows.zip                     (folder with "Video to Vertical.exe")
"""
import os
import platform
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
IS_WIN, IS_MAC = sys.platform.startswith("win"), sys.platform == "darwin"
EXE = ".exe" if IS_WIN else ""
NAME = "Video to Vertical"
YUNET = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
         "face_detection_yunet_2023mar.onnx")


def step(msg):
    print(f"\n==> {msg}", flush=True)


def main():
    for d in ("build", "dist", "bin", "models"):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs("bin")
    os.makedirs("models")

    step("Bundling ffmpeg")
    import imageio_ffmpeg
    ff = os.path.join("bin", "ffmpeg" + EXE)
    shutil.copy(imageio_ffmpeg.get_ffmpeg_exe(), ff)
    os.chmod(ff, 0o755)

    step("Bundling face-detection model")
    import certifi
    import ssl
    ctx = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(YUNET, context=ctx) as r, \
            open(os.path.join("models", "face_detection_yunet_2023mar.onnx"), "wb") as f:
        f.write(r.read())

    step("Running PyInstaller")
    import PyInstaller.__main__
    sep = os.pathsep
    args = [
        "desktop.py", "--noconfirm", "--clean", "--windowed", "--onedir",
        "--name", NAME,
        "--add-binary", f"{ff}{sep}bin",
        "--add-data", f"models{sep}models",
        "--collect-data", "certifi",
        "--exclude-module", "imageio_ffmpeg",
        "--exclude-module", "matplotlib",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PySide6",
    ]
    if IS_MAC:
        args += ["--osx-bundle-identifier", "org.videotovertical.app"]
    PyInstaller.__main__.run(args)

    step("Self-test of the packaged app")
    if IS_MAC:
        exe = os.path.join("dist", f"{NAME}.app", "Contents", "MacOS", NAME)
    else:
        exe = os.path.join("dist", NAME, NAME + EXE)
    report = os.path.abspath(os.path.join("dist", "selftest.txt"))
    rc = subprocess.run([exe, "--selftest", report]).returncode
    print(open(report, encoding="utf-8").read() if os.path.exists(report) else "(no report)")
    if rc != 0:
        sys.exit("Self-test failed.")

    step("Zipping")
    if IS_MAC:
        arch = "AppleSilicon" if platform.machine() == "arm64" else "Intel"
        zip_path = os.path.join("dist", f"VideoToVertical-mac-{arch}.zip")
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        os.path.join("dist", f"{NAME}.app"), zip_path], check=True)
    else:
        zip_path = shutil.make_archive(os.path.join("dist", "VideoToVertical-windows"), "zip",
                                       "dist", NAME)
    print(f"\nBuilt: {zip_path}  ({os.path.getsize(zip_path) / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
