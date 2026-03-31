# ========================================
# GRL V_QI Utility - Build Script (PowerShell)
# ========================================

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Building GRL_V_QI_UTILITY.exe" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Clean previous builds
Write-Host "[1/3] Cleaning previous builds..." -ForegroundColor Yellow
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }

# Run PyInstaller
Write-Host "[2/3] Running PyInstaller..." -ForegroundColor Yellow
pyinstaller packaging\GRL_V_QI_UTILITY.spec --clean --noconfirm

# Check if build was successful
if (Test-Path "dist\GRL_V_QI_UTILITY.exe") {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Green
    Write-Host "[3/3] Build successful!" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Executable location: " -NoNewline
    Write-Host "dist\GRL_V_QI_UTILITY.exe" -ForegroundColor White
    Write-Host ""

    # Get file size
    $fileSize = (Get-Item "dist\GRL_V_QI_UTILITY.exe").Length / 1MB
    Write-Host "File size: " -NoNewline
    Write-Host ("{0:N2} MB" -f $fileSize) -ForegroundColor White
    Write-Host ""
    Write-Host "You can now run: " -NoNewline
    Write-Host "dist\GRL_V_QI_UTILITY.exe" -ForegroundColor Cyan
    Write-Host ""
} else {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Red
    Write-Host "[3/3] Build FAILED!" -ForegroundColor Red
    Write-Host "========================================" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please check the error messages above." -ForegroundColor Red
    Write-Host ""
    exit 1
}

pause
