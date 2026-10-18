@echo off
rem r8brain とノイズシェーピングの DLL を事前ビルドする(MinGW-w64 の g++/gcc が PATH に必要)。
rem 作った DLL は miyamizu\ に置かれ、start.bat での起動と build.bat のビルドの両方で使われます。
setlocal
cd /d "%~dp0"
where g++ >nul 2>&1 || (echo [native] g++ が見つかりません。MSYS2 の UCRT64 の bin を PATH に追加してください & exit /b 1)
pushd miyamizu\native
g++ -O3 -std=c++17 -shared -static -static-libgcc -static-libstdc++ -Ir8brain-free-src r8b_shim.cpp -o ..\libr8bshim.dll || (popd & echo [native] r8brain のビルドに失敗 & exit /b 1)
popd
gcc -O2 -shared miyamizu\nshape.c -o miyamizu\libmiyamizu_nshape.dll || (echo [native] nshape のビルドに失敗 & exit /b 1)
echo [native] 完了: miyamizu\libr8bshim.dll / miyamizu\libmiyamizu_nshape.dll
