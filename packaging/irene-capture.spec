# PyInstaller spec for irene-capture.
#   pyinstaller packaging/irene-capture.spec --noconfirm
# Windows/Linux: one-file executable. macOS: .app bundle (one-dir inside).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
ICON_PNG = SRC / "irene_capture" / "resources" / "icon.png"
APP = "irene-capture"

hidden = (
    collect_submodules("irene_capture")
    + collect_submodules("pedalboard")
    + ["PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSvg"]
    + ["pedalboard_native", "scipy.signal", "scipy.fft", "scipy.ndimage", "soundfile"]
    + collect_submodules("pyqtgraph", filter=lambda m: not any(
        x in m for x in (".opengl", ".examples", ".jupyter", "canvas", ".flowchart", "multiprocess")))
)
datas = collect_data_files("irene_capture", includes=["resources/*"])
datas += collect_data_files("soundfile")
datas += collect_data_files("_soundfile_data") if sys.platform != "linux" else []
binaries = collect_dynamic_libs("pedalboard_native") + collect_dynamic_libs("soundfile")

# Qt modules the app doesn't use (keeps the build small).
excludes = [
    "tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PyQt6",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D", "PySide6.Qt3DCore",
    "PySide6.Qt3DRender", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtWebSockets", "PySide6.QtNetworkAuth",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtTextToSpeech",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtSpatialAudio",
    "PySide6.QtHttpServer", "PySide6.QtXml",
    "pyqtgraph.opengl", "pyqtgraph.examples", "pyqtgraph.jupyter", "OpenGL",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "win32":
    icon = str(ICON_PNG)  # converted to .ico by PyInstaller (needs Pillow)
elif sys.platform == "darwin":
    icon = str(ICON_PNG)
else:
    icon = None

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP, console=False, icon=icon,
              target_arch=None, codesign_identity=None)
    coll = COLLECT(exe, a.binaries, a.datas, name=APP)
    app = BUNDLE(
        coll,
        name=f"{APP}.app",
        icon=icon,
        bundle_identifier="ch.anschae.irene-capture",
        info_plist={
            "CFBundleName": APP,
            "CFBundleDisplayName": APP,
            "CFBundleShortVersionString": __import__("re").search(
                r'__version__ = "([^"]+)"', (SRC / "irene_capture" / "_version.py").read_text()).group(1),
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,  # follow light/dark mode
            "LSMinimumSystemVersion": "11.0",
        },
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name=APP,
        console=False,
        icon=icon,
        upx=False,
    )
