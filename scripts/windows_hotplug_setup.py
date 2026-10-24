#!/usr/bin/env python3
"""Conservative Docker Windows installer; plan/verify never modify host state.

PyYAML is an explicit helper prerequisite. Only reviewed, prebuilt wheels are
installed; this program never builds the checkout, elevates, or installs OS tools.
"""


# Inline raises intentionally participate in file-transaction restore handlers.
# ruff: noqa: TRY301

import argparse
import copy
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from email.parser import BytesParser
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

QMP = "unix:/qmp/qmp.sock,server=on,wait=off"


class SetupError(Exception):
    """Safe-to-display error: never include environment or subprocess output."""


def port(value):
    if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("port must be positive BUS:ROOTPORT integers")
    return tuple(int(part) for part in value.split(":"))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="default: read-only offline plan")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--rollback", type=Path, metavar="BACKUP_PATH")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--compose", type=Path)
    parser.add_argument("--service", default="windows")
    parser.add_argument("--port", type=port, action="append", default=[])
    parser.add_argument("--socket-label", default="User-approved Windows physical socket")
    parser.add_argument("--socket-dir", type=Path, default=Path("/run/vhotplug_windows"))
    parser.add_argument("--install-dir", type=Path, default=Path("/opt/vhotplug_windows"))
    parser.add_argument("--config", type=Path, default=Path("/etc/vhotplug/config_windows.json"))
    parser.add_argument("--unit-name", default="vhotplug_windows.service")
    parser.add_argument("--python", type=Path, default=Path(sys._base_executable))
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--remove-static-id", action="append", default=[])
    parser.add_argument("--start-service", action="store_true")
    parser.add_argument("--enable-autostart", action="store_true")
    parser.add_argument(
        "--auto-attach",
        action="store_true",
        help="attach socket devices on plug-in; by default only `vhotplugcli usb attach --tag TAG` attaches them",
    )
    parser.add_argument(
        "--allow-vm-restart", action="store_true", help="WARNING: save Windows work before allowing recreation"
    )
    args = parser.parse_args(argv)
    for value in (args.service, args.unit_name, *args.remove_static_id):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value.startswith("-"):
            parser.error("unsafe service/unit/device identifier")
    if not args.unit_name.endswith(".service"):
        parser.error("unit-name must end in .service")
    for value in (
        args.compose,
        args.socket_dir,
        args.install_dir,
        args.config,
        args.python,
        args.wheelhouse,
        args.rollback,
    ):
        if value is not None:
            safe_path(value)
    if any(ord(char) < 32 for char in args.socket_label):
        parser.error("socket label contains control characters")
    if len(os.fsencode(args.socket_dir / "qmp.sock")) >= 108:
        raise SetupError("Unix QMP socket path is too long (Linux limit: 107 bytes)")
    args.port = sorted(set(args.port))
    return args


def safe_path(path):
    if not path.is_absolute() or ".." in path.parts or not re.fullmatch(r"/[A-Za-z0-9_./+-]+", str(path)):
        raise SetupError("paths must be absolute, without traversal, whitespace or expansion characters")
    return path


def run(command, *, cwd=None):
    try:
        # --isolated alone still reads global/site pip configuration and
        # PIP_CONFIG_FILE. Disable all config so target/prefix cannot escape
        # the managed venv; drop inherited pip options as defense in depth.
        environment = {key: value for key, value in os.environ.items() if not key.startswith("PIP_")}
        environment["PIP_CONFIG_FILE"] = os.devnull
        return subprocess.run(
            command, cwd=cwd, env=environment, capture_output=True, text=True, check=False, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SetupError("required external command unavailable or timed out") from error


def checked(runner, command, *, cwd=None, label="external validation"):
    result = runner(command, cwd=cwd)
    if result.returncode:
        raise SetupError(f"{label} failed (output withheld to protect credentials)")
    return result.stdout


def load_compose(text):
    if yaml is None:
        raise SetupError("PyYAML is required by the installer helper; install it as an unprivileged caller")

    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            if not isinstance(key, str) or key in result:
                raise SetupError("Compose has duplicate or non-string mapping keys")
            result[key] = loader.construct_object(value_node)
        return result

    def boolean(loader, node):
        if node.value.lower() not in ("true", "false"):
            raise SetupError("ambiguous YAML boolean: quote yes/no/on/off values explicitly")
        return node.value.lower() == "true"

    def integer(loader, node):
        if not re.fullmatch(r"[-+]?(?:0|[1-9][0-9]*)", node.value):
            raise SetupError("ambiguous YAML integer scalar: quote it explicitly before applying")
        return loader.construct_yaml_int(node)

    def floating(loader, node):
        if ":" in node.value:
            raise SetupError("ambiguous YAML floating scalar: quote it explicitly before applying")
        return loader.construct_yaml_float(node)

    def timestamp(loader, node):
        raise SetupError("ambiguous YAML timestamp scalar: quote it explicitly before applying")

    UniqueLoader.add_constructor("tag:yaml.org,2002:bool", boolean)
    UniqueLoader.add_constructor("tag:yaml.org,2002:int", integer)
    UniqueLoader.add_constructor("tag:yaml.org,2002:float", floating)
    UniqueLoader.add_constructor("tag:yaml.org,2002:timestamp", timestamp)
    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        if any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)) for token in yaml.scan(text)):
            raise SetupError("YAML anchors/aliases are unsupported; flatten them explicitly first")
        result = yaml.load(text, Loader=UniqueLoader)
        if not isinstance(result, dict) or "services" not in result:
            raise SetupError("Compose must contain services")
        if "include" in result:
            raise SetupError("Compose includes cannot be safely analyzed; flatten explicitly")
        return result
    except yaml.YAMLError as error:
        raise SetupError("invalid or unsupported Compose YAML (content withheld)") from error


def discover_compose(args, runner=run):
    if args.compose is not None:
        return args.compose
    raw = checked(
        runner,
        ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.service={args.service}"],
        label="Docker discovery",
    )
    ids = raw.split()
    files = set()
    for identifier in ids:
        if not re.fullmatch(r"[a-f0-9]{12,64}", identifier):
            raise SetupError("invalid Docker discovery response")
        label = checked(
            runner,
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.project.config_files"}}',
                identifier,
            ],
            label="Docker label discovery",
        ).strip()
        if not label or "," in label:
            raise SetupError("multi-file or missing Compose labels: supply one flattened --compose explicitly")
        files.add(label)
    if len(files) != 1 or len(ids) != 1:
        raise SetupError("Compose discovery is ambiguous or empty: specify --compose")
    return safe_path(Path(files.pop()))


def environment(service):
    env = service.get("environment", {})
    if isinstance(env, dict):
        return env, False
    if isinstance(env, list) and all(isinstance(item, str) for item in env):
        values = {}
        for item in env:
            key, sep, value = item.partition("=")
            if key in values:
                raise SetupError("duplicate environment key")
            values[key] = value if sep else None
        return values, True
    raise SetupError("unsupported environment syntax")


def patch_arguments(arguments, args):
    if not isinstance(arguments, str) or "$" in arguments.replace("$$", "") or "`" in arguments:
        raise SetupError("ARGUMENTS must be a literal string (Compose $$ escapes are allowed)")
    pattern = r"""(?:[^\s"'\\]+|\\.|"(?:\\.|[^"\\])*"|'[^']*')+"""
    spans = list(re.finditer(pattern, arguments))
    tokens = []
    end = 0
    for span in spans:
        if arguments[end : span.start()].strip():
            raise SetupError("unsupported ARGUMENTS quoting")
        decoded = shlex.split(span.group())
        if len(decoded) != 1:
            raise SetupError("unsupported ARGUMENTS quoting")
        tokens.append(decoded[0])
        end = span.end()
    if arguments[end:].strip():
        raise SetupError("unsupported ARGUMENTS quoting")
    removals, removed = [], set()
    for index, word in enumerate(tokens):
        if word == "-qmp" or word.startswith("-qmp="):
            raise SetupError("ARGUMENTS supplies a conflicting QMP endpoint")
        if word == "-device":
            if index + 1 == len(tokens):
                raise SetupError("incomplete -device argument")
            value, last = tokens[index + 1], index + 1
        elif word.startswith("-device="):
            value, last = word[len("-device=") :], index
        else:
            continue
        if value.split(",", 1)[0] != "usb-host":
            continue
        fields = {}
        for item in value.split(",")[1:]:
            key, sep, val = item.partition("=")
            if not sep or key in fields:
                raise SetupError("unsupported usb-host selector")
            fields[key] = val
        identifier = fields.get("id")
        if identifier in args.remove_static_id:
            if identifier in removed:
                raise SetupError("duplicate static USB id")
            removed.add(identifier)
            removals.append((spans[index].start(), spans[last].end()))
            continue
        known = {"id", "hostbus", "hostport", "hostaddr", "vendorid", "productid", "bus", "port"}
        if fields.keys() - known:
            raise SetupError("unknown static USB selector; approve removal by explicit id")
        bus, root = fields.get("hostbus"), fields.get("hostport", "").split(".")[0]
        if (bus and not bus.isdecimal()) or (root and not root.isdecimal()):
            raise SetupError("unsupported static bus/port selector")
        bus = int(bus) if bus else None
        # QEMU hostbus=0 is a wildcard, not evidence of a disjoint bus.
        bus = bus or None
        disjoint = args.port and all(
            (bus is not None and bus != branch_bus) or (root and int(root) != branch_port)
            for branch_bus, branch_port in args.port
        )
        if not disjoint:
            raise SetupError("static USB selector overlaps or cannot be resolved; approve --remove-static-id")
    if set(args.remove_static_id) != removed:
        raise SetupError("approved static id not found; no unrelated arguments will be removed")
    for start, end in reversed(removals):
        arguments = arguments[:start] + arguments[end:]
    return arguments


def patch_compose(document, args):
    doc = copy.deepcopy(document)
    if args.service not in doc["services"] or not isinstance(doc["services"][args.service], dict):
        raise SetupError("selected Compose service does not exist")
    service = doc["services"][args.service]
    if any(key in service for key in ("command", "entrypoint", "extends", "env_file")):
        raise SetupError("command/entrypoint/extends/env_file cannot be safely analyzed; flatten settings explicitly")
    env, listed = environment(service)
    if "ARGUMENTS" in env:
        env["ARGUMENTS"] = patch_arguments(env["ARGUMENTS"], args)
    elif args.remove_static_id:
        raise SetupError("no ARGUMENTS static id found")
    if env.get("QMP") not in (None, "", QMP):
        raise SetupError("existing QMP conflicts with selected protected Unix endpoint")
    env["QMP"] = QMP
    service["environment"] = [key if val is None else f"{key}={val}" for key, val in env.items()] if listed else env
    for device in service.get("devices", []):
        if not isinstance(device, str) or "$" in device or "/dev/bus/usb" in device:
            raise SetupError(
                "static USB device-node overlays or unsupported devices syntax must be resolved explicitly"
            )
    volumes = service.setdefault("volumes", [])
    if not isinstance(volumes, list):
        raise SetupError("unsupported volumes syntax")
    for source, target in (("/dev/bus/usb", "/dev/bus/usb"), (str(args.socket_dir), "/qmp")):
        found = False
        for volume in volumes:
            if isinstance(volume, str):
                parts = volume.split(":")
                if len(parts) > 3:
                    raise SetupError("unsupported volume syntax")
                origin, destination = (parts[0], parts[1]) if len(parts) >= 2 else (None, parts[0])
                writable = len(parts) == 2 or (len(parts) == 3 and parts[2] == "rw")
                bind = origin is not None
            elif isinstance(volume, dict):
                origin, destination = volume.get("source"), volume.get("target")
                writable = volume.get("read_only", False) is False
                bind = volume.get("type") == "bind"
            else:
                raise SetupError("unsupported volume syntax")
            if (
                not isinstance(destination, str)
                or "$" in destination.replace("$$", "")
                or ".." in Path(destination).parts
            ):
                raise SetupError("unresolved or unsafe volume target")
            if destination.rstrip("/") == target and destination != target:
                raise SetupError("non-canonical USB/QMP mount target")
            if isinstance(destination, str) and destination != target:
                dest = destination.rstrip("/")
                if target.startswith(dest + "/") or dest.startswith(target + "/"):
                    raise SetupError("overlapping USB/QMP mount target")
            if destination == target:
                if found or origin != source or not writable or not bind:
                    raise SetupError("USB/QMP mount conflict; resolve explicitly")
                found = True
        if not found:
            volumes.append(f"{source}:{target}")
    rules = service.setdefault("device_cgroup_rules", [])
    if not isinstance(rules, list):
        raise SetupError("unsupported device_cgroup_rules syntax")
    if "c 189:* rwm" not in rules:
        rules.append("c 189:* rwm")
    return doc


# Root-only manual attach/detach endpoint. It is vhotplugcli's default path, inside the unit's
# 0700 StateDirectory, and deliberately outside the QMP directory that the container can see.
API_SOCKET = "/var/lib/vhotplug/vhotplug.sock"


def usb_tag(args):
    return f"{args.service}-usb"


def make_config(args):
    return {
        "general": {
            "api": {"enable": True, "transports": ["unix"], "unixSocket": API_SOCKET, "unixSocketMode": "0600"},
            "persistency": False,
        },
        "usbAuthorization": {"enable": False, "deauthorizeUnmatched": False},
        "usbPassthrough": [
            {
                "description": args.socket_label,
                "targetVm": args.service,
                "tag": usb_tag(args),
                "autoAttach": args.auto_attach,
                "allow": [{"bus": bus, "port": root} for bus, root in args.port],
                "deny": [{"deviceClass": 9}, {"interfaceClass": 9}],
            }
        ],
        "pciPassthrough": [],
        "evdevPassthrough": [],
        "vms": [
            {"name": args.service, "type": "qemu", "socket": str(args.socket_dir / "qmp.sock"), "usbHostDevice": True}
        ],
    }


def make_plan(args, runner=run):
    args.compose = discover_compose(args, runner)
    original = args.compose.read_bytes()
    document = load_compose(original.decode())
    updated = patch_compose(document, args)
    compose = original if updated == document else yaml.safe_dump(updated, sort_keys=False).encode()
    return {
        "compose": compose,
        "config": (json.dumps(make_config(args), indent=2) + "\n").encode(),
        "compose_changed": compose != original,
    }


class PathPolicy:
    """Reject symlinks and non-root writable ancestry; injection is Python-only.

    Tests inject a fenced temporary root and their uid. The CLI always uses /
    and uid 0; there is no environment variable or command-line bypass.
    """

    def __init__(self, root=Path("/"), uid=0):
        self.root, self.uid = root, uid

    def check(self, path):
        # CLI/systemd paths use safe_path; package-internal filenames need not
        # be ASCII (Python 3.14 venv can create a Unicode interpreter alias).
        if not path.is_absolute() or ".." in path.parts:
            raise SetupError("unsafe filesystem path")
        if not path.is_relative_to(self.root):
            raise SetupError("target outside trusted filesystem root")
        chain = [self.root]
        for part in path.relative_to(self.root).parts:
            chain.append(chain[-1] / part)
        for item in chain:
            if not item.exists() and not item.is_symlink():
                continue
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != self.uid or info.st_mode & 0o022:
                raise SetupError("unsafe path: symlink, owner, or non-root writable permissions")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise SetupError("unsafe path: expected regular file or directory")
            if item != path and not item.is_dir():
                raise SetupError("unsafe non-directory ancestor")
        return path

    def mkdir(self, path, mode=0o700):
        self.check(path)
        missing = []
        cursor = path
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for item in reversed(missing):
            item.mkdir(mode=mode)
        self.check(path)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def snapshot(path, policy):
    policy.check(path)
    if not path.exists():
        return None
    info = path.stat()
    if not path.is_file() or info.st_nlink != 1:
        raise SetupError("target must be a regular file with a single link")
    data = path.read_bytes()
    return {
        "data": data,
        "hash": digest(data),
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
    }


def atomic_write(path, data, policy, mode=0o600, owner=None):
    policy.check(path)
    policy.mkdir(path.parent)
    fd, name = tempfile.mkstemp(prefix=".vhotplug-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            if owner is not None and os.geteuid() == 0:
                os.fchown(stream.fileno(), *owner)
            os.fsync(stream.fileno())
        policy.check(path)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def restore(path, saved, policy):
    policy.check(path)
    if saved is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, saved["data"], policy, saved["mode"], (saved["uid"], saved["gid"]))


def targets(args, system_root):
    result = {
        "compose": args.compose,
        "config": args.config,
        "unit": system_root / "etc/systemd/system" / args.unit_name,
        "tmpfiles": system_root / "etc/tmpfiles.d" / f"{args.unit_name.removesuffix('.service')}.conf",
        "metadata": args.install_dir / "deployment.json",
    }
    lock_path = args.install_dir / ".installer.lock"
    if any(path.is_relative_to(lock_path) or lock_path.is_relative_to(path) for path in result.values()):
        raise SetupError("deployment target paths collide with installer lock")
    if len(set(result.values())) != len(result) or any(path == args.socket_dir for path in result.values()):
        raise SetupError("deployment target paths collide")
    return result


def unit_content(args, release):
    runtime = ""
    if args.socket_dir.is_relative_to(Path("/run")) and args.socket_dir != Path("/run"):
        runtime = (
            f"RuntimeDirectory={args.socket_dir.relative_to('/run')}\n"
            "RuntimeDirectoryMode=0700\nRuntimeDirectoryPreserve=yes\n"
        )
    return (
        "[Unit]\nDescription=Windows physical-socket USB hotplug\nAfter=systemd-tmpfiles-setup.service docker.service\n"
        "[Service]\nType=simple\nUser=root\nGroup=root\nUMask=0077\n"
        "Environment=PYTHONDONTWRITEBYTECODE=1\n"
        + runtime
        + "StateDirectory=vhotplug\nStateDirectoryMode=0700\n"
        + f"ExecStart={release}/venv/bin/python -I -B -m vhotplug --attach-connected --config {args.config}\n"
        "Restart=on-failure\n[Install]\nWantedBy=multi-user.target\n"
    ).encode()


def tree_digest(root, policy):
    policy.check(root)
    hashes = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            # venv creates internal lib64 links even with --copies.
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise SetupError("release contains an external symlink")
            policy.check(resolved)
            hashes.append((str(path.relative_to(root)), "link", os.readlink(path)))
        else:
            policy.check(path)
            if path.is_file():
                hashes.append(
                    (str(path.relative_to(root)), stat.S_IMODE(path.stat().st_mode), digest(path.read_bytes()))
                )
    return digest(json.dumps(hashes).encode())


def installed_metadata(path, args, policy):
    saved = snapshot(path, policy)
    if saved is None:
        return None
    metadata = json.loads(saved["data"])
    if not re.fullmatch(r"[a-f0-9]{32}", metadata.get("release", "")):
        raise SetupError("invalid installed release metadata")
    release = args.install_dir / "releases" / metadata["release"]
    if tree_digest(release, policy) != metadata.get("tree_hash"):
        raise SetupError("installed release hash drift")
    return metadata


def service_state(args, runner):
    result = runner(["systemctl", "is-active", args.unit_name])
    if result.returncode not in (0, 3, 4) or result.stdout.strip() not in ("active", "inactive", "failed", "unknown"):
        raise SetupError("cannot establish daemon state; refusing changes")
    return result.stdout.strip() == "active"


def validate_candidates(args, candidates, release, runner, policy):
    paths = []
    try:
        for key, parent, suffix in (
            ("compose", args.compose.parent, ".yaml"),
            ("unit", release, ".service"),
            ("config", release, ".json"),
        ):
            fd, name = tempfile.mkstemp(prefix=".vhotplug-candidate-", suffix=suffix, dir=parent)
            os.close(fd)
            path = Path(name)
            paths.append(path)
            atomic_write(path, candidates[key], policy)
        checked(
            runner,
            ["docker", "compose", "-f", str(paths[0]), "config", "-q"],
            cwd=args.compose.parent,
            label="Compose validation",
        )
        checked(runner, ["systemd-analyze", "verify", str(paths[1])], label="systemd unit validation")
        checked(
            runner,
            [
                str(release / "venv/bin/python"),
                "-I",
                "-B",
                "-c",
                "import sys; from vhotplug.config import Config; "
                "c=Config(sys.argv[1]); "
                "assert c.get_all_vms()[0]['usbHostDevice'] is True",
                str(paths[2]),
            ],
            label="installed config validation",
        )
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def backup_targets(paths, candidates, args, policy):
    backup = args.install_dir / "backups" / uuid.uuid4().hex
    policy.mkdir(backup)
    metadata = json.loads(candidates["metadata"])
    metadata["backup"] = backup.name
    candidates["metadata"] = (json.dumps(metadata, indent=2) + "\n").encode()
    originals, entries = {}, {}
    for key, path in paths.items():
        saved = snapshot(path, policy)
        originals[key] = saved
        entry = None
        if saved is not None:
            atomic_write(backup / key, saved["data"], policy, saved["mode"], (saved["uid"], saved["gid"]))
            entry = {name: saved[name] for name in ("hash", "mode", "uid", "gid")}
        entries[key] = {"before": entry, "after": digest(candidates[key])}
    manifest = {"version": 1, "targets": {key: str(path) for key, path in paths.items()}, "entries": entries}
    atomic_write(backup / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode(), policy)
    return backup, originals


def apply(args, *, runner=run, policy=None, system_root=Path("/"), activate_result=False):
    policy = policy or PathPolicy()
    if not args.port or not args.wheelhouse:
        raise SetupError("apply requires explicit --port and prebuilt --wheelhouse")
    args.compose = discover_compose(args, runner)
    paths = targets(args, system_root)
    for path in (*paths.values(), args.install_dir, args.socket_dir):
        policy.check(path)
    if len(args.install_dir.parts) < 3 or len(args.socket_dir.parts) < 3:
        raise SetupError("install and socket directories must be dedicated subdirectories")
    python = args.python.resolve(strict=True)
    policy.check(python)
    version = checked(
        runner,
        [str(python), "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
        label="Python version validation",
    ).strip()
    if not re.fullmatch(r"[0-9]+\.[0-9]+", version) or tuple(map(int, version.split("."))) < (3, 13):
        raise SetupError("daemon interpreter must be Python >=3.13")
    policy.mkdir(args.install_dir)
    lock_path = args.install_dir / ".installer.lock"
    policy.check(lock_path)
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        previous_umask = os.umask(0o077)
        try:
            result = apply_locked(args, paths, python, runner, policy)
            if activate_result:
                print(
                    "Saved installation",
                    "unchanged" if result["unchanged"] else "readback verified",
                    "backup:",
                    result["backup"] or "existing backup retained",
                )
                # Keep the same deployment lock through opt-in activation:
                # another apply/rollback must not change its files underneath it.
                return activate(args, result, runner=runner, policy=policy, system_root=system_root)
            return result
        finally:
            os.umask(previous_umask)


def validate_local_wheel(data):
    """--no-index is insufficient: direct URLs bypass indexes, so reject them."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = [item for item in archive.infolist() if item.filename.endswith(".dist-info/METADATA")]
            if (
                len(entries) != 1
                or not re.fullmatch(r"[A-Za-z0-9_.+-]+\.dist-info/METADATA", entries[0].filename)
                or entries[0].file_size > 1024 * 1024
            ):
                raise SetupError("invalid or oversized wheel dependency metadata")
            metadata = BytesParser().parsebytes(archive.read(entries[0]), headersonly=True)
            if metadata.defects:
                raise SetupError("malformed wheel dependency metadata")
            for requirement in metadata.get_all("Requires-Dist", []):
                if "@" in requirement or "://" in requirement:
                    raise SetupError(
                        "direct dependency references are forbidden; provide ordinary local wheel dependencies"
                    )
    except (zipfile.BadZipFile, RuntimeError, ValueError) as error:
        raise SetupError("invalid wheel dependency metadata") from error


def apply_locked(args, paths, python, runner, policy):
    previous = installed_metadata(paths["metadata"], args, policy)
    plan_args = copy.copy(args)
    if previous and previous.get("compose_hash") == digest(args.compose.read_bytes()):
        plan_args.remove_static_id = [
            identifier
            for identifier in args.remove_static_id
            if identifier not in previous.get("approved_removed_ids", [])
        ]
    plan = make_plan(plan_args, runner)
    pending = plan["compose_changed"] or bool(previous and previous.get("pending_vm_restart"))
    if args.socket_dir.exists() and stat.S_IMODE(args.socket_dir.stat().st_mode) != 0o700:
        raise SetupError("existing socket directory must already have mode 0700")
    wheels = sorted(args.wheelhouse.glob("*.whl"))
    if not wheels or not any(path.name.startswith("vhotplug-") for path in wheels):
        raise SetupError("wheelhouse must contain vhotplug and all dependency wheels")
    wheel_data = {}
    for path in wheels:
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r"[A-Za-z0-9_.+-]+\.whl", path.name):
            raise SetupError("unsafe wheelhouse entry")
        data = path.read_bytes()
        validate_local_wheel(data)
        wheel_data[path.name] = data
    fingerprint = digest(json.dumps({name: digest(data) for name, data in wheel_data.items()}).encode())
    if previous and previous.get("wheel_hash") == fingerprint and previous.get("python") == str(python):
        release = args.install_dir / "releases" / previous["release"]
        candidates = {
            "compose": plan["compose"],
            "config": plan["config"],
            "unit": unit_content(args, release),
            "tmpfiles": f"d {args.socket_dir} 0700 root root -\n".encode(),
            "metadata": paths["metadata"].read_bytes(),
        }
        if all(
            path.exists()
            and path.read_bytes() == candidates[key]
            and (key not in ("config", "metadata") or stat.S_IMODE(path.stat().st_mode) == 0o600)
            for key, path in paths.items()
        ):
            return {"unchanged": True, "backup": None, "pending_vm_restart": previous.get("pending_vm_restart", False)}
    if service_state(args, runner):
        raise SetupError("existing daemon is active: refuse replacement; stop it separately with explicit approval")
    release = args.install_dir / "releases" / uuid.uuid4().hex
    policy.mkdir(release)
    backup = None
    try:
        immutable_wheels = release / "wheelhouse"
        policy.mkdir(immutable_wheels)
        for name, data in wheel_data.items():
            atomic_write(immutable_wheels / name, data, policy, 0o400)
        immutable_wheels.chmod(0o500)
        checked(runner, [str(python), "-I", "-m", "venv", "--copies", str(release / "venv")], label="venv install")
        checked(
            runner,
            [
                str(release / "venv/bin/python"),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-index",
                "--no-cache-dir",
                "--only-binary=:all:",
                "--find-links",
                str(immutable_wheels),
                "vhotplug",
            ],
            label="wheel install",
        )
        checked(
            runner,
            [str(release / "venv/bin/python"), "-I", "-B", "-m", "vhotplug", "--help"],
            label="installed daemon validation",
        )
        metadata = {
            "release": release.name,
            "tree_hash": tree_digest(release, policy),
            "wheel_hash": fingerprint,
            "python": str(python),
            "pending_vm_restart": pending,
            "compose_hash": digest(plan["compose"]),
            "approved_removed_ids": sorted(
                set(args.remove_static_id) | set(previous.get("approved_removed_ids", []) if previous else [])
            ),
        }
        candidates = {
            "compose": plan["compose"],
            "config": plan["config"],
            "unit": unit_content(args, release),
            "tmpfiles": f"d {args.socket_dir} 0700 root root -\n".encode(),
            "metadata": (json.dumps(metadata, indent=2) + "\n").encode(),
        }
        validate_candidates(args, candidates, release, runner, policy)
        # Validation temporary files are gone; verify immutable release again.
        if tree_digest(release, policy) != metadata["tree_hash"]:
            raise SetupError("installed release changed during validation")
        backup, originals = backup_targets(paths, candidates, args, policy)
        policy.mkdir(args.socket_dir)
        if stat.S_IMODE(args.socket_dir.stat().st_mode) != 0o700:
            raise SetupError("existing socket directory must already have mode 0700")
        try:
            for key, path in paths.items():
                saved = originals[key]
                if (
                    saved is not None
                    and saved["data"] == candidates[key]
                    and (key not in ("config", "metadata") or saved["mode"] == 0o600)
                ):
                    continue
                mode = (
                    saved["mode"] if key == "compose" and saved else 0o600 if key in ("config", "metadata") else 0o644
                )
                owner = (saved["uid"], saved["gid"]) if saved else None
                atomic_write(path, candidates[key], policy, mode, owner)
            for key, path in paths.items():
                if snapshot(path, policy)["data"] != candidates[key]:
                    raise SetupError("deployment readback failed")
            checked(runner, ["systemctl", "daemon-reload"], label="systemd daemon-reload")
        except BaseException:
            for key, path in paths.items():
                restore(path, originals[key], policy)
            # Reload restored units, but do not hide the original failure.
            runner(["systemctl", "daemon-reload"])
            raise
        # Service/VM activation is explicitly outside the file transaction.
        # A failed activation leaves valid saved files and a guarded backup.
        return {"unchanged": False, "backup": backup, "pending_vm_restart": pending}
    except BaseException:
        if backup is None:
            immutable_wheels = release / "wheelhouse"
            if immutable_wheels.exists():
                immutable_wheels.chmod(0o700)
            shutil.rmtree(release)
        raise


def rollback(args, backup, *, runner=run, policy=None, system_root=Path("/")):
    policy = policy or PathPolicy()
    args.compose = discover_compose(args, runner)
    paths = targets(args, system_root)
    expected_parent = args.install_dir / "backups"
    if backup.parent != expected_parent or not re.fullmatch(r"[a-f0-9]{32}", backup.name):
        raise SetupError("rollback requires an installer backup under install-dir/backups")
    for path in (*paths.values(), backup, backup / "manifest.json"):
        policy.check(path)
    policy.check(args.install_dir / ".installer.lock")
    with (args.install_dir / ".installer.lock").open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        manifest = json.loads((backup / "manifest.json").read_bytes())
        if (
            manifest.get("version") != 1
            or manifest.get("targets") != {key: str(path) for key, path in paths.items()}
            or set(manifest.get("entries", {})) != set(paths)
        ):
            raise SetupError("backup target mismatch; arbitrary manifest restore is forbidden")
        installed_metadata(paths["metadata"], args, policy)
        saved, live = {}, {}
        for key, path in paths.items():
            live[key] = snapshot(path, policy)
            entry = manifest["entries"][key]
            if live[key] is None or live[key]["hash"] != entry["after"]:
                raise SetupError("rollback refused: deployed file hash drift")
            before = entry["before"]
            if before is None:
                saved[key] = None
                continue
            prior = snapshot(backup / key, policy)
            if prior is None or any(prior[field] != before.get(field) for field in ("hash", "mode", "uid", "gid")):
                raise SetupError("rollback refused: backup hash/metadata drift")
            saved[key] = prior
        if saved["metadata"] is not None:
            installed_metadata(backup / "metadata", args, policy)
        if service_state(args, runner):
            raise SetupError("daemon is active: stop it separately with explicit approval before rollback")
        try:
            for key, path in paths.items():
                restore(path, saved[key], policy)
            for key, path in paths.items():
                actual = snapshot(path, policy)
                if (actual is None) != (saved[key] is None) or (actual and actual["hash"] != saved[key]["hash"]):
                    raise SetupError("rollback readback failed")
            checked(runner, ["systemctl", "daemon-reload"], label="rollback daemon-reload")
        except BaseException:
            for key, path in paths.items():
                restore(path, live[key], policy)
            runner(["systemctl", "daemon-reload"])
            raise
    return {"backup": backup, "pending_vm_restart": True}


def activate(args, result, *, runner=run, policy=None, system_root=Path("/"), live_check=None):
    """Opt-in activation is not reversible: never disguise its failure as rollback."""
    policy = policy or PathPolicy()
    if result["pending_vm_restart"] and not args.allow_vm_restart:
        return result
    if result["pending_vm_restart"]:
        print("WARNING: Windows recreation approved; save guest work before using --allow-vm-restart.")
        checked(
            runner,
            [
                "docker",
                "compose",
                "-f",
                str(args.compose),
                "up",
                "-d",
                "--no-deps",
                "--pull",
                "never",
                "--no-build",
                args.service,
            ],
            cwd=args.compose.parent,
            label="approved Windows recreation",
        )
    if result["pending_vm_restart"] or args.start_service:
        live_ok = live_capability(args, runner, policy) if live_check is None else live_check()
        if not live_ok:
            raise SetupError("live QMP capability/readback failed; saved files remain; daemon not started")
    if result["pending_vm_restart"]:
        paths = targets(args, system_root)
        metadata = installed_metadata(paths["metadata"], args, policy)
        name = metadata.get("backup", "")
        if not re.fullmatch(r"[a-f0-9]{32}", name):
            raise SetupError("invalid activation backup metadata")
        manifest_path = args.install_dir / "backups" / name / "manifest.json"
        old_metadata = snapshot(paths["metadata"], policy)
        old_manifest = snapshot(manifest_path, policy)
        manifest = json.loads(old_manifest["data"])
        if (
            manifest.get("targets") != {key: str(path) for key, path in paths.items()}
            or manifest["entries"]["metadata"]["after"] != old_metadata["hash"]
        ):
            raise SetupError("activation backup drift")
        metadata["pending_vm_restart"] = False
        data = (json.dumps(metadata, indent=2) + "\n").encode()
        manifest["entries"]["metadata"]["after"] = digest(data)
        try:
            atomic_write(paths["metadata"], data, policy)
            atomic_write(manifest_path, (json.dumps(manifest, indent=2) + "\n").encode(), policy)
            if paths["metadata"].read_bytes() != data or json.loads(manifest_path.read_bytes()) != manifest:
                raise SetupError("activation metadata readback failed")
        except BaseException:
            restore(paths["metadata"], old_metadata, policy)
            restore(manifest_path, old_manifest, policy)
            raise
        result["pending_vm_restart"] = False
    if args.start_service:
        checked(runner, ["systemctl", "restart", args.unit_name], label="approved daemon restart")
        if not service_state(args, runner):
            raise SetupError("daemon did not become active; saved files and backup retained")
    if args.enable_autostart:
        checked(runner, ["systemctl", "enable", args.unit_name], label="approved autostart enable")
        state = checked(runner, ["systemctl", "is-enabled", args.unit_name], label="autostart readback").strip()
        if state != "enabled":
            raise SetupError("autostart readback failed")
    return result


def qmp_readonly(args, policy):
    policy.check(args.socket_dir)
    if stat.S_IMODE(args.socket_dir.stat().st_mode) != 0o700:
        raise SetupError("QMP directory must be root-controlled mode 0700")
    path = args.socket_dir / "qmp.sock"
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != policy.uid:
        raise SetupError("QMP endpoint is not a root-owned Unix socket")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(path))
            with connection.makefile("rwb") as stream:

                def receive(identifier=None):
                    for _ in range(32):
                        line = stream.readline(1024 * 1024)
                        if not line or len(line) >= 1024 * 1024:
                            raise SetupError("invalid or oversized QMP response")
                        response = json.loads(line)
                        if identifier is None or response.get("id") == identifier:
                            if "error" in response:
                                raise SetupError("read-only QMP query failed (details withheld)")
                            return response
                    raise SetupError("QMP event limit exceeded")

                if "QMP" not in receive():
                    raise SetupError("endpoint is not QMP")
                sequence = 0

                def query(command, arguments=None):
                    nonlocal sequence
                    sequence += 1
                    request = {"execute": command, "id": sequence}
                    if arguments is not None:
                        request["arguments"] = arguments
                    stream.write((json.dumps(request) + "\n").encode())
                    stream.flush()
                    return receive(sequence)["return"]

                query("qmp_capabilities")
                if query("query-status").get("status") != "running":
                    raise SetupError("Windows QEMU is not running")
                properties = query("device-list-properties", {"typename": "usb-host"})
                if not any(prop.get("name") == "hostdevice" for prop in properties):
                    raise SetupError("running QEMU usb-host lacks hostdevice capability")
                nodes = []
                for item in query("qom-list", {"path": "/machine/peripheral"}):
                    identifier = item.get("name", "")
                    if re.fullmatch(r"usb[0-9]+", identifier):
                        node = query("qom-get", {"path": f"/machine/peripheral/{identifier}", "property": "hostdevice"})
                        if isinstance(node, str) and re.fullmatch(r"/dev/bus/usb/[0-9]{3}/[0-9]{3}", node):
                            nodes.append(node)
                return nodes
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SetupError("QMP is unavailable or malformed; no devices were added/deleted") from error


def inspect_running(args, runner):
    identifiers = checked(
        runner,
        ["docker", "compose", "-f", str(args.compose), "ps", "-q", args.service],
        cwd=args.compose.parent,
        label="running container discovery",
    ).split()
    if len(identifiers) != 1 or not re.fullmatch(r"[a-f0-9]{12,64}", identifiers[0]):
        raise SetupError("running selected container is missing or ambiguous")
    template = (
        '{"status":{{json .State.Status}},"env":{{json .Config.Env}},'
        '"mounts":{{json .Mounts}},"rules":{{json .HostConfig.DeviceCgroupRules}},'
        '"compose":{{json (index .Config.Labels "com.docker.compose.project.config_files")}}}'
    )
    data = json.loads(
        checked(
            runner, ["docker", "inspect", "--format", template, identifiers[0]], label="running container inspection"
        )
    )
    if data.get("status") != "running" or data.get("compose") != str(args.compose):
        raise SetupError("running container state/Compose labels disagree with saved configuration")
    env, _ = environment({"environment": data.get("env")})
    if env.get("QMP") != QMP:
        raise SetupError("running QMP environment differs; VM restart pending")
    for source, target in (("/dev/bus/usb", "/dev/bus/usb"), (str(args.socket_dir), "/qmp")):
        matches = [mount for mount in data.get("mounts", []) if mount.get("Destination") == target]
        if len(matches) != 1 or matches[0].get("Source") != source or matches[0].get("RW") is not True:
            raise SetupError("running USB/QMP bind mount differs; VM restart pending")
    if "c 189:* rwm" not in (data.get("rules") or []):
        raise SetupError("running container lacks required USB cgroup rule")
    # Reanalyze live selectors after Compose interpolation. Literal dollar
    # characters are escaped again for the parser, never shell-evaluated.
    if "ARGUMENTS" in env:
        live_args = copy.copy(args)
        live_args.remove_static_id = []
        patch_arguments(env["ARGUMENTS"].replace("$", "$$"), live_args)
    return True


def live_capability(args, runner, policy):
    inspect_running(args, runner)
    qmp_readonly(args, policy)
    return True


def topology(branches, sysfs=Path("/sys/bus/usb/devices")):
    result = []
    if not sysfs.is_dir():
        return result
    for path in sorted(sysfs.iterdir()):
        match = re.fullmatch(r"([0-9]+)-([0-9]+)(?:\.[0-9]+)*", path.name)
        if not match or (branches and (int(match[1]), int(match[2])) not in branches):
            continue
        try:
            bus = int((path / "busnum").read_text())
            address = int((path / "devnum").read_text())
            device_class = int((path / "bDeviceClass").read_text().strip(), 16)
            is_hub = device_class == 9 or any(
                int((interface / "bInterfaceClass").read_text().strip(), 16) == 9
                for interface in path.glob(f"{path.name}:*")
                if (interface / "bInterfaceClass").is_file()
            )
            result.append({"topology": path.name, "node": f"/dev/bus/usb/{bus:03d}/{address:03d}", "hub": is_hub})
        except (OSError, ValueError):
            continue
    return result


def node_owners(nodes, proc_root):
    owners = {node: {"pids": [], "qemu_pids": []} for node in nodes}
    for process in proc_root.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            executable = (process / "exe").resolve().name
            for descriptor in (process / "fd").iterdir():
                link = os.readlink(descriptor)
                if link in owners:
                    pid = int(process.name)
                    owners[link]["pids"].append(pid)
                    if executable.startswith("qemu-system"):
                        owners[link]["qemu_pids"].append(pid)
        except OSError:
            continue
    return {node: {kind: sorted(set(pids)) for kind, pids in groups.items()} for node, groups in owners.items()}


def verify(
    args, *, runner=run, policy=None, system_root=Path("/"), sysfs=Path("/sys/bus/usb/devices"), proc_root=Path("/proc")
):
    policy = policy or PathPolicy()
    args.compose = discover_compose(args, runner)
    paths = targets(args, system_root)
    config_snapshot = snapshot(paths["config"], policy)
    if config_snapshot is None or config_snapshot["mode"] != 0o600:
        raise SetupError("deployed config missing or not mode 0600")
    config = json.loads(config_snapshot["data"])
    saved_selection = config["usbPassthrough"][0]
    saved_ports = saved_selection["allow"]
    if not args.port:
        if not saved_ports or any(
            set(rule) != {"bus", "port"}
            or type(rule["bus"]) is not int
            or type(rule["port"]) is not int
            or min(rule.values()) < 1
            for rule in saved_ports
        ):
            raise SetupError("saved USB port mapping is incomplete")
        args.port = sorted({(rule["bus"], rule["port"]) for rule in saved_ports})
    expected_args = copy.copy(args)
    expected_args.socket_label = saved_selection["description"]
    # The attach mode is a deployment choice, like the label: verify what was saved
    expected_args.auto_attach = saved_selection.get("autoAttach") is True
    if not isinstance(saved_selection["description"], str) or json.dumps(config, sort_keys=True) != json.dumps(
        make_config(expected_args), sort_keys=True
    ):
        raise SetupError("deployed config differs from complete restricted direct-node settings")
    plan_args = copy.copy(expected_args)
    plan_args.remove_static_id = []
    if make_plan(plan_args, runner)["compose_changed"]:
        raise SetupError("saved Compose is incomplete")
    metadata = installed_metadata(paths["metadata"], args, policy)
    if metadata is None:
        raise SetupError("installed release metadata missing")
    release = args.install_dir / "releases" / metadata["release"]
    for key, content in (
        ("unit", unit_content(args, release)),
        ("tmpfiles", f"d {args.socket_dir} 0700 root root -\n".encode()),
    ):
        saved = snapshot(paths[key], policy)
        if saved is None or saved["data"] != content:
            raise SetupError("saved service/tmpfiles incomplete or changed")
    if not service_state(args, runner):
        raise SetupError("configured but daemon is not active; live verification incomplete")
    fragment = checked(
        runner,
        ["systemctl", "show", args.unit_name, "--property=FragmentPath", "--value"],
        label="loaded service readback",
    ).strip()
    reload_needed = checked(
        runner,
        ["systemctl", "show", args.unit_name, "--property=NeedDaemonReload", "--value"],
        label="loaded unit state readback",
    ).strip()
    if fragment != str(paths["unit"]) or reload_needed != "no":
        raise SetupError("loaded service differs from saved unit")
    pid = checked(
        runner,
        ["systemctl", "show", args.unit_name, "--property=MainPID", "--value"],
        label="running daemon pid readback",
    ).strip()
    if not re.fullmatch(r"[1-9][0-9]*", pid):
        raise SetupError("running daemon pid unavailable")
    words = (proc_root / pid / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
    intended = [
        str(release / "venv/bin/python"),
        "-I",
        "-B",
        "-m",
        "vhotplug",
        "--attach-connected",
        "--config",
        str(args.config),
    ]
    if words != intended or (proc_root / pid / "exe").resolve() != (release / "venv/bin/python").resolve():
        raise SetupError("running daemon executable/config does not match installed release")
    enabled = runner(["systemctl", "is-enabled", args.unit_name])
    if enabled.returncode not in (0, 1) or enabled.stdout.strip() not in ("enabled", "disabled", "static"):
        raise SetupError("autostart state readback unavailable")
    inspect_running(args, runner)
    attached = qmp_readonly(args, policy)
    current = topology(args.port, sysfs)
    leaves = [entry["node"] for entry in current if not entry["hub"]]
    owners = node_owners(leaves, proc_root)
    return {
        "configured": True,
        "running": True,
        "hostdevice_capability": True,
        "autostart": enabled.stdout.strip(),
        "saved_pending_restart": metadata["pending_vm_restart"],
        "attach_mode": "automatic" if expected_args.auto_attach else "manual",
        "usb_tag": usb_tag(args),
        "current_usb_nodes": current,
        "attached_current_nodes": sorted(set(leaves) & set(attached)),
        "node_owners": owners,
        "current_node_qemu_ownership_verified": bool(leaves)
        and all(node in attached and owners[node]["qemu_pids"] for node in leaves),
        "physical_reconnect_verified": False,
        "guest_function_verified": False,
        "note": "Host readiness only. Validate guest function, leaf/hub unplug-replug, and companion branch physically.",
    }


def main(argv=None, *, runner=run, policy=None, system_root=Path("/"), euid=None):
    try:
        args = parse_args(argv)
        if args.apply or args.rollback:
            if (os.geteuid() if euid is None else euid) != 0 or not args.yes:
                raise SetupError("apply/rollback requires root and explicit --yes; no escalation is attempted")
            if args.rollback:
                if args.start_service or args.enable_autostart or args.allow_vm_restart:
                    raise SetupError("rollback restores files only; activation flags are unsupported for rollback")
                result = rollback(args, args.rollback, runner=runner, policy=policy, system_root=system_root)
                print("Rollback file restoration verified. Windows restart may be pending; boot state unchanged.")
            else:
                result = apply(args, runner=runner, policy=policy, system_root=system_root, activate_result=True)
                if result["pending_vm_restart"]:
                    print("Windows restart pending: daemon not started/autostart not enabled by this command.")
                    print(
                        "Save Windows work, then explicitly approve --allow-vm-restart; --start-service and --enable-autostart are separate."
                    )
                else:
                    print(
                        "Saved/running states differ unless independently verified with --verify. Existing boot state preserved."
                    )
            return 0
        if args.verify:
            print(json.dumps(verify(args, runner=runner, policy=policy, system_root=system_root), indent=2))
            return 0
        plan = make_plan(args, runner)
        print("read-only plan: selected service", args.service)
        print("Compose:", args.compose, "change required:", plan["compose_changed"])
        print(
            "Approved branches:",
            ", ".join(f"{bus}:{root}" for bus, root in args.port) or "none; --port required for apply",
        )
        print("Socket label:", args.socket_label, "QMP directory:", args.socket_dir)
        print(
            "Attach mode:",
            "automatic on plug-in"
            if args.auto_attach
            else f"manual (vhotplugcli usb attach/detach --tag {usb_tag(args)})",
            "API:",
            API_SOCKET,
            "(root-only Unix socket)",
        )
        print("Installation:", args.install_dir, "config:", args.config, "unit:", args.unit_name)
        print("Observed sysfs topology (not a physical socket/companion mapping):", json.dumps(topology(args.port)))
        print(
            "No USB2/USB3 companion mapping inferred. API is root-only Unix; authorization/PCI/evdev stay restricted."
        )
        print("Apply requires root-controlled target ancestry, Python >=3.13, prebuilt --wheelhouse and --yes.")
        return 0
    except Exception as error:
        message = (
            str(error) if isinstance(error, SetupError) else "invalid input or inaccessible file (details withheld)"
        )
        print("Error:", message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
