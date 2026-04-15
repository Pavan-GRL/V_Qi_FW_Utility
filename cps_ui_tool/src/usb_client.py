from __future__ import annotations

import threading
import time
import queue
from datetime import datetime
from typing import Callable, Optional, Union

try:
    import usb.core
    import usb.util
    import usb.backend.libusb1

    # On Windows, pyusb cannot auto-locate libusb-1.0.dll even when the
    # 'libusb' pip package is installed.  Ask that package for the DLL path
    # and hand it to the backend explicitly.
    def _get_libusb_backend():
        try:
            import libusb as _libusb_pkg
            return usb.backend.libusb1.get_backend(
                find_library=lambda _: _libusb_pkg.dll._name
            )
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
    hex_dump_spaces,
    hex_dump_multiline,
    format_ts,
    ts_bytes_to_hex,
    ts_bytes_to_le,
)
from logger import (
    SESSION_LOG_FILE, CPS_LOG_FILE, CPS_CSV_FILE,
    append_text, csv_init_if_needed, csv_append_row,
)

# ── Device identity ────────────────────────────────────────────────────────────
USB_VID = 0x227F
USB_PID = 0x0006

# ── Endpoints ──────────────────────────────────────────────────────────────────
EP_OUT_CMD = 0x01   # Bulk OUT – command / GET frames (start byte 0x01 or 0x07)
EP_OUT_FW  = 0x02   # Bulk OUT – FW-mode frames      (start byte 0x02)
EP_IN      = 0x81   # Bulk IN  – all device responses

# ── Timing (mirrors net_client constants) ─────────────────────────────────────
CPS_POLL_HEX          = "07 08 00 00 00 08 00 A1"
CPS_POLL_INTERVAL_SEC = 0.100
RX_WAIT_PER_TRY_SEC   = 0.500
MAX_RETRIES           = 3
LOOP_SLEEP_SEC        = 0.002

# ── USB transfer tunables ──────────────────────────────────────────────────────
USB_READ_TIMEOUT_MS   = 100    # ms – individual bulk-IN read
USB_WRITE_TIMEOUT_MS  = 1000   # ms – bulk-OUT write
USB_CTRL_TIMEOUT_MS   = 1000   # ms – EP0 control transfers
USB_MAX_READ          = 65536  # bytes – max per bulk-IN call

MAX_FRAME_LEN = 65535

# CPS frame marker at frame[3:8]
_CPS_MARKER = bytes.fromhex("00000800A1")


def _host_ts_full() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _bytes_from_hex(s: str) -> bytes:
    return bytes.fromhex(s.replace(" ", "").strip())


class USBClient:
    """
    WinUSB / libusb-backed USB client for VID_227F / PID_0006.

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
        on_tx: Optional[Callable[[str, str], None]] = None,
        on_rx: Optional[Callable[[str, str], None]] = None,
        on_retry_prompt: Optional[Callable[[str, str], bool]] = None,
    ):
        if not HAS_USB:
            raise RuntimeError(
                "pyusb is not installed. Run: pip install pyusb\n"
                "Also ensure the WinUSB driver is installed for VID_227F/PID_0006."
            )

        self.on_status       = on_status
        self.on_cps          = on_cps
        self.on_tx           = on_tx
        self.on_rx           = on_rx
        self.on_retry_prompt = on_retry_prompt

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

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self):
        self._stop.clear()
        csv_init_if_needed(self._csv_lock, CPS_CSV_FILE)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._release_device()

    def send_hex(self, hex_str: str, tag: str = "MANUAL"):
        b = _bytes_from_hex(hex_str)
        self.send_q.put((b, tag, hex_str.strip()))

    def send_bytes(self, b: bytes, tag: str = "MANUAL"):
        self.send_q.put((b, tag, hex_dump_spaces(b)))

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
        """Route frame to the correct bulk-OUT endpoint by start byte."""
        if payload and payload[0] == 0x02:
            return EP_OUT_FW
        return EP_OUT_CMD

    # ── Device lifecycle ───────────────────────────────────────────────────────

    def _open_device(self):
        self.on_status(f"Opening USB VID={USB_VID:04X} PID={USB_PID:04X} …")
        if _USB_BACKEND is None:
            raise RuntimeError(
                "libusb backend not found.\n"
                "Run:  pip install libusb\n"
                "If already installed, place libusb-1.0.dll next to app.py."
            )
        dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID, backend=_USB_BACKEND)
        if dev is None:
            # List all visible devices to help diagnosis
            all_devs = list(usb.core.find(find_all=True, backend=_USB_BACKEND))
            visible = ", ".join(
                f"VID={d.idVendor:04X}/PID={d.idProduct:04X}" for d in all_devs
            ) or "none"
            raise RuntimeError(
                f"USB device VID={USB_VID:04X}/PID={USB_PID:04X} not found.\n"
                f"Devices visible to libusb: {visible}\n"
                "Check: cable plugged, WinUSB driver bound via Zadig."
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
        self._log_session(
            f"[{_host_ts_full()}] [INFO] USB opened "
            f"VID={USB_VID:04X} PID={USB_PID:04X} "
            f"EP_OUT_CMD=0x{EP_OUT_CMD:02X} EP_OUT_FW=0x{EP_OUT_FW:02X} "
            f"EP_IN=0x{EP_IN:02X}\n"
        )
        self.on_status("USB Connected ✅  (Bulk OUT 0x01/0x02 | Bulk IN 0x81 | EP0 ctrl)")
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
            raw = self._dev.read(EP_IN, USB_MAX_READ, timeout=USB_READ_TIMEOUT_MS)
            if raw:
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
        dev.write(ep, self._inflight_payload, timeout=USB_WRITE_TIMEOUT_MS)
        now = _host_ts_full()
        self._log_session(
            f"[{now}] [TX:{self._inflight_tag}] {self._inflight_hex} "
            f"(ep=0x{ep:02X} try={self._retry_count + 1}/{MAX_RETRIES})\n"
        )
        self._emit_tx(self._inflight_hex, self._inflight_tag)
        self._try_deadline = time.monotonic() + RX_WAIT_PER_TRY_SEC

    def _rx_accepts_as_response(self, frame: bytes) -> bool:
        if self._inflight_kind == "SET":
            return len(frame) >= 1 and frame[0] in (0x01, 0x02)
        return True   # GET: any frame satisfies

    def _handle_rx_frame(self, frame: bytes):
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

        # ── CPS decode (identical logic to net_client) ─────────────────────
        if len(frame) >= 8 and frame[3:8] == _CPS_MARKER:
            info = parse_cps_response_frame(frame)
            info["_host_ts_full"] = now
            info["_rx_tag"]       = rx_tag

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

            pwr_ts_hex = ts_bytes_to_hex(info["pwr_ts_bytes"])
            pwr_ts_le  = ts_bytes_to_le(info["pwr_ts_bytes"])
            cps_ts_hex = cps_ts_le = ""
            if info["cps_ts_bytes"] is not None:
                cps_ts_hex = ts_bytes_to_hex(info["cps_ts_bytes"])
                cps_ts_le  = str(ts_bytes_to_le(info["cps_ts_bytes"]))
            rect_v = "" if info["rect_v"] is None else str(info["rect_v"])
            rect_i = "" if info["rect_i"] is None else str(info["rect_i"])
            row = [
                ht, pwr_ts_hex, pwr_ts_le, cps_ts_hex, cps_ts_le,
                rect_v, rect_i,
                info["pwr_len"], info["read_idx"], info["write_idx"],
                info["payload_valid"], info["pay_len"],
            ]
            try:
                csv_append_row(self._csv_lock, CPS_CSV_FILE, row)
            except PermissionError as csv_err:
                self._log_session(
                    f"[{_host_ts_full()}] [WARN] CSV write skipped (file locked): {csv_err}\n"
                )
            self.on_cps(info)

    # ── Main thread loop ───────────────────────────────────────────────────────

    def _run(self):
        try:
            self._open_device()
            dev = self._dev

            while not self._stop.is_set():

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
                    self._send_inflight(dev)
                    time.sleep(LOOP_SLEEP_SEC)
                    continue
                except queue.Empty:
                    pass

                # 4) CPS auto-poll
                now_m = time.monotonic()
                if self.plot_enabled and now_m >= self._next_cps_poll:
                    self._next_cps_poll = now_m + CPS_POLL_INTERVAL_SEC
                    self._arm_inflight(self._cps_poll_bytes, "AUTO", CPS_POLL_HEX)
                    self._send_inflight(dev)

                time.sleep(LOOP_SLEEP_SEC)

        except Exception as e:
            msg = f"USB Connect/Run error: {type(e).__name__}: {e}"
            self.on_status(msg)
            self._log_session(f"[{_host_ts_full()}] [ERROR] {msg}\n")
        finally:
            self._release_device()
            self.on_status("USB Disconnected")
