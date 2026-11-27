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

def format_timestamp_ms(timestamp_us: int | None) -> str:
    if timestamp_us is None:
        return ""
    return f"{timestamp_us / 1000.0:.3f}"


# Calibration API 0x07/0x02 response:
# 07 0D 00 00 00 07 00 02 00 VV VV II II
RAW_RECT_COUNTS_MARKER = bytes.fromhex("00 00 07 00 02 00")


def parse_raw_rect_counts_response(resp: bytes) -> dict:
    """Decode rectified voltage/current ADC counts from the calibration API."""
    if len(resp) < 13:
        raise ValueError(f"Raw rectified-count frame too short: {len(resp)} bytes")
    if resp[0] != 0x07:
        raise ValueError("Start byte != 07")
    declared = int.from_bytes(resp[1:3], "little")
    if declared != len(resp):
        raise ValueError(
            f"Raw rectified-count length mismatch: header={declared}, actual={len(resp)}"
        )
    if resp[3:9] != RAW_RECT_COUNTS_MARKER:
        raise ValueError(f"Not raw rectified-count response: {hex_dump_spaces(resp[3:9])}")
    return {
        "raw_vrect_count": int.from_bytes(resp[9:11], "little"),
        "raw_irect_count": int.from_bytes(resp[11:13], "little"),
        "raw_bytes": resp[9:13],
    }

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


def split_cps_payload_lines(info: dict, pending: bytearray) -> list[dict]:
    """
    Convert CPS streamer payload bytes into newline-delimited packet records.

    Firmware may deliver partial lines or multiple lines in one CPS response.
    The UI should only decode/display complete packets, so payload bytes are
    accumulated until LF is seen. CR from CRLF is stripped from the packet.
    """
    if int(info.get("payload_valid", 0)) != 1:
        return [info]

    payload = bytes(info.get("payload", b""))
    if not payload:
        return []

    pending.extend(payload)
    packets: list[dict] = []

    while True:
        newline_at = pending.find(b"\n")
        if newline_at < 0:
            break

        line = bytes(pending[:newline_at])
        del pending[:newline_at + 1]
        if line.endswith(b"\r"):
            line = line[:-1]
        if not line:
            continue

        line_info = dict(info)
        line_info["_frame_pay_len"] = int(info.get("pay_len", len(payload)))
        line_info["payload"] = line
        line_info["pay_len"] = len(line)
        line_info["_stream_line_packet"] = True
        line_info["_stream_line_seq"] = len(packets) + 1
        packets.append(line_info)

    return packets


# New framed-streamer format (Ethernet_Streamer_Polling_API.md).
STREAMER_SOP = bytes.fromhex("A5 5A C3 3C")
STREAMER_EOP = bytes.fromhex("3C C3 5A A5")
STREAMER_MAX_PAYLOAD = 256


def parse_new_streamer_response(resp: bytes) -> dict:
    """Decode the information prefix of a subtype-0x01 polling response."""
    if len(resp) < 17:
        raise ValueError(f"New streamer response too short: {len(resp)} bytes")
    if resp[0] != 0x07 or resp[5:9] != bytes.fromhex("08 00 A1 01"):
        raise ValueError("Not a new streamer polling response")

    declared = int.from_bytes(resp[1:3], "little")
    if declared != len(resp):
        raise ValueError(
            f"New streamer response length mismatch: header={declared}, actual={len(resp)}"
        )

    flags_and_count = int.from_bytes(resp[9:11], "little")
    flags = flags_and_count & 0xF000
    valid_count = flags_and_count & 0x0FFF
    if valid_count > STREAMER_MAX_PAYLOAD:
        raise ValueError(f"New streamer valid-byte count exceeds 256: {valid_count}")
    if len(resp) < 17 + valid_count:
        raise ValueError(
            f"New streamer response has {len(resp) - 17} stream bytes, expected {valid_count}"
        )

    read_index = int.from_bytes(resp[11:13], "little")
    write_index = int.from_bytes(resp[13:15], "little")
    if read_index >= 4096 or write_index >= 4096:
        raise ValueError(
            f"New streamer index out of range: read={read_index}, write={write_index}"
        )

    return {
        "streamer_format": "new",
        "streamer_flags": flags,
        "streamer_overflow": bool(flags & 0x1000),
        "valid_stream_byte_count": valid_count,
        "read_index": read_index,
        "write_index": write_index,
        "expected_next_read": (read_index + valid_count) % 4096,
        "stream_bytes": resp[17:17 + valid_count],
    }


def split_new_streamer_records(response_info: dict, pending: bytearray) -> list[dict]:
    """Append one poll's bytes and return every complete, validated record."""
    pending.extend(response_info.get("stream_bytes", b""))
    records: list[dict] = []

    while True:
        sop_at = pending.find(STREAMER_SOP)
        if sop_at < 0:
            # Retain a possible partial SOP suffix across polling responses.
            keep = min(len(pending), len(STREAMER_SOP) - 1)
            if len(pending) > keep:
                del pending[:len(pending) - keep]
            break
        if sop_at:
            del pending[:sop_at]
        if len(pending) < 14:
            break

        payload_len = int.from_bytes(pending[12:14], "little")
        if payload_len > STREAMER_MAX_PAYLOAD:
            del pending[0]
            continue
        payload_end = 14 + payload_len
        legacy_record_size = payload_end + len(STREAMER_EOP)
        if len(pending) < legacy_record_size:
            break

        packet_timestamp_bytes = None
        if bytes(pending[payload_end:legacy_record_size]) == STREAMER_EOP:
            record_size = legacy_record_size
        else:
            timestamp_eop_start = payload_end + 10
            record_size = timestamp_eop_start + len(STREAMER_EOP)
            if len(pending) < record_size:
                break
            if bytes(pending[timestamp_eop_start:record_size]) != STREAMER_EOP:
                del pending[0]
                continue
            packet_timestamp_bytes = bytes(pending[payload_end:timestamp_eop_start])

        record = bytes(pending[:record_size])
        del pending[:record_size]
        timestamp = int.from_bytes(record[4:8], "little")
        rect_v = int.from_bytes(record[8:10], "little")
        rect_i = int.from_bytes(record[10:12], "little")
        payload = record[14:14 + payload_len]
        packet_start_ts_bytes = (
            packet_timestamp_bytes[:4] if packet_timestamp_bytes is not None else None
        )
        packet_end_ts_bytes = (
            packet_timestamp_bytes[4:8] if packet_timestamp_bytes is not None else None
        )
        packet_reserved_bytes = (
            packet_timestamp_bytes[8:10] if packet_timestamp_bytes is not None else None
        )
        packet_start_ts = (
            int.from_bytes(packet_start_ts_bytes, "little")
            if packet_start_ts_bytes is not None else None
        )
        packet_end_ts = (
            int.from_bytes(packet_end_ts_bytes, "little")
            if packet_end_ts_bytes is not None else None
        )
        records.append({
            "streamer_format": "new",
            "pwr_ts_bytes": record[4:8],
            "pwr_len": 4,
            "pwr_bytes": record[8:12],
            "rect_v": rect_v,
            "rect_i": rect_i,
            "read_idx": response_info.get("read_index", 0),
            "write_idx": response_info.get("write_index", 0),
            "payload_valid": 1,
            "cps_ts_bytes": record[4:8],
            "pay_len": payload_len,
            "payload": payload,
            "packet_timestamp_bytes": packet_timestamp_bytes,
            "packet_start_ts_bytes": packet_start_ts_bytes,
            "packet_start_ts": packet_start_ts,
            "packet_end_ts_bytes": packet_end_ts_bytes,
            "packet_end_ts": packet_end_ts,
            "packet_timestamp_delta": (
                packet_end_ts - packet_start_ts
                if packet_start_ts is not None and packet_end_ts is not None else None
            ),
            "packet_reserved_bytes": packet_reserved_bytes,
            "cps_raw_only": record,
            "streamer_flags": response_info.get("streamer_flags", 0),
            "streamer_overflow": response_info.get("streamer_overflow", False),
            "read_index": response_info.get("read_index", 0),
            "write_index": response_info.get("write_index", 0),
            "expected_next_read": response_info.get("expected_next_read", 0),
            "_stream_record": True,
            "_stream_timestamp": timestamp,
        })

    return records


def reassemble_new_streamer_message(
    record_info: dict,
    pending_payload: bytearray,
    pending_info: dict,
) -> list[dict]:
    """Join a textual message that firmware split across streamer records.

    A framed streamer record guarantees binary framing, but its ASCII payload
    can still stop in the middle of a line. A trailing LF marks the end of the
    textual message currently displayed by the utility.
    """
    payload = bytes(record_info.get("payload", b""))
    if not payload:
        return []

    if not pending_payload:
        pending_info.clear()
        pending_info.update(record_info)
    pending_payload.extend(payload)

    if not pending_payload.endswith(b"\n"):
        return []

    joined = bytes(pending_payload)
    pending_payload.clear()
    message_info = dict(pending_info)
    pending_info.clear()
    if joined.endswith(b"\r\n"):
        joined = joined[:-2]
    elif joined.endswith(b"\n"):
        joined = joined[:-1]
    message_info["payload"] = joined
    message_info["pay_len"] = len(joined)
    message_info["_stream_message_reassembled"] = True
    return [message_info]
