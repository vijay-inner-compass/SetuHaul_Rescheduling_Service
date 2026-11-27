"""Extract the real client address behind Caddy, trusting X-Forwarded-For only from configured proxies."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from fastapi import Request

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class TrustedProxies:
    """Networks and exact peer names (e.g. 'testclient' in tests) whose X-Forwarded-For header is trusted."""

    networks: tuple[IPNetwork, ...] = ()
    names: frozenset[str] = frozenset()

    def contains(self, host: str) -> bool:
        if host in self.names:
            return True
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(addr in net for net in self.networks)

    def __bool__(self) -> bool:
        return bool(self.networks or self.names)


def parse_trusted_proxies(raw: str) -> TrustedProxies:
    """Parse a comma-separated list of IPs, CIDRs (e.g. '127.0.0.1,172.16.0.0/12') or peer names."""
    nets: list[IPNetwork] = []
    names: set[str] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            # Allow symbolic peer names (such as Starlette TestClient's 'testclient') without crashing.
            names.add(token)
    return TrustedProxies(networks=tuple(nets), names=frozenset(names))


def client_ip(request: Request, trusted: TrustedProxies | tuple[object, ...] = ()) -> str | None:
    """Return the client IP. Ignores X-Forwarded-For unless the direct peer is in ``trusted``."""
    peer = request.client.host if request.client else None
    if peer is None:
        return None
    proxies = trusted if isinstance(trusted, TrustedProxies) else TrustedProxies()
    if not proxies or not proxies.contains(peer):
        return peer
    xff = request.headers.get("x-forwarded-for", "")
    if not xff:
        return peer
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    # Walk right-to-left: Caddy appends the real connecting IP at the right, so the rightmost untrusted hop is the client.
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            continue
        if not proxies.contains(hop):
            return hop
    return peer
