import socket
from unittest.mock import MagicMock

import pytest

from conty.network import detect_host_ip


@pytest.fixture
def probe(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr("conty.network.socket.socket", factory)
    return factory


def test_detects_route_address_without_sending_packets(probe):
    connection = probe.return_value.__enter__.return_value
    connection.getsockname.return_value = ("192.168.20.15", 54321)
    assert detect_host_ip() == "192.168.20.15"
    probe.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
    connection.settimeout.assert_called_once_with(0.2)
    connection.connect.assert_called_once_with(("192.0.2.1", 9))
    connection.send.assert_not_called()
    connection.sendto.assert_not_called()
    probe.return_value.__exit__.assert_called_once()


@pytest.mark.parametrize("stage", ["socket", "connect", "getsockname"])
def test_network_errors_fall_back_to_loopback(probe, stage):
    if stage == "socket":
        probe.side_effect = OSError("No network")
    else:
        getattr(probe.return_value.__enter__.return_value, stage).side_effect = OSError(
            "No network"
        )
    assert detect_host_ip() == "127.0.0.1"


@pytest.mark.parametrize("address", ["127.0.0.1", "0.0.0.0", "224.0.0.1", "invalid", "::1"])
def test_unusable_route_addresses_fall_back_to_loopback(probe, address):
    probe.return_value.__enter__.return_value.getsockname.return_value = (address, 12345)
    assert detect_host_ip() == "127.0.0.1"
