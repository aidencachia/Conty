"""Discover the local IPv4 address selected by the host's routing table."""

import ipaddress
import socket


def detect_host_ip() -> str:
    # UDP connect selects a local interface without sending a packet, performing
    # DNS resolution or depending on an external service being reachable.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(("192.0.2.1", 9))
            address = ipaddress.IPv4Address(probe.getsockname()[0])
            if not (address.is_loopback or address.is_unspecified or address.is_multicast):
                return str(address)
    except (OSError, ValueError):
        pass
    # No usable IPv4 route: stay usable locally without binding every interface.
    return "127.0.0.1"
