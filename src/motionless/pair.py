"""Connect a phone to the daemon, over a USB cable or over Wi-Fi.

Both paths end in the same place: the phone's browser opens a page served by
:class:`~motionless.sources.phone.PhoneSource`. What differs is how the phone
reaches the laptop, and that difference is forced by one browser rule —
``DeviceMotionEvent`` only fires in a **secure context**.

USB (Android)
    ``adb reverse tcp:5577 tcp:5577`` makes the laptop's port appear on the
    phone's *own* ``localhost``. Browsers treat ``localhost`` as a trustworthy
    origin, so this needs no certificate, no Wi-Fi, and no app. It does need
    USB debugging turned on once.

Wi-Fi (iPhone, or Android without USB debugging)
    The phone connects to the laptop's LAN address, which is not a trustworthy
    origin, so the page must be served over HTTPS. There is no public
    certificate authority that will vouch for ``192.168.x.x``, so we generate a
    self-signed certificate and the phone's owner accepts it once.

There is no USB path for iPhone. ``usbmuxd`` forwards host-to-device only:
tools like ``iproxy`` let the laptop reach a port on the phone, not the other
way round, and nothing in iOS forwards a phone-side port to the host without a
jailbreak. Wi-Fi is the iPhone path.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from motionless.paths import ensure_dir, state_dir

#: Length of the generated pairing token, in URL-safe characters.
TOKEN_CHARS = 10

#: Certificate lifetime. 825 days is the longest Apple's platforms accept for
#: a manually trusted leaf certificate; longer ones are rejected outright.
CERT_DAYS = 825

#: Regenerate a certificate this long before it expires, so a pairing never
#: fails on the roadside for want of a fresh one.
CERT_RENEW_SECONDS = 30 * 24 * 3600

_ADB_FALLBACKS = (
    Path.home() / "Android/Sdk/platform-tools/adb",
    Path("/usr/lib/android-sdk/platform-tools/adb"),
)


class PairError(RuntimeError):
    """Raised when pairing cannot be completed, with a human-readable reason."""


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_CHARS)[:TOKEN_CHARS]


# ----------------------------------------------------------------- USB (adb)


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str

    @property
    def ready(self) -> bool:
        return self.state == "device"


def find_adb() -> str | None:
    """Locate ``adb``, including the usual SDK spot when it is not on PATH."""
    found = shutil.which("adb")
    if found:
        return found
    for candidate in _ADB_FALLBACKS:
        if candidate.is_file():
            return str(candidate)
    return None


def parse_devices(output: str) -> list[AdbDevice]:
    """Parse ``adb devices`` output, ignoring its header and blank lines."""
    devices = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("List of devices") or stripped.startswith("*"):
            continue
        parts = re.split(r"\s+", stripped)
        if len(parts) >= 2:
            devices.append(AdbDevice(parts[0], parts[1]))
    return devices


def _adb(adb: str, *args: str, timeout: float = 15.0) -> str:
    try:
        result = subprocess.run(
            [adb, *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except OSError as exc:
        raise PairError(f"could not run {adb}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PairError(f"`adb {' '.join(args)}` timed out") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise PairError(f"`adb {' '.join(args)}` failed: {detail}")
    return result.stdout


def adb_devices(adb: str) -> list[AdbDevice]:
    return parse_devices(_adb(adb, "devices"))


def adb_reverse(adb: str, port: int, *, serial: str = "") -> None:
    """Point the phone's ``localhost:port`` at our ``localhost:port``."""
    target = ["-s", serial] if serial else []
    _adb(adb, *target, "reverse", f"tcp:{port}", f"tcp:{port}")


# ------------------------------------------------------------ Wi-Fi (LAN/TLS)


def primary_address() -> str | None:
    """The address this machine would use to reach the local network.

    Asks the routing table by opening an unconnected UDP socket towards a
    documentation address: no packet is sent, but the kernel picks the source
    address it would use, which is the one a phone on the same network can
    reach. More reliable than resolving the hostname, which on a typical
    Ubuntu box answers ``127.0.1.1``.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed anywhere
        address = str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") else address


def local_addresses() -> list[str]:
    """Every plausible LAN address, best guess first."""
    found: list[str] = []
    primary = primary_address()
    if primary:
        found.append(primary)
    if shutil.which("ip"):
        try:
            output = subprocess.run(
                ["ip", "-o", "-4", "addr", "show", "scope", "global"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - ip is odd here
            output = ""
        for match in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/", output):
            address = match.group(1)
            if address not in found and not address.startswith(("127.", "169.254.")):
                found.append(address)
    return found


def tls_dir() -> Path:
    return state_dir() / "tls"


def _sidecar(directory: Path) -> Path:
    return directory / "cert.json"


def certificate_is_usable(directory: Path, addresses: list[str]) -> bool:
    """Whether the stored certificate still covers ``addresses`` and is fresh."""
    cert, key, sidecar = directory / "cert.pem", directory / "key.pem", _sidecar(directory)
    if not (cert.exists() and key.exists() and sidecar.exists()):
        return False
    try:
        record = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not set(addresses) <= set(record.get("addresses", [])):
        return False
    expires = float(record.get("expires", 0.0))
    return expires - time.time() > CERT_RENEW_SECONDS


def ensure_certificate(addresses: list[str], *, directory: Path | None = None) -> tuple[Path, Path]:
    """Return a certificate and key covering ``addresses``, generating if needed.

    Self-signed, because no certificate authority will vouch for a private
    address. The phone's owner accepts it once; the alternative — an installed
    trust profile — is more setup than the thing is worth for a laptop overlay.
    """
    target = ensure_dir(directory or tls_dir())
    cert, key = target / "cert.pem", target / "key.pem"
    if certificate_is_usable(target, addresses):
        return cert, key
    if not shutil.which("openssl"):
        raise PairError(
            "openssl is needed to generate a certificate for the Wi-Fi path; "
            "install it (`sudo apt install openssl`), or pair over USB instead"
        )
    names = ",".join([*(f"IP:{address}" for address in addresses), "DNS:localhost", "IP:127.0.0.1"])
    command = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
        "-days", str(CERT_DAYS), "-nodes",
        "-keyout", str(key), "-out", str(cert),
        "-subj", "/CN=motionless",
        "-addext", f"subjectAltName={names}",
        "-addext", "basicConstraints=critical,CA:FALSE",
        "-addext", "keyUsage=critical,digitalSignature,keyEncipherment",
        "-addext", "extendedKeyUsage=serverAuth",
    ]  # fmt: skip
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not cert.exists():
        raise PairError(f"openssl could not create a certificate: {result.stderr.strip()}")
    key.chmod(0o600)
    _sidecar(target).write_text(
        json.dumps({"addresses": addresses, "expires": time.time() + CERT_DAYS * 24 * 3600}),
        encoding="utf-8",
    )
    return cert, key


# ------------------------------------------------------------------ niceties


def qr_code(text: str) -> str | None:
    """A terminal QR code for ``text``, or ``None`` if ``qrencode`` is absent.

    Typing ``https://192.168.1.37:5577/?t=Xk3p9Qz2Lm`` on a phone keyboard in a
    moving car is exactly the kind of thing a QR code exists to avoid.
    """
    if not shutil.which("qrencode"):
        return None
    try:
        result = subprocess.run(
            ["qrencode", "-t", "UTF8", "-m", "1", text],
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
        return None
    return result.stdout if result.returncode == 0 and result.stdout.strip() else None
