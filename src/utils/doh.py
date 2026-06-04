"""DNS-over-HTTPS resolver for environments where Binance/etc. are blocked.

Many ISPs (notably Telkomsel in Indonesia) intercept DNS for
crypto exchanges and return a redirect page. Browsers can use
DoH directly, but Python's `socket.getaddrinfo` uses the OS
resolver — and gets the same redirect.

This module patches `socket.getaddrinfo` so that lookups for
hostnames in BLOCKED_HOSTS go through Cloudflare's or Google's
public DoH endpoint instead. The patch is process-local and
idempotent; safe to import multiple times.

Usage:
    from src.utils.doh import install_doh_resolver
    install_doh_resolver("cloudflare")  # or "google", or None to uninstall
"""

from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any
from urllib.request import Request, urlopen

from .logging import get_logger

logger = get_logger(__name__)


BLOCKED_HOSTS = frozenset({
    # Binance
    "api.binance.com",
    "fapi.binance.com",
    "dapi.binance.com",
    "testnet.binance.vision",
    # Bybit
    "api.bybit.com",
    "api.bytick.com",
    # OKX
    "www.okx.com",
    # Gate
    "api.gate.io",
    # Coinbase
    "api.coinbase.com",
    "api.exchange.coinbase.com",
    # Kraken
    "api.kraken.com",
})


DOH_ENDPOINTS = {
    "cloudflare": "https://1.1.1.1/dns-query",
    "google": "https://8.8.8.8/dns-query",
}


_DOH_CACHE: dict[str, str] = {}
_DOH_CACHE_LOCK = threading.Lock()
_PATCH_INSTALLED = False
_ORIGINAL_GETADDRINFO: Any = None


def _doh_resolve(hostname: str, provider: str) -> str | None:
    """Resolve hostname via DoH. Returns IPv4 string or None."""
    if provider not in DOH_ENDPOINTS:
        return None
    url = DOH_ENDPOINTS[provider]
    # Wire-format DNS query: simple GET with ?dns=base64url(message)
    import base64
    # Build a minimal DNS message for A record of `hostname`.
    # Header (12 bytes): ID=0x1234, FLAGS=0x0100 (RD), QDCOUNT=1, others=0
    # Question: QNAME + QTYPE=A + QCLASS=IN
    qname = b"".join(bytes([len(p)]) + p.encode() for p in hostname.split(".")) + b"\x00"
    header = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    question = qname + b"\x00\x01" + b"\x00\x01"
    message = header + question
    payload = base64.urlsafe_b64encode(message).rstrip(b"=").decode()
    full_url = f"{url}?dns={payload}"
    headers = {"Accept": "application/dns-message"}
    try:
        req = Request(full_url, headers=headers)
        with urlopen(req, timeout=5) as r:
            raw = r.read()
    except Exception as exc:
        logger.debug("DoH request failed", url=full_url, error=str(exc))
        return None
    # Parse: skip header (12 bytes), find answer section.
    # Very small parser — just enough for an A record with one answer.
    try:
        if len(raw) < 12:
            return None
        # Skip DNS header (12 bytes) and question section
        # (qname + qtype + qclass = len(hostname) + 2 + 4 + 2)
        i = 12
        # Skip QNAME
        while i < len(raw):
            ln = raw[i]
            if ln == 0:
                i += 1
                break
            if (ln & 0xC0) == 0xC0:  # compression pointer
                i += 2
                break
            i += 1 + ln
        i += 4  # QTYPE + QCLASS
        # Read answer count
        ancount = int.from_bytes(raw[6:8], "big")
        for _ in range(ancount):
            if i >= len(raw):
                break
            # NAME
            if i >= len(raw):
                break
            if (raw[i] & 0xC0) == 0xC0:
                i += 2
            else:
                while i < len(raw):
                    ln = raw[i]
                    if ln == 0:
                        i += 1
                        break
                    if (ln & 0xC0) == 0xC0:
                        i += 2
                        break
                    i += 1 + ln
            # Need at least 2 (TYPE) + 2 (CLASS) + 4 (TTL) + 2 (RDLEN) = 10 bytes
            if i + 10 > len(raw):
                break
            atype = int.from_bytes(raw[i:i+2], "big")
            i += 2 + 2 + 4  # TYPE + CLASS + TTL
            rdlen = int.from_bytes(raw[i:i+2], "big")
            i += 2
            if i + rdlen > len(raw):
                break
            if atype == 1 and rdlen == 4:  # A record
                ip = ".".join(str(b) for b in raw[i:i+4])
                return ip
            i += rdlen
    except Exception as exc:
        logger.debug("DoH parse failed", error=str(exc))
    return None


def _patched_getaddrinfo(host, *args, **kwargs):
    """Patched getaddrinfo: route blocked hosts through DoH."""
    # The `host` argument can be a hostname string or None
    if isinstance(host, str) and host.lower() in BLOCKED_HOSTS:
        provider = os.environ.get("HL_DOH_PROVIDER", "cloudflare")
        cache_key = f"{provider}:{host.lower()}"
        with _DOH_CACHE_LOCK:
            ip = _DOH_CACHE.get(cache_key)
        if not ip:
            logger.info("DoH resolving blocked hostname", host=host, provider=provider)
            ip = _doh_resolve(host, provider)
            if ip:
                with _DOH_CACHE_LOCK:
                    _DOH_CACHE[cache_key] = ip
        if ip:
            # Substitute the IP for the getaddrinfo call. aiohttp /
            # urllib will use it directly. The SNI / Host header
            # still goes to the original hostname (preserved in the
            # URL), so TLS verification works as normal.
            host = ip
    if _ORIGINAL_GETADDRINFO is None:
        return socket.getaddrinfo(host, *args, **kwargs)
    return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)


def install_doh_resolver(provider: str = "cloudflare") -> bool:
    """Install a DoH-based getaddrinfo patch for blocked crypto hostnames.

    Returns True if the patch was newly installed, False if already
    installed (idempotent) or the provider is invalid.
    """
    global _PATCH_INSTALLED, _ORIGINAL_GETADDRINFO
    if provider is None:
        uninstall_doh_resolver()
        return False
    if provider not in DOH_ENDPOINTS:
        logger.warning("Unknown DoH provider, skipping install", provider=provider)
        return False
    os.environ["HL_DOH_PROVIDER"] = provider
    if _PATCH_INSTALLED:
        logger.info("DoH resolver already installed", provider=provider)
        return False
    _ORIGINAL_GETADDRINFO = socket.getaddrinfo
    socket.getaddrinfo = _patched_getaddrinfo
    _PATCH_INSTALLED = True
    logger.info("DoH resolver installed", provider=provider, blocked=len(BLOCKED_HOSTS))
    return True


def uninstall_doh_resolver() -> bool:
    """Remove the DoH patch and restore the original getaddrinfo."""
    global _PATCH_INSTALLED, _ORIGINAL_GETADDRINFO
    if not _PATCH_INSTALLED:
        return False
    if _ORIGINAL_GETADDRINFO is not None:
        socket.getaddrinfo = _ORIGINAL_GETADDRINFO
    _ORIGINAL_GETADDRINFO = None
    _PATCH_INSTALLED = False
    logger.info("DoH resolver uninstalled")
    return True
