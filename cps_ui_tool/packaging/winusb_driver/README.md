# GRL V_QI WinUSB Driver Package

This folder replaces the manual Zadig step for the default `VID_3985 / PID_0007` USB path used by the app (`GRL Platform Solutions`, `V-QI`, language ID `1033`).

## Files

- `GRL_V_QI_WinUSB.inf`: WinUSB binding for the GRL V_QI USB device
- `install-driver.ps1`: stages and installs the signed driver package with `pnputil`
- `remove-driver.ps1`: removes the installed GRL driver package for this VID/PID

## Before You Install

1. Plug the device into the target laptop.
2. Open Device Manager.
3. Find the USB device and open `Properties > Details > Hardware Ids`.
4. Confirm the correct hardware ID:
   - Single-interface device: `USB\VID_3985&PID_0007`
   - Composite device: `USB\VID_3985&PID_0007&MI_00` or similar
5. If the hardware ID includes `MI_XX`, edit `GRL_V_QI_WinUSB.inf` and replace the ID in both model entries before generating the catalog.

## Build The Signed Package

1. Install the Windows Driver Kit on the build machine.
2. Generate the catalog from this folder:

   ```powershell
   Inf2Cat /driver:$($PWD.Path) /os:10_X64,11_X64,10_ARM64,11_ARM64
   ```

3. Sign `GRL_V_QI_WinUSB.cat`.
   - Internal lab only: sign with your internal code-signing certificate and trust that certificate on the target laptop.
   - Production release: submit the package for Microsoft signing through the Hardware Developer Center.

## Install On A New Laptop

1. Copy this entire folder to the new laptop.
2. Make sure `GRL_V_QI_WinUSB.cat` is present and signed.
3. Open PowerShell as Administrator.
4. Run:

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\install-driver.ps1
   ```

5. Unplug and reconnect the device once if Windows does not switch immediately.

## Verify

1. Open Device Manager.
2. Confirm the device appears under `Universal Serial Bus devices`.
3. Confirm the device uses `WinUSB`.
4. Run the app and verify USB communication succeeds.

## Notes

- This package binds the inbox Microsoft `WinUSB.sys`; it does not install a custom `.sys` driver.
- Unsigned driver packages will not install on standard Windows 10/11 x64 or ARM64 systems.
