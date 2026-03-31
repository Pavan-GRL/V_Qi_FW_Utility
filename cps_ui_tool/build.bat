@echo off
REM ========================================
REM GRL V_QI Utility - Build Script
REM ========================================

echo.
echo ========================================
echo Building GRL_V_QI_UTILITY.exe
echo ========================================
echo.

REM Clean previous builds
echo [1/3] Cleaning previous builds...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"

REM Run PyInstaller
echo [2/3] Running PyInstaller...
python -m PyInstaller packaging\GRL_V_QI_UTILITY.spec --clean --noconfirm

REM Check if build was successful
if exist "dist\GRL_V_QI_UTILITY.exe" (
    echo.
    echo ========================================
    echo [3/3] Build successful!
    echo ========================================
    echo.
    echo Executable location: dist\GRL_V_QI_UTILITY.exe
    echo.
    echo You can now run: dist\GRL_V_QI_UTILITY.exe
    echo.
) else (
    echo.
    echo ========================================
    echo [3/3] Build FAILED!
    echo ========================================
    echo.
    echo Please check the error messages above.
    echo.
    exit /b 1
)

pause
