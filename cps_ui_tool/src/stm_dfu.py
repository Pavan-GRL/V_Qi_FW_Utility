from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

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
        return usb.backend.libusb1.get_backend()

    _USB_BACKEND = _get_libusb_backend()
    HAS_USB = True
except ImportError:
    _USB_BACKEND = None
    HAS_USB = False


REQ_DETACH = 0
REQ_DNLOAD = 1
REQ_UPLOAD = 2
REQ_GETSTATUS = 3
REQ_CLRSTATUS = 4
REQ_GETSTATE = 5
REQ_ABORT = 6

STATE_NAMES = {
    0x00: "appIDLE",
    0x01: "appDETACH",
    0x02: "dfuIDLE",
    0x03: "dfuDNLOAD-SYNC",
    0x04: "dfuDNBUSY",
    0x05: "dfuDNLOAD-IDLE",
    0x06: "dfuMANIFEST-SYNC",
    0x07: "dfuMANIFEST",
    0x08: "dfuMANIFEST-WAIT-RESET",
    0x09: "dfuUPLOAD-IDLE",
    0x0A: "dfuERROR",
}

STATUS_NAMES = {
    0x00: "OK",
    0x01: "errTARGET",
    0x02: "errFILE",
    0x03: "errWRITE",
    0x04: "errERASE",
    0x05: "errCHECK_ERASED",
    0x06: "errPROG",
    0x07: "errVERIFY",
    0x08: "errADDRESS",
    0x09: "errNOTDONE",
    0x0A: "errFIRMWARE",
    0x0B: "errVENDOR",
    0x0C: "errUSBR",
    0x0D: "errPOR",
    0x0E: "errUNKNOWN",
    0x0F: "errSTALLEDPKT",
}

_STATE_BUSY = {0x03, 0x04, 0x06, 0x07}
_STATE_DFU_IDLEISH = {0x02, 0x05, 0x09}
_MEMORY_SEG_RE = re.compile(r"(?P<count>\d+)\*(?P<size>\d+)(?P<unit>[BKM])(?P<attr>[a-zA-Z])")

# STM32H755 dual-bank ping-pong constants
_OPTSR_CUR_ADDR = 0x5200201C  # FLASH_OPTSR_CUR  (Bank1 flash-ctrl base + 0x1C)
_OPTSR_PRG_ADDR = 0x52002020  # FLASH_OPTSR_PRG  (Bank1 flash-ctrl base + 0x20)
INACTIVE_BANK_BASE = 0x08100000  # CPU address of the bank not boot-mapped at 0x08000000
PINGPONG_CM4_OFFSET = 0x000A0000
PINGPONG_MAX_IMAGE_SIZE = 0x00100000
_SWAP_BANK_BIT  = 1 << 31     # Bit 31 of OPTSR_CUR / OPTSR_PRG
PINGPONG_WRITE_CHUNK = 1024
PINGPONG_WRITE_LOG_EVERY = 16
PINGPONG_PROGRESS_EVERY = 8
_CUBEPROG_CLI_CANDIDATES = (
    r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe",
    r"C:\ST\STM32CubeIDE_1.19.0\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.win32_2.2.200.202503041107\tools\bin\STM32_Programmer_CLI.exe",
)
_CUBEPROG_BUNDLED_REL = Path("tools") / "STM32CubeProgrammer" / "bin" / "STM32_Programmer_CLI.exe"


def state_name(value: int) -> str:
    return STATE_NAMES.get(value, f"0x{value:02X}")


def status_name(value: int) -> str:
    return STATUS_NAMES.get(value, f"0x{value:02X}")


def find_cubeprogrammer_cli() -> Optional[str]:
    env_override = os.environ.get("STM32_CUBE_PROGRAMMER_CLI", "").strip()
    if env_override and os.path.isfile(env_override):
        return env_override

    bundle_bases = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundle_bases.append(Path(meipass))
    if getattr(sys, "executable", ""):
        bundle_bases.append(Path(sys.executable).resolve().parent)
    bundle_bases.extend((Path.cwd(), Path(__file__).resolve().parent.parent))

    for base in bundle_bases:
        candidate = base / _CUBEPROG_BUNDLED_REL
        if candidate.is_file():
            return str(candidate)

    path_env = os.environ.get("PATH", "")
    for folder in path_env.split(os.pathsep):
        if not folder:
            continue
        candidate = os.path.join(folder, "STM32_Programmer_CLI.exe")
        if os.path.isfile(candidate):
            return candidate
    for candidate in _CUBEPROG_CLI_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def cubeprogrammer_flash_inactive_bank(
    device: "StmDfuDeviceInfo",
    image_path: str,
    log: Callable[[str], None],
    progress: Callable[[float, str], None],
    cancel_event: threading.Event,
) -> None:
    cli = find_cubeprogrammer_cli()
    if cli is None:
        raise RuntimeError("STM32_Programmer_CLI.exe was not found")

    connect_args = [
        "-c",
        "port=usb1",
        f"VID=0x{device.vid:04X}",
        f"PID=0x{device.pid:04X}",
    ]
    if device.serial_number:
        connect_args.append(f"sn={device.serial_number}")

    cmd = [
        cli,
        *connect_args,
        "-w",
        image_path,
        f"0x{INACTIVE_BANK_BASE:08X}",
        "-v",
    ]
    display_cmd = " ".join(f'"{item}"' if " " in item else item for item in cmd)
    log(f"[CubeProg] {display_cmd}")
    progress(0.05, "CubeProgrammer: writing inactive bank...")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            if cancel_event.is_set():
                proc.terminate()
                raise RuntimeError("Cancelled by user")
            line = line.rstrip()
            if line:
                log(f"[CubeProg] {line}")
        rc = proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()

    if rc != 0:
        raise RuntimeError(f"STM32_Programmer_CLI failed with exit code {rc}")
    progress(0.90, "CubeProgrammer: inactive bank write complete.")


@dataclass(slots=True)
class DfuStatus:
    status: int
    poll_timeout_ms: int
    state: int
    i_string: int
    raw: bytes = b""

    def summary(self) -> str:
        raw_text = f" raw={self.raw.hex(' ').upper()}" if self.raw else ""
        return (
            f"status={status_name(self.status)} "
            f"state={state_name(self.state)} "
            f"poll={self.poll_timeout_ms}ms"
            f"{raw_text}"
        )


@dataclass(slots=True)
class MemorySector:
    start: int
    size: int
    attr: str


@dataclass(slots=True)
class DfuElement:
    alt_setting: int
    address: int
    data: bytes
    name: str


@dataclass(slots=True)
class StmDfuDeviceInfo:
    key: str
    vid: int
    pid: int
    bus: Optional[int]
    address: Optional[int]
    serial_number: str
    manufacturer: str
    product: str
    configuration_value: int
    interface_number: int
    alt_setting: int
    protocol: int
    transfer_size: int
    dfu_version: int
    attributes: int
    interface_string: str
    memory_name: str
    memory_base: Optional[int]
    sectors: list[MemorySector]

    def label(self) -> str:
        parts = [f"{self.vid:04X}:{self.pid:04X}"]
        if self.product:
            parts.append(self.product)
        parts.append(f"alt{self.alt_setting}")
        if self.serial_number:
            parts.append(f"SN:{self.serial_number}")
        return " | ".join(parts)


def _safe_get_string(dev, index: int) -> str:
    if not index:
        return ""
    try:
        value = usb.util.get_string(dev, index)
        return value or ""
    except Exception:
        return ""


def _parse_dfu_functional_descriptor(extra: bytes) -> tuple[int, int]:
    idx = 0
    while idx + 2 <= len(extra):
        length = extra[idx]
        dtype = extra[idx + 1]
        if length < 2:
            break
        if dtype == 0x21 and idx + length <= len(extra) and length >= 9:
            attrs = extra[idx + 2]
            transfer_size = int.from_bytes(extra[idx + 5: idx + 7], "little")
            dfu_version = int.from_bytes(extra[idx + 7: idx + 9], "little")
            return attrs, transfer_size or 2048 if transfer_size else 2048, dfu_version
        idx += length
    return 0, 2048, 0x011A


def _parse_memory_map(text: str) -> tuple[str, Optional[int], list[MemorySector]]:
    if not text or not text.startswith("@"):
        return text.strip(), None, []
    parts = [part.strip() for part in text.split("/") if part.strip()]
    if len(parts) < 3:
        return text.strip(), None, []
    name = parts[0][1:].strip()
    try:
        base = int(parts[1], 16)
    except ValueError:
        return name, None, []
    sectors: list[MemorySector] = []
    cursor = base
    for token in parts[2].split(","):
        match = _MEMORY_SEG_RE.fullmatch(token.strip())
        if not match:
            continue
        count = int(match.group("count"))
        size = int(match.group("size"))
        unit = match.group("unit").upper()
        attr = match.group("attr")
        if unit == "K":
            size *= 1024
        elif unit == "M":
            size *= 1024 * 1024
        for _ in range(count):
            sectors.append(MemorySector(start=cursor, size=size, attr=attr))
            cursor += size
    return name, base, sectors


def enumerate_dfu_devices() -> list[StmDfuDeviceInfo]:
    if not HAS_USB:
        return []

    infos: list[StmDfuDeviceInfo] = []
    seen_keys: set[str] = set()
    devices = usb.core.find(find_all=True, backend=_USB_BACKEND)
    for dev in devices or []:
        manufacturer = _safe_get_string(dev, getattr(dev, "iManufacturer", 0))
        product = _safe_get_string(dev, getattr(dev, "iProduct", 0))
        serial = _safe_get_string(dev, getattr(dev, "iSerialNumber", 0))
        bus = getattr(dev, "bus", None)
        address = getattr(dev, "address", None)

        for cfg in dev:
            for intf in cfg:
                if intf.bInterfaceClass != 0xFE or intf.bInterfaceSubClass != 0x01:
                    continue
                extra = bytes(getattr(intf, "extra_descriptors", b"") or b"")
                attrs, transfer_size, dfu_version = _parse_dfu_functional_descriptor(extra)
                interface_string = _safe_get_string(dev, intf.iInterface)
                memory_name, memory_base, sectors = _parse_memory_map(interface_string)
                key = (
                    f"{dev.idVendor:04X}:{dev.idProduct:04X}:"
                    f"{serial or f'{bus}-{address}'}:"
                    f"{cfg.bConfigurationValue}:{intf.bInterfaceNumber}:{intf.bAlternateSetting}"
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                infos.append(
                    StmDfuDeviceInfo(
                        key=key,
                        vid=dev.idVendor,
                        pid=dev.idProduct,
                        bus=bus,
                        address=address,
                        serial_number=serial,
                        manufacturer=manufacturer,
                        product=product,
                        configuration_value=cfg.bConfigurationValue,
                        interface_number=intf.bInterfaceNumber,
                        alt_setting=intf.bAlternateSetting,
                        protocol=intf.bInterfaceProtocol,
                        transfer_size=transfer_size,
                        dfu_version=dfu_version,
                        attributes=attrs,
                        interface_string=interface_string,
                        memory_name=memory_name,
                        memory_base=memory_base,
                        sectors=sectors,
                    )
                )
    infos.sort(key=lambda item: (item.vid, item.pid, item.product, item.alt_setting, item.serial_number))
    return infos


def parse_dfuse_file(path: str) -> list[DfuElement]:
    data = open(path, "rb").read()
    if len(data) < 27:
        raise ValueError("DFU file is too small")
    if data[:5] != b"DfuSe":
        raise ValueError("File does not start with DfuSe signature")
    targets = data[10]
    offset = 11
    elements: list[DfuElement] = []
    for _ in range(targets):
        if offset + 274 > len(data):
            raise ValueError("Truncated target header in DFU file")
        if data[offset: offset + 6] != b"Target":
            raise ValueError("Invalid target signature in DFU file")
        alt_setting = data[offset + 6]
        target_named = data[offset + 7]
        name_raw = data[offset + 8: offset + 263]
        name = name_raw.split(b"\0", 1)[0].decode("ascii", errors="ignore").strip()
        target_size = int.from_bytes(data[offset + 266: offset + 270], "little")
        num_elements = int.from_bytes(data[offset + 270: offset + 274], "little")
        offset += 274
        target_end = offset + target_size
        for index in range(num_elements):
            if offset + 8 > len(data):
                raise ValueError("Truncated element header in DFU file")
            address = int.from_bytes(data[offset: offset + 4], "little")
            size = int.from_bytes(data[offset + 4: offset + 8], "little")
            offset += 8
            if offset + size > len(data):
                raise ValueError("Truncated element payload in DFU file")
            element_name = name if (target_named and name) else f"Target alt{alt_setting}"
            elements.append(
                DfuElement(
                    alt_setting=alt_setting,
                    address=address,
                    data=data[offset: offset + size],
                    name=f"{element_name} #{index + 1}",
                )
            )
            offset += size
        if offset != target_end:
            offset = target_end
    return elements


def load_flash_elements(path: str, default_address: int, default_alt: int) -> list[DfuElement]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".dfu":
        return parse_dfuse_file(path)
    if ext == ".bin":
        with open(path, "rb") as fh:
            payload = fh.read()
        return [DfuElement(alt_setting=default_alt, address=default_address, data=payload, name=os.path.basename(path))]
    raise ValueError("Only .bin and .dfu files are supported")


def describe_elements(elements: list[DfuElement]) -> str:
    total = sum(len(item.data) for item in elements)
    parts = [f"{len(elements)} image element(s)", f"{total} bytes total"]
    if elements:
        first = elements[0]
        parts.append(f"first @ 0x{first.address:08X}")
    return ", ".join(parts)


class StmDfuSession:
    def __init__(
        self,
        device: StmDfuDeviceInfo,
        log: Callable[[str], None],
        progress: Callable[[float, str], None],
        cancel_event: threading.Event,
    ):
        if not HAS_USB:
            raise RuntimeError("pyusb is not installed")
        self.device = device
        self.log = log
        self.progress = progress
        self.cancel = cancel_event
        self.dev = None

    def __enter__(self) -> "StmDfuSession":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def open(self) -> None:
        self.dev = self._locate_device()
        if self.dev is None:
            raise RuntimeError("Selected DFU device is no longer present")
        try:
            self.dev.set_configuration(self.device.configuration_value)
        except Exception:
            pass
        try:
            if self.dev.is_kernel_driver_active(self.device.interface_number):
                self.dev.detach_kernel_driver(self.device.interface_number)
        except Exception:
            pass
        usb.util.claim_interface(self.dev, self.device.interface_number)
        self.dev.set_interface_altsetting(
            interface=self.device.interface_number,
            alternate_setting=self.device.alt_setting,
        )
        self.log(
            f"Opened {self.device.label()} "
            f"(intf={self.device.interface_number}, transfer={self.device.transfer_size} B)"
        )
        self._recover_to_idle()

    def close(self) -> None:
        if self.dev is None:
            return
        try:
            usb.util.release_interface(self.dev, self.device.interface_number)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass
        self.dev = None

    def flash(
        self,
        elements: list[DfuElement],
        do_erase: bool = True,
        leave_dfu: bool = True,
    ) -> None:
        if not elements:
            raise RuntimeError("No firmware elements to flash")

        total_bytes = sum(len(item.data) for item in elements)
        written = 0
        for element_index, element in enumerate(elements, start=1):
            self._check_cancel()
            self._select_alt(element.alt_setting)
            self.log(
                f"Element {element_index}/{len(elements)}: {element.name} "
                f"alt={element.alt_setting} addr=0x{element.address:08X} size={len(element.data)}"
            )
            if do_erase:
                self._erase_for_range(element.address, len(element.data))
            else:
                self.log("Erase skipped by user setting.")
            self._set_address_pointer(element.address)
            transfer = max(64, self.device.transfer_size or 2048)
            chunks = max(1, (len(element.data) + transfer - 1) // transfer)
            for chunk_index, offset in enumerate(range(0, len(element.data), transfer), start=1):
                self._check_cancel()
                chunk = element.data[offset: offset + transfer]
                block_num = 2 + (offset // transfer)
                status = self._dnload(block_num, chunk)
                written += len(chunk)
                pct = written / max(total_bytes, 1)
                self.progress(
                    pct,
                    f"{element.name}: chunk {chunk_index}/{chunks} ({status_name(status.status)})",
                )
                self.log(
                    f"WRITE alt={element.alt_setting} block={block_num} "
                    f"addr=0x{element.address + offset:08X} len={len(chunk)} -> {status.summary()}"
                )

        if leave_dfu:
            self.log("Finalizing image and requesting manifestation.")
            try:
                status = self._dnload(0, b"")
                self.progress(1.0, f"Manifestation: {status.summary()}")
                self.log(f"MANIFEST -> {status.summary()}")
            except usb.core.USBError:
                self.progress(1.0, "Manifestation triggered; device likely reset.")
                self.log("MANIFEST -> USB device reset/disconnected as expected.")
        else:
            self.progress(1.0, "Flash payload downloaded. Device left in DFU mode.")

    def flash_pingpong(self, combined_bin: bytes) -> None:
        """
        Ping-pong dual-bank firmware update via ST ROM DFU bootloader.

        Erases and writes combined_bin to the inactive bank window (always at CPU
        0x08100000, regardless of current SWAP_BANK state).  Then reads
        FLASH_OPTSR_CUR, toggles bit 31 (SWAP_BANK), and writes the result to
        FLASH_OPTSR_PRG.  The STM32H755 option-byte programming sequence triggers
        an automatic system reset; the device boots from the newly written bank.

        combined_bin layout (must match physical flash map):
          offset 0x000000 — CM7 image  (up to 640 KB)
          offset 0x0A0000 — CM4 image  (up to 384 KB)
        """
        if not combined_bin:
            raise RuntimeError("No firmware data to flash")
        if len(combined_bin) > PINGPONG_MAX_IMAGE_SIZE:
            raise RuntimeError(
                f"Ping-pong image is {len(combined_bin)} bytes; "
                f"maximum inactive-bank payload is {PINGPONG_MAX_IMAGE_SIZE} bytes"
            )

        # ── Phase 1: Write firmware image to inactive bank (0x08100000) ────────
        self._select_alt(0)
        self.log(
            f"[PingPong] Erasing inactive bank 0x{INACTIVE_BANK_BASE:08X} "
            f"({len(combined_bin)} bytes)"
        )
        self._erase_for_range(INACTIVE_BANK_BASE, len(combined_bin))
        self._set_address_pointer(INACTIVE_BANK_BASE)

        transfer = min(max(64, self.device.transfer_size or 2048), PINGPONG_WRITE_CHUNK)
        self._write_pingpong_payload(combined_bin, transfer)

        self.toggle_swap_bank_and_reset()

    def toggle_swap_bank_and_reset(self) -> None:
        # ── Phase 2: Read OPTSR_CUR to learn the current SWAP_BANK bit ─────────
        self._select_alt(1)  # Option Bytes interface
        self._special_command(0x21, _OPTSR_CUR_ADDR)  # SET ADDRESS → OPTSR_CUR
        self._recover_to_idle()                        # dfuDNLOAD-IDLE → dfuIDLE
        raw = self.dev.ctrl_transfer(
            0xA1, REQ_UPLOAD, 2, self.device.interface_number, 4, timeout=3000
        )
        cur_optsr = struct.unpack_from("<I", bytes(raw))[0]
        swap_now = (cur_optsr >> 31) & 1
        self.log(f"[PingPong] OPTSR_CUR=0x{cur_optsr:08X}  SWAP_BANK={swap_now}")
        self.progress(0.93, f"OPTSR_CUR=0x{cur_optsr:08X}  SWAP_BANK={swap_now}")

        # ── Phase 3: Toggle SWAP_BANK and write OPTSR_PRG → device auto-resets ─
        new_optsr = cur_optsr ^ _SWAP_BANK_BIT
        self.log(
            f"[PingPong] Writing OPTSR_PRG=0x{new_optsr:08X} "
            f"(SWAP_BANK: {swap_now} → {1 - swap_now})"
        )
        self._select_alt(1)                            # recover from dfuUPLOAD-IDLE
        self._special_command(0x21, _OPTSR_PRG_ADDR)  # SET ADDRESS → OPTSR_PRG
        self._recover_to_idle()
        try:
            self._dnload(2, struct.pack("<I", new_optsr))
            self.log("[PingPong] Triggering DFU manifestation/reset")
            try:
                self._dnload(0, b"")
            except (usb.core.USBError, RuntimeError) as exc:
                self.log(
                    f"[PingPong] Manifest/reset disconnected DFU transport: "
                    f"{type(exc).__name__}: {exc}"
                )
            self.progress(
                1.0,
                f"Option bytes written (SWAP_BANK → {1 - swap_now}); device resetting...",
            )
        except usb.core.USBError:
            self.progress(
                1.0,
                "Option byte write triggered device reset (expected); new firmware will boot.",
            )
        self.log("[PingPong] Done — device resets and boots from the new bank.")

    def _write_pingpong_payload(self, combined_bin: bytes, transfer: int) -> None:
        total = len(combined_bin)
        n_chunks = max(1, (total + transfer - 1) // transfer)
        self.log(
            f"[PingPong] Using {transfer} byte write chunks with explicit SETADDR per chunk"
        )
        for idx, offset in enumerate(range(0, total, transfer), start=1):
            self._check_cancel()
            chunk = combined_bin[offset: offset + transfer]
            address = INACTIVE_BANK_BASE + offset
            self._set_address_pointer(address, log=False)
            status = self._dnload(2, chunk)
            if idx == 1 or idx == n_chunks or (idx % PINGPONG_PROGRESS_EVERY) == 0:
                self.progress(
                    idx / n_chunks * 0.90,
                    f"Writing bank: block {idx}/{n_chunks} "
                    f"@ 0x{address:08X}",
                )
            if idx == 1 or idx == n_chunks or (idx % PINGPONG_WRITE_LOG_EVERY) == 0:
                self.log(
                    f"[PingPong] WRITE {idx}/{n_chunks} 0x{address:08X} "
                    f"blk=2 len={len(chunk)} -> {status.summary()}"
                )

    def get_status(self) -> DfuStatus:
        self._check_open()
        raw = self.dev.ctrl_transfer(0xA1, REQ_GETSTATUS, 0, self.device.interface_number, 6, timeout=3000)
        data = bytes(raw)
        if len(data) != 6:
            raise RuntimeError(f"Unexpected GETSTATUS length: {len(data)}")
        return DfuStatus(
            status=data[0],
            poll_timeout_ms=data[1] | (data[2] << 8) | (data[3] << 16),
            state=data[4],
            i_string=data[5],
            raw=data,
        )

    def clear_status(self) -> None:
        self._ctrl_out(REQ_CLRSTATUS, 0, b"")

    def abort(self) -> None:
        self._ctrl_out(REQ_ABORT, 0, b"")

    def _locate_device(self):
        candidates = usb.core.find(
            find_all=True,
            idVendor=self.device.vid,
            idProduct=self.device.pid,
            backend=_USB_BACKEND,
        )
        for dev in candidates or []:
            serial = _safe_get_string(dev, getattr(dev, "iSerialNumber", 0))
            bus = getattr(dev, "bus", None)
            address = getattr(dev, "address", None)
            if self.device.serial_number:
                if serial != self.device.serial_number:
                    continue
            else:
                if self.device.bus is not None and bus is not None and bus != self.device.bus:
                    continue
                if self.device.address is not None and address is not None and address != self.device.address:
                    continue
            return dev
        return None

    def _check_open(self) -> None:
        if self.dev is None:
            raise RuntimeError("DFU device is not open")

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise RuntimeError("Cancelled by user")

    def _ctrl_out(self, request: int, block_num: int, payload: bytes) -> None:
        self._check_open()
        self.dev.ctrl_transfer(
            0x21,
            request,
            block_num,
            self.device.interface_number,
            payload,
            timeout=5000,
        )

    def _get_state(self) -> int:
        self._check_open()
        raw = self.dev.ctrl_transfer(0xA1, REQ_GETSTATE, 0, self.device.interface_number, 1, timeout=3000)
        data = bytes(raw)
        if len(data) != 1:
            raise RuntimeError(f"Unexpected GETSTATE length: {len(data)}")
        return data[0]

    def _poll_until_ready(self) -> DfuStatus:
        while True:
            self._check_cancel()
            status = self.get_status()
            if status.status not in STATUS_NAMES or status.state not in STATE_NAMES:
                raise RuntimeError(f"Malformed DFU GETSTATUS response: {status.summary()}")
            if status.status != 0:
                raise RuntimeError(f"DFU error: {status.summary()}")
            if status.state not in _STATE_BUSY:
                return status
            if status.poll_timeout_ms > 60000:
                raise RuntimeError(f"Malformed DFU poll timeout: {status.summary()}")
            time.sleep(max(status.poll_timeout_ms, 5) / 1000.0)

    def _recover_to_idle(self) -> None:
        for _ in range(5):
            state = self._get_state()
            if state in _STATE_DFU_IDLEISH:
                if state != 0x02:
                    self.abort()
                    time.sleep(0.05)
                return
            if state == 0x0A:
                self.clear_status()
                time.sleep(0.05)
                continue
            if state in {0x03, 0x04, 0x06, 0x07, 0x08, 0x09}:
                self.abort()
                time.sleep(0.05)
                continue
            if state in {0x00, 0x01}:
                raise RuntimeError(
                    "Device is in runtime mode, not DFU mode. Reboot the STM32 into USB DFU first."
                )
            time.sleep(0.05)
        raise RuntimeError(f"Unable to reach dfuIDLE from {state_name(self._get_state())}")

    def _select_alt(self, alt_setting: int) -> None:
        self._check_cancel()
        self.dev.set_interface_altsetting(
            interface=self.device.interface_number,
            alternate_setting=alt_setting,
        )
        self.log(f"Selected alt setting {alt_setting}.")
        self._recover_to_idle()

    def _dnload(self, block_num: int, payload: bytes) -> DfuStatus:
        self._ctrl_out(REQ_DNLOAD, block_num, payload)
        return self._poll_until_ready()

    def _special_command(self, command: int, address: int) -> DfuStatus:
        payload = bytes((command,)) + struct.pack("<I", address)
        return self._dnload(0, payload)

    def _set_address_pointer(self, address: int, log: bool = True) -> None:
        status = self._special_command(0x21, address)
        if log:
            self.log(f"SETADDR 0x{address:08X} -> {status.summary()}")

    def _erase_for_range(self, address: int, length: int) -> None:
        sectors = self._sectors_for_range(address, length)
        if not sectors:
            self.log("No DFU memory map found; skipping explicit erase.")
            return
        for sector in sectors:
            self._check_cancel()
            status = self._special_command(0x41, sector.start)
            self.log(
                f"ERASE 0x{sector.start:08X} len={sector.size} attr={sector.attr} -> {status.summary()}"
            )

    def _sectors_for_range(self, address: int, length: int) -> list[MemorySector]:
        if length <= 0:
            return []
        end = address + length
        matches: list[MemorySector] = []
        for sector in self.device.sectors:
            sector_end = sector.start + sector.size
            if sector_end <= address:
                continue
            if sector.start >= end:
                break
            matches.append(sector)
        return matches
