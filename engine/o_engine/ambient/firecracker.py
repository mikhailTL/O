"""Firecracker microVM sandbox (``firecracker-microvm/firecracker``) for headless jobs on Linux + KVM.

One microVM per step, no network device, booted from ``--config-file`` with ``--no-api``:
- rootfs ``O_FC_ROOTFS`` (read-only) must contain ``/sbin/o-init`` = ``GUEST_INIT`` below
  (the installer bakes it in); kernel ``O_FC_KERNEL`` (an uncompressed vmlinux);
- a writable ext4 work drive built from the job directory with ``mkfs.ext4 -d`` (no root needed),
  mounted at ``/work`` in the guest; the step command and cwd are written to ``/work/.o-run``;
- the guest prints base64 stdout/stderr and ``O-EXIT:<code>`` on the serial console, which the host
  parses; afterwards the work drive is copied back into the job dir with ``debugfs rdump`` (files are
  added or overwritten, never deleted: the job dir stays append-only).
The ``jailer`` wraps the VM (chroot, cgroups, uid/gid drop) when it is on PATH and the engine runs
as root; otherwise the VM runs unjailed and that is logged.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from ..containment import DisposableSandbox, _relative_target
from ..executor import StepResult

TIMEOUT_EXIT_CODE = 124
WORK_MOUNT = "/work"
RUN_DIR = ".o-run"
BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/o-init"
DEFAULT_VCPUS = 1
DEFAULT_MEM_MIB = 512
WORK_HEADROOM_MIB = 256
GUEST_INIT = """#!/bin/sh
mount -t proc proc /proc 2>/dev/null; mount -t sysfs sys /sys 2>/dev/null; mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /work && mount /dev/vdb /work
cd "/work/$(cat /work/.o-run/cwd 2>/dev/null)" 2>/dev/null || cd /work
sh /work/.o-run/cmd.sh >/work/.o-run/stdout 2>/work/.o-run/stderr; code=$?
echo O-STDOUT-BEGIN; base64 /work/.o-run/stdout; echo O-STDOUT-END
echo O-STDERR-BEGIN; base64 /work/.o-run/stderr; echo O-STDERR-END
echo "O-EXIT:$code"; sync; umount /work; reboot -f
"""
_BLOCK = re.compile(r"O-(STDOUT|STDERR)-BEGIN\s*(.*?)\s*O-\1-END", re.S)
_EXIT = re.compile(r"O-EXIT:(-?\d+)")


def parse_console(text: str) -> tuple[int | None, str, str]:
    """(exit code | None, stdout, stderr) from the guest's serial console output."""
    out = {"STDOUT": "", "STDERR": ""}
    for name, body in _BLOCK.findall(text or ""):
        try:
            out[name] = base64.b64decode("".join(body.split())).decode("utf-8", errors="replace")
        except Exception:
            out[name] = ""
    m = _EXIT.search(text or "")
    return (int(m.group(1)) if m else None), out["STDOUT"], out["STDERR"]


def vm_config(kernel: str, rootfs: str, work_img: str, vcpus: int = DEFAULT_VCPUS, mem_mib: int = DEFAULT_MEM_MIB) -> dict[str, Any]:
    return {
        "boot-source": {"kernel_image_path": kernel, "boot_args": BOOT_ARGS},
        "drives": [
            {"drive_id": "rootfs", "path_on_host": rootfs, "is_root_device": True, "is_read_only": True},
            {"drive_id": "work", "path_on_host": work_img, "is_root_device": False, "is_read_only": False},
        ],
        "machine-config": {"vcpu_count": int(vcpus), "mem_size_mib": int(mem_mib)},
    }


def _dir_mib(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total // (1024 * 1024) + 1


class FirecrackerSandbox(DisposableSandbox):
    name = "firecracker"

    def __init__(self, job_dir: str | os.PathLike[str] = ".", kernel: str | None = None, rootfs: str | None = None,
                 firecracker_bin: str | None = None, jailer_bin: str | None = None,
                 env: Mapping[str, str] | None = None, vcpus: int = DEFAULT_VCPUS, mem_mib: int = DEFAULT_MEM_MIB,
                 platform: str | None = None, kvm: str = "/dev/kvm", which: Any = shutil.which) -> None:
        env = os.environ if env is None else env
        self.job_dir = Path(job_dir).resolve()
        self.kernel = kernel if kernel is not None else env.get("O_FC_KERNEL", "")
        self.rootfs = rootfs if rootfs is not None else env.get("O_FC_ROOTFS", "")
        self.which = which
        self.firecracker_bin = firecracker_bin or env.get("O_FIRECRACKER_BIN", "") or (which("firecracker") or "")
        self.jailer_bin = jailer_bin if jailer_bin is not None else (which("jailer") or "")
        self.vcpus = vcpus
        self.mem_mib = mem_mib
        self.platform = platform or sys.platform
        self.kvm = kvm
        self._procs: list[subprocess.Popen] = []
        self.jailed = False

    @property
    def reason(self) -> str:
        if not self.platform.startswith("linux"):
            return "Firecracker needs Linux with KVM"
        if not os.path.exists(self.kvm):
            return f"{self.kvm} not present (KVM unavailable)"
        if not os.access(self.kvm, os.R_OK | os.W_OK):
            return f"{self.kvm} is not readable/writable by this user"
        if not self.firecracker_bin:
            return "firecracker binary not found (PATH or O_FIRECRACKER_BIN)"
        if not self.kernel or not os.path.isfile(self.kernel):
            return "guest kernel not found (O_FC_KERNEL)"
        if not self.rootfs or not os.path.isfile(self.rootfs):
            return "guest rootfs with /sbin/o-init not found (O_FC_ROOTFS)"
        if not self.which("mkfs.ext4"):
            return "mkfs.ext4 not found (e2fsprogs) for the work drive"
        return ""

    @property
    def available(self) -> bool:
        return self.reason == ""

    def use_jailer(self) -> bool:
        geteuid = getattr(os, "geteuid", None)
        return bool(self.jailer_bin) and callable(geteuid) and geteuid() == 0

    def argv(self, vm_id: str, config_path: str, chroot_base: str | None = None) -> list[str]:
        if chroot_base is not None:
            uid = os.environ.get("O_FC_UID", "65534")
            gid = os.environ.get("O_FC_GID", "65534")
            return [self.jailer_bin, "--id", vm_id, "--exec-file", self.firecracker_bin, "--uid", uid, "--gid", gid,
                    "--chroot-base-dir", chroot_base, "--", "--config-file", config_path, "--no-api"]
        return [self.firecracker_bin, "--config-file", config_path, "--no-api"]

    def _build_work(self, step: Any, tmp: Path) -> tuple[Path, str]:
        rel = _relative_target(getattr(step, "target_directory", "."))
        command, label = self._command_for(step, self.job_dir / ".o-scripts", WORK_MOUNT)
        run = self.job_dir / RUN_DIR
        run.mkdir(parents=True, exist_ok=True)
        (run / "cmd.sh").write_text(command + "\n", encoding="utf-8")
        (run / "cwd").write_text("" if rel == "." else rel, encoding="utf-8")
        img = tmp / "work.ext4"
        size = _dir_mib(self.job_dir) + WORK_HEADROOM_MIB
        subprocess.run([self.which("mkfs.ext4") or "mkfs.ext4", "-q", "-F", "-d", str(self.job_dir), str(img), f"{size}M"],
                       check=True, capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
        return img, label

    def _sync_back(self, img: Path, tmp: Path) -> None:
        debugfs = self.which("debugfs")
        if not debugfs:
            return
        out = tmp / "out"
        out.mkdir(exist_ok=True)
        subprocess.run([debugfs, "-R", f"rdump / {out}", str(img)], capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
        src = out
        for child in out.iterdir():
            if child.is_dir() and child.name in ("", "/"):
                src = child
        for p in src.rglob("*"):
            if p.name == "lost+found" or RUN_DIR in p.parts:
                continue
            dest = self.job_dir / p.relative_to(src)
            if p.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            elif p.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)

    def run(self, step: Any, index: int = 0) -> StepResult:
        started = time.monotonic()
        timeout = int(getattr(step, "timeout_seconds", 600) or 600)
        rel = _relative_target(getattr(step, "target_directory", "."))
        cwd = WORK_MOUNT if rel == "." else f"{WORK_MOUNT}/{rel}"
        label = getattr(step, "command_string", None) or "[script]"
        with tempfile.TemporaryDirectory(prefix="o-fc-") as tmpdir:
            tmp = Path(tmpdir)
            try:
                img, label = self._build_work(step, tmp)
            except Exception as exc:  # noqa: BLE001
                return StepResult(index=index, command=label, cwd=cwd, exit_code=127, stdout="",
                                  stderr=f"firecracker work drive failed: {exc}", elapsed_ms=0, timed_out=False)
            vm_id = f"o-{uuid.uuid4().hex[:12]}"
            kernel, rootfs, work = self.kernel, self.rootfs, str(img)
            chroot_base = None
            if self.use_jailer():
                chroot_base = str(tmp / "jail")
                root = Path(chroot_base) / Path(self.firecracker_bin).name / vm_id / "root"
                root.mkdir(parents=True, exist_ok=True)
                for src, name in ((self.kernel, "vmlinux"), (self.rootfs, "rootfs.ext4"), (str(img), "work.ext4")):
                    shutil.copy2(src, root / name)
                kernel, rootfs, work = "vmlinux", "rootfs.ext4", "work.ext4"
                self.jailed = True
            config = vm_config(kernel, rootfs, work, self.vcpus, self.mem_mib)
            cfg_path = (Path(chroot_base) / Path(self.firecracker_bin).name / vm_id / "root" / "vm.json") if chroot_base else (tmp / "vm.json")
            cfg_path.write_text(json.dumps(config), encoding="utf-8")
            argv = self.argv(vm_id, "vm.json" if chroot_base else str(cfg_path), chroot_base)
            timed_out = False
            try:
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                self._procs.append(proc)
                try:
                    console, _ = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    proc.kill()
                    console, _ = proc.communicate()
                finally:
                    if proc in self._procs:
                        self._procs.remove(proc)
            except OSError as exc:
                return StepResult(index=index, command=label, cwd=cwd, exit_code=127, stdout="",
                                  stderr=f"firecracker failed to run: {exc}", elapsed_ms=0, timed_out=False)
            text = (console or b"").decode("utf-8", errors="replace")
            code, stdout, stderr = parse_console(text)
            if chroot_base:
                img = Path(chroot_base) / Path(self.firecracker_bin).name / vm_id / "root" / "work.ext4"
            if not timed_out:
                try:
                    self._sync_back(img, tmp)
                except Exception:
                    pass
        if timed_out:
            code = TIMEOUT_EXIT_CODE
        elif code is None:
            code, stderr = 1, (stderr or "") + "\nfirecracker guest exited without O-EXIT (is /sbin/o-init in the rootfs?)"
        return StepResult(index=index, command=label, cwd=cwd, exit_code=int(code), stdout=stdout, stderr=stderr,
                          elapsed_ms=int((time.monotonic() - started) * 1000), timed_out=timed_out)

    def extract(self, relpath: str) -> bytes:
        target = (self.job_dir / relpath).resolve()
        if target != self.job_dir and self.job_dir not in target.parents:
            raise ValueError(f"{relpath!r} resolves outside the job directory")
        return target.read_bytes()

    def destroy(self) -> None:
        for proc in list(self._procs):
            try:
                proc.kill()
            except Exception:
                pass
        self._procs.clear()
