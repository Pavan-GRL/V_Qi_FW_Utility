from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import queue
import socket
import threading
from datetime import datetime
from typing import Optional
import traceback

from net_client import NetClient
from usb_client import USBClient, HAS_USB
from logger import LOG_DIR, append_text
from protocol import hex_dump_multiline
from ui_plot import PlotPanel
from udp_fsk import FSKReceiver
from udp_fsk_r2 import FSKR2Receiver
from esp_flasher import (
    ESPFlasher,
    CHIP_NAMES,
    DEFAULT_FLASH_BAUD,
    format_fw_update_plan,
)
from cps_fw_updater import (
    CpsFwUpdater,
    CpsUpdateReply,
    CpsStepContext,
    DEFAULT_CHUNK_SIZE as CPS_DEFAULT_CHUNK_SIZE,
    SUPPORTED_CHUNK_SIZES as CPS_CHUNK_SIZES,
    abort_session as cps_abort_session,
    abort_session_on_socket,
    image_name as cps_image_name,
    query_status as cps_query_status,
    query_status_on_socket,
    result_name as cps_result_name,
    stage_name as cps_stage_name,
    state_name as cps_state_name,
)
from project_paths import LOGS_DIR, ensure_dir, resource_path


# ── GRL Brand Colors ──────────────────────────────────────────────────────────
_C_BLUE      = "#003087"   # GRL navy blue (primary)
_C_BLUE_LT   = "#1A50A5"   # lighter blue for hover
_C_BLUE_DK   = "#002060"   # darker blue for disabled state
_C_BLUE_TAB  = "#C8D8F0"   # inactive notebook tab
_C_WHITE     = "#FFFFFF"
_C_BG        = "#F0F4FB"   # app background (very light blue-white)
_C_TEXT      = "#002060"   # dark navy text on light backgrounds
_C_TEXT_DIM  = "#4A4A4A"   # dimmed text


PORT_DEFAULT = 5002

CPS_API_PORT_DEFAULT = PORT_DEFAULT
_CPS_BOOT_HEX_DEFAULT = (
    r"D:\V_QI\CPS\FW_Donwload\03_CPS4041_Bootloader\CPS4041_BL_V0.1.hex"
)
_CPS_APP_HEX_DEFAULT = (
    r"D:\V_QI\CPS\CPS_FW_Releases\V1.0_release_for_sync_rectifier\01_Firmware\CPS4041_00_00_V1.0_CRCF566.hex"
)
CPS_BOOT_HEX_DEFAULT = _CPS_BOOT_HEX_DEFAULT if os.path.isfile(_CPS_BOOT_HEX_DEFAULT) else ""
CPS_APP_HEX_DEFAULT = _CPS_APP_HEX_DEFAULT if os.path.isfile(_CPS_APP_HEX_DEFAULT) else ""

# --------------- Register database (from regset.txt) ---------------
REGISTERS: list[tuple[str, int, int]] = [
    ("CHIP_ID",                     0x0000, 2),
    ("FW_MINOR",                    0x0008, 1),
    ("FW_MAJOR",                    0x0009, 1),
    ("SYS_MODE",                    0x000A, 1),
    ("INT_EN",                      0x0020, 4),
    ("INT_FLAG",                    0x0024, 4),
    ("INT_CLR",                     0x0028, 4),
    ("CMD",                         0x002C, 4),
    ("FUNC_EN",                     0x0030, 4),
    ("PPP_HEADER",                  0x0050, 1),
    ("PPP_COMMAND",                 0x0051, 1),
    ("PPP_DATA0",                   0x0052, 1),
    ("PPP_DATA1",                   0x0053, 1),
    ("PPP_DATA2",                   0x0054, 1),
    ("PPP_DATA3",                   0x0055, 1),
    ("PPP_DATA[26]",                0x0056, 1),
    ("BC_HEADER",                   0x0070, 1),
    ("BC_COMMAND",                  0x0071, 1),
    ("BC_DATA0",                    0x0072, 1),
    ("BC_DATA1",                    0x0073, 1),
    ("BC_DATA2",                    0x0074, 1),
    ("BC_DATA3",                    0x0075, 1),
    ("BC_DATA[26]",                 0x0076, 1),
    ("VOUT_SET",                    0x0090, 2),
    ("FC_VPA_VOLTAGE",              0x0092, 2),
    ("FC_MLDO_VOLTAGE",             0x0094, 2),
    ("FC_BOOST_MODE",               0x0096, 1),
    ("SYS_MODE_CONFIG",             0x0097, 1),
    ("FC_DELTA",                    0x0098, 2),
    ("WATCHDOG",                    0x009A, 2),
    ("DUMMY_LOAD_NO_MOD_VAL",       0x00A8, 1),
    ("DUMMY_LOAD_MOD_VAL",          0x00A9, 1),
    ("SW_OCP_TH",                   0x00B2, 2),
    ("EPT_VAL",                     0x00BE, 1),
    ("MAXPOWER",                    0x00BF, 1),
    ("RP_CURRENT_0",                0x00C0, 1),
    ("RP_CURRENT_1",                0x00C1, 1),
    ("RP_CURRENT_2",                0x00C2, 1),
    ("RP_CURRENT_3",                0x00C3, 1),
    ("RP_CURRENT_4",                0x00C4, 1),
    ("RP_CURRENT_5",                0x00C5, 1),
    ("RP_CURRENT_6",                0x00C6, 1),
    ("FOD_C0_GAIN",                 0x00C7, 1),
    ("FOD_C0_OFFSET",               0x00C8, 1),
    ("FOD_C1_GAIN",                 0x00C9, 1),
    ("FOD_C1_OFFSET",               0x00CA, 1),
    ("FOD_C2_GAIN",                 0x00CB, 1),
    ("FOD_C2_OFFSET",               0x00CC, 1),
    ("FOD_C3_GAIN",                 0x00CD, 1),
    ("FOD_C3_OFFSET",               0x00CE, 1),
    ("FOD_C4_GAIN",                 0x00CF, 1),
    ("FOD_C4_OFFSET",               0x00D0, 1),
    ("FOD_C5_GAIN",                 0x00D1, 1),
    ("FOD_C5_OFFSET",               0x00D2, 1),
    ("FOD_C6_GAIN",                 0x00D3, 1),
    ("FOD_C6_OFFSET",               0x00D4, 1),
    ("FOD_C7_GAIN",                 0x00D5, 1),
    ("FOD_C7_OFFSET",               0x00D6, 1),
    ("CLOAK_REASON",                0x00D7, 1),
    ("PEST_OFFSET",                 0x00D8, 2),
    ("DC_CURR_MOD_BASE_LIGHT_LOAD", 0x00DA, 1),
    ("DC_CURR_MOD_BASE_HEAVY_LOAD", 0x00DB, 1),
    ("DC_CURR_MOD_DEPTH",           0x00DC, 1),
    ("FOD_QF",                      0x00DD, 1),
    ("FOD_RF",                      0x00DE, 1),
    ("ALPHA0_RX",                   0x00DF, 1),
    ("ALPHA1_RX",                   0x00E0, 1),
    ("ALPHA_KTH_RX",                0x00E1, 1),
    ("RX_COIL_RAC",                 0x00E2, 2),
    ("RX_CAP_ESR",                  0x00E4, 2),
    ("M_COIL",                      0x00E6, 2),
    ("ALPHA_FM",                    0x00E8, 2),
    ("ALPHA_FM_DC",                 0x00EA, 2),
    ("G_COIL_TX",                   0x00EC, 2),
    ("PACKET_MASK",                 0x00F0, 4),
    ("ADC_VRECT",                   0x0104, 2),
    ("ADC_MLDO_DROP",               0x0106, 2),
    ("ADC_IRECT",                   0x0108, 2),
    ("ADC_VOUT",                    0x010A, 2),
    ("ADC_TMP_DIE",                 0x010C, 2),
    ("FOP_VAL",                     0x010E, 2),
    ("RP_VRECT",                    0x0110, 2),
    ("RP_IRECT",                    0x0112, 2),
    ("SS_VAL",                      0x0114, 2),
    ("CE_VAL",                      0x0116, 2),
    ("RP_VAL",                      0x0118, 2),
    ("EPT_CODE",                    0x011A, 2),
    ("STATUS",                      0x011C, 1),
    ("OP_MODE",                     0x011D, 1),
    ("NEGO_DONE_POWER",             0x011F, 1),
    ("PTX_EXT_ID[3]",               0x0120, 1),
    ("PLA_RECT",                    0x0124, 2),
    ("EDS0_SIZE",                   0x0400, 2),
    ("EDS0_NUMBER",                 0x0402, 1),
    ("EDS0_FLAG",                   0x0403, 1),
    ("EDS0[1020]",                  0x0404, 1),
]
_REG_LOOKUP: dict[str, tuple[int, int]] = {n: (a, s) for n, a, s in REGISTERS}
_REG_NAMES: list[str] = [n for n, _, _ in REGISTERS]


def _make_check_images(style: ttk.Style) -> dict:
    """Create tick-mark checkbox indicator images and apply to TCheckbutton."""
    try:
        from PIL import Image, ImageDraw, ImageTk
        sz = 16

        # Unchecked: white box with blue border
        unchecked = Image.new("RGBA", (sz, sz), _C_WHITE)
        d = ImageDraw.Draw(unchecked)
        d.rectangle([0, 0, sz - 1, sz - 1], outline=_C_BLUE, width=2)

        # Checked: blue box with white tick
        checked = Image.new("RGBA", (sz, sz), _C_BLUE)
        d = ImageDraw.Draw(checked)
        d.rectangle([0, 0, sz - 1, sz - 1], outline=_C_BLUE, width=2)
        # Draw tick (checkmark)
        d.line([(3, 8), (6, 12), (12, 3)], fill=_C_WHITE, width=2)

        imgs = {
            "unchecked": ImageTk.PhotoImage(unchecked),
            "checked":   ImageTk.PhotoImage(checked),
        }

        style.element_create("custom_check", "image", imgs["unchecked"],
                             ("selected", imgs["checked"]))
        style.layout("TCheckbutton", [
            ("Checkbutton.padding", {"children": [
                ("custom_check", {"side": "left", "sticky": ""}),
                ("Checkbutton.focus", {"side": "left", "sticky": "", "children": [
                    ("Checkbutton.label", {"sticky": "nswe"})
                ]})
            ], "sticky": "nswe"})
        ])
        return imgs
    except Exception:
        return {}


def host_ts_short() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class App(tk.Tk):
    def _apply_theme(self):
        """Apply GRL light theme with blue accents."""
        s = ttk.Style(self)
        s.theme_use("clam")
        self.configure(bg=_C_BG)

        s.configure("TFrame",            background=_C_BG)
        s.configure("TLabel",            background=_C_BG, foreground=_C_TEXT)
        # Checkbutton with tick mark instead of X
        self._cb_imgs = _make_check_images(s)
        s.configure("TCheckbutton",      background=_C_BG, foreground=_C_TEXT)
        s.map(      "TCheckbutton",      background=[("active", _C_BG)])
        s.configure("TEntry",            fieldbackground=_C_WHITE, foreground=_C_TEXT,
                                         bordercolor=_C_BLUE, insertcolor=_C_TEXT)
        s.configure("TSpinbox",          fieldbackground=_C_WHITE, foreground=_C_TEXT,
                                         background=_C_BG, arrowcolor=_C_BLUE,
                                         bordercolor=_C_BLUE, insertcolor=_C_TEXT)
        s.configure("TCombobox",         fieldbackground=_C_WHITE, foreground=_C_TEXT,
                                         selectbackground=_C_BLUE, selectforeground=_C_WHITE,
                                         arrowcolor=_C_BLUE)
        s.map("TCombobox",               fieldbackground=[("readonly", _C_WHITE)])
        s.configure("TButton",           background=_C_BLUE, foreground=_C_WHITE,
                                         font=("Segoe UI", 9, "bold"), padding=(8, 4),
                                         borderwidth=0, relief="flat")
        s.map("TButton",                 background=[("active", _C_BLUE_LT),
                                                     ("disabled", _C_BLUE_DK)],
                                         foreground=[("disabled", _C_TEXT_DIM)])
        s.configure("TScrollbar",        background=_C_BG, troughcolor="#D0DCF0",
                                         arrowcolor=_C_BLUE, bordercolor=_C_BG)
        s.configure("TLabelframe",       background=_C_BG, bordercolor=_C_BLUE)
        s.configure("TLabelframe.Label", background=_C_BG, foreground=_C_BLUE,
                                         font=("Segoe UI", 9, "bold"))

        # Notebook tabs: inactive = light blue-gray, selected = blue + white text
        s.configure("TNotebook",         background=_C_BG, bordercolor=_C_BLUE,
                                         tabmargins=[2, 5, 2, 0])
        s.configure("TNotebook.Tab",     background=_C_BLUE_TAB, foreground=_C_TEXT,
                                         padding=[14, 6], font=("Segoe UI", 9))
        s.map("TNotebook.Tab",           background=[("selected", _C_BLUE)],
                                         foreground=[("selected", _C_WHITE)],
                                         expand=[("selected", [1, 1, 1, 0])])

        # Top bar (blue background with white accents)
        s.configure("TopBar.TFrame",     background=_C_BLUE)
        s.configure("TopBar.TLabel",     background=_C_BLUE, foreground=_C_WHITE,
                                         font=("Segoe UI", 9))
        s.configure("TopBar.TEntry",     fieldbackground=_C_WHITE, foreground=_C_TEXT,
                                         bordercolor=_C_BLUE, insertcolor=_C_TEXT)
        s.configure("TopBar.TButton",    background=_C_WHITE, foreground=_C_BLUE,
                                         font=("Segoe UI", 9, "bold"), padding=(8, 4),
                                         borderwidth=0, relief="flat")
        s.map("TopBar.TButton",          background=[("active", "#E0EAF8"),
                                                     ("disabled", _C_BLUE_TAB)],
                                         foreground=[("disabled", _C_TEXT_DIM)])

    def report_callback_exception(self, exc_type, exc_val, exc_tb):
        """Capture tkinter callback exceptions to a log file (invisible in --windowed exe)."""
        err = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
        try:
            ensure_dir(LOGS_DIR)
            with (LOGS_DIR / "callback_error.log").open("a", encoding="utf-8") as f:
                f.write(err + "\n---\n")
        except Exception:
            pass

    def __init__(self):
        super().__init__()
        self.title("GRL_V_QI_UTILITY")
        self.geometry("1250x780")
        self._apply_theme()

        # Set window/taskbar icon
        try:
            icon_path = resource_path("assets", "grl_logo.ico")
            self.iconbitmap(icon_path)
        except Exception:
            pass  # Use default icon if loading fails

        self.ui_q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.client: "NetClient | USBClient | None" = None
        self.conn_type_var = tk.StringVar(value="Ethernet")
        self._fw_log_lock = threading.Lock()
        self._fw_log_file: str | None = None

        # ---- Logo images (keep references to prevent GC) ----
        self._logo_img = None       # top-bar logo
        self._watermark_img = None  # faded centred watermark for TX/RX
        try:
            from PIL import Image, ImageTk, ImageEnhance
            _raw = Image.open(resource_path("assets", "grl_logo.png")).convert("RGBA")

            # Logo for top bar (80px height)
            _h = 80
            _w = int(_raw.width * _h / _raw.height)
            self._logo_img = ImageTk.PhotoImage(_raw.resize((_w, _h), Image.LANCZOS))

            # Watermark: larger, faded (~12% opacity)
            _wh = 200
            _ww = int(_raw.width * _wh / _raw.height)
            _wm = _raw.resize((_ww, _wh), Image.LANCZOS)
            # Reduce alpha channel to ~12%
            r, g, b, a = _wm.split()
            a = a.point(lambda p: int(p * 0.12))
            _wm = Image.merge("RGBA", (r, g, b, a))
            self._watermark_img = ImageTk.PhotoImage(_wm)
        except Exception:
            pass

        # ---------------- Top bar (navy blue) ----------------
        top = ttk.Frame(self, style="TopBar.TFrame")
        top.pack(side=tk.TOP, fill=tk.X, padx=0, pady=0)

        # Logo badge on the far right with white background for visibility
        if self._logo_img is not None:
            logo_frame = tk.Frame(top, bg=_C_WHITE, relief=tk.FLAT, bd=2)
            logo_frame.pack(side=tk.RIGHT, padx=8, pady=4)
            tk.Label(logo_frame, image=self._logo_img,
                     bg=_C_WHITE, bd=0, padx=8, pady=4).pack()

        # ── Connection type selector ──────────────────────────────────────────
        # Style for TopBar radio buttons (white text on navy, toggles on select)
        s = ttk.Style(self)
        s.configure("TopBar.TRadiobutton",
                     background=_C_BLUE, foreground=_C_WHITE,
                     font=("Segoe UI", 9), focuscolor=_C_BLUE)
        s.map("TopBar.TRadiobutton",
              background=[("active", _C_BLUE_LT)],
              foreground=[("disabled", _C_TEXT_DIM)])

        ttk.Label(top, text="Type:", style="TopBar.TLabel").pack(
            side=tk.LEFT, padx=(12, 0), pady=8)
        self.rb_eth = ttk.Radiobutton(
            top, text="Ethernet", variable=self.conn_type_var, value="Ethernet",
            style="TopBar.TRadiobutton", command=self._on_conn_type_change)
        self.rb_eth.pack(side=tk.LEFT, padx=(4, 0), pady=8)
        self.rb_usb = ttk.Radiobutton(
            top, text="USB", variable=self.conn_type_var, value="USB",
            style="TopBar.TRadiobutton", command=self._on_conn_type_change)
        self.rb_usb.pack(side=tk.LEFT, padx=(4, 10), pady=8)

        # ── Ethernet IP / Port ────────────────────────────────────────────────
        self._eth_lbl_ip = ttk.Label(top, text="IP:", style="TopBar.TLabel")
        self._eth_lbl_ip.pack(side=tk.LEFT, padx=(0, 0), pady=8)
        self.ip_var = tk.StringVar(value="192.168.255.10")
        self._eth_entry_ip = ttk.Entry(top, textvariable=self.ip_var, width=18,
                                        style="TopBar.TEntry")
        self._eth_entry_ip.pack(side=tk.LEFT, padx=(6, 6), pady=8)

        self._eth_lbl_port = ttk.Label(top, text="Port:", style="TopBar.TLabel")
        self._eth_lbl_port.pack(side=tk.LEFT, pady=8)
        self.port_var = tk.IntVar(value=PORT_DEFAULT)
        self._eth_entry_port = ttk.Entry(top, textvariable=self.port_var, width=8,
                                          style="TopBar.TEntry")
        self._eth_entry_port.pack(side=tk.LEFT, padx=(6, 12), pady=8)

        self.btn_connect = ttk.Button(top, text="Connect", command=self.on_connect,
                                      style="TopBar.TButton")
        self.btn_connect.pack(side=tk.LEFT, pady=8)

        self.btn_disconnect = ttk.Button(top, text="Disconnect", command=self.on_disconnect,
                                         state=tk.DISABLED, style="TopBar.TButton")
        self.btn_disconnect.pack(side=tk.LEFT, padx=(8, 0), pady=8)

        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(top, textvariable=self.status_var,
                  style="TopBar.TLabel").pack(side=tk.LEFT, padx=(14, 0))

        # ---------------- Tabs ----------------
        nb = ttk.Notebook(self)
        nb.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=12, pady=12)

        self.tab_stream = ttk.Frame(nb)
        nb.add(self.tab_stream, text="Console")

        self.tab_plot = ttk.Frame(nb)
        nb.add(self.tab_plot, text="Plot")

        # ---------------- Console tab (left: controls, right: TX/RX) --------
        console_wrap = ttk.Frame(self.tab_stream)
        console_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # ---- Left panel: operations ----
        left = ttk.Frame(console_wrap, width=320)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6), pady=4)
        left.pack_propagate(False)

        # Set Mode
        mode_lf = ttk.LabelFrame(left, text="Set Mode")
        mode_lf.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(4, 4))

        mode_row = ttk.Frame(mode_lf)
        mode_row.pack(fill=tk.X, padx=8, pady=8)
        self._mode_names = ["BPP", "EPP", "MPP Full", "MPP Restricted"]
        self._mode_var = tk.StringVar(value=self._mode_names[0])
        ttk.Combobox(mode_row, textvariable=self._mode_var,
                     values=self._mode_names, width=18,
                     state="readonly").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(mode_row, text="Apply",
                   command=self._send_set_mode).pack(side=tk.LEFT)

        # Masking
        mask_lf = ttk.LabelFrame(left, text="Masking")
        mask_lf.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)

        mask_row = ttk.Frame(mask_lf)
        mask_row.pack(fill=tk.X, padx=8, pady=8)
        self._mask_names = ["Mask CE", "Unmask CE", "Mask RP", "Unmask RP"]
        self._mask_var = tk.StringVar(value=self._mask_names[0])
        ttk.Combobox(mask_row, textvariable=self._mask_var,
                     values=self._mask_names, width=18,
                     state="readonly").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(mask_row, text="Apply",
                   command=self._send_mask).pack(side=tk.LEFT)

        # Packet Configuration
        pkt_lf = ttk.LabelFrame(left, text="Packet Config")
        pkt_lf.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)

        # EPT sub-section
        ept_row = ttk.Frame(pkt_lf)
        ept_row.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(ept_row, text="EPT:").pack(side=tk.LEFT, padx=(0, 6))
        self._EPT_CODES = {
            "EPT/nul":                    0x00,
            "EPT/cc (Charge Complete)":   0x01,
            "EPT/if (Internal Fault)":    0x02,
            "EPT/ot (Over Temperature)":  0x03,
            "EPT/ov (Over Voltage)":      0x04,
            "EPT/oc (Over Current)":      0x05,
            "EPT/bf (Battery Failure)":   0x06,
            "EPT/nr (No Response)":       0x08,
            "EPT/an (Aborted Negotiation)": 0x0A,
            "EPT/rst (Restart)":          0x0B,
            "EPT/rep (Re-ping)":          0x0C,
            "EPT/nfc (NFC Tag)":          0x0D,
            "EPT/ptxnfc":                 0x0E,
        }
        self._ept_options = list(self._EPT_CODES.keys())
        self._ept_var = tk.StringVar(value=self._ept_options[0])
        ttk.Combobox(ept_row, textvariable=self._ept_var,
                     values=self._ept_options, width=28,
                     state="readonly").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(ept_row, text="Apply",
                   command=self._send_ept).pack(side=tk.LEFT)

        # Packet Control (CMD register 0x002C — each action sets one bit)
        pctrl_lf = ttk.LabelFrame(left, text="Packet Control")
        pctrl_lf.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)

        pctrl_row = ttk.Frame(pctrl_lf)
        pctrl_row.pack(fill=tk.X, padx=8, pady=8)
        self._pctrl_options = [
            "SEND_DATA (Proprietary Pkt)",
            "SEND_EPT (End of Power)",
            "SYS_RESET",
            "MPP_R_TO_F (Restricted to Full)",
            "MPP_R_TO_F_EPT (R to F + EPT)",
            "MPP_CLOAK_ENTER",
            "MPP_CLOAK_EXIT",
            "MPP_EDS_EN (Start EDS)",
        ]
        self._pctrl_var = tk.StringVar(value=self._pctrl_options[0])
        ttk.Combobox(pctrl_row, textvariable=self._pctrl_var,
                     values=self._pctrl_options, width=28,
                     state="readonly").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(pctrl_row, text="Apply",
                   command=self._send_pkt_ctrl).pack(side=tk.LEFT)

        # Custom HEX
        custom_lf = ttk.LabelFrame(left, text="Custom HEX")
        custom_lf.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)

        self.custom_hex_var = tk.StringVar(value="07 08 00 00 00 08 00 A1")
        ttk.Entry(custom_lf, textvariable=self.custom_hex_var).pack(
            side=tk.TOP, fill=tk.X, padx=8, pady=(8, 4))
        ttk.Button(custom_lf, text="Send",
                   command=self.send_custom_hex).pack(
            side=tk.TOP, anchor=tk.E, padx=8, pady=(0, 8))

        # USB Control Transfer (EP0) – shown only when USB mode is active
        self._usb_ctrl_lf = ttk.LabelFrame(left, text="USB Control Transfer  (EP0)")
        # Packed/unpacked dynamically by _on_conn_type_change()

        # Row 1: bmRequestType | bRequest
        _r1 = ttk.Frame(self._usb_ctrl_lf)
        _r1.pack(fill=tk.X, padx=8, pady=(6, 2))
        ttk.Label(_r1, text="bmReqType:").pack(side=tk.LEFT)
        self._usb_bm_req_type = tk.StringVar(value="C0")
        ttk.Entry(_r1, textvariable=self._usb_bm_req_type, width=5).pack(
            side=tk.LEFT, padx=(4, 12))
        ttk.Label(_r1, text="bRequest:").pack(side=tk.LEFT)
        self._usb_b_request = tk.StringVar(value="00")
        ttk.Entry(_r1, textvariable=self._usb_b_request, width=5).pack(
            side=tk.LEFT, padx=(4, 0))

        # Row 2: wValue | wIndex
        _r2 = ttk.Frame(self._usb_ctrl_lf)
        _r2.pack(fill=tk.X, padx=8, pady=2)
        ttk.Label(_r2, text="wValue:").pack(side=tk.LEFT)
        self._usb_w_value = tk.StringVar(value="0000")
        ttk.Entry(_r2, textvariable=self._usb_w_value, width=6).pack(
            side=tk.LEFT, padx=(4, 12))
        ttk.Label(_r2, text="wIndex:").pack(side=tk.LEFT)
        self._usb_w_index = tk.StringVar(value="0000")
        ttk.Entry(_r2, textvariable=self._usb_w_index, width=6).pack(
            side=tk.LEFT, padx=(4, 0))

        # Row 3: Direction
        _r3 = ttk.Frame(self._usb_ctrl_lf)
        _r3.pack(fill=tk.X, padx=8, pady=2)
        ttk.Label(_r3, text="Direction:").pack(side=tk.LEFT)
        self._usb_dir_var = tk.StringVar(value="IN")
        ttk.Radiobutton(_r3, text="IN",  variable=self._usb_dir_var, value="IN",
                         command=self._usb_dir_changed).pack(side=tk.LEFT, padx=(6, 4))
        ttk.Radiobutton(_r3, text="OUT", variable=self._usb_dir_var, value="OUT",
                         command=self._usb_dir_changed).pack(side=tk.LEFT)

        # Row 4: OUT data / IN length (mutually toggled)
        _r4 = ttk.Frame(self._usb_ctrl_lf)
        _r4.pack(fill=tk.X, padx=8, pady=2)
        self._usb_out_data_lbl = ttk.Label(_r4, text="OUT data (hex):")
        self._usb_out_data_lbl.pack(side=tk.LEFT)
        self._usb_out_data_var = tk.StringVar(value="")
        self._usb_out_data_entry = ttk.Entry(_r4, textvariable=self._usb_out_data_var, width=16)
        self._usb_out_data_entry.pack(side=tk.LEFT, padx=(4, 0))

        _r5 = ttk.Frame(self._usb_ctrl_lf)
        _r5.pack(fill=tk.X, padx=8, pady=2)
        self._usb_in_len_lbl = ttk.Label(_r5, text="IN length (bytes):")
        self._usb_in_len_lbl.pack(side=tk.LEFT)
        self._usb_in_len_var = tk.StringVar(value="64")
        self._usb_in_len_entry = ttk.Entry(_r5, textvariable=self._usb_in_len_var, width=8)
        self._usb_in_len_entry.pack(side=tk.LEFT, padx=(4, 0))

        ttk.Button(self._usb_ctrl_lf, text="Execute Control Transfer",
                   command=self._send_usb_control).pack(
            side=tk.TOP, anchor=tk.E, padx=8, pady=(4, 8))

        # Initial direction state
        self._usb_dir_changed()

        # ---- Right panel: TX/RX monitor ----
        txrx = ttk.LabelFrame(console_wrap, text="TX/RX Monitor")
        txrx.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, pady=4, padx=(0, 6))

        self.txrx_text = tk.Text(txrx, wrap="none",
                                 bg=_C_WHITE, fg=_C_TEXT, insertbackground=_C_TEXT,
                                 selectbackground=_C_BLUE, selectforeground=_C_WHITE,
                                 font=("Consolas", 9))
        txrx_vsb = ttk.Scrollbar(txrx, orient=tk.VERTICAL, command=self.txrx_text.yview)
        self.txrx_text.configure(yscrollcommand=txrx_vsb.set)
        txrx_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.txrx_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Centred watermark overlay on TX/RX monitor
        if self._watermark_img is not None:
            wm_lbl = tk.Label(self.txrx_text, image=self._watermark_img,
                              bg=_C_WHITE, bd=0)
            wm_lbl.place(relx=0.5, rely=0.5, anchor="center")
            wm_lbl.bind("<Button-1>", lambda e: "break")  # pass clicks through
            wm_lbl.lower()  # keep behind text cursor

        # ---------------- Plot tab (decoded packet list + plot) ----------------
        # Top control bar: Start / Pause / Stop
        plot_ctrl = ttk.Frame(self.tab_plot)
        plot_ctrl.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(6, 0))

        self.btn_plot_start = ttk.Button(plot_ctrl, text="Start", command=self._plot_start)
        self.btn_plot_start.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_plot_pause = ttk.Button(plot_ctrl, text="Pause", command=self._plot_pause, state=tk.DISABLED)
        self.btn_plot_pause.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_plot_stop = ttk.Button(plot_ctrl, text="Stop", command=self._plot_stop, state=tk.DISABLED)
        self.btn_plot_stop.pack(side=tk.LEFT)

        self._plot_paused = False   # True when Pause was pressed (not fully stopped)

        plot_wrap = ttk.Frame(self.tab_plot)
        plot_wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Left: decoded packet blocks
        pkt_frame = ttk.LabelFrame(plot_wrap, text="CPS Packet List (click first line to highlight on plot)")
        pkt_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8), pady=6)

        self._pkt_vsb = ttk.Scrollbar(pkt_frame, orient=tk.VERTICAL)
        pkt_hsb = ttk.Scrollbar(pkt_frame, orient=tk.HORIZONTAL)
        self.plot_pkt_text = tk.Text(
            pkt_frame, width=62, wrap="none",
            yscrollcommand=self._pkt_yscroll_set, xscrollcommand=pkt_hsb.set,
            bg=_C_WHITE, fg=_C_TEXT, insertbackground=_C_TEXT,
            selectbackground=_C_BLUE, selectforeground=_C_WHITE,
            font=("Consolas", 9),
        )
        self._pkt_vsb.configure(command=self.plot_pkt_text.yview)
        pkt_hsb.configure(command=self.plot_pkt_text.xview)
        self._pkt_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        pkt_hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.plot_pkt_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=(6, 0), pady=6)

        # map: tag -> timestamp(ms)
        self._pkt_tag_to_ts: dict[str, int] = {}
        self._pkt_counter = 0
        self._pkt_autoscroll = True  # False when user has scrolled up
        self.plot_pkt_text.bind("<Button-1>", self.on_packet_text_click)
        self.plot_pkt_text.bind("<Return>", self._pkt_jump_to_end)

        # Right: plot
        self.plot_panel = PlotPanel(plot_wrap)  # uses line graphs
        self.plot_panel.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        bottom = ttk.Frame(self.tab_plot)
        bottom.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(bottom, text="Clear Plot + Packets", command=self.clear_plot_all).pack(side=tk.RIGHT, padx=10, pady=10)

        # ---------------- Register Access tab ----------------
        self.tab_reg = ttk.Frame(nb)
        nb.add(self.tab_reg, text="Register Access")
        self._build_reg_tab()

        # ---------------- FSK Capture tab ----------------
        self.tab_fsk = ttk.Frame(nb)
        nb.add(self.tab_fsk, text="FSK Capture")
        self._build_fsk_tab()

        # ---------------- FW Update tab ----------------
        self.tab_fw = ttk.Frame(nb)
        nb.add(self.tab_fw, text="FW Update")
        self._build_fw_tab()

        # ---------------- CPS FW Update tab ----------------
        self.tab_cps_fw = ttk.Frame(nb)
        nb.add(self.tab_cps_fw, text="CPS FW upd")
        self._build_cps_fw_tab()

        # UI loop
        self.after(50, self.process_ui_queue)

    # ---------------- Connection ----------------
    def on_connect(self):
        if self.client:
            return

        conn_type = self.conn_type_var.get()

        if conn_type == "USB":
            if not HAS_USB:
                messagebox.showerror(
                    "pyusb not installed",
                    "USB support requires pyusb.\n\nInstall it with:\n  pip install pyusb\n\n"
                    "Also ensure the WinUSB driver is installed for VID_227F/PID_0006.",
                )
                return
            self.client = USBClient(
                on_status=lambda msg: self.ui_q.put(("status", msg)),
                on_cps=lambda info: self.ui_q.put(("cps", info)),
                on_tx=lambda hexs, tag: self.ui_q.put(("tx", (hexs, tag))),
                on_rx=lambda hexs, tag: self.ui_q.put(("rx", (hexs, tag))),
                on_retry_prompt=self._retry_prompt_dialog,
            )
        else:
            ip   = self.ip_var.get().strip()
            port = int(self.port_var.get())
            self.client = NetClient(
                ip=ip,
                port=port,
                on_status=lambda msg: self.ui_q.put(("status", msg)),
                on_cps=lambda info: self.ui_q.put(("cps", info)),
                on_tx=lambda hexs, tag: self.ui_q.put(("tx", (hexs, tag))),
                on_rx=lambda hexs, tag: self.ui_q.put(("rx", (hexs, tag))),
                on_retry_prompt=self._retry_prompt_dialog,
            )

        self.client.plot_enabled = False   # Start/Pause/Stop buttons control this
        self.client.start()

        self.btn_connect.configure(state=tk.DISABLED)
        self.btn_disconnect.configure(state=tk.NORMAL)
        # Lock type selector while connected
        self.rb_eth.configure(state=tk.DISABLED)
        self.rb_usb.configure(state=tk.DISABLED)
        self.status_var.set("Connecting…" if conn_type == "Ethernet" else "Opening USB…")

    def on_disconnect(self):
        if not self.client:
            return
        self.client.stop()
        self.client = None

        self.btn_connect.configure(state=tk.NORMAL)
        self.btn_disconnect.configure(state=tk.DISABLED)
        # Re-enable type selector
        self.rb_eth.configure(state=tk.NORMAL)
        self.rb_usb.configure(state=tk.NORMAL)
        self.status_var.set("Disconnected")

    # ---------------- Connection type helpers ----------------

    def _on_conn_type_change(self):
        """Show/hide the USB Control Transfer panel and IP/Port fields."""
        is_usb = self.conn_type_var.get() == "USB"
        if is_usb:
            self._usb_ctrl_lf.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        else:
            self._usb_ctrl_lf.pack_forget()

    def _usb_dir_changed(self):
        """Toggle OUT-data vs IN-length entry fields based on direction."""
        is_in = self._usb_dir_var.get() == "IN"
        state_in  = tk.NORMAL if is_in  else tk.DISABLED
        state_out = tk.NORMAL if not is_in else tk.DISABLED
        self._usb_in_len_entry.configure(state=state_in)
        self._usb_out_data_entry.configure(state=state_out)

    def _send_usb_control(self):
        """Execute a USB EP0 control transfer and log result to TX/RX monitor."""
        if not isinstance(self.client, USBClient):
            self._txrx_line(f"{host_ts_short()}  ⚠ USB not connected")
            return
        try:
            bm   = int(self._usb_bm_req_type.get().strip(), 16)
            br   = int(self._usb_b_request.get().strip(), 16)
            wv   = int(self._usb_w_value.get().strip(), 16)
            wi   = int(self._usb_w_index.get().strip(), 16)
        except ValueError as e:
            self._txrx_line(f"{host_ts_short()}  ✖ Invalid control field: {e}")
            return

        direction = self._usb_dir_var.get()
        log_req = (
            f"bmReqType=0x{bm:02X} bRequest=0x{br:02X} "
            f"wValue=0x{wv:04X} wIndex=0x{wi:04X} dir={direction}"
        )
        try:
            if direction == "OUT":
                raw = self._usb_out_data_var.get().strip()
                data = bytes.fromhex(raw.replace(" ", "")) if raw else b""
                self.client.send_control(bm, br, wv, wi, data)
                self._txrx_line(
                    f"{host_ts_short()}  USB-CTRL OUT: {log_req}  "
                    f"data=[{' '.join(f'{x:02X}' for x in data)}]"
                )
            else:  # IN
                length = int(self._usb_in_len_var.get().strip())
                result = self.client.send_control(bm, br, wv, wi, length)
                rx_hex = " ".join(f"{x:02X}" for x in result) if result else "(empty)"
                self._txrx_line(
                    f"{host_ts_short()}  USB-CTRL IN:  {log_req}  "
                    f"len={length}  rx=[{rx_hex}]"
                )
        except Exception as e:
            self._txrx_line(f"{host_ts_short()}  ✖ Control transfer error: {e}")

    # ---------------- Operations ----------------
    # Masking: 01 09 00 01 00 02 00 <target> <1=mask, 0=unmask>
    #   target 0x00 = CE, target 0x01 = RP
    _MASK_CMDS = {
        "Mask CE":    "01 09 00 01 00 02 00 00 01",
        "Unmask CE":  "01 09 00 01 00 02 00 00 00",
        "Mask RP":    "01 09 00 01 00 02 00 01 01",
        "Unmask RP":  "01 09 00 01 00 02 00 01 00",
    }

    def _send_mask(self):
        name = self._mask_var.get()
        cmd = self._MASK_CMDS.get(name)
        if cmd:
            self._send_hex(cmd, note=name)

    _MODE_CMDS = {
        "BPP":              "01 09 00 01 00 06 00 01 00",
        "EPP":              "01 09 00 01 00 06 00 01 01",
        "MPP Full":         "01 09 00 01 00 06 00 01 02",
        "MPP Restricted":   "01 09 00 01 00 06 00 01 03",
    }

    def _send_set_mode(self):
        name = self._mode_var.get()
        cmd = self._MODE_CMDS.get(name)
        if cmd:
            self._send_hex(cmd, note=f"Set Mode → {name}")

    # Packet Config: 01 08 00 01 00 03 00 YY ZZ
    #   YY=0x01 → EPT value config, ZZ = EPT code
    def _send_ept(self):
        sel = self._ept_var.get()
        zz = self._EPT_CODES.get(sel, 0x00)
        cmd = f"01 08 00 01 00 03 00 01 {zz:02X}"
        self._send_hex(cmd, note=f"Pkt Config EPT → {sel}")

    # Packet Control: 01 08 00 01 00 04 00 XX
    #   XX = byte with the selected bit set
    _PCTRL_BITS = {
        "SEND_DATA (Proprietary Pkt)":      0,
        "SEND_EPT (End of Power)":          2,
        "SYS_RESET":                        3,
        "MPP_R_TO_F (Restricted to Full)":  6,
        "MPP_R_TO_F_EPT (R to F + EPT)":   7,
        "MPP_CLOAK_ENTER":                  8,
        "MPP_CLOAK_EXIT":                   9,
        "MPP_EDS_EN (Start EDS)":           11,
    }

    def _send_pkt_ctrl(self):
        sel = self._pctrl_var.get()
        bit_num = self._PCTRL_BITS.get(sel)
        if bit_num is None:
            return
        xx = 1 << bit_num
        cmd = f"01 08 00 01 00 04 00 {xx:02X}"
        self._send_hex(cmd, note=f"Pkt Ctrl → {sel}")

    def send_custom_hex(self):
        s = self.custom_hex_var.get().strip()
        self._send_hex(s, note="Custom HEX")

    def _send_hex(self, hex_str: str, note: str):
        if not self.client:
            self._txrx_line(f"{host_ts_short()}  ⚠ Not connected")
            return
        try:
            data = bytes.fromhex(hex_str.replace(" ", "").strip())
        except ValueError as e:
            self._txrx_line(f"{host_ts_short()}  ✖ Invalid hex ({note}): {e}")
            return

        # Validate declared byte-count (bytes[1:3] LE) against actual length
        if len(data) >= 3:
            declared = data[1] | (data[2] << 8)
            actual   = len(data)
            if declared != actual:
                self._txrx_line(
                    f"{host_ts_short()}  ✖ Byte-count mismatch ({note}): "
                    f"header says {declared} bytes but frame has {actual} bytes — please fix bytes[1:2]"
                )
                return

        try:
            self.client.send_hex(hex_str, tag="MANUAL")
            self._txrx_line(f"{host_ts_short()}  TX ({note}) : {hex_str}")
        except Exception as e:
            self._txrx_line(f"{host_ts_short()}  ✖ TX error: {e}")

    def _send_bytes(self, b: bytes, note: str):
        if not self.client:
            self._txrx_line(f"{host_ts_short()}  ⚠ Not connected")
            return
        try:
            self.client.send_bytes(b, tag="MANUAL")
            self._txrx_line(f"{host_ts_short()}  TX ({note}) : {' '.join(f'{x:02X}' for x in b)}")
        except Exception as e:
            self._txrx_line(f"{host_ts_short()}  ✖ TX error: {e}")

    def _retry_prompt_dialog(self, hex_str: str, tag: str) -> bool:
        """
        Show a dialog asking user if they want to retry a failed command.
        Called from the NetClient thread when a MANUAL command gets no response.
        Returns True if user wants to retry, False to skip.
        """
        # This is called from the network thread, so we need to use messagebox
        # which is thread-safe in tkinter
        result = messagebox.askyesno(
            "Command Failed",
            f"No response received for {tag} command after 3 retries:\n\n"
            f"{hex_str}\n\n"
            f"Check the firmware and press:\n"
            f"• YES to retry the command\n"
            f"• NO to skip and continue",
            icon=messagebox.WARNING
        )
        return result

    def _fw_retry_prompt_dialog(self, step_name: str, detail: str) -> bool:
        """
        Show a dialog asking user if they want to retry a failed FW update step.
        Called from the ESPFlasher thread when a firmware operation fails.
        Returns True if user wants to retry, False to abort.
        """
        result = messagebox.askyesno(
            f"Firmware Update: {step_name}",
            f"{detail}\n\n"
            f"Press:\n"
            f"• YES to retry this operation\n"
            f"• NO to abort firmware update",
            icon=messagebox.ERROR
        )
        return result

    # ---------------- UI queue processing ----------------
    def process_ui_queue(self):
        fsk_lines: list[str] = []
        fsk_packets = 0
        fsk_r2_lines: list[str] = []
        fsk_r2_packets = 0
        try:
            while True:
                kind, payload = self.ui_q.get_nowait()

                if kind == "status":
                    self.status_var.set(str(payload))
                    self._txrx_line(f"{host_ts_short()}  ℹ {payload}")

                elif kind == "rx":
                    hexs, tag = payload  # type: ignore
                    if tag == "MANUAL_RSP":
                        line = f"{host_ts_short()}  RX GET-Response : {hexs}"
                        self._txrx_line(line)
                        self._reg_log(line)
                    elif tag == "MANUAL_ECHO":
                        line = f"{host_ts_short()}  RX SET-Echo     : {hexs}"
                        self._txrx_line(line)
                        self._reg_log(line)

                elif kind == "cps":
                    self._render_cps_to_plot_tab(payload)  # type: ignore

                elif kind == "fsk":
                    seq, rec_count, preview_records = payload  # type: ignore
                    fsk_packets += 1
                    fsk_lines.append(self._fsk_format_packet_summary(seq, rec_count, preview_records))

                elif kind == "fsk_r2":
                    seq, rec_count, preview_records = payload  # type: ignore
                    fsk_r2_packets += 1
                    fsk_r2_lines.append(
                        self._fsk_r2_format_packet_summary(seq, rec_count, preview_records)
                    )

                elif kind == "fsk_r2_status":
                    self._fsk_r2_status_var.set(str(payload))
                    self._fsk_r2_append_batch(f"[status] {payload}\n", 0)

        except queue.Empty:
            pass
        finally:
            if fsk_lines:
                self._fsk_append_batch("".join(fsk_lines), fsk_packets)
            if fsk_r2_lines:
                self._fsk_r2_append_batch("".join(fsk_r2_lines), fsk_r2_packets)
            self.after(50, self.process_ui_queue)

    # ---------------- CPS decoded block printing + plot update ----------------
    @staticmethod
    def _bytes_hex(b: bytes) -> str:
        return " ".join(f"{x:02X}" for x in b)

    def _insert_packet_block(self, block: str, adjusted_ts_ms: float | None):
        """
        First line is clickable only if we have an adjusted_ts_ms to highlight on plot.
        """
        self._pkt_counter += 1
        tag = f"pkt_{self._pkt_counter}"

        lines = block.splitlines()
        if not lines:
            return

        start = self.plot_pkt_text.index("end-1c")
        self.plot_pkt_text.insert("end", lines[0] + "\n")
        end = self.plot_pkt_text.index("end-1c")

        if adjusted_ts_ms is not None:
            self.plot_pkt_text.tag_add(tag, start, end)
            self.plot_pkt_text.tag_configure(tag, underline=True)
            self.plot_pkt_text.tag_bind(tag, "<Enter>", lambda e: self.plot_pkt_text.config(cursor="hand2"))
            self.plot_pkt_text.tag_bind(tag, "<Leave>", lambda e: self.plot_pkt_text.config(cursor=""))
            self._pkt_tag_to_ts[tag] = float(adjusted_ts_ms)
        else:
            # non-clickable if no packet_ts / no plot point
            self.plot_pkt_text.tag_bind(tag, "<Enter>", lambda e: None)

        for ln in lines[1:]:
            self.plot_pkt_text.insert("end", ln + "\n")
        self.plot_pkt_text.insert("end", "\n")
        if self._pkt_autoscroll:
            self.plot_pkt_text.see("end")


    def _render_cps_to_plot_tab(self, info: dict):
        """
        Plot using packet_ts (cps_ts_bytes) NOT pwr_ts.
        If payload_valid==0 (no packet_ts), we skip plotting.
        """
        pwr_ts_bytes: bytes = info["pwr_ts_bytes"]
        pwr_ts_le = int.from_bytes(pwr_ts_bytes, "little")

        rect_v = info.get("rect_v")
        rect_i = info.get("rect_i")

        cps_ts_bytes = info.get("cps_ts_bytes")
        payload_valid = int(info.get("payload_valid", 0))

        # We only plot when packet_ts exists (payload_valid==1)
        packet_ts_le: int | None = None
        if payload_valid == 1 and cps_ts_bytes is not None:
            packet_ts_le = int.from_bytes(cps_ts_bytes, "little")

        ascii_text = ""
        if payload_valid == 1:
            ascii_text = info.get("payload", b"").decode("ascii", errors="replace").rstrip()

        # Build decoded block (packet list)
        lines = []
        lines.append(f"pwr_ts        : {self._bytes_hex(pwr_ts_bytes)} (le={pwr_ts_le})")
        lines.append(f"rect_v        : {rect_v}")
        lines.append(f"rect_i        : {rect_i}")

        if packet_ts_le is not None:
            lines.append(f"packet_ts     : {self._bytes_hex(cps_ts_bytes)} (le={packet_ts_le})")  # type: ignore

        if ascii_text:
            for ln in ascii_text.splitlines():
                lines.append(ln)

        if rect_v is not None:
            lines.append(f"Vrect={rect_v}")

        block = "\n".join(lines)

        # Insert clickable packet block.
        # For hyperlink highlight, store the *adjusted* plot timestamp.
        adjusted_ts = None
        if packet_ts_le is not None and rect_v is not None and rect_i is not None:
            adjusted_ts = self.plot_panel.push_point(packet_ts_le, int(rect_v), int(rect_i))

        self._insert_packet_block(block, adjusted_ts)

    def _pkt_yscroll_set(self, first: str, last: str):
        """yscrollcommand wrapper — updates scrollbar and autoscroll flag for ALL scroll sources."""
        self._pkt_vsb.set(first, last)
        self._pkt_autoscroll = float(last) >= 0.999

    def _pkt_jump_to_end(self, event=None):
        """Return key: jump to latest packet and re-enable autoscroll."""
        self.plot_pkt_text.see("end")
        self._pkt_autoscroll = True
        return "break"  # suppress newline insertion

    def on_packet_text_click(self, event):
        idx = self.plot_pkt_text.index(f"@{event.x},{event.y}")
        tags = self.plot_pkt_text.tag_names(idx)
        for t in tags:
            if t.startswith("pkt_") and t in self._pkt_tag_to_ts:
                adj_ts = self._pkt_tag_to_ts[t]
                self.plot_panel.highlight_timestamp(adj_ts)
                break


    # ---------------- Helpers ----------------
    def _plot_start(self):
        self._plot_paused = False
        if self.client:
            self.client.plot_enabled = True
        self.btn_plot_start.configure(state=tk.DISABLED)
        self.btn_plot_pause.configure(state=tk.NORMAL, text="Pause")
        self.btn_plot_stop.configure(state=tk.NORMAL)

    def _plot_pause(self):
        if not self._plot_paused:
            # Pause
            self._plot_paused = True
            if self.client:
                self.client.plot_enabled = False
            self.btn_plot_pause.configure(text="Resume")
        else:
            # Resume
            self._plot_paused = False
            if self.client:
                self.client.plot_enabled = True
            self.btn_plot_pause.configure(text="Pause")

    def _plot_stop(self):
        self._plot_paused = False
        if self.client:
            self.client.plot_enabled = False
        self.btn_plot_start.configure(state=tk.NORMAL)
        self.btn_plot_pause.configure(state=tk.DISABLED, text="Pause")
        self.btn_plot_stop.configure(state=tk.DISABLED)

    def clear_plot_all(self):
        self.plot_panel.clear()
        self.plot_pkt_text.delete("1.0", "end")
        self._pkt_tag_to_ts.clear()
        self._pkt_counter = 0

    def _txrx_line(self, s: str):
        self.txrx_text.insert("end", s + "\n")
        self.txrx_text.see("end")

    # ---------------- Register Access tab ----------------
    # ============================================================
    #  FSK Capture tab
    # ============================================================
    def _build_fsk_tab(self):
        self._fsk_receiver: Optional[FSKReceiver] = None
        self._fsk_r2_receiver: Optional[FSKR2Receiver] = None
        self._fsk_pkt_count = 0
        self._fsk_r2_pkt_count = 0

        self._fsk_tabs = ttk.Notebook(self.tab_fsk)
        self._fsk_tabs.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(8, 10))

        self._fsk_tab_r1 = ttk.Frame(self._fsk_tabs)
        self._fsk_tab_r2 = ttk.Frame(self._fsk_tabs)
        self._fsk_tabs.add(self._fsk_tab_r1, text="R1")
        self._fsk_tabs.add(self._fsk_tab_r2, text="R2")

        # Top bar: Start / Stop / status / logging options
        top = ttk.Frame(self._fsk_tab_r1)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 4))

        ttk.Button(top, text="Start Capture",
                   command=self._fsk_start).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Stop Capture",
                   command=self._fsk_stop).pack(side=tk.LEFT, padx=(0, 12))

        self._fsk_status_var = tk.StringVar(value="Stopped")
        ttk.Label(top, textvariable=self._fsk_status_var).pack(side=tk.LEFT, padx=(0, 20))

        # Logging options checkboxes
        self._fsk_enable_log = tk.BooleanVar(value=False)  # .log file disabled by default
        self._fsk_enable_csv = tk.BooleanVar(value=True)   # .csv file enabled by default

        ttk.Checkbutton(top, text="Log to .log file",
                        variable=self._fsk_enable_log).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(top, text="Log to .csv file",
                        variable=self._fsk_enable_csv).pack(side=tk.LEFT)

        # Monitor text area (read-only for now — will show decoded data later)
        mon_frame = ttk.LabelFrame(
            self._fsk_tab_r1,
            text="FSK R1 Monitor (check boxes above to enable file logging)"
        )
        mon_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(4, 10))

        self._fsk_text = tk.Text(mon_frame, state=tk.DISABLED, wrap=tk.NONE,
                                 bg=_C_WHITE, fg=_C_TEXT,
                                 font=("Consolas", 9))
        fsk_sb = ttk.Scrollbar(mon_frame, command=self._fsk_text.yview)
        self._fsk_text.configure(yscrollcommand=fsk_sb.set)
        self._fsk_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        fsk_sb.pack(side=tk.RIGHT, fill=tk.Y)

        self._build_fsk_r2_tab(self._fsk_tab_r2)

    def _fsk_on_packet(self, seq: int, rec_count: int, preview_records: list):
        """Called from FSKReceiver thread — schedule UI update."""
        self.ui_q.put(("fsk", (seq, rec_count, preview_records)))

    def _fsk_format_packet_summary(self, seq: int, rec_count: int, preview_records: list) -> str:
        lines = [f"[seq={seq}] {rec_count} records"]
        for i, r in enumerate(preview_records):
            if "period" in r:
                lines.append(
                    f"  #{i:3d}  period={r['period']:5d}  delta={r['delta']:+6d}  "
                    f"ma={r['ma']:5d}  tog={int(r['toggled'])} vt={int(r['valid_transition'])}  "
                    f"sp={int(r['sign_positive'])}  state={r['state']}"
                )
            else:
                lines.append(
                    f"  #{i:3d}  counter={r['counter']:10d}  pattern={r['pattern']}  "
                    f"source={r['source']}  flags={r['flags']}"
                )
        remaining = rec_count - len(preview_records)
        if remaining > 0:
            lines.append(f"  ... {remaining} more record(s)")
        return "\n".join(lines) + "\n"

    def _fsk_append_batch(self, text: str, packets_added: int):
        self._fsk_text.configure(state=tk.NORMAL)
        self._fsk_text.insert(tk.END, text)
        self._fsk_text.see(tk.END)
        self._fsk_text.configure(state=tk.DISABLED)
        self._fsk_pkt_count += packets_added
        self._fsk_status_var.set(f"Capturing - {self._fsk_pkt_count} packets")

    def _fsk_start(self):
        if self._fsk_receiver and self._fsk_receiver.running:
            return
        if self._fsk_r2_receiver and self._fsk_r2_receiver.running:
            self._fsk_r2_stop()
        self._fsk_pkt_count = 0

        # Get logging preferences from checkboxes
        enable_log = self._fsk_enable_log.get()
        enable_csv = self._fsk_enable_csv.get()

        # Create receiver with logging options
        self._fsk_receiver = FSKReceiver(
            on_packet=self._fsk_on_packet,
            queue_size=10000,
            enable_log_file=enable_log,
            enable_csv_file=enable_csv,
            socket_buffer_size=8 * 1024 * 1024,
            ui_record_limit=3
        )
        self._fsk_receiver.start()

        # Update status to show what's being logged
        log_status = []
        if enable_log:
            log_status.append(".log")
        if enable_csv:
            log_status.append(".csv")

        if log_status:
            self._fsk_status_var.set(f"Capturing -> {' + '.join(log_status)}")
        else:
            self._fsk_status_var.set("Capturing (no file logging)")

    def _fsk_stop(self):
        if self._fsk_receiver:
            self._fsk_receiver.stop()
            self._fsk_receiver = None
        self._fsk_status_var.set("Stopped")

    def _build_fsk_r2_tab(self, parent: ttk.Frame):
        top = ttk.Frame(parent)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 4))

        ttk.Button(top, text="Start Capture",
                   command=self._fsk_r2_start).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text="Stop Capture",
                   command=self._fsk_r2_stop).pack(side=tk.LEFT, padx=(0, 12))

        self._fsk_r2_status_var = tk.StringVar(value="Stopped")
        ttk.Label(top, textvariable=self._fsk_r2_status_var).pack(side=tk.LEFT, padx=(0, 20))

        self._fsk_r2_enable_log = tk.BooleanVar(value=False)
        self._fsk_r2_enable_csv = tk.BooleanVar(value=True)

        ttk.Checkbutton(top, text="Log to .log file",
                        variable=self._fsk_r2_enable_log).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(top, text="Log to .csv file",
                        variable=self._fsk_r2_enable_csv).pack(side=tk.LEFT)

        mon_frame = ttk.LabelFrame(
            parent,
            text="FSK R2 Monitor (record_size=2 uses R2 software decode; other sizes use R1 fallback)"
        )
        mon_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(4, 10))

        self._fsk_r2_text = tk.Text(mon_frame, state=tk.DISABLED, wrap=tk.NONE,
                                    bg=_C_WHITE, fg=_C_TEXT,
                                    font=("Consolas", 9))
        fsk_r2_sb = ttk.Scrollbar(mon_frame, command=self._fsk_r2_text.yview)
        self._fsk_r2_text.configure(yscrollcommand=fsk_r2_sb.set)
        self._fsk_r2_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        fsk_r2_sb.pack(side=tk.RIGHT, fill=tk.Y)

    def _fsk_r2_on_packet(self, seq: int, rec_count: int, preview_records: list):
        self.ui_q.put(("fsk_r2", (seq, rec_count, preview_records)))

    def _fsk_r2_on_status(self, message: str):
        self.ui_q.put(("fsk_r2_status", message))

    def _fsk_r2_format_packet_summary(self, seq: int, rec_count: int, preview_records: list) -> str:
        variant = "R2"
        rec_size = "?"
        if preview_records:
            variant_value = preview_records[0].get("decoder_variant")
            if variant_value == "r1":
                variant = "R1-fallback"
            elif variant_value == "r2":
                variant = "R2"
            rec_size = str(preview_records[0].get("record_size", "?"))

        lines = [f"[seq={seq}] {rec_count} records  mode={variant}  rec_size={rec_size}"]
        for i, r in enumerate(preview_records):
            if "message" in r:
                lines.append(f"  #{i:3d}  {r['message']}  state={r.get('state', '-')}")
                continue

            if r.get("decoder_variant") == "r1" or ("diff" not in r and "delta" in r):
                lines.append(
                    f"  #{i:3d}  period={r['period']:5d}  delta={r['delta']:+6d}  "
                    f"ma={r['ma']:5d}  tog={int(r['toggled'])}  vt={int(r['valid_transition'])}  "
                    f"sp={int(r['sign_positive'])}  state={r['state']}"
                )
                continue

            bit_text = "-" if r["bit"] == "" else str(r["bit"])
            symbol = r["symbol"] or "-"
            extras: list[str] = []
            if r["byte_hex"]:
                extras.append(f"byte={r['byte_hex']}")
            if r["frame_hex"]:
                extras.append(f"frame={r['frame_hex']}")
            if r["frame_kind"]:
                extras.append(f"kind={r['frame_kind']}")
            if r["frame_status"]:
                extras.append(f"status={r['frame_status']}")
            extra_text = ""
            if extras:
                extra_text = "  " + "  ".join(extras)

            lines.append(
                f"  #{i:3d}  period={r['period']:5d}  ma={r['ma']:5d}  "
                f"diff={r['diff']:+6d}  ad={r['abs_diff']:5d}  "
                f"vt={int(r['valid_transition'])} tog={int(r['toggled'])}  "
                f"sign={r['sign']:+2d}  mode={r['timing_mode']:<5}  "
                f"cnt={r['period_counter']:4d}  sym={symbol:<7} bit={bit_text}  "
                f"bc={r['bit_count']:3d}  state={r['state']}{extra_text}"
            )

        remaining = rec_count - len(preview_records)
        if remaining > 0:
            lines.append(f"  ... {remaining} more record(s)")
        return "\n".join(lines) + "\n"

    def _fsk_r2_append_batch(self, text: str, packets_added: int):
        self._fsk_r2_text.configure(state=tk.NORMAL)
        self._fsk_r2_text.insert(tk.END, text)
        self._fsk_r2_text.see(tk.END)
        self._fsk_r2_text.configure(state=tk.DISABLED)
        self._fsk_r2_pkt_count += packets_added
        self._fsk_r2_status_var.set(f"Capturing - {self._fsk_r2_pkt_count} packets")

    def _fsk_r2_start(self):
        if self._fsk_r2_receiver and self._fsk_r2_receiver.running:
            return
        if self._fsk_receiver and self._fsk_receiver.running:
            self._fsk_stop()
        self._fsk_r2_pkt_count = 0

        enable_log = self._fsk_r2_enable_log.get()
        enable_csv = self._fsk_r2_enable_csv.get()

        self._fsk_r2_receiver = FSKR2Receiver(
            on_packet=self._fsk_r2_on_packet,
            on_status=self._fsk_r2_on_status,
            queue_size=10000,
            enable_log_file=enable_log,
            enable_csv_file=enable_csv,
            socket_buffer_size=8 * 1024 * 1024,
            ui_record_limit=3
        )
        self._fsk_r2_receiver.start()

        log_status = []
        if enable_log:
            log_status.append(".log")
        if enable_csv:
            log_status.append(".csv")

        if log_status:
            self._fsk_r2_status_var.set(f"Capturing -> {' + '.join(log_status)}")
        else:
            self._fsk_r2_status_var.set("Capturing (no file logging)")

    def _fsk_r2_stop(self):
        if self._fsk_r2_receiver:
            self._fsk_r2_receiver.stop()
            self._fsk_r2_receiver = None
        self._fsk_r2_status_var.set("Stopped")

    # ============================================================
    #  FW Update tab  (ESP flash via STM UART bridge)
    # ============================================================
    def _build_fw_tab(self):
        self._fw_file_path = tk.StringVar(value="")
        self._fw_offset_var = tk.StringVar(value="0x10000")
        self._fw_fparams_var = tk.StringVar(value="0x0220")
        self._fw_bridge_port_var = tk.StringVar(value="5003")
        self._fw_baud_var = tk.StringVar(value=str(DEFAULT_FLASH_BAUD))
        self._fw_timeout_var = tk.StringVar(value="1.5")  # base timeout for quick FW replies
        self._fw_progress_var = tk.DoubleVar(value=0.0)
        self._fw_status_var = tk.StringVar(value="Idle")
        self._fw_step_mode = tk.BooleanVar(value=True)  # step-by-step verification
        self._fw_cancel_event: threading.Event | None = None
        self._fw_step_gate: threading.Event | None = None
        self._fw_flasher: object | None = None  # ESPFlasher ref for auto_mode toggle
        self._fw_thread: threading.Thread | None = None

        # ---- Top: file selection ----
        file_lf = ttk.LabelFrame(self.tab_fw, text="Firmware Binary")
        file_lf.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 4))

        file_row = ttk.Frame(file_lf)
        file_row.pack(fill=tk.X, padx=8, pady=8)

        ttk.Entry(file_row, textvariable=self._fw_file_path, width=60).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(file_row, text="Browse...", command=self._fw_browse).pack(side=tk.LEFT)

        # ---- Settings row ----
        cfg_lf = ttk.LabelFrame(self.tab_fw, text="Flash Settings  (matches: esputil -fp <params> flash <offset> firmware.bin)")
        cfg_lf.pack(side=tk.TOP, fill=tk.X, padx=10, pady=4)

        cfg_row = ttk.Frame(cfg_lf)
        cfg_row.pack(fill=tk.X, padx=8, pady=8)

        ttk.Label(cfg_row, text="Flash offset (-fp):").pack(side=tk.LEFT)
        ttk.Entry(cfg_row, textvariable=self._fw_offset_var, width=12).pack(
            side=tk.LEFT, padx=(4, 16))
        ttk.Label(cfg_row, text="(ESP32-S3 app firmware.bin -> 0x10000)").pack(
            side=tk.LEFT, padx=(0, 16))

        ttk.Label(cfg_row, text="Flash params (-fp):").pack(side=tk.LEFT)
        ttk.Entry(cfg_row, textvariable=self._fw_fparams_var, width=10).pack(
            side=tk.LEFT, padx=(4, 6))
        ttk.Label(cfg_row, text="(0xABC: A=mode B=size C=freq)").pack(side=tk.LEFT, padx=(0, 16))

        cfg_row2 = ttk.Frame(cfg_lf)
        cfg_row2.pack(fill=tk.X, padx=8, pady=(0, 8))

        ttk.Label(cfg_row2, text="Bridge port:").pack(side=tk.LEFT)
        ttk.Entry(cfg_row2, textvariable=self._fw_bridge_port_var, width=8).pack(
            side=tk.LEFT, padx=(4, 16))
        ttk.Label(cfg_row2, text="(IP from top bar)").pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(cfg_row2, text="ESP flash baud:").pack(side=tk.LEFT)
        ttk.Entry(cfg_row2, textvariable=self._fw_baud_var, width=10).pack(
            side=tk.LEFT, padx=(4, 16))

        ttk.Label(cfg_row2, text="Base timeout (s):").pack(side=tk.LEFT)
        ttk.Entry(cfg_row2, textvariable=self._fw_timeout_var, width=6).pack(
            side=tk.LEFT, padx=(4, 6))
        ttk.Label(cfg_row2, text="(quick replies default 1.5s; erase/write steps scale automatically)").pack(side=tk.LEFT)

        ttk.Checkbutton(cfg_row2, text="Step-by-step verification (pause after each protocol step)",
                         variable=self._fw_step_mode).pack(side=tk.LEFT, padx=(16, 0))

        # ---- Action buttons ----
        btn_row = ttk.Frame(self.tab_fw)
        btn_row.pack(side=tk.TOP, fill=tk.X, padx=10, pady=4)

        self._fw_btn_flash = ttk.Button(btn_row, text="Start Flash",
                                         command=self._fw_start)
        self._fw_btn_flash.pack(side=tk.LEFT, padx=(0, 8))

        self._fw_btn_cancel = ttk.Button(btn_row, text="Cancel",
                                          command=self._fw_cancel, state=tk.DISABLED)
        self._fw_btn_cancel.pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(btn_row, textvariable=self._fw_status_var).pack(side=tk.LEFT)

        # ---- Progress bar ----
        prog_frame = ttk.Frame(self.tab_fw)
        prog_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=4)
        self._fw_progressbar = ttk.Progressbar(
            prog_frame, variable=self._fw_progress_var,
            maximum=1.0, mode="determinate")
        self._fw_progressbar.pack(fill=tk.X)

        # ---- Log output ----
        log_lf = ttk.LabelFrame(self.tab_fw, text="Flash Log")
        log_lf.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(4, 8))

        self._fw_log_text = tk.Text(log_lf, height=16, wrap=tk.WORD,
                                     bg=_C_WHITE, fg=_C_TEXT, insertbackground=_C_TEXT,
                                     selectbackground=_C_BLUE, selectforeground=_C_WHITE,
                                     font=("Consolas", 9))
        fw_scroll = ttk.Scrollbar(log_lf, orient=tk.VERTICAL,
                                   command=self._fw_log_text.yview)
        self._fw_log_text.configure(yscrollcommand=fw_scroll.set)
        fw_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._fw_log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    def _fw_browse(self):
        path = filedialog.askopenfilename(
            title="Select ESP firmware binary",
            filetypes=[("Binary files", "*.bin"), ("All files", "*.*")])
        if path:
            self._fw_file_path.set(path)

    @staticmethod
    def _fw_host_ts_full() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    @staticmethod
    def _fw_fmt_ms(seconds: float) -> str:
        return f"{seconds * 1000:.0f} ms"

    def _fw_make_log_path(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return os.path.join(LOG_DIR, "fw_update", f"fw_update_{stamp}.log")

    def _fw_write_file(self, text: str):
        path = self._fw_log_file
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            append_text(self._fw_log_lock, path, text)
        except Exception:
            pass

    def _fw_write_session_header(self, fw_path: str, firmware: bytes, ip: str,
                                 bridge_port: int, flash_offset: int,
                                 flash_params: int, flash_baud: int,
                                 step_mode: bool,
                                 cmd_timeout: float):
        lines = [
            "=" * 96,
            "FW UPDATE SESSION",
            f"Start time      : {self._fw_host_ts_full()}",
            f"Trace file      : {self._fw_log_file}",
            f"Firmware file   : {fw_path}",
            f"Firmware size   : {len(firmware)} bytes",
            f"Bridge endpoint : {ip}:{bridge_port}",
            f"Flash offset    : 0x{flash_offset:X}",
            f"Flash params    : 0x{flash_params:04X}",
            f"Flash baud      : {flash_baud}",
            f"Base timeout    : {self._fw_fmt_ms(cmd_timeout)}",
            f"Step mode       : {'ON' if step_mode else 'OFF'}",
            "",
            format_fw_update_plan(len(firmware), flash_offset, flash_params, flash_baud),
            "=" * 96,
            "",
        ]
        self._fw_write_file("\n".join(lines))

    def _fw_trace_log(self, event: dict):
        ts = self._fw_host_ts_full()
        kind = str(event.get("kind", ""))
        cmd_id = int(event.get("cmd_id", 0))
        op = int(event.get("op", 0))
        op_name = str(event.get("op_name", ""))
        lines = [
            "-" * 96,
            f"[{ts}] {kind} CMD {cmd_id:04d} {op_name} (op=0x{op:02X})",
        ]

        if kind == "TX":
            checksum = int(event.get("checksum", 0))
            packet_hex = str(event.get("packet_hex", ""))
            wire_hex = str(event.get("wire_hex", ""))
            lines.extend([
                f"Timeout        : {self._fw_fmt_ms(float(event.get('timeout_s', 0.0)))}",
                f"Checksum       : 0x{checksum:08X}",
                f"Packet length  : {int(event.get('packet_len', 0))} bytes",
                f"Wire length    : {int(event.get('wire_len', 0))} bytes",
                "Packet raw     :",
                hex_dump_multiline(bytes.fromhex(packet_hex)) if packet_hex else "",
                "Wire raw       :",
                hex_dump_multiline(bytes.fromhex(wire_hex)) if wire_hex else "",
            ])
        elif kind == "RX":
            packet_hex = str(event.get("packet_hex", ""))
            wire_hex = str(event.get("wire_hex", ""))
            lines.extend([
                f"Elapsed        : {self._fw_fmt_ms(float(event.get('elapsed_s', 0.0)))}",
                f"Response OK    : {bool(event.get('response_ok', False))}",
                f"Value          : 0x{int(event.get('value', 0)):08X}",
                f"Packet length  : {int(event.get('packet_len', 0))} bytes",
                f"Wire length    : {int(event.get('wire_len', 0))} bytes",
                "Packet raw     :",
                hex_dump_multiline(bytes.fromhex(packet_hex)) if packet_hex else "",
                "Wire raw       :",
                hex_dump_multiline(bytes.fromhex(wire_hex)) if wire_hex else "",
            ])
        elif kind == "TIMEOUT":
            lines.extend([
                f"Elapsed        : {self._fw_fmt_ms(float(event.get('elapsed_s', 0.0)))}",
                f"Timeout limit  : {self._fw_fmt_ms(float(event.get('timeout_s', 0.0)))}",
                "Result         : No matching response received before timeout",
            ])

        lines.append("")
        self._fw_write_file("\n".join(lines))

    def _fw_log(self, msg: str):
        """Thread-safe log append (schedules on main thread)."""
        ts = self._fw_host_ts_full()
        file_lines = msg.splitlines() or [""]
        file_block = "\n".join(
            f"[{ts}] {line}" if idx == 0 else f"    {line}"
            for idx, line in enumerate(file_lines)
        ) + "\n"
        self._fw_write_file(file_block)

        def _append():
            self._fw_log_text.insert("end", msg + "\n")
            self._fw_log_text.see("end")
        self.after(0, _append)

    def _fw_set_progress(self, pct: float, status: str):
        """Thread-safe progress update."""
        def _update():
            self._fw_progress_var.set(pct)
            self._fw_status_var.set(status)
        self.after(0, _update)

    def _fw_start(self):
        # Validate inputs
        fw_path = self._fw_file_path.get().strip()
        if not fw_path:
            messagebox.showerror("FW Update", "Please select a firmware binary file.")
            return
        try:
            with open(fw_path, "rb") as f:
                firmware = f.read()
        except Exception as e:
            messagebox.showerror("FW Update", f"Cannot read file:\n{e}")
            return
        if len(firmware) < 8:
            messagebox.showerror("FW Update", "File too small to be a valid firmware image.")
            return

        try:
            flash_offset = int(self._fw_offset_var.get().strip(), 0)
        except ValueError:
            messagebox.showerror("FW Update", "Invalid flash offset (use hex like 0x10000).")
            return

        try:
            flash_params = int(self._fw_fparams_var.get().strip(), 0)
        except ValueError:
            messagebox.showerror("FW Update", "Invalid flash params (use hex like 0x0220).")
            return

        ip = self.ip_var.get().strip()
        try:
            bridge_port = int(self._fw_bridge_port_var.get().strip())
        except ValueError:
            messagebox.showerror("FW Update", "Invalid bridge port number.")
            return

        try:
            flash_baud = int(self._fw_baud_var.get().strip(), 0)
            if flash_baud <= 0:
                raise ValueError("Baud must be positive")
        except ValueError:
            messagebox.showerror("FW Update", "Invalid ESP flash baud (use 115200 or 921600).")
            return

        try:
            cmd_timeout = float(self._fw_timeout_var.get().strip())
            if cmd_timeout <= 0:
                raise ValueError("Timeout must be positive")
        except ValueError:
            messagebox.showerror("FW Update", "Invalid timeout (use positive number in seconds, e.g., 0.5).")
            return

        step_mode = self._fw_step_mode.get()

        # Confirm
        size_kb = len(firmware) / 1024
        msg = (f"Flash {size_kb:.1f} KB to 0x{flash_offset:X}\n"
               f"Flash params: 0x{flash_params:04X}\n"
               f"ESP flash baud: {flash_baud}\n"
               f"via {ip}:{bridge_port}\n"
               f"Step-by-step: {'ON' if step_mode else 'OFF'}\n\n"
               f"Proceed?")
        if not messagebox.askyesno("FW Update", msg):
            return

        # Disable controls, enable cancel
        self._fw_btn_flash.configure(state=tk.DISABLED)
        self._fw_btn_cancel.configure(state=tk.NORMAL)
        self._fw_log_text.delete("1.0", "end")
        self._fw_progress_var.set(0.0)
        self._fw_status_var.set("Connecting...")
        self._fw_log_file = self._fw_make_log_path()
        self._fw_write_session_header(
            fw_path, firmware, ip, bridge_port, flash_offset,
            flash_params, flash_baud, step_mode, cmd_timeout,
        )
        self._fw_cancel_event = threading.Event()
        self._fw_step_gate = threading.Event() if step_mode else None
        if self._fw_step_gate:
            self._fw_step_gate.set()  # start unblocked; flasher clears it at each step

        self._fw_log(f"FW trace file: {self._fw_log_file}")

        self._fw_thread = threading.Thread(
            target=self._fw_worker,
            args=(ip, bridge_port, firmware, flash_offset, flash_params,
                  flash_baud, step_mode, cmd_timeout),
            daemon=True)
        self._fw_thread.start()

        # Start polling for step-gate pauses (shows popup on main thread)
        if step_mode:
            self.after(200, self._fw_poll_step_gate)

    def _fw_cancel(self):
        if self._fw_cancel_event:
            self._fw_cancel_event.set()
        # Unblock step gate so worker thread can exit
        if self._fw_step_gate:
            self._fw_step_gate.set()
        self._fw_status_var.set("Cancelling...")

    def _fw_poll_step_gate(self):
        """
        Polls the step gate from the main thread.
        When the worker clears the gate (paused at a step), shows a popup
        with Next Step / Automate / Cancel.
        """
        gate = self._fw_step_gate
        if gate is None:
            return
        # Check if worker thread is still alive
        if self._fw_thread is None or not self._fw_thread.is_alive():
            return

        if not gate.is_set():
            # Worker is waiting — show verification popup
            self._fw_show_step_popup()
        else:
            # Not paused yet, keep polling
            self.after(100, self._fw_poll_step_gate)

    def _fw_show_step_popup(self):
        """Show a modal popup with Next Step / Automate / Cancel buttons."""
        gate = self._fw_step_gate
        if gate is None:
            return

        dlg = tk.Toplevel(self)
        dlg.title("FW Update — Step Verification")
        dlg.geometry("500x320")
        dlg.resizable(True, True)
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(dlg, text="Step completed - verify TX/RX on STM side before continuing",
                  font=("Segoe UI", 10, "bold")).pack(padx=12, pady=(12, 4))
        ttk.Label(
            dlg,
            text="Programming traffic on the FW tab does not expect a TCP echo from STM; continue after the expected bootloader reply/ack.",
            wraplength=460,
            justify=tk.LEFT,
        ).pack(padx=12, pady=(0, 4), anchor="w")

        # Show the last few log lines as context
        log_text = tk.Text(dlg, height=12, wrap=tk.WORD, font=("Consolas", 8),
                           state=tk.NORMAL)
        log_text.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
        # Copy last ~30 lines from the flash log
        all_log = self._fw_log_text.get("1.0", "end").strip()
        lines = all_log.split("\n")
        recent = "\n".join(lines[-30:]) if len(lines) > 30 else all_log
        log_text.insert("1.0", recent)
        log_text.see("end")
        log_text.configure(state=tk.DISABLED)

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill=tk.X, padx=12, pady=(4, 12))

        def _next_step():
            dlg.destroy()
            gate.set()  # unblock worker for one step
            self.after(100, self._fw_poll_step_gate)

        def _automate():
            dlg.destroy()
            # Tell the flasher to skip all future gates
            if self._fw_flasher and hasattr(self._fw_flasher, 'auto_mode'):
                self._fw_flasher.auto_mode = True
            gate.set()  # unblock worker

        def _cancel():
            dlg.destroy()
            self._fw_cancel()

        ttk.Button(btn_frame, text="Next Step",
                   command=_next_step).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="Automate (skip all popups)",
                   command=_automate).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_frame, text="Cancel Flash",
                   command=_cancel).pack(side=tk.RIGHT)

        dlg.protocol("WM_DELETE_WINDOW", _next_step)  # X button = next step

    def _fw_worker(self, ip: str, port: int, firmware: bytes,
                   flash_offset: int, flash_params: int, flash_baud: int,
                   step_mode: bool, cmd_timeout: float):
        """
        Background thread: connect to STM bridge port and run the full
        ESP flash sequence (matches: esputil -fp <params> flash <offset> fw.bin).

        The STM is treated as a transparent TCP<->UART bridge here.
        No TCP echo is expected for programming traffic; the only expected
        RX frames are ESP ROM bootloader replies/acks coming back via STM.

        Sequence:
          1. SYNC
          2. READ_REG  (chip detect)
          3. CHANGE_BAUD (optional; only when flash baud > 115200)
          4. SPI_ATTACH
          5. SPI_SET_PARAMS
          6. FLASH_BEGIN (erase)
          7. FLASH_DATA  (4 KB blocks, with F1/F2 patching)
          8. FLASH_END   (reboot)
        """
        sock = None
        try:
            self._fw_log(f"Connecting to STM bridge at {ip}:{port}...")
            sock = socket.create_connection((ip, port), timeout=10.0)
            sock.settimeout(2.0)
            self._fw_log("Connected to bridge.")
            self._fw_log(f"Using FW base timeout: {self._fw_fmt_ms(cmd_timeout)}")
            self._fw_log(format_fw_update_plan(len(firmware), flash_offset, flash_params, flash_baud))

            fl = ESPFlasher(
                sock=sock,
                log=self._fw_log,
                progress=self._fw_set_progress,
                step_gate=self._fw_step_gate,
                cmd_timeout=cmd_timeout,  # Pass user-configured timeout
                on_retry_prompt=self._fw_retry_prompt_dialog,
                trace=self._fw_trace_log,
            )
            if self._fw_cancel_event:
                fl._cancel = self._fw_cancel_event
            # Store reference so UI "Automate" button can flip auto_mode
            self._fw_flasher = fl

            if not fl.sync():
                raise RuntimeError("Could not sync with ESP bootloader. "
                                   "Ensure the ESP is in download mode.")
            fl.detect_chip()
            fl.change_baudrate(flash_baud)
            fl.spi_attach()
            fl.flash_file(firmware, flash_offset, flash_params, flash_baud)
            fl.reboot()

            chip_name = CHIP_NAMES.get(fl.chip_id, f"0x{fl.chip_id:08X}")
            self._fw_set_progress(1.0, f"Done! ({chip_name})")
            self._fw_log(f"Flash complete. Chip: {chip_name}")

        except RuntimeError as e:
            self._fw_log(f"ERROR: {e}")
            self._fw_set_progress(0.0, f"Failed: {e}")
        except (ConnectionError, OSError) as e:
            self._fw_log(f"CONNECTION ERROR: {e}")
            self._fw_set_progress(0.0, f"Connection error: {e}")
        except Exception as e:
            self._fw_log(f"UNEXPECTED ERROR: {type(e).__name__}: {e}")
            self._fw_set_progress(0.0, f"Error: {e}")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            if self._fw_log_file:
                self._fw_log(f"FW trace saved to {self._fw_log_file}")
            self._fw_flasher = None
            # Re-enable controls on main thread
            def _restore():
                self._fw_btn_flash.configure(state=tk.NORMAL)
                self._fw_btn_cancel.configure(state=tk.DISABLED)
            self.after(0, _restore)

    def _build_cps_fw_tab(self):
        self._cpsfw_boot_path = tk.StringVar(value=CPS_BOOT_HEX_DEFAULT)
        self._cpsfw_app_path = tk.StringVar(value=CPS_APP_HEX_DEFAULT)
        self._cpsfw_port_var = tk.StringVar(value=str(CPS_API_PORT_DEFAULT))
        self._cpsfw_chunk_var = tk.StringVar(value=str(CPS_DEFAULT_CHUNK_SIZE))
        self._cpsfw_timeout_var = tk.StringVar(value="3.0")
        self._cpsfw_step_mode = tk.BooleanVar(value=False)
        self._cpsfw_progress_var = tk.DoubleVar(value=0.0)
        self._cpsfw_boot_progress_var = tk.DoubleVar(value=0.0)
        self._cpsfw_app_progress_var = tk.DoubleVar(value=0.0)
        self._cpsfw_status_var = tk.StringVar(value="Idle")
        self._cpsfw_reply_var = tk.StringVar(value="No session")
        self._cpsfw_result_var = tk.StringVar(value="-")
        self._cpsfw_state_var = tk.StringVar(value="-")
        self._cpsfw_image_var = tk.StringVar(value="-")
        self._cpsfw_stage_var = tk.StringVar(value="-")
        self._cpsfw_ready_var = tk.StringVar(value="-")
        self._cpsfw_hal_var = tk.StringVar(value="-")
        self._cpsfw_cps_status_var = tk.StringVar(value="-")
        self._cpsfw_chip_var = tk.StringVar(value="-")
        self._cpsfw_boot_hex_var = tk.StringVar(value="0 / 0")
        self._cpsfw_boot_written_var = tk.StringVar(value="0")
        self._cpsfw_app_hex_var = tk.StringVar(value="0 / 0")
        self._cpsfw_programmed_var = tk.StringVar(value="0")
        self._cpsfw_cancel_event: threading.Event | None = None
        self._cpsfw_step_gate: threading.Event | None = None
        self._cpsfw_thread: threading.Thread | None = None
        self._cpsfw_updater: CpsFwUpdater | None = None
        self._cpsfw_using_shared_socket = False
        self._cpsfw_busy_mode = ""
        self._cpsfw_log_file = ""
        self._cpsfw_log_lock = threading.Lock()
        self._cpsfw_pending_step = CpsStepContext(label="No pending command", detail="")
        self._cpsfw_step_popup_open = False

        info = ttk.LabelFrame(self.tab_cps_fw, text="Session Notes")
        info.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 4))
        ttk.Label(
            info,
            text=(
                "This tab streams raw Intel HEX text to STM with the BEGIN / DATA / END_IMAGE / EXECUTE API. "
                "If the top-bar Ethernet session is already connected, the CPS FW updater temporarily takes "
                "exclusive control of that NetClient socket, pauses polling/manual traffic, and then returns "
                "the socket after the operation finishes. If Ethernet is not connected, the tab falls back to "
                "its own temporary TCP session."
            ),
            wraplength=980,
            justify=tk.LEFT,
        ).pack(fill=tk.X, padx=8, pady=8)

        files_lf = ttk.LabelFrame(self.tab_cps_fw, text="HEX Files")
        files_lf.pack(side=tk.TOP, fill=tk.X, padx=10, pady=4)
        files_lf.columnconfigure(1, weight=1)

        ttk.Label(files_lf, text="Bootloader HEX:").grid(row=0, column=0, padx=8, pady=8, sticky=tk.W)
        ttk.Entry(files_lf, textvariable=self._cpsfw_boot_path).grid(
            row=0, column=1, padx=6, pady=8, sticky=tk.EW
        )
        ttk.Button(files_lf, text="Browse...", command=self._cpsfw_browse_boot).grid(
            row=0, column=2, padx=8, pady=8
        )

        ttk.Label(files_lf, text="Application HEX:").grid(row=1, column=0, padx=8, pady=8, sticky=tk.W)
        ttk.Entry(files_lf, textvariable=self._cpsfw_app_path).grid(
            row=1, column=1, padx=6, pady=8, sticky=tk.EW
        )
        ttk.Button(files_lf, text="Browse...", command=self._cpsfw_browse_app).grid(
            row=1, column=2, padx=8, pady=8
        )

        cfg_lf = ttk.LabelFrame(self.tab_cps_fw, text="API Transport")
        cfg_lf.pack(side=tk.TOP, fill=tk.X, padx=10, pady=4)

        row = ttk.Frame(cfg_lf)
        row.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(row, text="Target IP:").pack(side=tk.LEFT)
        ttk.Label(row, textvariable=self.ip_var).pack(side=tk.LEFT, padx=(4, 16))

        ttk.Label(row, text="API Port:").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self._cpsfw_port_var, width=8).pack(side=tk.LEFT, padx=(4, 16))

        ttk.Label(row, text="Chunk size:").pack(side=tk.LEFT)
        ttk.Combobox(
            row,
            textvariable=self._cpsfw_chunk_var,
            values=[str(v) for v in CPS_CHUNK_SIZES],
            state="readonly",
            width=6,
        ).pack(side=tk.LEFT, padx=(4, 16))

        ttk.Label(row, text="Cmd timeout (s):").pack(side=tk.LEFT)
        ttk.Entry(row, textvariable=self._cpsfw_timeout_var, width=8).pack(side=tk.LEFT, padx=(4, 8))

        row2 = ttk.Frame(cfg_lf)
        row2.pack(fill=tk.X, padx=8, pady=(0, 8))
        self._cpsfw_step_chk = ttk.Checkbutton(
            row2,
            text="Ask before each CPS FW command/chunk",
            variable=self._cpsfw_step_mode,
        )
        self._cpsfw_step_chk.pack(side=tk.LEFT)
        ttk.Label(
            row2,
            text="Popup options: Send this chunk, Send all, or Cancel.",
        ).pack(side=tk.LEFT, padx=(12, 0))

        btn_row = ttk.Frame(self.tab_cps_fw)
        btn_row.pack(side=tk.TOP, fill=tk.X, padx=10, pady=4)

        self._cpsfw_btn_start = ttk.Button(btn_row, text="Start Update", command=self._cpsfw_start)
        self._cpsfw_btn_start.pack(side=tk.LEFT, padx=(0, 8))

        self._cpsfw_btn_status = ttk.Button(btn_row, text="Query Status", command=self._cpsfw_query_status)
        self._cpsfw_btn_status.pack(side=tk.LEFT, padx=(0, 8))

        self._cpsfw_btn_abort = ttk.Button(btn_row, text="Abort Remote", command=self._cpsfw_abort_remote)
        self._cpsfw_btn_abort.pack(side=tk.LEFT, padx=(0, 16))

        self._cpsfw_btn_cancel = ttk.Button(
            btn_row, text="Cancel Active Update", command=self._cpsfw_cancel, state=tk.DISABLED
        )
        self._cpsfw_btn_cancel.pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(btn_row, textvariable=self._cpsfw_status_var).pack(side=tk.LEFT)

        status_lf = ttk.LabelFrame(self.tab_cps_fw, text="Progress")
        status_lf.pack(side=tk.TOP, fill=tk.X, padx=10, pady=4)

        self._cpsfw_progressbar = ttk.Progressbar(
            status_lf, variable=self._cpsfw_progress_var, maximum=1.0, mode="determinate"
        )
        self._cpsfw_progressbar.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(status_lf, textvariable=self._cpsfw_reply_var, wraplength=980, justify=tk.LEFT).pack(
            fill=tk.X, padx=8, pady=(0, 8)
        )

        detail_lf = ttk.LabelFrame(self.tab_cps_fw, text="CPS Progress Panel")
        detail_lf.pack(side=tk.TOP, fill=tk.X, padx=10, pady=4)
        for col in range(4):
            detail_lf.columnconfigure(col * 2 + 1, weight=1)

        ttk.Label(detail_lf, text="Result:").grid(row=0, column=0, padx=8, pady=(8, 4), sticky=tk.W)
        ttk.Label(detail_lf, textvariable=self._cpsfw_result_var).grid(row=0, column=1, padx=4, pady=(8, 4), sticky=tk.W)
        ttk.Label(detail_lf, text="State:").grid(row=0, column=2, padx=8, pady=(8, 4), sticky=tk.W)
        ttk.Label(detail_lf, textvariable=self._cpsfw_state_var).grid(row=0, column=3, padx=4, pady=(8, 4), sticky=tk.W)
        ttk.Label(detail_lf, text="Stage:").grid(row=0, column=4, padx=8, pady=(8, 4), sticky=tk.W)
        ttk.Label(detail_lf, textvariable=self._cpsfw_stage_var).grid(row=0, column=5, padx=4, pady=(8, 4), sticky=tk.W)
        ttk.Label(detail_lf, text="Image:").grid(row=0, column=6, padx=8, pady=(8, 4), sticky=tk.W)
        ttk.Label(detail_lf, textvariable=self._cpsfw_image_var).grid(row=0, column=7, padx=4, pady=(8, 4), sticky=tk.W)

        ttk.Label(detail_lf, text="Ready mask:").grid(row=1, column=0, padx=8, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, textvariable=self._cpsfw_ready_var).grid(row=1, column=1, padx=4, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, text="HAL status:").grid(row=1, column=2, padx=8, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, textvariable=self._cpsfw_hal_var).grid(row=1, column=3, padx=4, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, text="CPS status:").grid(row=1, column=4, padx=8, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, textvariable=self._cpsfw_cps_status_var).grid(row=1, column=5, padx=4, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, text="Chip ID:").grid(row=1, column=6, padx=8, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, textvariable=self._cpsfw_chip_var).grid(row=1, column=7, padx=4, pady=4, sticky=tk.W)

        ttk.Label(detail_lf, text="Boot HEX RX:").grid(row=2, column=0, padx=8, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, textvariable=self._cpsfw_boot_hex_var).grid(row=2, column=1, padx=4, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, text="Boot bytes written:").grid(row=2, column=2, padx=8, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, textvariable=self._cpsfw_boot_written_var).grid(row=2, column=3, padx=4, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, text="App HEX RX:").grid(row=2, column=4, padx=8, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, textvariable=self._cpsfw_app_hex_var).grid(row=2, column=5, padx=4, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, text="Programmed bytes:").grid(row=2, column=6, padx=8, pady=4, sticky=tk.W)
        ttk.Label(detail_lf, textvariable=self._cpsfw_programmed_var).grid(row=2, column=7, padx=4, pady=4, sticky=tk.W)

        ttk.Label(detail_lf, text="Boot RX progress:").grid(row=3, column=0, padx=8, pady=(6, 4), sticky=tk.W)
        ttk.Progressbar(
            detail_lf,
            variable=self._cpsfw_boot_progress_var,
            maximum=1.0,
            mode="determinate",
        ).grid(row=3, column=1, columnspan=7, padx=4, pady=(6, 4), sticky=tk.EW)

        ttk.Label(detail_lf, text="App RX progress:").grid(row=4, column=0, padx=8, pady=(4, 8), sticky=tk.W)
        ttk.Progressbar(
            detail_lf,
            variable=self._cpsfw_app_progress_var,
            maximum=1.0,
            mode="determinate",
        ).grid(row=4, column=1, columnspan=7, padx=4, pady=(4, 8), sticky=tk.EW)

        log_lf = ttk.LabelFrame(self.tab_cps_fw, text="CPS FW Update Log")
        log_lf.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(4, 8))

        self._cpsfw_log_text = tk.Text(
            log_lf,
            height=18,
            wrap=tk.WORD,
            bg=_C_WHITE,
            fg=_C_TEXT,
            insertbackground=_C_TEXT,
            selectbackground=_C_BLUE,
            selectforeground=_C_WHITE,
            font=("Consolas", 9),
        )
        scroll = ttk.Scrollbar(log_lf, orient=tk.VERTICAL, command=self._cpsfw_log_text.yview)
        self._cpsfw_log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._cpsfw_log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._cpsfw_reset_status_panel()

    def _cpsfw_browse_boot(self):
        path = filedialog.askopenfilename(
            title="Select CPS bootloader HEX",
            filetypes=[("Intel HEX", "*.hex"), ("All files", "*.*")],
        )
        if path:
            self._cpsfw_boot_path.set(path)

    def _cpsfw_browse_app(self):
        path = filedialog.askopenfilename(
            title="Select CPS application HEX",
            filetypes=[("Intel HEX", "*.hex"), ("All files", "*.*")],
        )
        if path:
            self._cpsfw_app_path.set(path)

    @staticmethod
    def _cpsfw_host_ts_full() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    @staticmethod
    def _cpsfw_ratio(value: int, total: int) -> float:
        if total <= 0:
            return 0.0
        return max(0.0, min(1.0, value / total))

    @staticmethod
    def _cpsfw_ready_text(mask: int) -> str:
        flags = []
        if mask & 0x01:
            flags.append("BOOT")
        if mask & 0x02:
            flags.append("APP")
        suffix = ", ".join(flags) if flags else "none"
        return f"0x{mask:02X} ({suffix})"

    def _cpsfw_reset_status_panel(self):
        self._cpsfw_reply_var.set("No session")
        self._cpsfw_result_var.set("-")
        self._cpsfw_state_var.set("-")
        self._cpsfw_image_var.set("-")
        self._cpsfw_stage_var.set("-")
        self._cpsfw_ready_var.set("-")
        self._cpsfw_hal_var.set("-")
        self._cpsfw_cps_status_var.set("-")
        self._cpsfw_chip_var.set("-")
        self._cpsfw_boot_hex_var.set("0 / 0")
        self._cpsfw_boot_written_var.set("0")
        self._cpsfw_app_hex_var.set("0 / 0")
        self._cpsfw_programmed_var.set("0")
        self._cpsfw_boot_progress_var.set(0.0)
        self._cpsfw_app_progress_var.set(0.0)

    def _cpsfw_make_log_path(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return os.path.join(LOG_DIR, "cps_fw_update", f"cps_fw_update_{stamp}.log")

    def _cpsfw_write_file(self, text: str):
        if not self._cpsfw_log_file:
            return
        try:
            os.makedirs(os.path.dirname(self._cpsfw_log_file), exist_ok=True)
            append_text(self._cpsfw_log_lock, self._cpsfw_log_file, text)
        except Exception:
            pass

    def _cpsfw_log(self, msg: str):
        ts = self._cpsfw_host_ts_full()
        self._cpsfw_write_file(f"[{ts}] {msg}\n")

        def _append():
            self._cpsfw_log_text.insert("end", msg + "\n")
            self._cpsfw_log_text.see("end")

        self.after(0, _append)

    def _cpsfw_set_progress(self, pct: float, status: str):
        def _update():
            self._cpsfw_progress_var.set(pct)
            self._cpsfw_status_var.set(status)

        self.after(0, _update)

    def _cpsfw_set_pending_step(self, context: CpsStepContext):
        self._cpsfw_pending_step = context
        self._cpsfw_status_var.set(f"Waiting for approval: {context.label}")

    def _cpsfw_set_reply(self, reply: CpsUpdateReply):
        self._cpsfw_reply_var.set(reply.summary())
        self._cpsfw_result_var.set(cps_result_name(reply.op_result))
        self._cpsfw_state_var.set(cps_state_name(reply.session_state))
        self._cpsfw_image_var.set(cps_image_name(reply.active_image))
        self._cpsfw_stage_var.set(cps_stage_name(reply.last_stage))
        self._cpsfw_ready_var.set(self._cpsfw_ready_text(reply.ready_mask))
        self._cpsfw_hal_var.set(f"0x{reply.hal_status:02X}")
        self._cpsfw_cps_status_var.set(f"0x{reply.cps_status:08X}")
        self._cpsfw_chip_var.set(f"0x{reply.chip_id:04X}")
        self._cpsfw_boot_hex_var.set(
            f"{reply.boot_hex_received} / {reply.boot_hex_expected}"
        )
        self._cpsfw_boot_written_var.set(str(reply.boot_bytes_written))
        self._cpsfw_app_hex_var.set(
            f"{reply.app_hex_received} / {reply.app_hex_expected}"
        )
        self._cpsfw_programmed_var.set(str(reply.programmed_bytes))
        self._cpsfw_boot_progress_var.set(
            self._cpsfw_ratio(reply.boot_hex_received, reply.boot_hex_expected)
        )
        self._cpsfw_app_progress_var.set(
            self._cpsfw_ratio(reply.app_hex_received, reply.app_hex_expected)
        )

    def _cpsfw_set_busy(self, busy: bool, mode: str = ""):
        self._cpsfw_busy_mode = mode if busy else ""
        self._cpsfw_btn_start.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._cpsfw_btn_status.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._cpsfw_btn_abort.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self._cpsfw_btn_cancel.configure(
            state=tk.NORMAL if (busy and mode == "update") else tk.DISABLED
        )
        self._cpsfw_step_chk.configure(state=tk.DISABLED if busy else tk.NORMAL)
        if hasattr(self, "btn_disconnect"):
            if busy and self._cpsfw_using_shared_socket:
                self.btn_disconnect.configure(state=tk.DISABLED)
            elif isinstance(self.client, NetClient):
                self.btn_disconnect.configure(state=tk.NORMAL)

    def _cpsfw_connected_net_client(self) -> NetClient | None:
        if isinstance(self.client, NetClient):
            return self.client
        return None

    def _cpsfw_require_transport_ready(self) -> bool:
        if isinstance(self.client, USBClient):
            messagebox.showerror(
                "CPS FW upd",
                "CPS FW update uses the Ethernet API path. Disconnect USB mode first, or switch the top bar "
                "back to Ethernet.",
            )
            return False
        if self._cpsfw_thread is not None and self._cpsfw_thread.is_alive():
            messagebox.showinfo("CPS FW upd", "A CPS FW operation is already running.")
            return False
        return True

    def _cpsfw_common_target(self) -> tuple[str, int, float] | None:
        ip = self.ip_var.get().strip()
        if not ip:
            messagebox.showerror("CPS FW upd", "Please enter the target IP address in the top bar.")
            return None

        try:
            port = int(self._cpsfw_port_var.get().strip())
        except ValueError:
            messagebox.showerror("CPS FW upd", "Invalid API port number.")
            return None

        try:
            timeout = float(self._cpsfw_timeout_var.get().strip())
            if timeout <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("CPS FW upd", "Invalid command timeout. Use a positive value in seconds.")
            return None

        return ip, port, timeout

    def _cpsfw_start(self):
        if not self._cpsfw_require_transport_ready():
            return

        boot_path = self._cpsfw_boot_path.get().strip()
        app_path = self._cpsfw_app_path.get().strip()
        if not boot_path or not app_path:
            messagebox.showerror("CPS FW upd", "Please select both bootloader and application HEX files.")
            return

        try:
            with open(boot_path, "rb") as fh:
                boot_hex = fh.read()
            with open(app_path, "rb") as fh:
                app_hex = fh.read()
        except Exception as exc:
            messagebox.showerror("CPS FW upd", f"Cannot read HEX files:\n{exc}")
            return

        if not boot_hex.startswith(b":"):
            messagebox.showerror("CPS FW upd", "Bootloader file does not look like an Intel HEX file.")
            return
        if not app_hex.startswith(b":"):
            messagebox.showerror("CPS FW upd", "Application file does not look like an Intel HEX file.")
            return

        try:
            chunk_size = int(self._cpsfw_chunk_var.get().strip())
        except ValueError:
            messagebox.showerror("CPS FW upd", "Invalid chunk size.")
            return
        if chunk_size not in CPS_CHUNK_SIZES:
            messagebox.showerror("CPS FW upd", "Chunk size must be 256 or 512 bytes.")
            return

        target = self._cpsfw_common_target()
        if target is None:
            return
        ip, port, timeout = target
        step_mode = self._cpsfw_step_mode.get()
        shared_client = self._cpsfw_connected_net_client()
        transport_mode = (
            "shared top-bar Ethernet socket"
            if shared_client is not None
            else "temporary CPS-only socket"
        )

        msg = (
            f"Target IP      : {ip}\n"
            f"API port       : {port}\n"
            f"Transport      : {transport_mode}\n"
            f"Boot HEX size  : {len(boot_hex)} bytes\n"
            f"App HEX size   : {len(app_hex)} bytes\n"
            f"Chunk size     : {chunk_size} bytes\n"
            f"Timeout        : {timeout:.1f} s\n"
            f"Manual pacing  : {'ON' if step_mode else 'OFF'}\n\n"
            f"Proceed with CPS FW update?"
        )
        if not messagebox.askyesno("CPS FW upd", msg):
            return

        self._cpsfw_log_text.delete("1.0", "end")
        self._cpsfw_progress_var.set(0.0)
        self._cpsfw_status_var.set("Preparing update...")
        self._cpsfw_reset_status_panel()
        self._cpsfw_log_file = self._cpsfw_make_log_path()
        self._cpsfw_cancel_event = threading.Event()
        self._cpsfw_step_gate = threading.Event() if step_mode else None
        if self._cpsfw_step_gate is not None:
            self._cpsfw_step_gate.set()
        self._cpsfw_pending_step = CpsStepContext(label="No pending command", detail="")
        self._cpsfw_step_popup_open = False
        self._cpsfw_using_shared_socket = shared_client is not None
        self._cpsfw_set_busy(True, mode="update")

        self._cpsfw_log("=" * 72)
        self._cpsfw_log("CPS FW update requested")
        self._cpsfw_log(f"Boot HEX : {boot_path}")
        self._cpsfw_log(f"App HEX  : {app_path}")
        self._cpsfw_log(f"Target   : {ip}:{port}")
        self._cpsfw_log(f"Transport: {transport_mode}")
        self._cpsfw_log(f"Chunk    : {chunk_size}")
        self._cpsfw_log(f"Timeout  : {timeout:.1f} s")
        self._cpsfw_log(f"Step mode: {'ON' if step_mode else 'OFF'}")
        self._cpsfw_log(f"Trace    : {self._cpsfw_log_file}")
        self._cpsfw_log("=" * 72)

        self._cpsfw_thread = threading.Thread(
            target=self._cpsfw_worker,
            args=(shared_client, ip, port, boot_hex, app_hex, chunk_size, timeout),
            daemon=True,
        )
        self._cpsfw_thread.start()
        if step_mode:
            self.after(200, self._cpsfw_poll_step_gate)

    def _cpsfw_cancel(self):
        if self._cpsfw_cancel_event is not None:
            self._cpsfw_cancel_event.set()
            self._cpsfw_status_var.set("Cancelling CPS FW update...")
        if self._cpsfw_step_gate is not None:
            self._cpsfw_step_gate.set()

    def _cpsfw_done(self, ok: bool, message: str):
        mode = self._cpsfw_busy_mode
        self._cpsfw_thread = None
        self._cpsfw_cancel_event = None
        self._cpsfw_step_gate = None
        self._cpsfw_updater = None
        self._cpsfw_using_shared_socket = False
        self._cpsfw_pending_step = CpsStepContext(label="No pending command", detail="")
        self._cpsfw_step_popup_open = False
        self._cpsfw_set_busy(False)
        self._cpsfw_status_var.set(message)
        if ok and mode == "update":
            self._cpsfw_progress_var.set(1.0)

    def _cpsfw_poll_step_gate(self):
        gate = self._cpsfw_step_gate
        if gate is None:
            return
        if self._cpsfw_thread is None or not self._cpsfw_thread.is_alive():
            return
        if not gate.is_set():
            if not self._cpsfw_step_popup_open:
                self._cpsfw_show_step_popup()
            return
        self.after(100, self._cpsfw_poll_step_gate)

    def _cpsfw_show_step_popup(self):
        gate = self._cpsfw_step_gate
        if gate is None or self._cpsfw_step_popup_open:
            return

        self._cpsfw_step_popup_open = True
        ctx = self._cpsfw_pending_step

        dlg = tk.Toplevel(self)
        dlg.title("CPS FW upd - Step Control")
        dlg.geometry("560x360")
        dlg.resizable(True, True)
        dlg.transient(self)
        dlg.grab_set()

        ttk.Label(
            dlg,
            text="Approve the next CPS FW command/chunk",
            font=("Segoe UI", 10, "bold"),
        ).pack(padx=12, pady=(12, 4), anchor="w")
        ttk.Label(
            dlg,
            text=ctx.label,
            font=("Segoe UI", 9, "bold"),
        ).pack(padx=12, pady=(0, 4), anchor="w")
        ttk.Label(
            dlg,
            text=ctx.detail or "No detail available.",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(padx=12, pady=(0, 6), anchor="w")

        log_text = tk.Text(dlg, height=11, wrap=tk.WORD, font=("Consolas", 8))
        log_text.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
        all_log = self._cpsfw_log_text.get("1.0", "end").strip()
        lines = all_log.split("\n") if all_log else []
        recent = "\n".join(lines[-30:]) if len(lines) > 30 else all_log
        log_text.insert("1.0", recent)
        log_text.see("end")
        log_text.configure(state=tk.DISABLED)

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill=tk.X, padx=12, pady=(4, 12))

        def _close_popup():
            self._cpsfw_step_popup_open = False
            try:
                dlg.destroy()
            except Exception:
                pass

        def _send_this():
            _close_popup()
            gate.set()
            self.after(100, self._cpsfw_poll_step_gate)

        def _send_all():
            _close_popup()
            if self._cpsfw_updater is not None:
                self._cpsfw_updater.auto_mode = True
            gate.set()
            self._cpsfw_status_var.set("Manual pacing disabled for the rest of this session.")
            self.after(100, self._cpsfw_poll_step_gate)

        def _cancel_update():
            _close_popup()
            self._cpsfw_cancel()

        ttk.Button(btn_frame, text="Send this chunk", command=_send_this).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_frame, text="Send all", command=_send_all).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        ttk.Button(btn_frame, text="Cancel update", command=_cancel_update).pack(
            side=tk.RIGHT
        )

        dlg.protocol("WM_DELETE_WINDOW", _cancel_update)

    def _cpsfw_worker(self, shared_client: NetClient | None, ip: str, port: int, boot_hex: bytes,
                      app_hex: bytes, chunk_size: int, timeout: float):
        owner = "CPS FW update"
        shared_sock = None
        try:
            if shared_client is not None:
                self._cpsfw_log("Requesting exclusive access to the connected Ethernet socket...")
                shared_sock = shared_client.acquire_exclusive_socket(owner, timeout=10.0)
                self._cpsfw_log("Exclusive socket granted. Polling/manual traffic is paused for CPS FW update.")

            updater = CpsFwUpdater(
                ip=ip,
                port=port,
                bootloader_hex=boot_hex,
                app_hex=app_hex,
                chunk_size=chunk_size,
                on_log=self._cpsfw_log,
                on_progress=self._cpsfw_set_progress,
                on_done=lambda ok, msg: self.after(0, lambda: self._cpsfw_done(ok, msg)),
                cancel_event=self._cpsfw_cancel_event or threading.Event(),
                cmd_timeout=timeout,
                on_reply=lambda reply: self.after(0, lambda: self._cpsfw_set_reply(reply)),
                step_gate=self._cpsfw_step_gate,
                on_step_pause=lambda ctx: self.after(0, lambda ctx=ctx: self._cpsfw_set_pending_step(ctx)),
                sock=shared_sock,
            )
            self._cpsfw_updater = updater
            updater.run()
        finally:
            if shared_client is not None:
                try:
                    shared_client.release_exclusive_socket(owner)
                    self._cpsfw_log("Exclusive Ethernet socket returned to the main client.")
                except Exception as exc:
                    self._cpsfw_log(f"[WARN] Failed to release shared socket cleanly: {exc}")

    def _cpsfw_query_status(self):
        if not self._cpsfw_require_transport_ready():
            return

        target = self._cpsfw_common_target()
        if target is None:
            return
        ip, port, timeout = target
        shared_client = self._cpsfw_connected_net_client()

        self._cpsfw_set_busy(True, mode="status")
        self._cpsfw_status_var.set("Querying CPS FW status...")
        self._cpsfw_thread = threading.Thread(
            target=self._cpsfw_query_worker,
            args=(shared_client, ip, port, timeout),
            daemon=True,
        )
        self._cpsfw_thread.start()

    def _cpsfw_query_worker(self, shared_client: NetClient | None, ip: str, port: int, timeout: float):
        owner = "CPS FW status"
        try:
            if shared_client is not None:
                self._cpsfw_log("Borrowing the connected Ethernet socket for STATUS query...")
                shared_sock = shared_client.acquire_exclusive_socket(owner, timeout=5.0)
                try:
                    reply = query_status_on_socket(shared_sock, timeout, log=self._cpsfw_log)
                finally:
                    shared_client.release_exclusive_socket(owner)
            else:
                reply = cps_query_status(ip, port, timeout, log=self._cpsfw_log)
            self.after(0, lambda: self._cpsfw_set_reply(reply))
            self.after(0, lambda: self._cpsfw_done(True, "CPS FW status received."))
        except Exception as exc:
            msg = f"Status query failed: {exc}"
            self._cpsfw_log(f"[ERROR] {type(exc).__name__}: {exc}")
            self.after(0, lambda: self._cpsfw_done(False, msg))

    def _cpsfw_abort_remote(self):
        if not self._cpsfw_require_transport_ready():
            return

        target = self._cpsfw_common_target()
        if target is None:
            return
        ip, port, timeout = target
        shared_client = self._cpsfw_connected_net_client()

        if not messagebox.askyesno(
            "CPS FW upd",
            f"Send ABORT to {ip}:{port} and ask STM to cancel the active CPS FW session?",
        ):
            return

        self._cpsfw_set_busy(True, mode="abort")
        self._cpsfw_status_var.set("Sending remote abort...")
        self._cpsfw_thread = threading.Thread(
            target=self._cpsfw_abort_worker,
            args=(shared_client, ip, port, timeout),
            daemon=True,
        )
        self._cpsfw_thread.start()

    def _cpsfw_abort_worker(self, shared_client: NetClient | None, ip: str, port: int, timeout: float):
        owner = "CPS FW abort"
        try:
            if shared_client is not None:
                self._cpsfw_log("Borrowing the connected Ethernet socket for ABORT...")
                shared_sock = shared_client.acquire_exclusive_socket(owner, timeout=5.0)
                try:
                    reply = abort_session_on_socket(shared_sock, timeout, log=self._cpsfw_log)
                finally:
                    shared_client.release_exclusive_socket(owner)
            else:
                reply = cps_abort_session(ip, port, timeout, log=self._cpsfw_log)
            self.after(0, lambda: self._cpsfw_set_reply(reply))
            self.after(0, lambda: self._cpsfw_done(True, "Remote abort command sent."))
        except Exception as exc:
            msg = f"Abort failed: {exc}"
            self._cpsfw_log(f"[ERROR] {type(exc).__name__}: {exc}")
            self.after(0, lambda: self._cpsfw_done(False, msg))

    def _build_reg_tab(self):
        # --- Register selector ---
        sel = ttk.LabelFrame(self.tab_reg, text="Register")
        sel.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(8, 4))

        ttk.Label(sel, text="Name:").grid(row=0, column=0, padx=6, pady=6, sticky=tk.W)
        self._reg_name_var = tk.StringVar(value=_REG_NAMES[0])
        reg_cb = ttk.Combobox(sel, textvariable=self._reg_name_var, values=_REG_NAMES,
                              width=28, state="readonly")
        reg_cb.grid(row=0, column=1, padx=6, pady=6, sticky=tk.W)
        reg_cb.bind("<<ComboboxSelected>>", self._reg_on_select)

        ttk.Label(sel, text="Address:").grid(row=0, column=2, padx=(20, 4), pady=6)
        self._reg_addr_var = tk.StringVar(value="0x0000")
        ttk.Entry(sel, textvariable=self._reg_addr_var, width=8,
                  state="readonly").grid(row=0, column=3, padx=4, pady=6)

        ttk.Label(sel, text="Size (bytes):").grid(row=0, column=4, padx=(20, 4), pady=6)
        self._reg_size_var = tk.StringVar(value="2")
        ttk.Entry(sel, textvariable=self._reg_size_var, width=6,
                  state="readonly").grid(row=0, column=5, padx=4, pady=6)

        # --- Read / Write panels ---
        mid = ttk.Frame(self.tab_reg)
        mid.pack(side=tk.TOP, fill=tk.X, padx=10, pady=4)
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)

        # Read panel
        read_lf = ttk.LabelFrame(mid, text="Read  (Gen I2C Read:  07 0C 00 02 00 A1 00 01 00 R_BC Addr_LB Addr_MB)")
        read_lf.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 6))
        read_lf.columnconfigure(1, weight=1)

        ttk.Label(read_lf, text="R_BC:").grid(row=0, column=0, padx=8, pady=8, sticky=tk.W)
        self._rbc_var = tk.IntVar(value=2)
        rbc_sb = ttk.Spinbox(read_lf, from_=1, to=64, textvariable=self._rbc_var, width=6)
        rbc_sb.grid(row=0, column=1, padx=4, pady=8, sticky=tk.W)
        rbc_sb.bind("<FocusOut>", lambda e: self._reg_update_read_preview())
        rbc_sb.bind("<<Increment>>", lambda e: self.after(10, self._reg_update_read_preview))
        rbc_sb.bind("<<Decrement>>", lambda e: self.after(10, self._reg_update_read_preview))

        ttk.Button(read_lf, text="Read", width=8,
                   command=self._reg_read).grid(row=0, column=2, padx=10, pady=8)

        ttk.Label(read_lf, text="Frame:").grid(row=1, column=0, padx=8, pady=(0, 8), sticky=tk.W)
        self._read_frame_var = tk.StringVar()
        ttk.Entry(read_lf, textvariable=self._read_frame_var, state="readonly").grid(
            row=1, column=1, columnspan=2, padx=4, pady=(0, 8), sticky=tk.EW)

        # Write panel
        write_lf = ttk.LabelFrame(mid, text="Write  (Gen I2C Write: 01 XX XX 01 00 A1 00 01 00 W_BC Addr_LB Addr_MB Data...)")
        write_lf.grid(row=0, column=1, sticky=tk.NSEW, padx=(6, 0))
        write_lf.columnconfigure(1, weight=1)

        ttk.Label(write_lf, text="Data (hex):").grid(row=0, column=0, padx=8, pady=8, sticky=tk.W)
        self._wdata_var = tk.StringVar(value="00 00")
        wdata_entry = ttk.Entry(write_lf, textvariable=self._wdata_var, width=28)
        wdata_entry.grid(row=0, column=1, padx=4, pady=8, sticky=tk.EW)
        wdata_entry.bind("<KeyRelease>", lambda e: self._reg_update_write_preview())

        ttk.Label(write_lf, text="W_BC:").grid(row=0, column=2, padx=(10, 2), pady=8)
        self._wbc_label_var = tk.StringVar(value="2")
        ttk.Label(write_lf, textvariable=self._wbc_label_var, width=3).grid(row=0, column=3, pady=8)

        ttk.Button(write_lf, text="Write", width=8,
                   command=self._reg_write).grid(row=0, column=4, padx=10, pady=8)

        ttk.Label(write_lf, text="Frame:").grid(row=1, column=0, padx=8, pady=(0, 8), sticky=tk.W)
        self._write_frame_var = tk.StringVar()
        ttk.Entry(write_lf, textvariable=self._write_frame_var, state="readonly").grid(
            row=1, column=1, columnspan=4, padx=4, pady=(0, 8), sticky=tk.EW)

        # --- Log ---
        log_lf = ttk.LabelFrame(self.tab_reg, text="Log")
        log_lf.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=(4, 8))

        self._reg_log_text = tk.Text(log_lf, height=18, wrap="none",
                                     bg=_C_WHITE, fg=_C_TEXT, insertbackground=_C_TEXT,
                                     selectbackground=_C_BLUE, selectforeground=_C_WHITE,
                                     font=("Consolas", 9))
        vsb = ttk.Scrollbar(log_lf, orient=tk.VERTICAL, command=self._reg_log_text.yview)
        self._reg_log_text.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._reg_log_text.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=4, pady=(4, 0))
        ttk.Button(log_lf, text="Clear Log",
                   command=lambda: self._reg_log_text.delete("1.0", "end")).pack(
            side=tk.BOTTOM, anchor=tk.E, padx=6, pady=4)

        # Init previews
        self._reg_update_read_preview()
        self._reg_update_write_preview()

    def _reg_on_select(self, event=None):
        name = self._reg_name_var.get()
        if name in _REG_LOOKUP:
            addr, size = _REG_LOOKUP[name]
            self._reg_addr_var.set(f"0x{addr:04X}")
            self._reg_size_var.set(str(size))
            self._rbc_var.set(size)
            self._wdata_var.set(" ".join(["00"] * size))
            self._reg_update_read_preview()
            self._reg_update_write_preview()

    def _reg_addr(self) -> int | None:
        try:
            return int(self._reg_addr_var.get(), 16)
        except ValueError:
            return None

    def _reg_update_read_preview(self):
        addr = self._reg_addr()
        if addr is None:
            self._read_frame_var.set("(invalid address)")
            return
        try:
            r_bc = int(self._rbc_var.get())
        except (ValueError, tk.TclError):
            self._read_frame_var.set("(invalid R_BC)")
            return
        frame = self._build_i2c_read_frame(addr, r_bc)
        self._read_frame_var.set(" ".join(f"{b:02X}" for b in frame))

    def _reg_update_write_preview(self):
        addr = self._reg_addr()
        if addr is None:
            self._write_frame_var.set("(invalid address)")
            return
        raw = self._wdata_var.get().strip()
        try:
            data = bytes.fromhex(raw.replace(" ", ""))
        except ValueError:
            self._write_frame_var.set("(invalid hex data)")
            self._wbc_label_var.set("?")
            return
        self._wbc_label_var.set(str(len(data)))
        frame = self._build_i2c_write_frame(addr, data)
        self._write_frame_var.set(" ".join(f"{b:02X}" for b in frame))

    def _reg_read(self):
        addr = self._reg_addr()
        if addr is None:
            self._reg_log(f"{host_ts_short()}  ✖ Invalid address")
            return
        try:
            r_bc = int(self._rbc_var.get())
        except (ValueError, tk.TclError):
            self._reg_log(f"{host_ts_short()}  ✖ Invalid R_BC")
            return
        frame = self._build_i2c_read_frame(addr, r_bc)
        hex_str = " ".join(f"{b:02X}" for b in frame)
        name = self._reg_name_var.get()
        self._reg_log(f"{host_ts_short()}  TX  Read  {name} @ 0x{addr:04X}  R_BC={r_bc} : {hex_str}")
        self._send_hex(hex_str, note=f"Read {name}")

    def _reg_write(self):
        addr = self._reg_addr()
        if addr is None:
            self._reg_log(f"{host_ts_short()}  ✖ Invalid address")
            return
        raw = self._wdata_var.get().strip()
        try:
            data = bytes.fromhex(raw.replace(" ", ""))
        except ValueError:
            self._reg_log(f"{host_ts_short()}  ✖ Invalid hex data: {raw!r}")
            return
        if not data:
            self._reg_log(f"{host_ts_short()}  ✖ No data bytes to write")
            return
        frame = self._build_i2c_write_frame(addr, data)
        hex_str = " ".join(f"{b:02X}" for b in frame)
        name = self._reg_name_var.get()
        self._reg_log(f"{host_ts_short()}  TX  Write {name} @ 0x{addr:04X}  W_BC={len(data)} : {hex_str}")
        self._send_hex(hex_str, note=f"Write {name}")

    def _reg_log(self, s: str):
        self._reg_log_text.insert("end", s + "\n")
        self._reg_log_text.see("end")

    @staticmethod
    def _build_i2c_read_frame(reg_addr: int, r_bc: int) -> bytes:
        """Gen I2C Read: 07 0C 00 02 00 A1 00 01 00 R_BC Addr_LB Addr_MB  (12 bytes fixed)"""
        return bytes([
            0x07, 0x0C, 0x00,
            0x02, 0x00, 0xA1,
            0x00, 0x01, 0x00,
            r_bc & 0xFF,
            reg_addr & 0xFF,
            (reg_addr >> 8) & 0xFF,
        ])

    @staticmethod
    def _build_i2c_write_frame(reg_addr: int, data: bytes) -> bytes:
        """Gen I2C Write: 01 XX XX 01 00 A1 00 01 00 W_BC Addr_LB Addr_MB Data...
        Total length = 12 + W_BC; byte-count field (bytes[1:3] LE) = total."""
        w_bc = len(data)
        total = 12 + w_bc
        return bytes([
            0x01,
            total & 0xFF, (total >> 8) & 0xFF,
            0x01, 0x00, 0xA1,
            0x00, 0x01, 0x00,
            w_bc & 0xFF,
            reg_addr & 0xFF,
            (reg_addr >> 8) & 0xFF,
        ]) + data


if __name__ == "__main__":
    try:
        print("Starting CPS UI...", flush=True)
        App().mainloop()
    except Exception:
        err = traceback.format_exc()
        print(err)
        ensure_dir(LOGS_DIR)
        with (LOGS_DIR / "startup_error.log").open("w", encoding="utf-8") as f:
            f.write(err)
        raise
