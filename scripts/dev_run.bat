@echo off
rem 開発用：Python で直接起動（Windows）
cd /d %~dp0\..
call .venv\Scripts\activate.bat
python run_app.py %*
