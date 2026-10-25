# fus.py – client for the Samsung Firmware Update Server (FUS)
#
# Author: Vladislav Tislenko (keklick1337), 2025-2026
#
# Uses the "Smart" FUS endpoints (the ones Smart Switch uses). They need no
# IMEI or serial number and also serve Galaxy Watch firmware. Protocol details
# (auth key, request layout) as documented by samloader-rs:
# https://github.com/topjohnwu/samloader-rs

import hashlib
import http.client
import http.cookiejar
import ssl
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional
from xml.sax.saxutils import escape

from Crypto.Cipher import AES

FUS_URL = "https://neofussvr.sslcs.cdngc.net/"
CLOUD_URL = "http://cloud-neofussvr.samsungmobile.com/"
FOTA_URL = "https://fota-cloud-dn.ospserver.net/firmware/{region}/{model}/version.xml"

FUS_USER_AGENT = "SMART 2.0"
FOTA_USER_AGENT = "Kies2.0_FUS"

# AES-128 key used to sign the server nonce
AUTH_KEY = bytes.fromhex("422e73733617ae2b198940fd4e32b0a5")

TIMEOUT = 60  # seconds per socket operation


class FUSError(Exception):
    """Raised when the server rejects a request or returns unexpected data."""


# --------------------------------------------------------------------------- #
# HTTP helpers (stdlib only)
# --------------------------------------------------------------------------- #

def _ssl_context() -> ssl.SSLContext:
    # Prefer certifi's CA bundle when available (python.org builds on macOS
    # ship without system certificates), otherwise use the system store.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _opener(cookies: Optional[http.cookiejar.CookieJar] = None) -> urllib.request.OpenerDirector:
    handlers = [urllib.request.HTTPSHandler(context=_ssl_context())]
    if cookies is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookies))
    return urllib.request.build_opener(*handlers)


# Errors worth retrying a download on (HTTP status errors are not among them)
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError)


# --------------------------------------------------------------------------- #
# Crypto helpers
# --------------------------------------------------------------------------- #

def sign_nonce(nonce: str) -> str:
    """Return the request signature for a server nonce (hex AES-ECB of the nonce)."""
    block = nonce.encode()[:16].ljust(16, b"0")
    return AES.new(AUTH_KEY, AES.MODE_ECB).encrypt(block).hex()


def logic_check(inp: str, nonce: str) -> str:
    """Pick characters of `inp` indexed by the low 4 bits of each nonce char."""
    return "".join(inp[ord(c) & 0xF] if (ord(c) & 0xF) < len(inp) else "." for c in nonce)


def normalize_version(version: str) -> str:
    """
    Normalize a 3- or 4-part firmware version to exactly 4 parts.

    :param version: e.g. "PDA/CSC/MODEM" or "PDA/CSC//PDA"
    :return: "PDA/CSC/MODEM/PDA"
    """
    parts = version.strip().split("/")
    if len(parts) == 3:
        parts.append(parts[0])
    if len(parts) >= 3 and parts[2] == "":
        parts[2] = parts[0]
    return "/".join(parts)


def v2_key(model: str, region: str, version: str) -> bytes:
    """Key for .enc2 files: MD5 of "region:model:version"."""
    return hashlib.md5(f"{region}:{model}:{version}".encode()).digest()


def v4_key(version: str, logic_value: str) -> bytes:
    """Key for .enc4 files: MD5 of the logic check of the version with LOGIC_VALUE_FACTORY."""
    return hashlib.md5(logic_check(version, logic_value).encode()).digest()


# --------------------------------------------------------------------------- #
# XML helpers
# --------------------------------------------------------------------------- #

def _fus_msg(put: Dict[str, str], get: Optional[List[str]] = None) -> str:
    """Build a FUSMsg request body."""
    body = "".join(f"<{k}><Data>{escape(str(v))}</Data></{k}>" for k, v in put.items())
    get_xml = ""
    if get:
        get_xml = "<Get><CmdID>2</CmdID>" + "".join(f"<{k}/>" for k in get) + "</Get>"
    return (
        "<FUSMsg><FUSHdr><ProtoVer>1.0</ProtoVer><SessionID>0</SessionID><MsgID>1</MsgID></FUSHdr>"
        f"<FUSBody><Put>{body}</Put>{get_xml}</FUSBody></FUSMsg>"
    )


def _parse_put(xml_text: str) -> Dict[str, str]:
    """Check the result status and return the <Put> section as tag → value."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise FUSError(f"Invalid server response: {e}") from e
    status = root.findtext("./FUSBody/Results/Status") or "?"
    if status not in ("200", "S00"):
        raise FUSError(f"Server returned status {status} — firmware is not available for this model/region/version (try another region)")
    put = root.find("./FUSBody/Put")
    return {e.tag: (e.findtext("Data") or "") for e in put} if put is not None else {}


# --------------------------------------------------------------------------- #
# version.xml (public FOTA metadata)
# --------------------------------------------------------------------------- #

@dataclass
class VersionInfo:
    latest: str
    previous: List[str]


def fetch_versions(model: str, region: str) -> VersionInfo:
    """Fetch the latest and previously published firmware versions from version.xml."""
    req = urllib.request.Request(FOTA_URL.format(region=region, model=model),
                                 headers={"User-Agent": FOTA_USER_AGENT})
    try:
        with _opener().open(req, timeout=TIMEOUT) as r:
            content = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            raise FUSError(f"Model or region not found ({e.code})") from None
        raise FUSError(f"version.xml request failed: HTTP {e.code}") from None
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise FUSError(f"Invalid version.xml: {e}") from e
    latest = (root.findtext("./firmware/version/latest") or "").strip()
    if not latest:
        raise FUSError("No firmware available")
    previous = [normalize_version(v.text.strip())
                for v in root.findall("./firmware/version/upgrade/value") if v.text and v.text.strip()]
    return VersionInfo(normalize_version(latest), previous)


def get_latest_version(model: str, region: str) -> str:
    return fetch_versions(model, region).latest


def resolve_version(model: str, region: str, version: Optional[str]) -> str:
    """
    Turn user input into a full 4-part version string.
    No input → latest; a partial version (e.g. only the PDA or CSC part)
    is looked up among the versions listed in version.xml.
    """
    if not version:
        return get_latest_version(model, region)
    version = version.strip()
    if "/" in version:
        return normalize_version(version)
    info = fetch_versions(model, region)
    for full in [info.latest] + info.previous:
        if version.upper() in full.upper().split("/"):
            return full
    raise FUSError(
        f"Could not find full version for '{version}'. "
        "Pass the full PDA/CSC/MODEM version (see the 'check' command)."
    )


# --------------------------------------------------------------------------- #
# FUS client
# --------------------------------------------------------------------------- #

@dataclass
class BinaryInfo:
    """Firmware package metadata returned by BinaryInform."""
    model: str
    region: str
    version: str
    filename: str
    path: str
    size: int
    model_type: str
    logic_value: str

    @property
    def encrypted(self) -> bool:
        return self.filename.endswith((".enc2", ".enc4"))

    @property
    def key(self) -> bytes:
        """AES key that decrypts this package."""
        if self.filename.endswith(".enc2"):
            return v2_key(self.model, self.region, self.version)
        return v4_key(self.version, self.logic_value)


class FUSClient:
    """Session with the FUS server; keeps the nonce and signature up to date."""

    def __init__(self) -> None:
        self.opener = _opener(http.cookiejar.CookieJar())
        self.encnonce = ""
        self.signature = ""
        self._post("NF_SmartDownloadGenerateNonce.do")

    def _headers(self) -> Dict[str, str]:
        auth = (f'FUS nonce="{self.encnonce}", signature="{self.signature}", '
                'nc="", type="", realm="", newauth="1"')
        return {"Authorization": auth, "User-Agent": FUS_USER_AGENT}

    def _update_nonce(self, headers) -> None:
        nonce = headers.get("NONCE")
        if nonce and nonce != self.encnonce:
            self.encnonce = nonce
            self.signature = sign_nonce(nonce)

    def _post(self, path: str, data: str = "") -> str:
        req = urllib.request.Request(FUS_URL + path, data=data.encode(), headers=self._headers(), method="POST")
        try:
            with self.opener.open(req, timeout=TIMEOUT) as r:
                self._update_nonce(r.headers)
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            self._update_nonce(e.headers)
            raise FUSError(f"{path} failed: HTTP {e.code}") from None

    def binary_inform(self, model: str, region: str, version: str) -> BinaryInfo:
        """Query the firmware package (file name, size, key material) for a version."""
        version = normalize_version(version)
        req = _fus_msg({
            "CmdID": 1,
            "ACCESS_MODE": 1,
            "BINARY_NATURE": 1,
            "REQUEST_TYPE": 2,
            "LOGIC_CHECK": logic_check(version, self.encnonce),
            "BINARY_SW_VERSION": version,
            "BINARY_LOCAL_CODE": region,
            "BINARY_MODEL_NAME": model,
        }, get=["BINARY_SW_VERSION"])
        kv = _parse_put(self._post("NF_SmartDownloadBinaryInform.do", req))
        try:
            return BinaryInfo(
                model=kv.get("BINARY_MODEL_NAME") or model,
                region=kv.get("BINARY_LOCAL_CODE") or region,
                version=kv.get("BINARY_SW_VERSION") or version,
                filename=kv["BINARY_NAME"],
                path=kv["MODEL_PATH"],
                size=int(kv["BINARY_BYTE_SIZE"]),
                model_type=kv.get("DEVICE_MODEL_TYPE", ""),
                logic_value=kv.get("LOGIC_VALUE_FACTORY") or kv.get("LOGIC_VALUE_HOME", ""),
            )
        except (KeyError, ValueError) as e:
            raise FUSError(f"Incomplete BinaryInform response: missing {e}") from e

    def init_download(self, info: BinaryInfo) -> None:
        """Ask the server to authorize a download of the given package."""
        fn = info.filename
        req = _fus_msg({
            "BINARY_NAME": fn,
            "BINARY_SW_VERSION": info.version,
            "DEVICE_LOCAL_CODE": info.region,
            "DEVICE_MODEL_TYPE": info.model_type,
            "LOGIC_CHECK": logic_check(fn[max(len(fn) - 25, 0):len(fn) - 9], self.encnonce),
        })
        _parse_put(self._post("NF_SmartDownloadBinaryInitForMass.do", req))

    def open_download(self, info: BinaryInfo, start: int = 0) -> http.client.HTTPResponse:
        """Open a streaming download of the package starting at byte `start` (use as a context manager)."""
        for attempt in range(2):
            headers = self._headers()
            if start > 0:
                headers["Range"] = f"bytes={start}-"
            # The server wants the path unescaped, so the query is built by hand
            req = urllib.request.Request(
                f"{CLOUD_URL}NF_SmartDownloadBinaryForMass.do?file={info.path}{info.filename}", headers=headers)
            try:
                return self.opener.open(req, timeout=TIMEOUT)
            except urllib.error.HTTPError as e:
                if e.code != 401 or attempt:
                    raise FUSError(f"Download failed: HTTP {e.code}") from None
            # Token expired: refresh nonce and re-authorize once
            self._post("NF_SmartDownloadGenerateNonce.do")
            self.init_download(info)
        raise FUSError("Download authorization failed")
