from __future__ import annotations

import threading
import time
import queue
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Union

try:
    import usb.core
    import usb.util
    import usb.backend.libusb1

    def _candidate_libusb_paths() -> list[str]:
        candidates: list[str] = []
        exe_dir = Path(getattr(sys, "executable", "")).resolve().parent if getattr(sys, "executable", "") else None
        module_dir = Path(__file__).resolve().parent
        meipass = getattr(sys, "_MEIPASS", None)

        for base in (
            exe_dir,
            module_dir,
            module_dir.parent,
            Path.cwd(),
            Path(meipass) if meipass else None,
        ):
            if base is None:
                continue
            for rel_path in (
                Path("libusb-1.0.dll"),
                Path("libusb_package") / "libusb-1.0.dll",
                Path("libusb") / "_platform" / "windows" / "x86_64" / "libusb-1.0.dll",
                Path("libusb") / "_platform" / "windows" / "x86" / "libusb-1.0.dll",
                Path("libusb") / "_platform" / "windows" / "arm64" / "libusb-1.0.dll",
            ):
                candidate = base / rel_path
                if candidate.is_file():
                    candidates.append(str(candidate))
        return candidates

    def _get_libusb_backend():
        for dll_path in _candidate_libusb_paths():
            try:
                backend = usb.backend.libusb1.get_backend(
                    find_library=lambda _: dll_path
                )
                if backend is not None:
                    return backend
            except Exception:
                pass
        try:
            import libusb as _libusb_pkg
            backend = usb.backend.libusb1.get_backend(
                find_library=lambda _: _libusb_pkg.dll._name
            )
            if backend is not None:
                return backend
        except Exception:
            pass
        return usb.backend.libusb1.get_backend()   # system-wide search (Linux/Mac)

    _USB_BACKEND = _get_libusb_backend()
    HAS_USB = True
    # USBTimeoutError was added in pyusb 1.1.0; fall back to USBError for older
    _USB_TIMEOUT_EX = getattr(usb.core, "USBTimeoutError", usb.core.USBError)
except ImportError:
    HAS_USB = False
    _USB_BACKEND = None
    _USB_TIMEOUT_EX = Exception  # placeholder so the rest of the module parses

from protocol import (
    parse_cps_response_frame,
    parse_raw_rect_counts_response,
    parse_new_streamer_response,
    reassemble_new_streamer_message,
    split_cps_payload_lines,
    split_new_streamer_records,
    hex_dump_spaces,
    hex_dump_multiline,
    format_ts,
)
from logger import (
    SESSION_LOG_FILE, CPS_LOG_FILE, CPS_CSV_FILE, CPS_ALL_READS_CSV_FILE,
    append_text, csv_init_if_needed, csv_append_row, build_cps_csv_row,
)

# ── Device identity ────────────────────────────────────────────────────────────
USB_VID = 0x3985
USB_PID = 0x0007
USB_LANGID = 1033
USB_MANUFACTURER = "GRL Platform Solutions"
USB_PRODUCT = "V-QI"
USB_LEGACY_VID = 0x227F
USB_LEGACY_PID = 0x0006

# ── Endpoints ──────────────────────────────────────────────────────────────────
EP_OUT_CMD = 0x01   # Bulk OUT – API commands / GET frames / USB FW bridge ingress
EP_OUT_FW  = 0x01   # Bulk OUT – ESP/CPS FW bridge traffic uses the same EP1 OUT in current FW
EP_IN      = 0x81   # Bulk IN  – all device responses

# ── Timing (mirrors net_client constants) ─────────────────────────────────────
CPS_POLL_HEX          = "07 08 00 00 00 08 00 A1"
NEW_CPS_POLL_HEX      = "07 09 00 00 00 08 00 A1 01"
RAW_RECT_COUNTS_POLL_HEX = "07 09 00 00 00 07 00 02 00"
RAW_RECT_COUNTS_POLL_INTERVAL_SEC = 0.100
CPS_POLL_INTERVAL_SEC = 0.100
RX_WAIT_PER_TRY_SEC   = 0.500
MAX_RETRIES           = 3
LOOP_SLEEP_SEC        = 0.002

# ── USB transfer tunables ──────────────────────────────────────────────────────
USB_READ_TIMEOUT_MS   = 100    # ms – individual bulk-IN read
USB_WRITE_TIMEOUT_MS  = 1000   # ms – bulk-OUT write
USB_CTRL_TIMEOUT_MS   = 1000   # ms – EP0 control transfers
USB_MAX_READ          = 65536  # bytes – max per bulk-IN call

USB_FW_WRITE_TIMEOUT_MS_DEFAULT = 5000
USB_FW_OUT_CHUNK_SIZE_DEFAULT = 64
USB_FW_IN_CHUNK_SIZE_DEFAULT = 64
USB_HS_OUT_PAD_BYTES = 512
USB_VENDOR_REQ_BULK_RESET = 0x5A
USB_VENDOR_OUT = 0x40

MAX_FRAME_LEN = 65535

# CPS frame marker at frame[3:8]
_CPS_MARKER = bytes.fromhex("00000800A1")
def _host_ts_full() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _bytes_from_hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", "").strip())


def _usb_speed_name(dev) -> str:
    speed = getattr(dev, "speed", None)
    if not HAS_USB or speed is None:
        return "unknown"
    names = {
        getattr(usb.core, "SPEED_LOW", object()): "LS",
        getattr(usb.core, "SPEED_FULL", object()): "FS",
        getattr(usb.core, "SPEED_HIGH", object()): "HS",
        getattr(usb.core, "SPEED_SUPER", object()): "SS",
        getattr(usb.core, "SPEED_SUPER_PLUS", object()): "SS+",
    }
    return names.get(speed, str(speed))


def _usb_endpoint_mps(dev, ep_addr: int, default: int = USB_FW_OUT_CHUNK_SIZE_DEFAULT) -> int:
    try:
        cfg = dev.get_active_configuration()
        for intf in cfg:
            for ep in intf:
                if int(ep.bEndpointAddress) == int(ep_addr):
                    mps = int(ep.wMaxPacketSize)
                    if mps > 0:
                        return mps
    except Exception:
        pass
    return int(default)


def usb_fw_out_chunk_size(dev) -> int:
    return _usb_endpoint_mps(dev, EP_OUT_FW, USB_FW_OUT_CHUNK_SIZE_DEFAULT)


def usb_fw_in_chunk_size(dev) -> int:
    return _usb_endpoint_mps(dev, EP_IN, USB_FW_IN_CHUNK_SIZE_DEFAULT)


def _usb_safe_out_write_size(mps: int) -> int:
    mps = int(mps or USB_FW_OUT_CHUNK_SIZE_DEFAULT)
    return max(1, mps, USB_HS_OUT_PAD_BYTES)


def _iter_usb_out_writes(data: bytes, mps: int):
    chunk_size = _usb_safe_out_write_size(mps)
    for offset in range(0, len(data), chunk_size):
        yield data[offset: offset + chunk_size]


def _usb_read_nonzero(dev, ep_addr: int, size: int, timeout_ms: int):
    deadline = time.monotonic() + max(0.001, timeout_ms / 1000.0)
    last_timeout = None

    while True:
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        try:
            raw = dev.read(ep_addr, size, timeout=remaining_ms)
        except _USB_TIMEOUT_EX as exc:
            last_timeout = exc
            if time.monotonic() >= deadline:
                raise
            continue

        if raw:
            return raw

        # Bulk-IN ZLP: transfer terminator only, not an API frame.
        if time.monotonic() >= deadline:
            if last_timeout is not None:
                raise last_timeout
            return raw


def _recover_usb_pipe_after_timeout(dev, ep_addr: int) -> str:
    """Best-effort host recovery after a failed transfer only."""
    actions: list[str] = []
    try:
        dev.ctrl_transfer(
            USB_VENDOR_OUT,
            USB_VENDOR_REQ_BULK_RESET,
            int(ep_addr) & 0xFFFF,
            0,
            b"",
            timeout=USB_CTRL_TIMEOUT_MS,
        )
        actions.append("vendor_bulk_reset")
        time.sleep(0.010)
    except Exception as ctrl_exc:
        actions.append(f"vendor_bulk_reset failed: {ctrl_exc!r}")

    try:
        dev.clear_halt(ep_addr)
        actions.append(f"clear_halt(0x{ep_addr:02X})")
        return ", ".join(actions)
    except Exception as clear_exc:
        actions.append(f"clear_halt(0x{ep_addr:02X}) failed: {clear_exc!r}")

    try:
        dev.reset()
        actions.append("device_reset")
    except Exception as reset_exc:
        actions.append(f"device_reset failed: {reset_exc!r}")
    return ", ".join(actions)


class USBFlashBridge:
    """
    Raw bulk USB transport for ESP flashing over the STM firmware bridge.

    It exposes a socket-like subset used by `esp_flasher.ESPFlasher`:
    `sendall()`, `recv()`, `settimeout()`, and `close()`.
    """

    def __init__(self, vid: int = USB_VID, pid: int = USB_PID):
        if not HAS_USB:
            raise RuntimeError(
                "pyusb is not installed. Run: pip install pyusb\n"
                "Also ensure a libusb-compatible driver is installed for the selected VID/PID."
            )
        if _USB_BACKEND is None:
            raise RuntimeError(
                "libusb backend not found.\n"
                "Run:  pip install libusb\n"
                "If already installed, place libusb-1.0.dll next to app.py."
            )

        self.usb_vid = int(vid) & 0xFFFF
        self.usb_pid = int(pid) & 0xFFFF
        dev = usb.core.find(idVendor=self.usb_vid, idProduct=self.usb_pid, backend=_USB_BACKEND)
        if dev is None:
            raise RuntimeError(
                f"USB device VID={self.usb_vid:04X}/PID={self.usb_pid:04X} not found.\n"
                "Check: cable plugged and driver bound to this VID/PID."
            )
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, AttributeError):
            pass

        dev.set_configuration()
        self._dev = dev
        self._timeout_s = 1.0
        self._rx_buf = bytearray()
        self.fw_write_timeout_ms = USB_FW_WRITE_TIMEOUT_MS_DEFAULT
        self.fw_out_chunk_size = usb_fw_out_chunk_size(dev)
        self.fw_out_chunk_size_detected = self.fw_out_chunk_size
        self.fw_in_chunk_size = usb_fw_in_chunk_size(dev)
        self.fw_in_chunk_size_detected = self.fw_in_chunk_size
        self.usb_speed_name = _usb_speed_name(dev)
        self.safe_out_write_size = _usb_safe_out_write_size(self.fw_out_chunk_size)
        self.pad_fw_writes_to_bytes = _usb_safe_out_write_size(self.fw_out_chunk_size)

    def settimeout(self, timeout: float):
        self._timeout_s = max(float(timeout), 0.0)

    def sendall(self, data: bytes):
        timeout_ms = max(int(self.fw_write_timeout_ms), int(max(self._timeout_s, 1.0) * 1000))
        written_total = 0
        for wire in _iter_usb_out_writes(data, self.fw_out_chunk_size):
            try:
                written = self._dev.write(EP_OUT_FW, wire, timeout=timeout_ms)
            except _USB_TIMEOUT_EX as exc:
                recovery = _recover_usb_pipe_after_timeout(self._dev, EP_OUT_FW)
                raise ConnectionError(
                    f"USB FW OUT write timed out/failed "
                    f"(frame_len={len(data)}, chunk_len={len(wire)}, written_total={written_total}, "
                    f"safe_out={self.safe_out_write_size}, timeout={timeout_ms} ms, "
                    f"err={exc!r}, recovery={recovery})"
                ) from exc
            except Exception as exc:
                raise ConnectionError(
                    f"USB FW OUT write failed "
                    f"(frame_len={len(data)}, chunk_len={len(wire)}, written_total={written_total}, "
                    f"safe_out={self.safe_out_write_size}, timeout={timeout_ms} ms, err={exc!r})"
                ) from exc
            if written != len(wire):
                raise ConnectionError(
                    f"USB FW OUT short write ({written}/{len(wire)} bytes, "
                    f"frame_len={len(data)}, written_total={written_total})"
                )
            written_total += written

    def recv(self, size: int) -> bytes:
        take = max(1, int(size))
        if self._rx_buf:
            out = bytes(self._rx_buf[:take])
            del self._rx_buf[:take]
            return out
        read_len = max(1, int(getattr(self, "fw_in_chunk_size_detected", 64) or 64))
        try:
            raw = _usb_read_nonzero(
                self._dev,
                EP_IN,
                read_len,
                max(1, int(self._timeout_s * 1000)),
            )
            if not raw:
                raise TimeoutError("USB bulk read returned only ZLP before timeout")
            self._rx_buf.extend(raw)
            out = bytes(self._rx_buf[:take])
            del self._rx_buf[:take]
            return out
        except TimeoutError as exc:
            recovery = _recover_usb_pipe_after_timeout(self._dev, EP_IN)
            raise TimeoutError(f"USB bulk read timed out (recovery={recovery})") from exc
        except _USB_TIMEOUT_EX as exc:
            recovery = _recover_usb_pipe_after_timeout(self._dev, EP_IN)
            raise TimeoutError(f"USB bulk read timed out (recovery={recovery})") from exc

    def discard_zero_padding(self) -> None:
        while self._rx_buf and self._rx_buf[0] == 0:
            del self._rx_buf[:1]

    def close(self):
        dev, self._dev = self._dev, None
        if dev is None:
            return
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass


class USBExclusiveBridge:
    """
    Socket-like wrapper over an already-open USBClient session that has granted
    exclusive access to the underlying device.
    """

    def __init__(self, client: "USBClient", owner: str):
        self._client = client
        self._owner = owner
        self._timeout_s = 1.0
        self._closed = False

    def settimeout(self, timeout: float):
        self._timeout_s = max(float(timeout), 0.0)

    def sendall(self, data: bytes):
        self._client._exclusive_write(self._owner, data, self._timeout_s)

    def recv(self, size: int) -> bytes:
        return self._client._exclusive_read(self._owner, size, self._timeout_s)

    def discard_zero_padding(self) -> None:
        self._client._exclusive_discard_zero_padding(self._owner)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._client.release_exclusive_device(self._owner)

    @property
    def fw_out_chunk_size(self) -> int:
        return self._client.fw_out_chunk_size

    @fw_out_chunk_size.setter
    def fw_out_chunk_size(self, value: int) -> None:
        self._client.fw_out_chunk_size = int(value)

    @property
    def fw_out_chunk_size_detected(self) -> int:
        return self._client.fw_out_chunk_size_detected

    @property
    def usb_speed_name(self) -> str:
        return self._client.usb_speed_name

    @property
    def fw_write_timeout_ms(self) -> int:
        return self._client.fw_write_timeout_ms

    @fw_write_timeout_ms.setter
    def fw_write_timeout_ms(self, value: int) -> None:
        self._client.fw_write_timeout_ms = int(value)

    @property
    def pad_fw_writes_to_bytes(self) -> int:
        return self._client.pad_fw_writes_to_bytes

    @pad_fw_writes_to_bytes.setter
    def pad_fw_writes_to_bytes(self, value: int) -> None:
        self._client.pad_fw_writes_to_bytes = int(value)


class USBClient:
    """
    WinUSB / libusb-backed USB client for a configurable VID/PID.

    Drop-in duck-type replacement for NetClient:
      • Same constructor callbacks (on_status, on_cps, on_tx, on_rx, on_retry_prompt)
      • Same public methods: start(), stop(), send_hex(), send_bytes()
      • Same attribute: plot_enabled (bool)

    Extra public method:
      send_control(bmRequestType, bRequest, wValue, wIndex, data_or_len)
          → EP0 control transfer; returns bytes (IN) or None (OUT)

    Frame routing by start byte:
      0x01 or 0x07  →  Bulk OUT EP 0x01
      0x02          →  Bulk OUT EP 0x02
    All responses come from Bulk IN EP 0x81.
    """

    def __init__(
        self,
        on_status: Callable[[str], None],
        on_cps: Callable[[dict], None],
        on_raw_counts: Optional[Callable[[dict], None]] = None,
        on_tx: Optional[Callable[[str, str], None]] = None,
        on_rx: Optional[Callable[[str, str], None]] = None,
        on_retry_prompt: Optional[Callable[[str, str], bool]] = None,
        vid: int = USB_VID,
        pid: int = USB_PID,
    ):
        if not HAS_USB:
            raise RuntimeError(
                "pyusb is not installed. Run: pip install pyusb\n"
                "Also ensure a libusb-compatible driver is installed for the selected VID/PID."
            )

        self.on_status       = on_status
        self.on_cps          = on_cps
        self.on_raw_counts   = on_raw_counts
        self.on_tx           = on_tx
        self.on_rx           = on_rx
        self.on_retry_prompt = on_retry_prompt
        self.usb_vid         = int(vid) & 0xFFFF
        self.usb_pid         = int(pid) & 0xFFFF

        self._stop   = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Manual TX queue: (payload_bytes, tag, hex_str_for_log)
        self.send_q: "queue.Queue[tuple[bytes, str, str]]" = queue.Queue()

        self._session_lock = threading.Lock()
        self._cps_lock     = threading.Lock()
        self._csv_lock     = threading.Lock()

        self._cps_poll_bytes = _bytes_from_hex(CPS_POLL_HEX)
        self._next_cps_poll  = 0.0
        self.plot_enabled: bool = False
        self.raw_counts_enabled: bool = False
        self._next_raw_counts_poll = 0.0
        self._prefer_raw_counts_poll = False
        self.new_streamer_enabled: bool = False
        self.reassemble_streamer_messages: bool = False
        self._streamer_control_on = False
        self._streamer_control_pending = False
        self._streamer_control_done = threading.Event()

        # In-flight tracking (mirrors net_client)
        self._waiting          = False
        self._inflight_payload: bytes = b""
        self._inflight_hex:     str   = ""
        self._inflight_tag:     str   = "AUTO"
        self._inflight_kind:    str   = "GET"
        self._retry_count:      int   = 0
        self._try_deadline:     float = 0.0

        self._dev = None           # usb.core.Device, set by _open_device()
        self._rx_buf = bytearray() # reassembly buffer for bulk-IN data
        self.fw_write_timeout_ms = USB_FW_WRITE_TIMEOUT_MS_DEFAULT
        self.fw_out_chunk_size = USB_FW_OUT_CHUNK_SIZE_DEFAULT
        self.fw_out_chunk_size_detected = USB_FW_OUT_CHUNK_SIZE_DEFAULT
        self.fw_in_chunk_size = USB_FW_IN_CHUNK_SIZE_DEFAULT
        self.fw_in_chunk_size_detected = USB_FW_IN_CHUNK_SIZE_DEFAULT
        self.usb_speed_name = "unknown"
        self.safe_out_write_size = _usb_safe_out_write_size(self.fw_out_chunk_size)
        self.pad_fw_writes_to_bytes = self.safe_out_write_size
        self._exclusive_cv = threading.Condition()
        self._exclusive_rx_buf = bytearray()
        self._exclusive_requested = False
        self._exclusive_active = False
        self._exclusive_owner = ""
        self._exclusive_prev_plot_enabled = False
        self._runtime_blocked = False
        self._runtime_block_owner = ""
        self._runtime_prev_plot_enabled = False
        self._cps_line_buf = bytearray()
        self._new_stream_buf = bytearray()
        self._new_message_buf = bytearray()
        self._new_message_info: dict = {}
        self._new_expected_read: int | None = None
        self._cps_csv_frame_index = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self):
        self._stop.clear()
        try:
            csv_init_if_needed(self._csv_lock, CPS_CSV_FILE)
            csv_init_if_needed(self._csv_lock, CPS_ALL_READS_CSV_FILE)
        except PermissionError as exc:
            self._log_session(
                f"[{_host_ts_full()}] [WARN] CPS CSV is locked; connection will continue "
                f"without CSV writes until it is unlocked: {exc}\n"
            )
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread and self._thread.is_alive():
            if self._streamer_control_on:
                self.set_streamer_control(False)
            if self._streamer_control_pending:
                self._streamer_control_done.wait(timeout=1.6)
        self._stop.set()
        with self._exclusive_cv:
            self._exclusive_cv.notify_all()
        self._release_device()

    def send_hex(self, hex_str: str, tag: str = "MANUAL"):
        b = _bytes_from_hex(hex_str)
        with self._exclusive_cv:
            if self._runtime_blocked:
                owner = self._runtime_block_owner or "firmware update"
                raise RuntimeError(f"Runtime API traffic is blocked by {owner}")
            if self._exclusive_requested or self._exclusive_active:
                owner = self._exclusive_owner or "another operation"
                raise RuntimeError(f"USB device is reserved for {owner}")
        self.send_q.put((b, tag, hex_str.strip()))

    def send_bytes(self, b: bytes, tag: str = "MANUAL"):
        with self._exclusive_cv:
            if self._runtime_blocked:
                owner = self._runtime_block_owner or "firmware update"
                raise RuntimeError(f"Runtime API traffic is blocked by {owner}")
            if self._exclusive_requested or self._exclusive_active:
                owner = self._exclusive_owner or "another operation"
                raise RuntimeError(f"USB device is reserved for {owner}")
        self.send_q.put((b, tag, hex_dump_spaces(b)))

    def configure_streamer_format(self, use_new: bool):
        self.new_streamer_enabled = bool(use_new)
        self._cps_line_buf.clear()
        self._new_stream_buf.clear()
        self._new_message_buf.clear()
        self._new_message_info.clear()
        self._new_expected_read = None

    def configure_streamer_message_reassembly(self, enabled: bool):
        self.reassemble_streamer_messages = bool(enabled)
        self._new_message_buf.clear()
        self._new_message_info.clear()

    def set_streamer_control(self, enabled: bool):
        if self._streamer_control_pending and self._thread and self._thread.is_alive():
            self._streamer_control_done.wait(timeout=1.6)
        value = 0x01 if enabled else 0x00
        frame = bytes.fromhex("01 09 00 01 00 A1 00 02") + bytes([value])
        self._streamer_control_done.clear()
        self._streamer_control_on = bool(enabled)
        self._streamer_control_pending = True
        self.send_q.put((frame, "AUTO", hex_dump_spaces(frame)))

    def set_runtime_api_blocked(self, blocked: bool, owner: str = "firmware update"):
        with self._exclusive_cv:
            if blocked:
                if not self._runtime_blocked:
                    self._runtime_prev_plot_enabled = self.plot_enabled
                self._runtime_blocked = True
                self._runtime_block_owner = owner
                self.plot_enabled = False
                while True:
                    try:
                        self.send_q.get_nowait()
                    except queue.Empty:
                        break
                self._waiting = False
                self._retry_count = 0
                self._inflight_payload = b""
                self._inflight_hex = ""
                self._inflight_tag = "AUTO"
                self._inflight_kind = "GET"
                self._try_deadline = 0.0
                self._rx_buf.clear()
                self._cps_line_buf.clear()
                self._cps_csv_frame_index = 0
                self._next_cps_poll = time.monotonic() + CPS_POLL_INTERVAL_SEC
                self._log_session(
                    f"[{_host_ts_full()}] [INFO] Runtime API traffic blocked by {owner}\n"
                )
            else:
                if self._runtime_blocked:
                    self._log_session(
                        f"[{_host_ts_full()}] [INFO] Runtime API traffic unblocked by {owner}\n"
                    )
                self._runtime_blocked = False
            self._runtime_block_owner = ""
            self.plot_enabled = self._runtime_prev_plot_enabled
            self._runtime_prev_plot_enabled = False
            self._next_cps_poll = time.monotonic() + CPS_POLL_INTERVAL_SEC
            self._cps_line_buf.clear()
            self._exclusive_cv.notify_all()

    def acquire_exclusive_device(self, owner: str, timeout: float = 10.0) -> USBExclusiveBridge:
        deadline = time.monotonic() + max(timeout, 0.1)
        with self._exclusive_cv:
            if self._dev is None:
                raise RuntimeError("USB device is not connected")
            if self._exclusive_requested or self._exclusive_active:
                active_owner = self._exclusive_owner or "another operation"
                raise RuntimeError(f"USB device is already reserved for {active_owner}")

            self._exclusive_requested = True
            self._exclusive_owner = owner
            self._exclusive_prev_plot_enabled = self.plot_enabled
            self.plot_enabled = False
            self._log_session(
                f"[{_host_ts_full()}] [INFO] Exclusive USB requested by {owner}\n"
            )
            self._exclusive_cv.notify_all()

            while True:
                if self._exclusive_active and self._dev is not None:
                    self._log_session(
                        f"[{_host_ts_full()}] [INFO] Exclusive USB granted to {owner}\n"
                    )
                    return USBExclusiveBridge(self, owner)
                if self._stop.is_set():
                    self._exclusive_requested = False
                    self._exclusive_owner = ""
                    self.plot_enabled = self._exclusive_prev_plot_enabled
                    self._exclusive_prev_plot_enabled = False
                    self._exclusive_cv.notify_all()
                    raise RuntimeError("USB client stopped while waiting for exclusive access")

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._exclusive_requested = False
                    self._exclusive_owner = ""
                    self.plot_enabled = self._exclusive_prev_plot_enabled
                    self._exclusive_prev_plot_enabled = False
                    self._exclusive_cv.notify_all()
                    raise TimeoutError("Timed out waiting for exclusive USB access")
                self._exclusive_cv.wait(timeout=min(0.2, remaining))

    def release_exclusive_device(self, owner: str):
        with self._exclusive_cv:
            if not self._exclusive_requested and not self._exclusive_active:
                return
            if self._exclusive_owner and owner != self._exclusive_owner:
                raise RuntimeError(
                    f"USB device is reserved for {self._exclusive_owner}, not {owner}"
                )

            self._log_session(
                f"[{_host_ts_full()}] [INFO] Exclusive USB released by {owner}\n"
            )
            self._exclusive_requested = False
            self._exclusive_active = False
            self._exclusive_owner = ""
            self.safe_out_write_size = _usb_safe_out_write_size(self.fw_out_chunk_size_detected)
            self.plot_enabled = self._exclusive_prev_plot_enabled
            self._exclusive_prev_plot_enabled = False
            self._waiting = False
            self._retry_count = 0
            self._inflight_payload = b""
            self._inflight_hex = ""
            self._inflight_tag = "AUTO"
            self._inflight_kind = "GET"
            self._try_deadline = 0.0
            self._rx_buf.clear()
            self._exclusive_rx_buf.clear()
            self._cps_line_buf.clear()
            self._cps_csv_frame_index = 0
            self._next_cps_poll = time.monotonic() + CPS_POLL_INTERVAL_SEC
            self._exclusive_cv.notify_all()

    def _exclusive_write(self, owner: str, data: bytes, timeout_s: float):
        with self._exclusive_cv:
            if not self._exclusive_active or owner != self._exclusive_owner:
                raise RuntimeError("USB exclusive access is not active")
            dev = self._dev
        if dev is None:
            raise RuntimeError("USB device is not open")

        timeout_ms = max(int(self.fw_write_timeout_ms), int(max(timeout_s, 1.0) * 1000))
        written_total = 0
        for wire in _iter_usb_out_writes(data, self.fw_out_chunk_size_detected):
            try:
                written = dev.write(EP_OUT_FW, wire, timeout=timeout_ms)
            except _USB_TIMEOUT_EX as exc:
                recovery = _recover_usb_pipe_after_timeout(dev, EP_OUT_FW)
                raise ConnectionError(
                    f"USB FW OUT write timed out/failed "
                    f"(frame_len={len(data)}, chunk_len={len(wire)}, written_total={written_total}, "
                    f"safe_out={self.safe_out_write_size}, timeout={timeout_ms} ms, "
                    f"err={exc!r}, recovery={recovery})"
                ) from exc
            except Exception as exc:
                raise ConnectionError(
                    f"USB FW OUT write failed "
                    f"(frame_len={len(data)}, chunk_len={len(wire)}, written_total={written_total}, "
                    f"safe_out={self.safe_out_write_size}, timeout={timeout_ms} ms, err={exc!r})"
                ) from exc
            if written != len(wire):
                raise ConnectionError(
                    f"USB FW OUT short write ({written}/{len(wire)} bytes, "
                    f"frame_len={len(data)}, written_total={written_total})"
                )
            written_total += written

    def _exclusive_read(self, owner: str, size: int, timeout_s: float) -> bytes:
        with self._exclusive_cv:
            if not self._exclusive_active or owner != self._exclusive_owner:
                raise RuntimeError("USB exclusive access is not active")
            dev = self._dev
        if dev is None:
            raise RuntimeError("USB device is not open")
        take = max(1, int(size))
        if self._exclusive_rx_buf:
            out = bytes(self._exclusive_rx_buf[:take])
            del self._exclusive_rx_buf[:take]
            return out
        read_len = max(1, int(getattr(self, "fw_in_chunk_size_detected", 64) or 64))
        try:
            raw = _usb_read_nonzero(
                dev,
                EP_IN,
                read_len,
                max(1, int(max(timeout_s, 0.001) * 1000)),
            )
            if not raw:
                raise TimeoutError("USB bulk read returned only ZLP before timeout")
            self._exclusive_rx_buf.extend(raw)
            out = bytes(self._exclusive_rx_buf[:take])
            del self._exclusive_rx_buf[:take]
            return out
        except TimeoutError as exc:
            recovery = _recover_usb_pipe_after_timeout(dev, EP_IN)
            raise TimeoutError(f"USB bulk read timed out (recovery={recovery})") from exc
        except _USB_TIMEOUT_EX as exc:
            recovery = _recover_usb_pipe_after_timeout(dev, EP_IN)
            raise TimeoutError(f"USB bulk read timed out (recovery={recovery})") from exc

    def _exclusive_discard_zero_padding(self, owner: str) -> None:
        with self._exclusive_cv:
            if not self._exclusive_active or owner != self._exclusive_owner:
                raise RuntimeError("USB exclusive access is not active")
            while self._exclusive_rx_buf and self._exclusive_rx_buf[0] == 0:
                del self._exclusive_rx_buf[:1]

    def send_control(
        self,
        bm_request_type: int,
        b_request:        int,
        w_value:          int,
        w_index:          int,
        data_or_len:      Union[bytes, int],
        timeout_ms:       int = USB_CTRL_TIMEOUT_MS,
    ) -> Optional[bytes]:
        """
        Issue a USB control transfer on EP0.

        For OUT transfers  – pass data_or_len as bytes (payload).
        For IN  transfers  – pass data_or_len as int   (bytes to read).
        Returns bytes received (IN) or None (OUT).

        Raises RuntimeError if not connected.
        """
        if self._dev is None:
            raise RuntimeError("USB device is not open")
        result = self._dev.ctrl_transfer(
            bm_request_type, b_request, w_value, w_index,
            data_or_len, timeout=timeout_ms,
        )
        # ctrl_transfer returns an array.array for IN; convert for callers
        if result is not None and not isinstance(result, (bytes, int)):
            return bytes(result)
        return result

    # ── Internal helpers ───────────────────────────────────────────────────────

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

    def _ep_for_payload(self, payload: bytes) -> int:
        """Current firmware exposes a single bulk OUT endpoint (EP1 OUT)."""
        return EP_OUT_CMD

    # ── Device lifecycle ───────────────────────────────────────────────────────

    def _open_device(self):
        self.on_status(f"Opening USB VID={self.usb_vid:04X} PID={self.usb_pid:04X} …")
        if _USB_BACKEND is None:
            raise RuntimeError(
                "libusb backend not found.\n"
                "Run:  pip install libusb\n"
                "If already installed, place libusb-1.0.dll next to app.py."
            )
        dev = usb.core.find(idVendor=self.usb_vid, idProduct=self.usb_pid, backend=_USB_BACKEND)
        if dev is None:
            # List all visible devices to help diagnosis
            all_devs = list(usb.core.find(find_all=True, backend=_USB_BACKEND))
            visible = ", ".join(
                f"VID={d.idVendor:04X}/PID={d.idProduct:04X}" for d in all_devs
            ) or "none"
            raise RuntimeError(
                f"USB device VID={self.usb_vid:04X}/PID={self.usb_pid:04X} not found.\n"
                f"Devices visible to libusb: {visible}\n"
                "Check: cable plugged and driver bound to this VID/PID."
            )
        # Detach any kernel driver (Linux/Mac; no-op on Windows)
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, AttributeError):
            pass
        dev.set_configuration()
        self._dev    = dev
        self._rx_buf = bytearray()
        self._cps_line_buf.clear()
        self._cps_csv_frame_index = 0
        self.fw_out_chunk_size = usb_fw_out_chunk_size(dev)
        self.fw_out_chunk_size_detected = self.fw_out_chunk_size
        self.fw_in_chunk_size = usb_fw_in_chunk_size(dev)
        self.fw_in_chunk_size_detected = self.fw_in_chunk_size
        self.usb_speed_name = _usb_speed_name(dev)
        self.safe_out_write_size = _usb_safe_out_write_size(self.fw_out_chunk_size)
        self.pad_fw_writes_to_bytes = self.safe_out_write_size
        self._log_session(
            f"[{_host_ts_full()}] [INFO] USB opened "
            f"VID={self.usb_vid:04X} PID={self.usb_pid:04X} "
            f"EP_OUT_CMD=0x{EP_OUT_CMD:02X} EP_OUT_FW=0x{EP_OUT_FW:02X} "
            f"EP_IN=0x{EP_IN:02X} SPEED={self.usb_speed_name} "
            f"FW_OUT_MPS={self.fw_out_chunk_size} "
            f"FW_IN_MPS={self.fw_in_chunk_size} "
            f"SAFE_OUT_WRITE={self.safe_out_write_size}\n"
        )
        self.on_status(
            f"USB Connected ✅  ({self.usb_speed_name}, EP1 OUT MPS {self.fw_out_chunk_size}, "
            f"EP1 IN MPS {self.fw_in_chunk_size})"
        )
        self._next_cps_poll = time.monotonic() + CPS_POLL_INTERVAL_SEC

    def _release_device(self):
        dev, self._dev = self._dev, None
        if dev is None:
            return
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass

    # ── RX reassembly ──────────────────────────────────────────────────────────

    def _usb_read_into_buf(self) -> bool:
        """
        Non-blocking bulk-IN read.  Returns True if bytes were appended.
        Raises USBError on hard errors; silently returns False on timeout.
        """
        try:
            read_len = max(1, int(getattr(self, "fw_in_chunk_size_detected", 64) or 64))
            raw = self._dev.read(EP_IN, read_len, timeout=USB_READ_TIMEOUT_MS)
            if not raw:
                print("[USB DEBUG] RX ZLP ignored")
                return False
            self._rx_buf.extend(raw)
            print(f"[USB DEBUG] RX {len(raw)} bytes: "
                  f"{' '.join(f'{b:02X}' for b in raw[:32])}"
                  f"{'…' if len(raw) > 32 else ''}")
            return bool(raw)
        except _USB_TIMEOUT_EX:
            return False

    def _try_extract_frame(self) -> Optional[bytes]:
        """
        Extract one complete length-prefixed frame from self._rx_buf, or None.
        Discards unrecognised bytes (bad start byte) with a debug print.
        """
        buf = self._rx_buf
        while len(buf) >= 1 and buf[0] not in (0x07, 0x01, 0x02):
            print(f"[USB DEBUG] Bad start byte 0x{buf[0]:02X}, discarding")
            del buf[:1]
        if len(buf) < 3:
            return None
        total_len = buf[1] | (buf[2] << 8)
        if total_len < 3 or total_len > MAX_FRAME_LEN:
            print(f"[USB DEBUG] Bad frame length {total_len}, discarding 3-byte header")
            del buf[:3]
            return None
        if len(buf) < total_len:
            return None          # wait for more data
        frame = bytes(buf[:total_len])
        del buf[:total_len]
        while buf and buf[0] == 0:
            del buf[:1]
        print(f"[USB DEBUG] Frame extracted {total_len}B: "
              f"{' '.join(f'{b:02X}' for b in frame)}")
        return frame

    # ── In-flight state machine (mirrors net_client) ───────────────────────────

    def _kind_from_payload(self, payload: bytes) -> str:
        if not payload:
            return "GET"
        return "SET" if payload[0] in (0x01, 0x02) else "GET"

    def _arm_inflight(self, payload: bytes, tag: str, hex_str: str):
        self._waiting          = True
        self._inflight_payload = payload
        self._inflight_hex     = hex_str
        self._inflight_tag     = tag
        self._inflight_kind    = self._kind_from_payload(payload)
        self._retry_count      = 0
        self._try_deadline     = time.monotonic() + RX_WAIT_PER_TRY_SEC

    def _send_inflight(self, dev):
        ep = self._ep_for_payload(self._inflight_payload)
        timeout_ms = USB_WRITE_TIMEOUT_MS
        if self._inflight_payload and self._inflight_payload[0] == 0x02:
            timeout_ms = max(timeout_ms, int(self.fw_write_timeout_ms))
        written_total = 0
        wire_chunks = 0
        for wire in _iter_usb_out_writes(self._inflight_payload, self.fw_out_chunk_size_detected):
            wire_chunks += 1
            try:
                written = dev.write(ep, wire, timeout=timeout_ms)
            except _USB_TIMEOUT_EX as exc:
                recovery = _recover_usb_pipe_after_timeout(dev, ep)
                raise ConnectionError(
                    f"USB write timed out/failed "
                    f"(frame_len={len(self._inflight_payload)}, chunk_len={len(wire)}, "
                    f"written_total={written_total}, safe_out={self.safe_out_write_size}, "
                    f"ep=0x{ep:02X}, timeout={timeout_ms} ms, err={exc!r}, recovery={recovery})"
                ) from exc
            except Exception as exc:
                raise ConnectionError(
                    f"USB write failed "
                    f"(frame_len={len(self._inflight_payload)}, chunk_len={len(wire)}, "
                    f"written_total={written_total}, safe_out={self.safe_out_write_size}, "
                    f"ep=0x{ep:02X}, timeout={timeout_ms} ms, err={exc!r})"
                ) from exc
            if written != len(wire):
                raise ConnectionError(
                    f"USB short write ({written}/{len(wire)} bytes, "
                    f"frame_len={len(self._inflight_payload)}, written_total={written_total}, "
                    f"ep=0x{ep:02X}, timeout={timeout_ms} ms)"
                )
            written_total += written
        now = _host_ts_full()
        self._log_session(
            f"[{now}] [TX:{self._inflight_tag}] {self._inflight_hex} "
            f"(ep=0x{ep:02X} try={self._retry_count + 1}/{MAX_RETRIES} "
            f"frame_len={len(self._inflight_payload)} wire_len={written_total} "
            f"wire_chunks={wire_chunks} safe_out={self.safe_out_write_size} "
            f"timeout={timeout_ms}ms)\n"
        )
        self._emit_tx(self._inflight_hex, self._inflight_tag)
        self._try_deadline = time.monotonic() + RX_WAIT_PER_TRY_SEC

    def _rx_accepts_as_response(self, frame: bytes) -> bool:
        if self._inflight_kind == "SET":
            return len(frame) >= 1 and frame[0] in (0x01, 0x02)
        return True   # GET: any frame satisfies

    def _handle_rx_frame(self, frame: bytes):
        completed_streamer_control = (
            self._inflight_kind == "SET" and self._streamer_control_pending
        )
        rx_hex = hex_dump_spaces(frame)
        if self._inflight_kind == "SET":
            rx_tag = "AUTO_ECHO"  if self._inflight_tag == "AUTO" else "MANUAL_ECHO"
        else:
            rx_tag = "AUTO_RSP"   if self._inflight_tag == "AUTO" else "MANUAL_RSP"

        now = _host_ts_full()
        self._log_session(f"[{now}] [RX:{rx_tag}] {rx_hex}\n")
        self._emit_rx(rx_hex, rx_tag)

        if self._waiting and self._rx_accepts_as_response(frame):
            self._waiting          = False
            self._inflight_payload = b""
            self._inflight_hex     = ""
            self._inflight_tag     = "AUTO"
            self._inflight_kind    = "GET"
            self._retry_count      = 0
            self._try_deadline     = 0.0
            if completed_streamer_control:
                self._streamer_control_pending = False
                self._streamer_control_done.set()

        if len(frame) >= 13 and frame[3:9] == bytes.fromhex("00 00 07 00 02 00"):
            info = parse_raw_rect_counts_response(frame)
            info["_host_ts_full"] = now
            if self.on_raw_counts:
                self.on_raw_counts(info)
            return

        if len(frame) >= 9 and frame[5:9] == bytes.fromhex("08 00 A1 01"):
            response_info = parse_new_streamer_response(frame)
            discontinuity = (self._new_expected_read is not None
                    and response_info["valid_stream_byte_count"]
                    and response_info["read_index"] != self._new_expected_read)
            if discontinuity:
                self._log_session(
                    f"[{now}] [WARN] Streamer read-index discontinuity: "
                    f"expected={self._new_expected_read}, got={response_info['read_index']}\n"
                )
            if response_info["streamer_overflow"]:
                self._log_session(f"[{now}] [WARN] Streamer overflow/data loss reported\n")
            if discontinuity or response_info["streamer_overflow"]:
                self._new_message_buf.clear()
                self._new_message_info.clear()
            self._new_expected_read = response_info["expected_next_read"]
            for packet_info in split_new_streamer_records(response_info, self._new_stream_buf):
                self._cps_csv_frame_index += 1
                packet_info["_host_ts_full"] = now
                packet_info["_rx_tag"] = rx_tag
                packet_info["_csv_frame_index"] = self._cps_csv_frame_index
                try:
                    csv_append_row(self._csv_lock, CPS_ALL_READS_CSV_FILE,
                                   build_cps_csv_row(now, packet_info))
                except PermissionError as csv_err:
                    self._log_session(
                        f"[{_host_ts_full()}] [WARN] All-reads CSV write skipped "
                        f"(file locked): {csv_err}\n"
                    )
                self._log_cps(
                    "----- NEW STREAMER RECORD -----\n"
                    f"host_ts       : {now}\n"
                    f"read_index    : {packet_info['read_index']}\n"
                    f"write_index   : {packet_info['write_index']}\n"
                    f"overflow      : {packet_info['streamer_overflow']}\n"
                    f"uart_ts       : {format_ts(packet_info['cps_ts_bytes'])}\n"
                    f"rect_v        : {packet_info['rect_v']}\n"
                    f"rect_i        : {packet_info['rect_i']}\n"
                    f"pay_len       : {packet_info['pay_len']}\n"
                    f"ascii         :\n{packet_info['payload'].decode('ascii', errors='replace')}\n\n"
                )
                message_infos = (
                    reassemble_new_streamer_message(
                        packet_info, self._new_message_buf, self._new_message_info
                    )
                    if self.reassemble_streamer_messages
                    else [packet_info]
                )
                for message_info in message_infos:
                    try:
                        csv_append_row(self._csv_lock, CPS_CSV_FILE,
                                       build_cps_csv_row(now, message_info))
                    except PermissionError as csv_err:
                        self._log_session(
                            f"[{_host_ts_full()}] [WARN] CSV write skipped (file locked): {csv_err}\n"
                        )
                    self.on_cps(message_info)
            return

        # ── CPS decode (identical logic to net_client) ─────────────────────
        if len(frame) >= 8 and frame[3:8] == _CPS_MARKER:
            info = parse_cps_response_frame(frame)
            info["_host_ts_full"] = now
            info["_rx_tag"]       = rx_tag
            self._cps_csv_frame_index += 1
            info["_csv_frame_index"] = self._cps_csv_frame_index

            try:
                csv_append_row(self._csv_lock, CPS_ALL_READS_CSV_FILE,
                               build_cps_csv_row(now, info))
            except PermissionError as csv_err:
                self._log_session(
                    f"[{_host_ts_full()}] [WARN] All-reads CSV write skipped "
                    f"(file locked): {csv_err}\n"
                )

            ht        = now
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
                f"queue_status  : 0x{((info['read_idx'] << 4) | info['write_idx']):02X} "
                f"(read_idx={info['read_idx']}, write_idx={info['write_idx']})\n"
                f"payload_valid : {info['payload_valid']}\n"
            )
            if info["payload_valid"] == 0:
                cps_entry += "info          : NO VALID PAYLOAD\n"
                cps_entry += f"cps_raw_only  :\n{hex_dump_multiline(info['cps_raw_only'])}\n\n"
            else:
                ascii_text = info["payload"].decode("ascii", errors="replace")
                cps_entry += (
                    f"cps_ts        : {format_ts(info['cps_ts_bytes'])}\n"
                    f"pay_len       : {info['pay_len']}\n"
                    f"ascii         :\n{ascii_text}\n"
                    f"cps_raw_only  :\n{hex_dump_multiline(info['cps_raw_only'])}\n\n"
                )
            self._log_cps(cps_entry)

            for packet_info in split_cps_payload_lines(info, self._cps_line_buf):
                if int(packet_info.get("payload_valid", 0)) == 1:
                    row = build_cps_csv_row(ht, packet_info)
                    try:
                        csv_append_row(self._csv_lock, CPS_CSV_FILE, row)
                    except PermissionError as csv_err:
                        self._log_session(
                            f"[{_host_ts_full()}] [WARN] CSV write skipped "
                            f"(file locked): {csv_err}\n"
                        )
                self.on_cps(packet_info)

    # ── Main thread loop ───────────────────────────────────────────────────────

    def _run(self):
        try:
            self._open_device()
            dev = self._dev

            while not self._stop.is_set():
                with self._exclusive_cv:
                    if self._exclusive_requested:
                        self._exclusive_active = True
                        self._waiting = False
                        self._retry_count = 0
                        self._inflight_payload = b""
                        self._inflight_hex = ""
                        self._inflight_tag = "AUTO"
                        self._inflight_kind = "GET"
                        self._try_deadline = 0.0
                        self._rx_buf.clear()
                        self._exclusive_rx_buf.clear()
                        self._cps_line_buf.clear()
                        self._exclusive_cv.notify_all()
                        while self._exclusive_requested and not self._stop.is_set():
                            self._exclusive_cv.wait(timeout=0.1)
                        self._exclusive_active = False
                        self._exclusive_cv.notify_all()
                        if self._stop.is_set():
                            break
                    runtime_blocked = self._runtime_blocked

                if runtime_blocked:
                    time.sleep(LOOP_SLEEP_SEC)
                    continue

                # 1) RX pump – pull bytes off the wire, extract complete frames
                try:
                    self._usb_read_into_buf()
                    while True:
                        frame = self._try_extract_frame()
                        if frame is None:
                            break
                        self._handle_rx_frame(frame)
                except _USB_TIMEOUT_EX:
                    pass   # expected during idle
                except usb.core.USBError as e:
                    self._log_session(
                        f"[{_host_ts_full()}] [ERROR] USB RX error: {e}\n"
                    )
                    self.on_status(f"USB error: {e}")
                    break

                # 2) Retry deadline check
                if self._waiting:
                    if time.monotonic() >= self._try_deadline:
                        self._retry_count += 1
                        if self._retry_count < MAX_RETRIES:
                            self._send_inflight(dev)
                        else:
                            self._log_session(
                                f"[{_host_ts_full()}] [WARN] No USB response after "
                                f"{MAX_RETRIES} tries (tag={self._inflight_tag}).\n"
                            )
                            recovery = _recover_usb_pipe_after_timeout(dev, EP_IN)
                            self._log_session(
                                f"[{_host_ts_full()}] [INFO] USB timeout recovery "
                                f"for {self._inflight_tag}: {recovery}\n"
                            )
                            if self._inflight_tag != "AUTO" and self.on_retry_prompt:
                                self.on_status(
                                    f"⚠ No USB response to {self._inflight_tag} – "
                                    "waiting for user decision…"
                                )
                                should_retry = self.on_retry_prompt(
                                    self._inflight_hex, self._inflight_tag
                                )
                                if should_retry:
                                    self._log_session(
                                        f"[{_host_ts_full()}] [INFO] User chose to retry\n"
                                    )
                                    self._retry_count = 0
                                    self._send_inflight(dev)
                                    time.sleep(LOOP_SLEEP_SEC)
                                    continue
                                else:
                                    self._log_session(
                                        f"[{_host_ts_full()}] [INFO] User chose to skip\n"
                                    )
                                    self.on_status(
                                        f"⚠ Skipped {self._inflight_tag} command"
                                    )
                            elif self._inflight_tag != "AUTO":
                                self.on_status(
                                    f"⚠ No USB response to {self._inflight_tag}"
                                )

                            # Clear inflight
                            self._waiting          = False
                            self._retry_count      = 0
                            self._inflight_payload = b""
                            self._inflight_hex     = ""
                            self._inflight_tag     = "AUTO"
                            self._inflight_kind    = "GET"
                            self._try_deadline     = 0.0
                    time.sleep(LOOP_SLEEP_SEC)
                    continue

                # 3) Manual sends – priority over auto poll
                try:
                    payload, tag, hex_str = self.send_q.get_nowait()
                    self._arm_inflight(payload, tag, hex_str)
                    try:
                        self._send_inflight(dev)
                    except Exception as exc:
                        msg = (
                            f"USB TX error for {tag}: {type(exc).__name__}: {exc}"
                        )
                        self.on_status(msg)
                        self._log_session(f"[{_host_ts_full()}] [ERROR] {msg}\n")
                        self._waiting = False
                        self._retry_count = 0
                        self._inflight_payload = b""
                        self._inflight_hex = ""
                        self._inflight_tag = "AUTO"
                        self._inflight_kind = "GET"
                        self._try_deadline = 0.0
                    time.sleep(LOOP_SLEEP_SEC)
                    continue
                except queue.Empty:
                    pass

                # 4) CPS auto-poll
                now_m = time.monotonic()
                cps_due = self.plot_enabled and now_m >= self._next_cps_poll
                raw_due = (
                    self.raw_counts_enabled
                    and now_m >= self._next_raw_counts_poll
                )
                if raw_due and (self._prefer_raw_counts_poll or not cps_due):
                    self._next_raw_counts_poll = now_m + RAW_RECT_COUNTS_POLL_INTERVAL_SEC
                    self._prefer_raw_counts_poll = False
                    self._arm_inflight(
                        _bytes_from_hex(RAW_RECT_COUNTS_POLL_HEX),
                        "AUTO",
                        RAW_RECT_COUNTS_POLL_HEX,
                    )
                    self._send_inflight(dev)
                elif cps_due:
                    self._next_cps_poll = now_m + CPS_POLL_INTERVAL_SEC
                    self._prefer_raw_counts_poll = True
                    poll_hex = NEW_CPS_POLL_HEX if self.new_streamer_enabled else CPS_POLL_HEX
                    self._arm_inflight(_bytes_from_hex(poll_hex), "AUTO", poll_hex)
                    self._send_inflight(dev)

                time.sleep(LOOP_SLEEP_SEC)

        except Exception as e:
            msg = f"USB Connect/Run error: {type(e).__name__}: {e}"
            self.on_status(msg)
            self._log_session(f"[{_host_ts_full()}] [ERROR] {msg}\n")
        finally:
            self._release_device()
            self.on_status("USB Disconnected")
