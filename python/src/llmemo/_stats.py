"""Running totals (A4) and the spending limit (M6)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, fields
from typing import Any

from ._entry import USAGE_NAMES
from ._errors import SpendLimitExceeded


@dataclass
class Totals:
    """Totals for a set of calls. `cached_*` is what replayed calls originally cost or took;
    `uncached_*` is what this run spent. Token counts use the metadata's names, e.g.
    `inputTokens` (spec §5.4)."""

    hits: int = 0
    misses: int = 0
    cached_cost_usd: float = 0.0
    uncached_cost_usd: float = 0.0
    cached_latency_ms: int = 0
    uncached_latency_ms: int = 0
    cached_tokens: dict[str, int] = field(default_factory=dict)
    uncached_tokens: dict[str, int] = field(default_factory=dict)

    @property
    def calls(self) -> int:
        return self.hits + self.misses

    @property
    def cost_without_cache_usd(self) -> float:
        """What the run would have cost if nothing had been cached (Goal 6)."""
        return self.cached_cost_usd + self.uncached_cost_usd

    def add(self, meta: dict[str, Any], cached: bool) -> None:
        prefix = "cached" if cached else "uncached"
        if cached:
            self.hits += 1
        else:
            self.misses += 1
        cost = meta.get("costUsd")
        if isinstance(cost, (int, float)):
            setattr(self, f"{prefix}_cost_usd", getattr(self, f"{prefix}_cost_usd") + cost)
        latency = meta.get("latencyMs")
        if isinstance(latency, int):
            setattr(self, f"{prefix}_latency_ms", getattr(self, f"{prefix}_latency_ms") + latency)
        tokens: dict[str, int] = getattr(self, f"{prefix}_tokens")
        usage = meta.get("usage")
        if isinstance(usage, dict):
            for name in USAGE_NAMES:
                if isinstance(usage.get(name), int):
                    tokens[name] = tokens.get(name, 0) + usage[name]

    def __add__(self, other: Totals) -> Totals:
        out = Totals()
        for f in fields(Totals):
            a, b = getattr(self, f.name), getattr(other, f.name)
            if isinstance(a, dict):
                setattr(out, f.name, {k: a.get(k, 0) + b.get(k, 0) for k in a.keys() | b.keys()})
            else:
                setattr(out, f.name, a + b)
        return out

    def to_json(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(Totals)}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Totals:
        return cls(**{f.name: data[f.name] for f in fields(Totals) if f.name in data})


class Stats:
    """Running totals of hits, misses, cost, latency, and tokens, by label and served model.

    Safe to update from several threads. Add two with `+` to combine runs; `to_json()` and
    `from_json()` save and load them.
    """

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._groups: dict[tuple[str | None, str | None], Totals] = {}

    def add(self, label: str | None, meta: dict[str, Any], cached: bool) -> None:
        model = meta.get("servedModel")
        group = (label, model if isinstance(model, str) else None)
        with self._mutex:
            self._groups.setdefault(group, Totals()).add(meta, cached)

    def _sum(self, by: int | None) -> dict[Any, Totals]:
        out: dict[Any, Totals] = {}
        with self._mutex:
            for group, totals in self._groups.items():
                k = None if by is None else group[by]
                out[k] = out.get(k, Totals()) + totals
        return out

    @property
    def total(self) -> Totals:
        return self._sum(None).get(None, Totals())

    def by_label(self) -> dict[str | None, Totals]:
        return self._sum(0)

    def by_model(self) -> dict[str | None, Totals]:
        """Totals by the model that answered (`meta.servedModel`); None if unreported."""
        return self._sum(1)

    def reset(self) -> None:
        with self._mutex:
            self._groups.clear()

    def __add__(self, other: Stats) -> Stats:
        out = Stats()
        for s in (self, other):
            with s._mutex:
                for group, totals in s._groups.items():
                    out._groups[group] = out._groups.get(group, Totals()) + totals
        return out

    def to_json(self) -> list[dict[str, Any]]:
        with self._mutex:
            return [
                {"label": label, "model": model, **totals.to_json()}
                for (label, model), totals in self._groups.items()
            ]

    @classmethod
    def from_json(cls, data: list[dict[str, Any]]) -> Stats:
        out = cls()
        for row in data:
            out._groups[(row.get("label"), row.get("model"))] = Totals.from_json(row)
        return out

    def __repr__(self) -> str:
        t = self.total
        return (
            f"Stats(hits={t.hits}, misses={t.misses}, uncached_cost_usd={t.uncached_cost_usd:.6f},"
            f" cached_cost_usd={t.cached_cost_usd:.6f})"
        )


class SpendLimit:
    """Stops backend calls once a run has spent `usd` (M6). Share one across caches to
    limit them together.

    The check happens before each call, so calls already running when the limit is
    reached can still finish and add to the total. A Claude subscription's reported
    cost counts too, since it's the best measure of usage available. Calls whose backend
    reports no cost can't be counted, so a warning is logged the first time that happens.
    """

    def __init__(self, usd: float) -> None:
        if not usd >= 0:
            raise ValueError("the spending limit must be a non-negative number of US dollars")
        self.usd = usd
        self.spent_usd = 0.0
        self.warned_unknown_cost = False
        self._mutex = threading.Lock()

    def check(self) -> None:
        with self._mutex:
            if self.spent_usd >= self.usd:
                raise SpendLimitExceeded(
                    f"llmemo stopped before calling the backend: this run has spent"
                    f" ${self.spent_usd:.4f}, and its limit is ${self.usd:.4f}"
                )

    def charge(self, cost_usd: float | None) -> bool:
        """Add a call's cost. Returns False if the cost was unknown."""
        with self._mutex:
            if cost_usd is None:
                return False
            self.spent_usd += cost_usd
            return True

    def __repr__(self) -> str:
        return f"SpendLimit(usd={self.usd}, spent_usd={self.spent_usd})"
