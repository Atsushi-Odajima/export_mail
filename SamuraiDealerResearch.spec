# PyInstaller 設定（フォルダー一式を作る onedir 方式）
# ビルド: pyinstaller --noconfirm SamuraiDealerResearch.spec
# 1 つのフォルダーに 2 つの実行ファイルを作る：
#   SamuraiDealerResearch.exe      専用ウィンドウ版（コンソールなし。通常はこちら）
#   SamuraiDealerResearch-cli.exe  コマンド用（共有モード・バックアップ等。黒いウィンドウでメッセージを表示）
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

hiddenimports = collect_submodules("uvicorn") + ["multipart", "python_multipart"]
# AI（Anthropic 公式 SDK）・資格情報保管（keyring。保管先はエントリーポイントで探すため配布情報も同梱）・時刻帯データ
hiddenimports += collect_submodules("anthropic") + collect_submodules("httpx2") + collect_submodules("keyring.backends")
hiddenimports += collect_submodules("tzdata")
datas_extra = copy_metadata("keyring") + copy_metadata("anthropic") + collect_data_files("tzdata") + collect_data_files("certifi")
if sys.platform == "win32":
    # 専用ウィンドウ（pywebview の Edge WebView2 表示）。フック（pyinstaller-hooks-contrib）が WebView2 の DLL も同梱する
    hiddenimports += ["webview", "webview.platforms.winforms", "webview.platforms.edgechromium", "clr"]
    hiddenimports += collect_submodules("win32ctypes")  # Windows 資格情報マネージャー

a = Analysis(
    ["run_app.py"],
    pathex=[],
    datas=[
        ("sdr/templates", "sdr/templates"),
        ("sdr/static", "sdr/static"),
        ("sdr/seed", "sdr/seed"),
    ] + datas_extra,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PyInstaller"],
    noarchive=False,
)
pyz = PYZ(a.pure)
gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SamuraiDealerResearch",
    console=False,
    icon="installer/app.ico",
    upx=False,
)
cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SamuraiDealerResearch-cli",
    console=True,
    icon="installer/app.ico",
    upx=False,
)
coll = COLLECT(gui, cli, a.binaries, a.datas, strip=False, upx=False, name="SamuraiDealerResearch")
