"""Minimal disk cache for archive lookups and content.

Stage-4 will add size accounting and fill-then-evict policy; this version
just stores immutable blobs (Wayback snapshots don't change) under
namespaced keys.
"""
from __future__ import annotations

import hashlib
import os
import tempfile


class DiskCache:
    def __init__(self, root: str):
        self.root = os.path.join(root, "v1")
        # directories are created lazily on first write

    def _path(self, namespace: str, ident: str) -> str:
        digest = hashlib.sha1(ident.encode()).hexdigest()
        # fan out two levels so no directory gets huge
        return os.path.join(self.root, namespace, digest[:2], digest[2:4], digest)

    def get(self, namespace: str, ident: str) -> bytes | None:
        try:
            with open(self._path(namespace, ident), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def put(self, namespace: str, ident: str, data: bytes) -> None:
        path = self._path(namespace, ident)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except OSError:
            pass  # cache is best-effort; never fail a request over it
