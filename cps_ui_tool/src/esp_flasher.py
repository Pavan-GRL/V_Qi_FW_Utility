"""
ESP32 ROM Bootloader Flasher — over Ethernet via STM UART bridge.

Protocol reference: https://github.com/cpq/esputil
                    https://docs.espressif.com/projects/esptool/en/latest/advanced-topics/serial-protocol.html

Sequence (matches `esputil -fp 0x220 flash 0 firmware.bin`):
  1. SYNC           (op=0x08)  — handshake with ROM bootloader
  2. READ_REG       (op=0x0A)  — detect chip ID from 0x40001000
  3. CHANGE_BAUD    (op=0x0F)  — optional; only when flash baud > 115200
  4. SPI_ATTACH     (op=0x0D)  — attach SPI flash
  5. SPI_SET_PARAMS (op=0x0B)  — configure flash geometry
  6. FLASH_BEGIN    (op=0x02)  — erase target region
  7. FLASH_DATA     (op=0x03)  — write 4 KB blocks (with F1/F2 patching on first block)
  8. FLASH_END      (op=0x04)  — finish and reboot

Chain: PC ─TCP→ STM H7 ─UART→ ESP ROM bootloader
The STM acts as a transparent byte-level bridge once put into flash-bridge mode.
"""
from __future__ import annotations

import struct
import socket
import time
import threading
from typing import Callable, Optional

# ── SLIP constants (RFC 1055) ────────────────────────────────────────────────
SLIP_END     = 0xC0
SLIP_ESC     = 0xDB
SLIP_ESC_END = 0xDC
SLIP_ESC_ESC = 0xDD

# ── ESP ROM bootloader command opcodes ────────────────────────────────────────
OP_FLASH_BEGIN     = 0x02
OP_FLASH_DATA      = 0x03
OP_FLASH_END       = 0x04
OP_MEM_BEGIN        = 0x05
OP_MEM_END          = 0x06
OP_MEM_DATA         = 0x07
OP_SYNC             = 0x08
OP_WRITE_REG        = 0x09
OP_READ_REG         = 0x0A
OP_SPI_SET_PARAMS   = 0x0B
OP_SPI_ATTACH       = 0x0D
OP_READ_FLASH_SLOW  = 0x0E
OP_CHANGE_BAUDRATE  = 0x0F

OP_NAMES = {
    OP_FLASH_BEGIN:    "FLASH_BEGIN",
    OP_FLASH_DATA:     "FLASH_DATA",
    OP_FLASH_END:      "FLASH_END",
    OP_SYNC:           "SYNC",
    OP_READ_REG:       "READ_REG",
    OP_SPI_ATTACH:     "SPI_ATTACH",
    OP_SPI_SET_PARAMS: "SPI_SET_PARAMS",
    OP_READ_FLASH_SLOW:"READ_FLASH_SLOW",
    OP_CHANGE_BAUDRATE:"CHANGE_BAUD",
}

# ── Known chip IDs (read from ROM @ 0x40001000) ──────────────────────────────
CHIP_ESP32       = 0x00F01D83
CHIP_ESP32S2     = 0x000007C6
CHIP_ESP32C3_12  = 0x6921506F   # ECO 1/2
CHIP_ESP32C3_3   = 0x1B31506F   # ECO 3
CHIP_ESP32S3     = 0x00000009
CHIP_ESP8266     = 0xFFF0C101

CHIP_NAMES = {
    CHIP_ESP32:      "ESP32",
    CHIP_ESP32S2:    "ESP32-S2",
    CHIP_ESP32C3_12: "ESP32-C3 (ECO1/2)",
    CHIP_ESP32C3_3:  "ESP32-C3 (ECO3)",
    CHIP_ESP32S3:    "ESP32-S3",
    CHIP_ESP8266:    "ESP8266",
}

# Bootloader flash address per chip
CHIP_BLA = {
    CHIP_ESP32:      0x1000,
    CHIP_ESP32S2:    0x1000,
    CHIP_ESP32C3_12: 0x0000,
    CHIP_ESP32C3_3:  0x0000,
    CHIP_ESP32S3:    0x0000,
    CHIP_ESP8266:    0x0000,
}

FLASH_BLOCK_SIZE = 4096
DEFAULT_FLASH_BAUD = 921600

FW_UPDATE_STEP_INFO = (
    {
        "name": "SYNC",
        "opcode": OP_SYNC,
        "purpose": "Handshake with the ESP ROM bootloader and confirm the STM bridge path.",
        "payload": "07 07 12 20 + 32 x 55 bytes",
        "reply_expected": True,
    },
    {
        "name": "READ_REG",
        "opcode": OP_READ_REG,
        "purpose": "Read ROM register 0x40001000 to detect the target chip ID.",
        "payload": "addr=0x40001000",
        "reply_expected": True,
    },
    {
        "name": "SPI_ATTACH",
        "opcode": OP_SPI_ATTACH,
        "purpose": "Attach the SPI flash to the ROM flasher (skipped for ESP8266).",
        "payload": "0x00000000, 0x00000000",
        "reply_expected": True,
    },
    {
        "name": "SPI_SET_PARAMS",
        "opcode": OP_SPI_SET_PARAMS,
        "purpose": "Set flash geometry parameters used by the ROM flasher.",
        "payload": "flash_id=0, size=4MB, block=64KB, sector=4KB, page=256, status_mask=0xFFFF",
        "reply_expected": True,
    },
    {
        "name": "FLASH_BEGIN",
        "opcode": OP_FLASH_BEGIN,
        "purpose": "Erase the target flash region before programming.",
        "payload": "size, num_blocks, block_size=4096, flash_offset[, encrypt=0]",
        "reply_expected": True,
    },
    {
        "name": "FLASH_DATA",
        "opcode": OP_FLASH_DATA,
        "purpose": "Program one 4KB block at a time; block 0 may have flash params patched.",
        "payload": "len, seq, 0, 0 + data block, checksum=data XOR seeded with 0xEF",
        "reply_expected": True,
    },
    {
        "name": "FLASH_END",
        "opcode": OP_FLASH_END,
        "purpose": "Finish flashing and request reboot.",
        "payload": "flag=0 for reboot, flag=1 to stay in bootloader",
        "reply_expected": True,
    },
)


def _build_fw_update_step_info(flash_baud: int) -> list[dict]:
    """Return the protocol flow, inserting CHANGE_BAUD when needed."""
    steps = list(FW_UPDATE_STEP_INFO[:2])
    if flash_baud > 115200:
        steps.append({
            "name": "CHANGE_BAUD",
            "opcode": OP_CHANGE_BAUDRATE,
            "purpose": "Request a higher ESP ROM bootloader UART baud before flash traffic.",
            "payload": f"baud={flash_baud}, second_arg=0",
            "reply_expected": True,
        })
    steps.extend(FW_UPDATE_STEP_INFO[2:])
    return steps


def format_fw_update_plan(
    firmware_size: int,
    flash_offset: int,
    flash_params: int,
    flash_baud: int = DEFAULT_FLASH_BAUD,
) -> str:
    """Return a human-readable summary of the FW update command flow."""
    num_blocks = (firmware_size + FLASH_BLOCK_SIZE - 1) // FLASH_BLOCK_SIZE
    steps = _build_fw_update_step_info(flash_baud)
    lines = [
        "FW update command flow:",
        "  TX path        : HostPC -> STM TCP bridge -> ESP/disp ROM bootloader",
        "  RX path        : ESP/disp ROM bootloader -> STM -> HostPC",
        "  Echo expected  : NO. The STM bridge is transparent and should not echo programming bytes.",
        "  Reply expected : YES, for each ESP ROM bootloader command below.",
        f"  Image size      : {firmware_size} bytes ({num_blocks} blocks of {FLASH_BLOCK_SIZE} bytes)",
        f"  Flash offset    : 0x{flash_offset:X}",
        f"  Flash params    : 0x{flash_params:04X}",
        f"  Flash baud      : {flash_baud}",
    ]

    for step_num, item in enumerate(steps, start=1):
        repeat_suffix = f" x {num_blocks}" if item["name"] == "FLASH_DATA" else ""
        reply_text = (
            "YES - bootloader reply/ack returns on the same bridge path"
            if item["reply_expected"]
            else "NO"
        )
        lines.append(f"Step {step_num}: {item['name']}{repeat_suffix} (op=0x{item['opcode']:02X})")
        lines.append(f"  Purpose        : {item['purpose']}")
        lines.append(f"  Command/payload: {item['payload']}")
        lines.append(f"  Reply expected : {reply_text}")

    return "\n".join(lines)

# ── SLIP encode / decode ─────────────────────────────────────────────────────

def slip_encode(data: bytes) -> bytes:
    """Wrap *data* in a SLIP frame: END … escaped-payload … END."""
    out = bytearray([SLIP_END])
    for b in data:
        if b == SLIP_END:
            out += bytes([SLIP_ESC, SLIP_ESC_END])
        elif b == SLIP_ESC:
            out += bytes([SLIP_ESC, SLIP_ESC_ESC])
        else:
            out.append(b)
    out.append(SLIP_END)
    return bytes(out)


def slip_decode(stream: bytes) -> list[bytes]:
    """Extract all complete SLIP packets from *stream*."""
    packets: list[bytes] = []
    buf = bytearray()
    in_packet = False
    prev_esc = False
    for b in stream:
        if b == SLIP_END:
            if in_packet and len(buf) > 0:
                packets.append(bytes(buf))
            buf = bytearray()
            in_packet = not in_packet
            prev_esc = False
        elif in_packet:
            if prev_esc:
                if b == SLIP_ESC_END:
                    buf.append(SLIP_END)
                elif b == SLIP_ESC_ESC:
                    buf.append(SLIP_ESC)
                prev_esc = False
            elif b == SLIP_ESC:
                prev_esc = True
            else:
                buf.append(b)
    return packets


# ── Checksum (XOR seeded with 0xEF) ──────────────────────────────────────────

def esp_checksum(data: bytes) -> int:
    cs = 0xEF
    for b in data:
        cs ^= b
    return cs


# ── Command helpers ───────────────────────────────────────────────────────────

def build_cmd(op: int, payload: bytes = b"", checksum: int = 0) -> bytes:
    """Build an 8-byte header + payload for the ESP ROM bootloader."""
    hdr = struct.pack("<BBHI", 0x00, op, len(payload), checksum)
    return hdr + payload


def parse_response(data: bytes, expected_op: int) -> tuple[bool, int, bytes]:
    """
    Parse a bootloader response.
    Returns (ok, value, raw).
    ok=True  if direction==1, opcode matches, and no error.
    value    is the 32-bit register/result value.
    """
    if len(data) < 10:
        return False, 0, data
    direction = data[0]
    op = data[1]
    _length = struct.unpack_from("<H", data, 2)[0]
    value = struct.unpack_from("<I", data, 4)[0]
    if direction != 0x01 or op != expected_op:
        return False, value, data
    # Error flag is in last 4 bytes for ESP32 (last 2 for ESP8266, but 4 is safe)
    error_flag = data[-4] if len(data) >= 12 else data[-2]
    return error_flag == 0, value, data


def _hex_preview(data: bytes, max_bytes: int = 32) -> str:
    """Return hex dump of first *max_bytes* for logging."""
    preview = " ".join(f"{b:02X}" for b in data[:max_bytes])
    if len(data) > max_bytes:
        preview += f" ... ({len(data)} bytes total)"
    return preview


def _hex_full(data: bytes) -> str:
    """Return a full hex dump for structured trace logging."""
    return " ".join(f"{b:02X}" for b in data)


def _fmt_ms(seconds: float) -> str:
    """Format a duration in milliseconds."""
    return f"{seconds * 1000:.0f} ms"


# ── ESP Flasher class ────────────────────────────────────────────────────────

class ESPFlasher:
    """
    Drives the ESP32 ROM bootloader flash protocol over a TCP socket.

    The TCP socket connects to the STM H7, which transparently bridges
    bytes to/from the ESP UART.

    Step verification:
        Set *step_gate* to a threading.Event. After each protocol step,
        the flasher will log details and wait on the gate before proceeding.
        Clear the gate to pause; set it to continue. Set *auto_mode* to
        skip the gate entirely.
    """

    def __init__(
        self,
        sock: socket.socket,
        log: Callable[[str], None] | None = None,
        progress: Callable[[float, str], None] | None = None,
        step_gate: threading.Event | None = None,
        cmd_timeout: float = 1.5,  # Default base timeout for quick command replies
        on_retry_prompt: Callable[[str, str], bool] | None = None,  # (step_name, details) -> should_retry
        trace: Callable[[dict], None] | None = None,
    ):
        self.sock = sock
        self._log = log or (lambda s: None)
        self._progress = progress or (lambda p, s: None)
        self._rx_buf = bytearray()
        self.chip_id: int = 0
        self._cancel = threading.Event()
        # Step-by-step verification gate
        self._step_gate = step_gate        # wait on this after each step
        self.auto_mode = False             # when True, skip gate waits
        self._cmd_timeout = cmd_timeout    # command reply timeout
        self._on_retry_prompt = on_retry_prompt  # callback for retry prompts
        self._trace = trace or (lambda event: None)
        self._cmd_seq = 0

    def cancel(self):
        self._cancel.set()

    def _check_cancel(self):
        if self._cancel.is_set():
            raise RuntimeError("Flash operation cancelled by user")

    def _wait_gate(self, step_name: str, detail: str = ""):
        """
        If step_gate is set and auto_mode is False, clear the gate and
        wait for it to be set again (by UI "Next Step" or "Automate" button).
        """
        if self._step_gate is None or self.auto_mode:
            return
        msg = f"[STEP] {step_name}"
        if detail:
            msg += f"\n{detail}"
        self._log(msg)
        self._step_gate.clear()
        # Wait in small increments so we can check cancel
        while not self._step_gate.is_set():
            self._check_cancel()
            self._step_gate.wait(timeout=0.2)

    def _prompt_retry(self, step_name: str, detail: str) -> bool:
        """
        Prompt user to retry a failed operation.
        Returns True if user wants to retry, False to abort.
        """
        if self._on_retry_prompt is None:
            return False
        self._log(f"[RETRY?] {step_name} - waiting for user decision...")
        return self._on_retry_prompt(step_name, detail)

    def _timeout_for_step(self, minimum: float, scale: float = 1.0) -> float:
        """Return a timeout derived from the user-selected base timeout."""
        return max(minimum, self._cmd_timeout * scale)

    def _emit_trace(self, event: dict):
        """Emit a structured FW trace event for file logging."""
        self._trace(event)

    # ── Low-level transport ───────────────────────────────────────────────

    def _send_raw(self, data: bytes):
        """Send raw bytes over the socket."""
        self.sock.sendall(data)

    def _recv_raw(self, timeout: float) -> bytes:
        """Receive available bytes from socket within *timeout* seconds."""
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(4096)
            if not data:
                raise ConnectionError("Socket closed by peer")
            return data
        except socket.timeout:
            return b""

    def _send_slip(self, data: bytes):
        """SLIP-encode and send."""
        self._send_raw(slip_encode(data))

    def _recv_slip_packet(self, timeout: float) -> bytes | None:
        """
        Read from socket until we get a complete SLIP packet, or timeout.
        Returns the decoded packet payload, or None on timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = self._recv_raw(min(remaining, 0.1))
            if chunk:
                self._rx_buf.extend(chunk)
            # Try to extract a complete SLIP packet
            packets = slip_decode(bytes(self._rx_buf))
            if packets:
                self._consume_first_slip_packet()
                return packets[0]
        return None

    def _consume_first_slip_packet(self):
        """Remove bytes of the first complete SLIP frame from _rx_buf."""
        mode = False
        i = 0
        while i < len(self._rx_buf):
            b = self._rx_buf[i]
            if b == SLIP_END:
                if mode:
                    self._rx_buf = self._rx_buf[i + 1:]
                    return
                mode = True
            i += 1
        self._rx_buf.clear()

    # ── Command execution ─────────────────────────────────────────────────

    def cmd(self, op: int, payload: bytes = b"", checksum: int = 0,
            timeout: float | None = None) -> tuple[bool, int, bytes]:
        """
        Send command and wait for matching response.
        Returns (ok, value, raw_response).
        If timeout is None, uses self._cmd_timeout.
        """
        if timeout is None:
            timeout = self._cmd_timeout

        pkt = build_cmd(op, payload, checksum)
        slip_frame = slip_encode(pkt)
        op_name = OP_NAMES.get(op, f"0x{op:02X}")
        start_time = time.monotonic()
        self._cmd_seq += 1
        cmd_id = self._cmd_seq
        self._log(f"  TX {op_name}: {_hex_preview(slip_frame)}  [timeout={_fmt_ms(timeout)}]")
        self._send_raw(slip_frame)
        self._emit_trace({
            "kind": "TX",
            "cmd_id": cmd_id,
            "op": op,
            "op_name": op_name,
            "timeout_s": timeout,
            "checksum": checksum,
            "packet_len": len(pkt),
            "wire_len": len(slip_frame),
            "packet_hex": _hex_full(pkt),
            "wire_hex": _hex_full(slip_frame),
        })

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancel()
            remaining = deadline - time.monotonic()
            resp = self._recv_slip_packet(min(remaining, 0.5))
            if resp is None:
                continue
            elapsed = time.monotonic() - start_time
            self._log(f"  RX {op_name} after {_fmt_ms(elapsed)}: {_hex_preview(resp)}")
            ok, value, raw = parse_response(resp, op)
            self._emit_trace({
                "kind": "RX",
                "cmd_id": cmd_id,
                "op": op,
                "op_name": op_name,
                "elapsed_s": elapsed,
                "response_ok": ok,
                "value": value,
                "packet_len": len(resp),
                "wire_len": len(slip_encode(resp)),
                "packet_hex": _hex_full(resp),
                "wire_hex": _hex_full(slip_encode(resp)),
            })
            if resp[0:1] == b'\x01' and len(resp) >= 2 and resp[1] == op:
                return ok, value, raw
        self._log(f"  TIMEOUT {op_name} after {_fmt_ms(timeout)}")
        self._emit_trace({
            "kind": "TIMEOUT",
            "cmd_id": cmd_id,
            "op": op,
            "op_name": op_name,
            "elapsed_s": time.monotonic() - start_time,
            "timeout_s": timeout,
        })
        return False, 0, b""

    # ── High-level protocol steps ─────────────────────────────────────────

    def sync(self, retries: int = 10) -> bool:
        """
        Send SYNC commands until the bootloader responds.
        Returns True on success.
        """
        self._log("Step 1: SYNC — handshake with ESP ROM bootloader")
        self._progress(0.0, "Syncing with ESP bootloader...")
        sync_payload = bytes([0x07, 0x07, 0x12, 0x20]) + (b'\x55' * 32)

        attempt_count = 0
        while True:
            self._check_cancel()
            attempt_count += 1
            self._log(f"  SYNC attempt {attempt_count}")
            ok, _, raw = self.cmd(OP_SYNC, sync_payload, timeout=self._timeout_for_step(1.5))
            if ok:
                self._log("  SYNC OK — bootloader responded")
                # Flush extra sync responses
                time.sleep(0.1)
                self._rx_buf.clear()
                try:
                    while True:
                        self.sock.settimeout(0.05)
                        self.sock.recv(4096)
                except socket.timeout:
                    pass
                self._wait_gate("SYNC complete",
                                "Bootloader responded to SYNC. Ready to detect chip.")
                return True

            # This attempt failed - prompt user after every failure
            self._log(f"  SYNC attempt {attempt_count} failed")
            detail = (f"No response from ESP bootloader (attempt {attempt_count}).\n\n"
                      f"Check:\n"
                      f"• ESP is in download mode (GPIO0 low, reset)\n"
                      f"• UART connection is correct\n"
                      f"• Bridge is active on port 5003\n\n"
                      f"Press YES to retry immediately")
            if self._prompt_retry("SYNC Failed", detail):
                self._log(f"  User chose to retry SYNC (attempt {attempt_count + 1})")
                continue  # Try again
            else:
                self._log("  User chose to abort SYNC")
                return False

    def detect_chip(self) -> int:
        """Read chip ID from ROM address 0x40001000."""
        self._log("Step 2: READ_REG — detect chip ID @ 0x40001000")
        payload = struct.pack("<I", 0x40001000)
        read_timeout = self._timeout_for_step(1.5)

        while True:
            ok, value, raw = self.cmd(OP_READ_REG, payload, timeout=read_timeout)
            if ok:
                self.chip_id = value
                name = CHIP_NAMES.get(value, f"Unknown (0x{value:08X})")
                self._log(f"  Chip detected: {name} (ID=0x{value:08X})")
                self._wait_gate("Chip detected",
                                f"Chip: {name}\nID: 0x{value:08X}")
                return value

            # Failed - prompt retry
            self._log("  READ_REG failed - no response")
            detail = ("Failed to read chip ID register.\n\n"
                      "Check:\n"
                      "• ESP is still in download mode\n"
                      "• Connection is stable")
            if self._prompt_retry("Chip Detection Failed", detail):
                self._log("  User chose to retry READ_REG")
                continue
            else:
                raise RuntimeError("Failed to read chip ID register")

    def change_baudrate(self, baud: int):
        """Send CHANGE_BAUD_RATE if a higher flash baud was requested."""
        if baud <= 115200:
            self._log(f"Step 3: CHANGE_BAUD — skipped (flash baud {baud} <= 115200)")
            return

        self._log(f"Step 3: CHANGE_BAUD — request {baud} baud")
        payload = struct.pack("<II", baud, 0)
        baud_timeout = self._timeout_for_step(1.5)
        while True:
            ok, _, raw = self.cmd(OP_CHANGE_BAUDRATE, payload, timeout=baud_timeout)
            if ok:
                self._log(f"  CHANGE_BAUD OK — ESP ROM acknowledged {baud}")
                self._wait_gate("CHANGE_BAUD complete",
                                f"Requested baud: {baud}\n"
                                f"TX: {_hex_preview(payload)}\n"
                                f"RX: {_hex_preview(raw)}")
                return

            detail = (f"Failed to switch ESP ROM baud to {baud}.\n\n"
                      "Check:\n"
                      "• STM bridge has baud-switch support enabled\n"
                      "• ESP is still responding at 115200\n"
                      "• Connection is stable")
            if self._prompt_retry("CHANGE_BAUD Failed", detail):
                self._log(f"  User chose to retry CHANGE_BAUD to {baud}")
                continue
            raise RuntimeError(f"CHANGE_BAUD failed for {baud}")

    def spi_attach(self):
        """Attach SPI flash (non-ESP8266)."""
        if self.chip_id == CHIP_ESP8266:
            self._log("  Skipping SPI attach (ESP8266)")
            return

        self._log("SPI_ATTACH — attach SPI flash")
        payload = struct.pack("<II", 0, 0)
        attach_timeout = self._timeout_for_step(1.5)
        while True:
            ok, _, raw = self.cmd(OP_SPI_ATTACH, payload, timeout=attach_timeout)
            if ok:
                self._log("  SPI_ATTACH OK")
                self._wait_gate("SPI_ATTACH complete",
                                f"TX: {_hex_preview(payload)}\nRX: {_hex_preview(raw)}")
                break
            # Failed - prompt retry
            detail = "Failed to attach SPI flash.\n\nCheck ESP connection."
            if self._prompt_retry("SPI_ATTACH Failed", detail):
                continue
            else:
                raise RuntimeError("SPI_ATTACH failed")

        self._log("SPI_SET_PARAMS — flash geometry (4MB, 64K/4K/256)")
        params = struct.pack("<6I", 0, 4 * 1024 * 1024, 65536, 4096, 256, 0xFFFF)
        params_timeout = self._timeout_for_step(1.5)
        while True:
            ok, _, raw = self.cmd(OP_SPI_SET_PARAMS, params, timeout=params_timeout)
            if ok:
                self._log("  SPI_SET_PARAMS OK")
                self._wait_gate("SPI_SET_PARAMS complete",
                                f"TX: {_hex_preview(params)}\nRX: {_hex_preview(raw)}")
                break
            # Failed - prompt retry
            detail = "Failed to set SPI flash parameters.\n\nCheck ESP connection."
            if self._prompt_retry("SPI_SET_PARAMS Failed", detail):
                continue
            else:
                raise RuntimeError("SPI_SET_PARAMS failed")

    def flash_begin(self, size: int, num_blocks: int, block_size: int,
                    offset: int) -> bool:
        """Send FLASH_BEGIN — erases the target region."""
        self._log(f"FLASH_BEGIN — erase {num_blocks} blocks at 0x{offset:X} "
                  f"({size} bytes)")
        is_newer = self.chip_id not in (CHIP_ESP32, CHIP_ESP8266)
        if is_newer:
            payload = struct.pack("<5I", size, num_blocks, block_size, offset, 0)
        else:
            payload = struct.pack("<4I", size, num_blocks, block_size, offset)
        erase_timeout = self._timeout_for_step(45.0, scale=30.0)

        while True:
            ok, _, raw = self.cmd(OP_FLASH_BEGIN, payload, timeout=erase_timeout)
            if ok:
                self._log("  FLASH_BEGIN OK — erase complete")
                self._wait_gate("FLASH_BEGIN (erase) complete",
                                f"Erased {num_blocks} blocks at 0x{offset:X}\n"
                                f"TX payload: {_hex_preview(payload)}\n"
                                f"RX: {_hex_preview(raw)}")
                return True
            # Failed - prompt retry
            detail = (f"FLASH_BEGIN failed (erase timeout?).\n\n"
                      f"Erasing {num_blocks} blocks at 0x{offset:X}\n\n"
                      f"Check:\n"
                      f"• ESP connection is stable\n"
                      f"• Flash is not write-protected")
            if self._prompt_retry("FLASH_BEGIN Failed", detail):
                self._log("  User chose to retry FLASH_BEGIN")
                continue
            else:
                raise RuntimeError("FLASH_BEGIN failed (erase timeout?)")

    def flash_data(self, data: bytes, seq: int) -> tuple[bool, bytes]:
        """Send one FLASH_DATA block. Returns (ok, raw_response)."""
        header = struct.pack("<IIII", len(data), seq, 0, 0)
        block = header + data
        cs = esp_checksum(data)  # checksum covers data only, not sub-header
        data_timeout = self._timeout_for_step(5.0, scale=5.0)

        while True:
            ok, _, raw = self.cmd(OP_FLASH_DATA, block, checksum=cs, timeout=data_timeout)
            if ok:
                return True, raw
            # Failed - prompt retry
            detail = (f"FLASH_DATA failed at block {seq}.\n\n"
                      f"Writing {len(data)} bytes\n\n"
                      f"Check:\n"
                      f"• ESP connection is stable\n"
                      f"• No power interruption")
            if self._prompt_retry(f"FLASH_DATA Block {seq} Failed", detail):
                self._log(f"  User chose to retry FLASH_DATA block {seq}")
                continue
            else:
                raise RuntimeError(f"FLASH_DATA failed at block {seq}")

    def flash_end(self, reboot: bool = False):
        """Send FLASH_END. reboot=True to reboot the chip immediately."""
        flag = 0 if reboot else 1  # 0=reboot, 1=stay in bootloader
        self._log(f"FLASH_END (reboot={'yes' if reboot else 'no'})")
        payload = struct.pack("<I", flag)
        ok, _, raw = self.cmd(OP_FLASH_END, payload, timeout=self._timeout_for_step(1.5))
        if not ok:
            self._log("  WARNING: FLASH_END response not OK (may still be fine)")
        else:
            self._log("  FLASH_END OK")
        self._wait_gate("FLASH_END complete",
                        f"Reboot flag: {flag}\nRX: {_hex_preview(raw)}")

    def flash_file(self, firmware: bytes, flash_offset: int = 0x10000,
                   flash_params: int = 0x0220, flash_baud: int = DEFAULT_FLASH_BAUD):
        """
        Flash a firmware binary to the given offset.

        *firmware*      raw bytes of the .bin file
        *flash_offset*  target address in flash (e.g. 0x0000 for C3, 0x1000 for ESP32)
        *flash_params*  F1<<8|F2 — e.g. 0x0220 = DIO, 4MB, 40MHz
        *flash_baud*    target ESP ROM baud after CHANGE_BAUD_RATE
        """
        size = len(firmware)
        num_blocks = (size + FLASH_BLOCK_SIZE - 1) // FLASH_BLOCK_SIZE

        fp_mode = (flash_params >> 8) & 0xFF
        fp_sf   = flash_params & 0xFF
        self._log(f"Flash params: 0x{flash_params:04X} "
                  f"(mode=0x{fp_mode:02X}, size|freq=0x{fp_sf:02X})")
        self._log(f"Flash baud: {flash_baud}")
        self._log(f"Flashing {size} bytes ({num_blocks} blocks) to 0x{flash_offset:X}")
        self._progress(0.0, f"Erasing {num_blocks} blocks...")

        self.flash_begin(size, num_blocks, FLASH_BLOCK_SIZE, flash_offset)

        bla = CHIP_BLA.get(self.chip_id, 0x1000)
        self._log(f"FLASH_DATA — writing {num_blocks} blocks...")

        for seq in range(num_blocks):
            self._check_cancel()
            start = seq * FLASH_BLOCK_SIZE
            chunk = firmware[start:start + FLASH_BLOCK_SIZE]

            # Patch flash params into first block if this is the bootloader area
            patched = False
            if seq == 0 and flash_offset == bla and len(chunk) >= 4:
                chunk = bytearray(chunk)
                if chunk[0] == 0xE9:  # valid ESP image magic
                    chunk[2] = (flash_params >> 8) & 0xFF  # F1: flash mode
                    chunk[3] = flash_params & 0xFF          # F2: size | freq
                    patched = True
                    self._log(f"  Patched F1=0x{chunk[2]:02X} F2=0x{chunk[3]:02X} "
                              f"into block 0 header")
                chunk = bytes(chunk)

            ok, raw = self.flash_data(chunk, seq)

            pct = (seq + 1) / num_blocks
            self._progress(pct, f"Block {seq + 1}/{num_blocks}")

            # Log every block for step-by-step, or every 16th for auto
            if not self.auto_mode or (seq + 1) % 16 == 0 or seq + 1 == num_blocks:
                self._log(f"  Block {seq + 1}/{num_blocks} OK  "
                          f"(offset=0x{flash_offset + start:X}, "
                          f"{len(chunk)} bytes, cs=0x{esp_checksum(chunk):02X})")

            detail = (f"Block {seq + 1}/{num_blocks}\n"
                      f"Flash addr: 0x{flash_offset + start:X}\n"
                      f"Size: {len(chunk)} bytes\n"
                      f"Checksum: 0x{esp_checksum(chunk):02X}\n"
                      f"Data[0:16]: {_hex_preview(chunk, 16)}\n"
                      f"RX: {_hex_preview(raw)}")
            if patched:
                detail += "\n** Flash params patched in this block **"
            self._wait_gate(f"FLASH_DATA block {seq + 1}/{num_blocks}", detail)

        self._log("  All blocks written.")

    def reboot(self):
        """Send FLASH_END with reboot flag."""
        self._log("Rebooting ESP...")
        self.flash_end(reboot=True)
        self._log("Reboot command sent.")


# ── Convenience: full flash sequence ──────────────────────────────────────────

def flash_esp_over_bridge(
    sock: socket.socket,
    firmware: bytes,
    flash_offset: int = 0x10000,
    flash_params: int = 0x0220,
    flash_baud: int = DEFAULT_FLASH_BAUD,
    log: Callable[[str], None] | None = None,
    progress: Callable[[float, str], None] | None = None,
    cancel_event: threading.Event | None = None,
    step_gate: threading.Event | None = None,
):
    """
    Run the full ESP flash sequence over an already-connected socket
    (STM is assumed to be in UART bridge mode already).

    Sequence (matches esputil):
      1. SYNC
      2. READ_REG (chip detect)
      3. CHANGE_BAUD (optional; only when flash_baud > 115200)
      4. SPI_ATTACH
      5. SPI_SET_PARAMS
      6. FLASH_BEGIN (erase)
      7. FLASH_DATA  (write blocks, with F1/F2 patching)
      8. FLASH_END   (reboot)
    """
    fl = ESPFlasher(sock, log, progress, step_gate)
    if cancel_event:
        fl._cancel = cancel_event

    if not fl.sync():
        raise RuntimeError("Could not sync with ESP bootloader. "
                           "Ensure the ESP is in download mode.")

    fl.detect_chip()
    fl.change_baudrate(flash_baud)
    fl.spi_attach()
    fl.flash_file(firmware, flash_offset, flash_params, flash_baud)
    fl.reboot()
    return fl.chip_id
