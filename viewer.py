#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import shutil
import sys
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Self
import argparse
import subprocess
import tempfile
from dataclasses import dataclass

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore

    Tk = TkinterDnD.Tk
except ImportError:
    Tk = tk.Tk
    DND_FILES = "DND_Files"

import matplotlib
import numpy as np
from matplotlib.backends.backend_tkagg import (
    FigureCanvasTkAgg,
    NavigationToolbar2Tk,  # pyright: ignore[reportPrivateImportUsage]
)
from matplotlib.figure import Figure
import matplotlib.lines as lines
import matplotlib.patches as patches
import matplotlib.ticker as ticker
import infiray_irg

matplotlib.use("TkAgg")

# fmt: off
class ChainablePack:
    def pack(self, *args, **kwargs) -> Self:
        super().pack(*args, **kwargs)  # type: ignore
        return self
class Button(ChainablePack, tk.Button): ...
class Checkbutton(ChainablePack, tk.Checkbutton): ...
class Combobox(ChainablePack, ttk.Combobox): ...
class Entry(ChainablePack, tk.Entry): ...
class Frame(ChainablePack, tk.Frame): ...
class Label(ChainablePack, tk.Label): ...
class Radiobutton(ChainablePack, tk.Radiobutton): ...
class Scale(ChainablePack, tk.Scale): ...
# fmt: on

Point = tuple[float, float]
Rectangle = tuple[Point, Point]  # top left, bottom right
Line = tuple[Point, Point]  # start, end


@dataclass
class Measurement:
    tool: str
    coords: Rectangle | Line
    text_pos: Point


class ThermalViewer(Tk):  # type: ignore
    TITLE = "Thermal image viewer"

    def __init__(self):
        super().__init__(className="infiray-viewer")
        self.title(self.TITLE)
        self.geometry("1500x800")

        self.fine_data = None
        self.raw_fine_data = None
        self.vis_data = None
        self.coarse_data = None
        self.current_file = None
        self.directory_files = []

        self.ax_coarse = None
        self.ax_fine = None
        self.ax_vis = None

        # UI state
        self.show_coarse = tk.BooleanVar(value=False)
        self.show_thermal = tk.BooleanVar(value=True)
        self.show_visible = tk.BooleanVar(value=True)
        self.fusion_alpha_var = tk.DoubleVar(value=0.0)
        self.palettes = ["inferno", "plasma", "viridis", "magma", "gray", "jet"]
        self.cmap_var = tk.StringVar(value=self.palettes[0])
        self.tool_var = tk.StringVar(value="None")
        self.show_global_minmax = tk.BooleanVar(value=False)

        self.epsilon_var = tk.DoubleVar(value=0.95)
        self.trefl_var = tk.DoubleVar(value=20.0)

        # Drawing state
        self.rect_start = None
        self.current_patches = []
        self.current_text = None
        self.is_drawing = False
        self.measurement: Measurement | None = None

        self._MEASUREMENT_TOOLS = {
            "None": None,
            "Rectangle": self.measure_rectangle,
            "Line": self.measure_line,
        }

        self._setup_ui()

        self.bind("<Left>", lambda e: self.prev_file())
        self.bind("<Right>", lambda e: self.next_file())

    def _setup_ui(self):
        # Top bar
        toolbar_frame = Frame(self).pack(side=tk.TOP, fill=tk.X, padx=5, pady=2)

        # Second top bar
        toolbar_frame2 = Frame(self).pack(side=tk.TOP, fill=tk.X, padx=5, pady=2)

        # File operations
        Button(toolbar_frame, text="Open", command=self.open_file).pack(side=tk.LEFT, padx=2)
        self.btn_prev = Button(
            toolbar_frame, text="< Prev", command=self.prev_file, state=tk.DISABLED
        ).pack(side=tk.LEFT, padx=2)
        self.btn_next = Button(
            toolbar_frame, text="Next >", command=self.next_file, state=tk.DISABLED
        ).pack(side=tk.LEFT, padx=2)

        # View options
        Label(toolbar_frame, text=" | Views:").pack(side=tk.LEFT, padx=2)
        Checkbutton(
            toolbar_frame, text="Coarse", variable=self.show_coarse, command=self.redraw_plots
        ).pack(side=tk.LEFT)
        Checkbutton(
            toolbar_frame, text="Thermal", variable=self.show_thermal, command=self.redraw_plots
        ).pack(side=tk.LEFT)
        Checkbutton(
            toolbar_frame, text="Visible", variable=self.show_visible, command=self.redraw_plots
        ).pack(side=tk.LEFT)
        Label(toolbar_frame, text=" Fusion:").pack(side=tk.LEFT, padx=(4, 0))
        self.fusion_alpha_scale = Scale(
            toolbar_frame,
            variable=self.fusion_alpha_var,
            from_=0.0,
            to=1.0,
            resolution=0.01,
            orient=tk.HORIZONTAL,
            length=100,
            showvalue=True,
            command=lambda _: self.redraw_plots(),
        ).pack(side=tk.LEFT)
        Checkbutton(
            toolbar_frame,
            text="Global Min/Max",
            variable=self.show_global_minmax,
            command=self.redraw_plots,
        ).pack(side=tk.LEFT)

        # Palette selector
        Label(toolbar_frame, text=" | Palette:").pack(side=tk.LEFT, padx=2)
        Combobox(
            toolbar_frame,
            textvariable=self.cmap_var,
            values=self.palettes,
            state="readonly",
            width=8,
        ).pack(side=tk.LEFT, padx=2).bind("<<ComboboxSelected>>", lambda e: self.redraw_plots())

        # Measurement tools
        Label(toolbar_frame, text=" | Measure:").pack(side=tk.LEFT, padx=2)
        for t in self._MEASUREMENT_TOOLS.keys():
            r = Radiobutton(
                toolbar_frame,
                text=t,
                variable=self.tool_var,
                value=t,
            ).pack(side=tk.LEFT)
            if t == "None":
                r.configure(command=self.clear_measurement)

        # Emissivity correction
        Label(toolbar_frame2, text="Emissivity (ε):").pack(side=tk.LEFT, padx=2)
        Entry(toolbar_frame2, textvariable=self.epsilon_var, width=5).pack(side=tk.LEFT, padx=2)
        Label(toolbar_frame2, text="Reflected temp. (°C):").pack(side=tk.LEFT, padx=2)
        Entry(toolbar_frame2, textvariable=self.trefl_var, width=6).pack(side=tk.LEFT, padx=2)
        Button(toolbar_frame2, text="Apply", command=self.apply_epsilon).pack(side=tk.LEFT, padx=5)

        # Timestamp
        self.timestamp_label = Label(toolbar_frame2, text="").pack(side=tk.RIGHT)

        # Matplotlib figure
        self.fig = Figure(figsize=(10, 8), layout="constrained")
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)

        # Pack Matplotlib toolbar before the canvas so it isn't squeezed out
        self.mpl_toolbar = NavigationToolbar2Tk(self.canvas, self)
        self.mpl_toolbar.update()

        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=1)

        # Hover tooltip
        self.hover_tooltip = Label(
            self.canvas.get_tk_widget(),
            text="",
            bg="black",
            fg="white",
            font=("Arial", 10),
            relief=tk.SOLID,
            borderwidth=1,
        )

        self.canvas.mpl_connect("motion_notify_event", self.on_mouse_move)
        self.canvas.mpl_connect("button_press_event", self.on_mouse_press)
        self.canvas.mpl_connect("button_release_event", self.on_mouse_release)

        # DnD
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self.on_drop)
        except Exception:
            pass

    def on_drop(self, event):
        self.load_image(self.tk.splitlist(event.data)[0])

    def open_file(self):
        file_path = filedialog.askopenfilename(
            title="Select image file",
            filetypes=[("InfiRay IRG files", "*.irg"), ("All files", "*.*")],
        )
        if file_path:
            self.load_image(file_path)

    def update_directory_files(self, file_path):
        p = Path(file_path)
        if not p.exists():
            return

        self.current_file = p.resolve()
        dir_path = self.current_file.parent
        self.directory_files = sorted(dir_path.glob("*.irg"))
        self.update_nav_buttons()

    def update_nav_buttons(self):
        if not self.directory_files or self.current_file not in self.directory_files:
            self.btn_prev.config(state=tk.DISABLED)
            self.btn_next.config(state=tk.DISABLED)
            return

        idx = self.directory_files.index(self.current_file)
        self.btn_prev.config(state=tk.NORMAL if idx > 0 else tk.DISABLED)
        self.btn_next.config(
            state=tk.NORMAL if idx < len(self.directory_files) - 1 else tk.DISABLED
        )

    def prev_file(self):
        if self.current_file not in self.directory_files:
            return

        idx = self.directory_files.index(self.current_file)
        if idx > 0:
            self.load_image(str(self.directory_files[idx - 1]))

    def next_file(self):
        if self.current_file not in self.directory_files:
            return

        idx = self.directory_files.index(self.current_file)
        if idx < len(self.directory_files) - 1:
            self.load_image(str(self.directory_files[idx + 1]))

    def load_image(self, file_path):
        try:
            p = Path(file_path)
            data = p.read_bytes()
            coarse, fine, vis = infiray_irg.load(data)
            self.coarse_data = coarse
            self.raw_fine_data = fine.copy()
            self.vis_data = vis

            self.update_directory_files(file_path)
            self.title(f"{self.TITLE} - {p.name}")

            time = datetime.fromtimestamp(p.stat().st_mtime)
            self.timestamp_label.config(text=datetime.strftime(time, "%Y-%m-%d %H:%M:%S"))

            self.apply_epsilon()

        except Exception as e:
            messagebox.showerror("Error", f"Failed to load image:\n{e}")

    def apply_epsilon(self):
        if self.raw_fine_data is None:
            return

        try:
            eps = self.epsilon_var.get()
            trefl = self.trefl_var.get()

            eps = max(0.01, min(eps, 1.0))

            if eps == 1.0:
                self.fine_data = self.raw_fine_data.copy()
            else:
                meas_k = self.raw_fine_data + 273.15
                refl_k = trefl + 273.15

                val = (meas_k**4 - (1 - eps) * refl_k**4) / eps
                val = np.maximum(val, 0)
                self.fine_data = val**0.25 - 273.15

            self.clear_measurement()
            self.redraw_plots()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to apply emissivity:\n{e}")

    def redraw_plots(self):
        self.fig.clear()
        self.current_patches = []
        self.current_text = None

        self.fusion_alpha_scale.configure(state="normal" if self.show_thermal.get() else "disabled")

        views_to_show = []
        if self.show_coarse.get() and self.coarse_data is not None:
            views_to_show.append("coarse")
        if self.show_thermal.get() and self.fine_data is not None:
            views_to_show.append("thermal")
        if self.show_visible.get() and self.vis_data is not None:
            views_to_show.append("visible")

        if not views_to_show:
            self.canvas.draw()
            return

        for i, view in enumerate(views_to_show):
            ax = self.fig.add_subplot(1, len(views_to_show), i + 1)

            if view == "coarse" and self.coarse_data is not None:
                self.ax_coarse = ax
                ax.imshow(self.coarse_data, cmap="gray")
                ax.set_title("Coarse")
                ax.axis("off")

            elif view == "thermal" and self.fine_data is not None:
                self.ax_fine = ax
                min_temp = self.fine_data.min()
                max_temp = self.fine_data.max()
                fine_img = ax.imshow(
                    self.fine_data, cmap=self.cmap_var.get(), vmin=min_temp, vmax=max_temp
                )

                if (fusion_alpha := self.fusion_alpha_var.get()) > 0 and self.vis_data is not None:
                    ax.imshow(np.asarray(self.vis_data), alpha=fusion_alpha)
                    ax.set_title("Fusion")
                else:
                    ax.set_title("Temperature (°C)")

                ax.axis("off")
                cax = ax.inset_axes((0, -0.08, 1, 0.05))
                cbar = self.fig.colorbar(fine_img, cax=cax, orientation="horizontal")
                locator = ticker.MaxNLocator(nbins=9, min_n_ticks=8)
                cbar.locator = locator
                cbar.update_ticks()

                ticks = cbar.get_ticks()
                range_temp = max_temp - min_temp
                if range_temp > 0:
                    ticks = [
                        t
                        for t in ticks
                        if min_temp < t < max_temp
                        and abs(t - min_temp) > 0.05 * range_temp
                        and abs(t - max_temp) > 0.05 * range_temp
                    ]

                new_ticks = sorted(ticks + [min_temp, max_temp])
                cbar.set_ticks(new_ticks)
                cbar.ax.set_xticklabels([f"{t:.1f}" for t in new_ticks])
                cbar.ax.set_xlim(min_temp, max_temp)

                if self.show_global_minmax.get():
                    min_y, min_x = np.unravel_index(self.fine_data.argmin(), self.fine_data.shape)
                    max_y, max_x = np.unravel_index(self.fine_data.argmax(), self.fine_data.shape)
                    self.plot_marker(min_x, min_y, "blue")
                    self.plot_marker(max_x, max_y, "red")

                if self.measurement is not None:
                    tool = self.measurement.tool
                    self.draw_tool_shape(tool, *self.measurement.coords)
                    tool_fn = self._MEASUREMENT_TOOLS.get(tool)
                    if tool_fn and (stats_text := tool_fn(*self.measurement.coords)):
                        self.draw_measurement_text(stats_text, *self.measurement.text_pos)

            elif view == "visible" and self.vis_data is not None:
                self.ax_vis = ax
                ax.imshow(self.vis_data)
                ax.set_title("Visible")
                ax.axis("off")

        self.canvas.draw()

    def clear_measurement(self):
        self.measurement = None
        if self.ax_fine is None:
            return

        for p in self.current_patches:
            try:
                p.remove()
            except Exception:
                pass
        self.current_patches = []
        if self.current_text:
            try:
                self.current_text.remove()
            except Exception:
                pass
        self.current_text = None

        self.canvas.draw_idle()

    def on_mouse_press(self, event):
        if event.inaxes != self.ax_fine or self.fine_data is None:
            return

        if self.tool_var.get() == "None":
            return

        if event.button == 1:
            self.is_drawing = True
            self.clear_measurement()
            self.rect_start = (event.xdata, event.ydata)

    def on_mouse_move(self, event):
        if event.inaxes != self.ax_fine:
            self.hover_tooltip.place_forget()
            return

        if self.ax_fine is None or self.fine_data is None:
            return

        if event.xdata is None or event.ydata is None:
            return

        x, y = int(event.xdata + 0.5), int(event.ydata + 0.5)
        if 0 <= y < self.fine_data.shape[0] and 0 <= x < self.fine_data.shape[1]:
            temp = self.fine_data[y, x]

            self.hover_tooltip.config(text=f"{temp:.2f} °C")
            tk_x = event.x + 10
            tk_y = self.canvas.get_tk_widget().winfo_height() - event.y + 10
            self.hover_tooltip.place(x=tk_x, y=tk_y)
        else:
            self.hover_tooltip.place_forget()

        # Region measurement tools
        if not (
            self.is_drawing
            and (tool := self.tool_var.get()) != "None"
            and self.rect_start is not None
        ):
            return

        x0, y0 = self.rect_start

        for p in self.current_patches:
            try:
                p.remove()
            except Exception:
                pass
        self.current_patches = []

        self.draw_tool_shape(tool, (x0, y0), (event.xdata, event.ydata))
        self.canvas.draw_idle()

    def draw_tool_shape(self, tool: str, p0: Point, p1: Point):
        assert self.ax_fine is not None
        x0, y0 = p0
        x1, y1 = p1

        if tool == "Rectangle":
            p = patches.Rectangle(
                (x0, y0), x1 - x0, y1 - y0, fill=False, edgecolor="cyan", linewidth=2
            )
            self.ax_fine.add_patch(p)
            self.current_patches.append(p)
        elif tool == "Line":
            p = lines.Line2D([x0, x1], [y0, y1], color="cyan", linewidth=2)
            self.ax_fine.add_line(p)
            self.current_patches.append(p)

    def draw_measurement_text(self, stats_text: str, text_x: float, text_y: float):
        assert self.ax_fine is not None
        bbox_props = dict(boxstyle="round,pad=0.3", fc="black", ec="cyan", alpha=0.7)
        self.current_text = self.ax_fine.text(
            text_x,
            text_y,
            stats_text,
            color="white",
            fontsize=10,
            verticalalignment="bottom",
            bbox=bbox_props,
        )

    def plot_marker(self, x, y, color):
        assert self.ax_fine is not None and self.fine_data is not None
        ref = min(self.fine_data.shape)  # 240 on original camera display (240x320)
        sq = ref * 14 / 240  # square side length
        s = sq / 2
        L = ref * 5 / 240  # arm length beyond the square's edge
        lw = 5

        res = []
        square = patches.Rectangle(
            (x - s, y - s), sq, sq, fill=False, edgecolor=color, linewidth=lw
        )
        self.ax_fine.add_patch(square)
        res.append(square)
        for x0, y0, x1, y1 in (
            (x, y - s, x, y - s - L),  # top
            (x, y + s, x, y + s + L),  # bottom
            (x - s, y, x - s - L, y),  # left
            (x + s, y, x + s + L, y),  # right
        ):
            ln = lines.Line2D([x0, x1], [y0, y1], color=color, linewidth=lw)
            self.ax_fine.add_line(ln)
            res.append(ln)
        return res

    def measure_rectangle(self, start: Point, end: Point) -> str:
        assert self.fine_data is not None and self.ax_fine is not None

        xs = (start[0], end[0])
        ys = (start[1], end[1])
        x_min, x_max = int(min(xs)), int(max(xs))
        y_min, y_max = int(min(ys)), int(max(ys))

        x_min = max(0, min(x_min, self.fine_data.shape[1] - 1))
        x_max = max(0, min(x_max, self.fine_data.shape[1] - 1))
        y_min = max(0, min(y_min, self.fine_data.shape[0] - 1))
        y_max = max(0, min(y_max, self.fine_data.shape[0] - 1))

        roi = self.fine_data[y_min : y_max + 1, x_min : x_max + 1]
        if roi.size == 0:
            return ""

        min_y_roi, min_x_roi = np.unravel_index(roi.argmin(), roi.shape)
        max_y_roi, max_x_roi = np.unravel_index(roi.argmax(), roi.shape)

        min_x_abs, min_y_abs = x_min + min_x_roi, y_min + min_y_roi
        max_x_abs, max_y_abs = x_min + max_x_roi, y_min + max_y_roi

        self.current_patches.extend(self.plot_marker(min_x_abs, min_y_abs, "blue"))
        self.current_patches.extend(self.plot_marker(max_x_abs, max_y_abs, "red"))

        min_val, max_val, mean_val = roi.min(), roi.max(), roi.mean()
        return f"Min: {min_val:.2f}°C\nMax: {max_val:.2f}°C\nAvg: {mean_val:.2f}°C"

    def measure_line(self, start: Point, end: Point) -> str:
        assert self.fine_data is not None and self.ax_fine is not None

        x0, y0 = start
        x1, y1 = end
        length = int(np.hypot(x1 - x0, y1 - y0))
        if length == 0:
            return ""

        x_idx = np.linspace(x0, x1, length).astype(int)
        y_idx = np.linspace(y0, y1, length).astype(int)

        valid = (
            (x_idx >= 0)
            & (x_idx < self.fine_data.shape[1])
            & (y_idx >= 0)
            & (y_idx < self.fine_data.shape[0])
        )
        x_idx, y_idx = x_idx[valid], y_idx[valid]

        if len(x_idx) == 0:
            return ""

        vals = self.fine_data[y_idx, x_idx]
        min_idx, max_idx = vals.argmin(), vals.argmax()
        min_x_abs, min_y_abs = x_idx[min_idx], y_idx[min_idx]
        max_x_abs, max_y_abs = x_idx[max_idx], y_idx[max_idx]

        self.current_patches.extend(self.plot_marker(min_x_abs, min_y_abs, "blue"))
        self.current_patches.extend(self.plot_marker(max_x_abs, max_y_abs, "red"))
        min_val, max_val, mean_val = vals.min(), vals.max(), vals.mean()
        return f"Min: {min_val:.2f}°C\nMax: {max_val:.2f}°C\nAvg: {mean_val:.2f}°C"

    def on_mouse_release(self, event):
        if event.button != 1 or not self.is_drawing or self.rect_start is None:
            return
        self.is_drawing = False

        if self.ax_fine is None or self.fine_data is None:
            return

        if event.inaxes != self.ax_fine:
            return

        tool = self.tool_var.get()
        if tool == "None":
            return

        x1, y1 = event.xdata, event.ydata
        x0, y0 = self.rect_start
        coords = self.rect_start, (x1, y1)

        if abs(x1 - x0) < 1 and abs(y1 - y0) < 1:
            self.clear_measurement()
            return

        if stats_text := self._MEASUREMENT_TOOLS[tool](*coords):
            text_x = min(max(x1, 10), self.fine_data.shape[1] - 70)
            text_y = min(max(y1, 10), self.fine_data.shape[0] - 30)
            self.draw_measurement_text(stats_text, text_x, text_y)
            self.measurement = Measurement(
                tool=tool,
                coords=coords,
                text_pos=(text_x, text_y),
            )
            self.canvas.draw_idle()


def install_desktop():
    MIME_XML = """<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
	<mime-type type="image/x-infiray-irg">
		<comment>InfiRay Thermal Image</comment>
		<comment xml:lang="en">InfiRay Thermal Image</comment>
		<glob pattern="*.irg"/>
		<magic>
			<match value="0xcaac" type="big16" offset="0"/>
			<match value="0x0080" type="little16" offset="2"/>
			<match value="0xacca" type="big16" offset="126"/>
		</magic>
	</mime-type>
</mime-info>
"""

    DESKTOP_ENTRY = """[Desktop Entry]
Version=1.0
Type=Application
Name=InfiRay Thermal Viewer
GenericName=Thermal image viewer
Comment=View and analyse InfiRay thermal camera images
Exec=infiray-viewer %f
Icon=camera-photo
Terminal=false
MimeType=image/x-infiray-irg;
Categories=Graphics;2DGraphics;RasterGraphics;Viewer;Science;
Keywords=thermal;infrared;infiray;irg;temperature;image;viewer
StartupNotify=true
StartupWMClass=infiray-viewer
"""

    bad = False
    for x in ("update-desktop-database", "update-mime-database", "xdg-desktop-menu", "xdg-mime"):
        if shutil.which(x) is None:
            print(f"Error: {x} not found", file=sys.stderr)
            bad = True
    if bad:
        sys.exit(1)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        xml_path = tmp_path / "infiray-irg.xml"
        desktop_path = tmp_path / "infiray-viewer.desktop"

        xml_path.write_text(MIME_XML, encoding="utf-8")
        desktop_path.write_text(DESKTOP_ENTRY, encoding="utf-8")

        local_share_path = Path.home() / ".local" / "share"

        print("Installing MIME type for .irg files")
        subprocess.run(["xdg-mime", "install", "--novendor", xml_path], check=True)
        subprocess.run(["update-mime-database", local_share_path / "mime"], check=True)

        print("Installing .desktop file")
        subprocess.run(["xdg-desktop-menu", "install", "--novendor", desktop_path], check=True)
        subprocess.run(["update-desktop-database", local_share_path / "applications"], check=True)

        print("Done")


def main():
    parser = argparse.ArgumentParser(description=ThermalViewer.TITLE)
    parser.add_argument("file", nargs="?", help="Thermal image file (.irg) to load")
    parser.add_argument(
        "--install-desktop",
        action="store_true",
        help="Install for desktop integration and associate with .irg files",
    )
    args = parser.parse_args()

    if args.install_desktop:
        install_desktop()
        sys.exit(0)

    app = ThermalViewer()

    if args.file:
        file_to_load = Path(args.file)
        if file_to_load.exists():
            app.load_image(str(file_to_load))
        else:
            print(f"Error: File not found: {file_to_load}")
            sys.exit(1)

    app.mainloop()


if __name__ == "__main__":
    main()
