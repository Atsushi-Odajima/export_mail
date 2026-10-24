# 利用しているライブラリとライセンス

配布版（Windows の実行ファイル）に同梱される Python ライブラリと、その版・ライセンスです（`requirements.txt` で版を固定）。
2026-10-24 に各パッケージのメタデータで確認しました。

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
| Python 本体（実行ファイルに同梱） | 3.12 | PSF License |

ビルドにだけ使うもの（配布物には含まれません）：PyInstaller 6.22.3（GPLv2 以降・ブートローダー例外付き。例外により生成した実行ファイルの配布条件は制限されません）、pytest 9.1.1（MIT）。

候補発見で使うデータ：OpenStreetMap © OpenStreetMap contributors（ODbL 1.0）。
