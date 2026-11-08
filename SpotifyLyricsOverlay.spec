# PyInstaller build recipe -> dist\SpotifyLyricsOverlay\SpotifyLyricsOverlay.exe
# Build with:  pyinstaller SpotifyLyricsOverlay.spec --noconfirm --clean
# "onedir" (a folder) is used instead of a single .exe because it starts faster
# and is far less likely to be falsely flagged by antivirus software.

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

hidden = [
    "winrt.runtime",
    "winrt.system",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.media.control",
    "winrt.windows.storage.streams",
]
binaries = []
datas = [("assets/icon.png", "assets"), ("assets/icon.ico", "assets"), ("assets/demo_cover.png", "assets")]
try:
    datas += collect_data_files("pykakasi")      # romaji dictionaries
    hidden += collect_submodules("pykakasi")
except Exception as e:
    print("pykakasi collection skipped:", e)
for pkg in ("faster_whisper", "soundcard", "ctranslate2", "onnxruntime", "sherpa_onnx"):
    try:                                         # AI sync: speech recognition + sound capture
        datas += collect_data_files(pkg)
        binaries += collect_dynamic_libs(pkg)
        hidden += collect_submodules(pkg) if pkg in ("faster_whisper", "soundcard", "sherpa_onnx") else []
    except Exception as e:
        print(pkg, "collection skipped:", e)
try:
    hidden += collect_submodules("winrt")
    binaries += collect_dynamic_libs("winrt")
except Exception as e:  # e.g. building on a non-Windows machine
    print("winrt collection skipped:", e)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "torch", "tensorflow",
              "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml",
              "PySide6.QtQuick", "PySide6.QtMultimedia", "PySide6.Qt3DCore", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtSql", "PySide6.QtTest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SpotifyLyricsOverlay",
    icon="assets/icon.ico",
    console=False,          # no black terminal window
    upx=False,
    version="version_info.txt",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="SpotifyLyricsOverlay")
