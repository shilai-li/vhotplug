import asyncio
from typing import Any
from unittest.mock import Mock

import pytest

from vhotplug.appcontext import AppContext
from vhotplug.qemulink import QEMULink
from vhotplug.usb import USBInfo
from vhotplug.vmm import vmm_add_device


def capture_qmp(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    commands: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self: QEMULink, cmd: str, args: dict[str, Any] | None = None, retry: bool = True
    ) -> dict[str, Any] | str:
        commands.append((cmd, args or {}))
        return "" if cmd == "human-monitor-command" else {}

    monkeypatch.setattr(QEMULink, "_execute", execute)
    monkeypatch.setattr(QEMULink, "_wait_for_boot", lambda self: True)
    return commands


def test_vm_hostdevice_option_uses_current_node(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = capture_qmp(monkeypatch)
    vm = {"type": "qemu", "socket": "/qmp/test.sock", "usbHostDevice": True}
    dev = USBInfo(device_node="/dev/bus/usb/004/048", busnum=4, devnum=48)

    asyncio.run(vmm_add_device(Mock(spec=AppContext), vm, dev))

    assert commands[-1] == (
        "device_add",
        {"driver": "usb-host", "hostdevice": "/dev/bus/usb/004/048", "id": "usb448"},
    )


@pytest.mark.parametrize("node", [None, "", "/dev/mem", "/dev/bus/usb/../048"])
def test_hostdevice_rejects_invalid_node(monkeypatch: pytest.MonkeyPatch, node: str | None) -> None:
    commands = capture_qmp(monkeypatch)
    dev = USBInfo(device_node=node, busnum=4, devnum=48)

    with pytest.raises(RuntimeError, match="USB device node"):
        asyncio.run(QEMULink("/qmp/test.sock").add_usb_device(dev, use_hostdevice=True))

    assert all(cmd != "device_add" for cmd, _ in commands)


@pytest.mark.parametrize("option", [None, False, "true", "false"])
def test_bus_address_mode_is_unchanged_by_default(monkeypatch: pytest.MonkeyPatch, option: Any) -> None:
    commands = capture_qmp(monkeypatch)
    vm = {"type": "qemu", "socket": "/qmp/test.sock"}
    if option is not None:
        vm["usbHostDevice"] = option
    dev = USBInfo(device_node="/dev/bus/usb/004/048", busnum=4, devnum=48)

    asyncio.run(vmm_add_device(Mock(spec=AppContext), vm, dev))

    assert commands[-1] == (
        "device_add", {"driver": "usb-host", "hostbus": 4, "hostaddr": 48, "id": "usb448"}
    )


def test_reconnect_uses_new_node_and_removes_old_id(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = capture_qmp(monkeypatch)
    old_dev = USBInfo(device_node="/dev/bus/usb/004/048", busnum=4, devnum=48)
    new_dev = USBInfo(device_node="/dev/bus/usb/004/049", busnum=4, devnum=49)

    async def reconnect() -> None:
        link = QEMULink("/qmp/test.sock")
        await link.add_usb_device(old_dev, use_hostdevice=True)
        await link.remove_usb_device(old_dev)
        await link.add_usb_device(new_dev, use_hostdevice=True)

    asyncio.run(reconnect())

    changes = [(cmd, args) for cmd, args in commands if cmd in {"device_add", "device_del"}]
    assert changes == [
        ("device_add", {"driver": "usb-host", "hostdevice": old_dev.device_node, "id": "usb448"}),
        ("device_del", {"id": "usb448"}),
        ("device_add", {"driver": "usb-host", "hostdevice": new_dev.device_node, "id": "usb449"}),
    ]
