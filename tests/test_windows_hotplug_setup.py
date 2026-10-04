"""Installer tests: real filesystem/CLI, simulated external privileged commands."""

import fcntl
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "scripts/windows_hotplug_setup.py"
ENTRY = REPO / "scripts/configure_windows_hotplug.sh"


@pytest.fixture
def setup_module():
    assert HELPER.exists(), "installer helper is not implemented"
    spec = importlib.util.spec_from_file_location("windows_hotplug_setup", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sandbox():
    with tempfile.TemporaryDirectory(prefix="hotplug-test-", dir=tempfile.gettempdir()) as directory:
        yield Path(directory)


def compose_file(sandbox, environment=None):
    path = sandbox / "compose.yaml"
    doc = {
        "services": {
            "windows": {
                "image": "example/windows:reviewed",
                "environment": environment
                or {
                    "PASSWORD": "secret-not-for-output",
                    "VERSION": "${VERSION:-11}",
                    "ARGUMENTS": '-name "VM with spaces"  -device virtio-net-pci,id=net0',
                },
                "volumes": ["./storage:/storage"],
                "ports": ["3389:3389"],
            },
            "other": {"image": "example/unrelated", "environment": {"SECRET": "unchanged"}},
        }
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path, doc


def options(module, compose, sandbox, *extra):
    return module.parse_args(
        [
            "--compose",
            str(compose),
            "--port",
            "17:3",
            "--socket-label",
            "Test socket",
            "--socket-dir",
            str(sandbox / "run/socket"),
            "--install-dir",
            str(sandbox / "opt/install"),
            "--config",
            str(sandbox / "etc/config.json"),
            *extra,
        ]
    )


def test_bash_help_is_real_and_offline_plan_does_not_write(sandbox):
    assert ENTRY.exists(), "executable Bash entry is not implemented"
    env = dict(os.environ, VHOTPLUG_INSTALLER_PYTHON=sys.executable)
    help_result = subprocess.run([str(ENTRY), "--help"], env=env, capture_output=True, text=True, check=False)
    assert help_result.returncode == 0
    assert "--rollback" in help_result.stdout
    compose, _ = compose_file(sandbox)
    before = compose.read_bytes()
    result = subprocess.run(
        [
            str(ENTRY),
            "--compose",
            str(compose),
            "--port",
            "17:3",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "read-only" in result.stdout
    assert "secret-not-for-output" not in result.stdout + result.stderr
    assert compose.read_bytes() == before
    assert sorted(p.name for p in sandbox.iterdir()) == ["compose.yaml"]


def test_plan_preserves_unrelated_semantics_and_argument_bytes(setup_module, sandbox):
    compose, original = compose_file(sandbox)
    plan = setup_module.make_plan(options(setup_module, compose, sandbox))
    updated = yaml.safe_load(plan["compose"])
    service = updated["services"]["windows"]
    assert updated["services"]["other"] == original["services"]["other"]
    assert service["environment"]["ARGUMENTS"] == original["services"]["windows"]["environment"]["ARGUMENTS"]
    assert service["environment"]["PASSWORD"] == "secret-not-for-output"  # noqa: S105 - test sentinel
    assert service["environment"]["QMP"] == "unix:/qmp/qmp.sock,server=on,wait=off"
    assert service["volumes"][0] == "./storage:/storage"
    assert "/dev/bus/usb:/dev/bus/usb" in service["volumes"]
    assert service["device_cgroup_rules"] == ["c 189:* rwm"]
    config = json.loads(plan["config"])
    assert config["vms"][0]["usbHostDevice"] is True
    assert config["usbPassthrough"][0]["allow"] == [{"bus": 17, "port": 3}]
    assert config["usbPassthrough"][0]["deny"] == [{"deviceClass": 9}, {"interfaceClass": 9}]
    assert config["general"] == {"api": {"enable": False}, "persistency": False}
    assert setup_module.make_plan(options(setup_module, compose, sandbox))["compose"] == plan["compose"]


@pytest.mark.parametrize(
    "arguments",
    [
        "-device usb-host,id=phone,hostbus=17,hostport=3.2",
        "-device usb-host,id=phone,vendorid=0x1234,productid=0x0001",
        "-device usb-host,id=phone,hostbus=17,hostaddr=7",
        "-device usb-host,id=phone,unknown=1",
        "-qmp tcp:0.0.0.0:4444,server=on",
        "-device ${STATIC_USB}",
    ],
)
def test_reject_unsafe_or_unresolved_arguments(setup_module, sandbox, arguments):
    compose, _ = compose_file(sandbox, {"ARGUMENTS": arguments})
    with pytest.raises(setup_module.SetupError):
        setup_module.make_plan(options(setup_module, compose, sandbox))


@pytest.mark.parametrize("bus", ["0", "00"])
def test_zero_hostbus_is_wildcard_not_disjoint(setup_module, sandbox, bus):
    compose, _ = compose_file(sandbox)
    args = options(setup_module, compose, sandbox)
    arguments = f"-device usb-host,id=phone,hostbus={bus},hostport=3,vendorid=0x1234"
    with pytest.raises(setup_module.SetupError, match="overlaps or cannot be resolved"):
        setup_module.patch_arguments(arguments, args)
    args.remove_static_id = ["phone"]
    assert setup_module.patch_arguments(arguments, args) == ""
    args.remove_static_id = []
    disjoint = arguments.replace("hostport=3", "hostport=2")
    assert setup_module.patch_arguments(disjoint, args) == disjoint
    concrete_bus = arguments.replace(f"hostbus={bus}", "hostbus=19")
    assert setup_module.patch_arguments(concrete_bus, args) == concrete_bus


def test_remove_only_explicit_static_id_preserving_other_argument_bytes(setup_module, sandbox):
    arguments = '-name "a $$literal"  -device usb-host,id=phone,hostbus=17,hostport=3.2   -device usb-host,id=keep,hostbus=19,hostport=2'
    compose, _ = compose_file(sandbox, [f"ARGUMENTS={arguments}", "PASSWORD=secret-not-for-output", "PASSTHROUGH"])
    args = options(setup_module, compose, sandbox, "--remove-static-id", "phone")
    updated = yaml.safe_load(setup_module.make_plan(args)["compose"])
    env = updated["services"]["windows"]["environment"]
    assert 'ARGUMENTS=-name "a $$literal"     -device usb-host,id=keep,hostbus=19,hostport=2' in env
    assert "PASSTHROUGH" in env
    compose.write_text(yaml.safe_dump(updated))
    args.remove_static_id = []
    assert setup_module.make_plan(args)["compose_changed"] is False


@pytest.mark.parametrize(
    "text",
    [
        "services: {windows: {image: a, image: b}}",
        "services: {windows: &w {image: a}, other: *w}",
        "services: {windows: {<<: {image: a}}}",
        "services: {windows: {environment: {QMP: 'tcp:localhost:4'}}}",
        "services: {windows: {volumes: ['/wrong:/qmp']}}",
        "services: {windows: {volumes: ['/dev/bus/usb:/dev/bus/usb:ro']}}",
        "services: {windows: {volumes: ['/wrong:/dev']}}",
        "services: {windows: {command: 'qemu -qmp tcp::4444'}}",
    ],
)
def test_reject_yaml_or_mount_ambiguities(setup_module, sandbox, text):
    compose = sandbox / "compose.yaml"
    compose.write_text(text)
    with pytest.raises(setup_module.SetupError):
        setup_module.make_plan(options(setup_module, compose, sandbox))


def test_long_bind_syntax_is_preserved(setup_module, sandbox):
    compose, doc = compose_file(sandbox)
    mount = {
        "type": "bind",
        "source": str(sandbox / "run/socket"),
        "target": "/qmp",
        "bind": {"create_host_path": False},
    }
    doc["services"]["windows"]["volumes"].append(mount)
    compose.write_text(yaml.safe_dump(doc))
    updated = yaml.safe_load(setup_module.make_plan(options(setup_module, compose, sandbox))["compose"])
    assert mount in updated["services"]["windows"]["volumes"]


class SimulatedCommands:
    """Only Docker/systemd/pip/python execution is simulated, not installer logic."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
        self.active = False
        self.enabled = False

    def __call__(self, command, *, cwd=None):
        self.calls.append((list(map(str, command)), cwd))
        words = list(map(str, command))
        if self.fail and self.fail(words):
            return subprocess.CompletedProcess(words, 1, "credential-leak", "credential-leak")
        if "is-active" in words:
            return subprocess.CompletedProcess(
                words, 0 if self.active else 3, "active\n" if self.active else "inactive\n", ""
            )
        if "is-enabled" in words:
            return subprocess.CompletedProcess(
                words, 0 if self.enabled else 1, "enabled\n" if self.enabled else "disabled\n", ""
            )
        if "restart" in words:
            self.active = True
        if "enable" in words:
            self.enabled = True
        if "venv" in words:
            venv = Path(words[-1])
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").write_text("simulated installed interpreter")
            (venv / "bin/python").chmod(0o755)
        stdout = "3.14\n" if "-c" in words else ""
        return subprocess.CompletedProcess(words, 0, stdout, "")


def write_test_wheel(path, *, requirement=None, note="initial"):
    """Synthetic ZIP metadata for mocked installs; the real smoke uses real wheels."""
    metadata = f"Metadata-Version: 2.1\nName: vhotplug\nVersion: 1.0\nSummary: SIMULATION {note}\n"
    if requirement is not None:
        metadata += f"Requires-Dist: {requirement}\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("vhotplug-1.0.dist-info/METADATA", metadata)


def deployment(module, sandbox, *extra):
    compose, original = compose_file(sandbox)
    wheelhouse = sandbox / "wheelhouse"
    wheelhouse.mkdir()
    write_test_wheel(wheelhouse / "vhotplug-1.0-py3-none-any.whl")
    interpreter = sandbox / "python"
    interpreter.write_text("SIMULATION: root-controlled interpreter")
    interpreter.chmod(0o755)
    args = options(
        module,
        compose,
        sandbox,
        "--apply",
        "--yes",
        "--wheelhouse",
        str(wheelhouse),
        "--python",
        str(interpreter),
        *extra,
    )
    policy = module.PathPolicy(sandbox, os.getuid())
    return args, policy, original


def test_sandbox_apply_transaction_backup_idempotence_and_rollback(setup_module, sandbox):
    args, policy, original = deployment(setup_module, sandbox)
    commands = SimulatedCommands()
    old = args.compose.read_bytes()
    args.compose.chmod(0o640)
    result = setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    assert result["pending_vm_restart"] is True
    assert result["backup"].is_dir()
    assert args.compose.stat().st_mode & 0o777 == 0o640
    assert args.config.stat().st_mode & 0o777 == 0o600
    assert args.socket_dir.stat().st_mode & 0o777 == 0o700
    assert yaml.safe_load(args.compose.read_text())["services"]["other"] == original["services"]["other"]
    assert not any("up" in cmd or "restart" in cmd or "enable" in cmd for cmd, _ in commands.calls)
    pip = next(cmd for cmd, _ in commands.calls if "pip" in cmd)
    assert "--no-index" in pip
    assert "--no-deps" not in pip
    assert str(args.wheelhouse) not in pip
    compose_validation = next((cmd, cwd) for cmd, cwd in commands.calls if "config" in cmd)
    assert compose_validation[1] == args.compose.parent
    assert Path(compose_validation[0][3]).parent == args.compose.parent
    assert "secret-not-for-output" not in (result["backup"] / "manifest.json").read_text()
    count = len(commands.calls)
    again = setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    assert again["unchanged"] is True
    assert not any("pip" in cmd for cmd, _ in commands.calls[count:])
    setup_module.rollback(args, result["backup"], runner=commands, policy=policy, system_root=sandbox)
    assert args.compose.read_bytes() == old
    assert not args.config.exists()
    assert not (sandbox / "etc/systemd/system" / args.unit_name).exists()
    assert not (args.install_dir / "deployment.json").exists()


@pytest.mark.parametrize("failure", ["pip", "config", "verify", "daemon-reload"])
def test_apply_failure_preserves_original_targets(setup_module, sandbox, failure):
    args, policy, _ = deployment(setup_module, sandbox)
    old = args.compose.read_bytes()
    commands = SimulatedCommands(lambda cmd: failure in cmd)
    with pytest.raises(setup_module.SetupError, match="failed") as error:
        setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    assert "credential-leak" not in str(error.value)
    assert args.compose.read_bytes() == old
    assert not args.config.exists()
    assert not (sandbox / "etc/systemd/system" / args.unit_name).exists()


def test_rollback_detects_drift_before_any_restore(setup_module, sandbox):
    args, policy, _ = deployment(setup_module, sandbox)
    commands = SimulatedCommands()
    result = setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    deployed = args.compose.read_bytes()
    args.config.write_text("drift")
    with pytest.raises(setup_module.SetupError, match="drift"):
        setup_module.rollback(args, result["backup"], runner=commands, policy=policy, system_root=sandbox)
    assert args.compose.read_bytes() == deployed


@pytest.mark.parametrize("kind", ["symlink", "writable-parent", "writable-file", "outside", "no-port", "active"])
def test_apply_security_preflight_refuses_unsafe_targets(setup_module, sandbox, kind):
    args, policy, _ = deployment(setup_module, sandbox)
    commands = SimulatedCommands()
    if kind == "symlink":
        args.config.parent.mkdir(parents=True)
        args.config.symlink_to(args.compose)
    elif kind == "writable-parent":
        args.config.parent.mkdir(parents=True)
        args.config.parent.chmod(0o777)
    elif kind == "writable-file":
        args.compose.chmod(0o666)
    elif kind == "outside":
        args.config = Path("/etc/NEVER-WRITE-test.json")
    elif kind == "no-port":
        args.port = []
    else:
        commands.active = True
    old = args.compose.read_bytes()
    with pytest.raises(setup_module.SetupError):
        setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    assert args.compose.read_bytes() == old
    assert not any("pip" in cmd for cmd, _ in commands.calls)


def test_real_config_validation_subprocess_matches_existing_api(setup_module, sandbox):
    args, policy, _ = deployment(setup_module, sandbox)
    simulated = SimulatedCommands()

    def runner(command, *, cwd=None):
        if "-c" in command and "vhotplug.config" in command[command.index("-c") + 1]:
            return subprocess.run([sys.executable, *command[1:]], cwd=REPO, capture_output=True, text=True, check=False)
        return simulated(command, cwd=cwd)

    setup_module.apply(args, runner=runner, policy=policy, system_root=sandbox)


def test_activation_requires_separate_flags_and_live_check(setup_module, sandbox):
    args, policy, _ = deployment(setup_module, sandbox, "--start-service", "--enable-autostart")
    simulated = SimulatedCommands()
    result = setup_module.apply(args, runner=simulated, policy=policy, system_root=sandbox)
    setup_module.activate(args, result, runner=simulated, policy=policy, system_root=sandbox, live_check=lambda: True)
    assert not any("restart" in cmd or "enable" in cmd or "up" in cmd for cmd, _ in simulated.calls)
    args.allow_vm_restart = True
    setup_module.activate(args, result, runner=simulated, policy=policy, system_root=sandbox, live_check=lambda: True)
    assert any("up" in cmd and "--no-deps" in cmd for cmd, _ in simulated.calls)
    assert simulated.active and simulated.enabled
    assert (
        setup_module.installed_metadata(args.install_dir / "deployment.json", args, policy)["pending_vm_restart"]
        is False
    )


def test_nonroot_apply_refusal_and_unsafe_cli_are_real_subprocesses(sandbox):
    assert os.geteuid() != 0, "test must not run as root"
    env = dict(os.environ, VHOTPLUG_INSTALLER_PYTHON=sys.executable)
    for args in (
        ["--apply", "--yes"],
        ["--socket-dir", "/run/unsafe%name"],
        ["--unit-name", "../oops.service"],
        ["--port", "17:0"],
    ):
        result = subprocess.run([str(ENTRY), *args], env=env, capture_output=True, text=True, check=False)
        assert result.returncode != 0
    assert (
        "root"
        in subprocess.run([str(ENTRY), "--apply", "--yes"], env=env, capture_output=True, text=True, check=False).stderr
    )


class QMPServer:
    """Real Unix socket server; protocol simulation never attaches USB hardware."""

    def __init__(self, path, hostdevice=True):
        self.path = path
        self.hostdevice = hostdevice
        self.commands = []
        self.errors = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        path.chmod(0o600)
        self.server.listen(1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            self.server.settimeout(5)
            connection, _ = self.server.accept()
            with connection, connection.makefile("rwb") as stream:
                stream.write(b'{"QMP": {"version": {}}}\n')
                stream.flush()
                while line := stream.readline():
                    request = json.loads(line)
                    self.commands.append(request)
                    command = request["execute"]
                    response = {
                        "qmp_capabilities": {},
                        "query-status": {"status": "running"},
                        "device-list-properties": [{"name": "hostdevice"}] if self.hostdevice else [],
                        "qom-list": [],
                    }[command]
                    stream.write((json.dumps({"return": response, "id": request["id"]}) + "\n").encode())
                    stream.flush()
        except Exception as error:
            self.errors.append(error)
        finally:
            self.server.close()

    def finish(self):
        self.thread.join(timeout=6)
        assert not self.thread.is_alive()
        assert not self.errors


def live_fixture(module, sandbox, hostdevice=True):
    args, policy, _ = deployment(module, sandbox)
    simulated = SimulatedCommands()
    module.apply(args, runner=simulated, policy=policy, system_root=sandbox)
    simulated.active = True
    metadata = module.installed_metadata(args.install_dir / "deployment.json", args, policy)
    release = args.install_dir / "releases" / metadata["release"]
    proc_root = sandbox / "proc"
    (proc_root / "100/fd").mkdir(parents=True)
    cmdline = [
        str(release / "venv/bin/python"),
        "-I",
        "-B",
        "-m",
        "vhotplug",
        "--attach-connected",
        "--config",
        str(args.config),
    ]
    (proc_root / "100/cmdline").write_bytes(b"\0".join(word.encode() for word in cmdline) + b"\0")
    (proc_root / "100/exe").symlink_to(release / "venv/bin/python")
    sysfs = sandbox / "sys"
    sysfs.mkdir()
    server = QMPServer(args.socket_dir / "qmp.sock", hostdevice)

    def runner(command, *, cwd=None):
        words = list(map(str, command))
        if words[:2] == ["docker", "inspect"]:
            state = {
                "status": "running",
                "env": [f"QMP={module.QMP}", "PASSWORD=never-print"],
                "mounts": [
                    {"Source": "/dev/bus/usb", "Destination": "/dev/bus/usb", "RW": True},
                    {"Source": str(args.socket_dir), "Destination": "/qmp", "RW": True},
                ],
                "rules": ["c 189:* rwm"],
                "compose": str(args.compose),
            }
            return subprocess.CompletedProcess(words, 0, json.dumps(state), "")
        if "ps" in words:
            return subprocess.CompletedProcess(words, 0, "a" * 64 + "\n", "")
        if "show" in words:
            value = (
                "100"
                if "--property=MainPID" in words
                else "no"
                if "--property=NeedDaemonReload" in words
                else str(sandbox / "etc/systemd/system" / args.unit_name)
            )
            return subprocess.CompletedProcess(words, 0, value + "\n", "")
        return simulated(command, cwd=cwd)

    return args, policy, runner, server, sysfs, proc_root


@pytest.mark.parametrize("hostdevice", [True, False])
def test_readonly_verify_checks_saved_running_and_real_qmp_protocol(setup_module, sandbox, hostdevice):
    args, policy, runner, server, sysfs, proc_root = live_fixture(setup_module, sandbox, hostdevice)
    before = {path: path.read_bytes() for path in sandbox.rglob("*") if path.is_file()}
    try:
        if hostdevice:
            report = setup_module.verify(
                args, runner=runner, policy=policy, system_root=sandbox, sysfs=sysfs, proc_root=proc_root
            )
            assert report["configured"] and report["running"]
            assert report["physical_reconnect_verified"] is False
            assert report["guest_function_verified"] is False
        else:
            with pytest.raises(setup_module.SetupError, match="hostdevice"):
                setup_module.verify(
                    args, runner=runner, policy=policy, system_root=sandbox, sysfs=sysfs, proc_root=proc_root
                )
    finally:
        server.finish()
    assert all(path.read_bytes() == data for path, data in before.items())
    assert [cmd["execute"] for cmd in server.commands][:3] == [
        "qmp_capabilities",
        "query-status",
        "device-list-properties",
    ]
    assert not any(cmd["execute"] in ("device_add", "device_del") for cmd in server.commands)


def test_verify_incomplete_saved_config_fails_before_docker(setup_module, sandbox):
    args, policy, _ = deployment(setup_module, sandbox)
    commands = SimulatedCommands()
    setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    doc = json.loads(args.config.read_bytes())
    doc["vms"][0]["usbHostDevice"] = "true"
    args.config.write_text(json.dumps(doc))
    before = len(commands.calls)
    with pytest.raises(setup_module.SetupError):
        setup_module.verify(args, runner=commands, policy=policy, system_root=sandbox)
    assert len(commands.calls) == before


def test_main_apply_and_rollback_execute_sandbox_transaction(setup_module, sandbox, capsys):
    args, policy, _ = deployment(setup_module, sandbox)
    argv = [
        "--compose",
        str(args.compose),
        "--port",
        "17:3",
        "--socket-dir",
        str(args.socket_dir),
        "--install-dir",
        str(args.install_dir),
        "--config",
        str(args.config),
        "--python",
        str(args.python),
        "--wheelhouse",
        str(args.wheelhouse),
        "--apply",
        "--yes",
    ]
    commands = SimulatedCommands()
    assert setup_module.main(argv, runner=commands, policy=policy, system_root=sandbox, euid=0) == 0
    output = capsys.readouterr()
    assert "pending" in output.out
    assert "secret-not-for-output" not in output.out + output.err
    backup = next((args.install_dir / "backups").iterdir())
    argv = [*argv[:-2], "--rollback", str(backup), "--yes"]
    assert setup_module.main(argv, runner=commands, policy=policy, system_root=sandbox, euid=0) == 0
    assert not args.config.exists()


@pytest.mark.parametrize("fault", ["replacement", "readback"])
def test_atomic_replace_or_readback_failure_restores_all_files(setup_module, sandbox, monkeypatch, fault):
    args, policy, _ = deployment(setup_module, sandbox)
    before = args.compose.read_bytes()
    original_write = setup_module.atomic_write
    failed = False

    def write(path, data, *rest, **kwargs):
        nonlocal failed
        if path == args.config and not failed:
            failed = True
            if fault == "replacement":
                raise OSError("SIMULATED: interrupted replacement")
            return original_write(path, b"SIMULATED: corrupted readback", *rest, **kwargs)
        return original_write(path, data, *rest, **kwargs)

    monkeypatch.setattr(setup_module, "atomic_write", write)
    with pytest.raises((OSError, setup_module.SetupError)):
        setup_module.apply(args, runner=SimulatedCommands(), policy=policy, system_root=sandbox)
    assert args.compose.read_bytes() == before
    assert not args.config.exists()
    assert not (sandbox / "etc/systemd/system" / args.unit_name).exists()


@pytest.mark.parametrize("tamper", ["target", "backup-bytes", "manifest-symlink", "package-bytes"])
def test_guarded_rollback_rejects_tampered_artifacts(setup_module, sandbox, tamper):
    args, policy, _ = deployment(setup_module, sandbox)
    commands = SimulatedCommands()
    result = setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    backup = result["backup"]
    live = args.compose.read_bytes()
    if tamper == "target":
        manifest = json.loads((backup / "manifest.json").read_bytes())
        manifest["targets"]["compose"] = str(sandbox / "arbitrary-victim")
        (backup / "manifest.json").write_text(json.dumps(manifest))
    elif tamper == "backup-bytes":
        (backup / "compose").write_bytes(b"tampered")
    elif tamper == "manifest-symlink":
        (backup / "manifest.json").unlink()
        (backup / "manifest.json").symlink_to(args.config)
    else:
        metadata = setup_module.installed_metadata(args.install_dir / "deployment.json", args, policy)
        (args.install_dir / "releases" / metadata["release"] / "venv/bin/python").write_bytes(b"tampered")
    with pytest.raises(setup_module.SetupError):
        setup_module.rollback(args, backup, runner=commands, policy=policy, system_root=sandbox)
    assert args.compose.read_bytes() == live


def test_all_root_python_imports_are_isolated_from_checkout_and_environment(setup_module, sandbox):
    args, policy, _ = deployment(setup_module, sandbox)
    commands = SimulatedCommands()
    setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    for command, _ in commands.calls:
        if "-m" in command or "-c" in command:
            assert "-I" in command
    unit = (sandbox / "etc/systemd/system" / args.unit_name).read_text()
    assert "/venv/bin/python -I -B -m vhotplug" in unit


@pytest.mark.parametrize(
    "setting",
    [
        {"volumes": ["/wrong:/qmp/"]},
        {"volumes": ["/wrong:${TARGET}"]},
        {"volumes": ["/wrong:/other/../qmp"]},
        {"devices": ["/dev/bus/usb/017/002:/dev/bus/usb/017/002"]},
    ],
)
def test_reject_ambiguous_mount_spellings_and_static_node_overlays(setup_module, sandbox, setting):
    compose, document = compose_file(sandbox)
    document["services"]["windows"].update(setting)
    compose.write_text(yaml.safe_dump(document))
    with pytest.raises(setup_module.SetupError):
        setup_module.make_plan(options(setup_module, compose, sandbox))


def test_verify_numeric_true_is_not_boolean_true(setup_module, sandbox):
    args, policy, _ = deployment(setup_module, sandbox)
    commands = SimulatedCommands()
    setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    config = json.loads(args.config.read_bytes())
    config["vms"][0]["usbHostDevice"] = 1
    args.config.write_text(json.dumps(config))
    before = len(commands.calls)
    with pytest.raises(setup_module.SetupError, match="config"):
        setup_module.verify(args, runner=commands, policy=policy, system_root=sandbox)
    assert len(commands.calls) == before


def test_reject_unix_socket_path_overflow_before_writes(setup_module, sandbox):
    compose, _ = compose_file(sandbox)
    with pytest.raises(setup_module.SetupError, match="socket"):
        options(setup_module, compose, sandbox, "--socket-dir", "/run/" + "a" * 110)


def test_real_offline_wheel_install_and_rollback(setup_module, sandbox, monkeypatch):
    """Opt-in prepared wheelhouse: Python/pip/systemd-analyze real, Docker/systemctl simulated."""
    prepared = os.environ.get("VHOTPLUG_TEST_WHEELHOUSE")
    if not prepared:
        pytest.skip("prepare wheels as unprivileged caller and set VHOTPLUG_TEST_WHEELHOUSE for real install smoke")
    args, policy, _ = deployment(setup_module, sandbox)
    args.wheelhouse = Path(prepared)
    args.python.write_text(f"#!/bin/sh\nexec '{sys._base_executable}' \"$@\"\n")
    simulated = SimulatedCommands()
    outside = sandbox / "escaped_install"
    pip_config = sandbox / "redirect_pip.conf"
    pip_config.write_text(f"[install]\ntarget = {outside}\n")
    monkeypatch.setenv("PIP_CONFIG_FILE", str(pip_config))
    monkeypatch.setenv("PIP_TARGET", str(outside))

    def runner(command, *, cwd=None):
        if str(command[0]) in ("docker", "systemctl"):
            return simulated(command, cwd=cwd)
        if "pip" in command:
            # Site configuration must be suppressed as well as inherited config.
            (Path(command[0]).parent.parent / "pip.conf").write_text(pip_config.read_text())
        return setup_module.run(command, cwd=cwd)

    result = setup_module.apply(args, runner=runner, policy=policy, system_root=sandbox)
    assert not outside.exists(), "pip must never redirect installation outside its managed release"
    metadata = setup_module.installed_metadata(args.install_dir / "deployment.json", args, policy)
    release = args.install_dir / "releases" / metadata["release"]
    output = setup_module.checked(
        runner, [str(release / "venv/bin/python"), "-I", "-B", "-c", "import vhotplug.qemulink as q; print(q.__file__)"]
    )
    assert str(release) in output
    assert str(REPO) not in output
    assert setup_module.apply(args, runner=runner, policy=policy, system_root=sandbox)["unchanged"]
    setup_module.rollback(args, result["backup"], runner=runner, policy=policy, system_root=sandbox)
    assert not args.config.exists()


@pytest.mark.parametrize("drift", ["config-mode", "socket-mode"])
def test_idempotent_apply_does_not_ignore_security_permission_drift(setup_module, sandbox, drift):
    args, policy, _ = deployment(setup_module, sandbox)
    commands = SimulatedCommands()
    setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    if drift == "config-mode":
        args.config.chmod(0o644)
        setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
        assert args.config.stat().st_mode & 0o777 == 0o600
    else:
        args.socket_dir.chmod(0o755)
        with pytest.raises(setup_module.SetupError, match="socket"):
            setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)


def test_package_update_preserves_pending_vm_restart(setup_module, sandbox):
    args, policy, _ = deployment(setup_module, sandbox, "--start-service")
    commands = SimulatedCommands()
    setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    write_test_wheel(args.wheelhouse / "vhotplug-1.0-py3-none-any.whl", note="update")
    result = setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    assert result["pending_vm_restart"] is True
    setup_module.activate(args, result, runner=commands, policy=policy, system_root=sandbox)
    assert not commands.active


def test_apply_static_id_removal_is_idempotent_with_same_approved_arguments(setup_module, sandbox):
    args, policy, _ = deployment(setup_module, sandbox, "--remove-static-id", "phone")
    doc = yaml.safe_load(args.compose.read_bytes())
    doc["services"]["windows"]["environment"]["ARGUMENTS"] += " -device usb-host,id=phone,hostbus=17,hostport=3"
    args.compose.write_text(yaml.safe_dump(doc))
    commands = SimulatedCommands()
    setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    assert setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)["unchanged"]


def test_topology_hub_interfaces_and_current_node_owners(setup_module, sandbox):
    sysfs = sandbox / "sys"
    sysfs.mkdir()
    for name, address, device_class in (("17-3", 4, "00"), ("17-3.2", 7, "00"), ("18-1", 3, "00")):
        device = sysfs / name
        device.mkdir()
        (device / "busnum").write_text(name.split("-")[0])
        (device / "devnum").write_text(str(address))
        (device / "bDeviceClass").write_text(device_class)
    interface = sysfs / "17-3/17-3:1.0"
    interface.mkdir()
    (interface / "bInterfaceClass").write_text("09")
    topo = setup_module.topology([(17, 3)], sysfs)
    assert topo == [
        {"topology": "17-3", "node": "/dev/bus/usb/017/004", "hub": True},
        {"topology": "17-3.2", "node": "/dev/bus/usb/017/007", "hub": False},
    ]
    proc = sandbox / "proc"
    for pid, executable in ((200, "qemu-system-x86_64"), (300, "host-application")):
        directory = proc / str(pid)
        (directory / "fd").mkdir(parents=True)
        (directory / "exe").symlink_to(sandbox / executable)
        (directory / "fd/4").symlink_to("/dev/bus/usb/017/007")
        (directory / "fd/5").symlink_to("/dev/bus/usb/017/007")
    owners = setup_module.node_owners(["/dev/bus/usb/017/007"], proc)
    assert owners == {"/dev/bus/usb/017/007": {"pids": [200, 300], "qemu_pids": [200]}}


def test_rollback_rejects_drift_in_archived_package_before_restoring(setup_module, sandbox):
    args, policy, _ = deployment(setup_module, sandbox)
    commands = SimulatedCommands()
    setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    old_metadata = setup_module.installed_metadata(args.install_dir / "deployment.json", args, policy)
    write_test_wheel(args.wheelhouse / "vhotplug-1.0-py3-none-any.whl", note="update")
    update = setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    current_metadata = (args.install_dir / "deployment.json").read_bytes()
    (args.install_dir / "releases" / old_metadata["release"] / "venv/bin/python").write_bytes(
        b"SIMULATION: old package drift"
    )
    with pytest.raises(setup_module.SetupError, match="drift"):
        setup_module.rollback(args, update["backup"], runner=commands, policy=policy, system_root=sandbox)
    assert (args.install_dir / "deployment.json").read_bytes() == current_metadata


@pytest.mark.parametrize("failure", ["daemon-reload", "active"])
def test_rollback_failure_retains_current_deployment(setup_module, sandbox, failure):
    args, policy, _ = deployment(setup_module, sandbox)
    commands = SimulatedCommands()
    result = setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    before = {name: path.read_bytes() for name, path in setup_module.targets(args, sandbox).items()}
    if failure == "active":
        commands.active = True
    else:
        commands.fail = lambda words: "daemon-reload" in words
    with pytest.raises(setup_module.SetupError):
        setup_module.rollback(args, result["backup"], runner=commands, policy=policy, system_root=sandbox)
    assert {name: path.read_bytes() for name, path in setup_module.targets(args, sandbox).items()} == before


@pytest.mark.parametrize("count", [0, 1, 2])
def test_compose_label_discovery_requires_one_container_and_one_file(setup_module, sandbox, count):
    compose, _ = compose_file(sandbox)
    commands = []

    def runner(command, *, cwd=None):
        commands.append(command)
        output = ("\n".join(["a" * 64, "b" * 64][:count]) + "\n") if command[1] == "ps" else str(compose)
        return subprocess.CompletedProcess(command, 0, output, "")

    args = setup_module.parse_args([])
    if count == 1:
        assert setup_module.discover_compose(args, runner) == compose
    else:
        with pytest.raises(setup_module.SetupError):
            setup_module.discover_compose(args, runner)


@pytest.mark.parametrize(
    "text",
    [
        "include: [other.yaml]\nservices: {windows: {image: example}}",
        "services: {windows: {environment: {PASSWORD: yes}}}",
    ],
)
def test_reject_unanalyzed_includes_and_ambiguous_yaml_credentials(setup_module, sandbox, text):
    compose = sandbox / "compose.yaml"
    compose.write_text(text)
    with pytest.raises(setup_module.SetupError):
        setup_module.make_plan(options(setup_module, compose, sandbox))
    assert compose.read_text() == text


def test_interrupt_during_replacement_restores_originals(setup_module, sandbox, monkeypatch):
    args, policy, _ = deployment(setup_module, sandbox)
    before = args.compose.read_bytes()
    write = setup_module.atomic_write
    interrupted = False

    def interrupt(path, data, *rest, **kwargs):
        nonlocal interrupted
        if path == args.config and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return write(path, data, *rest, **kwargs)

    monkeypatch.setattr(setup_module, "atomic_write", interrupt)
    with pytest.raises(KeyboardInterrupt):
        setup_module.apply(args, runner=SimulatedCommands(), policy=policy, system_root=sandbox)
    assert args.compose.read_bytes() == before
    assert not args.config.exists()


@pytest.mark.parametrize("target", ["config", "compose"])
@pytest.mark.parametrize("relation", ["same", "ancestor", "descendant"])
def test_targets_reject_installer_lock_collisions_before_mutation(setup_module, sandbox, target, relation):
    args, policy, _ = deployment(setup_module, sandbox)
    lock = args.install_dir / ".installer.lock"
    collision = {"same": lock, "ancestor": args.install_dir, "descendant": lock / "child"}[relation]
    setattr(args, target, collision)
    before = sorted(sandbox.rglob("*"))
    with pytest.raises(setup_module.SetupError, match="collide"):
        setup_module.targets(args, sandbox)
    with pytest.raises(setup_module.SetupError, match="collide"):
        setup_module.apply(args, runner=SimulatedCommands(), policy=policy, system_root=sandbox)
    assert sorted(sandbox.rglob("*")) == before
    assert not args.install_dir.exists()


def test_cli_holds_deployment_lock_through_activation(setup_module, sandbox, monkeypatch):
    args, policy, _ = deployment(setup_module, sandbox)
    monkeypatch.setattr(setup_module, "parse_args", lambda argv=None: args)
    observed = []

    def activate_under_lock(args, result, **kwargs):
        with (args.install_dir / ".installer.lock").open("a+b") as descriptor:
            try:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observed.append(True)
            else:
                observed.append(False)
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
        return result

    monkeypatch.setattr(setup_module, "activate", activate_under_lock)
    assert setup_module.main(runner=SimulatedCommands(), policy=policy, system_root=sandbox, euid=0) == 0
    assert observed == [True], "activation must not race with a second apply or rollback"


def test_sandbox_fixture_works_when_tmpdir_is_not_exported(sandbox, monkeypatch):
    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sandbox))
    generator = globals()["sandbox"].__wrapped__()
    try:
        try:
            directory = next(generator)
        except KeyError:
            pytest.fail("installer tests must not require a host-specific TMPDIR export")
        assert directory.is_dir()
        assert directory.parent == sandbox
    finally:
        generator.close()


@pytest.mark.parametrize("scalar", ["12:34", "0123", "2026-10-03", "12:34.5"])
def test_plan_rejects_ambiguous_yaml_scalars_without_rewriting(setup_module, sandbox, scalar):
    compose = sandbox / "compose.yaml"
    original = f"services:\n  windows:\n    environment:\n      PASSWORD: {scalar}\n"
    compose.write_text(original)
    with pytest.raises(setup_module.SetupError, match="scalar"):
        setup_module.make_plan(options(setup_module, compose, sandbox))
    assert compose.read_text() == original


def test_external_commands_disable_pip_configuration(setup_module, sandbox, monkeypatch):
    monkeypatch.setenv("PIP_CONFIG_FILE", str(sandbox / "untrusted_pip.conf"))
    monkeypatch.setenv("PIP_TARGET", str(sandbox / "outside_release"))
    monkeypatch.setenv("PIP_PREFIX", str(sandbox / "outside_prefix"))
    observed = {}

    def fake_run(command, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(setup_module.subprocess, "run", fake_run)
    setup_module.run(["python", "-I", "-m", "pip", "--isolated", "install"])
    assert observed.get("env", {}).get("PIP_CONFIG_FILE") == os.devnull
    assert "PIP_TARGET" not in observed["env"]
    assert "PIP_PREFIX" not in observed["env"]


@pytest.mark.parametrize(
    "requirement",
    [
        "dep @ https://example.invalid/dep.whl",
        "dep @ file:///outside/dep.whl",
        "dep @ git+https://example.invalid/dep.git",
    ],
)
def test_apply_rejects_direct_dependency_references_before_install(setup_module, sandbox, requirement):
    args, policy, _ = deployment(setup_module, sandbox)
    write_test_wheel(args.wheelhouse / "vhotplug-1.0-py3-none-any.whl", requirement=requirement)
    before = args.compose.read_bytes()
    commands = SimulatedCommands()
    with pytest.raises(setup_module.SetupError, match="dependency"):
        setup_module.apply(args, runner=commands, policy=policy, system_root=sandbox)
    assert args.compose.read_bytes() == before
    assert not args.config.exists()
    assert not any("pip" in command for command, _ in commands.calls)
