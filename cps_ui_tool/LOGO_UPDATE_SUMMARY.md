# Logo Integration Summary

Date: March 31, 2026

This summary reflects the logo assets currently referenced by the checked-in application.

## Current Logo Assets

- Window and taskbar icon: `assets\grl_logo.ico`
- In-app top-bar logo: `assets\grl_logo.png`

## Current App Behavior

- `src\app.py` loads the icon through `resource_path("assets", "grl_logo.ico")`.
- `src\app.py` loads the top-bar logo through `resource_path("assets", "grl_logo.png")`.
- The logo is shown inside a white frame on the right side of the top bar.

## Packaging

- `packaging\GRL_V_QI_UTILITY.spec` bundles both current logo assets under the `assets` directory in the executable build.

## Notes

- Earlier notes in this repo referenced alternate logo filenames and a dark-theme rollout. Those references do not match the assets currently present in `assets\`.
- This file is documentation alignment only. No logo asset files were changed in the March 31, 2026 cleanup.
