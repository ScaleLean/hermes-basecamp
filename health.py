"""Safe health, circuit, and metrics state for Basecamp runtime lanes."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CircuitState:
    failure_threshold: int = 5
    reset_seconds: float = 60.0
    failures: int = 0
    opened_at: float | None = None

    @property
    def open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.reset_seconds:
            self.failures = 0
            self.opened_at = None
            return False
        return True

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold and self.opened_at is None:
            self.opened_at = time.monotonic()


@dataclass
class RuntimeHealth:
    connected: bool = False
    identity_ok: bool = False
    role_ok: bool = False
    revoked: bool = False
    last_success_at: float | None = None
    counters: dict[str, int] = field(default_factory=dict)
    circuits: dict[str, CircuitState] = field(default_factory=dict)

    def mark(self, metric: str, amount: int = 1) -> None:
        self.counters[metric] = self.counters.get(metric, 0) + amount

    def lane(self, name: str) -> CircuitState:
        return self.circuits.setdefault(name, CircuitState())

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "identity_ok": self.identity_ok,
            "role_ok": self.role_ok,
            "revoked": self.revoked,
            "last_success_at": self.last_success_at,
            "counters": dict(self.counters),
            "circuits": {
                name: {"open": state.open, "failures": state.failures} for name, state in self.circuits.items()
            },
        }
