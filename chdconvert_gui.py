import os
import re
import subprocess
import shutil
import threading
import zipfile
import tarfile
from collections import defaultdict
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext

try:
    import py7zr
    _PY7ZR = True
except ImportError:
    _PY7ZR = False

if os.name == 'nt':
    CHDMAN = os.path.join(os.path.dirname(__file__), 'chdman.exe')
else:
    CHDMAN = 'chdman'

ERROR_LOG = "skipped_archives.txt"
ARCHIVE_EXTS = {"gz", "7z", "zip"}


def extract_archive(path, output_dir):
    ext = path.rsplit(".", 1)[-1].lower()
    if ext == "zip":
        with zipfile.ZipFile(path) as zf:
            zf.extractall(output_dir)
    elif ext == "gz":
        with tarfile.open(path) as tf:
            tf.extractall(output_dir)
    elif ext == "7z":
        if not _PY7ZR:
            raise RuntimeError("py7zr is required for .7z files. Run: pip install py7zr")
        with py7zr.SevenZipFile(path, mode="r") as zf:
            zf.extractall(output_dir)
    else:
        raise ValueError(f"Unsupported archive format: .{ext}")


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def run_chdman(cmd, on_progress, on_line):
    proc = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    buf = b""
    for chunk in iter(lambda: proc.stdout.read(128), b""):
        buf += chunk
        parts = re.split(b"[\r\n]", buf)
        buf = parts[-1]
        for part in parts[:-1]:
            line = part.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            m = re.search(r"Compressing,\s*([\d.]+)%", line)
            if m:
                on_progress(float(m.group(1)))
            else:
                on_line(line + "\n")
    proc.wait()
    return proc.returncode


def archives_in(directory):
    return sorted(f for f in os.listdir(directory) if f.split(".")[-1] in ARCHIVE_EXTS)


def find_loose_files(directory):
    """Walk directory tree and return (dirpath, filename) for iso/cue/orphan-bin files."""
    result = []
    for root, _, files in os.walk(directory):
        cue_basenames = {f.rsplit(".", 1)[0].lower() for f in files if f.lower().endswith(".cue")}
        for f in files:
            ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
            if ext in ("iso", "cue"):
                result.append((root, f))
            elif ext == "bin" and f.rsplit(".", 1)[0].lower() not in cue_basenames:
                result.append((root, f))
    return result


def run_conversion(input_dirs, delete_tmp, replace_originals, delete_archive, use_dirname,
                   output_dir_override, log, set_overall, set_file_progress, set_status):

    all_archives = [(d, f) for d in input_dirs for f in archives_in(d)]
    all_loose = [(d, root, f) for d in input_dirs for root, f in find_loose_files(d)]
    total = len(all_archives) + len(all_loose)

    if not total:
        log("No archives or loose ISO/CUE/BIN files found in the selected directories.\n")
        return

    set_overall(0, total)
    done = 0

    # --- Archives ---
    for input_dir, x in all_archives:
        set_status(f"Processing {done + 1} of {total}: {x}")
        set_file_progress(0)
        archive_path = os.path.join(input_dir, x)
        tmp_dir = input_dir + "_tmp"

        try:
            archive_size = os.path.getsize(archive_path)

            if not os.path.exists(tmp_dir):
                os.mkdir(tmp_dir)

            archive_name = x.rsplit(".", 1)[0]
            out = os.path.join(tmp_dir, archive_name)
            if not os.path.exists(out):
                os.mkdir(out)

            log(f"\n--- {x}  [{os.path.basename(input_dir)}] ---\n")
            log(f"Archive size : {fmt_size(archive_size)}\n")
            log("Extracting...\n")
            extract_archive(archive_path, out)

            extracted_files = []
            for root, _, files in os.walk(out):
                cue_basenames_here = {f.rsplit(".", 1)[0].lower() for f in files if f.lower().endswith(".cue")}
                for f in files:
                    ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
                    if ext in ("iso", "cue"):
                        extracted_files.append(os.path.join(root, f))
                    elif ext == "bin" and f.rsplit(".", 1)[0].lower() not in cue_basenames_here:
                        extracted_files.append(os.path.join(root, f))

            ret = 0
            for i_path in extracted_files:
                iso_size = os.path.getsize(i_path)
                log(f"ISO/CUE/BIN size : {fmt_size(iso_size)}\n")

                output_dir = output_dir_override if output_dir_override else input_dir
                if not os.path.exists(output_dir):
                    os.makedirs(output_dir)

                chd_path = os.path.join(output_dir, archive_name + ".chd")
                cmd = " ".join([
                    CHDMAN, "createcd", "-f",
                    "-i", f'"{i_path}"',
                    "-o", f'"{chd_path}"',
                ])
                log("Converting to CHD...\n")
                ret = run_chdman(cmd, on_progress=set_file_progress, on_line=log)

                if ret == 0:
                    chd_size = os.path.getsize(chd_path)
                    ratio = chd_size / iso_size * 100 if iso_size else 0
                    log(
                        f"CHD size     : {fmt_size(chd_size)}\n"
                        f"Ratio        : {ratio:.1f}% of source size"
                        f"  ({fmt_size(iso_size - chd_size)} saved)\n"
                    )
                else:
                    log(f"chdman failed (exit code {ret}).\n")

            if replace_originals or delete_archive:
                if os.path.exists(archive_path):
                    os.remove(archive_path)
                    log("Deleted original archive.\n")

            set_file_progress(100 if ret == 0 else 0)

            if delete_tmp or replace_originals:
                if os.path.exists(tmp_dir):
                    shutil.rmtree(tmp_dir)

        except Exception as e:
            log(f"Error: {e}\n")
            with open(ERROR_LOG, "a") as f:
                f.write(f"{x}: {str(e)}\n")

        done += 1
        set_overall(done, total)

    # --- Loose iso/cue/bin files (grouped by source directory) ---
    loose_by_dir = defaultdict(list)
    for input_dir, file_dir, f in all_loose:
        loose_by_dir[(input_dir, file_dir)].append(f)

    for (input_dir, file_dir), files in loose_by_dir.items():
        in_subdir = os.path.normpath(file_dir) != os.path.normpath(input_dir)
        output_dir = output_dir_override if output_dir_override else file_dir
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        all_succeeded = True
        for f in files:
            set_status(f"Processing {done + 1} of {total}: {f}")
            set_file_progress(0)
            file_path = os.path.join(file_dir, f)

            try:
                file_size = os.path.getsize(file_path)
                label = os.path.relpath(file_dir, input_dir)
                label = f if label == "." else f"{label}/{f}"
                log(f"\n--- {label}  [{os.path.basename(input_dir)}] ---\n")
                log(f"File size    : {fmt_size(file_size)}\n")

                chd_name = os.path.basename(file_dir) if use_dirname and in_subdir else f.rsplit(".", 1)[0]
                chd_path = os.path.join(output_dir, chd_name + ".chd")
                cmd = " ".join([
                    CHDMAN, "createcd", "-f",
                    "-i", f'"{file_path}"',
                    "-o", f'"{chd_path}"',
                ])
                log("Converting to CHD...\n")
                ret = run_chdman(cmd, on_progress=set_file_progress, on_line=log)

                if ret == 0:
                    chd_size = os.path.getsize(chd_path)
                    ratio = chd_size / file_size * 100 if file_size else 0
                    log(
                        f"CHD size     : {fmt_size(chd_size)}\n"
                        f"Ratio        : {ratio:.1f}% of source size"
                        f"  ({fmt_size(file_size - chd_size)} saved)\n"
                    )
                else:
                    log(f"chdman failed (exit code {ret}).\n")
                    all_succeeded = False

                set_file_progress(100 if ret == 0 else 0)

            except Exception as e:
                log(f"Error: {e}\n")
                with open(ERROR_LOG, "a") as ef:
                    ef.write(f"{f}: {str(e)}\n")
                all_succeeded = False

            done += 1
            set_overall(done, total)

        if (replace_originals or delete_archive) and all_succeeded and in_subdir:
            for f in files:
                src = os.path.join(file_dir, f)
                try:
                    if os.path.exists(src):
                        os.remove(src)
                        log(f"Deleted source file: {src}\n")
                except Exception as e:
                    log(f"Could not delete {src}: {e}\n")
            try:
                if not os.listdir(file_dir):
                    os.rmdir(file_dir)
                    log(f"Removed empty directory: {file_dir}\n")
            except Exception as e:
                log(f"Could not remove directory {file_dir}: {e}\n")

    set_status(f"Done — {total} item{'s' if total != 1 else ''} processed.")
    log("\nAll done.\n")


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
        self.chk_use_dirname.pack(anchor="w", padx=8, pady=(2, 6))

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
