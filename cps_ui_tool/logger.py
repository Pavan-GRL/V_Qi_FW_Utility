from __future__ import annotations
import csv
import os
import threading
from typing import List

LOG_DIR = "logs"

SESSION_LOG_FILE = os.path.join(LOG_DIR, "session_full.log")
CPS_LOG_FILE = os.path.join(LOG_DIR, "cps_only.log")
CPS_CSV_FILE = os.path.join(LOG_DIR, "cps_data.csv")

CSV_HEADER = [
    "host_ts",
    "pwr_ts_raw_hex",
    "pwr_ts_le",
    "cps_ts_raw_hex",
    "cps_ts_le",
    "rect_v",
    "rect_i",
    "pwr_len",
    "queue_read_idx",
    "queue_write_idx",
    "payload_valid",
    "payload_len",
]

def ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)

def append_text(lock: threading.Lock, path: str, text: str):
    ensure_log_dir()
    with lock:
        with open(path, "a", encoding="utf-8", newline="") as f:
            f.write(text)

def csv_init_if_needed(lock: threading.Lock, path: str):
    ensure_log_dir()
    with lock:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(CSV_HEADER)

def csv_append_row(lock: threading.Lock, path: str, row: List):
    ensure_log_dir()
    with lock:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(row)
