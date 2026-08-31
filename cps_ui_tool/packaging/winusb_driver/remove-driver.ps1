[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if (-not (Test-IsAdministrator)) {
    throw "Run this script from an elevated PowerShell window."
}

$pnputil = Join-Path $env:windir "System32\pnputil.exe"
$presentDrivers = Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
    Where-Object {
        $_.DeviceID -like "USB\\VID_3985&PID_0007*" -and
        $_.DriverProviderName -eq "GRL Platform Solutions"
    } |
    Select-Object -ExpandProperty InfName -Unique

$stagedDrivers = Get-WindowsDriver -Online -ErrorAction SilentlyContinue |
    Where-Object {
        $_.OriginalFileName -ieq "GRL_V_QI_WinUSB.inf" -or
        $_.ProviderName -eq "GRL Platform Solutions"
    } |
    Select-Object -ExpandProperty Driver -Unique

$drivers = @($presentDrivers + $stagedDrivers) | Where-Object { $_ } | Sort-Object -Unique

if (-not $drivers) {
    Write-Host "No installed GRL WinUSB driver instances were found for VID_3985/PID_0007."
    exit 0
}

foreach ($driver in $drivers) {
    Write-Host "Removing $driver" -ForegroundColor Yellow
    & $pnputil /delete-driver $driver /uninstall /force
    if ($LASTEXITCODE -ne 0) {
        throw "pnputil failed while removing $driver (exit code $LASTEXITCODE)"
    }
}

Write-Host "Driver package removal completed." -ForegroundColor Green
