# PyInstaller 設定（フォルダー一式を作る onedir 方式）
# ビルド: pyinstaller --noconfirm SamuraiDealerResearch.spec
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("uvicorn") + ["multipart", "python_multipart"]

a = Analysis(
    ["run_app.py"],
    pathex=[],
    datas=[
        ("sdr/templates", "sdr/templates"),
        ("sdr/static", "sdr/static"),
        ("sdr/seed", "sdr/seed"),
    ],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SamuraiDealerResearch",
    console=True,  # 黒いウィンドウ＝起動中の目印。閉じるとアプリも終了する
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="SamuraiDealerResearch")
