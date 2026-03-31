"""UDP FSK1 packet receiver – listens on port 9000, decodes and logs."""
from __future__ import annotations

import csv
import os
import queue
import socket
import struct
import threading
from datetime import datetime
from typing import Callable, List, Optional

LOG_DIR = "logs"
FSK_LOG_FILE = os.path.join(LOG_DIR, "fsk_raw.log")
FSK_CSV_FILE = os.path.join(LOG_DIR, "fsk_data.csv")

MAGIC_FSK1 = 0x314B5346  # "FSK1" little-endian
HEADER_FMT = "<IHHIHH"   # magic, version, header_size, sequence, record_count, record_size
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 16 bytes
RECORD_FMT = "<HhHBB"    # period, delta, ma, flags, state
RECORD_SIZE = struct.calcsize(RECORD_FMT)  # 8 bytes

# Test-mode record: fsk_test_record_t
TEST_RECORD_FMT = "<IHBB"  # counter(u32), pattern(u16), source(u8), flags(u8)
TEST_RECORD_SIZE = struct.calcsize(TEST_RECORD_FMT)  # 8 bytes
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


def _ensure_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


class FSKReceiver:
    """Receives FSK1 UDP packets on port 9000, decodes and logs them."""

    def __init__(self, on_packet: Optional[Callable] = None,
                 test_mode: bool = False):
        self._on_packet = on_packet
        self._test_mode = test_mode
        self._sock: Optional[socket.socket] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None
        self._running = False
        self._log_lock = threading.Lock()
        self._csv_lock = threading.Lock()
        self._last_seq: Optional[int] = None  # Track last sequence number
        self._total_packets_received = 0
        self._total_packets_lost = 0
        self._packet_queue: queue.Queue = queue.Queue(maxsize=1000)  # Decouple RX from processing

    # ── public ────────────────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        _ensure_dir()
        self._csv_init()
        self._running = True

        # Start receiver thread (fast, just receives UDP packets)
        self._recv_thread = threading.Thread(target=self._listen, daemon=True)
        self._recv_thread.start()

        # Start processor thread (handles decoding, file I/O)
        self._process_thread = threading.Thread(target=self._process_packets, daemon=True)
        self._process_thread.start()

    def stop(self):
        self._running = False
        sock = self._sock
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        self._sock = None

    @property
    def running(self) -> bool:
        return self._running

    # ── internal ──────────────────────────────────────────────────────
    def _listen(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Increase receive buffer size to reduce packet loss (2 MB)
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
                actual_buf = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
                self._log(f"[INFO] UDP receive buffer size set to {actual_buf} bytes\n")
            except OSError as e:
                self._log(f"[WARN] Could not set buffer size: {e}\n")

            self._sock.bind(("0.0.0.0", 9000))
            self._sock.settimeout(1.0)
        except OSError as e:
            self._log(f"[ERROR] Cannot bind UDP port 9000: {e}\n")
            self._running = False
            return

        self._log(f"[INFO] FSK UDP listener started on port 9000\n")

        while self._running:
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            # Fast path: just timestamp and queue the packet
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            try:
                # Non-blocking put - if queue is full, we'll know we're falling behind
                self._packet_queue.put_nowait((ts, data, addr))
            except queue.Full:
                self._log(f"[ERROR] Packet queue full! Processing thread cannot keep up. Dropping packet.\n")

        self._log("[INFO] FSK UDP listener stopped\n")
        self._running = False

    def _process_packets(self):
        """
        Consumer thread: processes packets from queue.
        This thread handles all the slow I/O (logging, CSV writing, decoding).
        """
        self._log("[INFO] FSK packet processor started\n")
        packets_processed = 0

        while self._running:
            try:
                # Block with timeout so we can check _running flag
                ts, data, addr = self._packet_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            packets_processed += 1

            # Log the raw packet
            self._log(f"[{ts}] RX {len(data)} bytes from {addr[0]}:{addr[1]}  "
                       f"hex={data.hex()}\n")

            # Decode and write to CSV
            self._decode_and_log(ts, data)

            # Periodic queue depth logging (every 100 packets)
            if packets_processed % 100 == 0:
                qsize = self._packet_queue.qsize()
                if qsize > 50:
                    self._log(f"[WARN] Queue depth: {qsize}/1000 - processing thread falling behind!\n")

        self._log("[INFO] FSK packet processor stopped\n")

    def _decode_and_log(self, ts: str, data: bytes):
        if len(data) < HEADER_SIZE:
            self._log(f"  [WARN] Packet too short ({len(data)} < {HEADER_SIZE})\n")
            return

        magic, version, hdr_sz, seq, rec_count, rec_size = struct.unpack_from(
            HEADER_FMT, data, 0
        )

        if magic != MAGIC_FSK1:
            self._log(f"  [WARN] Bad magic 0x{magic:08X} (expected 0x{MAGIC_FSK1:08X})\n")
            return

        # ── Packet loss detection ────────────────────────────────────────
        # NOTE: FW rolls over from 169 to 0 (not 65535)
        self._total_packets_received += 1
        packets_lost_now = 0
        if self._last_seq is not None:
            # Handle FW-specific rollover (0-169, then wraps to 0)
            if self._last_seq == 169:
                expected_seq = 0  # FW rolls over from 169 to 0
            else:
                expected_seq = self._last_seq + 1

            if seq != expected_seq:
                # Calculate number of lost packets
                if seq > expected_seq:
                    packets_lost_now = seq - expected_seq
                else:
                    # Handle rollover case (169->0 is normal, but 169->50 means loss)
                    if self._last_seq == 169 and seq == 0:
                        packets_lost_now = 0  # Normal rollover
                    else:
                        # Packet loss during rollover (e.g., 169 -> 5 means lost 0,1,2,3,4)
                        packets_lost_now = (170 - expected_seq) + seq

                if packets_lost_now > 0:
                    self._total_packets_lost += packets_lost_now
                    self._log(f"  [PACKET LOSS] Expected seq={expected_seq}, got seq={seq}. "
                              f"Lost {packets_lost_now} packet(s). "
                              f"Total lost: {self._total_packets_lost}/{self._total_packets_received + self._total_packets_lost} "
                              f"({100.0 * self._total_packets_lost / (self._total_packets_received + self._total_packets_lost):.2f}%)\n")
        self._last_seq = seq

        mode_tag = "TEST" if self._test_mode else "NORMAL"
        self._log(f"  seq={seq}  records={rec_count}  ver={version}  "
                  f"rec_size={rec_size}  mode={mode_tag}\n")

        records: List[dict] = []
        offset = hdr_sz

        if self._test_mode:
            self._decode_test_records(ts, seq, rec_count, rec_size, data, offset, records, packets_lost_now)
        else:
            self._decode_normal_records(ts, seq, rec_count, rec_size, data, offset, records, packets_lost_now)

        if self._on_packet:
            try:
                self._on_packet(seq, records)
            except Exception:
                pass

    def _decode_normal_records(self, ts, seq, rec_count, rec_size, data, offset, records, packets_lost_now):
        for i in range(rec_count):
            if offset + RECORD_SIZE > len(data):
                self._log(f"  [WARN] Truncated at record {i}\n")
                break
            period, delta, ma, flags, state = struct.unpack_from(RECORD_FMT, data, offset)
            toggled          = bool(flags & 0x01)
            valid_transition = bool(flags & 0x02)
            sign_positive    = bool(flags & 0x04)

            rec = dict(period=period, delta=delta, ma=ma,
                       toggled=toggled, valid_transition=valid_transition,
                       sign_positive=sign_positive, state=state)
            records.append(rec)

            # Only mark first record with packet loss info
            loss_marker = packets_lost_now if i == 0 else 0

            self._csv_row([
                ts, seq, i,
                period, delta, ma,
                int(toggled), int(valid_transition), int(sign_positive), state,
                loss_marker,
            ])
            offset += rec_size

    def _decode_test_records(self, ts, seq, rec_count, rec_size, data, offset, records, packets_lost_now):
        for i in range(rec_count):
            if offset + TEST_RECORD_SIZE > len(data):
                self._log(f"  [WARN] Truncated at test record {i}\n")
                break
            counter, pattern, source, flags = struct.unpack_from(
                TEST_RECORD_FMT, data, offset
            )
            ok = (pattern == TEST_PATTERN)
            if not ok:
                self._log(f"  [WARN] rec {i}: bad pattern 0x{pattern:04X} "
                          f"(expected 0x{TEST_PATTERN:04X})\n")

            rec = dict(counter=counter, pattern=f"0x{pattern:04X}",
                       source=source, flags=flags, pattern_ok=ok)
            records.append(rec)

            self._csv_row([ts, seq, i, counter, f"0x{pattern:04X}", source, flags])
            offset += rec_size

    # ── logging helpers ───────────────────────────────────────────────
    def _log(self, text: str):
        _ensure_dir()
        with self._log_lock:
            with open(FSK_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(text)

    def _csv_init(self):
        _ensure_dir()
        hdr = CSV_HEADER_TEST if self._test_mode else CSV_HEADER
        with self._csv_lock:
            # Always rewrite header when starting (mode may have changed)
            with open(FSK_CSV_FILE, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(hdr)

    def _csv_row(self, row: list):
        try:
            with self._csv_lock:
                with open(FSK_CSV_FILE, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(row)
        except PermissionError:
            pass
