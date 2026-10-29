# 仅由 build_release.py 调用；资源白名单与审核在该脚本中执行。
from pathlib import Path
from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

root = Path(SPECPATH).parent
hidden = collect_submodules("bleak.backends.winrt") + [
    "sherpa_onnx", "a1_auth_test", "a1_download", "dtyj_to_ogg",
    "extract_preferences",
]
analysis = Analysis(
    [str(root / "app" / "main.py")],
    pathex=[str(root), str(root / "app"),
            str(root / "app" / "vendor")],
    binaries=collect_dynamic_libs("sherpa_onnx"),
    datas=[(str(root / "app" / "web" / "dist"), "web")],
    hiddenimports=hidden,
    excludes=["torch", "torchaudio", "nemo", "transformers", "buzz", "frida",
              "frida_tools", "PySide6", "tkinter", "matplotlib", "pandas",
              "numpy.f2py", "charset_normalizer", "vosk"],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True, name="A1Recorder",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
)
collect = COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False,
                  name="A1Recorder")
