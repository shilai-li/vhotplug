#!/usr/bin/env bash
# No privilege escalation or dependency installation is performed here.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${VHOTPLUG_INSTALLER_PYTHON:-python3}" "$here/windows_hotplug_setup.py" "$@"
