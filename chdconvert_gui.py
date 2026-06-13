import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext

from converter import archives_in, find_loose_files, run_conversion, fmt_size, _PSUTIL

# Status label colours
_STATUS_COLOR = {
    "Pending":    "gray",
    "Converting": "#0055cc",
    "Done":       "#006600",
    "Error":      "#cc0000",
    "Skipped":    "gray",
    "Stopped":    "#cc7700",
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CHD Convert")
        self.resizable(True, True)
        self.minsize(600, 580)

        pad = {"padx": 10, "pady": 5}

        # --- Input directories ---
        dir_frame = ttk.LabelFrame(self, text="Input Directories")
        dir_frame.pack(fill="x", **pad)

        dir_list_frame = ttk.Frame(dir_frame)
        dir_list_frame.pack(fill="x", padx=8, pady=6)

        dir_scroll = ttk.Scrollbar(dir_list_frame, orient="vertical")
        self.dir_listbox = tk.Listbox(
            dir_list_frame,
            height=4,
            yscrollcommand=dir_scroll.set,
            selectmode="extended",
            activestyle="none",
            font=("Consolas", 9),
        )
        dir_scroll.config(command=self.dir_listbox.yview)
        self.dir_listbox.pack(side="left", fill="x", expand=True)
        dir_scroll.pack(side="left", fill="y")

        btn_col = ttk.Frame(dir_list_frame)
        btn_col.pack(side="left", padx=(6, 0))
        ttk.Button(btn_col, text="Add…",   width=8, command=self._add_dir).pack(pady=(0, 4))
        ttk.Button(btn_col, text="Remove", width=8, command=self._remove_dirs).pack()

        # --- Files to convert (grid) ---
        self.files_frame = ttk.LabelFrame(self, text="Files to Convert")
        self.files_frame.pack(fill="x", **pad)

        # Column header
        hdr = ttk.Frame(self.files_frame)
        hdr.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Label(hdr, text="File", font=("Consolas", 9, "bold"),
                  anchor="w").pack(side="left", fill="x", expand=True)
        ttk.Label(hdr, text="Status",   font=("Consolas", 9, "bold"),
                  width=10, anchor="center").pack(side="left", padx=(4, 0))
        ttk.Label(hdr, text="Progress", font=("Consolas", 9, "bold"),
                  width=20, anchor="center").pack(side="left", padx=(4, 8))
        ttk.Separator(self.files_frame, orient="horizontal").pack(fill="x", padx=8, pady=(2, 0))

        # Scrollable canvas that holds the grid rows
        grid_outer = ttk.Frame(self.files_frame)
        grid_outer.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        self._grid_canvas = tk.Canvas(grid_outer, height=140, highlightthickness=0)
        _grid_scroll = ttk.Scrollbar(grid_outer, orient="vertical",
                                     command=self._grid_canvas.yview)
        self._grid_canvas.configure(yscrollcommand=_grid_scroll.set)
        _grid_scroll.pack(side="right", fill="y")
        self._grid_canvas.pack(side="left", fill="both", expand=True)

        self._grid_inner = ttk.Frame(self._grid_canvas)
        self._grid_win = self._grid_canvas.create_window(
            (0, 0), window=self._grid_inner, anchor="nw")

        self._grid_inner.bind("<Configure>", lambda e: self._grid_canvas.configure(
            scrollregion=self._grid_canvas.bbox("all")))
        self._grid_canvas.bind("<Configure>", lambda e: self._grid_canvas.itemconfig(
            self._grid_win, width=e.width))
        self._grid_canvas.bind("<MouseWheel>",
                               lambda e: self._grid_canvas.yview_scroll(
                                   int(-1 * (e.delta / 120)), "units"))

        self._file_rows = {}  # full_path -> {frame, status_lbl, bar, pct_lbl}

        # --- Output directory ---
        out_frame = ttk.LabelFrame(self, text="Output Directory")
        out_frame.pack(fill="x", **pad)

        out_row = ttk.Frame(out_frame)
        out_row.pack(fill="x", padx=8, pady=(6, 2))

        self.out_dir_var = tk.StringVar()
        self.out_dir_entry = ttk.Entry(out_row, textvariable=self.out_dir_var,
                                       font=("Consolas", 9))
        self.out_dir_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Browse…", width=8,
                   command=self._browse_out_dir).pack(side="left", padx=(6, 0))

        ttk.Label(out_frame, text="Leave blank to output CHDs alongside the source files.",
                  foreground="gray").pack(anchor="w", padx=8, pady=(0, 6))

        # --- Options ---
        opt_frame = ttk.LabelFrame(self, text="Options")
        opt_frame.pack(fill="x", **pad)

        self.delete_var        = tk.BooleanVar()
        self.replace_var       = tk.BooleanVar()
        self.delete_archive_var = tk.BooleanVar()
        self.use_dirname_var   = tk.BooleanVar()
        self.skip_existing_var = tk.BooleanVar()

        self.chk_delete = ttk.Checkbutton(
            opt_frame, text="Delete temp directory after conversion  (-d)",
            variable=self.delete_var)
        self.chk_delete.pack(anchor="w", padx=8, pady=(6, 2))

        self.chk_replace = ttk.Checkbutton(
            opt_frame,
            text="Replace original archives (output to input dir, implies delete temp)  (-r)",
            variable=self.replace_var, command=self._on_replace_toggle)
        self.chk_replace.pack(anchor="w", padx=8, pady=(2, 2))

        self.chk_delete_archive = ttk.Checkbutton(
            opt_frame,
            text="Delete original archive/ISO/CUE/BIN after successful conversion",
            variable=self.delete_archive_var)
        self.chk_delete_archive.pack(anchor="w", padx=8, pady=(2, 2))

        self.chk_use_dirname = ttk.Checkbutton(
            opt_frame,
            text="Name output CHD after parent folder (e.g. 'Game Title/track.cue' → 'Game Title.chd')",
            variable=self.use_dirname_var)
        self.chk_use_dirname.pack(anchor="w", padx=8, pady=(2, 2))

        self.chk_skip_existing = ttk.Checkbutton(
            opt_frame, text="Skip if output CHD already exists  (uncheck to overwrite)",
            variable=self.skip_existing_var)
        self.chk_skip_existing.pack(anchor="w", padx=8, pady=(2, 4))

        # Concurrency / resource throttle
        throttle_row = ttk.Frame(opt_frame)
        throttle_row.pack(anchor="w", padx=8, pady=(2, 2))

        ttk.Label(throttle_row, text="Concurrent jobs (ISO/CUE/BIN only):").pack(side="left")
        self.workers_var = tk.IntVar(value=1)
        ttk.Spinbox(throttle_row, from_=1, to=16, width=4,
                    textvariable=self.workers_var).pack(side="left", padx=(4, 16))

        ttk.Label(throttle_row, text="CPU limit:").pack(side="left")
        self.cpu_threshold_var = tk.IntVar(value=0)
        ttk.Spinbox(throttle_row, from_=0, to=100, width=4,
                    textvariable=self.cpu_threshold_var).pack(side="left", padx=(4, 2))
        ttk.Label(throttle_row, text="%").pack(side="left", padx=(0, 16))

        ttk.Label(throttle_row, text="Disk write limit:").pack(side="left")
        self.disk_threshold_var = tk.IntVar(value=0)
        ttk.Spinbox(throttle_row, from_=0, to=10000, width=6,
                    textvariable=self.disk_threshold_var).pack(side="left", padx=(4, 2))
        ttk.Label(throttle_row, text="MB/s").pack(side="left")

        throttle_row2 = ttk.Frame(opt_frame)
        throttle_row2.pack(anchor="w", padx=8, pady=(2, 2))
        ttk.Label(throttle_row2, text="Stop if output drive exceeds:").pack(side="left")
        self.disk_full_var = tk.IntVar(value=90)
        ttk.Spinbox(throttle_row2, from_=0, to=99, width=4,
                    textvariable=self.disk_full_var).pack(side="left", padx=(4, 2))
        ttk.Label(throttle_row2, text="% full").pack(side="left")

        ttk.Label(opt_frame,
                  text="CPU/disk limits: 0 = no limit. Drive limit: 0 = disabled.",
                  foreground="gray").pack(anchor="w", padx=8, pady=(0, 6))

        # --- Progress ---
        prog_frame = ttk.LabelFrame(self, text="Progress")
        prog_frame.pack(fill="x", **pad)

        self.status_label = ttk.Label(prog_frame, text="Idle", anchor="w")
        self.status_label.pack(fill="x", padx=8, pady=(6, 2))

        ttk.Label(prog_frame, text="Overall:", anchor="w").pack(fill="x", padx=8)
        self.overall_bar = ttk.Progressbar(prog_frame, orient="horizontal", mode="determinate")
        self.overall_bar.pack(fill="x", padx=8, pady=(0, 4))

        if _PSUTIL:
            stats_row = ttk.Frame(prog_frame)
            stats_row.pack(fill="x", padx=8, pady=(0, 8))
            self.cpu_stat_label = ttk.Label(stats_row, text="CPU: —", anchor="w", width=14)
            self.cpu_stat_label.pack(side="left")
            self.disk_stat_label = ttk.Label(
                stats_row, text="Disk — Read: —   Write: —", anchor="w")
            self.disk_stat_label.pack(side="left", padx=(12, 0))
        else:
            ttk.Label(prog_frame,
                      text="Install psutil for CPU/disk monitoring and core affinity.",
                      foreground="gray").pack(anchor="w", padx=8, pady=(0, 8))

        # --- Run button ---
        self.run_btn = ttk.Button(self, text="Convert", command=self._start)
        self.run_btn.pack(**pad)

        # --- Log output ---
        log_frame = ttk.LabelFrame(self, text="Output")
        log_frame.pack(fill="both", expand=True, **pad)

        self.log_box = scrolledtext.ScrolledText(
            log_frame, state="disabled", wrap="word", height=10, font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=4, pady=4)

    # ------------------------------------------------------------------
    # Grid helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _short_name(display_name, max_len=55):
        if len(display_name) <= max_len:
            return display_name
        # Keep [folder] prefix + truncate the filename portion
        if display_name.startswith("["):
            bracket_end = display_name.find("]")
            if bracket_end != -1:
                prefix = display_name[:bracket_end + 1]
                rest = display_name[bracket_end + 1:]
                keep = max_len - len(prefix) - 1  # -1 for "…"
                if keep > 0:
                    return prefix + rest[:keep] + "…"
        return display_name[:max_len - 1] + "…"

    def _add_grid_row(self, key, display_name):
        row = ttk.Frame(self._grid_inner)
        row.pack(fill="x", padx=2, pady=1)

        short = self._short_name(display_name)
        name_lbl = ttk.Label(row, text=short, font=("Consolas", 9), anchor="w", width=55)
        name_lbl.pack(side="left", padx=(0, 4))

        status_lbl = ttk.Label(row, text="Pending", width=10, font=("Consolas", 9),
                               anchor="center", foreground=_STATUS_COLOR["Pending"])
        status_lbl.pack(side="left", padx=(4, 2))

        bar = ttk.Progressbar(row, length=120, maximum=100, mode="determinate")
        bar.pack(side="left", padx=(0, 2))

        pct_lbl = ttk.Label(row, text="  0%", width=5, font=("Consolas", 9), anchor="e")
        pct_lbl.pack(side="left", padx=(0, 4))

        self._file_rows[key] = {"frame": row, "status": status_lbl, "bar": bar, "pct": pct_lbl}

    def _reset_grid_rows(self):
        for row in self._file_rows.values():
            row["status"].config(text="Pending", foreground=_STATUS_COLOR["Pending"])
            row["bar"].config(value=0)
            row["pct"].config(text="  0%")

    def _scroll_to_row(self, path):
        row_info = self._file_rows.get(path)
        if not row_info:
            return
        self._grid_canvas.update_idletasks()
        row_y = row_info["frame"].winfo_y()
        row_h = row_info["frame"].winfo_height()
        canvas_h = self._grid_canvas.winfo_height()
        inner_h = self._grid_inner.winfo_height()
        if inner_h <= canvas_h:
            return
        target = row_y - (canvas_h - row_h) // 2
        frac = max(0.0, min(1.0, target / (inner_h - canvas_h)))
        self._grid_canvas.yview_moveto(frac)

    # ------------------------------------------------------------------
    # Job event callbacks (called on main thread via after())
    # ------------------------------------------------------------------

    def _on_job_start(self, path):
        row = self._file_rows.get(path)
        if row:
            row["status"].config(text="Converting",
                                 foreground=_STATUS_COLOR["Converting"])
            row["bar"].config(value=0)
            row["pct"].config(text="  0%")
            self._scroll_to_row(path)

    def _on_job_progress(self, path, pct):
        row = self._file_rows.get(path)
        if row:
            row["bar"].config(value=pct)
            row["pct"].config(text=f"{pct:3.0f}%")

    def _on_job_done(self, path, result):
        row = self._file_rows.get(path)
        if not row:
            return
        label_map = {
            "done":    "Done",
            "error":   "Error",
            "skipped": "Skipped",
            "stopped": "Stopped",
        }
        text = label_map.get(result, result.title())
        color = _STATUS_COLOR.get(text, "black")
        row["status"].config(text=text, foreground=color)
        if result == "done":
            row["bar"].config(value=100)
            row["pct"].config(text="100%")

    # ------------------------------------------------------------------
    # Directory / file list
    # ------------------------------------------------------------------

    def _dirs(self):
        return list(self.dir_listbox.get(0, "end"))

    def _add_dir(self):
        path = filedialog.askdirectory(title="Select Input Directory", parent=self)
        if path and path not in self._dirs():
            self.dir_listbox.insert("end", path)
            self._refresh_files()

    def _remove_dirs(self):
        for idx in reversed(self.dir_listbox.curselection()):
            self.dir_listbox.delete(idx)
        self._refresh_files()

    def _refresh_files(self):
        for child in self._grid_inner.winfo_children():
            child.destroy()
        self._file_rows.clear()

        dirs = self._dirs()
        total = 0
        for d in dirs:
            if not os.path.isdir(d):
                continue
            label = os.path.basename(d) or d
            for f in archives_in(d):
                key = os.path.join(d, f)
                self._add_grid_row(key, f"[{label}]  {f}")
                total += 1
            for root, f in find_loose_files(d):
                key = os.path.join(root, f)
                rel = os.path.relpath(root, d)
                display = f if rel == "." else f"{rel}/{f}"
                self._add_grid_row(key, f"[{label}]  {display}")
                total += 1

        self.files_frame.config(
            text=(f"Files to Convert — {total} file{'s' if total != 1 else ''} found"
                  if total else "Files to Convert"))

    def _browse_out_dir(self):
        path = filedialog.askdirectory(title="Select Output Directory", parent=self)
        if path:
            self.out_dir_var.set(path)

    def _on_replace_toggle(self):
        if self.replace_var.get():
            self.delete_var.set(True)
            self.chk_delete.state(["disabled"])
            self.chk_delete_archive.state(["disabled"])
            self.out_dir_entry.state(["disabled"])
        else:
            self.chk_delete.state(["!disabled"])
            self.chk_delete_archive.state(["!disabled"])
            self.out_dir_entry.state(["!disabled"])

    # ------------------------------------------------------------------
    # Logging / progress callbacks
    # ------------------------------------------------------------------

    def _log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_overall(self, value, maximum):
        self.overall_bar.config(maximum=maximum, value=value)

    def _set_status(self, text):
        self.status_label.config(text=text)

    def _update_resource_stats(self, cpu_pct, disk_read_bps, disk_write_bps):
        if not _PSUTIL:
            return
        if cpu_pct == 0.0 and disk_read_bps == 0.0 and disk_write_bps == 0.0:
            self.cpu_stat_label.config(text="CPU: —")
            self.disk_stat_label.config(text="Disk — Read: —   Write: —")
        else:
            self.cpu_stat_label.config(text=f"CPU: {cpu_pct:.0f}%")
            self.disk_stat_label.config(
                text=(f"Disk — Read: {fmt_size(disk_read_bps)}/s"
                      f"   Write: {fmt_size(disk_write_bps)}/s"))

    # ------------------------------------------------------------------
    # Start
    # ------------------------------------------------------------------

    def _start(self):
        dirs = self._dirs()
        valid = [d for d in dirs if os.path.isdir(d)]
        if not valid:
            self._log("Please add at least one valid input directory.\n")
            return

        self._reset_grid_rows()
        self.run_btn.configure(state="disabled")
        self.overall_bar["value"] = 0
        self.status_label.config(text="Starting…")
        self._log(
            f"Starting conversion across {len(valid)} "
            f"director{'ies' if len(valid) != 1 else 'y'}.\n")

        max_workers       = max(1, self.workers_var.get())
        cpu_threshold     = max(0, self.cpu_threshold_var.get())
        disk_threshold    = max(0, self.disk_threshold_var.get()) * 1024 * 1024
        disk_full_pct     = max(0, self.disk_full_var.get())

        def worker():
            run_conversion(
                valid,
                delete_tmp=self.delete_var.get(),
                replace_originals=self.replace_var.get(),
                delete_archive=self.delete_archive_var.get(),
                use_dirname=self.use_dirname_var.get(),
                skip_existing=self.skip_existing_var.get(),
                output_dir_override=self.out_dir_var.get().strip() or None,
                max_workers=max_workers,
                cpu_threshold=cpu_threshold,
                disk_threshold=disk_threshold,
                disk_full_pct=disk_full_pct,
                log=lambda msg: self.after(0, self._log, msg),
                set_overall=lambda v, m: self.after(0, self._set_overall, v, m),
                set_file_progress=lambda v: None,   # grid rows handle per-file progress
                set_status=lambda t: self.after(0, self._set_status, t),
                set_resource_stats=lambda c, r, w: self.after(
                    0, self._update_resource_stats, c, r, w),
                on_job_start=lambda p: self.after(0, self._on_job_start, p),
                on_job_progress=lambda p, pct: self.after(0, self._on_job_progress, p, pct),
                on_job_done=lambda p, s: self.after(0, self._on_job_done, p, s),
            )
            self.after(0, lambda: self.run_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    app = App()
    app.mainloop()
