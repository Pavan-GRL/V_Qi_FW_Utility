[CmdletBinding()]
param(
    [string]$InfPath = (Join-Path $PSScriptRoot "GRL_V_QI_WinUSB.inf")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-UsbHardwareIds {
    param(
        [string]$Pattern
    )

    $devices = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -like $Pattern }

    foreach ($device in $devices) {
        $property = Get-PnpDeviceProperty -InstanceId $device.InstanceId `
            -KeyName "DEVPKEY_Device_HardwareIds" -ErrorAction SilentlyContinue

        if ($null -ne $property) {
            foreach ($id in $property.Data) {
                $id
            }
        }
    }
}

if (-not (Test-IsAdministrator)) {
    throw "Run this script from an elevated PowerShell window."
}

if (-not (Test-Path -LiteralPath $InfPath)) {
    throw "INF file not found: $InfPath"
}

$catPath = [System.IO.Path]::ChangeExtension($InfPath, ".cat")
if (-not (Test-Path -LiteralPath $catPath)) {
    throw "Catalog file not found: $catPath. Generate and sign the catalog before installation."
}

$hardwareIds = @(Get-UsbHardwareIds -Pattern "USB\\VID_227F&PID_0006*") | Sort-Object -Unique
if ($hardwareIds.Count -gt 0) {
    Write-Host "Detected hardware IDs for connected V_QI hardware:" -ForegroundColor Cyan
    $hardwareIds | ForEach-Object { Write-Host "  $_" }

    if ($hardwareIds | Where-Object { $_ -match "&MI_[0-9A-F]{2}$" }) {
        Write-Warning "Composite interface IDs were detected. Make sure the INF Models section matches the exact MI_XX hardware ID before install."
    }
}
else {
    Write-Warning "No connected USB device matching VID_227F/PID_0006 was detected. The driver will still be staged in the driver store."
}

$pnputil = Join-Path $env:windir "System32\pnputil.exe"
& $pnputil /add-driver $InfPath /install
if ($LASTEXITCODE -ne 0) {
    throw "pnputil failed with exit code $LASTEXITCODE"
}

& $pnputil /scan-devices | Out-Null

Write-Host ""
Write-Host "Driver installation completed." -ForegroundColor Green
Write-Host "If the device was already connected, unplug and reconnect it once."
