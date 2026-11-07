"""Where evolve runs candidate code: a disposable, offline sandbox, never the person's computer.

Signals come from the internet, so a built upgrade is untrusted code. Every command that executes it (the
builder's test runs through ``o-sandbox-test``, and every dynamic gate: acceptance test, engine pytest, node
selftests, offline eval) goes through a ``Runner`` from ``choose``:

* ``docker`` (``DockerRunner``): one ``docker run --rm`` per command, built on ``containment.DockerSandbox``,
  with ``--network none``, the build's worktree as the only host directory mounted read-write (plus an empty
  per-run output directory), a read-only root filesystem, every capability dropped, no-new-privileges,
  CPU / memory / pids limits, a hard timeout, and the image pinned by digest (``sandbox.Dockerfile``,
  built once from O's own trusted files and tagged with their hash). No home, ssh, keychain or O data dir.
* ``e2b`` (``E2BRunner``): a cloud instance from ``containment.E2BSandbox`` created with internet access off;
  the worktree is uploaded per command and output files come back as a tarball.
* ``unsafe-host-dev`` (``UnsafeHostRunner``): DEV ONLY. Runs on the host with a secret-free env. Selected only
  when ``evolveSandbox`` is exactly ``unsafe-host-dev`` AND the engine's env has ``O_EVOLVE_DEV_UNSAFE=1``;
  never a default, never reachable from the app's settings (main/settings.js refuses the value). It exists
  for proving the pipeline inside an already-disposable cloud machine.

``evolveSandbox`` = ``auto`` (default: docker, then e2b) | ``docker`` | ``e2b``. When nothing is available
``choose`` says so and the pipeline pauses the code-build tier with ``PAUSED_MESSAGE``; scanning, triage,
practice and skills keep running.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..containment import DockerSandbox, E2BSandbox
from ..schema import ProvisionStep

MODES = ("auto", "docker", "e2b", "unsafe-host-dev")
APP_MODES = ("auto", "docker", "e2b")  # what the app may set
UNSAFE_MODE = "unsafe-host-dev"
UNSAFE_ENV = "O_EVOLVE_DEV_UNSAFE"
DOCKERFILE = Path(__file__).with_name("sandbox.Dockerfile")
IMAGE_REPO = "o-evolve-sandbox"
E2B_TEMPLATE = "o-evolve-sandbox"  # built from sandbox.Dockerfile with `e2b template build`
WORK = "/work"
OUT = "/o-out"
CPUS = "2"
MEMORY = "4g"
PIDS = 512
TMPFS = "/tmp:rw,exec,nosuid,size=2g"
MAX_TIMEOUT_S = 3600
PAUSED_MESSAGE = "Code upgrades paused: install Docker Desktop to let O test upgrades safely"
PAUSED_E2B = "Code upgrades paused: E2B is not reachable, so O cannot test upgrades safely"
SANDBOX_ENV = {"HOME": "/tmp/home", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8", "CI": "1",
               "PIP_NO_INDEX": "1", "npm_config_offline": "true"}
_TAR_SKIP = (".git", "node_modules", "__pycache__", ".pytest_cache")


@dataclass
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0


class Runner(ABC):
    """Runs argv against one worktree. ``ref`` names an output file the command may write (junit, report)."""

    name = "runner"
    python = "python3"
    isolated = True

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).resolve()

    @abstractmethod
    def run(self, argv: Sequence[str], cwd: str = ".", timeout: float = 600.0,
            out_dir: Path | None = None) -> RunResult: ...

    def ref(self, out_dir: Path, name: str) -> str:
        return f"{OUT}/{name}"

    def close(self) -> None:
        pass

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "isolated": self.isolated, "root": str(self.root)}

    def __enter__(self) -> "Runner":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _rel(cwd: str) -> str:
    p = os.path.normpath(cwd or ".").replace("\\", "/")
    if p.startswith("/") or p == ".." or p.startswith("../"):
        raise ValueError(f"cwd {cwd!r} leaves the worktree")
    return "." if p in ("", ".") else p


# ---------------------------------------------------------------- unsafe host (dev only)

class UnsafeHostRunner(Runner):
    """DEV ONLY: the candidate's code runs on this machine (secret-free env, throwaway data dir)."""

    name = UNSAFE_MODE
    isolated = False

    def __init__(self, root: str | os.PathLike[str], extra_env: Mapping[str, str] | None = None) -> None:
        super().__init__(root)
        self.python = sys.executable
        self.extra_env = dict(extra_env or {})

    def ref(self, out_dir: Path, name: str) -> str:
        return str(Path(out_dir) / name)

    def run(self, argv: Sequence[str], cwd: str = ".", timeout: float = 600.0,
            out_dir: Path | None = None) -> RunResult:
        from .baseline import untrusted_env

        t0 = time.monotonic()
        try:
            proc = subprocess.run(list(argv), cwd=self.root / _rel(cwd), capture_output=True, text=True,
                                  errors="replace", timeout=timeout, env=untrusted_env(self.extra_env),
                                  stdin=subprocess.DEVNULL)
            return RunResult(proc.returncode, proc.stdout, proc.stderr, False, round(time.monotonic() - t0, 2))
        except subprocess.TimeoutExpired as e:
            return RunResult(124, _text(e.stdout), _text(e.stderr) + f"\ntimed out after {timeout}s", True,
                             round(time.monotonic() - t0, 2))
        except OSError as e:
            return RunResult(127, "", f"cannot run: {e}", False, round(time.monotonic() - t0, 2))


def _text(b: bytes | str | None) -> str:
    if b is None:
        return ""
    return b.decode("utf-8", errors="replace") if isinstance(b, bytes) else b


# ---------------------------------------------------------------- docker

class EvolveDockerSandbox(DockerSandbox):
    """``containment.DockerSandbox`` with evolve's hardening: offline, resource-limited, read-only root, no
    capabilities, the worktree as the only read-write host mount, plus an empty output dir."""

    def __init__(self, image: str, job_dir: str | os.PathLike[str], out_dir: Path | None = None,
                 docker_bin: str = "docker", user: str | None = None) -> None:
        super().__init__(image=image, job_dir=job_dir, docker_bin=docker_bin, env=dict(SANDBOX_ENV), network="none")
        self.network = "none"  # never anything else, whatever DockerSandbox defaults to
        self.out_dir = Path(out_dir).resolve() if out_dir else None
        self.user = user if user is not None else _host_user()

    def container_name(self) -> str:
        return f"o-evolve-{os.urandom(6).hex()}"

    def argv(self, step: Any, name: str) -> tuple[list[str], str, str]:
        rel = _rel(getattr(step, "target_directory", "."))
        workdir = WORK if rel == "." else f"{WORK}/{rel}"
        command = str(getattr(step, "command_string", "") or "")
        argv = [self.docker_bin, "run", "--rm", "--name", name, "--network", "none",
                "--cpus", CPUS, "--memory", MEMORY, "--memory-swap", MEMORY, "--pids-limit", str(PIDS),
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--tmpfs", TMPFS,
                "--ipc", "none", "-v", f"{self.job_dir}:{WORK}:rw"]
        if self.out_dir is not None:
            argv += ["-v", f"{self.out_dir}:{OUT}:rw"]
        if self.user:
            argv += ["--user", self.user]
        argv += ["-w", workdir]
        for key, value in sorted(self.env.items()):
            argv += ["-e", f"{key}={value}"]
        argv += [self.image, "sh", "-c", command]
        return argv, command, workdir


def _host_user() -> str:
    if hasattr(os, "getuid"):
        return f"{os.getuid()}:{os.getgid()}"
    return ""


class DockerRunner(Runner):
    name = "docker"

    def __init__(self, root: str | os.PathLike[str], image: str, docker_bin: str = "docker") -> None:
        super().__init__(root)
        self.image, self.docker_bin = image, docker_bin
        self.last_argv: list[str] = []

    def run(self, argv: Sequence[str], cwd: str = ".", timeout: float = 600.0,
            out_dir: Path | None = None) -> RunResult:
        timeout = max(1, min(int(timeout), MAX_TIMEOUT_S))
        sbx = EvolveDockerSandbox(self.image, self.root, out_dir, docker_bin=self.docker_bin)
        step = ProvisionStep(target_directory=_rel(cwd), command_string=shlex.join(list(argv)), timeout_seconds=timeout)
        self.last_argv = sbx.argv(step, "probe")[0]
        t0 = time.monotonic()
        with sbx:
            r = sbx.run(step)
        return RunResult(r.exit_code, r.stdout, r.stderr, r.timed_out, round(time.monotonic() - t0, 2))

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "image": self.image, "network": "none", "cpus": CPUS, "memory": MEMORY}


def image_tag(engine_dir: str | os.PathLike[str] | None = None) -> str:
    """``o-evolve-sandbox:<hash>`` of the Dockerfile and the trusted engine's pyproject (deps baked in)."""
    engine = Path(engine_dir) if engine_dir else Path(__file__).resolve().parents[2]
    h = hashlib.sha256(DOCKERFILE.read_bytes())
    pp = engine / "pyproject.toml"
    if pp.is_file():
        h.update(pp.read_bytes())
    return f"{IMAGE_REPO}:{h.hexdigest()[:16]}"


def ensure_image(docker_bin: str = "docker", engine_dir: str | os.PathLike[str] | None = None,
                 timeout: float = 1800.0, run: Callable[..., Any] = subprocess.run) -> str:
    """The pinned image, built once from O's own Dockerfile + pyproject (never from a candidate's files)."""
    tag = image_tag(engine_dir)
    if run([docker_bin, "image", "inspect", tag], capture_output=True, timeout=30).returncode == 0:
        return tag
    engine = Path(engine_dir) if engine_dir else Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="o-evolve-image-") as ctx:
        shutil.copy(DOCKERFILE, Path(ctx) / "Dockerfile")
        shutil.copy(engine / "pyproject.toml", Path(ctx) / "pyproject.toml")
        proc = run([docker_bin, "build", "-t", tag, ctx], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"building {tag} failed: {(proc.stderr or proc.stdout or '')[-400:]}")
    return tag


# ---------------------------------------------------------------- e2b

class EvolveE2BSandbox(E2BSandbox):
    """``containment.E2BSandbox`` created with internet access off; refuses an SDK that cannot turn it off."""

    def _instance(self) -> Any:
        if self._sbx is None:
            if not self.available:
                raise RuntimeError(f"E2B sandbox unavailable: {self.reason}")
            try:
                self._sbx = self._cls(template=self.template, api_key=self.api_key, allow_internet_access=False)
            except TypeError as e:
                raise RuntimeError("this e2b SDK cannot disable internet access; refusing to run candidate code") from e
        return self._sbx


def _worktree_tar(root: Path) -> bytes:
    buf = io.BytesIO()

    def skip(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(ti.name).parts
        return None if any(p in _TAR_SKIP for p in parts) or ti.issym() or ti.islnk() else ti

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for child in sorted(root.iterdir()):
            if child.name in _TAR_SKIP:
                continue
            tf.add(child, arcname=child.name, filter=skip)
    return buf.getvalue()


class E2BRunner(Runner):
    name = "e2b"

    def __init__(self, root: str | os.PathLike[str], api_key: str, template: str = E2B_TEMPLATE,
                 sandbox_cls: Any = None) -> None:
        super().__init__(root)
        self.sbx = EvolveE2BSandbox(api_key=api_key, template=template)
        if sandbox_cls is not None:  # tests
            self.sbx._cls, self.sbx._available, self.sbx._reason = sandbox_cls, True, ""

    def run(self, argv: Sequence[str], cwd: str = ".", timeout: float = 600.0,
            out_dir: Path | None = None) -> RunResult:
        t0 = time.monotonic()
        try:
            inst = self.sbx._instance()
            inst.files.write("/tmp/o-wt.tgz", _worktree_tar(self.root))
            inst.commands.run(f"rm -rf {WORK} {OUT} && mkdir -p {WORK} {OUT} && tar -xzf /tmp/o-wt.tgz -C {WORK}", timeout=300)
        except Exception as e:  # noqa: BLE001
            return RunResult(127, "", f"e2b setup failed: {e}", False, round(time.monotonic() - t0, 2))
        rel = _rel(cwd)
        step = ProvisionStep(target_directory=rel, command_string=shlex.join(list(argv)),
                             timeout_seconds=max(1, min(int(timeout), MAX_TIMEOUT_S)))
        r = self.sbx.run(step)
        if out_dir is not None:
            try:
                inst.commands.run(f"tar -czf /tmp/o-out.tgz -C {OUT} .", timeout=120)
                data = inst.files.read("/tmp/o-out.tgz", format="bytes")
                with tarfile.open(fileobj=io.BytesIO(bytes(data)), mode="r:gz") as tf:
                    tf.extractall(out_dir, filter="data")
            except Exception:  # noqa: BLE001 - the caller sees the missing report
                pass
        return RunResult(r.exit_code, r.stdout, r.stderr, r.timed_out, round(time.monotonic() - t0, 2))

    def close(self) -> None:
        self.sbx.destroy()


# ---------------------------------------------------------------- choice

@dataclass
class SandboxChoice:
    available: bool
    backend: str  # docker | e2b | unsafe-host-dev | none
    mode: str
    reason: str = ""
    message: str = ""  # the one calm line the Evolution feed shows when unavailable
    factory: Callable[[str | os.PathLike[str]], Runner] | None = None
    prepare: Callable[[], None] | None = None  # builds the pinned image on first use
    tried: list[str] = field(default_factory=list)

    def runner(self, root: str | os.PathLike[str]) -> Runner:
        if not self.available or self.factory is None:
            raise RuntimeError(self.reason or "no sandbox available")
        return self.factory(root)

    def view(self) -> dict[str, Any]:
        return {"enabled": self.available, "backend": self.backend, "mode": self.mode, "reason": self.reason,
                "message": self.message, "isolated": self.backend in ("docker", "e2b")}


def normalize_mode(mode: Any) -> str:
    m = str(mode or "auto").strip().lower()
    return m if m in MODES else "auto"


def _setting(settings_like: Any, *names: str) -> Any:
    for n in names:
        v = settings_like.get(n) if isinstance(settings_like, Mapping) else getattr(settings_like, n, None)
        if v not in (None, ""):
            return v
    return None


def choose(mode: Any = "auto", settings_like: Any = None, env: Mapping[str, str] | None = None, *,
           docker_bin: str = "docker", docker_probe: Callable[[], tuple[bool, str]] | None = None,
           e2b_probe: Callable[[str], tuple[bool, str]] | None = None) -> SandboxChoice:
    """The sandbox evolve may run candidate code in, or why there is none. Never falls back to the host."""
    env = os.environ if env is None else env
    m = normalize_mode(mode)
    if m == UNSAFE_MODE:
        if str(env.get(UNSAFE_ENV, "")).strip() == "1":
            return SandboxChoice(True, UNSAFE_MODE, m, "dev-only: candidate code runs on this host (O_EVOLVE_DEV_UNSAFE=1)",
                                 factory=lambda root: UnsafeHostRunner(root))
        return SandboxChoice(False, "none", m, f"{UNSAFE_MODE} needs {UNSAFE_ENV}=1 (dev only)", PAUSED_MESSAGE)
    tried: list[str] = []
    order = {"auto": ("docker", "e2b"), "docker": ("docker",), "e2b": ("e2b",)}[m]
    for backend in order:
        if backend == "docker":
            ok, why = (docker_probe or _docker_probe(docker_bin))()
            if ok:
                holder: dict[str, str] = {}

                def prepare(h: dict[str, str] = holder) -> None:
                    h.setdefault("image", ensure_image(docker_bin))

                def factory(root: Any, h: dict[str, str] = holder) -> Runner:
                    prepare()
                    return DockerRunner(root, h["image"], docker_bin)

                return SandboxChoice(True, "docker", m, "", factory=factory, prepare=prepare, tried=tried)
            tried.append(f"docker: {why}")
        else:
            key = str(_setting(settings_like, "e2bApiKey", "e2b_api_key") or env.get("E2B_API_KEY", "") or "")
            ok, why = (e2b_probe or _e2b_probe)(key)
            if ok:
                return SandboxChoice(True, "e2b", m, "", factory=lambda root, k=key: E2BRunner(root, k), tried=tried)
            tried.append(f"e2b: {why}")
    msg = PAUSED_E2B if m == "e2b" else PAUSED_MESSAGE
    return SandboxChoice(False, "none", m, "; ".join(tried) or "no sandbox backend", msg, tried=tried)


def _docker_probe(docker_bin: str) -> Callable[[], tuple[bool, str]]:
    def probe() -> tuple[bool, str]:
        d = DockerSandbox(docker_bin=docker_bin)
        return d.available, d.reason
    return probe


def _e2b_probe(key: str) -> tuple[bool, str]:
    s = E2BSandbox(api_key=key)
    return s.available, s.reason


class CachedChoice:
    """``choose`` re-probed at most every ``ttl_s`` (``docker info`` is not free); settings read each time."""

    def __init__(self, mode_fn: Callable[[], Any], settings_like: Any = None, ttl_s: float = 300.0,
                 chooser: Callable[..., SandboxChoice] = choose, now: Callable[[], float] = time.monotonic) -> None:
        self.mode_fn, self.settings_like, self.ttl_s, self.chooser, self.now = mode_fn, settings_like, ttl_s, chooser, now
        self._cached: tuple[float, str, SandboxChoice] | None = None

    def __call__(self) -> SandboxChoice:
        mode = normalize_mode(self.mode_fn())
        c = self._cached
        if c is None or c[1] != mode or self.now() - c[0] > self.ttl_s:
            self._cached = (self.now(), mode, self.chooser(mode, self.settings_like))
        return self._cached[2]


# ---------------------------------------------------------------- the builder's test wrapper

WRAPPER_NAME = "o-sandbox-test"


def install_wrapper(dest_dir: str | os.PathLike[str], worktree: str | os.PathLike[str], mode: str,
                    timeout_s: float = 900.0, python: str | None = None) -> Path:
    """Write ``<dest_dir>/bin/o-sandbox-test`` (+ its config) outside the worktree, read-only.

    The builder's Claude Code session may run tests only through this script; it re-selects the sandbox from
    ``mode`` and runs the command there. The config and the script live outside the worktree so the session
    (whose edits are confined to the worktree) cannot rewrite them.
    """
    d = Path(dest_dir).resolve()
    wt = Path(worktree).resolve()
    if d == wt or wt in d.parents:
        raise ValueError("the wrapper must live outside the worktree")
    (d / "bin").mkdir(parents=True, exist_ok=True)
    cfg = d / "sandbox-test.json"
    engine = Path(__file__).resolve().parents[2]
    for p in (cfg, d / "bin" / WRAPPER_NAME):
        if p.exists():
            p.chmod(0o600)
    cfg.write_text(json.dumps({"mode": normalize_mode(mode), "worktree": str(wt), "timeout_s": float(timeout_s),
                               "engine": str(engine)}), encoding="utf-8")
    script = d / "bin" / WRAPPER_NAME
    py = python or sys.executable
    script.write_text("#!/bin/sh\n# O evolve: run tests for this build inside its sandbox (generated; read-only)\n"
                      f"exec {shlex.quote(py)} -I {shlex.quote(str(engine / 'o_engine' / 'evolve' / 'sandbox_test.py'))} "
                      f"--config {shlex.quote(str(cfg))} \"$@\"\n", encoding="utf-8")
    cfg.chmod(0o444)
    script.chmod(0o555)
    return script
