"""Disk cache with LRU eviction for archive lookups and content.

Eviction policy: fill first, evict least-recently-used once the cache
exceeds its budget. The budget is the smaller of an absolute cap
(max_bytes) and a percentage of the backing filesystem (max_pct),
accounting for space used by everything else on that filesystem.

Access recency: reads bump the entry's mtime (atime is unreliable on
noatime-mounted SD cards).
"""
from __future__ import annotations

import hashlib
import os
import tempfile

# how many writes between eviction checks (scanning is O(files))
CHECK_EVERY_WRITES = 10


class DiskCache:
    def __init__(self, root: str, max_pct: float = 85.0, max_bytes: int | None = None):
        self.root = os.path.join(root, "v1")
        self.max_pct = max_pct
        self.max_bytes = max_bytes
        self._writes = 0
        # directories are created lazily on first write

    # ------------------------------------------------------------- basic --

    def _path(self, namespace: str, ident: str) -> str:
        digest = hashlib.sha1(ident.encode()).hexdigest()
        # fan out two levels so no directory gets huge
        return os.path.join(self.root, namespace, digest[:2], digest[2:4], digest)

    def get(self, namespace: str, ident: str) -> bytes | None:
        path = self._path(namespace, ident)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        try:
            os.utime(path, None)  # bump mtime -> LRU recency
        except OSError:
            pass
        return data

    def put(self, namespace: str, ident: str, data: bytes) -> None:
        path = self._path(namespace, ident)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            return  # cache is best-effort; never fail a request over it
        self._writes += 1
        if self._writes >= CHECK_EVERY_WRITES:
            self.enforce()

    # ---------------------------------------------------------- eviction --

    def stats(self) -> dict:
        """Usage snapshot: {bytes, files, budget}. Used by the admin UI."""
        entries = self._scan()
        return {
            "bytes": sum(s for _, s, _ in entries),
            "files": len(entries),
            "budget": self._compute_budget(sum(s for _, s, _ in entries)),
        }

    def purge(self) -> int:
        """Delete everything. Returns bytes freed."""
        entries = self._scan()
        total = sum(s for _, s, _ in entries)
        for _mtime, _size, path in entries:
            try:
                os.unlink(path)
            except OSError:
                pass
        self._prune_empty_dirs()
        return total

    def enforce(self) -> int:
        """Evict LRU entries until within budget. Returns bytes freed."""
        self._writes = 0
        entries = self._scan()  # (mtime, size, path)
        cache_total = sum(s for _, s, _ in entries)
        budget = self._compute_budget(cache_total)
        if budget is None or cache_total <= budget:
            return 0

        freed = 0
        entries.sort()  # oldest mtime first
        for _mtime, size, path in entries:
            if cache_total - freed <= budget:
                break
            try:
                os.unlink(path)
                freed += size
            except OSError:
                pass
        if freed:
            self._prune_empty_dirs()
        return freed

    def _scan(self) -> list[tuple[float, int, str]]:
        out = []
        for dirpath, _dirs, files in os.walk(self.root):
            for fn in files:
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                    out.append((st.st_mtime, st.st_size, p))
                except OSError:
                    continue
        return out

    def _compute_budget(self, cache_total: int) -> int | None:
        """Bytes the cache may occupy: min(absolute cap, filesystem share)."""
        candidates: list[int] = []
        if self.max_bytes:
            candidates.append(self.max_bytes)
        if self.max_pct is not None:
            try:
                st = os.statvfs(self.root)
                fs_total = st.f_blocks * st.f_frsize
                fs_used_other = (st.f_blocks - st.f_bavail) * st.f_frsize - cache_total
                share = int(fs_total * self.max_pct / 100) - max(fs_used_other, 0)
                candidates.append(max(share, 0))
            except OSError:
                pass
        return min(candidates) if candidates else None

    def _prune_empty_dirs(self) -> None:
        for dirpath, _dirs, files in os.walk(self.root, topdown=False):
            try:
                if not files:
                    os.rmdir(dirpath)
            except OSError:
                pass
