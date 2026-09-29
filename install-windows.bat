@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo 没装 Python，先去 https://www.python.org/downloads/ 装一个，安装时勾选 Add to PATH & pause & exit /b 1)
python -m venv .venv && .venv\Scripts\pip install -q -r requirements.txt && .venv\Scripts\python guard.py init
echo.
echo 装好了。以后在这个文件夹里用 oc embed 图.png --to "发给谁"
pause
