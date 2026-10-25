# core.py – high-level operations shared by the CLI and the GUI
#
# Author: Vladislav Tislenko (keklick1337), 2025-2026

import concurrent.futures
import os
import threading
from typing import Callable, Iterable, Optional

from Crypto.Cipher import AES

from .fus import NETWORK_ERRORS, FUSClient, FUSError, get_latest_version, normalize_version, v2_key

# progress(stage, done_bytes, total_bytes); stage is "download" or "decrypt"
ProgressFn = Callable[[str, int, int], None]
LogFn = Callable[[str], None]

CHUNK_SIZE = 0x10000
MAX_RETRIES = 5


class Cancelled(Exception):
    """Raised when an operation is cancelled through its cancel event."""


def format_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def format_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} sec"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} hrs"


def _check_cancel(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def check_regions(
    model: str,
    regions: Iterable[str],
    on_result: Callable[[str, Optional[str], Optional[str]], None],
    cancel: Optional[threading.Event] = None,
    workers: int = 10,
) -> None:
    """
    Look up the latest firmware for each region in parallel.

    :param on_result: called as on_result(region, version, error) for every region
    """
    def worker(region: str) -> None:
        if cancel is not None and cancel.is_set():
            return
        try:
            on_result(region, get_latest_version(model, region), None)
        except Exception as e:
            on_result(region, None, str(e))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, regions))


def decrypted_name(path: str) -> str:
    """firmware.zip.enc4 → firmware.zip"""
    return path.rsplit(".", 1)[0] if path.endswith((".enc2", ".enc4")) else path + ".dec"


def get_decryption_key(enc_ver: int, model: str, region: str, version: str) -> bytes:
    """Key for manual decryption of an .enc2/.enc4 file."""
    version = normalize_version(version)
    if enc_ver == 2:
        return v2_key(model, region, version)
    if enc_ver == 4:
        info = FUSClient().binary_inform(model, region, version)
        return info.key
    raise ValueError(f"Unknown encryption version: {enc_ver}")


def decrypt_file(
    src: str,
    dst: str,
    key: bytes,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[threading.Event] = None,
) -> None:
    """Decrypt an AES-ECB encrypted firmware file. Writes to `dst` atomically."""
    total = os.path.getsize(src)
    if total % 16:
        raise ValueError("Invalid encrypted file size (not a multiple of 16 bytes)")
    cipher = AES.new(key, AES.MODE_ECB)
    tmp = dst + ".part"
    done = 0
    try:
        with open(src, "rb") as inf, open(tmp, "wb") as outf:
            while True:
                _check_cancel(cancel)
                block = inf.read(CHUNK_SIZE * 16)
                if not block:
                    break
                done += len(block)
                dec = cipher.decrypt(block)
                if done == total:
                    dec = _unpad(dec)
                outf.write(dec)
                if progress:
                    progress("decrypt", done, total)
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _unpad(data: bytes) -> bytes:
    """Strip PKCS#7 padding if present."""
    if data:
        pad = data[-1]
        if 1 <= pad <= 16 and data.endswith(bytes([pad]) * pad):
            return data[:-pad]
    return data


def _fetch(client: FUSClient, info, out: str, progress: Optional[ProgressFn],
           cancel: Optional[threading.Event], log: LogFn) -> None:
    """Download (or resume) the package into `out`, retrying on network errors."""
    retries = 0
    while True:
        offset = os.path.getsize(out) if os.path.exists(out) else 0
        if offset >= info.size:
            return
        try:
            with client.open_download(info, offset) as r:
                if offset and r.status != 206:
                    offset = 0  # server ignored Range, start over
                with open(out, "ab" if offset else "wb") as fd:
                    done = offset
                    while True:
                        _check_cancel(cancel)
                        chunk = r.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        fd.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress("download", done, info.size)
            if os.path.getsize(out) < info.size:
                raise ConnectionError("connection closed early")
            return
        except NETWORK_ERRORS as e:
            retries += 1
            if retries > MAX_RETRIES:
                raise
            log(f"Network error ({e}), resuming... [{retries}/{MAX_RETRIES}]")


def download_firmware(
    model: str,
    region: str,
    version: str,
    out_dir: str,
    decrypt: bool = True,
    log: LogFn = print,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[threading.Event] = None,
) -> str:
    """
    Download a firmware package (resuming a partial file if present) and optionally decrypt it.

    :return: path of the resulting file
    """
    client = FUSClient()
    info = client.binary_inform(model, region, version)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, info.filename)
    final = decrypted_name(out) if info.encrypted else out

    if decrypt and info.encrypted and os.path.exists(final) and not os.path.exists(out):
        log(f"{final} already exists, skipping")
        return final

    existing = os.path.getsize(out) if os.path.exists(out) else 0
    if existing > info.size:
        os.remove(out)
        existing = 0
    if existing == info.size:
        log(f"{info.filename} already downloaded")
    else:
        log(("Resuming " if existing else "Downloading ") + info.filename)
        client.init_download(info)
        _fetch(client, info, out, progress, cancel, log)

    if not (decrypt and info.encrypted):
        return out
    log(f"Decrypting {info.filename}")
    decrypt_file(out, final, info.key, progress, cancel)
    os.remove(out)
    return final


__all__ = [
    "Cancelled", "FUSError", "check_regions", "decrypt_file", "decrypted_name",
    "download_firmware", "format_size", "format_time", "get_decryption_key",
]
