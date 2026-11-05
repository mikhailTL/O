"""Plan J: supervisor (restart/backoff/start limit, dependency repair incl. Nix/devbox), Firecracker,
microsandbox and the headless isolation selector."""
from __future__ import annotations

import asyncio
import base64
import subprocess
import sys
import types
from pathlib import Path

import pytest

from o_engine.ambient import firecracker as fc
from o_engine.ambient import supervisor as sup
from o_engine.ambient.isolation import IsolatedJobSandbox, IsolatedSandboxes, select_isolated
from o_engine.ambient.microsandbox import MicrosandboxSandbox, microsandbox_status
from o_engine.schema import ProvisionStep


class ListLog:
    def __init__(self):
        self.rows = []

    def record(self, kind, summary, **detail):
        self.rows.append((kind, summary, detail))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# ----------------------------------------------------------------------------- supervisor


class FakeProc:
    def __init__(self, code=None):
        self.code = code
        self.pid = 1234

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15

    def wait(self, timeout=None):
        return self.code


def test_supervisor_restarts_with_backoff_and_gives_up():
    procs = []
    clock = Clock()
    log = ListLog()

    def popen(argv, **kw):
        p = FakeProc()
        procs.append(p)
        return p

    s = sup.Supervisor(log, clock=clock, popen=popen, find_spec=lambda m: object())
    child = s.add(sup.Child(name="screenpipe", argv=["screenpipe"], restart="always"))
    s._start_child(child)
    delays = []
    for _ in range(8):
        procs[-1].code = 1  # crash
        s.poll()
        if child.state == "failed":
            break
        assert child.state == "backoff"
        delays.append(child.next_start - clock.t)
        clock.t = child.next_start
        s.poll()
    assert delays[:3] == [1.0, 2.0, 4.0]
    assert child.state == "failed"
    assert any("start limit" in r[1] for r in log.rows)
    assert all(r[0] in ("note", "remediate") for r in log.rows)


def test_supervisor_on_failure_does_not_restart_clean_exit():
    clock = Clock()
    p = FakeProc()
    s = sup.Supervisor(ListLog(), clock=clock, popen=lambda argv, **kw: p, find_spec=lambda m: object())
    child = s.add(sup.Child(name="vllm", argv=["vllm"], restart="on-failure"))
    s._start_child(child)
    p.code = 0
    s.poll()
    assert child.state == "stopped"


def test_supervisor_restarts_dead_worker_and_resets_backoff():
    class Worker:
        def __init__(self):
            self.running = False
            self.starts = 0

        def start(self):
            self.starts += 1
            self.running = True
            return True

        def stop(self):
            self.running = False

    clock = Clock()
    w = Worker()
    s = sup.Supervisor(ListLog(), clock=clock, find_spec=lambda m: object())
    child = s.add(sup.Child(name="sensor:sonar", worker=w, restart="always"))
    s._start_child(child)
    w.running = False
    s.poll()
    clock.t = child.next_start
    s.poll()
    assert w.starts == 2 and child.state == "running" and child.restarts == 1
    clock.t += sup.STABLE_S + 1
    s.poll()
    assert child.backoff == sup.BACKOFF_BASE_S


def test_dependency_repair_gated_by_auto_repair(tmp_path):
    ran = []
    find = lambda m: None if m == "numpy" else object()  # noqa: E731
    runner = lambda argv, **kw: (ran.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1]  # noqa: E731
    log = ListLog()
    off = sup.Supervisor(log, auto_repair=False, project_root=tmp_path, find_spec=find, runner=runner, which=lambda n: None)
    assert off.check_dependencies() == ["numpy"] and ran == []
    assert any("auto-repair is off" in r[1] for r in log.rows)
    on = sup.Supervisor(log, auto_repair=True, project_root=tmp_path, find_spec=find, runner=runner, which=lambda n: None)
    on.check_dependencies()
    assert ran == [[sys.executable, "-m", "pip", "install", "numpy"]]
    assert on.repairs[0]["via"] == "pip"


def test_repair_command_prefers_nix_then_devbox(tmp_path):
    which = lambda n: f"/usr/bin/{n}"  # noqa: E731
    assert sup.repair_command("numpy", tmp_path, "py", which) == ["py", "-m", "pip", "install", "numpy"]
    (tmp_path / "devbox.json").write_text("{}")
    assert sup.repair_command("numpy", tmp_path, "py", which)[:2] == ["/usr/bin/devbox", "run"]
    (tmp_path / "flake.nix").write_text("{}")
    cmd = sup.repair_command("numpy", tmp_path, "py", which)
    assert cmd[:3] == ["/usr/bin/nix", "develop", str(tmp_path)] and cmd[3:5] == ["--command", "py"]
    assert sup.repair_command("numpy", tmp_path, "py", lambda n: None)[0] == "py"


def test_missing_packages_default_set_is_installed():
    assert sup.missing_packages({"json": "json"}) == []
    assert sup.missing_packages({"definitely_not_a_module_xyz": "x"}) == ["definitely_not_a_module_xyz"]


# ----------------------------------------------------------------------------- firecracker


def test_firecracker_console_parsing():
    out = base64.b64encode(b"hello\n").decode()
    err = base64.b64encode(b"warn").decode()
    console = f"[ boot ]\nO-STDOUT-BEGIN\n{out}\nO-STDOUT-END\nO-STDERR-BEGIN\n{err}\nO-STDERR-END\nO-EXIT:3\n"
    assert fc.parse_console(console) == (3, "hello\n", "warn")
    assert fc.parse_console("kernel panic")[0] is None


def test_firecracker_config_and_argv(tmp_path):
    cfg = fc.vm_config("/k", "/r", "/w", 2, 256)
    assert cfg["drives"][0]["is_read_only"] is True and cfg["drives"][1]["drive_id"] == "work"
    assert "init=/sbin/o-init" in cfg["boot-source"]["boot_args"] and "network-interfaces" not in cfg
    assert cfg["machine-config"] == {"vcpu_count": 2, "mem_size_mib": 256}
    sb = fc.FirecrackerSandbox(job_dir=tmp_path, kernel="/k", rootfs="/r", firecracker_bin="/fc", jailer_bin="/jailer",
                               env={}, platform="linux")
    assert sb.argv("vm1", "/c.json") == ["/fc", "--config-file", "/c.json", "--no-api"]
    jailed = sb.argv("vm1", "vm.json", "/srv/jail")
    assert jailed[0] == "/jailer" and "--chroot-base-dir" in jailed and jailed[-3:] == ["--config-file", "vm.json", "--no-api"]
    assert "mount /dev/vdb /work" in fc.GUEST_INIT and "O-EXIT:" in fc.GUEST_INIT


def test_firecracker_availability_reasons(tmp_path):
    def make(**kw):
        base = dict(job_dir=tmp_path, kernel="", rootfs="", firecracker_bin="", jailer_bin="", env={},
                    platform="linux", kvm=str(tmp_path / "kvm"), which=lambda n: None)
        base.update(kw)
        return fc.FirecrackerSandbox(**base)

    assert "Linux" in make(platform="darwin").reason
    assert "KVM" in make().reason
    kvm = tmp_path / "kvm"
    kvm.write_text("")
    assert "firecracker binary" in make().reason
    assert "kernel" in make(firecracker_bin="/fc").reason
    k, r = tmp_path / "vmlinux", tmp_path / "rootfs.ext4"
    k.write_text("k")
    r.write_text("r")
    assert "mkfs.ext4" in make(firecracker_bin="/fc", kernel=str(k), rootfs=str(r)).reason
    ok = make(firecracker_bin="/fc", kernel=str(k), rootfs=str(r), which=lambda n: "/sbin/" + n)
    assert ok.available and ok.reason == ""


# ----------------------------------------------------------------------------- microsandbox


class FakeMountConfig:
    def __init__(self, bind):
        self.bind = bind


class FakeVolume:
    @staticmethod
    def bind(path, **kw):
        return FakeMountConfig(path)


class FakeMSB:
    """Mirrors microsandbox 0.7.13: create(name, *, image, cpus, memory, volumes: Mapping, replace),
    exec(cmd, args, *, timeout) -> ExecOutput, stop(), static remove(name)."""

    created = []
    removed = []

    def __init__(self, name):
        self.name = name
        self.stopped = False
        self.execs = []

    @classmethod
    async def create(cls, name, *, image=None, cpus=None, memory=None, volumes=None, replace=False):
        if volumes is not None and not isinstance(volumes, dict):
            raise TypeError("volumes must be a Mapping")
        sb = cls(name)
        sb.opts = dict(image=image, cpus=cpus, memory=memory, volumes=volumes, replace=replace)
        cls.created.append(sb)
        return sb

    @staticmethod
    async def remove(name):
        FakeMSB.removed.append(name)

    async def exec(self, cmd, args=None, *, cwd=None, env=None, timeout=None):
        self.execs.append((cmd, args, timeout))
        return types.SimpleNamespace(stdout_text="ok\n", stderr_text="", exit_code=0, success=True)

    async def stop(self, timeout=None):
        self.stopped = True


def test_microsandbox_runs_step_and_stops(tmp_path):
    FakeMSB.created.clear()
    FakeMSB.removed.clear()
    sb = MicrosandboxSandbox(job_dir=tmp_path, image="img", sdk=FakeMSB, volume=FakeVolume)
    assert sb.available
    step = ProvisionStep(target_directory="sub", command_string="echo ok", timeout_seconds=10)
    res = sb.run(step, 0)
    assert res.exit_code == 0 and res.stdout == "ok\n" and res.cwd == "/work/sub"
    vm = FakeMSB.created[0]
    assert set(vm.opts["volumes"]) == {"/work"} and vm.opts["volumes"]["/work"].bind == str(tmp_path.resolve())
    assert vm.opts["image"] == "img" and vm.opts["replace"] is True
    assert vm.execs[0][0] == "sh" and "cd /work/sub && echo ok" in vm.execs[0][1][1] and vm.execs[0][2] == 10.0
    sb.run(step, 1)
    assert len(FakeMSB.created) == 1  # one microVM per job
    sb.destroy()
    assert vm.stopped and FakeMSB.removed == [vm.name]


def test_microsandbox_create_failure_is_a_failed_step_and_cleans_up(tmp_path):
    class Boom(FakeMSB):
        @classmethod
        async def create(cls, name, **kw):
            raise RuntimeError("sandbox process exited (signal: 6 (SIGABRT)) before agent relay became available")

    FakeMSB.removed.clear()
    sb = MicrosandboxSandbox(job_dir=tmp_path, sdk=Boom, volume=FakeVolume)
    res = sb.run(ProvisionStep(target_directory=".", command_string="true", timeout_seconds=5), 0)
    assert res.exit_code == 127 and "SIGABRT" in res.stderr and not res.timed_out
    sb.destroy()
    assert FakeMSB.removed == [sb.vm_name]


def test_microsandbox_status_reasons():
    ok, reason = microsandbox_status(find_spec=lambda n: None)
    assert not ok and "not installed" in reason
    present = lambda n: object()  # noqa: E731
    ok, reason = microsandbox_status(find_spec=present, platform="linux", access=lambda p, m: False)
    assert not ok and "/dev/kvm" in reason
    ok, reason = microsandbox_status(find_spec=present, platform="darwin", machine="x86_64")
    assert not ok and "Apple Silicon" in reason
    ok, reason = microsandbox_status(find_spec=present, platform="linux", access=lambda p, m: True,
                                     runtime_check=lambda: (True, ""))
    assert ok and reason == ""
    ok, reason = microsandbox_status(find_spec=present, platform="darwin", machine="arm64",
                                     runtime_check=lambda: (False, "runtime missing"))
    assert not ok and reason == "runtime missing"


# ----------------------------------------------------------------------------- isolation


class FakeBackend:
    def __init__(self, name, available, root=None):
        self.name = name
        self._available = available
        self.reason = "" if available else f"{name} missing"
        self.root = root
        self.steps = []
        self.destroyed = False

    @property
    def available(self):
        return self._available

    def run(self, step, index=0):
        from o_engine.executor import Sandbox

        self.steps.append(step)
        local = step.model_copy(update={"command_string": step.command_string.replace("/work", str(self.root))})
        return Sandbox(str(self.root)).run(local, index)

    def destroy(self):
        self.destroyed = True


def test_select_isolated_order_and_logging(tmp_path):
    log = ListLog()
    cands = lambda d, s: [FakeBackend("microsandbox", False), FakeBackend("firecracker", False), FakeBackend("docker", True)]  # noqa: E731
    chosen = select_isolated(str(tmp_path), log, None, cands, job_id="j1")
    assert chosen.name == "docker"
    assert "microsandbox missing" in log.rows[-1][1] and "firecracker missing" in log.rows[-1][1]
    none = select_isolated(str(tmp_path), log, None, lambda d, s: [FakeBackend("microsandbox", False)])
    assert none is None and "local sandbox" in log.rows[-1][1]
    first = select_isolated(str(tmp_path), log, None, lambda d, s: [FakeBackend("microsandbox", True), FakeBackend("docker", True)])
    assert first.name == "microsandbox"


def test_default_candidate_order(tmp_path):
    from o_engine.ambient.isolation import default_candidates

    assert [c.name for c in default_candidates(str(tmp_path))] == ["microsandbox", "firecracker", "docker"]


def test_isolated_job_sandbox_rewrites_paths(tmp_path):
    root = tmp_path / "job"
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)
    script = cache / "abc.sh"
    script.write_text("echo cached > from-cache.txt\n")
    backend = FakeBackend("fakevm", True, root=root)
    sb = IsolatedJobSandbox(root, backend, import_roots=[tmp_path / "data"])
    (root / "sub").mkdir()
    step = ProvisionStep(target_directory=str(root / "sub"), command_string=f"bash {script} && ls {root}/sub /usr/bin/env",
                         timeout_seconds=10)
    moved = sb.localize(step.command_string)
    assert str(script) not in moved and "/work/.o-imports/" in moved and "/work/sub" in moved and "/usr/bin/env" in moved
    res = sb.run(ProvisionStep(target_directory=".", command_string=f"bash {script}", timeout_seconds=10))
    assert res.exit_code == 0 and (root / "from-cache.txt").read_text().strip() == "cached"
    assert backend.steps[-1].target_directory == "."
    sb2 = IsolatedSandboxes(tmp_path / "sbx", ListLog(), candidates=lambda d, s: [FakeBackend("fakevm", True, root=Path(d))])
    a = sb2.for_job("j")
    assert sb2.for_job("j") is a and sb2.last_backend == "fakevm"
    sb2.release("j")
    assert a.backend.destroyed
