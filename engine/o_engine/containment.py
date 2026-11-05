"""Disposable sandboxes for high-risk provisioning requests.

A ``DisposableSandbox`` runs steps somewhere that can be thrown away afterwards: an E2B
cloud instance (``E2BSandbox``) or a one-shot Docker container (``DockerSandbox``). The
job directory is the only thing that survives (mounted at ``/work``), and nothing here
ever deletes from it. ``select_sandbox`` picks the first available backend in
[preference, docker, local] and logs every fall-back; ``run_high_risk`` runs a request
through it and destroys the instance when done.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Mapping, Sequence

from .executor import StepResult, scrub_env
from .schema import ProvisionStep

TIMEOUT_EXIT_CODE = 124
DOCKER_PROBE_SECONDS = 5.0
DEFAULT_IMAGE = "python:3.12-slim"
DEFAULT_NETWORK = "bridge"
WORK_MOUNT = "/work"
SCRIPT_DIRNAME = ".o-scripts"
BACKENDS = ("e2b", "docker", "local")

_SCRIPT_RUNNERS: dict[str, tuple[str, str]] = {
    "python": ("python3", ".py"),
    "bash": ("bash", ".sh"),
    "javascript": ("node", ".js"),
    "powershell": ("pwsh -File", ".ps1"),
}


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def _setting(settings_like: Any, *names: str, default: Any = None) -> Any:
    """Read a setting from a dict or an object, trying each name (camelCase and snake_case)."""
    if settings_like is None:
        return default
    for name in names:
        if isinstance(settings_like, Mapping):
            if name in settings_like and settings_like[name] is not None:
                return settings_like[name]
        else:
            value = getattr(settings_like, name, None)
            if value is not None:
                return value
    return default


def _record(log: Any, kind: str, summary: str, **detail: Any) -> None:
    if log is None:
        return
    try:
        log.record(kind, summary, **detail)
    except Exception:
        pass


def _result(index: int, command: str, cwd: str, exit_code: int, stdout: str, stderr: str,
            started: float, timed_out: bool) -> StepResult:
    return StepResult(
        index=index,
        command=command,
        cwd=cwd,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        timed_out=timed_out,
    )


def _relative_target(target_directory: str) -> str:
    """A step's target_directory as a path relative to the job dir (absolute paths are re-rooted)."""
    raw = (target_directory or ".").strip()
    p = Path(raw)
    if p.is_absolute():
        parts = [part for part in p.parts if part not in (p.anchor, "/", "\\")]
        p = Path(*parts) if parts else Path(".")
    clean = os.path.normpath(str(p)).replace("\\", "/")
    if clean.startswith(".."):
        raise ValueError(f"target_directory {target_directory!r} would leave the job directory")
    return "." if clean in ("", ".") else clean


class DisposableSandbox(ABC):
    """Something that runs steps and can be destroyed afterwards. Context manager: destroys on exit."""

    name: str = "disposable"

    @property
    @abstractmethod
    def available(self) -> bool: ...

    @property
    @abstractmethod
    def reason(self) -> str:
        """Why ``available`` is False (empty when it is True)."""

    @abstractmethod
    def run(self, step: ProvisionStep, index: int = 0) -> StepResult: ...

    @abstractmethod
    def extract(self, relpath: str) -> bytes: ...

    @abstractmethod
    def destroy(self) -> None: ...

    def __enter__(self) -> "DisposableSandbox":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.destroy()

    # -- shared helpers ---------------------------------------------------------------

    def _command_for(self, step: Any, script_dir: Path, mount: str) -> tuple[str, str]:
        """(shell command, label) for a step: its command_string or a script payload written to disk."""
        command = getattr(step, "command_string", None)
        if command:
            return command, command
        script = getattr(step, "script", None)
        if script is None:
            raise ValueError("step has neither command_string nor script")
        language = str(getattr(script, "language", "bash")).lower()
        runner, ext = _SCRIPT_RUNNERS.get(language, ("bash", ".sh"))
        script_dir.mkdir(parents=True, exist_ok=True)
        name = f"step-{uuid.uuid4().hex[:12]}{ext}"
        (script_dir / name).write_text(str(getattr(script, "code", "")), encoding="utf-8")
        path = f"{mount}/{SCRIPT_DIRNAME}/{name}"
        return f"{runner} {path}", f"[{language} script] {path}"


class DockerSandbox(DisposableSandbox):
    """One ``docker run --rm --network <network>`` per step; the job dir is mounted at /work.

    ``network`` is the Docker network name (setting ``dockerNetwork``/``docker_network``,
    env ``O_DOCKER_NETWORK``); ``bridge`` by default, ``none`` for an offline container.
    """

    name = "docker"

    def __init__(self, image: str = DEFAULT_IMAGE, job_dir: str | os.PathLike[str] = ".",
                 docker_bin: str = "docker", env: Mapping[str, str] | None = None,
                 network: str | None = DEFAULT_NETWORK) -> None:
        self.image = image or DEFAULT_IMAGE
        self.network = str(network).strip() if network is not None and str(network).strip() else DEFAULT_NETWORK
        self.job_dir = Path(job_dir).resolve()
        self.docker_bin = docker_bin
        self.env = dict(env or {})
        self._containers: list[str] = []
        self._available: bool | None = None
        self._reason = ""

    def _probe(self) -> None:
        exe = shutil.which(self.docker_bin)
        if not exe:
            self._available, self._reason = False, f"{self.docker_bin!r} not on PATH"
            return
        try:
            proc = subprocess.run(
                [exe, "info"], capture_output=True, text=True, timeout=DOCKER_PROBE_SECONDS,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            self._available, self._reason = False, f"docker info did not answer within {DOCKER_PROBE_SECONDS:g} s"
            return
        except OSError as e:
            self._available, self._reason = False, f"docker info failed to run: {e}"
            return
        if proc.returncode != 0:
            first = (proc.stderr or proc.stdout or "").strip().splitlines()
            self._available = False
            self._reason = "docker daemon unavailable" + (f": {first[0][:200]}" if first else "")
            return
        self._available, self._reason = True, ""

    @property
    def available(self) -> bool:
        if self._available is None:
            self._probe()
        return bool(self._available)

    @property
    def reason(self) -> str:
        if self._available is None:
            self._probe()
        return self._reason

    def container_name(self) -> str:
        return f"o-hr-{uuid.uuid4().hex[:12]}"

    def argv(self, step: Any, name: str) -> tuple[list[str], str, str]:
        """(argv, command label, container cwd) for ``docker run``."""
        rel = _relative_target(getattr(step, "target_directory", "."))
        workdir = WORK_MOUNT if rel == "." else f"{WORK_MOUNT}/{rel}"
        command, label = self._command_for(step, self.job_dir / SCRIPT_DIRNAME, WORK_MOUNT)
        argv = [
            self.docker_bin, "run", "--rm", "--network", self.network, "--name", name,
            "-v", f"{self.job_dir}:{WORK_MOUNT}", "-w", workdir,
        ]
        for key, value in sorted(scrub_env(self.env).items()):
            argv += ["-e", f"{key}={value}"]
        argv += [self.image, "sh", "-c", command]
        return argv, label, workdir

    def _kill(self, name: str) -> None:
        try:
            subprocess.run([self.docker_bin, "kill", name], capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
        except Exception:
            pass
        try:
            subprocess.run([self.docker_bin, "rm", "-f", name], capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
        except Exception:
            pass

    def run(self, step: ProvisionStep, index: int = 0) -> StepResult:
        name = self.container_name()
        argv, label, workdir = self.argv(step, name)
        timeout = int(getattr(step, "timeout_seconds", 600) or 600)
        self._containers.append(name)
        started = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
            exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as e:
            timed_out = True
            exit_code = TIMEOUT_EXIT_CODE
            stdout, stderr = _decode(e.stdout), _decode(e.stderr)
            self._kill(name)
        except OSError as e:
            exit_code, stdout, stderr = 127, "", f"docker failed to run: {e}"
        finally:
            if name in self._containers:
                self._containers.remove(name)
        return _result(index, label, workdir, exit_code, stdout, stderr, started, timed_out)

    def extract(self, relpath: str) -> bytes:
        target = (self.job_dir / relpath).resolve()
        if target != self.job_dir and self.job_dir not in target.parents:
            raise ValueError(f"{relpath!r} resolves outside the job directory")
        return target.read_bytes()

    def destroy(self) -> None:
        """Kill leftover containers. The job dir is append-only: nothing in it is removed."""
        for name in list(self._containers):
            self._kill(name)
        self._containers.clear()


class E2BSandbox(DisposableSandbox):
    """A cloud instance from the ``e2b`` SDK; created lazily on the first ``run``."""

    name = "e2b"

    def __init__(self, api_key: str | None = None, template: str = "base", job_dir: str | os.PathLike[str] | None = None) -> None:
        self.api_key = api_key or ""
        self.template = template or "base"
        self.job_dir = Path(job_dir).resolve() if job_dir else None
        self._sbx: Any = None
        self._cls: Any = None
        self._reason = ""
        self._available: bool | None = None

    def _probe(self) -> None:
        if not self.api_key:
            self._available, self._reason = False, "E2B_API_KEY not set"
            return
        try:
            from e2b import Sandbox as E2B  # type: ignore
        except Exception as e:  # ImportError or a broken install
            self._available, self._reason = False, f"e2b SDK not importable ({type(e).__name__})"
            return
        self._cls = E2B
        self._available, self._reason = True, ""

    @property
    def available(self) -> bool:
        if self._available is None:
            self._probe()
        return bool(self._available)

    @property
    def reason(self) -> str:
        if self._available is None:
            self._probe()
        return self._reason

    def _instance(self) -> Any:
        if self._sbx is None:
            if not self.available:
                raise RuntimeError(f"E2B sandbox unavailable: {self.reason}")
            try:
                self._sbx = self._cls(template=self.template, api_key=self.api_key)
            except TypeError:
                self._sbx = self._cls(self.template, api_key=self.api_key)
        return self._sbx

    def run(self, step: ProvisionStep, index: int = 0) -> StepResult:
        rel = _relative_target(getattr(step, "target_directory", "."))
        cwd = WORK_MOUNT if rel == "." else f"{WORK_MOUNT}/{rel}"
        timeout = int(getattr(step, "timeout_seconds", 600) or 600)
        started = time.monotonic()
        try:
            sbx = self._instance()
        except Exception as e:
            return _result(index, getattr(step, "command_string", "") or "", cwd, 127, "", str(e), started, False)
        command = getattr(step, "command_string", None)
        label = command
        if not command:
            script = getattr(step, "script", None)
            if script is None:
                return _result(index, "", cwd, 127, "", "step has neither command_string nor script", started, False)
            language = str(getattr(script, "language", "bash")).lower()
            runner, ext = _SCRIPT_RUNNERS.get(language, ("bash", ".sh"))
            path = f"{WORK_MOUNT}/{SCRIPT_DIRNAME}/step-{index}{ext}"
            sbx.files.write(path, str(getattr(script, "code", "")))
            command, label = f"{runner} {path}", f"[{language} script] {path}"
        try:
            sbx.commands.run(f"mkdir -p {cwd}", timeout=30)
        except Exception:
            pass
        timed_out = False
        try:
            out = sbx.commands.run(command, cwd=cwd, timeout=timeout)
            exit_code = int(getattr(out, "exit_code", 0) or 0)
            stdout = str(getattr(out, "stdout", "") or "")
            stderr = str(getattr(out, "stderr", "") or "")
        except Exception as e:  # CommandExitException / TimeoutException from the SDK
            exit_code = int(getattr(e, "exit_code", 1) or 1)
            stdout = str(getattr(e, "stdout", "") or "")
            stderr = str(getattr(e, "stderr", "") or "") or str(e)
            if "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower():
                timed_out, exit_code = True, TIMEOUT_EXIT_CODE
        return _result(index, label or "", cwd, exit_code, stdout, stderr, started, timed_out)

    def extract(self, relpath: str) -> bytes:
        sbx = self._instance()
        path = relpath if relpath.startswith("/") else f"{WORK_MOUNT}/{relpath}"
        data = sbx.files.read(path, format="bytes")
        return bytes(data) if not isinstance(data, str) else data.encode("utf-8")

    def destroy(self) -> None:
        sbx, self._sbx = self._sbx, None
        if sbx is None:
            return
        try:
            sbx.kill()
        except Exception:
            pass


def _candidates(preference: str, job_dir: str | os.PathLike[str], settings_like: Any) -> list[DisposableSandbox]:
    image = _setting(settings_like, "dockerImage", "docker_image", default=os.environ.get("O_DOCKER_IMAGE") or DEFAULT_IMAGE)
    network = _setting(settings_like, "dockerNetwork", "docker_network", default=None)
    if not (isinstance(network, str) and network.strip()):
        network = os.environ.get("O_DOCKER_NETWORK") or DEFAULT_NETWORK
    api_key = _setting(settings_like, "e2bApiKey", "e2b_api_key", default=os.environ.get("E2B_API_KEY", ""))
    template = _setting(settings_like, "e2bTemplate", "e2b_template", default="base")
    by_name: dict[str, DisposableSandbox] = {}
    order: list[str] = []
    for name in (preference, "docker"):
        if name in ("e2b", "docker") and name not in order:
            order.append(name)
    for name in order:
        if name == "docker":
            by_name[name] = DockerSandbox(image=image, job_dir=job_dir, network=network)
        else:
            by_name[name] = E2BSandbox(api_key=api_key, template=template, job_dir=job_dir)
    return [by_name[n] for n in order]


def select_sandbox(preference: str, job_dir: str | os.PathLike[str], settings_like: Any = None,
                   log: Any = None) -> DisposableSandbox | None:
    """First available of [preference, docker]; None means the local path sandbox. Fall-backs are logged."""
    preference = (preference or "docker").lower()
    if preference not in BACKENDS:
        _record(log, "note", f"unknown highRiskSandbox {preference!r}; using docker", preference=preference)
        preference = "docker"
    if preference == "local":
        _record(log, "note", "high-risk sandbox preference is local; running on the host path sandbox", preference="local")
        return None
    tried: list[str] = []
    for candidate in _candidates(preference, job_dir, settings_like):
        if candidate.available:
            if candidate.name != preference:
                _record(log, "note", f"{preference} sandbox unavailable; falling back to {candidate.name} ({'; '.join(tried)})",
                        preference=preference, selected=candidate.name, tried=tried)
            return candidate
        tried.append(f"{candidate.name}: {candidate.reason}")
    _record(log, "note", f"no disposable sandbox available; falling back to local path sandbox ({'; '.join(tried)})",
            preference=preference, selected="local", tried=tried)
    return None


def run_high_risk(request: Any, job_dir: str | os.PathLike[str], preference: str, settings_like: Any = None,
                  log: Any = None, local_sandbox: Any = None, job_id: str = "", runner: Any = None) -> list[StepResult]:
    """Run every step of a high-risk request in a disposable sandbox, then destroy it.

    All-zero exits mean the caller stores the script in the cache as usual. A step that
    fails stops the run (like the execute node) and the sandbox is still destroyed.
    On the local fallback, script steps go through ``runner`` (the native runner when
    none is given) because the path sandbox only runs command steps.
    """
    risk = getattr(request, "risk", "normal")
    steps: Sequence[Any] = tuple(getattr(request, "steps", ()) or ())
    sandbox = select_sandbox(preference, job_dir, settings_like, log)
    prefix = f"{job_id}: " if job_id else ""
    results: list[StepResult] = []
    if sandbox is None:
        if local_sandbox is None:
            raise RuntimeError("no disposable sandbox available and no local sandbox given")
        _record(log, "note", f"{prefix}high-risk request ({risk}) running on the local path sandbox", job_id=job_id, risk=risk, backend="local")
        isolated_display = bool(getattr(request, "isolated_display", False))
        for index, step in enumerate(steps):
            script = getattr(step, "script", None)
            if script is not None:
                if runner is None:
                    from .runners import get_runner

                    runner = get_runner("native")
                result = runner.run(script, local_sandbox, index, None, isolated_display)
            else:
                result = local_sandbox.run(step, index)
            results.append(result)
            _record(log, "result", f"{prefix}step {index} exit={result.exit_code} [local] {result.command[:80]}",
                    job_id=job_id, index=index, backend="local", exit_code=result.exit_code,
                    elapsed_ms=result.elapsed_ms, timed_out=result.timed_out)
            if result.exit_code != 0 or result.timed_out:
                break
        return results
    network = getattr(sandbox, "network", None)
    net_detail = {"network": network} if network is not None else {}
    net_note = f" network={network}" if network is not None else ""
    _record(log, "note", f"{prefix}high-risk request ({risk}) running in {sandbox.name} sandbox{net_note}",
            job_id=job_id, risk=risk, backend=sandbox.name, **net_detail)
    try:
        with sandbox:
            for index, step in enumerate(steps):
                result = sandbox.run(step, index)
                results.append(result)
                _record(log, "result", f"{prefix}step {index} exit={result.exit_code} [{sandbox.name}] {result.command[:80]}",
                        job_id=job_id, index=index, backend=sandbox.name, exit_code=result.exit_code,
                        elapsed_ms=result.elapsed_ms, timed_out=result.timed_out, **net_detail)
                if result.exit_code != 0 or result.timed_out:
                    break
    finally:
        ok = bool(results) and len(results) == len(steps) and all(r.exit_code == 0 and not r.timed_out for r in results)
        _record(log, "cache", f"{prefix}high-risk run {'succeeded; script eligible for cache' if ok else 'failed; nothing cached'}",
                job_id=job_id, backend=sandbox.name, ok=ok, steps=len(results))
        _record(log, "note", f"{prefix}disposable sandbox destroyed ({sandbox.name})", job_id=job_id, backend=sandbox.name)
    return results
