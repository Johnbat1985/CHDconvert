import os
import re
import subprocess
import shutil
import zipfile
import tarfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import py7zr
    _PY7ZR = True
except ImportError:
    _PY7ZR = False

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

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


def run_chdman(cmd, on_progress, on_line, core_list=None):
    proc = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    # Pin the chdman process to the assigned core slice when running concurrently
    if core_list is not None and _PSUTIL:
        try:
            psutil.Process(proc.pid).cpu_affinity(core_list)
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            pass
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


def _bins_referenced_by_cues(root, files):
    """Return the set of .bin filenames (lowercase) referenced inside any .cue in this dir."""
    referenced = set()
    for f in files:
        if not f.lower().endswith(".cue"):
            continue
        try:
            with open(os.path.join(root, f), "r", errors="replace") as fh:
                for line in fh:
                    m = re.match(r'\s*FILE\s+"?([^"]+)"?\s+BINARY', line, re.IGNORECASE)
                    if m:
                        referenced.add(m.group(1).lower())
        except OSError:
            pass
    return referenced


def find_loose_files(directory):
    """Walk directory tree and return (dirpath, filename) for iso/cue/orphan-bin files."""
    result = []
    for root, _, files in os.walk(directory):
        referenced_bins = _bins_referenced_by_cues(root, files)
        for f in files:
            ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
            if ext in ("iso", "cue"):
                result.append((root, f))
            elif ext == "bin" and f.lower() not in referenced_bins:
                result.append((root, f))
    return result


def run_conversion(input_dirs, delete_tmp, replace_originals, delete_archive, use_dirname,
                   output_dir_override, log, set_overall, set_file_progress, set_status,
                   skip_existing=False, max_workers=1, cpu_threshold=0, disk_threshold=0,
                   disk_full_pct=90, set_resource_stats=None,
                   on_job_start=None, on_job_progress=None, on_job_done=None):
    """
    cpu_threshold:  max CPU % before throttling new jobs (0 = no limit).
    disk_threshold: max disk write bytes/s before throttling new jobs (0 = no limit).
    disk_full_pct:  stop starting new jobs when output drive reaches this % full (0 = disabled).
    set_resource_stats: callback(cpu_pct, disk_read_bps, disk_write_bps) for live display.
    on_job_start(path):          called when a file begins converting.
    on_job_progress(path, pct):  called with chdman progress updates.
    on_job_done(path, result):   result is "done", "error", "skipped", or "stopped".
    """

    all_archives = [(d, f) for d in input_dirs for f in archives_in(d)]
    all_loose = [(d, root, f) for d in input_dirs for root, f in find_loose_files(d)]
    total = len(all_archives) + len(all_loose)

    if not total:
        log("No archives or loose ISO/CUE/BIN files found in the selected directories.\n")
        return

    set_overall(0, total)

    # --- Resource sampling ---
    cpu_pct = [0.0]
    disk_read_bps = [0.0]
    disk_write_bps = [0.0]
    stop_monitor = threading.Event()
    disk_stop = threading.Event()

    def _check_disk_full(path):
        """Set disk_stop and return True if the drive containing path is over disk_full_pct."""
        if disk_full_pct <= 0 or disk_stop.is_set():
            return disk_stop.is_set()
        try:
            usage = psutil.disk_usage(path)
            if usage.percent >= disk_full_pct:
                disk_stop.set()
                log(f"\nOutput drive is {usage.percent:.0f}% full "
                    f"(threshold: {disk_full_pct}%) — stopping new conversions.\n")
                set_status(f"Stopped: output drive {usage.percent:.0f}% full.")
                return True
        except Exception:
            pass
        return False

    def _monitor_resources():
        if not _PSUTIL:
            return
        psutil.cpu_percent()  # prime — first call always returns 0.0
        prev_disk = psutil.disk_io_counters()
        prev_t = time.time()
        while not stop_monitor.wait(1.0):
            cpu_pct[0] = psutil.cpu_percent()
            curr_disk = psutil.disk_io_counters()
            curr_t = time.time()
            elapsed = max(curr_t - prev_t, 0.001)
            disk_read_bps[0] = (curr_disk.read_bytes - prev_disk.read_bytes) / elapsed
            disk_write_bps[0] = (curr_disk.write_bytes - prev_disk.write_bytes) / elapsed
            prev_disk, prev_t = curr_disk, curr_t
            if set_resource_stats:
                set_resource_stats(cpu_pct[0], disk_read_bps[0], disk_write_bps[0])

    monitor_thread = threading.Thread(target=_monitor_resources, daemon=True)
    monitor_thread.start()

    # --- Slot management ---
    total_cores = os.cpu_count() or 1
    cores_per_slot = max(1, total_cores // max_workers)
    lock = threading.Lock()
    available_slots = list(range(max_workers))
    done = [0]

    def _done_increment():
        with lock:
            done[0] += 1
            set_overall(done[0], total)

    def acquire_slot():
        """
        Block until a slot is free and (if other jobs are already running) resource
        usage is below the configured thresholds.  Returns -1 if disk_stop is set.
        """
        while True:
            if disk_stop.is_set():
                return -1
            with lock:
                if available_slots:
                    slots_in_use = max_workers - len(available_slots)
                    if slots_in_use == 0:
                        # Always let the very first job start immediately
                        return available_slots.pop(0)
                    cpu_ok = not _PSUTIL or cpu_threshold <= 0 or cpu_pct[0] < cpu_threshold
                    disk_ok = not _PSUTIL or disk_threshold <= 0 or disk_write_bps[0] < disk_threshold
                    if cpu_ok and disk_ok:
                        return available_slots.pop(0)
            time.sleep(0.5)

    def release_slot(slot):
        with lock:
            available_slots.append(slot)
            available_slots.sort()

    def get_core_list(slot):
        """Return the CPU cores assigned to this slot, or None for single-worker runs."""
        if not _PSUTIL or max_workers <= 1:
            return None
        start = slot * cores_per_slot
        end = min(start + cores_per_slot, total_cores)
        return list(range(start, end)) if start < total_cores else None

    # Whether any of the cleanup flags requires removing the temp dir
    def _should_clean_tmp():
        return delete_tmp or replace_originals or delete_archive

    # --- Per-item workers ---

    def process_archive(input_dir, x):
        archive_path = os.path.join(input_dir, x)
        slot = acquire_slot()
        if slot < 0:
            log(f"Skipping {x} — output drive at capacity.\n")
            if on_job_done: on_job_done(archive_path, "stopped")
            _done_increment()
            return
        core_list = get_core_list(slot)
        try:
            check_dir = output_dir_override if output_dir_override else input_dir
            if _check_disk_full(check_dir):
                log(f"Skipping {x} — output drive at capacity.\n")
                if on_job_done: on_job_done(archive_path, "stopped")
                return

            set_status(f"Converting: {x}")
            set_file_progress(0)
            tmp_parent = input_dir + "_tmp"
            archive_name = x.rsplit(".", 1)[0]
            # Per-archive subdir so concurrent jobs from the same input_dir don't collide
            out = os.path.join(tmp_parent, archive_name)

            def _cleanup_tmp():
                if _should_clean_tmp():
                    try:
                        if os.path.exists(out):
                            shutil.rmtree(out)
                        os.rmdir(tmp_parent)  # succeeds only when all sibling jobs are done
                    except OSError:
                        pass

            job_status = "error"
            try:
                archive_size = os.path.getsize(archive_path)
                os.makedirs(out, exist_ok=True)

                log(f"\n--- {x}  [{os.path.basename(input_dir)}] ---\n")
                log(f"Archive size : {fmt_size(archive_size)}\n")
                if on_job_start: on_job_start(archive_path)
                log("Extracting...\n")
                extract_archive(archive_path, out)

                extracted_files = []
                for root, _, files in os.walk(out):
                    cue_basenames_here = {
                        f.rsplit(".", 1)[0].lower() for f in files if f.lower().endswith(".cue")
                    }
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
                    if skip_existing:
                        alt_path = os.path.join(input_dir, archive_name + ".chd")
                        found = next((p for p in (chd_path, alt_path) if os.path.exists(p)), None)
                        if found:
                            log(f"Skipping — CHD already exists: {found}\n")
                            continue
                    elif os.path.exists(chd_path):
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

                    def _prog(pct, _path=archive_path):
                        set_file_progress(pct)
                        if on_job_progress: on_job_progress(_path, pct)

                    ret = run_chdman(cmd, on_progress=_prog, on_line=log, core_list=core_list)

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
                job_status = "done" if ret == 0 else "error"

            except Exception as e:
                log(f"Error: {e}\n")
                with open(ERROR_LOG, "a") as f:
                    f.write(f"{x}: {str(e)}\n")

            # Cleanup runs whether the conversion succeeded or failed
            _cleanup_tmp()
            if on_job_done: on_job_done(archive_path, job_status)

        finally:
            release_slot(slot)
            _done_increment()

    def process_loose_group(input_dir, file_dir, files):
        slot = acquire_slot()
        if slot < 0:
            for f in files:
                file_path = os.path.join(file_dir, f)
                log(f"Skipping {f} — output drive at capacity.\n")
                if on_job_done: on_job_done(file_path, "stopped")
                _done_increment()
            return
        core_list = get_core_list(slot)
        try:
            check_dir = output_dir_override if output_dir_override else file_dir
            if _check_disk_full(check_dir):
                for f in files:
                    file_path = os.path.join(file_dir, f)
                    log(f"Skipping {f} — output drive at capacity.\n")
                    if on_job_done: on_job_done(file_path, "stopped")
                    _done_increment()
                return

            in_subdir = os.path.normpath(file_dir) != os.path.normpath(input_dir)
            output_dir = output_dir_override if output_dir_override else file_dir
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)

            all_succeeded = True
            for f in files:
                file_path = os.path.join(file_dir, f)
                set_status(f"Converting: {f}")
                set_file_progress(0)

                file_status = "error"
                try:
                    file_size = os.path.getsize(file_path)
                    label = os.path.relpath(file_dir, input_dir)
                    label = f if label == "." else f"{label}/{f}"
                    log(f"\n--- {label}  [{os.path.basename(input_dir)}] ---\n")
                    log(f"File size    : {fmt_size(file_size)}\n")

                    chd_name = (os.path.basename(file_dir) if use_dirname and in_subdir
                                else f.rsplit(".", 1)[0])
                    chd_path = os.path.join(output_dir, chd_name + ".chd")
                    if skip_existing:
                        alt_path = os.path.join(file_dir, chd_name + ".chd")
                        found = next((p for p in (chd_path, alt_path) if os.path.exists(p)), None)
                        if found:
                            log(f"Skipping — CHD already exists: {found}\n")
                            if on_job_done: on_job_done(file_path, "skipped")
                            _done_increment()
                            continue
                    elif os.path.exists(chd_path):
                        os.remove(chd_path)

                    if on_job_start: on_job_start(file_path)

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

                    def _prog(pct, _path=file_path):
                        set_file_progress(pct)
                        if on_job_progress: on_job_progress(_path, pct)

                    ret = run_chdman(cmd, on_progress=_prog, on_line=log, core_list=core_list)

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
                    file_status = "done" if ret == 0 else "error"

                except Exception as e:
                    log(f"Error: {e}\n")
                    with open(ERROR_LOG, "a") as ef:
                        ef.write(f"{f}: {str(e)}\n")
                    all_succeeded = False

                if on_job_done: on_job_done(file_path, file_status)
                _done_increment()

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

        finally:
            release_slot(slot)

    # --- Dispatch ---
    loose_by_dir = defaultdict(list)
    for input_dir, file_dir, f in all_loose:
        loose_by_dir[(input_dir, file_dir)].append(f)

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = (
                [executor.submit(process_archive, d, x) for d, x in all_archives] +
                [executor.submit(process_loose_group, d, fd, fs)
                 for (d, fd), fs in loose_by_dir.items()]
            )
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    log(f"Worker error: {e}\n")
    finally:
        stop_monitor.set()
        if set_resource_stats:
            set_resource_stats(0.0, 0.0, 0.0)

    set_status(f"Done — {total} item{'s' if total != 1 else ''} processed.")
    log("\nAll done.\n")
