"""Enumerate this machine's IPv4 addresses and broadcast addresses using only the stdlib.

Discovery must reach every interface (Wi-Fi, Ethernet, VM bridges), not just the default route, and
``255.255.255.255`` alone is often only sent out of one interface.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass

GLOBAL_BROADCAST = "255.255.255.255"
# Reaches sockets on this machine even with no network at all, as when a demo laptop hosts itself.
LOOPBACK_BROADCAST = "127.255.255.255"


@dataclass(frozen=True)
class Interface:
    address: str
    broadcast: str


def _linux_interfaces() -> list[Interface]:
    import fcntl  # POSIX only, hence the local import

    siocgifaddr, siocgifbrdaddr = 0x8915, 0x8919
    found: list[Interface] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in socket.if_nameindex():
            packed = struct.pack("256s", name.encode()[:15])
            try:
                addr = fcntl.ioctl(probe.fileno(), siocgifaddr, packed)[20:24]
                brd = fcntl.ioctl(probe.fileno(), siocgifbrdaddr, packed)[20:24]
            except OSError:
                continue  # interface is down or has no IPv4 address
            address = socket.inet_ntoa(addr)
            if not address.startswith("127."):
                found.append(Interface(address, socket.inet_ntoa(brd)))
    finally:
        probe.close()
    return found


def _generic_interfaces() -> list[Interface]:
    """Fallback for Windows and macOS. The stdlib has no netmask API there, so assume a /24."""
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(str(info[4][0]))
    except OSError:
        pass
    # Connecting a UDP socket sends nothing; it only makes the OS pick the outbound address.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return [
        Interface(a, a.rsplit(".", 1)[0] + ".255")
        for a in sorted(addresses)
        if not a.startswith("127.")
    ]


def list_interfaces() -> list[Interface]:
    try:
        found = _linux_interfaces()
    except (ImportError, OSError, AttributeError):
        found = []
    return found or _generic_interfaces()


def broadcast_targets() -> list[str]:
    targets = {iface.broadcast for iface in list_interfaces()}
    targets.update((GLOBAL_BROADCAST, LOOPBACK_BROADCAST))
    return sorted(targets)


def open_discovery_socket(port: int, host: str = "") -> socket.socket:
    """Bind the shared discovery port. Options go before bind, or the OS ignores them.

    SO_REUSEADDR lets a server and clients on one host share the port. Broadcast datagrams are
    delivered to every such socket, so a same-host server and client both see each other.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind((host, port))
    sock.setblocking(False)
    return sock
