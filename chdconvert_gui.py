import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext

from converter import archives_in, find_loose_files, run_conversion


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CHD Convert")
        self.resizable(True, True)
        self.minsize(560, 560)

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
        ttk.Button(btn_col, text="Add…", width=8, command=self._add_dir).pack(pady=(0, 4))
        ttk.Button(btn_col, text="Remove", width=8, command=self._remove_dirs).pack()

        # --- Files to convert ---
        self.files_frame = ttk.LabelFrame(self, text="Files to Convert")
        self.files_frame.pack(fill="x", **pad)

        file_scroll = ttk.Scrollbar(self.files_frame, orient="vertical")
        self.file_listbox = tk.Listbox(
            self.files_frame,
            height=5,
            yscrollcommand=file_scroll.set,
            selectmode="browse",
            activestyle="none",
            font=("Consolas", 9),
        )
        file_scroll.config(command=self.file_listbox.yview)
        self.file_listbox.pack(side="left", fill="x", expand=True, padx=(8, 0), pady=6)
        file_scroll.pack(side="left", fill="y", padx=(0, 8), pady=6)

        # --- Output directory ---
        out_frame = ttk.LabelFrame(self, text="Output Directory")
        out_frame.pack(fill="x", **pad)

        out_row = ttk.Frame(out_frame)
        out_row.pack(fill="x", padx=8, pady=(6, 2))

        self.out_dir_var = tk.StringVar()
        self.out_dir_entry = ttk.Entry(out_row, textvariable=self.out_dir_var, font=("Consolas", 9))
        self.out_dir_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(out_row, text="Browse…", width=8, command=self._browse_out_dir).pack(side="left", padx=(6, 0))

        ttk.Label(
            out_frame,
            text="Leave blank to output CHDs alongside the source files.",
            foreground="gray",
        ).pack(anchor="w", padx=8, pady=(0, 6))

        # --- Options ---
        opt_frame = ttk.LabelFrame(self, text="Options")
        opt_frame.pack(fill="x", **pad)

        self.delete_var = tk.BooleanVar()
        self.replace_var = tk.BooleanVar()
        self.delete_archive_var = tk.BooleanVar()
        self.use_dirname_var = tk.BooleanVar()
        self.skip_existing_var = tk.BooleanVar()

        self.chk_delete = ttk.Checkbutton(
            opt_frame,
            text="Delete temp directory after conversion  (-d)",
            variable=self.delete_var,
        )
        self.chk_delete.pack(anchor="w", padx=8, pady=(6, 2))

        self.chk_replace = ttk.Checkbutton(
            opt_frame,
            text="Replace original archives (output to input dir, implies delete temp)  (-r)",
            variable=self.replace_var,
            command=self._on_replace_toggle,
        )
        self.chk_replace.pack(anchor="w", padx=8, pady=(2, 2))

        self.chk_delete_archive = ttk.Checkbutton(
            opt_frame,
            text="Delete original archive/ISO/CUE/BIN after successful conversion",
            variable=self.delete_archive_var,
        )
        self.chk_delete_archive.pack(anchor="w", padx=8, pady=(2, 2))

        self.chk_use_dirname = ttk.Checkbutton(
            opt_frame,
            text="Name output CHD after parent folder (e.g. 'Game Title/track.cue' → 'Game Title.chd')",
            variable=self.use_dirname_var,
        )
        self.chk_use_dirname.pack(anchor="w", padx=8, pady=(2, 2))

        self.chk_skip_existing = ttk.Checkbutton(
            opt_frame,
            text="Skip if output CHD already exists  (uncheck to overwrite)",
            variable=self.skip_existing_var,
        )
        self.chk_skip_existing.pack(anchor="w", padx=8, pady=(2, 6))

        # --- Progress ---
        prog_frame = ttk.LabelFrame(self, text="Progress")
        prog_frame.pack(fill="x", **pad)

        self.status_label = ttk.Label(prog_frame, text="Idle", anchor="w")
        self.status_label.pack(fill="x", padx=8, pady=(6, 2))

        ttk.Label(prog_frame, text="Overall:", anchor="w").pack(fill="x", padx=8)
        self.overall_bar = ttk.Progressbar(prog_frame, orient="horizontal", mode="determinate")
        self.overall_bar.pack(fill="x", padx=8, pady=(0, 4))

        ttk.Label(prog_frame, text="Current file:", anchor="w").pack(fill="x", padx=8)
        self.file_bar = ttk.Progressbar(
            prog_frame, orient="horizontal", mode="determinate", maximum=100
        )
        self.file_bar.pack(fill="x", padx=8, pady=(0, 8))

        # --- Run button ---
        self.run_btn = ttk.Button(self, text="Convert", command=self._start)
        self.run_btn.pack(**pad)

        # --- Log output ---
        log_frame = ttk.LabelFrame(self, text="Output")
        log_frame.pack(fill="both", expand=True, **pad)

        self.log_box = scrolledtext.ScrolledText(
            log_frame, state="disabled", wrap="word", height=10, font=("Consolas", 9)
        )
        self.log_box.pack(fill="both", expand=True, padx=4, pady=4)

    def _dirs(self):
        return list(self.dir_listbox.get(0, "end"))

    def _add_dir(self):
        path = filedialog.askdirectory(title="Select Input Directory")
        if path and path not in self._dirs():
            self.dir_listbox.insert("end", path)
            self._refresh_files()

    def _remove_dirs(self):
        for idx in reversed(self.dir_listbox.curselection()):
            self.dir_listbox.delete(idx)
        self._refresh_files()

    def _refresh_files(self):
        self.file_listbox.delete(0, "end")
        dirs = self._dirs()
        total = 0
        for d in dirs:
            if not os.path.isdir(d):
                continue
            label = os.path.basename(d) or d
            for f in archives_in(d):
                self.file_listbox.insert("end", f"[{label}]  {f}")
                total += 1
            for root, f in find_loose_files(d):
                rel = os.path.relpath(root, d)
                display = f if rel == "." else f"{rel}/{f}"
                self.file_listbox.insert("end", f"[{label}]  {display}")
                total += 1
        self.files_frame.config(
            text=f"Files to Convert — {total} file{'s' if total != 1 else ''} found"
            if total else "Files to Convert"
        )

    def _browse_out_dir(self):
        path = filedialog.askdirectory(title="Select Output Directory")
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

    def _log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_overall(self, value, maximum):
        self.overall_bar.config(maximum=maximum, value=value)

    def _set_file_progress(self, value):
        self.file_bar["value"] = value

    def _set_status(self, text):
        self.status_label.config(text=text)

    def _start(self):
        dirs = self._dirs()
        valid = [d for d in dirs if os.path.isdir(d)]
        if not valid:
            self._log("Please add at least one valid input directory.\n")
            return

        self.run_btn.configure(state="disabled")
        self.overall_bar["value"] = 0
        self.file_bar["value"] = 0
        self.status_label.config(text="Starting…")
        self._log(f"Starting conversion across {len(valid)} director{'ies' if len(valid) != 1 else 'y'}.\n")

        def worker():
            run_conversion(
                valid,
                delete_tmp=self.delete_var.get(),
                replace_originals=self.replace_var.get(),
                delete_archive=self.delete_archive_var.get(),
                use_dirname=self.use_dirname_var.get(),
                skip_existing=self.skip_existing_var.get(),
                output_dir_override=self.out_dir_var.get().strip() or None,
                log=lambda msg: self.after(0, self._log, msg),
                set_overall=lambda v, m: self.after(0, self._set_overall, v, m),
                set_file_progress=lambda v: self.after(0, self._set_file_progress, v),
                set_status=lambda t: self.after(0, self._set_status, t),
            )
            self.after(0, lambda: self.run_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    app = App()
    app.mainloop()
