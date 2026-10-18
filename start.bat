@echo off
rem Miyamizu 起動スクリプト(Windows): venv 作成 -> 有効化 -> 不足パッケージのみ導入 -> 起動
setlocal
cd /d "%~dp0"
if "%MIYAMIZU_VENV%"=="" (set "VENV=.venv") else (set "VENV=%MIYAMIZU_VENV%")
if "%PYTHON%"=="" (set "PY=py -3") else (set "PY=%PYTHON%")

if not exist "%VENV%\Scripts\python.exe" (
    echo [start] venv を作成します: %VENV%
    %PY% -m venv "%VENV%" || goto :fail
)
call "%VENV%\Scripts\activate.bat"

python -c "import packaging" 2>nul || python -m pip install -q packaging
python tools\check_requirements.py >nul 2>&1
if errorlevel 1 (
    echo [start] 不足パッケージを導入します
    python -m pip install --upgrade pip >nul 2>&1
    python -m pip install -r requirements.txt || goto :fail
)
python -m miyamizu %*
exit /b %errorlevel%

:fail
echo [start] 失敗しました。Python 3.12 以降が入っているか確認してください。
if not defined CI pause
exit /b 1
