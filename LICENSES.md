# 利用しているライブラリとライセンス

配布版（Windows の実行ファイル）に同梱される Python ライブラリと、その版・ライセンスです（`requirements.txt` で版を固定）。
2026-10-24 に各パッケージのメタデータで確認しました（営業メール機能で追加した分は 2026-10-26 に確認）。

| ライブラリ | 版 | ライセンス |
|---|---|---|
| fastapi | 0.162.0 | MIT |
| starlette | 1.9.0 | BSD-3-Clause |
| uvicorn | 0.54.0 | BSD-3-Clause |
| jinja2 | 3.1.6 | BSD-3-Clause |
| markupsafe | 3.0.4 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| httpx | 0.28.1 | BSD-3-Clause |
| httpcore | 1.0.9 | BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| anyio | 4.15.1 | MIT |
| idna | 3.20 | BSD-3-Clause |
| certifi | 2026.10.23 | MPL-2.0 |
| beautifulsoup4 | 4.15.0 | MIT |
| soupsieve | 3.0.4 | MIT |
| pydantic / pydantic-core | 2.14.1 / 2.50.1 | MIT |
| annotated-types / annotated-doc / typing-inspection | 0.8.0 / 0.0.5 / 0.4.4 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| click | 8.5.0 | BSD-3-Clause |
| opentelemetry-api（fastapi の依存） | 1.45.1 | Apache-2.0 |
| pywebview（専用ウィンドウ。Windows のみ） | 6.2.1 | BSD-3-Clause |
| pythonnet / clr-loader（pywebview が使う .NET 連携） | 3.2.1 / 0.3.1 | MIT / MIT |
| cffi / pycparser | 2.1.1 / 3.12 | MIT-0 / BSD-3-Clause |
| bottle / proxy-tools（pywebview の依存） | 0.13.4 / 0.1.0 | MIT / MIT |
| anthropic（Claude API の公式 SDK。AI を使う場合のみ通信） | 2.3.0 | Apache-2.0 |
| httpx2 / httpcore2（anthropic の依存） | 2.16.0 / 2.16.0 | BSD-3-Clause |
| jiter / docstring-parser / sniffio / truststore（anthropic の依存） | 0.18.0 / 0.18.0 / 1.3.1 / 0.10.5 | MIT / MIT / MIT OR Apache-2.0 / MIT |
| keyring（OS の資格情報保管の利用） | 25.7.0 | MIT |
| jaraco.classes / jaraco.context / jaraco.functools / more-itertools（keyring の依存） | 3.4.0 / 6.1.2 / 4.6.0 / 11.2.1 | MIT |
| pywin32-ctypes（keyring の Windows 用部品。Windows のみ） | 0.2.3 | BSD-3-Clause（PyPI の記載） |
| tzdata（時刻帯データ。Windows には IANA の時刻帯データがないため） | 2026.5 | Apache-2.0 |
| Microsoft Edge WebView2 の読み込み部品（pywebview に同梱の WebView2Loader.dll 等） | — | Microsoft の WebView2 SDK ライセンス（再配布可） |
| Python 本体（実行ファイルに同梱） | 3.12 | PSF License |

ビルドにだけ使うもの（配布物には含まれません）：Inno Setup 6（インストーラー作成。Inno Setup License：商用利用を含め無償）、PyInstaller 6.22.3（GPLv2 以降・ブートローダー例外付き。例外により生成した実行ファイルの配布条件は制限されません）、pytest 9.1.1（MIT）。

候補発見で使うデータ：OpenStreetMap © OpenStreetMap contributors（ODbL 1.0）。

## iPhone 版（`mobile/`）

iPhone 版は外部のライブラリを画面にも Worker にも同梱していません（画面は素の JavaScript、Worker は Cloudflare Workers の標準機能のみ）。
開発・試験・公開にだけ使うもの（`mobile/package.json` で版を固定。2026-10-24 確認）：

| ツール | 版 | ライセンス |
|---|---|---|
| wrangler | 4.149.0 | MIT OR Apache-2.0 |
| miniflare | 4.20260730.0 | MIT |
| @cloudflare/workers-types | 5.20261009.1 | MIT OR Apache-2.0 |
| typescript | 5.9.3 | Apache-2.0 |
| vitest | 4.1.12 | MIT |
