"""Level tracker contract (Sense owns the real side-cam tracker in sense/) and a fake tube.

The real tracker runs its own 30 FPS thread; read() returns the latest sample and never blocks.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np


@dataclass
class LevelSample:
    ml: float     # volume above the last zero(), mL
    px: int       # liquid column height in the crop, px
    conf: float   # 0..1, drops to 0 if the crop is dark, occluded or the tube is missing
    ts: float     # time.time() of the frame


def px_to_ml(px: float, curve: Sequence[float]) -> float:
    return float(np.polyval(curve, px))


class LevelTracker(ABC):
    @abstractmethod
    def start(self, cam_index: int, crop: tuple[int, int, int, int], curve: Sequence[float]) -> None:
        """Start the capture thread. crop = (x, y, w, h) px, curve = px->mL coefficients."""

    @abstractmethod
    def read(self) -> LevelSample:
        """Latest sample. Never blocks."""

    @abstractmethod
    def zero(self) -> None:
        """Treat the current level as 0 mL."""


class FakeLevelTracker(LevelTracker):
    """A simulated tube. The fake world pours into it with add(); read() returns what the camera would see.

    stuck=True simulates a tracker that stops seeing the level rise.
    """

    def __init__(self, noise_ml: float = 0.02, seed: int = 0):
        self.true_ml = 0.0
        self.noise_ml = noise_ml
        self.curve: Sequence[float] = [0.06, 0.0]
        self.conf = 1.0
        self.stuck = False
        self.started = False
        self._zero_ml = 0.0
        self._rng = np.random.default_rng(seed)

    def start(self, cam_index=0, crop=(0, 0, 0, 0), curve=None):
        self.started = True
        if curve is not None:
            self.curve = curve

    def read(self) -> LevelSample:
        seen = self.true_ml + self._rng.normal(0.0, self.noise_ml)
        a, b = self.curve[-2], self.curve[-1]
        px = max(0, int(round((seen - b) / a)))
        return LevelSample(ml=px_to_ml(px, self.curve) - self._zero_ml, px=px, conf=self.conf, ts=time.time())

    def zero(self):
        self._zero_ml = px_to_ml(max(0, int(round((self.true_ml - self.curve[-1]) / self.curve[-2]))), self.curve)

    # fake-only helpers
    def add(self, ml: float) -> None:
        if not self.stuck:
            self.true_ml += ml

    def empty(self) -> None:
        self.true_ml = 0.0
        self._zero_ml = 0.0
