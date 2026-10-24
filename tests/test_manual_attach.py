import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from vhotplug import device as device_module
from vhotplug import vhotplug as daemon_module
from vhotplug.config import Config, PassthroughInfo
from vhotplug.devicestate import DeviceState
from vhotplug.qemulink import QEMULink
from vhotplug.usb import USBInfo

WINDOWS_PORT = {"bus": 4, "port": 1}


def write_config(tmp_path: Path, rule_extra: dict[str, Any]) -> Config:
    path = tmp_path / "config.json"
    rule = {"description": "Windows socket", "targetVm": "windows", "tag": "windows-usb", "allow": [WINDOWS_PORT]}
    path.write_text(json.dumps({"usbPassthrough": [rule | rule_extra], "vms": [{"name": "windows"}]}))
    return Config(str(path))


@pytest.mark.parametrize(
    ("rule_extra", "expected"),
    [({}, True), ({"autoAttach": True}, True), ({"autoAttach": False}, False), ({"autoAttach": "false"}, False)],
)
def test_auto_attach_rule_option(tmp_path: Path, rule_extra: dict[str, Any], expected: bool) -> None:
    res = write_config(tmp_path, rule_extra).vm_for_device(USBInfo(busnum=4, ports=[1, 2]))
    assert res is not None and res.auto_attach is expected


def usb_device(node: str) -> USBInfo:
    return USBInfo(device_node=node, busnum=4, ports=[1], vid="1234", pid="0001")


def app_with_devices(monkeypatch: pytest.MonkeyPatch, devices: list[USBInfo], res: PassthroughInfo) -> Mock:
    app = Mock()
    app.udev_context.list_devices.return_value = devices
    app.dev_state = DeviceState()
    monkeypatch.setattr(device_module, "is_usb_device", lambda _dev: True)
    monkeypatch.setattr(device_module, "get_usb_info", lambda dev: dev)
    monkeypatch.setattr(device_module, "get_drivers_from_modaliases", lambda *_args: [])
    monkeypatch.setattr(device_module, "log_device", lambda _dev: None)
    monkeypatch.setattr(device_module, "find_vm_for_device", lambda *_args: res)
    return app


MANUAL = PassthroughInfo("windows", None, tag="windows-usb", auto_attach=False)


def test_startup_and_vm_restart_skip_manual_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    app = app_with_devices(monkeypatch, [usb_device("/dev/bus/usb/004/005")], MANUAL)
    attach = AsyncMock()
    monkeypatch.setattr(device_module, "attach_device", attach)

    asyncio.run(device_module.attach_connected_usb(app))
    asyncio.run(device_module.attach_connected_usb(app, ["windows"]))

    attach.assert_not_awaited()


def test_attach_by_tag_attaches_manual_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    devices = [usb_device("/dev/bus/usb/004/005"), usb_device("/dev/bus/usb/003/007")]
    app = app_with_devices(monkeypatch, devices, MANUAL)
    attach = AsyncMock()
    monkeypatch.setattr(device_module, "attach_device", attach)

    asyncio.run(device_module.attach_connected_usb(app, tag="windows-usb"))

    assert [call.args[2] for call in attach.await_args_list] == devices


def test_attach_by_tag_reports_missing_and_failed_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    app = app_with_devices(monkeypatch, [], MANUAL)
    with pytest.raises(RuntimeError, match="No connected USB devices match tag windows-usb"):
        asyncio.run(device_module.attach_connected_usb(app, tag="windows-usb"))

    app = app_with_devices(monkeypatch, [usb_device("/dev/bus/usb/004/005")], MANUAL)
    monkeypatch.setattr(device_module, "attach_device", AsyncMock(side_effect=RuntimeError("VM is not booted")))
    with pytest.raises(RuntimeError, match="Failed to attach USB devices with tag windows-usb"):
        asyncio.run(device_module.attach_connected_usb(app, tag="windows-usb"))


def test_detach_by_tag_reports_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    dev = usb_device("/dev/bus/usb/004/005")
    app = app_with_devices(monkeypatch, [dev], MANUAL)
    app.config.vm_for_device.return_value = MANUAL
    app.dev_state.set_vm_for_device(dev, "windows")
    monkeypatch.setattr(device_module, "usb_device_by_node", lambda _app, _node: dev)
    monkeypatch.setattr(device_module, "_remove_existing_device", AsyncMock(side_effect=RuntimeError("QMP down")))

    with pytest.raises(RuntimeError, match="Failed to detach USB devices with tag windows-usb"):
        asyncio.run(device_module.detach_connected_usb(app, tag="windows-usb", permanent=True))


def test_plug_event_leaves_manual_device_on_host(monkeypatch: pytest.MonkeyPatch) -> None:
    dev = usb_device("/dev/bus/usb/004/005")
    app = Mock(api_server=None)
    app.config.usb_authorization_enabled.return_value = False
    attach = AsyncMock()
    monkeypatch.setattr(daemon_module, "is_usb_device", lambda _dev: True)
    monkeypatch.setattr(daemon_module, "get_usb_info", lambda _dev: dev)
    monkeypatch.setattr(daemon_module, "get_drivers_from_modaliases", lambda *_args: [])
    monkeypatch.setattr(daemon_module, "get_usb_device_authorization", lambda _dev: 1)
    monkeypatch.setattr(daemon_module, "log_device", lambda _dev: None)
    monkeypatch.setattr(daemon_module, "find_vm_for_device", lambda *_args: MANUAL)
    monkeypatch.setattr(daemon_module, "attach_device", attach)

    asyncio.run(daemon_module.device_event(app, Mock(action="add", device_node=dev.device_node)))

    attach.assert_not_awaited()
    app.config.usb_authorization_host_allowed.assert_not_called()


def test_vm_stop_or_restart_forgets_its_usb_devices() -> None:
    app = Mock()
    app.dev_state = DeviceState()
    app.dev_state.set_vm_for_device(usb_device("/dev/bus/usb/004/005"), "windows")
    app.dev_state.set_vm_for_device(usb_device("/dev/bus/usb/001/002"), "other")

    device_module.forget_usb_devices_of_vms(app, ["windows"])

    assert app.dev_state.list_usb_devices() == {"/dev/bus/usb/001/002": "other"}


def test_attach_to_stopped_vm_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(QEMULink, "_wait_for_boot", lambda self: False)
    with pytest.raises(RuntimeError, match="not booted"):
        asyncio.run(QEMULink("/qmp/test.sock").add_usb_device(usb_device("/dev/bus/usb/004/005")))
