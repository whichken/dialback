"""Traffic tracking for the control UI: raw requests + per-host rollup."""
from __future__ import annotations

import collections
import datetime


class TrafficLog:
    """Records served requests.

    - requests: bounded raw deque (for debugging)
    - hosts: per-domain rollup {host -> provider, count, last, era},
      most-recently-active last; bounded to max_hosts domains
    """

    def __init__(self, max_requests: int = 300, max_hosts: int = 150):
        self.requests = collections.deque(maxlen=max_requests)
        self.hosts: collections.OrderedDict[str, dict] = collections.OrderedDict()
        self.max_hosts = max_hosts

    def record(self, client: str, provider: str, method: str,
               host: str, path: str, era: str) -> None:
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self.requests.append({
            "time": now, "client": client, "provider": provider,
            "method": method, "host": host, "path": path, "era": era,
        })
        if host and provider != "admin":
            h = host.lower()
            entry = self.hosts.get(h)
            if entry is None:
                entry = {"provider": provider, "count": 0,
                         "last": now, "era": era}
                self.hosts[h] = entry
            self.hosts.move_to_end(h)          # keep most-recent at the end
            entry["provider"] = provider       # latest provider wins
            entry["count"] += 1
            entry["last"] = now
            entry["era"] = era
            while len(self.hosts) > self.max_hosts:
                self.hosts.popitem(last=False)
