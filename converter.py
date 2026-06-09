import os
import re
import subprocess
import shutil
import zipfile
import tarfile
from collections import defaultdict

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
            m = re.search(r"Compressing,\s*(.+?)%", line)
            if m:
                try:
                    on_progress(float(m.group(1)))
                except ValueError:
                    pass  # chdman outputs "nan%" for some disc images; skip silently
            else:
                on_line(line + "\n")
    proc.wait()
    return proc.returncode


def archives_in(directory):
    return sorted(f for f in os.listdir(directory) if f.split(".")[-1] in ARCHIVE_EXTS)


def detect_bin_mode(path):
    """Inspect the first sector of a .bin to determine its track mode."""
    with open(path, "rb") as f:
        header = f.read(16)
    sync = b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x00"
    if len(header) >= 16 and header[:12] == sync:
        mode_byte = header[15]
        if mode_byte == 1:
            return "MODE1/2352"
        if mode_byte == 2:
            return "MODE2/2352"
    return "MODE1/2048"


def generate_cue_for_bin(bin_path, cue_path):
    """Write a minimal single-track .cue for an orphan .bin. Returns the detected mode string."""
    mode = detect_bin_mode(bin_path)
    with open(cue_path, "w") as f:
        f.write(f'FILE "{os.path.basename(bin_path)}" BINARY\n')
        f.write(f"  TRACK 01 {mode}\n")
        f.write(f"    INDEX 01 00:00:00\n")
    return mode


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
                   output_dir_override, log, set_overall, set_file_progress, set_status,
                   skip_existing=False):

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
                if os.path.exists(chd_path):
                    if skip_existing:
                        log(f"Skipping {os.path.basename(chd_path)} — already exists.\n")
                        continue
                    os.remove(chd_path)

                input_path = i_path
                generated_cue = None
                if i_path.lower().endswith(".bin"):
                    generated_cue = i_path.rsplit(".", 1)[0] + ".cue"
                    mode = generate_cue_for_bin(i_path, generated_cue)
                    log(f"Generated .cue ({mode}) for orphan .bin\n")
                    input_path = generated_cue

                cmd = " ".join([
                    CHDMAN, "createcd", "-f",
                    "-i", f'"{os.path.normpath(input_path)}"',
                    "-o", f'"{os.path.normpath(chd_path)}"',
                ])
                log("Converting to CHD...\n")
                ret = run_chdman(cmd, on_progress=set_file_progress, on_line=log)

                if generated_cue and os.path.exists(generated_cue):
                    os.remove(generated_cue)

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
                if os.path.exists(chd_path):
                    if skip_existing:
                        log(f"Skipping {os.path.basename(chd_path)} — already exists.\n")
                        done += 1
                        set_overall(done, total)
                        continue
                    os.remove(chd_path)

                input_path = file_path
                generated_cue = None
                if f.lower().endswith(".bin"):
                    generated_cue = file_path.rsplit(".", 1)[0] + ".cue"
                    mode = generate_cue_for_bin(file_path, generated_cue)
                    log(f"Generated .cue ({mode}) for orphan .bin\n")
                    input_path = generated_cue

                cmd = " ".join([
                    CHDMAN, "createcd", "-f",
                    "-i", f'"{os.path.normpath(input_path)}"',
                    "-o", f'"{os.path.normpath(chd_path)}"',
                ])
                log("Converting to CHD...\n")
                ret = run_chdman(cmd, on_progress=set_file_progress, on_line=log)

                if generated_cue and os.path.exists(generated_cue):
                    os.remove(generated_cue)

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
