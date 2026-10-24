# Docker Windows USB hotplug installer

The installer configures a Linux host for this fork's QEMU direct-node USB hotplug feature. It does not rebuild QEMU or modify Windows files.

Start with a read-only plan. Apply only after confirming the physical socket, reviewing the changes, and preparing the package wheels. A fresh machine still needs a real cable reconnect and guest-side functional check before hotplug can be called working.

## Prerequisites

- Linux with systemd, Docker Engine and the Compose plugin.
- A trusted, reviewed checkout of this fork.
- Python **3.13 or newer**, including `venv`; use a root-controlled system interpreter for the installed daemon.
- PyYAML in the interpreter running the installer helper.
- A Docker Windows image whose launcher accepts `QMP=unix:/qmp/qmp.sock,server=on,wait=off` and whose QEMU implements `usb-host.hostdevice`. Verify the actual image/launcher; the installer cannot infer compatibility from an image tag alone.
- Permission to read the selected Compose file and access Docker/systemd for privileged operations.

The installer does not install OS packages, change elevation mechanisms, stop ADB, or take over an entire USB controller.

### Omarchy launcher compatibility

Before applying on Omarchy, use a Windows VM launcher whose `rewrite_compose_security` preserves the original storage/shared list indentation. This installer can emit valid indentless YAML sequences; an older launcher that rewrites only those two entries with a hardcoded six-space prefix can mix them with four-space USB/QMP entries and corrupt Compose on the next launch. The original list prefix must remain unchanged when replacing a mount source.

The launcher's source-hardening steps must also explicitly clear directory setuid/setgid bits, for example with `chmod u=rwx,go=,a-s`. Numeric `0700` can leave Samba's setgid bit in place and fail an exact-mode-700 preflight check.

These are launcher fixes, not changes this installer deploys. Updating the checkout alone does not update the root-owned helper used for privileged actions; verify both copies. Check launcher compatibility before applying, then validate Compose after its first launch and test physical USB reconnects.

## Prepare as an ordinary user

From the repository root, choose a Python 3.13+ interpreter:

```bash
python3 -m venv .installer_venv
.installer_venv/bin/python -m pip install PyYAML
.installer_venv/bin/python -m pip wheel . --wheel-dir wheelhouse
export VHOTPLUG_INSTALLER_PYTHON="$PWD/.installer_venv/bin/python"
./scripts/configure_windows_hotplug.sh --help
```

Preparation downloads/builds wheels in the caller's environment. Review the checkout and dependencies before installing. The privileged application uses the supplied wheelhouse offline instead of building source or fetching packages as root. It explicitly disables pip configuration and rejects direct dependency references (including URL and external-file references) in every supplied wheel; `--no-index` alone is not an offline guarantee. Supply ordinary named dependencies and their wheels. Do not commit the environment or wheelhouse.

On distributions without `ensurepip`, use the distribution's supported virtual-environment tooling; do not change a working system Python merely for this script.

## Select the physical socket

Confirm the user's intended socket by inserting a known device and inspecting `lsusb -t` and `/sys/bus/usb/devices/`. A USB 3 socket can have separate USB 2 and SuperSpeed branches: observe both rather than guessing a companion bus.

Each `--port BUS:ROOTPORT` assigns **all matching non-hub descendants** of that branch to Windows, including future devices and nested hub children. Physical hubs remain on Linux. Keep Linux-needed input/network devices and mounted storage off that socket; unmount storage before transferring it.

The examples below use shell variables so no machine-specific numbers become defaults:

```bash
COMPOSE=/absolute/path/to/docker-compose.yml
SERVICE=windows
USB2_BRANCH='BUS:ROOTPORT'  # Replace with an observed numeric pair.
USB3_BRANCH='BUS:ROOTPORT'  # Replace with its separately observed companion.
```

## Attach mode

By default the selected socket is in **manual** mode: plugged-in devices stay on
Linux, also when Windows starts or restarts, until you move them while Windows
is running:

```bash
sudo ./scripts/windows_usb.sh attach   # devices in the socket go to Windows
sudo ./scripts/windows_usb.sh detach   # they return to Linux; Windows keeps running
sudo ./scripts/windows_usb.sh status   # which VM holds each device
```

The wrapper runs the installed release's `vhotplugcli` with the rule tag
`<service>-usb` (override with `VHOTPLUG_USB_TAG`, and the install directory with
`VHOTPLUG_INSTALL_DIR`). The daemon serves this API only on the root-only Unix
socket `/var/lib/vhotplug/vhotplug.sock`, inside the unit's 0700 state directory
and outside the QMP directory the container can see. Eject storage in Windows
before detaching: to Windows, detaching is a cable pull. A device unplugged
while attached returns to Linux when plugged in again; attach it again by command.

Pass `--auto-attach` to apply to forward every matching device on plug-in
instead, as earlier versions did. `--verify` reports the saved mode.

## Preview

```bash
./scripts/configure_windows_hotplug.sh --plan \
  --compose "$COMPOSE" --service "$SERVICE" \
  --port "$USB2_BRANCH" --port "$USB3_BRANCH" \
  --socket-label 'Dedicated Windows USB socket'
```

Preview is read-only and does not launch Windows. It reports intended targets without printing guest credentials or the full Compose environment. Provide the Compose path explicitly when its discovery is ambiguous; do not treat a Docker permission denial as absence of a VM.

The default deployment targets are:

| Target | Purpose |
|---|---|
| `/opt/vhotplug_windows/` | Root-controlled installed package, releases and backups |
| `/etc/vhotplug/config_windows.json` | Port selection, hub exclusions, direct-node mode |
| `/etc/systemd/system/vhotplug_windows.service` | Host daemon |
| `/etc/tmpfiles.d/vhotplug_windows.conf` | Recreate the protected QMP directory after reboot |
| `/run/vhotplug_windows/qmp.sock` | Privileged QMP endpoint |
| Selected Compose file | QMP endpoint, socket/USB bind mounts and USB cgroup access |

The script preserves unrelated Compose settings semantically. It does not promise to preserve YAML comments or formatting. Conflicting endpoints/mounts, unsupported YAML structures or unknown static assignments require inspection, not blind replacement. Backups contain the original file contents, so keep them private.

If a static `usb-host` assignment overlaps the selected scope, remove it only by naming its reviewed QEMU device ID with `--remove-static-id ID`. Unrelated static devices must remain intact.

## Apply

Use the host's approved privilege mechanism. `--yes` confirms the reviewed persistent-file changes; it does **not** authorize a Windows restart or host-boot autostart.

```bash
sudo env VHOTPLUG_INSTALLER_PYTHON="$VHOTPLUG_INSTALLER_PYTHON" \
  ./scripts/configure_windows_hotplug.sh --apply --yes \
  --compose "$COMPOSE" --service "$SERVICE" \
  --port "$USB2_BRANCH" --port "$USB3_BRANCH" \
  --socket-label 'Dedicated Windows USB socket' \
  --wheelhouse "$PWD/wheelhouse" --python /absolute/path/to/system/python3
```

Do not copy the interpreter placeholder literally. Choose an existing, root-controlled Python 3.13+ executable. If elevation is denied, stop rather than using another mechanism or supplying a password through a script.

Separate opt-ins:

- `--start-service`: start/restart the host daemon when the configuration is ready.
- `--enable-autostart`: enable the daemon at host boot. Without it, existing boot enablement is not silently changed.
- `--allow-vm-restart`: permit Compose to recreate the Windows container if required. **Save guest work first.** Do not use this option merely to make a verification command pass.

Without restart approval, edited Compose settings may remain pending in the running container. A saved-file configuration is not proof of a live QMP endpoint or successful USB forwarding. Do not start a new daemon against incompatible old container settings.

Existing active daemons are not replaced in place. For an update that is not already idempotent, review `systemctl cat UNIT`, confirm the scope, and explicitly stop **only that daemon** before applying. The installer does not stop it for you. Do not stop ADB or restart Windows as a workaround.

The installer backs up reviewed targets before replacing them, validates candidates, uses root-controlled installed code/config, and verifies file readback. Record its reported backup path for rollback. Repeat the plan before rerunning apply; idempotent settings should not accumulate duplicate mounts or rules.

Opt-in VM/service activation is outside the reversible file transaction, although the CLI holds the deployment lock through activation. If activation fails, installed files and backups remain for diagnosis; do not assume the running VM was rolled back. The script checks readiness immediately rather than waiting indefinitely. If Windows/QMP is still starting, wait for it to be ready and rerun the reviewed command.

The installer deliberately rejects multi-file Compose discovery, YAML anchors/aliases/merges/includes, ambiguous unquoted `yes/no/on/off`, YAML-1.1 integer/floating/timestamp forms that can change meaning under Compose (for example `12:34`, `0123`, and date-shaped strings), selected-service `env_file`/`extends`/command/entrypoint overrides, and unsupported device/mount syntax. Quote ambiguous scalar values and review/flatten unsupported structures explicitly before using it. Ordinary failures and interrupts restore file changes; power loss or `SIGKILL` cannot be made a multi-file atomic transaction.

## Verify the deployed and running setup

```bash
sudo env VHOTPLUG_INSTALLER_PYTHON="$VHOTPLUG_INSTALLER_PYTHON" \
  ./scripts/configure_windows_hotplug.sh --verify \
  --compose "$COMPOSE" --service "$SERVICE" \
  --port "$USB2_BRANCH" --port "$USB3_BRANCH"
```

Verification is read-only. It checks saved settings and installation plus the protected live QMP endpoint and `usb-host.hostdevice` support. It must report missing prerequisites or a pending container update rather than claiming hotplug success.

Then complete the hardware verification in [AGENTS.md](../AGENTS.md):

1. Confirm selected-port devices match, unrelated sockets do not, and hubs stay on Linux.
2. Confirm a plugged-in device stays on Linux, then `windows_usb.sh attach` uses the device's **current** node and QEMU actually holds it (with `--auto-attach`, confirm this on plug-in).
3. Confirm useful recognition/function in Windows.
4. Run `windows_usb.sh detach` and confirm the device returns to Linux while Windows keeps running; attach it again.
5. Physically unplug/replug the leaf device; verify its newly enumerated node and that an attach returns it to the guest without a VM restart.
6. Reconnect the whole hub with a child left inserted; verify the same criteria.

J-Link/ST-LINK enumeration alone does not prove debugging, flashing, or firmware-update re-enumeration. These require their own functional tests. ADB and other host applications can independently reclaim interfaces; the installer never stops them silently.

## Rollback

```bash
sudo env VHOTPLUG_INSTALLER_PYTHON="$VHOTPLUG_INSTALLER_PYTHON" \
  ./scripts/configure_windows_hotplug.sh --rollback /absolute/path/to/reported/backup --yes \
  --compose "$COMPOSE" --service "$SERVICE"
```

Use only a backup produced for the same deployment targets, specifying the same path options used for apply (such as `--socket-dir`, `--install-dir`, `--config`, and `--unit-name` if customized). Stop an active host daemon separately, with explicit approval, before rollback. Rollback must reject unsafe backup paths or targets changed since application rather than overwrite newer work. Inspect a conflict and choose recovery deliberately.

Restoring files does not rewind a running VM, restore boot-enablement state, remove arbitrary QEMU objects, or delete archived package releases/backups. A container restart is a separate decision; never delete unrelated guest devices or guest storage to roll back host configuration.

## Development verification

```bash
.venv/bin/python -m pip install '.[test]' ruff
.venv/bin/python -m pytest -q
.venv/bin/ruff check scripts/windows_hotplug_setup.py tests/test_windows_hotplug_setup.py
bash -n scripts/configure_windows_hotplug.sh
```

The optional real installation smoke test additionally requires a prepared wheelhouse:

```bash
VHOTPLUG_TEST_WHEELHOUSE="$PWD/wheelhouse" .venv/bin/python -m pytest -q
```

Without it, that one smoke test is skipped; ordinary installer tests still run. The tests respect `TMPDIR` when set and also work when it is unset.

Installer tests use temporary sandbox files and injected command runners for privileged dependencies. That exercises transformations, installation transactions, failure handling, readback and rollback without altering a real Windows VM. It is not a substitute for a privileged fresh-host deployment and physical USB tests.
