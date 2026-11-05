"""Local text-to-speech: macOS ``say``, Windows SAPI via PowerShell, Linux ``espeak-ng`` / ``spd-say``.

``speak(text)`` returns at once; the voice process runs detached and is reaped on a daemon thread.
Text goes to the process on stdin (or as one argv element for ``spd-say``); no shell is involved.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from typing import Any, Callable

MAX_CHARS = 2000
SAPI = ("Add-Type -AssemblyName System.Speech; "
        "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak([Console]::In.ReadToEnd())")


def tts_command(text: str, platform: str | None = None,
                which: Callable[[str], str | None] = shutil.which) -> tuple[list[str], bytes | None] | None:
    """(argv, stdin bytes) for this platform, or None when no local TTS exists."""
    platform = platform or sys.platform
    text = " ".join(str(text or "").split())[:MAX_CHARS]
    if not text:
        return None
    data = text.encode("utf-8")
    if platform == "darwin" and which("say"):
        return [which("say") or "say", "-f", "-"], data
    if platform.startswith("win"):
        exe = which("powershell") or which("pwsh")
        if exe:
            return [exe, "-NoProfile", "-NonInteractive", "-Command", SAPI], data
        return None
    if which("espeak-ng"):
        return [which("espeak-ng") or "espeak-ng", "--stdin"], data
    if which("spd-say"):
        return [which("spd-say") or "spd-say", "-w", "--", text], None
    return None


def tts_available(platform: str | None = None) -> tuple[bool, str]:
    if tts_command("test", platform) is None:
        return False, "no local TTS found (macOS say, Windows PowerShell SAPI, Linux espeak-ng or spd-say)"
    return True, ""


class Speaker:
    """Opt-in speaker. ``speak`` never blocks and never raises."""

    def __init__(self, enabled: bool = False, log: Any = None, popen: Callable[..., Any] = subprocess.Popen,
                 platform: str | None = None) -> None:
        self.enabled = bool(enabled)
        self.log = log
        self.popen = popen
        self.platform = platform

    def speak(self, text: str) -> bool:
        if not self.enabled:
            return False
        cmd = tts_command(text, self.platform)
        if cmd is None:
            self._note("tts: no local voice available")
            return False
        argv, data = cmd
        try:
            proc = self.popen(argv, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            self._note(f"tts failed to start: {type(exc).__name__}: {exc}")
            return False

        def feed() -> None:
            try:
                if data is not None and proc.stdin is not None:
                    proc.stdin.write(data)
                    proc.stdin.close()
                proc.wait(timeout=120)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        threading.Thread(target=feed, name="o-tts", daemon=True).start()
        self._note("tts: speaking brief", engine=argv[0], chars=len(text))
        return True

    def _note(self, summary: str, **detail: Any) -> None:
        if self.log is not None:
            try:
                self.log.record("note", summary, **detail)
            except Exception:
                pass


def speak(text: str, enabled: bool = True) -> bool:
    return Speaker(enabled).speak(text)
