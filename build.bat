@echo off
rem Miyamizu を PyInstaller でビルド(Windows): venv 作成 -> 有効化 -> 不足パッケージのみ導入 -> ビルド
rem 出力: dist\Miyamizu\Miyamizu.exe (フォルダ配布。icon.png を exe の隣に置くとアイコンを差し替え可)
setlocal
cd /d "%~dp0"
if "%MIYAMIZU_VENV%"=="" (set "VENV=.venv") else (set "VENV=%MIYAMIZU_VENV%")
if "%PYTHON%"=="" (set "PY=py -3") else (set "PY=%PYTHON%")

if not exist "%VENV%\Scripts\python.exe" (
    echo [build] venv を作成します: %VENV%
    %PY% -m venv "%VENV%" || goto :fail
)
call "%VENV%\Scripts\activate.bat"

python -c "import packaging" 2>nul || python -m pip install -q packaging
python tools\check_requirements.py --build >nul 2>&1
if errorlevel 1 (
    echo [build] 不足パッケージを導入します
    python -m pip install --upgrade pip >nul 2>&1
    python -m pip install -r requirements.txt pyinstaller || goto :fail
)

rem リサンプラ(r8brain)とノイズシェーピングの DLL。MinGW-w64(g++/gcc)が PATH にあれば事前ビルド、無ければ同梱せず機能は無効(soxr / TPDF ディザで動作)
where g++ >nul 2>&1
if not errorlevel 1 (
    pushd miyamizu\native
    g++ -O3 -std=c++17 -shared -static -static-libgcc -static-libstdc++ -Ir8brain-free-src r8b_shim.cpp -o ..\libr8bshim.dll && echo [build] r8brain DLL をビルドしました || echo [build] r8brain DLL のビルドに失敗
    popd
) else (
    echo [build] g++ が無いので r8brain は同梱しません(エンジンで選べなくなります)
)
where gcc >nul 2>&1
if not errorlevel 1 (
    gcc -O2 -shared miyamizu\nshape.c -o miyamizu\libmiyamizu_nshape.dll && echo [build] nshape DLL をビルドしました || echo [build] nshape DLL のビルドに失敗
) else (
    echo [build] gcc が無いので nshape は同梱しません(ノイズシェーピングは使えません)
)

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
pyinstaller --noconfirm --clean miyamizu.spec || goto :fail
copy /y icon.png dist\Miyamizu\icon.png >nul 2>&1
echo [build] 完了: dist\Miyamizu\Miyamizu.exe
exit /b 0

:fail
echo [build] 失敗しました。
pause
exit /b 1
