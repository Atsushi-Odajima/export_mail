@echo off
rem Windows で配布版（フォルダー一式）を作る手順。Python 3.12 64bit を導入済みの PC で実行。
cd /d %~dp0\..
py -3.12 -m venv .venv || exit /b 1
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt -r requirements-dev.txt || exit /b 1
python -m pytest -q -p no:cacheprovider tests || exit /b 1
pyinstaller --noconfirm SamuraiDealerResearch.spec || exit /b 1
copy README.md dist\SamuraiDealerResearch\ >nul
copy FUTURE_EMAIL_PLAN.md dist\SamuraiDealerResearch\ >nul
copy LICENSES.md dist\SamuraiDealerResearch\ >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path dist\SamuraiDealerResearch -DestinationPath SamuraiDealerResearch-windows.zip"
rem インストーラー（Inno Setup 6 を導入済みの場合だけ作成）
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
  for /f %%v in ('python -c "import sdr; print(sdr.__version__)"') do %ISCC% /DAppVersion=%%v installer\SamuraiDealerResearch.iss || exit /b 1
  echo 完成: dist-installer\SamuraiDealerResearch-Setup-*.exe
) else (
  echo Inno Setup 6 が見つからないため、インストーラーは作りませんでした（https://jrsoftware.org/isdl.php）
)
echo 完成: SamuraiDealerResearch-windows.zip
