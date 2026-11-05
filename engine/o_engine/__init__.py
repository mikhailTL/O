"""O AI engine.

Deterministic provisioning for local dev environments:

- schema     strict, immutable step schema (pydantic)
- graph      fixed linear DAG (langgraph); edges are hardcoded and never rewritten at runtime
- gate       the circuit-breaker: log-only (default), ask (blocking approval, 60 s), or off
- executor   sandboxed shell runner confined to one directory tree
- cache      append-only script cache; exit 0 -> hashed and saved; identical request -> replayed, no inference
- telemetry  screenpipe SQLite adapter + local JSONL fallback, feeds crash diagnostics
- parser     local vLLM (OpenAI-compatible) error -> structured steps; Claude Code CLI fallback
- verify     headless Playwright check of local ports
- display    isolated Xvfb display for background UI tests
- server     localhost HTTP API used by the O desktop app
"""

__version__ = "1.0.0"
