"""Era profile loading: one declarative source of truth per era."""
from __future__ import annotations

import dataclasses
import pathlib

import yaml


@dataclasses.dataclass
class EraProfile:
    """A configured point in time for the internet Dialback serves.

    Fields derived from the YAML file:
      start/end as compact YYYYMMDD ints, suitable for CDX range filters
      and numeric closeness comparison.
    """

    name: str
    start: int                 # YYYYMMDD
    end: int                   # YYYYMMDD
    raw: dict                  # full profile for style hints / providers

    @property
    def label(self) -> str:
        return self.name

    @classmethod
    def load(cls, era_dir: str | pathlib.Path, name: str) -> "EraProfile":
        path = pathlib.Path(era_dir) / f"{name}.yaml"
        with open(path) as f:
            raw = yaml.safe_load(f)
        start = raw["start_date"].replace("-", "")
        end = raw["end_date"].replace("-", "")
        return cls(name=name, start=int(start), end=int(end), raw=raw)
