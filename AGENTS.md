# Agent guide: Docker Windows USB hotplug

Read this file before configuring USB passthrough from a Linux host into a Docker-hosted Windows VM. Follow the user's chosen physical socket rather than forwarding every USB device on the machine.

This guide describes the local extension of upstream `tiiuae/vhotplug`. The repository already contains the direct-node feature; a new installation normally needs configuration and deployment, not another implementation of it.

## What the extension does

- `vhotplug/qemulink.py`: `add_usb_device(..., use_hostdevice=True)` sends QMP `device_add` with `driver: usb-host` and the current udev device-node path in `hostdevice`.
- `vhotplug/vmm.py`: only the JSON boolean `usbHostDevice: true` enables that mode for a QEMU VM. The default remains `hostbus`/`hostaddr`.
- Each plug event supplies a fresh `/dev/bus/usb/BBB/DDD` node. Never cache a device address across reconnects. Missing or malformed node paths fail explicitly.
- Existing udev monitoring, allow/deny selection, and QMP `device_del` cleanup remain in use.

Direct-node mode addresses container QEMU/libusb inventories that remain stale after reconnect even though the new USB node is visible. It does not fix missing Windows drivers, unsupported QEMU properties, or competing host applications.

## Safety boundaries

1. Inspect before editing. Do not assume a container name, Compose path, image version, QMP format, or physical-port mapping.
2. Ask the user which physical socket should belong to Windows. Explain that all matching non-hub descendants of that socket can be forwarded, including devices plugged in later.
3. Do not claim the user's keyboard, mouse, network adapter, mounted storage, or boot device without explicit approval. Safely unmount storage before transferring it.
4. Never pass through the entire PCI USB controller just to forward a socket or hub. Other sockets and built-in devices can share that controller.
5. Ask before stopping ADB or another host application. Stopping host ADB also interrupts wireless ADB sessions. Do not start ADB during passthrough verification just to identify a phone.
6. Ask before persistent host changes and before recreating or restarting Windows. Confirm that guest work is saved. A host-daemon restart is different from a VM restart.
7. Use the user's approved privilege mechanism. Do not request passwords in chat or work around failed elevation.
8. Protect QMP: do not expose it on an unauthenticated TCP listener or make its host directory world-writable.
9. Do not print full Docker environments or Compose configurations; they can contain credentials. Read only the fields needed for this task and redact secrets.
10. Leave the API, persistency, USB authorization/deauthorization, PCI passthrough, and evdev passthrough disabled unless the user specifically requests them.
11. Do not commit or publish machine-specific configuration, credentials, captured logs, or backups without permission. Never run `git add -A` without reviewing the files.

## Installer workflow

Use `scripts/configure_windows_hotplug.sh` and follow [the installer guide](docs/windows_hotplug_installer.md) for deterministic deployment-file changes. The helper requires PyYAML; prepare the wheelhouse without elevation and select a root-controlled Python 3.13+ interpreter for the daemon. On Omarchy, first verify both the checkout and privileged launcher preserve YAML list indentation during security migration and explicitly clear source-directory setuid/setgid bits; the installer does not update that launcher.

Start with `--plan` and explicitly supply the approved `--port BUS:ROOTPORT` pairs. Inspect conflicts rather than weakening safeguards. `--apply --yes` approves host-file changes only; `--allow-vm-restart`, `--start-service` and `--enable-autostart` are separate choices. Review static device IDs before using `--remove-static-id`. Never add permission flags simply to clear an error.

Use `--verify` for read-only deployed/live checks and keep the real physical/guest verification requirements below. Installer sandbox tests are not evidence of a live host deployment. Do not apply the installer to an already working setup just to test the script. Use its reported backup path with `--rollback` only after reviewing the scope; restoring Compose still leaves the question of a separately approved container restart.

## 1. Discover the installation

Use read-only checks to establish:

- The Linux host and available Python version. This project requires Python 3.13 or newer.
- The target Windows container, whether it is running, and its installed image/QEMU version.
- The active Compose files from container labels, including any existing overrides.
- Existing USB bind mounts, cgroup/device permissions, QMP endpoints, and static `usb-host` selectors.
- Whether a host vhotplug process/service is already running and which installed package and configuration it actually loads.

Useful commands, with discovered identifiers substituted:

```sh
lsusb -t
lsusb
sudo docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
sudo docker inspect CONTAINER --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
systemctl cat vhotplug_windows.service
```

A missing service is normal on a new installation. A permission denial is not evidence that no container exists. Do not launch Windows merely to inspect an image or configuration.

## 2. Map the chosen physical socket

Have the user insert a known device into one labelled socket at a time. Confirm the physical label with the user; sysfs cannot tell you which socket they intended.

Inspect `lsusb -t` and the corresponding `/sys/bus/usb/devices/BUS-PORT` path. Read its bus number, device number, speed, device class, and physical topology. Resolve its PCI ancestor when needed.

A physical USB 3 socket can have separate USB 2 and SuperSpeed branches. Map both with suitable devices or an appropriate hub before promising support for both. A USB 2 serial adapter may enumerate on a different bus/root port from a USB 3 phone in the same socket. Do not guess the companion mapping from bus numbers alone.

Record the approved mapping in the user's local configuration. Repository tests may use synthetic bus/port numbers; those are not deployment defaults.

## 3. Configure port-based selection and hubs

Start from a minimal configuration rather than the upstream `config.json`, which contains unrelated sample assignments:

```json
{
  "general": {
    "api": {"enable": false},
    "persistency": false
  },
  "usbAuthorization": {
    "enable": false,
    "deauthorizeUnmatched": false
  },
  "usbPassthrough": [
    {
      "description": "User-approved Windows-only physical socket",
      "targetVm": "windows",
      "allow": [],
      "deny": [{"deviceClass": 9}, {"interfaceClass": 9}]
    }
  ],
  "pciPassthrough": [],
  "evdevPassthrough": [],
  "vms": [
    {
      "name": "windows",
      "type": "qemu",
      "socket": "/run/vhotplug_windows/qmp.sock",
      "usbHostDevice": true
    }
  ]
}
```

The empty `allow` list intentionally selects nothing. After the user confirms the mapping, add one `{ "bus": N, "port": P }` rule per verified branch. Use integers, not placeholder strings. Coordinate the socket path with the actual Compose mount and service configuration.

A bus/root-port rule includes downstream and nested hub devices on that branch. Keep physical hubs managed by Linux and forward their non-hub children individually. Explicitly deny USB hub class 9 at both device and interface level: startup scanning skips hubs, but live plug-event handling must not rely on that alone when optional authorization is disabled.

Do not combine a port rule with a vendor/product field assuming every category is ANDed. Inspect `Config._match_usb`; different matching categories can act as alternatives. For a dedicated socket, use bus/port pairs without a broad vendor-only alternative.

Check the hub's actual negotiated speed. A hub observed at 12 Mb/s will not preserve a phone's direct SuperSpeed throughput. Device enumeration does not prove that J-Link/ST-Link debugging, flashing, or firmware-update re-enumeration works.

## 4. Connect vhotplug to container QEMU

Confirm the running QEMU supports the `usb-host.hostdevice` property using QMP `device-list-properties` with `typename: usb-host`. Do not assume support from a version number alone. If it is missing, report the incompatibility instead of silently falling back to the stale lookup path.

The container needs visibility of newly created USB nodes and permission to open them. For Docker Compose, the relevant settings commonly include:

```yaml
services:
  windows:  # Replace with the discovered service name.
    environment:
      QMP: "unix:/qmp/qmp.sock,server=on,wait=off"
    volumes:
      - /dev/bus/usb:/dev/bus/usb
      - /run/vhotplug_windows:/qmp
    device_cgroup_rules:
      - "c 189:* rwm"
```

This is a merge fragment, not a complete replacement Compose file. Preserve storage, credentials, existing arguments, devices, and unrelated mounts. Check the installed image launcher: some versions pass `QMP` verbatim, while others may expect a different format. Reuse an existing safe QMP endpoint when possible; do not add a conflicting second one. An HMP monitor socket is not a QMP socket.

Back up the actual Compose files before editing. With approval, remove only static `usb-host` selectors that overlap the devices this daemon will manage. Do not remove unrelated device assignments.

Validate the merged configuration with `docker compose ... config -q`. Create the chosen host QMP directory with restricted ownership/permissions before starting or recreating the container. `/run` is ephemeral, so define how that directory is recreated at boot. Apply container recreation only after explicit approval; later device reconnects should not require restarting Windows.

Verify the live container, not just the saved YAML: new USB nodes must be visible inside it, QMP must answer, and the guest must remain running.

## 5. Install the host daemon

For development, from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install . pytest ruff
.venv/bin/python -m vhotplug --help
```

Select a Python 3.13+ interpreter first. Do not upgrade a working system interpreter or install system packages without permission.

For a persistent privileged service:

- Install the reviewed package and its environment in a root-controlled location such as `/opt/vhotplug_windows`; do not permanently run root code from a user-writable checkout.
- Install the reviewed local configuration under `/etc/vhotplug` with root-controlled permissions.
- Launch the installed interpreter with `-m vhotplug --attach-connected --config CONFIG_PATH`.
- Use a root-controlled systemd unit if the host supports systemd. Coordinate `RuntimeDirectory`, its permissions and lifetime, the Compose mount, and the QMP path. If the service creates the directory, account for startup ordering and preserve it while QEMU still uses it.
- A service that may start before the VM must watch for its QMP socket and attach matching connected devices when it becomes available. Inspect the existing file-watcher behavior rather than inventing a separate polling daemon.
- Restart only vhotplug when updating its package or configuration, unless the container settings themselves changed.
- Check both `is-active` and `is-enabled`. Ask separately whether the user wants host-boot autostart; an active service may still be disabled at boot.

Read back the exact installed modules, configuration, and unit after deployment. A change in the source checkout does not automatically update the installed service copy. Remove only explicitly known manual test attachments before handing those devices to the daemon.

## 6. Verification criteria

Run repository tests and relevant lint checks:

```sh
.venv/bin/python -m pip install '.[test]' ruff
.venv/bin/python -m pytest -q
.venv/bin/ruff check vhotplug/qemulink.py vhotplug/vmm.py tests/test_qemu_usb_hotplug.py tests/test_windows_usb_ports.py
```

`tests/test_windows_usb_ports.py` uses the committed synthetic fixture `tests/fixtures/windows_usb_ports.json`, so a fresh clone can run the tests without a local deployment configuration. Its bus/port values are test data, not defaults for a new machine. Keep the user's actual `config_windows.json` local; the repository ignores that filename.

Hardware verification requires all of the following, not just passing unit tests:

1. Confirm that a device on the selected branch matches the Windows rule and that representative devices on other sockets do not.
2. Confirm the hub remains bound to Linux's `hub` driver and is not forwarded itself.
3. Confirm a real leaf device is attached automatically. Read QMP `qom-get` for `/machine/peripheral/DEVICE_ID`, property `hostdevice`; it must equal the current sysfs-derived USB node.
4. Check ownership of the current node with `sudo fuser -v /dev/bus/usb/BBB/DDD`. The QEMU process must actually hold it. A QMP command that returns success or a generic empty USB placeholder is insufficient.
5. Confirm recognition and useful function in Windows: a phone in Explorer/MTP or a CH340 under Device Manager's Ports category. Test actual debugging/flashing separately for debug probes.
6. Physically unplug/replug the leaf device, then verify the newly enumerated node is attached without a VM restart. Monitor-only detach/attach does not exercise this path.
7. With a hub, physically unplug/replug the upstream hub cable while leaving a leaf device inserted. Verify the hub and leaf re-enumerate, the child returns in Windows, and old QEMU objects are cleaned up.
8. Confirm Windows was not restarted during the reconnect tests. If guest inspection tools are unavailable, ask the user for the guest result and distinguish their confirmation from host-side observations.

A bounded read-only topology/QMP monitor can run while the user performs the physical steps. Do not replace a missing physical test with a claim that hotplug is complete.

## Troubleshooting

| Symptom | Check first |
|---|---|
| USB 3 phone works, USB 2 serial adapter does not | The chosen socket's USB 2 companion branch and allow rules |
| Hub disappears or children disconnect unexpectedly | Hub exclusions and whether the hub is still bound to Linux |
| New node exists but bus/address attachment fails | Stale container libusb enumeration, `usbHostDevice: true`, and supported `hostdevice` property |
| QEMU cannot open or claim the new node | Container visibility, permissions, current node, kernel drivers, and competing host applications |
| Initial attachment works but reconnect fails | Fresh event node, running installed package/config, removal logs, and new-node ownership |
| Windows sees an unknown device but QEMU owns the node | Guest driver installation and device-specific compatibility |
| Source changes have no effect | The daemon's actual installed package path and whether it was restarted |
| Service is active but does not return after reboot | Boot enablement, QMP-directory recreation, and startup ordering |

## Rollback and handoff

Keep root-controlled backups of the deployed package/configuration, unit, and any edited Compose files. To roll back, stop the host daemon, remove only its known managed USB objects if needed, restore the intended backups, and validate the result. Restoring Compose changes can require a separately approved Windows restart. Do not delete guest disks or broad sets of QEMU devices.

Report the physical socket label, discovered branch pairs, modified file paths, backup paths, service/boot state, tests actually executed, host ownership evidence, and guest results. State remaining limitations. Do not include credentials or claim untested devices are fully compatible.

## Suggested user prompt

> Read AGENTS.md and help configure this repository for physical-port-based USB hotplug into my Docker Windows VM. Inspect my installation first, ask which socket should belong to Windows, map its USB 2 and USB 3 branches, keep hubs on Linux, and ask before persistent changes or restarting Windows. Verify a real device reconnect and a whole-hub reconnect before declaring success.
