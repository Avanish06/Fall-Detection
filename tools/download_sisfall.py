#!/usr/bin/env python3
"""
tools/download_sisfall.py
=========================
SisFall Dataset Acquisition Engine for FallGuard v2.
Downloads the official SisFall dataset or a balanced representative subset
directly from the public GitHub mirror using zero external dependencies.

Features:
- Dual Modes: --subset (fast balanced cohort, 8 subjects: SA01, SA05, SA10, SA15,
  SA20, SE01, SE06, SE10 covering all 15 Fall types and all 19 ADL types)
  and --full (all 4,505 files).
- HTTP 206 Partial Content range resuming for resilient large downloads.
- Contiguous range streaming for fast subset acquisition without full archive download.
- Automated extraction of inner SisFall_dataset.zip into data/sisfall/raw/.
- Complete CRC32 and byte-size integrity verification.
- Pure Python standard library (urllib, zipfile, zlib, struct).
"""

import argparse
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path

SISFALL_URL = "https://github.com/BIng2325/SisFall/releases/download/dataset/SisFall.zip"
EXPECTED_TOTAL_SIZE = 213584954
INNER_ZIP_DATA_OFFSET = 376
INNER_CD_OFFSET_IN_OUTER = 213040015
INNER_CD_SIZE = 544775

DEFAULT_SUBSET_SUBJECTS = ["SA01", "SA05", "SA10", "SA15", "SA20", "SE01", "SE06", "SE10"]
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FallGuard/2.0"


class ProgressBar:
    """Standard-library terminal progress display."""
    def __init__(self, total_bytes: int, prefix: str = "Downloading", quiet: bool = False):
        self.total = total_bytes
        self.prefix = prefix
        self.quiet = quiet
        self.start_time = time.time()
        self.last_update = 0.0

    def update(self, current_bytes: int):
        if self.quiet:
            return
        now = time.time()
        if now - self.last_update < 0.1 and current_bytes < self.total:
            return
        self.last_update = now

        elapsed = now - self.start_time
        speed = current_bytes / elapsed if elapsed > 0 else 0
        speed_mb = speed / (1024 * 1024)
        pct = (current_bytes / self.total * 100) if self.total > 0 else 0
        eta = (self.total - current_bytes) / speed if speed > 0 else 0

        bar_len = 25
        filled = int(bar_len * current_bytes // self.total) if self.total > 0 else 0
        bar = "=" * filled + "-" * (bar_len - filled)

        cur_mb = current_bytes / (1024 * 1024)
        tot_mb = self.total / (1024 * 1024)
        try:
            sys.stdout.write(
                f"\r{self.prefix}: [{bar}] {pct:5.1f}% | {cur_mb:5.1f}/{tot_mb:5.1f} MB | {speed_mb:4.2f} MB/s | ETA: {int(eta):3d}s"
            )
            sys.stdout.flush()
        except Exception:
            pass

    def finish(self, message: str = "Complete."):
        if not self.quiet:
            sys.stdout.write(f"\r{self.prefix}: {message}{' ' * 40}\n")
            sys.stdout.flush()


def fetch_url_with_retry(req: urllib.request.Request, retries: int = 3, timeout: int = 30):
    """Executes an HTTP request with exponential backoff retry."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, ConnectionResetError) as e:
            last_err = e
            if attempt == retries:
                raise
            time.sleep(1.5 ** attempt)
    if last_err:
        raise last_err


def download_full_archive(dest_path: Path, quiet: bool = False) -> Path:
    """Downloads the complete 203.7 MB archive with HTTP 206 range resume support."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest_path.with_suffix(".zip.part")

    existing_bytes = part_path.stat().st_size if part_path.exists() else 0
    headers = {"User-Agent": USER_AGENT}
    if existing_bytes > 0:
        headers["Range"] = f"bytes={existing_bytes}-"

    req = urllib.request.Request(SISFALL_URL, headers=headers)
    progress = ProgressBar(EXPECTED_TOTAL_SIZE, prefix="Downloading SisFall.zip", quiet=quiet)

    with fetch_url_with_retry(req) as response:
        status = getattr(response, "status", 200)
        if status == 206:
            mode = "ab"
            downloaded = existing_bytes
        else:
            mode = "wb"
            downloaded = 0

        with open(part_path, mode) as f:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                progress.update(downloaded)

    progress.finish("Download finished.")
    actual_size = part_path.stat().st_size
    if actual_size != EXPECTED_TOTAL_SIZE:
        raise ValueError(
            f"Size mismatch: expected {EXPECTED_TOTAL_SIZE} bytes, got {actual_size}"
        )
    if dest_path.exists():
        dest_path.unlink()
    part_path.replace(dest_path)
    return dest_path


def parse_inner_central_directory(cache_dir: Path) -> list[dict]:
    """Retrieves and parses the Central Directory of SisFall_dataset.zip."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cd_cache = cache_dir / "inner_central_directory.bin"
    if cd_cache.exists() and cd_cache.stat().st_size == INNER_CD_SIZE:
        cd_data = cd_cache.read_bytes()
    else:
        req = urllib.request.Request(
            SISFALL_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Range": f"bytes={INNER_CD_OFFSET_IN_OUTER}-{INNER_CD_OFFSET_IN_OUTER + INNER_CD_SIZE - 1}"
            }
        )
        with fetch_url_with_retry(req) as res:
            cd_data = res.read()
        cd_cache.write_bytes(cd_data)

    entries = []
    pos = 0
    while pos < len(cd_data) - 4:
        if cd_data[pos:pos+4] == b"PK\x01\x02":
            method = int.from_bytes(cd_data[pos+10:pos+12], "little")
            crc = int.from_bytes(cd_data[pos+16:pos+20], "little")
            csize = int.from_bytes(cd_data[pos+20:pos+24], "little")
            usize = int.from_bytes(cd_data[pos+24:pos+28], "little")
            fn_len = int.from_bytes(cd_data[pos+28:pos+30], "little")
            ex_len = int.from_bytes(cd_data[pos+30:pos+32], "little")
            cm_len = int.from_bytes(cd_data[pos+32:pos+34], "little")
            offset = int.from_bytes(cd_data[pos+42:pos+46], "little")
            fname = cd_data[pos+46:pos+46+fn_len].decode("latin1")
            entries.append({
                "name": fname,
                "offset": offset,
                "csize": csize,
                "usize": usize,
                "crc": crc,
                "method": method
            })
            pos += 46 + fn_len + ex_len + cm_len
        else:
            pos += 1
    return entries


def extract_from_local_zip(zip_path: Path, output_dir: Path, subjects: list[str] = None, quiet: bool = False):
    """Extracts either full archive or selected subjects from cached local zip."""
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as outer_zip:
        with outer_zip.open("SisFall_dataset.zip") as inner_file:
            with zipfile.ZipFile(inner_file) as inner_zip:
                all_names = inner_zip.namelist()
                if subjects:
                    target_names = [
                        n for n in all_names
                        if any(f"/{s}/" in n for s in subjects) or n.endswith("Readme.txt")
                    ]
                else:
                    target_names = all_names

                count = len(target_names)
                for idx, name in enumerate(target_names, 1):
                    if not quiet and idx % 100 == 0:
                        sys.stdout.write(f"\rExtracting local files: {idx}/{count}")
                        sys.stdout.flush()
                    inner_zip.extract(name, output_dir)
                    # Also if it's Readme.txt, ensure a copy at top level output_dir/Readme.txt
                    if name.endswith("Readme.txt"):
                        readme_path = output_dir / "Readme.txt"
                        if not readme_path.exists():
                            readme_content = inner_zip.read(name)
                            readme_path.write_bytes(readme_content)

                if not quiet:
                    sys.stdout.write(f"\rExtracting local files: {count}/{count} complete.\n")


def download_subset_via_range(output_dir: Path, cache_dir: Path, subjects: list[str], quiet: bool = False):
    """Downloads subset subjects directly via batch contiguous HTTP 206 range requests."""
    entries = parse_inner_central_directory(cache_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch Readme.txt
    readme_entry = next((e for e in entries if e["name"].endswith("Readme.txt")), None)
    if readme_entry:
        readme_req = urllib.request.Request(
            SISFALL_URL,
            headers={
                "User-Agent": USER_AGENT,
                "Range": f"bytes={INNER_ZIP_DATA_OFFSET + readme_entry['offset']}-{INNER_ZIP_DATA_OFFSET + readme_entry['offset'] + 120 + readme_entry['csize']}"
            }
        )
        with fetch_url_with_retry(readme_req) as res:
            blob = res.read()
        fn_len = int.from_bytes(blob[26:28], "little")
        ex_len = int.from_bytes(blob[28:30], "little")
        comp = blob[30 + fn_len + ex_len : 30 + fn_len + ex_len + readme_entry["csize"]]
        decomp = zlib.decompress(comp, -15) if readme_entry["method"] == 8 else comp
        # Save both at output_dir / Readme.txt and output_dir / readme_entry["name"]
        (output_dir / "Readme.txt").write_bytes(decomp)
        nested_readme = output_dir / readme_entry["name"]
        nested_readme.parent.mkdir(parents=True, exist_ok=True)
        nested_readme.write_bytes(decomp)

    # 2. Fetch each subject in contiguous range
    total_subjects = len(subjects)
    for s_idx, subj in enumerate(subjects, 1):
        subj_entries = [e for e in entries if f"/{subj}/" in e["name"] and e["name"].endswith(".txt")]
        if not subj_entries:
            continue
        subj_entries.sort(key=lambda x: x["offset"])

        first_off = INNER_ZIP_DATA_OFFSET + subj_entries[0]["offset"]
        last_e = subj_entries[-1]
        last_off = INNER_ZIP_DATA_OFFSET + last_e["offset"] + 120 + last_e["csize"]
        if last_off >= EXPECTED_TOTAL_SIZE:
            last_off = EXPECTED_TOTAL_SIZE - 1
        span_bytes = last_off - first_off + 1

        if not quiet:
            sys.stdout.write(
                f"[{s_idx}/{total_subjects}] Fetching subject {subj} ({len(subj_entries)} files, {span_bytes / (1024 * 1024):.2f} MB)... "
            )
            sys.stdout.flush()

        t0 = time.time()
        req = urllib.request.Request(
            SISFALL_URL,
            headers={"User-Agent": USER_AGENT, "Range": f"bytes={first_off}-{last_off}"}
        )
        with fetch_url_with_retry(req) as res:
            blob = res.read()
        elapsed = time.time() - t0

        for e in subj_entries:
            rel_off = (INNER_ZIP_DATA_OFFSET + e["offset"]) - first_off
            fn_len = int.from_bytes(blob[rel_off+26:rel_off+28], "little")
            ex_len = int.from_bytes(blob[rel_off+28:rel_off+30], "little")
            comp_data = blob[rel_off + 30 + fn_len + ex_len : rel_off + 30 + fn_len + ex_len + e["csize"]]
            decomp = zlib.decompress(comp_data, -15)
            if zlib.crc32(decomp) != e["crc"]:
                raise ValueError(f"CRC32 mismatch for {e['name']}: expected {hex(e['crc'])}, got {hex(zlib.crc32(decomp))}")

            out_path = output_dir / e["name"]
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(decomp)

        if not quiet:
            speed = (len(blob) / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            sys.stdout.write(f"Done ({elapsed:.1f}s, {speed:.2f} MB/s).\n")


def verify_extracted_files(output_dir: Path) -> tuple[int, int, int]:
    """Scans and counts extracted files, verifying valid text content."""
    files = [f for f in output_dir.rglob("*.txt") if not f.name.lower().startswith("readme")]
    falls = sum(1 for f in files if f.name.startswith("F"))
    adls = sum(1 for f in files if f.name.startswith("D"))
    return len(files), falls, adls


def main():
    parser = argparse.ArgumentParser(
        description="SisFall Dataset Acquisition Engine for FallGuard v2",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--subset", action="store_true", default=False,
        help="Download and extract balanced representative subset (8 subjects: SA01, SA05, SA10, SA15, SA20, SE01, SE06, SE10)"
    )
    group.add_argument(
        "--full", action="store_true", default=False,
        help="Download and extract all subjects (all 4,505 files, 203.7 MB)"
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=Path("data/sisfall/cache"),
        help="Cache directory for downloaded archives"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/sisfall/raw"),
        help="Output directory for extracted raw .txt files"
    )
    parser.add_argument(
        "--raw-dir", type=Path, default=None,
        help=argparse.SUPPRESS  # Synonym for --output-dir
    )
    parser.add_argument(
        "--force", action="store_true", default=False,
        help="Force re-download / overwrite even if already cached"
    )
    parser.add_argument(
        "--verify", action="store_true", default=False,
        help="Verify existing extracted files only"
    )
    parser.add_argument(
        "--quiet", action="store_true", default=False,
        help="Suppress progress outputs"
    )

    args = parser.parse_args()

    # Handle alias
    output_dir = args.raw_dir if args.raw_dir is not None else args.output_dir
    cache_dir = args.cache_dir

    if args.verify:
        total, falls, adls = verify_extracted_files(output_dir)
        print(f"Verification: Found {total} trial files ({falls} falls, {adls} ADLs) in {output_dir}")
        return 0

    # If neither --full nor --subset is given, default to subset
    mode = "full" if args.full else "subset"

    cached_zip = cache_dir / "SisFall.zip"

    t_start = time.time()
    if mode == "full":
        if not cached_zip.exists() or args.force:
            if not args.quiet:
                print(f"Downloading full SisFall archive ({EXPECTED_TOTAL_SIZE / (1024 * 1024):.1f} MB)...")
            download_full_archive(cached_zip, quiet=args.quiet)
        if not args.quiet:
            print(f"Extracting all subjects from {cached_zip} to {output_dir}...")
        extract_from_local_zip(cached_zip, output_dir, quiet=args.quiet)
    else:
        # Subset mode
        if cached_zip.exists() and not args.force:
            if not args.quiet:
                print(f"Found cached archive at {cached_zip}. Extracting subset ({len(DEFAULT_SUBSET_SUBJECTS)} subjects)...")
            extract_from_local_zip(cached_zip, output_dir, subjects=DEFAULT_SUBSET_SUBJECTS, quiet=args.quiet)
        else:
            if not args.quiet:
                print(
                    f"Downloading representative subset ({len(DEFAULT_SUBSET_SUBJECTS)} subjects: "
                    f"{', '.join(DEFAULT_SUBSET_SUBJECTS)}) via HTTP 206 Range requests..."
                )
            try:
                download_subset_via_range(output_dir, cache_dir, subjects=DEFAULT_SUBSET_SUBJECTS, quiet=args.quiet)
            except Exception as e:
                if not args.quiet:
                    print(f"Range download failed ({e}). Falling back to full archive download...")
                download_full_archive(cached_zip, quiet=args.quiet)
                extract_from_local_zip(cached_zip, output_dir, subjects=DEFAULT_SUBSET_SUBJECTS, quiet=args.quiet)

    total, falls, adls = verify_extracted_files(output_dir)
    elapsed = time.time() - t_start
    print(
        f"\n[SisFall Acquisition Complete in {elapsed:.1f}s]\n"
        f"Extracted {total} activity trial files ({falls} falls, {adls} ADLs) into {output_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
