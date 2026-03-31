"""UDP FSK1 packet receiver for live capture, decode, and logging."""
from __future__ import annotations

import os
import queue
import socket
import struct
import threading
import time
from typing import Callable, List, Optional

from project_paths import LOGS_DIR, ensure_dir

FSK_LOG_DIR = LOGS_DIR / "fsk"
LOG_DIR = os.fspath(FSK_LOG_DIR)
FSK_LOG_FILE = os.fspath(FSK_LOG_DIR / "fsk_raw.log")
FSK_CSV_FILE = os.fspath(FSK_LOG_DIR / "fsk_data.csv")

MAGIC_FSK1 = 0x314B5346
HEADER_FMT = "<IHHIHH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
RECORD_FMT = "<HhHBB"
RECORD_SIZE = struct.calcsize(RECORD_FMT)

TEST_RECORD_FMT = "<IHBB"
TEST_RECORD_SIZE = struct.calcsize(TEST_RECORD_FMT)
TEST_PATTERN = 0xA55A

CSV_HEADER = [
    "host_ts", "sequence", "rec_idx",
    "period", "delta", "ma",
    "toggled", "valid_transition", "sign_positive", "state",
    "packets_lost_before_this",
]

CSV_HEADER_TEST = [
    "host_ts", "sequence", "rec_idx",
    "counter", "pattern", "source", "flags",
]


def _ensure_dir() -> None:
    ensure_dir(FSK_LOG_DIR)


class FSKReceiver:
    """Receive FSK1 UDP packets on port 9000, decode them, and log the results."""

    def __init__(
        self,
        on_packet: Optional[Callable[[int, int, List[dict]], None]] = None,
        test_mode: bool = False,
        queue_size: int = 10000,
        enable_log_file: bool = True,
        enable_csv_file: bool = True,
        sequence_wrap_at: Optional[int] = None,
        socket_buffer_size: int = 8 * 1024 * 1024,
        ui_record_limit: int = 3,
    ):
        self._on_packet = on_packet
        self._test_mode = test_mode
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

    def start(self) -> None:
        if self._running:
            return

        if self._enable_log_file or self._enable_csv_file:
            _ensure_dir()

        if self._enable_log_file:
            self._log_file = open(FSK_LOG_FILE, "a", encoding="utf-8", buffering=1024 * 1024)

        if self._enable_csv_file:
            self._csv_init()

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
            self._running = False
            return

        self._log("[INFO] FSK UDP listener started on port 9000\n")

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

        self._log("[INFO] FSK UDP listener stopped\n")
        self._running = False

    def _process_packets(self) -> None:
        self._log("[INFO] FSK packet processor started\n")
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

        self._log(
            "[INFO] FSK packet processor stopped "
            f"(processed {packets_processed} total, queue-dropped {self._queue_dropped_packets} total)\n"
        )

    def _decode_and_log(self, ts: str, data: bytes) -> None:
        if len(data) < HEADER_SIZE:
            self._log(f"  [WARN] Packet too short ({len(data)} < {HEADER_SIZE})\n")
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

        if self._enable_log_file:
            mode_tag = "TEST" if self._test_mode else "NORMAL"
            self._log(
                f"  seq={seq}  records={rec_count}  ver={version}  "
                f"rec_size={rec_size}  mode={mode_tag}\n"
            )

        preview_records: List[dict] = []
        offset = hdr_sz

        if self._test_mode:
            self._decode_test_records(
                ts, seq, rec_count, rec_size, data, offset, preview_records, packets_lost_now
            )
        else:
            self._decode_normal_records(
                ts, seq, rec_count, rec_size, data, offset, preview_records, packets_lost_now
            )

        if self._on_packet:
            try:
                self._on_packet(seq, rec_count, preview_records)
            except Exception:
                pass

    def _decode_normal_records(
        self,
        ts: str,
        seq: int,
        rec_count: int,
        rec_size: int,
        data: bytes,
        offset: int,
        preview_records: List[dict],
        packets_lost_now: int,
    ) -> None:
        csv_rows: list[str] | None = [] if self._enable_csv_file else None
        for i in range(rec_count):
            if offset + RECORD_SIZE > len(data):
                self._log(f"  [WARN] Truncated at record {i}\n")
                break

            period, delta, ma, flags, state = struct.unpack_from(RECORD_FMT, data, offset)
            toggled = bool(flags & 0x01)
            valid_transition = bool(flags & 0x02)
            sign_positive = bool(flags & 0x04)

            if self._should_capture_preview(preview_records):
                preview_records.append(
                    {
                        "period": period,
                        "delta": delta,
                        "ma": ma,
                        "toggled": toggled,
                        "valid_transition": valid_transition,
                        "sign_positive": sign_positive,
                        "state": state,
                    }
                )

            if csv_rows is not None:
                loss_marker = packets_lost_now if i == 0 else 0
                csv_rows.append(
                    f"{ts},{seq},{i},{period},{delta},{ma},{int(toggled)},"
                    f"{int(valid_transition)},{int(sign_positive)},{state},{loss_marker}\n"
                )
            offset += rec_size

        if csv_rows:
            self._csv_rows(csv_rows)

    def _decode_test_records(
        self,
        ts: str,
        seq: int,
        rec_count: int,
        rec_size: int,
        data: bytes,
        offset: int,
        preview_records: List[dict],
        packets_lost_now: int,
    ) -> None:
        del packets_lost_now
        csv_rows: list[str] | None = [] if self._enable_csv_file else None
        for i in range(rec_count):
            if offset + TEST_RECORD_SIZE > len(data):
                self._log(f"  [WARN] Truncated at test record {i}\n")
                break

            counter, pattern, source, flags = struct.unpack_from(TEST_RECORD_FMT, data, offset)
            ok = pattern == TEST_PATTERN
            if not ok:
                self._log(
                    f"  [WARN] rec {i}: bad pattern 0x{pattern:04X} "
                    f"(expected 0x{TEST_PATTERN:04X})\n"
                )

            if self._should_capture_preview(preview_records):
                preview_records.append(
                    {
                        "counter": counter,
                        "pattern": f"0x{pattern:04X}",
                        "source": source,
                        "flags": flags,
                        "pattern_ok": ok,
                    }
                )

            if csv_rows is not None:
                csv_rows.append(f"{ts},{seq},{i},{counter},0x{pattern:04X},{source},{flags}\n")
            offset += rec_size

        if csv_rows:
            self._csv_rows(csv_rows)

    def _log(self, text: str) -> None:
        if not self._enable_log_file or not self._log_file:
            return
        with self._log_lock:
            if self._log_file:
                self._log_file.write(text)

    def _csv_init(self) -> None:
        _ensure_dir()
        header = CSV_HEADER_TEST if self._test_mode else CSV_HEADER
        self._csv_file = open(FSK_CSV_FILE, "w", newline="", encoding="utf-8", buffering=1024 * 1024)
        self._csv_file.write(",".join(header) + "\n")

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
