"""Level reading contract for pour/.

read() must be cheap (adaptive_pour polls at 5 Hz).  prompt(msg) is for interactive moments
(start / end of a pour).  The manual backend caches whatever was last prompted and returns
that from read(), so 5 Hz polling never spams the user; the opencv backend uses Amogh's
external camera model and must do the real work in a thread so read() is non-blocking.

Backends:
  manual   prompts on prompt(); read() returns the cached last-typed value.
  opencv   NOT YET WIRED.  Will be one of: Python function import, JSON/CSV file, or socket.
           Tell me the format and I'll wire it up here.

Usage:
  lv = LevelSource(channel=3, backend="manual")
  start = lv.prompt("starting mL")
  while pouring:
      cur = lv.read()
      ...
  final = lv.prompt("final mL")
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class LevelSource:
    channel: int
    backend: str = "manual"

    def __post_init__(self):
        self._last: float = 0.0
        if self.backend not in ("manual", "opencv"):
            raise ValueError(f"unknown level backend {self.backend!r}; use 'manual' or 'opencv'")

    def read(self) -> float:
        """Return the latest known mL in self.channel. Cheap, non-blocking."""
        if self.backend == "manual":
            return self._last
        if self.backend == "opencv":
            raise NotImplementedError(
                "opencv backend not wired. Tell me how Amogh's model exposes the reading: "
                "(a) Python function import, (b) JSON/CSV path, or (c) host:port socket."
            )
        raise AssertionError

    def prompt(self, msg: str = "current mL") -> float:
        """Force a fresh reading. For manual, prompts the user; for opencv, same as read()."""
        if self.backend == "manual":
            while True:
                raw = input(f"[level_source] channel {self.channel}: {msg} > ").strip()
                try:
                    self._last = float(raw)
                    return self._last
                except ValueError:
                    print(f"  not a number: {raw!r}")
        if self.backend == "opencv":
            return self.read()  # once wired, this should just read the fresh value
        raise AssertionError


def default_backend() -> str:
    """CLI convention: --level=... or env POUR_LEVEL=... overrides, else manual."""
    return os.environ.get("POUR_LEVEL", "manual")
