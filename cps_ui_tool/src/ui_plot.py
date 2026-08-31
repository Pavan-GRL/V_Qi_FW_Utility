from __future__ import annotations
import tkinter as tk
from tkinter import ttk
import time
import traceback

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.ticker import MultipleLocator

from project_paths import LOGS_DIR, ensure_dir


class PlotPanel(ttk.Frame):
    def __init__(self, parent, max_points: int = 8000, window_ms: int = 10_000):
        super().__init__(parent)

        self.max_points = max_points
        self.window_ms = window_ms
        self.enabled_var = tk.BooleanVar(value=True)
        self.live_vrect_var = tk.StringVar(value="-- mV")
        self.live_irect_var = tk.StringVar(value="-- mA")
        self.raw_vrect_count_var = tk.StringVar(value="-- counts")
        self.raw_irect_count_var = tk.StringVar(value="-- counts")
        self._plot_follow = True    # auto-follow latest; False when user pans/zooms
        self._setting_xlim = False  # guard: True while we programmatically set xlim

        # ---- top bar ----
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=8)
        ttk.Checkbutton(top, text="Enable Plot", variable=self.enabled_var).pack(side=tk.LEFT)
        ttk.Button(top, text="Live", command=self._go_live).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(top, text="Pop Out", command=self._open_popout).pack(side=tk.LEFT, padx=(6, 0))

        # Latest rectified values, kept visible alongside the waveform.
        live_values = ttk.LabelFrame(self, text="Live Rectified Values")
        live_values.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 4))
        ttk.Label(live_values, text="VRECT:").pack(side=tk.LEFT, padx=(10, 4), pady=5)
        ttk.Label(
            live_values,
            textvariable=self.live_vrect_var,
            width=14,
            anchor="e",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 24), pady=5)
        ttk.Label(live_values, text="IRECT:").pack(side=tk.LEFT, padx=(0, 4), pady=5)
        ttk.Label(
            live_values,
            textvariable=self.live_irect_var,
            width=14,
            anchor="e",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(side=tk.LEFT, pady=5)

        # Calibration-only ADC counts. Hidden until enabled by the Plot-tab
        # checkbox so it does not consume space during normal streaming.
        self.raw_counts_frame = ttk.LabelFrame(
            self, text="Live Raw Rectified ADC Counts (Calibration mode)"
        )
        ttk.Label(self.raw_counts_frame, text="VRECT RAW:").pack(
            side=tk.LEFT, padx=(10, 4), pady=5
        )
        ttk.Label(
            self.raw_counts_frame,
            textvariable=self.raw_vrect_count_var,
            width=14,
            anchor="e",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 24), pady=5)
        ttk.Label(self.raw_counts_frame, text="IRECT RAW:").pack(
            side=tk.LEFT, padx=(0, 4), pady=5
        )
        ttk.Label(
            self.raw_counts_frame,
            textvariable=self.raw_irect_count_var,
            width=14,
            anchor="e",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(side=tk.LEFT, pady=5)

        # ---- main figure ----
        self.fig = Figure(figsize=(8.5, 4.5))
        self.ax = self.fig.add_subplot(111)
        self.ax2 = self.ax.twinx()          # right Y-axis for current
        _style_axes(self.ax, self.ax2)

        self.line_v, = self.ax.plot([], [], color="#D32F2F", linestyle="-",
                                    linewidth=1.2, label="Voltage (mV)")
        self.line_i, = self.ax2.plot([], [], color="#1565C0", linestyle="-",
                                     linewidth=1.2, label="Current (mA)")
        lines = [self.line_v, self.line_i]
        legend = self.ax.legend(lines, [l.get_label() for l in lines], loc="upper left",
                                facecolor="#FFFFFF", edgecolor="#003087",
                                labelcolor="#002060", framealpha=0.9)

        try:
            self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        except Exception:
            ensure_dir(LOGS_DIR)
            with (LOGS_DIR / "plot_error.log").open("w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            raise
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Navigation toolbar (pan / zoom / home / save)
        self.toolbar = NavigationToolbar2Tk(self.canvas, self, pack_toolbar=False)
        self.toolbar.update()
        self.toolbar.pack(side=tk.BOTTOM, fill=tk.X)

        # Detect user pan/zoom → disable live follow
        self.ax.callbacks.connect("xlim_changed", self._on_main_xlim_changed)

        # Mouse scroll → zoom in/out on X axis
        self.canvas.mpl_connect("scroll_event", self._on_scroll)

        # ---- data store (all history) ----
        self.t_ms: list[float] = []
        self.v_mv: list[float] = []
        self.i_ma: list[float] = []

        self._marker_line = None
        self._last_redraw_ms = 0

        # Timestamp unwrap state
        self._ts_offset = 0.0
        self._last_raw_ts: float | None = None
        self._last_adj_ts: float | None = None

        # Pop-out registry
        self._popouts: list[dict] = []

    # ------------------------------------------------------------------ #
    #  Live / pan / zoom control                                           #
    # ------------------------------------------------------------------ #
    def _on_main_xlim_changed(self, ax):
        if not self._setting_xlim:
            self._plot_follow = False   # user panned or zoomed

    def _on_scroll(self, event):
        """Scroll-wheel zoom centred on mouse cursor X position."""
        if not self.t_ms:
            return
        if event.inaxes not in (self.ax, self.ax2):
            return
        x_data = event.xdata
        if x_data is None:
            return

        x_min, x_max = self.ax.get_xlim()
        x_range = x_max - x_min
        if x_range <= 0:
            return

        zoom = 0.80 if event.button == "up" else 1.0 / 0.80
        frac = (x_data - x_min) / x_range
        new_range = x_range * zoom
        new_x_min = x_data - frac * new_range
        new_x_max = new_x_min + new_range

        self._plot_follow = False
        self._setting_xlim = True
        self.ax.set_xlim(new_x_min, new_x_max)
        self._setting_xlim = False
        _update_ylims(self.ax, self.ax2, self.t_ms, self.v_mv, self.i_ma,
                      new_x_min, new_x_max)
        self.canvas.draw_idle()

    def _go_live(self):
        """Re-enable auto-follow and jump to latest."""
        self._plot_follow = True
        self._redraw(force=True)

    # ------------------------------------------------------------------ #
    #  Pop-out window                                                      #
    # ------------------------------------------------------------------ #
    def _open_popout(self):
        win = tk.Toplevel(self)
        win.title("CPS Plot")
        win.geometry("960x540")

        fig = Figure(figsize=(9.6, 5.0))
        ax  = fig.add_subplot(111)
        ax2 = ax.twinx()
        _style_axes(ax, ax2)

        line_v, = ax.plot([], [], color="#D32F2F", linestyle="-",
                          linewidth=1.2, label="Voltage (mV)")
        line_i, = ax2.plot([], [], color="#1565C0", linestyle="-",
                           linewidth=1.2, label="Current (mA)")
        lines = [line_v, line_i]
        ax.legend(lines, [l.get_label() for l in lines], loc="upper left")

        po: dict = {
            "ax": ax, "ax2": ax2, "line_v": line_v, "line_i": line_i,
            "follow": True, "setting_xlim": False,
        }

        btn_bar = ttk.Frame(win)
        btn_bar.pack(side=tk.TOP, fill=tk.X, padx=6, pady=4)
        ttk.Button(btn_bar, text="Live", command=lambda: _po_go_live(po)).pack(side=tk.LEFT)

        live_values = ttk.LabelFrame(win, text="Live Rectified Values")
        live_values.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(0, 4))
        ttk.Label(live_values, text="VRECT:").pack(side=tk.LEFT, padx=(10, 4), pady=5)
        ttk.Label(
            live_values, textvariable=self.live_vrect_var, width=14,
            anchor="e", font=("TkDefaultFont", 11, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 24), pady=5)
        ttk.Label(live_values, text="IRECT:").pack(side=tk.LEFT, padx=(0, 4), pady=5)
        ttk.Label(
            live_values, textvariable=self.live_irect_var, width=14,
            anchor="e", font=("TkDefaultFont", 11, "bold"),
        ).pack(side=tk.LEFT, pady=5)

        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        toolbar = NavigationToolbar2Tk(canvas, win, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side=tk.BOTTOM, fill=tk.X)

        po["canvas"] = canvas

        def _on_xlim_changed(a):
            if not po["setting_xlim"]:
                po["follow"] = False

        ax.callbacks.connect("xlim_changed", _on_xlim_changed)

        def _on_po_scroll(event):
            if not self.t_ms:
                return
            if event.inaxes not in (ax, ax2):
                return
            x_data = event.xdata
            if x_data is None:
                return
            x_min, x_max = ax.get_xlim()
            x_range = x_max - x_min
            if x_range <= 0:
                return
            zoom = 0.80 if event.button == "up" else 1.0 / 0.80
            frac = (x_data - x_min) / x_range
            new_range = x_range * zoom
            new_x_min = x_data - frac * new_range
            new_x_max = new_x_min + new_range
            po["follow"] = False
            po["setting_xlim"] = True
            ax.set_xlim(new_x_min, new_x_max)
            po["setting_xlim"] = False
            _update_ylims(ax, ax2, self.t_ms, self.v_mv, self.i_ma,
                          new_x_min, new_x_max)
            canvas.draw_idle()

        canvas.mpl_connect("scroll_event", _on_po_scroll)

        def _redraw_po(force: bool = False):
            if not self.t_ms:
                canvas.draw_idle()
                return
            line_v.set_data(self.t_ms, self.v_mv)
            line_i.set_data(self.t_ms, self.i_ma)
            if po["follow"]:
                po["setting_xlim"] = True
                _apply_follow_xlim(ax, ax2, self.t_ms, self.v_mv, self.i_ma,
                                   self.window_ms)
                po["setting_xlim"] = False
            canvas.draw_idle()

        def _po_go_live(p):
            p["follow"] = True
            _redraw_po(force=True)

        po["redraw"] = _redraw_po
        _redraw_po(force=True)

        def on_close():
            self._popouts.remove(po)
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        self._popouts.append(po)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #
    def clear(self):
        self.t_ms.clear()
        self.v_mv.clear()
        self.i_ma.clear()
        self.live_vrect_var.set("-- mV")
        self.live_irect_var.set("-- mA")
        self.clear_raw_counts()
        self._clear_marker()
        self._last_redraw_ms = 0
        self._ts_offset = 0.0
        self._last_raw_ts = None
        self._last_adj_ts = None
        self._redraw(force=True)

    def show_raw_counts(self, visible: bool):
        """Show or hide the calibration ADC-count live-value panel."""
        if visible:
            if not self.raw_counts_frame.winfo_manager():
                self.raw_counts_frame.pack(
                    side=tk.TOP, fill=tk.X, padx=10, pady=(0, 4),
                    before=self.canvas.get_tk_widget(),
                )
        else:
            self.raw_counts_frame.pack_forget()
            self.clear_raw_counts()

    def update_raw_counts(self, vrect_count: int, irect_count: int):
        self.raw_vrect_count_var.set(f"{int(vrect_count):,} counts")
        self.raw_irect_count_var.set(f"{int(irect_count):,} counts")

    def clear_raw_counts(self):
        self.raw_vrect_count_var.set("-- counts")
        self.raw_irect_count_var.set("-- counts")

    def push_point(self, raw_ts_ms: int, rect_v_mv: int, rect_i_ma: int) -> float | None:
        self.live_vrect_var.set(f"{int(rect_v_mv):,} mV")
        self.live_irect_var.set(f"{int(rect_i_ma):,} mA")

        if not self.enabled_var.get():
            return None

        raw = float(raw_ts_ms)
        v   = float(rect_v_mv)
        i   = float(rect_i_ma)

        # Unwrap timestamps
        if self._last_raw_ts is None:
            adj = raw
            self._last_raw_ts = raw
            self._last_adj_ts = adj
        else:
            if raw < self._last_raw_ts - 5.0:
                assert self._last_adj_ts is not None
                self._ts_offset += (self._last_adj_ts + 1.0) - raw
            adj = raw + self._ts_offset
            assert self._last_adj_ts is not None
            if adj <= self._last_adj_ts:
                adj = self._last_adj_ts + 0.001
            self._last_raw_ts = raw
            self._last_adj_ts = adj

        self.t_ms.append(adj)
        self.v_mv.append(v)
        self.i_ma.append(i)

        if len(self.t_ms) > self.max_points:
            drop = len(self.t_ms) - self.max_points
            del self.t_ms[:drop]
            del self.v_mv[:drop]
            del self.i_ma[:drop]

        force = len(self.t_ms) <= 3
        self._redraw(force=force, throttle_ms=120)
        return adj

    def highlight_timestamp(self, adjusted_ts_ms: float):
        if not self.t_ms:
            return
        self._clear_marker()
        self._marker_line = self.ax.axvline(float(adjusted_ts_ms))
        self.canvas.draw_idle()

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #
    def _clear_marker(self):
        if self._marker_line is not None:
            try:
                self._marker_line.remove()
            except Exception:
                pass
            self._marker_line = None

    def _redraw(self, throttle_ms: int = 120, force: bool = False):
        now_ms = int(time.time() * 1000)
        if not force and (now_ms - self._last_redraw_ms) < throttle_ms:
            return
        self._last_redraw_ms = now_ms

        if not self.t_ms:
            self.canvas.draw_idle()
            return

        self.line_v.set_data(self.t_ms, self.v_mv)
        self.line_i.set_data(self.t_ms, self.i_ma)

        if self._plot_follow:
            self._setting_xlim = True
            _apply_follow_xlim(self.ax, self.ax2, self.t_ms, self.v_mv, self.i_ma,
                               self.window_ms)
            self._setting_xlim = False

        self.canvas.draw_idle()

        for po in self._popouts:
            po["redraw"]()


# ------------------------------------------------------------------ #
#  Module-level helpers                                                #
# ------------------------------------------------------------------ #
def _style_axes(ax, ax2):
    # Light theme colors
    bg_color = "#FFFFFF"
    text_color = "#002060"
    grid_color = "#D0DCF0"

    ax.set_facecolor(bg_color)
    ax.figure.patch.set_facecolor(bg_color)

    ax.set_title("Rectified Voltage & Current vs Packet Timestamp", color=text_color)
    ax.set_xlabel("Packet Timestamp (ms)", color=text_color)
    ax.set_ylabel("Voltage (mV)", color="#D32F2F")
    ax.tick_params(axis="y", labelcolor="#D32F2F", colors=text_color)
    ax.yaxis.set_major_locator(MultipleLocator(1000))
    ax.tick_params(axis="x", colors=text_color)
    ax2.set_ylabel("Current (mA)", color="#1565C0")
    ax2.tick_params(axis="y", labelcolor="#1565C0")
    ax2.yaxis.set_major_locator(MultipleLocator(500))

    # Update spine colors
    for spine in ax.spines.values():
        spine.set_color(grid_color)
    for spine in ax2.spines.values():
        spine.set_color(grid_color)

    ax.grid(True, color=grid_color, alpha=0.5)


def _set_ylim(ax, values: list) -> None:
    y_min, y_max = min(values), max(values)
    if y_min == y_max:
        y_min -= 1.0
        y_max += 1.0
    ypad = max(10.0, (y_max - y_min) * 0.05)
    ax.set_ylim(y_min - ypad, y_max + ypad)


def _update_ylims(ax, ax2, t_ms: list, v_mv: list, i_ma: list,
                  x_min: float, x_max: float) -> None:
    """Refit both Y-axes to data visible in [x_min, x_max]."""
    vs, is_ = [], []
    for k in range(len(t_ms)):
        if x_min <= t_ms[k] <= x_max:
            vs.append(v_mv[k])
            is_.append(i_ma[k])
    if vs:
        _set_ylim(ax, vs)
    if is_:
        _set_ylim(ax2, is_)


def _apply_follow_xlim(ax, ax2, t_ms: list, v_mv: list, i_ma: list,
                       window_ms: int) -> None:
    """Set xlim to latest window and auto-scale each Y-axis independently."""
    x_end   = t_ms[-1]
    x_start = max(t_ms[0], x_end - float(window_ms))

    idx0 = 0
    for k in range(len(t_ms) - 1, -1, -1):
        if t_ms[k] < x_start:
            idx0 = k + 1
            break
    vs  = v_mv[idx0:]
    is_ = i_ma[idx0:]

    if x_start == x_end:
        x_start -= 1.0
        x_end   += 1.0

    ax.set_xlim(x_start, x_end)

    if vs:
        _set_ylim(ax, vs)
    if is_:
        _set_ylim(ax2, is_)
