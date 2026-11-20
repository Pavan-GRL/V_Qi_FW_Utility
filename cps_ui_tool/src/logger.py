from __future__ import annotations
import csv
import os
import re
import threading
from datetime import datetime
from typing import List

from project_paths import LOGS_DIR, ensure_dir
from protocol import (
    format_ts,
    hex_dump_multiline,
    hex_dump_spaces,
    ts_bytes_to_le,
)

LOG_DIR = os.fspath(LOGS_DIR)

SESSION_LOG_FILE = os.fspath(LOGS_DIR / "session_full.log")
CPS_LOG_FILE = os.fspath(LOGS_DIR / "cps_only.log")
CPS_CSV_FILE = os.fspath(LOGS_DIR / "cps_data.csv")
CPS_ALL_READS_CSV_FILE = os.fspath(LOGS_DIR / "cps_all_reads.csv")

CSV_HEADER = [
    "Timestamp",
    "Index",
    "Sequence",
    "PwrTimeS",
    "PwrLength",
    "RectVoltage",
    "RectCurrent",
    "WriteIndex",
    "ReadIndex",
    "ValidPacket",
    "QiTimestamp",
    "QiLength",
    "QiPacket",
    "PacketStartTimestamp",
    "PacketEndTimestamp",
    "PacketReserved",
]

_EXT_CTRL_ERROR_RE = re.compile(r"Extended Control Error:\s*\{([^}]*)\}")


def _decode_ascii_fields(payload: bytes) -> dict[str, str]:
    ascii_text = payload.decode("ascii", errors="replace").rstrip()
    lines = [line.strip() for line in ascii_text.splitlines() if line.strip()]

    message_type = ""
    message_hex = ""
    extended_control_error = ""
    ascii_vrect = ""
    ascii_vout = ""
    ascii_irect = ""
    ascii_temperature = ""

    if lines and ":" in lines[0]:
        message_type, _, message_hex = lines[0].partition(":")
        message_type = message_type.strip()
        message_hex = message_hex.strip()

    for line in lines:
        match = _EXT_CTRL_ERROR_RE.search(line)
        if match:
            extended_control_error = match.group(1).strip()

        if "=" not in line:
            continue

        for part in line.split(","):
            key, sep, value = part.partition("=")
            if not sep:
                continue
            key = key.strip().lower()
            value = value.strip()
            if key == "vrect":
                ascii_vrect = value
            elif key == "vout":
                ascii_vout = value
            elif key == "irect":
                ascii_irect = value
            elif key == "temperature":
                ascii_temperature = value

    return {
        "ascii_text": ascii_text,
        "ascii_line_1": lines[0] if len(lines) > 0 else "",
        "ascii_line_2": lines[1] if len(lines) > 1 else "",
        "ascii_line_3": lines[2] if len(lines) > 2 else "",
        "message_type": message_type,
        "message_hex": message_hex,
        "extended_control_error": extended_control_error,
        "ascii_vrect": ascii_vrect,
        "ascii_vout": ascii_vout,
        "ascii_irect": ascii_irect,
        "ascii_temperature": ascii_temperature,
    }


def _csv_timestamp(host_ts: str) -> str:
    try:
        dt = datetime.strptime(host_ts, "%Y-%m-%d %H:%M:%S.%f")
        return f"{dt:%H:%M:%S}.{dt.microsecond // 100000}"
    except ValueError:
        return host_ts


def _scaled_by_1000(value: object) -> str:
    if value is None:
        return ""
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return str(value)
    if raw == 0:
        return "0"
    return f"{raw / 1000.0:.3f}"


def _qi_packet_text(payload: bytes) -> str:
    return payload.decode("ascii", errors="replace").strip()


def _is_valid_qi_packet(text: str, payload_valid: int) -> str:
    if payload_valid != 1 or not text:
        return "FALSE"
    upper = text.lstrip(" ?").upper()
    if upper.startswith(("ASK:", "FSK:")):
        return "FALSE"
    return "TRUE"


def build_cps_csv_row(host_ts: str, info: dict) -> List[str]:
    pwr_ts_le = str(ts_bytes_to_le(info["pwr_ts_bytes"]))
    payload_valid = int(info.get("payload_valid", 0))
    qi_packet = _qi_packet_text(info.get("payload", b"")) if payload_valid == 1 else ""
    cps_ts_bytes = info.get("cps_ts_bytes")
    qi_ts = str(ts_bytes_to_le(cps_ts_bytes)) if cps_ts_bytes is not None else ""
    qi_len = str(info.get("_frame_pay_len", info.get("pay_len", 0)))

    return [
        _csv_timestamp(host_ts),
        str(info.get("_csv_frame_index", "")),
        str(info.get("_stream_line_seq", "")),
        pwr_ts_le,
        str(info.get("pwr_len", "")),
        _scaled_by_1000(info.get("rect_v")),
        _scaled_by_1000(info.get("rect_i")),
        f"0x{int(info.get('write_idx', 0)):X}",
        f"0x{int(info.get('read_idx', 0)):X}",
        _is_valid_qi_packet(qi_packet, payload_valid),
        qi_ts,
        qi_len,
        qi_packet,
        str(info.get("packet_start_ts", "")),
        str(info.get("packet_end_ts", "")),
        hex_dump_spaces(info["packet_reserved_bytes"])
        if info.get("packet_reserved_bytes") is not None else "",
    ]


def build_cps_log_entry(host_ts: str, info: dict) -> str:
    cps_entry = (
        "----- CPS POLL -----\n"
        f"host_ts       : {host_ts}\n"
        f"pwr_ts        : {format_ts(info['pwr_ts_bytes'])}\n"
        f"pwr_len       : {info['pwr_len']}\n"
        f"pwr_raw       : {hex_dump_spaces(info['pwr_bytes'])}\n"
    )
    if info["rect_v"] is not None and info["rect_i"] is not None:
        cps_entry += (
            f"rect_v        : {info['rect_v']}\n"
            f"rect_i        : {info['rect_i']}\n"
        )
    cps_entry += (
        f"queue_status  : 0x{((info['read_idx'] << 4) | info['write_idx']):02X} "
        f"(read_idx={info['read_idx']}, write_idx={info['write_idx']})\n"
        f"payload_valid : {info['payload_valid']}\n"
    )
    if info["payload_valid"] == 0:
        cps_entry += "info          : NO VALID PAYLOAD\n"
        cps_entry += "cps_raw_only  :\n"
        cps_entry += f"{hex_dump_multiline(info['cps_raw_only'])}\n\n"
    else:
        ascii_text = info["payload"].decode("ascii", errors="replace")
        cps_entry += (
            f"cps_ts        : {format_ts(info['cps_ts_bytes'])}\n"
            f"pay_len       : {info['pay_len']}\n"
            "ascii         :\n"
            f"{ascii_text}\n"
            "cps_raw_only  :\n"
            f"{hex_dump_multiline(info['cps_raw_only'])}\n\n"
        )
    return cps_entry

def ensure_log_dir():
    ensure_dir(LOGS_DIR)

def append_text(lock: threading.Lock, path: str, text: str):
    ensure_log_dir()
    with lock:
        with open(path, "a", encoding="utf-8", newline="") as f:
            f.write(text)

def csv_init_if_needed(lock: threading.Lock, path: str):
    ensure_log_dir()
    with lock:
        needs_reset = not os.path.exists(path) or os.path.getsize(path) == 0
        if not needs_reset:
            try:
                with open(path, "r", newline="", encoding="utf-8") as f:
                    first_row = next(csv.reader(f), [])
                needs_reset = first_row != CSV_HEADER
            except OSError:
                needs_reset = True

        if needs_reset:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(CSV_HEADER)

def csv_append_row(lock: threading.Lock, path: str, row: List):
    ensure_log_dir()
    with lock:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(row)
