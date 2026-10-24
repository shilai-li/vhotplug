#!/usr/bin/env bash
# Move the Windows USB socket's devices between Linux and the running Windows VM.
# Runs the installed, root-controlled vhotplugcli against the root-only API socket.
set -euo pipefail

install_dir="${VHOTPLUG_INSTALL_DIR:-/opt/vhotplug_windows}"
tag="${VHOTPLUG_USB_TAG:-windows-usb}"

usage() {
    echo "usage: sudo $0 attach|detach|status" >&2
    echo "  attach  give the devices plugged into the Windows socket to Windows" >&2
    echo "  detach  return them to Linux; Windows keeps running" >&2
    echo "  status  list the socket's devices and the VM holding each one" >&2
    exit 2
}

[[ $# -eq 1 ]] || usage
case "$1" in
    attach) command=(usb attach --tag "$tag") ;;
    detach) command=(usb detach --tag "$tag") ;;
    status) command=(usb list --tag "$tag") ;;
    *) usage ;;
esac

if [[ $EUID -ne 0 ]]; then
    echo "error: run with sudo; the vhotplug API socket is root-only" >&2
    exit 1
fi

release="$(sed -n 's/^ *"release": "\([a-f0-9]\{32\}\)",\{0,1\}$/\1/p' "$install_dir/deployment.json")"
if [[ -z $release ]]; then
    echo "error: no installed vhotplug release found in $install_dir" >&2
    exit 1
fi

exec "$install_dir/releases/$release/venv/bin/python" -I -B -m vhotplugcli "${command[@]}"
