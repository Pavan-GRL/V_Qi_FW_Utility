from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from protocol import hex_dump_spaces


DEFAULT_CHUNK_SIZE = 512
SUPPORTED_CHUNK_SIZES = (256, 512)
DEFAULT_MAX_RETRIES = 3

_FRAME_START_BYTES = (0x01, 0x02, 0x07)

_FRAME_TYPE = 0x02
_FUNC_FW_UPDATE = 0xB1
_RESERVED = 0x00
_QI_UNIT = 0x03

OP_BEGIN = 0x01
OP_DATA = 0x02
OP_END_IMAGE = 0x03
OP_EXECUTE = 0x04
OP_ABORT = 0x05
OP_STATUS = 0x06

IMAGE_NONE = 0x00
IMAGE_BOOTLOADER = 0x01
IMAGE_APPLICATION = 0x02

RESULT_OK = 0x00

RESULT_NAMES = {
    0x00: "OK",
    0x01: "BAD_PARAM",
    0x02: "BAD_IMAGE",
    0x03: "SEQUENCE",
    0x04: "PARSE_ERROR",
    0x05: "NOT_READY",
    0x06: "BUSY",
    0x07: "PROGRAM_FAIL",
}

STATE_NAMES = {
    0x00: "IDLE",
    0x01: "RX_BOOT",
    0x02: "RX_APP",
    0x03: "WAIT_IMAGE",
    0x04: "READY",
    0x05: "PROGRAMMING",
    0x06: "DONE",
    0x07: "ERROR",
}

IMAGE_NAMES = {
    0x00: "NONE",
    0x01: "BOOT",
    0x02: "APP",
}

STAGE_NAMES = {
    0x00: "IDLE",
    0x01: "PARSE_BOOT",
    0x02: "PARSE_APP",
    0x03: "ENTER_EXCLUSIVE",
    0x04: "BOOT_LOAD",
    0x05: "BOOT_VERIFY",
    0x06: "APP_LOAD",
    0x07: "APP_VERIFY",
    0x08: "RESET",
    0x09: "CHIP_ID",
    0x0A: "DONE",
}


def result_name(code: int) -> str:
    return RESULT_NAMES.get(code, f"0x{code:02X}")


def state_name(code: int) -> str:
    return STATE_NAMES.get(code, f"0x{code:02X}")


def image_name(code: int) -> str:
    return IMAGE_NAMES.get(code, f"0x{code:02X}")


def stage_name(code: int) -> str:
    return STAGE_NAMES.get(code, f"0x{code:02X}")


def _u16le(value: int) -> bytes:
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


def _u32le(value: int) -> bytes:
    return bytes((
        value & 0xFF,
        (value >> 8) & 0xFF,
        (value >> 16) & 0xFF,
        (value >> 24) & 0xFF,
    ))


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("Socket closed by peer")
        data.extend(chunk)
    return bytes(data)


def read_len_prefixed_frame(sock: socket.socket) -> bytes:
    header = _recv_exact(sock, 3)
    if header[0] not in _FRAME_START_BYTES:
        raise ValueError(f"Unexpected frame start byte 0x{header[0]:02X}")

    total_len = header[1] | (header[2] << 8)
    if total_len < 3:
        raise ValueError(f"Invalid frame length {total_len}")

    return header + _recv_exact(sock, total_len - 3)


def _build_frame(seq: int, op: int, payload: bytes = b"") -> bytes:
    frame = bytearray((
        _FRAME_TYPE,
        0x00,
        0x00,
        seq & 0xFF,
        (seq >> 8) & 0xFF,
        _FUNC_FW_UPDATE,
        _RESERVED,
        op,
        _QI_UNIT,
    ))
    frame.extend(payload)
    total_len = len(frame)
    frame[1] = total_len & 0xFF
    frame[2] = (total_len >> 8) & 0xFF
    return bytes(frame)


def build_begin_frame(seq: int, image_type: int, expected_hex_bytes: int) -> bytes:
    return _build_frame(seq, OP_BEGIN, bytes((image_type,)) + _u32le(expected_hex_bytes))


def build_data_frame(seq: int, image_type: int, offset: int, chunk: bytes) -> bytes:
    payload = bytes((image_type,)) + _u32le(offset) + _u16le(len(chunk)) + chunk
    return _build_frame(seq, OP_DATA, payload)


def build_end_frame(seq: int, image_type: int, expected_hex_bytes: int) -> bytes:
    return _build_frame(seq, OP_END_IMAGE, bytes((image_type,)) + _u32le(expected_hex_bytes))


def build_execute_frame(seq: int) -> bytes:
    return _build_frame(seq, OP_EXECUTE)


def build_abort_frame(seq: int) -> bytes:
    return _build_frame(seq, OP_ABORT)


def build_status_frame(seq: int) -> bytes:
    return _build_frame(seq, OP_STATUS)


@dataclass(slots=True)
class CpsUpdateReply:
    raw_frame: bytes
    op_result: int
    session_state: int
    active_image: int
    ready_mask: int
    boot_hex_received: int
    boot_hex_expected: int
    boot_bytes_written: int
    app_hex_received: int
    app_hex_expected: int
    app_bytes_streamed: int
    last_stage: int
    hal_status: int
    cps_status: int
    programmed_bytes: int
    chip_id: int

    @classmethod
    def from_frame(cls, frame: bytes) -> "CpsUpdateReply":
        if len(frame) < 40:
            raise ValueError(f"Response too short for CPS status trailer: {len(frame)} bytes")

        trailer = frame[-40:]
        return cls(
            raw_frame=frame,
            op_result=trailer[0],
            session_state=trailer[1],
            active_image=trailer[2],
            ready_mask=trailer[3],
            boot_hex_received=int.from_bytes(trailer[4:8], "little"),
            boot_hex_expected=int.from_bytes(trailer[8:12], "little"),
            boot_bytes_written=int.from_bytes(trailer[12:16], "little"),
            app_hex_received=int.from_bytes(trailer[16:20], "little"),
            app_hex_expected=int.from_bytes(trailer[20:24], "little"),
            app_bytes_streamed=int.from_bytes(trailer[24:28], "little"),
            last_stage=trailer[28],
            hal_status=trailer[29],
            cps_status=int.from_bytes(trailer[30:34], "little"),
            programmed_bytes=int.from_bytes(trailer[34:38], "little"),
            chip_id=int.from_bytes(trailer[38:40], "little"),
        )

    @property
    def ok(self) -> bool:
        return self.op_result == RESULT_OK

    def summary(self) -> str:
        return (
            f"result={result_name(self.op_result)} "
            f"state={state_name(self.session_state)} "
            f"image={image_name(self.active_image)} "
            f"boot={self.boot_hex_received}/{self.boot_hex_expected} "
            f"boot_wr={self.boot_bytes_written} "
            f"app={self.app_hex_received}/{self.app_hex_expected} "
            f"app_wr={self.app_bytes_streamed} "
            f"stage={stage_name(self.last_stage)} "
            f"hal=0x{self.hal_status:02X} "
            f"cps=0x{self.cps_status:08X} "
            f"prog={self.programmed_bytes} "
            f"chip=0x{self.chip_id:04X}"
        )


@dataclass(slots=True)
class CpsStepContext:
    label: str
    detail: str


def _default_logger(_: str) -> None:
    pass


def _ascii_preview(data: bytes, limit: int = 96) -> str:
    preview = data[:limit].decode("ascii", errors="replace")
    preview = preview.replace("\r", "\\r").replace("\n", "\\n")
    if len(data) > limit:
        preview += "..."
    return preview


def _exchange_once(
    sock: socket.socket,
    frame: bytes,
    timeout: float,
    log: Callable[[str], None],
    label: str,
) -> CpsUpdateReply:
    preview_tx = hex_dump_spaces(frame[:32])
    if len(frame) > 32:
        preview_tx += f" ...+{len(frame) - 32}B"
    log(f"TX [{label}] {preview_tx} ({len(frame)} B)")

    sock.settimeout(timeout)
    sock.sendall(frame)
    response = read_len_prefixed_frame(sock)

    preview_rx = hex_dump_spaces(response[:32])
    if len(response) > 32:
        preview_rx += f" ...+{len(response) - 32}B"
    log(f"RX [{label}] {preview_rx} ({len(response)} B)")

    reply = CpsUpdateReply.from_frame(response)
    log(f"   {reply.summary()}")
    return reply


def query_status(ip: str, port: int, timeout: float,
                 log: Optional[Callable[[str], None]] = None) -> CpsUpdateReply:
    logger = log or _default_logger
    with socket.create_connection((ip, port), timeout=10.0) as sock:
        reply = query_status_on_socket(sock, timeout, logger)
    return reply


def abort_session(ip: str, port: int, timeout: float,
                  log: Optional[Callable[[str], None]] = None) -> CpsUpdateReply:
    logger = log or _default_logger
    with socket.create_connection((ip, port), timeout=10.0) as sock:
        reply = abort_session_on_socket(sock, timeout, logger)
    return reply


def query_status_on_socket(sock: socket.socket, timeout: float,
                           log: Optional[Callable[[str], None]] = None) -> CpsUpdateReply:
    logger = log or _default_logger
    return _exchange_once(sock, build_status_frame(1), timeout, logger, "STATUS")


def abort_session_on_socket(sock: socket.socket, timeout: float,
                            log: Optional[Callable[[str], None]] = None) -> CpsUpdateReply:
    logger = log or _default_logger
    return _exchange_once(sock, build_abort_frame(1), timeout, logger, "ABORT")


class CpsFwUpdater:
    def __init__(
        self,
        ip: str,
        port: int,
        bootloader_hex: bytes,
        app_hex: bytes,
        chunk_size: int,
        on_log: Callable[[str], None],
        on_progress: Callable[[float, str], None],
        on_done: Callable[[bool, str], None],
        cancel_event: threading.Event,
        cmd_timeout: float = 3.0,
        on_reply: Optional[Callable[[CpsUpdateReply], None]] = None,
        step_gate: Optional[threading.Event] = None,
        on_step_pause: Optional[Callable[[CpsStepContext], None]] = None,
        sock: Optional[socket.socket] = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        if chunk_size not in SUPPORTED_CHUNK_SIZES:
            raise ValueError(f"Unsupported chunk size {chunk_size}")
        if max_retries <= 0:
            raise ValueError("max_retries must be positive")

        self.ip = ip
        self.port = port
        self.bootloader_hex = bootloader_hex
        self.app_hex = app_hex
        self.chunk_size = chunk_size
        self.on_log = on_log
        self.on_progress = on_progress
        self.on_done = on_done
        self.cancel = cancel_event
        self.cmd_timeout = cmd_timeout
        self.on_reply = on_reply
        self._step_gate = step_gate
        self._on_step_pause = on_step_pause
        self.max_retries = max_retries
        self.auto_mode = False

        self._sock: Optional[socket.socket] = sock
        self._owns_socket = sock is None
        self._seq = 1

    def run(self) -> None:
        try:
            self._connect()
            self._run_update()
            self.on_done(True, "CPS firmware update completed successfully.")
        except RuntimeError as exc:
            if str(exc) == "Cancelled by user":
                self._abort_best_effort()
            self.on_log(f"[ABORT] {exc}")
            self.on_done(False, str(exc))
        except Exception as exc:
            self._abort_best_effort()
            self.on_log(f"[ERROR] {type(exc).__name__}: {exc}")
            self.on_done(False, f"{type(exc).__name__}: {exc}")
        finally:
            self._disconnect()

    def _connect(self) -> None:
        if self._sock is not None and not self._owns_socket:
            self.on_log(f"Using shared TCP socket for {self.ip}:{self.port}.")
            return
        self.on_log(f"Connecting to {self.ip}:{self.port} ...")
        self._sock = socket.create_connection((self.ip, self.port), timeout=10.0)
        self.on_log("Connected.")

    def _disconnect(self) -> None:
        if not self._owns_socket:
            self._sock = None
            return
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.close()
        except Exception:
            pass

    def _next_seq(self) -> int:
        value = self._seq & 0xFFFF
        self._seq = (self._seq + 1) & 0xFFFF
        if self._seq == 0:
            self._seq = 1
        return value

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise RuntimeError("Cancelled by user")

    def _wait_gate(self, label: str, detail: str) -> None:
        if self._step_gate is None or self.auto_mode:
            return

        if self._on_step_pause is not None:
            self._on_step_pause(CpsStepContext(label=label, detail=detail))

        self.on_log(f"[PAUSE] {label}")
        self._step_gate.clear()
        while not self._step_gate.is_set():
            self._check_cancel()
            self._step_gate.wait(timeout=0.2)

    def _exchange(self, frame: bytes, label: str, detail: str = "") -> CpsUpdateReply:
        self._check_cancel()
        self._wait_gate(label, detail)

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._check_cancel()
            if self._sock is None:
                if self._owns_socket:
                    self._connect()
                else:
                    raise RuntimeError("Shared socket is not available")

            try:
                if attempt > 1:
                    self.on_log(f"[RETRY] {label} attempt {attempt}/{self.max_retries}")
                reply = _exchange_once(self._sock, frame, self.cmd_timeout, self.on_log, label)
            except (socket.timeout, ValueError) as exc:
                last_exc = exc
                self.on_log(
                    f"[WARN] {label} attempt {attempt}/{self.max_retries} failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                if attempt >= self.max_retries:
                    break
                continue
            except (ConnectionError, OSError) as exc:
                last_exc = exc
                self.on_log(
                    f"[WARN] {label} transport error on attempt {attempt}/{self.max_retries}: "
                    f"{type(exc).__name__}: {exc}"
                )
                if not self._owns_socket or attempt >= self.max_retries:
                    break
                self._disconnect()
                self._connect()
                continue

            if self.on_reply is not None:
                self.on_reply(reply)
            if not reply.ok:
                raise RuntimeError(f"{label} failed: {reply.summary()}")
            return reply

        if last_exc is None:
            raise RuntimeError(f"{label} failed without a response")
        raise RuntimeError(
            f"{label} transport failed after {self.max_retries} attempts: "
            f"{type(last_exc).__name__}: {last_exc}"
        )

    def _abort_best_effort(self) -> None:
        if self._sock is None:
            return
        try:
            _exchange_once(
                self._sock,
                build_abort_frame(self._next_seq()),
                self.cmd_timeout,
                self.on_log,
                "ABORT",
            )
        except Exception:
            pass

    def _run_update(self) -> None:
        boot_total = len(self.bootloader_hex)
        app_total = len(self.app_hex)

        self.on_log("=" * 72)
        self.on_log("CPS FW update session")
        self.on_log(f"Boot HEX : {boot_total} bytes")
        self.on_log(f"App HEX  : {app_total} bytes")
        self.on_log(f"Chunk    : {self.chunk_size} bytes")
        self.on_log("=" * 72)

        self.on_progress(0.02, "Begin bootloader session")
        self._exchange(
            build_begin_frame(self._next_seq(), IMAGE_BOOTLOADER, boot_total),
            "BEGIN_BOOT",
            (
                "Command: BEGIN_BOOT\n"
                "Image  : BOOT\n"
                f"HEX size: {boot_total} bytes"
            ),
        )

        boot_chunks = (boot_total + self.chunk_size - 1) // self.chunk_size
        for index, offset in enumerate(range(0, boot_total, self.chunk_size), start=1):
            chunk = self.bootloader_hex[offset: offset + self.chunk_size]
            self._exchange(
                build_data_frame(self._next_seq(), IMAGE_BOOTLOADER, offset, chunk),
                f"BOOT_DATA {index}/{boot_chunks}",
                (
                    "Command : BOOT_DATA\n"
                    f"Chunk   : {index}/{boot_chunks}\n"
                    f"Offset  : {offset}\n"
                    f"Length  : {len(chunk)} bytes\n"
                    f"Preview : {_ascii_preview(chunk)}"
                ),
            )
            pct = 0.05 + (0.25 * index / max(boot_chunks, 1))
            self.on_progress(pct, f"Bootloader chunk {index}/{boot_chunks}")

        self.on_progress(0.35, "Finalize bootloader")
        self._exchange(
            build_end_frame(self._next_seq(), IMAGE_BOOTLOADER, boot_total),
            "END_BOOT",
            (
                "Command: END_BOOT\n"
                "Image  : BOOT\n"
                f"HEX size: {boot_total} bytes"
            ),
        )

        self.on_progress(0.40, "Begin app session")
        self._exchange(
            build_begin_frame(self._next_seq(), IMAGE_APPLICATION, app_total),
            "BEGIN_APP",
            (
                "Command: BEGIN_APP\n"
                "Image  : APP\n"
                f"HEX size: {app_total} bytes"
            ),
        )

        app_chunks = (app_total + self.chunk_size - 1) // self.chunk_size
        for index, offset in enumerate(range(0, app_total, self.chunk_size), start=1):
            chunk = self.app_hex[offset: offset + self.chunk_size]
            self._exchange(
                build_data_frame(self._next_seq(), IMAGE_APPLICATION, offset, chunk),
                f"APP_DATA {index}/{app_chunks}",
                (
                    "Command : APP_DATA\n"
                    f"Chunk   : {index}/{app_chunks}\n"
                    f"Offset  : {offset}\n"
                    f"Length  : {len(chunk)} bytes\n"
                    f"Preview : {_ascii_preview(chunk)}"
                ),
            )
            pct = 0.45 + (0.45 * index / max(app_chunks, 1))
            self.on_progress(pct, f"App chunk {index}/{app_chunks}")

        self.on_progress(0.92, "Finalize app image")
        self._exchange(
            build_end_frame(self._next_seq(), IMAGE_APPLICATION, app_total),
            "END_APP",
            (
                "Command: END_APP\n"
                "Image  : APP\n"
                f"HEX size: {app_total} bytes"
            ),
        )

        self.on_progress(0.97, "Execute verify/reset flow")
        self._exchange(
            build_execute_frame(self._next_seq()),
            "EXECUTE",
            "Command: EXECUTE\nAction : verify, reset, chip-id check",
        )
        self.on_progress(1.0, "Done")
