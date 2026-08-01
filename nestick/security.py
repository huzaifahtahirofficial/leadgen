"""Security hardening: SSRF guards, spreadsheet-injection escaping, safe paths.

Everything a scraper touches is attacker-controlled — the pages it fetches, the
links it follows, and the text it writes into your spreadsheet. These helpers
sit on those three boundaries.
"""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

# --------------------------------------------------------------------------- #
# 1. SSRF — never let a scraped link reach the internal network
# --------------------------------------------------------------------------- #

#: Cloud instance-metadata endpoints. Reaching these leaks credentials.
METADATA_HOSTS: frozenset[str] = frozenset({
    "169.254.169.254",          # AWS / Azure / DigitalOcean / OpenStack
    "metadata.google.internal", # GCP
    "metadata.goog",
    "100.100.100.100",          # Alibaba Cloud
    "192.0.0.192",              # Oracle Cloud
    "fd00:ec2::254",            # AWS IPv6
})

#: Hostnames that always resolve to the local machine.
LOCAL_NAMES: frozenset[str] = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "127.0.0.1", "0.0.0.0", "::1", "[::1]",
})

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Ports that are never web content — probing them is a port scan.
BLOCKED_PORTS: frozenset[int] = frozenset({
    22, 23, 25, 445, 587, 993, 995,        # ssh/telnet/smtp/smb/imap
    1433, 1521, 3306, 5432, 6379, 9200,    # databases & search
    2375, 2376, 5984, 7001, 8020, 9000,    # docker, couch, hadoop
    11211, 27017, 50070,                   # memcached, mongo, hdfs
})


class BlockedTarget(ValueError):
    """The URL points somewhere we refuse to fetch."""


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    """True for loopback, RFC1918, link-local, CGNAT and other non-public space."""
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    # Carrier-grade NAT 100.64.0.0/10 is not flagged private by ipaddress.
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) hides a loopback address.
    if ip.version == 6:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            return _is_private_ip(mapped)
    return False


def check_url(url: str, *, allow_private: bool = False,
              resolve: bool = False) -> None:
    """Raise :class:`BlockedTarget` if ``url`` must not be fetched.

    ``resolve=True`` also performs a DNS lookup, which catches a public
    hostname that deliberately points at ``127.0.0.1`` (DNS rebinding).
    """
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise BlockedTarget(f"malformed URL: {exc}") from exc

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedTarget(f"scheme {parts.scheme!r} not allowed")

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise BlockedTarget("URL has no host")

    try:
        port = parts.port
    except ValueError as exc:
        raise BlockedTarget(f"invalid port: {exc}") from exc
    if port is not None and port in BLOCKED_PORTS:
        raise BlockedTarget(f"port {port} is not a web port")

    if host in METADATA_HOSTS:
        raise BlockedTarget(f"{host} is a cloud metadata endpoint")

    if allow_private:
        return

    if host in LOCAL_NAMES or host.endswith((".localhost", ".local", ".internal",
                                             ".localdomain")):
        raise BlockedTarget(f"{host} resolves to the local machine")

    # Literal IP in the URL.
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        if str(ip) in METADATA_HOSTS or _is_private_ip(ip):
            raise BlockedTarget(f"{host} is a private or reserved address")
        return

    if resolve:
        for addr in resolve_all(host):
            if str(addr) in METADATA_HOSTS or _is_private_ip(addr):
                raise BlockedTarget(
                    f"{host} resolves to internal address {addr}")


#: Resolution is ~1.4 ms per call, and a crawl hits the same host repeatedly,
#: so results are memoised. Bounded to keep memory flat on large runs.
_DNS_CACHE: dict[str, list] = {}
_DNS_CACHE_MAX = 4096


def resolve_all(host: str) -> list[ipaddress._BaseAddress]:
    """Every A/AAAA record for ``host`` (empty list if it cannot be resolved)."""
    cached = _DNS_CACHE.get(host)
    if cached is not None:
        return cached
    out: list[ipaddress._BaseAddress] = []
    try:
        for family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
            host, None, proto=socket.IPPROTO_TCP
        ):
            try:
                out.append(ipaddress.ip_address(sockaddr[0]))
            except ValueError:
                continue
    except (socket.gaierror, UnicodeError, OSError):
        _DNS_CACHE[host] = []
        return []
    if len(_DNS_CACHE) >= _DNS_CACHE_MAX:
        _DNS_CACHE.clear()
    _DNS_CACHE[host] = out
    return out


def is_safe_url(url: str, *, allow_private: bool = False,
                resolve: bool = False) -> bool:
    try:
        check_url(url, allow_private=allow_private, resolve=resolve)
        return True
    except BlockedTarget:
        return False


# --------------------------------------------------------------------------- #
# 2. Spreadsheet formula injection
# --------------------------------------------------------------------------- #

#: Excel / LibreOffice / Sheets treat a leading one of these as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitise_cell(value: object) -> object:
    """Neutralise CSV/Excel formula injection while keeping the text readable.

    ``=cmd|'/c calc'!A1`` becomes ``'=cmd|'/c calc'!A1`` — a leading apostrophe
    tells every major spreadsheet to treat the cell as literal text. Numbers and
    non-strings pass through untouched so sorting still works.
    """
    if not isinstance(value, str) or not value:
        return value
    if value[0] in _FORMULA_PREFIXES:
        # A plain negative number is legitimate data, not a formula.
        if value[0] == "-":
            try:
                float(value)
                return value
            except ValueError:
                pass
        return "'" + value
    return value


def sanitise_row(row: dict[str, object]) -> dict[str, object]:
    return {k: sanitise_cell(v) for k, v in row.items()}


# --------------------------------------------------------------------------- #
# 3. Output paths
# --------------------------------------------------------------------------- #
def safe_output_path(path: str | Path, base: str | Path | None = None) -> Path:
    """Resolve ``path``, refusing to escape ``base`` when one is given."""
    p = Path(path).expanduser()
    if base is None:
        return p
    root = Path(base).expanduser().resolve()
    resolved = (root / p).resolve() if not p.is_absolute() else p.resolve()
    if not resolved.is_relative_to(root):
        raise BlockedTarget(f"output path {path!r} escapes {root}")
    return resolved


def filter_safe(urls: Iterable[str], *, allow_private: bool = False) -> list[str]:
    return [u for u in urls if is_safe_url(u, allow_private=allow_private)]
