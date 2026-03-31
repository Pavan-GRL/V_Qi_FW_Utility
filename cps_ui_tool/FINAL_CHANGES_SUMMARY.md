# GRL V_QI Utility - Current Summary

Date: March 31, 2026

This file reflects the current checked-in state of `d:\V_QI\PoC\python_scripts\cps_ui_tool`.

## Current UI

- The application uses a light theme with blue accents.
- The window icon is loaded from `assets\grl_logo.ico`.
- The top bar logo is loaded from `assets\grl_logo.png`.
- The top-right logo is displayed inside a white badge for contrast.
- The custom checkbox styling uses a blue checked state and white unchecked state.

## Current Layout and Packaging

- Source code lives under `src\`.
- Shared runtime and build paths are centralized in `src\project_paths.py`.
- The PyInstaller spec is `packaging\GRL_V_QI_UTILITY.spec`.
- The packaged assets are:
  - `assets\grl_logo.png`
  - `assets\grl_logo.ico`
  - `include\regset.txt`

## Functional Additions in This Tree

- Additional FSK and capture analysis utilities are included:
  - `src\analyze_pcap.py`
  - `src\check_sequences.py`
  - `src\simple_gap_check.py`
- Logging and report folders are part of the repo layout:
  - `logs\`
  - `reports\`
  - `captures\`

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

## Notes

- Older summary files in this repo previously described a dark/orange branding pass. That does not match the current code or current assets.
- This summary is documentation cleanup only. It does not imply that a new production build was created as part of the March 31, 2026 cleanup.
