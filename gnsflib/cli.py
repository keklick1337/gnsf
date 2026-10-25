# cli.py – command-line interface
#
# Author: Vladislav Tislenko (keklick1337), 2025-2026

import argparse
import shutil
import sys
import threading
import time
from typing import Dict, List, Optional

from . import VERSION
from .core import (Cancelled, check_regions, decrypt_file, download_firmware, format_size, format_time,
                   get_decryption_key)
from .csc import CSC_DICT
from .firmware import format_firmware_info, sort_key
from .fus import FUSError, fetch_versions, resolve_version


class ConsoleProgress:
    """Progress callback that draws a single-line progress bar on stderr."""

    def __init__(self) -> None:
        self.stage = ""
        self.start = self.start_done = 0.0
        self.last_draw = 0.0

    def __call__(self, stage: str, done: int, total: int) -> None:
        now = time.monotonic()
        if stage != self.stage:
            self.close()
            self.stage, self.start, self.start_done = stage, now, done
        elif now - self.last_draw < 0.2 and done < total:
            return
        self.last_draw = now
        elapsed = now - self.start
        speed = (done - self.start_done) / elapsed if elapsed > 0 else 0
        eta = f" ETA {format_time((total - done) / speed)}" if speed > 0 and done < total else ""
        info = (f" {done * 100 / total:5.1f}% {format_size(done)}/{format_size(total)}"
                f" {format_size(speed)}/s{eta}") if total else ""
        width = max(shutil.get_terminal_size((80, 20)).columns - len(stage) - len(info) - 4, 10)
        filled = int(width * done / total) if total else 0
        sys.stderr.write(f"\r{stage} [{'#' * filled}{'.' * (width - filled)}]{info}")
        sys.stderr.flush()

    def log(self, msg: str) -> None:
        """Print a message without breaking the progress line."""
        self.close()
        print(msg)

    def close(self) -> None:
        if self.stage:
            sys.stderr.write("\n")
            self.stage = ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gnsf", description="Download and decrypt Samsung firmware")
    p.add_argument("-m", "--dev-model", required=True, help="device model, e.g. SM-S928B")
    p.add_argument("-r", "--dev-region", help="region (CSC) code, e.g. XSA")
    # IMEI / serial number are no longer needed by the server; accepted for compatibility
    p.add_argument("-i", "--dev-imei", help=argparse.SUPPRESS)
    p.add_argument("-s", "--dev-serial", help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version=f"GNSF {VERSION}")

    subs = p.add_subparsers(dest="command", required=True)

    chk = subs.add_parser("check", help="print the latest firmware (all regions if -r is omitted)")
    chk.add_argument("-a", "--all-versions", action="store_true",
                     help="also list older versions published for the region")

    dl = subs.add_parser("download", help="download & decrypt firmware")
    dl.add_argument("-v", "--fw-ver",
                    help="firmware version (full PDA/CSC/MODEM or a single part); latest if omitted")
    dl.add_argument("-O", "--out-dir", default="./downloads", help="directory to save firmware")
    dl.add_argument("--no-decrypt", action="store_true", help="keep the encrypted file")
    dl.add_argument("-R", "--resume", action="store_true", help=argparse.SUPPRESS)  # always on now

    dec = subs.add_parser("decrypt", help="manually decrypt an .enc2/.enc4 file")
    dec.add_argument("-v", "--fw-ver", required=True, help="full firmware version of the file")
    dec.add_argument("-V", "--enc-ver", type=int, choices=[2, 4],
                     help="encryption version (detected from the file name by default)")
    dec.add_argument("-i", "--in-file", required=True, help="input .enc2/.enc4 file")
    dec.add_argument("-o", "--out-file", help="output file (default: input without .encN)")
    return p


def cmd_check(args: argparse.Namespace) -> int:
    if args.dev_region:
        info = fetch_versions(args.dev_model, args.dev_region)
        print(format_firmware_info(info.latest))
        if args.all_versions:
            print("\nPrevious versions:")
            for v in sorted(info.previous, key=sort_key, reverse=True):
                print(f"  {v}")
        return 0

    lock = threading.Lock()
    found: Dict[str, str] = {}

    def on_result(region: str, version: Optional[str], error: Optional[str]) -> None:
        name = CSC_DICT.get(region, "")
        with lock:
            if version:
                found[region] = version
                print(f"CSC [{region}] ({name}) -> {version}")

    check_regions(args.dev_model, CSC_DICT.keys(), on_result)
    if not found:
        print(f"No firmware found for {args.dev_model}")
        return 1
    print(f"\nFound in {len(found)} of {len(CSC_DICT)} regions")
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    if not args.dev_region:
        print("Error: -r/--dev-region is required for download", file=sys.stderr)
        return 1
    version = resolve_version(args.dev_model, args.dev_region, args.fw_ver)
    print(format_firmware_info(version))
    progress = ConsoleProgress()
    try:
        path = download_firmware(
            args.dev_model, args.dev_region, version, args.out_dir,
            decrypt=not args.no_decrypt, log=progress.log, progress=progress,
        )
    finally:
        progress.close()
    print(f"Saved to {path}")
    return 0


def cmd_decrypt(args: argparse.Namespace) -> int:
    enc_ver = args.enc_ver
    if enc_ver is None:
        if args.in_file.endswith(".enc2"):
            enc_ver = 2
        elif args.in_file.endswith(".enc4"):
            enc_ver = 4
        else:
            print("Error: cannot detect encryption version, pass -V 2 or -V 4", file=sys.stderr)
            return 1
    if not args.dev_region:
        print("Error: -r/--dev-region is required for decryption", file=sys.stderr)
        return 1
    out = args.out_file or args.in_file.rsplit(".", 1)[0]
    key = get_decryption_key(enc_ver, args.dev_model, args.dev_region, args.fw_ver)
    progress = ConsoleProgress()
    try:
        decrypt_file(args.in_file, out, key, progress)
    finally:
        progress.close()
    print(f"Decrypted to {out}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dev_imei or args.dev_serial:
        print("Note: IMEI/serial number are no longer required and are ignored", file=sys.stderr)
    args.dev_model = args.dev_model.strip().upper()
    if args.dev_region:
        args.dev_region = args.dev_region.strip().upper()
    commands = {"check": cmd_check, "download": cmd_download, "decrypt": cmd_decrypt}
    try:
        return commands[args.command](args)
    except (FUSError, Cancelled) as e:
        print(f"Error: {e}", file=sys.stderr)
    except KeyboardInterrupt:
        print("\nInterrupted (run the same command again to resume)", file=sys.stderr)
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
    return 1
