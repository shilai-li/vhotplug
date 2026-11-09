#!/usr/bin/env bash
# Move the Windows USB socket's devices between Linux and the running Windows VM.
# Runs the installed, root-controlled vhotplugcli against the root-only API socket.
#
#   windows-usb attach   give the devices plugged into the Windows socket to Windows
#   windows-usb detach   return them to Linux; Windows keeps running
#   windows-usb          show the socket's devices and the VM holding each one
#
# Re-runs itself with sudo when not root.
set -euo pipefail

install_dir="${VHOTPLUG_INSTALL_DIR:-/opt/vhotplug_windows}"
tag="${VHOTPLUG_USB_TAG:-windows-usb}"
name="$(basename "$0")"

usage() {
    echo "usage: $name [attach|detach|status]" >&2
    echo "  attach  give the devices plugged into the Windows socket to Windows" >&2
    echo "  detach  return them to Linux; Windows keeps running" >&2
    echo "  status  list the socket's devices and the VM holding each one (default)" >&2
    exit 2
}

[[ $# -le 1 ]] || usage
case "${1:-status}" in
    attach) command=(usb attach --tag "$tag") ;;
    detach) command=(usb detach --tag "$tag") ;;
    status) command=(usb list --tag "$tag") ;;
    -h|--help|help) usage ;;
    *) usage ;;
esac

if [[ $EUID -ne 0 ]]; then
    # The vhotplug API socket is root-only; sudo drops the environment, so pass the overrides on.
    exec sudo env VHOTPLUG_INSTALL_DIR="$install_dir" VHOTPLUG_USB_TAG="$tag" \
        "$(realpath "$0")" "$@"
fi

release="$(sed -n 's/^ *"release": "\([a-f0-9]\{32\}\)",\{0,1\}$/\1/p' "$install_dir/deployment.json")"
if [[ -z $release ]]; then
    echo "error: no installed vhotplug release found in $install_dir" >&2
    exit 1
fi

exec "$install_dir/releases/$release/venv/bin/python" -I -B -m vhotplugcli "${command[@]}"
