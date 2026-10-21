"""Who is actually on the other end of a request — for rate limits and the audit trail.

`X-Forwarded-For` is written by whoever sends the request, so its FIRST entry is whatever an
attacker wants it to be: a limiter keyed on it hands out a fresh budget per request. The only
part that can be believed is what our own proxies appended, i.e. the chain read from the
RIGHT, and only when the socket peer is itself one of those proxies.

So: the socket peer is the answer unless it is in TRUSTED_PROXIES. If it is, take
CLIENT_IP_HEADER (when configured — e.g. CF-Connecting-IP), otherwise walk X-Forwarded-For
right to left and return the first hop that is not a trusted proxy. A malformed hop ends the
walk: everything left of it was written by somebody we cannot vouch for.

Generic by design — it does not know which proxy is in front: Cloudflare, traefik, nginx or
nothing at all. Exposed directly to the internet, the peer is a public address, is not
trusted, and every header is ignored.
"""
import ipaddress
from functools import lru_cache

from flask import request

from . import config as cfg


def _ip(value: str):
    try:
        return ipaddress.ip_address(value.strip().strip("[]"))
    except ValueError:
        return None


@lru_cache(maxsize=8)
def _networks(spec: tuple[str, ...]):
    nets = []
    for s in spec:
        try:
            nets.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def trusted(addr) -> bool:
    ip = addr if not isinstance(addr, str) else _ip(addr)
    if ip is None:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return any(ip in n for n in _networks(tuple(cfg.TRUSTED_PROXIES)) if n.version == ip.version)


def client_ip() -> str:
    remote = request.remote_addr or ""
    if not trusted(remote):
        return remote or "unknown"
    if cfg.CLIENT_IP_HEADER:
        v = request.headers.get(cfg.CLIENT_IP_HEADER, "")
        if _ip(v) is not None:
            return v.strip().strip("[]")
    hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
    for hop in reversed(hops):
        ip = _ip(hop)
        if ip is None:
            break
        if not trusted(ip):
            return str(ip)
    # No forwarded chain, or every hop is one of ours (a request from inside the network).
    return remote or "unknown"
