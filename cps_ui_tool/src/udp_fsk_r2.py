"""UDP FSK R2 packet receiver with software-side period decoding."""
from __future__ import annotations

import os
import queue
import socket
import struct
import threading
import time
from collections import deque
from typing import Callable, List, Optional

from project_paths import LOGS_DIR, ensure_dir

FSK_R2_LOG_DIR = LOGS_DIR / "fsk_r2"
FSK_R2_LOG_FILE = os.fspath(FSK_R2_LOG_DIR / "fsk_r2_raw.log")
FSK_R2_CSV_FILE = os.fspath(FSK_R2_LOG_DIR / "fsk_r2_data.csv")

MAGIC_FSK1 = 0x314B5346
HEADER_FMT = "<IHHIHH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
PERIOD_FMT = "<H"
PERIOD_RECORD_SIZE = struct.calcsize(PERIOD_FMT)
R1_RECORD_FMT = "<HhHBB"
R1_RECORD_SIZE = struct.calcsize(R1_RECORD_FMT)

SYMBOL_HALF = "HALF"
SYMBOL_FULL = "FULL"
SYMBOL_IDLE = "IDLE"
SYMBOL_INVALID = "INVALID"

TIMING_WIDE = "wide"
TIMING_SHORT = "short"
MPP_ACK = 0x88

CSV_HEADER = [
    "host_ts",
    "sequence",
    "rec_idx",
    "decode_variant",
    "record_size",
    "period",
    "ma",
    "delta",
    "diff",
    "abs_diff",
    "valid_transition",
    "toggled",
    "sign",
    "sign_positive",
    "decoder_active",
    "timing_mode",
    "period_counter",
    "symbol",
    "bit",
    "bit_count",
    "byte_hex",
    "frame_hex",
    "frame_kind",
    "frame_status",
    "r1_state",
    "packets_lost_before_this",
]


def _ensure_dir() -> None:
    ensure_dir(FSK_R2_LOG_DIR)


class FSKR2SignalDecoder:
    """Decode raw 16-bit period samples into symbols, bits, and frames."""

    def __init__(
        self,
        *,
        average_window: int = 16,
        delay_window: int = 24,
        delta_min: int = 4,
        delta_max: int = 4096,
        wide_half_target: int = 256,
        wide_full_target: int = 512,
        short_half_target: int = 64,
        short_full_target: int = 128,
        symbol_tolerance_ratio: float = 0.35,
        idle_ratio: float = 1.5,
        bits_msb_first: bool = True,
        odd_parity: bool = True,
    ) -> None:
        self._average_window = average_window
        self._delay_window = delay_window
        self._delta_min = delta_min
        self._delta_max = delta_max
        self._wide_half_target = wide_half_target
        self._wide_full_target = wide_full_target
        self._short_half_target = short_half_target
        self._short_full_target = short_full_target
        self._symbol_tolerance_ratio = symbol_tolerance_ratio
        self._idle_ratio = idle_ratio
        self._bits_msb_first = bits_msb_first
        self._odd_parity = odd_parity

        self._period_window: deque[int] = deque(maxlen=average_window)
        self._ma_history: deque[int] = deque(maxlen=delay_window + 1)

        self._timing_mode = TIMING_WIDE
        self._last_transition_sign: Optional[int] = None
        self._decoder_active = False
        self._period_counter = 0
        self._half_pending = False
        self._bit_buffer: list[int] = []

    def reset(self, *, keep_mode: bool) -> None:
        self._period_window.clear()
        self._ma_history.clear()
        self._last_transition_sign = None
        self._decoder_active = False
        self._period_counter = 0
        self._half_pending = False
        self._bit_buffer.clear()
        if not keep_mode:
            self._timing_mode = TIMING_WIDE

    def feed(self, period: int) -> dict:
        period = int(period) & 0xFFFF
        record = {
            "period": period,
            "ma": 0,
            "diff": 0,
            "abs_diff": 0,
            "valid_transition": False,
            "toggled": False,
            "sign": 0,
            "sign_positive": False,
            "decoder_active": self._decoder_active,
            "timing_mode": self._timing_mode,
            "period_counter": self._period_counter,
            "symbol": "",
            "bit": "",
            "bit_count": len(self._bit_buffer),
            "byte_hex": "",
            "frame_hex": "",
            "frame_kind": "",
            "frame_status": "",
            "state": "avg_fill",
        }

        self._period_window.append(period)
        ma = sum(self._period_window) // len(self._period_window)
        self._ma_history.append(ma)

        diff = 0
        if len(self._ma_history) == self._ma_history.maxlen:
            diff = ma - self._ma_history[0]
        abs_diff = abs(diff)
        sign = 1 if diff > 0 else -1 if diff < 0 else 0
        valid_transition = (
            len(self._ma_history) == self._ma_history.maxlen
            and self._delta_min < abs_diff < self._delta_max
        )

        if len(self._period_window) < self._average_window:
            state = "avg_fill"
        elif len(self._ma_history) < self._ma_history.maxlen:
            state = "diff_fill"
        else:
            state = "tracking"

        if self._decoder_active:
            self._period_counter += 1

        toggled = False
        symbol = ""
        bit: int | str = ""
        byte_hex = ""
        frame_hex = ""
        frame_kind = ""
        frame_status = ""
        classified_count = self._period_counter

        if valid_transition and sign != 0:
            if self._last_transition_sign is None:
                self._last_transition_sign = sign
                self._decoder_active = True
                self._period_counter = 0
                state = "armed"
            elif sign == -self._last_transition_sign:
                toggled = True
                classified_count = self._period_counter
                symbol = self._classify_symbol(classified_count)
                self._last_transition_sign = sign
                bit, byte_hex, frame_hex, frame_kind, frame_status = self._consume_symbol(symbol)
                self._period_counter = 0
                state = self._symbol_state(symbol, frame_hex, frame_status)
            else:
                state = "candidate_same_sign"
        elif valid_transition:
            state = "candidate_zero_sign"

        record.update(
            {
                "ma": ma,
                "diff": diff,
                "abs_diff": abs_diff,
                "valid_transition": valid_transition,
                "toggled": toggled,
                "sign": sign,
                "sign_positive": sign > 0,
                "decoder_active": self._decoder_active,
                "timing_mode": self._timing_mode,
                "period_counter": classified_count if toggled else self._period_counter,
                "symbol": symbol,
                "bit": bit,
                "bit_count": len(self._bit_buffer),
                "byte_hex": byte_hex,
                "frame_hex": frame_hex,
                "frame_kind": frame_kind,
                "frame_status": frame_status,
                "state": state,
            }
        )
        return record

    def _classify_symbol(self, period_counter: int) -> str:
        if period_counter <= 0:
            return SYMBOL_INVALID

        half_target, full_target = self._targets_for_mode()
        half_tol = max(2, int(half_target * self._symbol_tolerance_ratio))
        full_tol = max(4, int(full_target * self._symbol_tolerance_ratio))
        idle_min = int(full_target * self._idle_ratio)

        if abs(period_counter - half_target) <= half_tol:
            return SYMBOL_HALF
        if abs(period_counter - full_target) <= full_tol:
            return SYMBOL_FULL
        if period_counter >= idle_min:
            return SYMBOL_IDLE
        return SYMBOL_INVALID

    def _consume_symbol(self, symbol: str) -> tuple[int | str, str, str, str, str]:
        if symbol == SYMBOL_HALF:
            if self._half_pending:
                self._half_pending = False
                return self._push_bit(1)
            self._half_pending = True
            return "", "", "", "", ""

        if symbol == SYMBOL_FULL:
            if self._half_pending:
                self._reset_decoder_state()
                return "", "", "", "", "dangling_half_before_full"
            return self._push_bit(0)

        if symbol == SYMBOL_IDLE:
            return self._process_frame()

        self._reset_decoder_state()
        return "", "", "", "", "invalid_symbol_reset"

    def _push_bit(self, bit: int) -> tuple[int | str, str, str, str, str]:
        self._bit_buffer.append(bit & 0x1)
        return bit & 0x1, "", "", "", ""

    def _process_frame(self) -> tuple[int | str, str, str, str, str]:
        frame_bits = self._bit_buffer[:]
        self._reset_decoder_state()

        if not frame_bits:
            return "", "", "", "", "idle_no_bits"

        if len(frame_bits) == 8:
            value = self._bits_to_byte(frame_bits)
            frame_hex = f"{value:02X}"
            status = "response_byte"
            if value == MPP_ACK:
                self._timing_mode = TIMING_SHORT
                status = "response_byte_mpp_ack_switch_short"
            return "", frame_hex, frame_hex, "response", status

        if len(frame_bits) % 11 != 0:
            bits_text = "".join(str(bit) for bit in frame_bits)
            return "", "", bits_text, "invalid", f"unexpected_bit_count={len(frame_bits)}"

        frame_bytes: list[int] = []
        issues: list[str] = []

        for index in range(0, len(frame_bits), 11):
            word = frame_bits[index:index + 11]
            start_bit = word[0]
            data_bits = word[1:9]
            parity_bit = word[9]
            stop_bit = word[10]
            value = self._bits_to_byte(data_bits)
            frame_bytes.append(value)

            if start_bit != 0:
                issues.append(f"start@{index // 11}")
            if stop_bit != 1:
                issues.append(f"stop@{index // 11}")
            if not self._parity_ok(data_bits, parity_bit):
                issues.append(f"parity@{index // 11}")

        frame_hex = " ".join(f"{value:02X}" for value in frame_bytes)
        status = "ok" if not issues else ";".join(issues)
        return "", "", frame_hex, "data", status

    def _reset_decoder_state(self) -> None:
        self._last_transition_sign = None
        self._decoder_active = False
        self._period_counter = 0
        self._half_pending = False
        self._bit_buffer.clear()

    def _targets_for_mode(self) -> tuple[int, int]:
        if self._timing_mode == TIMING_SHORT:
            return self._short_half_target, self._short_full_target
        return self._wide_half_target, self._wide_full_target

    def _bits_to_byte(self, bits: list[int]) -> int:
        if self._bits_msb_first:
            value = 0
            for bit in bits:
                value = (value << 1) | (bit & 0x1)
            return value

        value = 0
        for index, bit in enumerate(bits):
            value |= (bit & 0x1) << index
        return value

    def _parity_ok(self, data_bits: list[int], parity_bit: int) -> bool:
        ones = sum(data_bits) + (parity_bit & 0x1)
        if self._odd_parity:
            return (ones % 2) == 1
        return (ones % 2) == 0

    @staticmethod
    def _symbol_state(symbol: str, frame_hex: str, frame_status: str) -> str:
        if symbol == SYMBOL_HALF:
            return "half_symbol"
        if symbol == SYMBOL_FULL:
            return "full_symbol"
        if symbol == SYMBOL_IDLE:
            if frame_hex:
                return "frame_process"
            return f"idle_{frame_status}" if frame_status else "idle_symbol"
        return f"invalid_{frame_status}" if frame_status else "invalid_symbol"


class FSKR2Receiver:
    """Receive FSK packets and decode 16-bit period records in software."""

    def __init__(
        self,
        on_packet: Optional[Callable[[int, int, List[dict]], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        queue_size: int = 10000,
        enable_log_file: bool = True,
        enable_csv_file: bool = True,
        sequence_wrap_at: Optional[int] = None,
        socket_buffer_size: int = 8 * 1024 * 1024,
        ui_record_limit: int = 3,
    ) -> None:
        self._on_packet = on_packet
        self._on_status = on_status
        self._enable_log_file = enable_log_file
        self._enable_csv_file = enable_csv_file
        self._sequence_wrap_at = sequence_wrap_at
        self._socket_buffer_size = socket_buffer_size
        self._ui_record_limit = ui_record_limit

        self._sock: Optional[socket.socket] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None
        self._running = False

        self._log_lock = threading.Lock()
        self._csv_lock = threading.Lock()
        self._last_seq: Optional[int] = None
        self._total_packets_received = 0
        self._total_packets_lost = 0
        self._queue_size = queue_size
        self._packet_queue: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=queue_size)
        self._queue_dropped_packets = 0
        self._last_queue_warn_bucket = -1

        self._log_file = None
        self._csv_file = None
        self._csv_batch_buffer: list[str] = []
        self._csv_batch_size = 20000
        self._decoder = FSKR2SignalDecoder()
        self._last_decode_variant: Optional[str] = None

    def start(self) -> None:
        if self._running:
            return

        if self._enable_log_file or self._enable_csv_file:
            _ensure_dir()

        if self._enable_log_file:
            self._log_file = open(FSK_R2_LOG_FILE, "a", encoding="utf-8", buffering=1024 * 1024)

        if self._enable_csv_file:
            self._csv_init()

        self._decoder.reset(keep_mode=False)
        self._last_decode_variant = None
        self._running = True
        self._recv_thread = threading.Thread(target=self._listen, daemon=True)
        self._recv_thread.start()
        self._process_thread = threading.Thread(target=self._process_packets, daemon=True)
        self._process_thread.start()

    def stop(self) -> None:
        self._running = False
        sock = self._sock
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        self._sock = None

        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=2.0)

        if self._process_thread and self._process_thread.is_alive():
            self._process_thread.join(timeout=5.0)

        if self._csv_batch_buffer:
            self._flush_csv_batch()

        if self._log_file:
            self._log_file.flush()
            self._log_file.close()
            self._log_file = None

        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None

    @property
    def running(self) -> bool:
        return self._running

    def _listen(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._socket_buffer_size)
                actual_buf = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
                self._log(f"[INFO] UDP receive buffer size set to {actual_buf} bytes\n")
            except OSError as exc:
                self._log(f"[WARN] Could not set buffer size: {exc}\n")

            self._sock.bind(("0.0.0.0", 9000))
            self._sock.settimeout(1.0)
        except OSError as exc:
            self._log(f"[ERROR] Cannot bind UDP port 9000: {exc}\n")
            self._notify_status(f"R2 bind failed on UDP 9000: {exc}")
            self._running = False
            return

        self._log("[INFO] FSK R2 UDP listener started on port 9000\n")
        self._notify_status("R2 listening on UDP 9000")

        while self._running:
            try:
                data, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                self._packet_queue.put_nowait((time.time_ns(), data))
            except queue.Full:
                self._queue_dropped_packets += 1
                if self._queue_dropped_packets == 1 or self._queue_dropped_packets % 100 == 0:
                    self._log(
                        "[ERROR] Packet queue full! Processing thread cannot keep up. "
                        f"Dropped {self._queue_dropped_packets} packet(s) so far.\n"
                    )

        self._log("[INFO] FSK R2 UDP listener stopped\n")
        if not self._running:
            self._notify_status("R2 listener stopped")
        self._running = False

    def _process_packets(self) -> None:
        self._log("[INFO] FSK R2 packet processor started\n")
        packets_processed = 0

        while self._running or not self._packet_queue.empty():
            try:
                rx_ts_ns, data = self._packet_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            packets_processed += 1
            ts = self._format_host_ts(rx_ts_ns)

            if self._enable_log_file:
                hex_preview = data[:32].hex()
                self._log(f"[{ts}] RX {len(data)} bytes  hex={hex_preview}...\n")

            self._decode_and_log(ts, data)

            if packets_processed % 250 == 0:
                if self._csv_batch_buffer:
                    self._flush_csv_batch()

                if self._log_file:
                    self._log_file.flush()
                if self._csv_file:
                    self._csv_file.flush()

                qsize = self._packet_queue.qsize()
                warn_threshold = max(100, self._queue_size // 10)
                if qsize > warn_threshold:
                    bucket = min(9, (qsize * 10) // max(1, self._queue_size))
                    if bucket != self._last_queue_warn_bucket:
                        self._log(
                            f"[WARN] Queue depth: {qsize}/{self._queue_size} - "
                            "processing thread falling behind!\n"
                        )
                        self._last_queue_warn_bucket = bucket
                else:
                    self._last_queue_warn_bucket = -1

        if self._csv_batch_buffer:
            self._flush_csv_batch()

        self._decoder.reset(keep_mode=False)
        self._last_decode_variant = None
        self._log(
            "[INFO] FSK R2 packet processor stopped "
            f"(processed {packets_processed} total, queue-dropped {self._queue_dropped_packets} total)\n"
        )

    def _decode_and_log(self, ts: str, data: bytes) -> None:
        if len(data) < HEADER_SIZE:
            self._log(f"  [WARN] Packet too short ({len(data)} < {HEADER_SIZE})\n")
            self._notify_status(f"R2 packet too short: {len(data)} bytes")
            return

        magic, version, hdr_sz, seq, rec_count, rec_size = struct.unpack_from(HEADER_FMT, data, 0)
        if magic != MAGIC_FSK1:
            self._log(f"  [WARN] Bad magic 0x{magic:08X} (expected 0x{MAGIC_FSK1:08X})\n")
            return

        self._total_packets_received += 1
        packets_lost_now = 0
        if self._last_seq is not None:
            expected_seq = self._expected_seq(self._last_seq)
            if seq != expected_seq:
                packets_lost_now = self._calculate_packets_lost(expected_seq, seq)
                if packets_lost_now > 0:
                    self._total_packets_lost += packets_lost_now
                    total_seen = self._total_packets_received + self._total_packets_lost
                    self._log(
                        f"  [PACKET LOSS] Expected seq={expected_seq}, got seq={seq}. "
                        f"Lost {packets_lost_now} packet(s). "
                        f"Total lost: {self._total_packets_lost}/{total_seen} "
                        f"({100.0 * self._total_packets_lost / total_seen:.2f}%)\n"
                    )
        self._last_seq = seq

        if packets_lost_now > 0:
            self._decoder.reset(keep_mode=True)

        if self._enable_log_file:
            self._log(
                f"  seq={seq}  records={rec_count}  ver={version}  rec_size={rec_size}  mode=R2\n"
            )

        preview_records: List[dict] = []
        csv_rows: list[str] | None = [] if self._enable_csv_file else None
        offset = hdr_sz

        decode_variant = "r2" if rec_size == PERIOD_RECORD_SIZE else "r1"
        if self._last_decode_variant is not None and self._last_decode_variant != decode_variant:
            self._decoder.reset(keep_mode=True)
        self._last_decode_variant = decode_variant

        if decode_variant == "r2":
            self._decode_period_records(
                ts, seq, rec_count, rec_size, data, offset, preview_records, csv_rows, packets_lost_now
            )
        else:
            self._decode_r1_records(
                ts, seq, rec_count, rec_size, data, offset, preview_records, csv_rows, packets_lost_now
            )

        if csv_rows:
            self._csv_rows(csv_rows)

        self._emit_preview(seq, rec_count, preview_records)

    def _log(self, text: str) -> None:
        if not self._enable_log_file or not self._log_file:
            return
        with self._log_lock:
            if self._log_file:
                self._log_file.write(text)

    def _decode_period_records(
        self,
        ts: str,
        seq: int,
        rec_count: int,
        rec_size: int,
        data: bytes,
        offset: int,
        preview_records: List[dict],
        csv_rows: list[str] | None,
        packets_lost_now: int,
    ) -> None:
        fallback_preview: List[dict] = []

        for i in range(rec_count):
            if offset + rec_size > len(data):
                self._log(f"  [WARN] Truncated at record {i}\n")
                break

            (period,) = struct.unpack_from(PERIOD_FMT, data, offset)
            decoded = self._decoder.feed(period)
            decoded["decoder_variant"] = "r2"
            decoded["record_size"] = rec_size

            if self._should_capture_preview(fallback_preview):
                fallback_preview.append(decoded.copy())
            if self._is_interesting_preview(decoded) and self._should_capture_preview(preview_records):
                preview_records.append(decoded.copy())

            if csv_rows is not None:
                loss_marker = packets_lost_now if i == 0 else 0
                csv_rows.append(
                    f"{ts},{seq},{i},r2,{rec_size},{decoded['period']},{decoded['ma']},,"
                    f"{decoded['diff']},{decoded['abs_diff']},{int(decoded['valid_transition'])},"
                    f"{int(decoded['toggled'])},{decoded['sign']},{int(decoded['sign_positive'])},"
                    f"{int(decoded['decoder_active'])},{decoded['timing_mode']},"
                    f"{decoded['period_counter']},{decoded['symbol']},{decoded['bit']},"
                    f"{decoded['bit_count']},{decoded['byte_hex']},{decoded['frame_hex']},"
                    f"{decoded['frame_kind']},{decoded['frame_status']},,{loss_marker}\n"
                )

            offset += rec_size

        if not preview_records:
            preview_records.extend(fallback_preview)

    def _decode_r1_records(
        self,
        ts: str,
        seq: int,
        rec_count: int,
        rec_size: int,
        data: bytes,
        offset: int,
        preview_records: List[dict],
        csv_rows: list[str] | None,
        packets_lost_now: int,
    ) -> None:
        if rec_size < R1_RECORD_SIZE:
            message = f"R1 fallback requires rec_size >= {R1_RECORD_SIZE}, got {rec_size}"
            self._log(f"  [WARN] {message}\n")
            self._notify_status(f"R2 received seq={seq} with {message}")
            preview_records.append(
                {
                    "message": message,
                    "state": "r1_record_too_small",
                    "decoder_variant": "r1",
                    "record_size": rec_size,
                }
            )
            return

        for i in range(rec_count):
            if offset + R1_RECORD_SIZE > len(data):
                self._log(f"  [WARN] Truncated at record {i}\n")
                break

            period, delta, ma, flags, state = struct.unpack_from(R1_RECORD_FMT, data, offset)
            toggled = bool(flags & 0x01)
            valid_transition = bool(flags & 0x02)
            sign_positive = bool(flags & 0x04)

            decoded = {
                "decoder_variant": "r1",
                "record_size": rec_size,
                "period": period,
                "delta": delta,
                "ma": ma,
                "toggled": toggled,
                "valid_transition": valid_transition,
                "sign_positive": sign_positive,
                "state": state,
            }

            if self._should_capture_preview(preview_records):
                preview_records.append(decoded.copy())

            if csv_rows is not None:
                loss_marker = packets_lost_now if i == 0 else 0
                csv_rows.append(
                    f"{ts},{seq},{i},r1,{rec_size},{period},{ma},{delta},,"
                    f"{int(valid_transition)},{int(toggled)},,{int(sign_positive)},,,,"
                    f",,,,{state},{loss_marker}\n"
                )

            offset += rec_size

    def _csv_init(self) -> None:
        _ensure_dir()
        self._csv_file = open(FSK_R2_CSV_FILE, "w", newline="", encoding="utf-8", buffering=1024 * 1024)
        self._csv_file.write(",".join(CSV_HEADER) + "\n")

    def _csv_rows(self, rows: list[str]) -> None:
        if not self._enable_csv_file or not rows:
            return

        try:
            self._csv_batch_buffer.extend(rows)
            if len(self._csv_batch_buffer) >= self._csv_batch_size:
                self._flush_csv_batch()
        except (PermissionError, AttributeError):
            pass

    def _flush_csv_batch(self) -> None:
        if not self._csv_batch_buffer or not self._csv_file:
            return

        try:
            with self._csv_lock:
                if self._csv_file:
                    self._csv_file.writelines(self._csv_batch_buffer)
                    self._csv_batch_buffer.clear()
        except (PermissionError, AttributeError):
            pass

    def _notify_status(self, message: str) -> None:
        if self._on_status is None:
            return
        try:
            self._on_status(message)
        except Exception:
            pass

    def _emit_preview(self, seq: int, rec_count: int, preview_records: List[dict]) -> None:
        if self._on_packet is None:
            return
        try:
            self._on_packet(seq, rec_count, preview_records)
        except Exception:
            pass

    @staticmethod
    def _format_host_ts(ts_ns: int) -> str:
        seconds = ts_ns // 1_000_000_000
        millis = (ts_ns // 1_000_000) % 1000
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds)) + f".{millis:03d}"

    def _expected_seq(self, last_seq: int) -> int:
        if self._sequence_wrap_at is None:
            return last_seq + 1
        return 0 if last_seq >= self._sequence_wrap_at else last_seq + 1

    def _calculate_packets_lost(self, expected_seq: int, seq: int) -> int:
        if self._sequence_wrap_at is None or seq > expected_seq:
            return seq - expected_seq
        return ((self._sequence_wrap_at + 1) - expected_seq) + seq

    def _should_capture_preview(self, preview_records: List[dict]) -> bool:
        if self._on_packet is None or self._ui_record_limit == 0:
            return False
        if self._ui_record_limit < 0:
            return True
        return len(preview_records) < self._ui_record_limit

    @staticmethod
    def _is_interesting_preview(record: dict) -> bool:
        return bool(
            record["valid_transition"]
            or record["toggled"]
            or record["symbol"]
            or record["bit"] != ""
            or record["frame_hex"]
            or record["frame_status"]
        )
