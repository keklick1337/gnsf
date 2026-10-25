# firmware.py – parsing of Samsung firmware version strings
#
# Author: Vladislav Tislenko (keklick1337), 2025-2026

import string
from dataclasses import dataclass
from typing import Optional

from .fus import normalize_version

_ITERATION_CHARS = string.digits + string.ascii_uppercase


@dataclass
class FirmwareInfo:
    bootloader: Optional[str]  # e.g. "U8" (U = upgrade, S = security) or None for old builds
    major: Optional[int]       # major iteration (A = 0, B = 1, ...)
    year: int
    month: int                 # 1..12
    minor: int                 # minor iteration (1 = 1, ... A = 10, ...)


def _year(ch: str) -> int:
    # Newer builds: R = 2018, S = 2019, ...; older builds: A = 2001, B = 2002, ...
    if ch >= "R":
        return 2018 + ord(ch) - ord("R")
    return 2001 + ord(ch) - ord("A")


def parse_firmware(version: str) -> FirmwareInfo:
    """
    Extract bootloader/date/iteration from the PDA part of a version string.

    :param version: full "PDA/CSC/MODEM" string or a single PDA code
    :raises ValueError: if the version string is not recognized
    """
    pda = version.split("/")[0].strip().upper()
    if len(pda) < 8 or not pda.isalnum():
        raise ValueError(f"unrecognized firmware version: {version}")

    tail = pda[-6:]
    bootloader = major = None
    if len(tail) == 6 and tail[0] in "US" and tail[2] in string.ascii_uppercase:
        bootloader = tail[:2]
        major = ord(tail[2]) - ord("A")

    year_ch, month_ch, minor_ch = pda[-3:]
    if year_ch not in string.ascii_uppercase or month_ch not in "ABCDEFGHIJKL":
        raise ValueError(f"unrecognized firmware date in: {version}")
    return FirmwareInfo(
        bootloader=bootloader,
        major=major,
        year=_year(year_ch),
        month=ord(month_ch) - ord("A") + 1,
        minor=_ITERATION_CHARS.index(minor_ch),
    )


def sort_key(version: str) -> tuple:
    """Sort key: newest firmware last (unparseable versions first)."""
    try:
        fw = parse_firmware(version)
    except ValueError:
        return (0, 0, 0, 0)
    return (fw.year, fw.month, fw.major or 0, fw.minor)


def format_firmware_info(version: str) -> str:
    """Human-readable description of a firmware version."""
    lines = [f"Firmware: {normalize_version(version) if '/' in version else version}"]
    try:
        fw = parse_firmware(version)
    except ValueError:
        lines.append("(could not parse version details)")
        return "\n".join(lines)
    if fw.bootloader:
        lines.append(f"Bootloader type: {fw.bootloader}")
    lines.append(f"Date: {fw.year}.{fw.month:02d} (YYYY.MM)")
    lines.append(f"Version iteration: {fw.major if fw.major is not None else 0}.{fw.minor}")
    return "\n".join(lines)
