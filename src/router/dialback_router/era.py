"""Era profile loading: one declarative source of truth per era."""
from __future__ import annotations

import dataclasses
import datetime
import pathlib

import yaml

# Nothing is served from before the floor date (Dialback's horizon)
FLOOR_DATE = "1996-10-01"
FLOOR = 19961001


@dataclasses.dataclass
class EraProfile:
    """A configured point in time for the internet Dialback serves.

    start/end are compact YYYYMMDD ints bounding the archive search window;
    target is the ideal capture date (archive provider picks the snapshot
    closest to it).
    """

    name: str                 # label shown in UI/logs ("1997", "1999-03-15")
    start: int                # YYYYMMDD
    end: int                  # YYYYMMDD
    target: int               # YYYYMMDD - the dialed date
    raw: dict                 # full profile for style hints / providers

    @property
    def label(self) -> str:
        return self.name

    @classmethod
    def load(cls, era_dir: str | pathlib.Path, name: str) -> "EraProfile":
        path = pathlib.Path(era_dir) / f"{name}.yaml"
        with open(path) as f:
            raw = yaml.safe_load(f)
        start = int(raw["start_date"].replace("-", ""))
        end = int(raw["end_date"].replace("-", ""))
        return cls(name=name, start=start, end=end,
                   target=(start + end) // 2, raw=raw)

    @classmethod
    def for_date(cls, date_iso: str) -> "EraProfile":
        """Synthetic profile for an arbitrary dialed date (YYYY-MM-DD).

        The archive window runs from the Dialback floor up to the chosen
        date: browsing 'as of' a day serves the latest snapshots that
        existed by then — no leaks from the future.
        """
        t = int(date_iso.replace("-", ""))
        return cls(name=date_iso, start=min(FLOOR, t), end=t, target=t,
                   raw={
                       "name": date_iso,
                       "start_date": f"{t // 10000:04d}-{t // 100 % 100:02d}-{t % 100:02d}",
                       "end_date": f"{t // 10000:04d}-{t // 100 % 100:02d}-{t % 100:02d}",
                   })


def valid_date(date_iso: str) -> bool:
    """Must parse and fall within [floor, today]."""
    try:
        d = datetime.date.fromisoformat(date_iso)
    except ValueError:
        return False
    floor = datetime.date.fromisoformat(FLOOR_DATE)
    return floor <= d <= datetime.date.today()
