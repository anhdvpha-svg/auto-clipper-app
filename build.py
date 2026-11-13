"""Freeze the app with PyInstaller into dist/AutoClipper/ (a folder; the installer packs it).

  python build.py
"""
import os, sys

import PyInstaller.__main__

ROOT = os.path.dirname(os.path.abspath(__file__))
SEP = os.pathsep  # ";" on Windows, ":" elsewhere

args = [
    os.path.join(ROOT, "run_app.py"),
    "--name", "AutoClipper",
    "--noconfirm", "--clean",
    "--windowed",
    "--onedir",
    "--icon", os.path.join(ROOT, "assets", "icon.ico"),
    "--add-data", f"{os.path.join(ROOT, 'assets')}{SEP}assets",
    "--collect-all", "customtkinter",
    "--collect-all", "faster_whisper",
    "--collect-all", "ctranslate2",
    "--collect-binaries", "onnxruntime",
    "--collect-data", "cv2",
    "--collect-submodules", "app",
    "--collect-all", "webview",            # in-app login window (Edge WebView2)
    "--collect-all", "sounddevice",        # sound for the in-app video player

    "--hidden-import", "PIL._tkinter_finder",
    "--exclude-module", "matplotlib",
    "--exclude-module", "pytest",
    "--distpath", os.path.join(ROOT, "dist"),
    "--workpath", os.path.join(ROOT, "build", "pyi"),
    "--specpath", os.path.join(ROOT, "build"),
]
if sys.platform == "win32":
    args += ["--collect-all", "pythonnet", "--collect-all", "clr_loader", "--hidden-import", "clr",
             "--collect-all", "_sounddevice_data"]
if sys.platform == "darwin":
    args += ["--osx-bundle-identifier", "app.autoclipper"]

PyInstaller.__main__.run(args)
