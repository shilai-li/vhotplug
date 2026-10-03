from pathlib import Path

import pytest

from vhotplug.config import Config
from vhotplug.usb import USBInfo

CONFIG_PATH = Path(__file__).resolve().parent / "fixtures" / "windows_usb_ports.json"


def test_port_rules_fixture_is_available_without_local_config() -> None:
    fixture = Path(__file__).resolve().parent / "fixtures" / "windows_usb_ports.json"
    assert fixture.is_file()


def test_ch340_under_designated_hub_is_forwarded() -> None:
    config = Config(str(CONFIG_PATH))
    dev = USBInfo(
        vid="1a86", pid="7523", busnum=3, ports=[5, 1],
        device_class=0, interfaces=":ff0102:",
    )
    result = config.vm_for_device(dev)
    assert result is not None
    assert result.target_vm == "windows"


@pytest.mark.parametrize(("bus", "ports"), [(3, [5]), (3, [5, 2]), (4, [1]), (4, [1, 3])])
@pytest.mark.parametrize(("device_class", "interfaces"), [(9, ":090000:"), (0, ":090000:")])
def test_hub_itself_stays_on_linux(bus: int, ports: list[int], device_class: int, interfaces: str) -> None:
    config = Config(str(CONFIG_PATH))
    dev = USBInfo(busnum=bus, ports=ports, device_class=device_class, interfaces=interfaces)
    assert config.vm_for_device(dev) is None


@pytest.mark.parametrize(("bus", "ports"), [(3, [5]), (3, [5, 1]), (3, [5, 2, 3]), (4, [1]), (4, [1, 2])])
def test_only_designated_socket_descendants_are_forwarded(bus: int, ports: list[int]) -> None:
    config = Config(str(CONFIG_PATH))
    dev = USBInfo(busnum=bus, ports=ports, device_class=0, interfaces=":ff0000:")
    result = config.vm_for_device(dev)
    assert result is not None
    assert result.target_vm == "windows"


@pytest.mark.parametrize(("bus", "ports"), [(2, [1]), (3, [4]), (3, [6]), (3, [7]), (4, [2]), (3, [1, 5])])
def test_other_sockets_stay_on_linux(bus: int, ports: list[int]) -> None:
    config = Config(str(CONFIG_PATH))
    # Same VID/PID as the CH340 must not override the socket restriction.
    dev = USBInfo(vid="1a86", pid="7523", busnum=bus, ports=ports, device_class=0, interfaces=":ff0102:")
    assert config.vm_for_device(dev) is None
