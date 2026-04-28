from __future__ import annotations

import socket
import threading
import time
import queue
from datetime import datetime
from typing import Callable, Optional

from protocol import (
    parse_cps_response_frame,
    hex_dump_spaces,
    hex_dump_multiline,
    format_ts,
    ts_bytes_to_hex,
    ts_bytes_to_le,
)
from logger import (
    SESSION_LOG_FILE, CPS_LOG_FILE, CPS_CSV_FILE,
    append_text, csv_init_if_needed, csv_append_row
)

# -----------------------------
# Streamer / pacing config
# -----------------------------
CPS_POLL_HEX = "07 08 00 00 00 08 00 A1"
CPS_POLL_INTERVAL_SEC = 0.100   # streamer cadence

# Your requirement:
RX_WAIT_PER_TRY_SEC = 0.500     # 500ms wait per try (increased for slow STM responses)
MAX_RETRIES = 3                 # 3 retries then show popup

# Small sleep to avoid busy spinning
LOOP_SLEEP_SEC = 0.002

MAX_FRAME_LEN = 65535

# Optional strictness:
# If True, for GET we will ignore "echo frames" that are byte-identical to TX.
# Set False if your device never echoes GET and any frame is fine.
GET_IGNORE_ECHO_IDENTICAL_TO_TX = False


def host_ts_full() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def bytes_from_hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", "").strip())


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("Socket closed by peer")
            buf += chunk
        except socket.timeout:
            # If we've read partial data, this is a protocol error
            if len(buf) > 0:
                print(f"[DEBUG] TIMEOUT after reading {len(buf)}/{n} bytes: {' '.join(f'{b:02X}' for b in buf)}")
            raise
    return buf


def read_len_prefixed_frame(sock: socket.socket, max_len: int = MAX_FRAME_LEN) -> bytes:
    hdr = recv_exact(sock, 3)
    # Debug: print what we received
    print(f"[DEBUG] RX header: {' '.join(f'{b:02X}' for b in hdr)}")
    if hdr[0] not in (0x07, 0x01, 0x02):   # 0x07 = GET/CPS frames, 0x01 = SET echo, 0x02 = FW mode commands
        raise ValueError(f"Bad start byte: {hdr[0]:02X}, expected 0x01/0x02/0x07")
    total_len = hdr[1] | (hdr[2] << 8)
    print(f"[DEBUG] Frame length: {total_len}")
    if total_len < 3 or total_len > max_len:
        raise ValueError(f"Bad frame length: {total_len}")
    rest = recv_exact(sock, total_len - 3)
    frame = hdr + rest
    print(f"[DEBUG] Complete frame: {' '.join(f'{b:02X}' for b in frame)}")
    return frame


class NetClient:
    """
    Behavior:
      - One in-flight TX at a time.
      - Wait for RX up to 100ms; if none, resend same TX (retry) up to 3 tries.
      - If still no RX, close socket and reconnect (fresh 3-way handshake).
      - Streamer CPS poll runs every 100ms but only when idle (no manual pending).
    """

    def __init__(
        self,
        ip: str,
        port: int,
        on_status: Callable[[str], None],
        on_cps: Callable[[dict], None],
        on_tx: Optional[Callable[[str, str], None]] = None,
        on_rx: Optional[Callable[[str, str], None]] = None,
        on_retry_prompt: Optional[Callable[[str, str], bool]] = None,  # (hex_str, tag) -> should_retry
        connect_timeout: float = 5.0,
    ):
        self.ip = ip
        self.port = port
        self.on_status = on_status
        self.on_cps = on_cps
        self.on_tx = on_tx
        self.on_rx = on_rx
        self.on_retry_prompt = on_retry_prompt
        self.connect_timeout = connect_timeout

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # manual queue entries: (bytes_to_send, tag, hex_str_for_log)
        self.send_q: "queue.Queue[tuple[bytes, str, str]]" = queue.Queue()

        self._session_lock = threading.Lock()
        self._cps_lock = threading.Lock()
        self._csv_lock = threading.Lock()

        self._cps_poll_bytes = bytes_from_hex(CPS_POLL_HEX)
        self._next_cps_poll = 0.0
        self.plot_enabled: bool = False   # controlled by UI checkbox

        # inflight tracking
        self._waiting = False
        self._inflight_payload: bytes = b""
        self._inflight_hex: str = ""
        self._inflight_tag: str = "AUTO"   # AUTO or MANUAL
        self._inflight_kind: str = "GET"   # GET or SET (based on first byte)
        self._retry_count: int = 0
        self._try_deadline: float = 0.0

        # socket
        self._sock: Optional[socket.socket] = None
        self._exclusive_cv = threading.Condition()
        self._exclusive_requested = False
        self._exclusive_active = False
        self._exclusive_owner = ""
        self._exclusive_prev_plot_enabled = False

    def start(self):
        self._stop.clear()
        csv_init_if_needed(self._csv_lock, CPS_CSV_FILE)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._exclusive_cv:
            self._exclusive_cv.notify_all()
        self._close_socket()

    def send_hex(self, hex_str: str, tag: str = "MANUAL"):
        b = bytes_from_hex(hex_str)
        with self._exclusive_cv:
            if self._exclusive_requested or self._exclusive_active:
                owner = self._exclusive_owner or "another operation"
                raise RuntimeError(f"Socket is reserved for {owner}")
        self.send_q.put((b, tag, hex_str.strip()))

    def send_bytes(self, b: bytes, tag: str = "MANUAL"):
        with self._exclusive_cv:
            if self._exclusive_requested or self._exclusive_active:
                owner = self._exclusive_owner or "another operation"
                raise RuntimeError(f"Socket is reserved for {owner}")
        self.send_q.put((b, tag, hex_dump_spaces(b)))

    def acquire_exclusive_socket(self, owner: str, timeout: float = 10.0) -> socket.socket:
        deadline = time.monotonic() + max(timeout, 0.1)
        with self._exclusive_cv:
            if self._sock is None:
                raise RuntimeError("Ethernet client is not connected")
            if self._exclusive_requested or self._exclusive_active:
                active_owner = self._exclusive_owner or "another operation"
                raise RuntimeError(f"Socket is already reserved for {active_owner}")

            self._exclusive_requested = True
            self._exclusive_owner = owner
            self._exclusive_prev_plot_enabled = self.plot_enabled
            self.plot_enabled = False
            self._log_session(
                f"[{host_ts_full()}] [INFO] Exclusive socket requested by {owner}\n"
            )
            self._exclusive_cv.notify_all()

            while True:
                if self._exclusive_active and self._sock is not None:
                    self._log_session(
                        f"[{host_ts_full()}] [INFO] Exclusive socket granted to {owner}\n"
                    )
                    return self._sock
                if self._stop.is_set():
                    self._exclusive_requested = False
                    self._exclusive_owner = ""
                    self.plot_enabled = self._exclusive_prev_plot_enabled
                    self._exclusive_prev_plot_enabled = False
                    self._exclusive_cv.notify_all()
                    raise RuntimeError("Ethernet client stopped while waiting for exclusive access")

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._exclusive_requested = False
                    self._exclusive_owner = ""
                    self.plot_enabled = self._exclusive_prev_plot_enabled
                    self._exclusive_prev_plot_enabled = False
                    self._exclusive_cv.notify_all()
                    raise TimeoutError("Timed out waiting for exclusive socket access")
                self._exclusive_cv.wait(timeout=min(0.2, remaining))

    def release_exclusive_socket(self, owner: str):
        with self._exclusive_cv:
            if not self._exclusive_requested and not self._exclusive_active:
                return
            if self._exclusive_owner and owner != self._exclusive_owner:
                raise RuntimeError(
                    f"Socket is reserved for {self._exclusive_owner}, not {owner}"
                )

            self._log_session(
                f"[{host_ts_full()}] [INFO] Exclusive socket released by {owner}\n"
            )
            self._exclusive_requested = False
            self._exclusive_active = False
            self._exclusive_owner = ""
            self.plot_enabled = self._exclusive_prev_plot_enabled
            self._exclusive_prev_plot_enabled = False
            self._waiting = False
            self._retry_count = 0
            self._inflight_payload = b""
            self._inflight_hex = ""
            self._inflight_tag = "AUTO"
            self._inflight_kind = "GET"
            self._try_deadline = 0.0
            self._next_cps_poll = time.monotonic() + CPS_POLL_INTERVAL_SEC
            sock = self._sock
            self._exclusive_cv.notify_all()

        if sock is not None:
            try:
                sock.settimeout(0.1)
            except Exception:
                pass

    # ---------------- internal helpers ----------------
    def _emit_tx(self, hexs: str, tag: str):
        if self.on_tx:
            self.on_tx(hexs, tag)

    def _emit_rx(self, hexs: str, tag: str):
        if self.on_rx:
            self.on_rx(hexs, tag)

    def _log_session(self, line: str):
        append_text(self._session_lock, SESSION_LOG_FILE, line)

    def _log_cps(self, line: str):
        append_text(self._cps_lock, CPS_LOG_FILE, line)

    def _connect(self) -> socket.socket:
        self.on_status(f"Connecting to {self.ip}:{self.port} ...")
        sock = socket.create_connection((self.ip, self.port), timeout=self.connect_timeout)

        # Socket recv timeout - needs to be long enough for TCP segments to arrive
        # but short enough to check retry deadlines frequently
        sock.settimeout(0.1)  # 100ms timeout for individual recv() calls

        self._sock = sock
        port_info = ""
        if self.port == 5002:
            port_info = " (API Frame Protocol - 0x01/0x07/0x02)"
        self.on_status(f"Connected ✅{port_info}")
        self._log_session(f"[{host_ts_full()}] [INFO] Connected to {self.ip}:{self.port}{port_info}\n")
        self._next_cps_poll = time.monotonic() + CPS_POLL_INTERVAL_SEC
        return sock

    def _close_socket(self):
        s = self._sock
        self._sock = None
        if not s:
            return
        try:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            s.close()
        except Exception:
            pass

    def _reconnect(self):
        self._log_session(f"[{host_ts_full()}] [INFO] Reconnecting...\n")
        self.on_status("Reconnecting...")
        self._close_socket()
        # reset inflight state
        self._waiting = False
        self._retry_count = 0
        self._inflight_payload = b""
        self._inflight_hex = ""
        self._inflight_tag = "AUTO"
        self._inflight_kind = "GET"
        self._try_deadline = 0.0

    def _kind_from_payload(self, payload: bytes) -> str:
        if not payload:
            return "GET"
        if payload[0] == 0x07:
            return "GET"
        elif payload[0] in (0x01, 0x02):  # 0x01 = SET, 0x02 = FW mode (both echo back)
            return "SET"
        else:
            return "GET"

    def _arm_inflight(self, payload: bytes, tag: str, hex_str: str):
        self._waiting = True
        self._inflight_payload = payload
        self._inflight_hex = hex_str
        self._inflight_tag = tag
        self._inflight_kind = self._kind_from_payload(payload)
        self._retry_count = 0
        self._try_deadline = time.monotonic() + RX_WAIT_PER_TRY_SEC

    def _send_inflight(self, sock: socket.socket):
        sock.sendall(self._inflight_payload)
        now = host_ts_full()
        self._log_session(f"[{now}] [TX:{self._inflight_tag}] {self._inflight_hex} (try={self._retry_count+1}/{MAX_RETRIES})\n")
        self._emit_tx(self._inflight_hex, self._inflight_tag)
        self._try_deadline = time.monotonic() + RX_WAIT_PER_TRY_SEC

    def _rx_accepts_as_response(self, frame: bytes) -> bool:
        """
        Decide if this RX satisfies the current in-flight TX.
        - SET (0x01/0x02 start): echo-back (accept frames with matching start byte)
        - GET (0x07 start): accept any frame; optionally ignore frame identical to TX
        """
        if self._inflight_kind == "SET":
            # Accept frames with 0x01 or 0x02 prefix as echo.
            # Exact byte match is avoided because the server may echo back
            # with a different length field (bytes[1:3]) than what was sent.
            return len(frame) >= 1 and frame[0] in (0x01, 0x02)

        # GET
        if GET_IGNORE_ECHO_IDENTICAL_TO_TX:
            return frame != self._inflight_payload
        return True

    def _handle_rx_frame(self, frame: bytes):
        rx_hex = hex_dump_spaces(frame)
        if self._inflight_kind == "SET":
            rx_tag = "AUTO_ECHO"  if self._inflight_tag == "AUTO" else "MANUAL_ECHO"
        else:
            rx_tag = "AUTO_RSP"   if self._inflight_tag == "AUTO" else "MANUAL_RSP"

        now = host_ts_full()
        self._log_session(f"[{now}] [RX:{rx_tag}] {rx_hex}\n")
        self._emit_rx(rx_hex, rx_tag)

        # Does this satisfy current in-flight?
        if self._waiting and self._rx_accepts_as_response(frame):
            # release gate
            self._waiting = False
            self._inflight_payload = b""
            self._inflight_hex = ""
            self._inflight_tag = "AUTO"
            self._inflight_kind = "GET"
            self._retry_count = 0
            self._try_deadline = 0.0

        # CPS decode if CPS response
        if len(frame) >= 8 and frame[3:8] == bytes.fromhex("00 00 08 00 A1"):
            info = parse_cps_response_frame(frame)
            info["_host_ts_full"] = now
            info["_rx_tag"] = rx_tag

            ht = now
            cps_entry = (
                "----- CPS POLL -----\n"
                f"host_ts       : {ht}\n"
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
                f"queue_status  : 0x{((info['read_idx']<<4) | info['write_idx']):02X} "
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
            self._log_cps(cps_entry)

            # CSV row
            pwr_ts_hex = ts_bytes_to_hex(info["pwr_ts_bytes"])
            pwr_ts_le = ts_bytes_to_le(info["pwr_ts_bytes"])
            cps_ts_hex = ""
            cps_ts_le = ""
            if info["cps_ts_bytes"] is not None:
                cps_ts_hex = ts_bytes_to_hex(info["cps_ts_bytes"])
                cps_ts_le = str(ts_bytes_to_le(info["cps_ts_bytes"]))
            rect_v = "" if info["rect_v"] is None else str(info["rect_v"])
            rect_i = "" if info["rect_i"] is None else str(info["rect_i"])

            row = [
                ht, pwr_ts_hex, pwr_ts_le,
                cps_ts_hex, cps_ts_le,
                rect_v, rect_i,
                info["pwr_len"], info["read_idx"], info["write_idx"],
                info["payload_valid"], info["pay_len"],
            ]
            try:
                csv_append_row(self._csv_lock, CPS_CSV_FILE, row)
            except PermissionError as csv_err:
                self._log_session(f"[{host_ts_full()}] [WARN] CSV write skipped (file locked): {csv_err}\n")

            self.on_cps(info)

    # ---------------- main loop ----------------
    def _run(self):
        sock: Optional[socket.socket] = None
        try:
            sock = self._connect()

            while not self._stop.is_set():
                assert sock is not None

                with self._exclusive_cv:
                    if (
                        self._exclusive_requested
                        and not self._exclusive_active
                        and not self._waiting
                        and self.send_q.empty()
                    ):
                        self._exclusive_active = True
                        self._exclusive_cv.notify_all()
                    exclusive_active = self._exclusive_active

                if exclusive_active:
                    time.sleep(LOOP_SLEEP_SEC)
                    continue

                # 1) RX pump (non-blocking, short timeout)
                try:
                    frame = read_len_prefixed_frame(sock)
                    self._handle_rx_frame(frame)
                except socket.timeout:
                    # Check if there's partial data stuck in the socket
                    pass
                except (ConnectionError, OSError) as e:
                    self._log_session(f"[{host_ts_full()}] [ERROR] Socket error: {e}\n")
                    self._reconnect()
                    sock = self._connect()
                    continue
                except ValueError as e:
                    self._log_session(f"[{host_ts_full()}] [ERROR] Frame parse: {e}\n")
                    # Drain any remaining unexpected data to prevent TCP window overflow
                    try:
                        sock.settimeout(0.001)  # Very short timeout for drain
                        while True:
                            chunk = sock.recv(4096)
                            if not chunk:
                                break
                    except socket.timeout:
                        pass
                    finally:
                        sock.settimeout(0.1)  # Restore normal timeout (100ms)

                # 2) If waiting for a response -> check per-try deadline
                if self._waiting:
                    if time.monotonic() >= self._try_deadline:
                        self._retry_count += 1
                        if self._retry_count < MAX_RETRIES:
                            # resend inflight
                            self._send_inflight(sock)
                        else:
                            # No response after all retries.
                            self._log_session(
                                f"[{host_ts_full()}] [WARN] No response after {MAX_RETRIES} tries"
                                f" (tag={self._inflight_tag}).\n"
                            )

                            # For MANUAL commands, prompt user to retry
                            if self._inflight_tag != "AUTO" and self.on_retry_prompt:
                                self.on_status(f"⚠ No response to {self._inflight_tag} command - waiting for user decision...")
                                # Ask user via callback (this will block until user responds)
                                should_retry = self.on_retry_prompt(self._inflight_hex, self._inflight_tag)

                                if should_retry:
                                    # User chose to retry - reset retry count and resend
                                    self._log_session(f"[{host_ts_full()}] [INFO] User chose to retry {self._inflight_tag} command\n")
                                    self.on_status(f"Retrying {self._inflight_tag} command...")
                                    self._retry_count = 0
                                    self._send_inflight(sock)
                                    continue
                                else:
                                    # User chose to skip
                                    self._log_session(f"[{host_ts_full()}] [INFO] User chose to skip {self._inflight_tag} command\n")
                                    self.on_status(f"⚠ Skipped {self._inflight_tag} command")
                            elif self._inflight_tag != "AUTO":
                                # No retry prompt callback - just notify
                                self.on_status(f"⚠ No response to {self._inflight_tag} command")

                            # Reset inflight without reconnecting
                            self._waiting = False
                            self._retry_count = 0
                            self._inflight_payload = b""
                            self._inflight_hex = ""
                            self._inflight_tag = "AUTO"
                            self._inflight_kind = "GET"
                            self._try_deadline = 0.0
                    time.sleep(LOOP_SLEEP_SEC)
                    continue

                # 3) Idle -> manual has priority
                try:
                    payload, tag, hex_str = self.send_q.get_nowait()
                    self._arm_inflight(payload, tag, hex_str)
                    self._send_inflight(sock)
                    time.sleep(LOOP_SLEEP_SEC)
                    continue
                except queue.Empty:
                    pass

                # 4) Idle -> streamer CPS poll on 100ms cadence (only if enabled)
                now_m = time.monotonic()
                if self.plot_enabled and now_m >= self._next_cps_poll:
                    self._next_cps_poll = now_m + CPS_POLL_INTERVAL_SEC
                    self._arm_inflight(self._cps_poll_bytes, "AUTO", CPS_POLL_HEX)
                    self._send_inflight(sock)

                time.sleep(LOOP_SLEEP_SEC)

        except Exception as e:
            msg = f"Connect/Run error: {type(e).__name__}: {e}"
            self.on_status(msg)
            self._log_session(f"[{host_ts_full()}] [ERROR] {msg}\n")
        finally:
            self._close_socket()
            self.on_status("Disconnected")
