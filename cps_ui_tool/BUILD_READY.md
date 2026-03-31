# GRL V_QI Utility - Build Notes

Date: March 31, 2026

This note documents the current build inputs that exist in the repository. It does not claim that a fresh build was executed as part of the March 31, 2026 non-core cleanup.

## Current Build Inputs

- Entry point: `src\app.py`
- PyInstaller spec: `packaging\GRL_V_QI_UTILITY.spec`
- Assets bundled by the spec:
  - `assets\grl_logo.png`
  - `assets\grl_logo.ico`
  - `include\regset.txt`

## Build Commands

Recommended:

```bash
cd d:\V_QI\PoC\python_scripts\cps_ui_tool
.\build.bat
```

Manual:

```bash
cd d:\V_QI\PoC\python_scripts\cps_ui_tool
python -m PyInstaller packaging\GRL_V_QI_UTILITY.spec --clean --noconfirm
```

## Current UI and Branding References

- The checked-in app uses a light background with blue accents.
- The app icon path is `assets\grl_logo.ico`.
- The in-app logo path is `assets\grl_logo.png`.

## Validation Status for This Cleanup

- `src\app.py` styling regression fixed without touching core protocol, FSK, or flash logic.
- Python compile validation can be run with:

```bash
cd d:\V_QI\PoC\python_scripts\cps_ui_tool
python -m py_compile src\app.py src\udp_fsk.py src\ui_plot.py src\logger.py src\protocol.py src\net_client.py src\esp_flasher.py src\project_paths.py
```
