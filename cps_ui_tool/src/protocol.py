from __future__ import annotations

def hex_dump_spaces(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)

def hex_dump_multiline(data: bytes, width: int = 16) -> str:
    return "\n".join(
        " ".join(f"{b:02X}" for b in data[i:i + width])
        for i in range(0, len(data), width)
    )

def ts_bytes_to_le(tsb: bytes) -> int:
    return int.from_bytes(tsb, byteorder="little", signed=False)

def ts_bytes_to_hex(tsb: bytes) -> str:
    return hex_dump_spaces(tsb)

def format_ts(ts_bytes: bytes) -> str:
    le_val = ts_bytes_to_le(ts_bytes)
    return f"{hex_dump_spaces(ts_bytes)} (le={le_val})"

# -------------------------------------------------------------------
# CPS response parser (latest format)
# -------------------------------------------------------------------
# [0]       = 07
# [1..2]    = total frame length (LSB first)
# [3..7]    = 00 00 08 00 A1
# [8..11]   = pwr_ts (4 bytes)
# [12]      = pwr_len (1 byte) usually 4
# [13..]    = pwr_bytes (pwr_len bytes) first 4 => rect_v/rect_i
# next      = queue status (1 byte) : MSB nibble read_idx, LSB nibble write_idx
# next      = payload_valid (1 byte): 1 valid, 0 invalid
# if payload_valid == 1:
# next 4    = cps_ts (4 bytes)
# next 2    = payload length (u16, LSB first)
# next N    = payload bytes (ASCII)
# if payload_valid == 0: stop (no extra bytes)
# -------------------------------------------------------------------
def parse_cps_response_frame(resp: bytes) -> dict:
    if len(resp) < 13:
        raise ValueError(f"CPS frame too short: {len(resp)} bytes")

    if resp[0] != 0x07:
        raise ValueError("Start byte != 07")

    if resp[3:8] != bytes.fromhex("00 00 08 00 A1"):
        raise ValueError(f"Not CPS response body: {hex_dump_spaces(resp[3:8])}")

    offset = 8

    pwr_ts_bytes = resp[offset:offset + 4]
    offset += 4

    if offset >= len(resp):
        raise ValueError("Missing pwr_len")
    pwr_len = resp[offset]
    offset += 1

    if offset + pwr_len > len(resp):
        raise ValueError("pwr_len exceeds frame size")
    pwr_bytes = resp[offset:offset + pwr_len]
    offset += pwr_len

    rect_v = None
    rect_i = None
    if pwr_len >= 4:
        rect_v = pwr_bytes[0] | (pwr_bytes[1] << 8)
        rect_i = pwr_bytes[2] | (pwr_bytes[3] << 8)

    if offset + 2 > len(resp):
        raise ValueError("Missing queue_status/payload_valid")

    q = resp[offset]
    offset += 1
    read_idx = (q >> 4) & 0x0F
    write_idx = q & 0x0F

    payload_valid = resp[offset]
    offset += 1

    if payload_valid == 0:
        return {
            "pwr_ts_bytes": pwr_ts_bytes,
            "pwr_len": pwr_len,
            "pwr_bytes": pwr_bytes,
            "rect_v": rect_v,
            "rect_i": rect_i,
            "read_idx": read_idx,
            "write_idx": write_idx,
            "payload_valid": 0,
            "cps_ts_bytes": None,
            "pay_len": 0,
            "payload": b"",
            "cps_raw_only": resp[3:offset],
        }

    if offset + 6 > len(resp):
        raise ValueError("Valid payload but missing cps_ts/pay_len")

    cps_ts_bytes = resp[offset:offset + 4]
    offset += 4

    pay_len = resp[offset] | (resp[offset + 1] << 8)
    offset += 2

    payload_start = offset
    payload_end = payload_start + pay_len
    payload = resp[payload_start:payload_end] if payload_end <= len(resp) else resp[payload_start:]

    cps_raw_only = resp[3:payload_end] if payload_end <= len(resp) else resp[3:]

    return {
        "pwr_ts_bytes": pwr_ts_bytes,
        "pwr_len": pwr_len,
        "pwr_bytes": pwr_bytes,
        "rect_v": rect_v,
        "rect_i": rect_i,
        "read_idx": read_idx,
        "write_idx": write_idx,
        "payload_valid": 1,
        "cps_ts_bytes": cps_ts_bytes,
        "pay_len": pay_len,
        "payload": payload,
        "cps_raw_only": cps_raw_only,
    }
