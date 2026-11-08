"""Headless web automation.

Two runners share one result shape:

* ``BrowserUseRunner`` drives ``browser_use.Agent`` with an OpenAI-compatible model under
  headless Chromium: the local vLLM when it answers, else O's own API proxy (main/proxy.js,
  ``O_PROXY_URL`` / ``O_PROXY_TOKEN``), which runs the user's Claude subscription on the fast
  model (``O_FAST_MODEL``, default haiku). See ``select_web_llm``. ``available`` is False when the package
  is not importable; ``run`` never raises and reports failures in ``error``.
* ``PlaywrightDomRunner`` executes a short list of scripted DOM actions
  (``goto`` / ``fill`` / ``click`` / ``read`` / ``wait``) with sync Playwright,
  the same headless pattern ``verify.py`` uses. DOM only; no screenshots.

Both strip the host ``DISPLAY`` and use only the isolated display's env, so
nothing here can open a window on the user's desktop. There is no CAPTCHA or
proxy handling anywhere in this module by design.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

WebAction = Literal["goto", "fill", "click", "read", "wait"]
WEB_ACTIONS: tuple[str, ...] = ("goto", "fill", "click", "read", "wait")
DEFAULT_TIMEOUT_S = 300
DOM_TIMEOUT_S = 60
MAX_STEPS = 50
MAX_TEXT = 20000
_HOST_DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY")


class WebResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    final_text: str = ""
    steps: list[dict] = Field(default_factory=list)
    screenshots_sha: list[str] = Field(default_factory=list)
    via: str
    error: Optional[str] = None
    elapsed_ms: int = Field(default=0, ge=0)


class DomAction(BaseModel):
    """One scripted DOM action. Strict: unknown keys are rejected."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    op: WebAction
    selector: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    text: Optional[str] = Field(default=None, max_length=MAX_TEXT)
    url: Optional[str] = Field(default=None, min_length=1, max_length=4096)
    seconds: Optional[float] = Field(default=None, ge=0, le=60)


def _display_env(display: Any) -> dict[str, str]:
    """Only the isolated display's variables; the host DISPLAY is never passed through."""
    if display is None:
        return {}
    if isinstance(display, dict):
        return {k: str(v) for k, v in display.items() if k in _HOST_DISPLAY_VARS}
    env_fn = getattr(display, "env", None)
    if callable(env_fn):
        try:
            return {k: str(v) for k, v in dict(env_fn() or {}).items() if k in _HOST_DISPLAY_VARS}
        except Exception:
            return {}
    if isinstance(display, str) and display.startswith(":"):
        return {"DISPLAY": display}
    return {}


_env_lock = threading.Lock()


@contextmanager
def _scoped_display(display: Any) -> Iterator[dict[str, str]]:
    """Temporarily replace DISPLAY/WAYLAND_DISPLAY in os.environ with the isolated display's (or nothing).

    Playwright and browser-use launch Chromium from the current process environment, so the
    host display is removed for the duration of the run and restored afterwards.
    """
    wanted = _display_env(display)
    with _env_lock:
        saved = {k: os.environ.get(k) for k in _HOST_DISPLAY_VARS}
        for k in _HOST_DISPLAY_VARS:
            os.environ.pop(k, None)
        os.environ.update(wanted)
        try:
            yield wanted
        finally:
            for k in _HOST_DISPLAY_VARS:
                os.environ.pop(k, None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _err(e: BaseException) -> str:
    first = (str(e) or "").splitlines()
    return f"{type(e).__name__}: {first[0][:300] if first else ''}"


# ----------------------------------------------------------------- browser-use

PROXY_MODEL = "o-ai-code"  # main/proxy.js: JSON-only answers (padding stripped)
PROXY_CALLER = "browser-use"  # x-o-caller: the proxy runs the fast model with no tools
WEB_PROBE_TIMEOUT_S = 2.0


def _endpoint_answers(base_url: str, token: str = "") -> bool:
    """GET <base_url>/models answers 200 within WEB_PROBE_TIMEOUT_S."""
    if not base_url:
        return False
    try:
        import httpx

        headers = {"authorization": f"Bearer {token}"} if token else {}
        r = httpx.get(base_url.rstrip("/") + "/models", headers=headers, timeout=WEB_PROBE_TIMEOUT_S)
        return r.status_code == 200
    except Exception:
        return False


def select_web_llm(vllm_url: str, vllm_model: str, proxy_url: str = "", proxy_token: str = "",
                   fast_model: str = "", probe: Any = None) -> dict[str, Any]:
    """Which OpenAI-compatible endpoint browser-use talks to.

    vLLM when its /models answers (unchanged behaviour); else O's proxy when it is configured and answers;
    else vLLM anyway, so the error names the endpoint that was tried. Returns
    ``{via, base_url, model, api_key, headers, fast_model}``; ``via`` is "vllm", "proxy" or "none"."""
    probe = probe or _endpoint_answers
    vllm_url = (vllm_url or "").rstrip("/")
    proxy_url = (proxy_url or "").rstrip("/")
    vllm_key = os.environ.get("OPENAI_API_KEY") or "local"
    if vllm_url and probe(vllm_url, ""):
        return {"via": "vllm", "base_url": vllm_url, "model": vllm_model, "api_key": vllm_key, "headers": {}, "fast_model": ""}
    if proxy_url and proxy_token and probe(proxy_url, proxy_token):
        fm = (fast_model or "haiku").strip() or "haiku"
        return {"via": "proxy", "base_url": proxy_url, "model": PROXY_MODEL, "api_key": proxy_token,
                "headers": {"x-o-caller": PROXY_CALLER}, "fast_model": fm}
    return {"via": "none", "base_url": vllm_url, "model": vllm_model, "api_key": vllm_key, "headers": {}, "fast_model": ""}



class BrowserUseRunner:
    """``browser_use.Agent`` + headless Chromium, LLM = OpenAI-compatible endpoint at ``vllm_url``."""

    via = "browser-use"

    def __init__(self, vllm_url: str, model: str, display: Any = None, timeout_s: int = DEFAULT_TIMEOUT_S,
                 proxy_url: str = "", proxy_token: str = "", fast_model: str = ""):
        self.vllm_url = (vllm_url or "").rstrip("/")
        self.model = model
        self.display = display
        self.timeout_s = int(timeout_s)
        self.proxy_url = (proxy_url or "").rstrip("/")
        self.proxy_token = proxy_token or ""
        self.fast_model = fast_model or ""
        self.endpoint: dict[str, Any] | None = None  # set by run(): the select_web_llm choice

    def _endpoint(self) -> dict[str, Any]:
        if self.endpoint is None:
            self.endpoint = select_web_llm(self.vllm_url, self.model, self.proxy_url, self.proxy_token, self.fast_model)
        return self.endpoint

    @property
    def available(self) -> bool:
        try:
            from browser_use import Agent, Browser  # noqa: F401
        except Exception:
            return False
        return True

    # browser_use has changed its LLM surface across releases: older versions take a
    # langchain chat model, newer ones ship their own ``ChatOpenAI``. Try each in turn.
    def _make_llm(self) -> Any:
        ep = self._endpoint()
        base = {"model": ep["model"], "base_url": ep["base_url"], "api_key": ep["api_key"]}
        extra: dict[str, Any] = {}
        if ep["headers"]:
            extra["default_headers"] = dict(ep["headers"])
        if ep["via"] == "proxy":
            # the proxy flattens messages to text: put the output schema in the prompt as well
            extra["add_schema_to_system_prompt"] = True
        errors: list[str] = []
        try:
            from browser_use.llm import ChatOpenAI as BUChatOpenAI  # type: ignore

            return _construct(BUChatOpenAI, base, extra)
        except Exception as e:
            errors.append(_err(e))
        try:
            from langchain_openai import ChatOpenAI  # type: ignore

            return _construct(ChatOpenAI, {**base, "temperature": 0}, extra)
        except Exception as e:
            errors.append(_err(e))
        raise RuntimeError("no OpenAI-compatible chat model class importable (" + "; ".join(errors) + ")")

    def _agent_kwargs(self) -> dict[str, Any]:
        # The proxy carries text only (images are dropped), so browser-use works from the DOM there.
        return {"use_vision": False} if self._endpoint()["via"] == "proxy" else {}

    def _make_browser(self) -> Any:
        from browser_use import Browser  # type: ignore

        params = inspect.signature(Browser).parameters
        kwargs: dict[str, Any] = {}
        if "headless" in params:
            kwargs["headless"] = True
        if "config" in params:
            try:
                from browser_use import BrowserConfig  # type: ignore

                kwargs["config"] = BrowserConfig(headless=True)
            except Exception:
                pass
        return Browser(**kwargs)

    @staticmethod
    def _history_to_steps(history: Any) -> tuple[list[dict], str, list[str]]:
        steps: list[dict] = []
        final = ""
        shas: list[str] = []
        if history is None:
            return steps, final, shas
        try:
            fr = getattr(history, "final_result", None)
            final = str(fr() if callable(fr) else fr or "")
        except Exception:
            final = ""
        try:
            names = getattr(history, "action_names", None)
            names = list(names() if callable(names) else names or [])
            for i, n in enumerate(names[:MAX_STEPS]):
                steps.append({"index": i, "action": str(n)})
        except Exception:
            pass
        try:
            shots = getattr(history, "screenshots", None)
            shots = list(shots() if callable(shots) else shots or [])
            for s in shots[:MAX_STEPS]:
                if isinstance(s, str) and s:
                    shas.append(_sha(s.encode("utf-8")))
                elif isinstance(s, (bytes, bytearray)):
                    shas.append(_sha(bytes(s)))
        except Exception:
            pass
        return steps, final, shas

    def run(self, task: str, url: str | None = None) -> WebResult:
        started = time.monotonic()
        if not self.available:
            return WebResult(ok=False, via=self.via, error="browser_use is not installed",
                             elapsed_ms=int((time.monotonic() - started) * 1000))
        full_task = f"Open {url} first. {task}" if url else task
        browser = None
        try:
            with _scoped_display(self.display):
                from browser_use import Agent  # type: ignore

                llm = self._make_llm()
                browser = self._make_browser()
                agent = _construct(Agent, {"task": full_task, "llm": llm, "browser": browser}, self._agent_kwargs())

                async def _go() -> Any:
                    return await asyncio.wait_for(agent.run(), timeout=self.timeout_s)

                history = asyncio.run(_go())
                steps, final, shas = self._history_to_steps(history)
            return WebResult(ok=True, final_text=final[:MAX_TEXT], steps=steps, screenshots_sha=shas, via=self.via,
                             elapsed_ms=int((time.monotonic() - started) * 1000))
        except BaseException as e:  # never raises
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            return WebResult(ok=False, via=self.via, error=_err(e), elapsed_ms=int((time.monotonic() - started) * 1000))
        finally:
            close = getattr(browser, "close", None)
            if callable(close):
                try:
                    r = close()
                    if inspect.isawaitable(r):
                        asyncio.run(r)
                except Exception:
                    pass


def _construct(cls: Any, base: dict[str, Any], extra: dict[str, Any]) -> Any:
    """``cls(**base, **extra)`` keeping only the ``extra`` keywords ``cls`` accepts (browser-use's surface
    changes across releases); ``base`` is always passed."""
    if extra:
        try:
            params = inspect.signature(cls).parameters
            if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
                extra = {k: v for k, v in extra.items() if k in params}
        except (TypeError, ValueError):
            extra = {}
    return cls(**base, **extra)


# ------------------------------------------------------------ playwright DOM


class PlaywrightDomRunner:
    """Scripted DOM actions under headless Chromium. No model, no screenshots."""

    via = "playwright-dom"

    def __init__(self, display: Any = None):
        self.display = display

    @property
    def available(self) -> bool:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except Exception:
            return False
        return True

    @staticmethod
    def _parse(actions: list[Any]) -> list[DomAction]:
        if not isinstance(actions, (list, tuple)):
            raise ValueError("actions must be a list")
        if len(actions) > MAX_STEPS:
            raise ValueError(f"at most {MAX_STEPS} actions")
        out: list[DomAction] = []
        for a in actions:
            out.append(a if isinstance(a, DomAction) else DomAction.model_validate(a))
        return out

    def run(self, actions: list[Any], timeout_s: int = DOM_TIMEOUT_S) -> WebResult:
        started = time.monotonic()
        steps: list[dict] = []
        texts: list[str] = []

        def done(ok: bool, error: str | None = None) -> WebResult:
            return WebResult(ok=ok, final_text="\n".join(texts)[:MAX_TEXT], steps=steps, screenshots_sha=[],
                             via=self.via, error=error, elapsed_ms=int((time.monotonic() - started) * 1000))

        try:
            parsed = self._parse(actions)
        except Exception as e:
            return done(False, _err(e))
        if not self.available:
            return done(False, "playwright is not installed")
        timeout_ms = max(1, int(timeout_s)) * 1000
        try:
            with _scoped_display(self.display):
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        page.set_default_timeout(timeout_ms)
                        for index, a in enumerate(parsed):
                            t0 = time.monotonic()
                            rec: dict[str, Any] = {"index": index, "op": a.op, "ok": True}
                            try:
                                if a.op == "goto":
                                    if not a.url:
                                        raise ValueError("goto needs url")
                                    resp = page.goto(a.url, timeout=timeout_ms, wait_until="load")
                                    rec["url"] = a.url
                                    rec["status"] = int(resp.status) if resp is not None else None
                                elif a.op == "fill":
                                    if not a.selector:
                                        raise ValueError("fill needs selector")
                                    page.fill(a.selector, a.text or "")
                                    rec["selector"] = a.selector
                                elif a.op == "click":
                                    if not a.selector:
                                        raise ValueError("click needs selector")
                                    page.click(a.selector)
                                    rec["selector"] = a.selector
                                elif a.op == "read":
                                    text = page.inner_text(a.selector) if a.selector else page.inner_text("body")
                                    text = (text or "").strip()[:MAX_TEXT]
                                    rec["selector"] = a.selector or "body"
                                    rec["text"] = text
                                    texts.append(text)
                                elif a.op == "wait":
                                    if a.selector:
                                        page.wait_for_selector(a.selector, timeout=timeout_ms)
                                        rec["selector"] = a.selector
                                    else:
                                        secs = float(a.seconds if a.seconds is not None else 1.0)
                                        page.wait_for_timeout(min(secs, 60.0) * 1000)
                                        rec["seconds"] = secs
                            except Exception as e:
                                rec["ok"] = False
                                rec["error"] = _err(e)
                                rec["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
                                steps.append(rec)
                                return done(False, f"step {index} ({a.op}) failed: {rec['error']}")
                            rec["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
                            steps.append(rec)
                    finally:
                        browser.close()
            return done(True)
        except Exception as e:
            return done(False, _err(e))


# ----------------------------------------------------------------- selection


def get_web_runner(prefer: str = "browser-use", vllm_url: str = "", model: str = "", display: Any = None,
                   timeout_s: int = DEFAULT_TIMEOUT_S, proxy_url: str = "", proxy_token: str = "",
                   fast_model: str = "") -> Any:
    """``browser-use`` when importable (and preferred), else the Playwright DOM runner."""
    prefer = (prefer or "browser-use").lower()
    if prefer in ("browser-use", "browser_use", "browseruse"):
        bu = BrowserUseRunner(vllm_url, model, display=display, timeout_s=timeout_s,
                              proxy_url=proxy_url, proxy_token=proxy_token, fast_model=fast_model)
        if bu.available:
            return bu
    return PlaywrightDomRunner(display=display)
