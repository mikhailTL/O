"""Stream filter that lets only the first balanced top-level JSON value through.

Model output arrives as a stream of tokens that may be wrapped in prose or
markdown fences. ``TokenFilter`` tracks JSON string and escape state so that
brackets inside string literals do not affect depth; everything before the
opening ``[``/``{`` and after its matching close is discarded.
"""
from __future__ import annotations

from typing import Iterable

_OPENERS = {"[": "]", "{": "}"}
_CLOSERS = {"]", "}"}


class TokenFilter:
    """Incremental filter. ``feed`` returns the characters that belong to the JSON value."""

    __slots__ = ("_started", "_done", "_depth", "_in_string", "_escape", "_buf")

    def __init__(self) -> None:
        self._started = False
        self._done = False
        self._depth = 0
        self._in_string = False
        self._escape = False
        self._buf: list[str] = []

    @property
    def done(self) -> bool:
        return self._done

    @property
    def started(self) -> bool:
        return self._started

    @property
    def text(self) -> str:
        return "".join(self._buf)

    def feed(self, token: str) -> str:
        if self._done or not token:
            return ""
        out: list[str] = []
        for ch in token:
            if not self._started:
                if ch in _OPENERS:
                    self._started = True
                    self._depth = 1
                    out.append(ch)
                continue
            out.append(ch)
            if self._in_string:
                if self._escape:
                    self._escape = False
                elif ch == "\\":
                    self._escape = True
                elif ch == '"':
                    self._in_string = False
                continue
            if ch == '"':
                self._in_string = True
            elif ch in _OPENERS:
                self._depth += 1
            elif ch in _CLOSERS:
                self._depth -= 1
                if self._depth == 0:
                    self._done = True
                    break
        chunk = "".join(out)
        if chunk:
            self._buf.append(chunk)
        return chunk

    def filter_stream(self, tokens: Iterable[str]) -> str:
        for tok in tokens:
            self.feed(tok)
            if self._done:
                break
        return self.text


def filter_stream(tokens: Iterable[str], token_filter: TokenFilter | None = None) -> str:
    """Run an iterable of tokens through a filter and return the extracted JSON text."""
    tf = token_filter if token_filter is not None else TokenFilter()
    return tf.filter_stream(tokens)
