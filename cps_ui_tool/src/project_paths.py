from __future__ import annotations

import sys
from pathlib import Path


def _project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = _project_root()
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))

SRC_DIR = PROJECT_ROOT / "src"
DOCS_DIR = PROJECT_ROOT / "docs"
ASSETS_DIR = PROJECT_ROOT / "assets"
INCLUDE_DIR = PROJECT_ROOT / "include"
LOGS_DIR = PROJECT_ROOT / "logs"
REPORTS_DIR = PROJECT_ROOT / "reports"
CAPTURES_DIR = PROJECT_ROOT / "captures"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


def resource_path(*parts: str) -> Path:
    return RESOURCE_ROOT.joinpath(*parts)


def first_capture_path() -> Path:
    for pattern in ("*.pcapng", "*.pcap"):
        matches = sorted(CAPTURES_DIR.rglob(pattern))
        if matches:
            return matches[0]
    return CAPTURES_DIR / "attachments" / "cap.pcapng"
