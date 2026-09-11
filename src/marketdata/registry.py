# marketdata/registry.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

import pandas as pd

from marketdata.data_types import DataType, DataProvider, GapStrategy


class DataSource(Protocol):
    def fetch(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame: ...


@dataclass
class SourceRegistration:
    data_type:  DataType
    provider:   DataProvider
    adapter:    DataSource
    resolve_fn: Callable[[str], Optional[str]]
    strategy:   GapStrategy
    priority:   int = 0
    columns:    Optional[list[str]] = None  # post-fetch column filter; None = keep all
    # Bar width. Every current source is hourly, so the default is correct
    # everywhere today; a future sub-hourly source must set this explicitly or
    # _fill_gaps's "is this bar closed yet" check silently uses the wrong unit.
    interval:   pd.Timedelta = pd.Timedelta(hours=1)


@dataclass
class ResolvedAsset:
    registration: SourceRegistration
    symbol:       str


class SourceRegistry:
    def __init__(self) -> None:
        self._registrations: list[SourceRegistration] = []

    def register(self, reg: SourceRegistration) -> None:
        self._registrations.append(reg)

    def build_plan(
        self,
        asset_ids: list[str],
        data_type: DataType,
    ) -> dict[str, ResolvedAsset]:
        """Pre-compute asset_id → (winning registration, resolved symbol).

        For each asset, tries registrations for data_type in descending priority order.
        Returns only assets that resolved; others are silently excluded.
        """
        candidates = sorted(
            [r for r in self._registrations if r.data_type == data_type],
            key=lambda r: r.priority,
            reverse=True,
        )
        plan: dict[str, ResolvedAsset] = {}
        for asset_id in asset_ids:
            for reg in candidates:
                sym = reg.resolve_fn(asset_id)
                if sym is not None:
                    plan[asset_id] = ResolvedAsset(registration=reg, symbol=sym)
                    break
        return plan
