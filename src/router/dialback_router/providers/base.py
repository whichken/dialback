"""Provider base class and registry."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..request import Request

_REGISTRY: dict[str, type["Provider"]] = {}


def register(cls: type["Provider"]) -> type["Provider"]:
    """Class decorator: adds a Provider subclass to the registry by its `name`."""
    _REGISTRY[cls.name] = cls
    return cls


def create(name: str, options: dict) -> "Provider":
    if name not in _REGISTRY:
        raise KeyError(f"unknown provider '{name}' (known: {sorted(_REGISTRY)})")
    return _REGISTRY[name](options)


def known() -> list[str]:
    return sorted(_REGISTRY)


class Provider(ABC):
    """A provider fulfills intercepted requests.

    Subclasses must set `name` and implement handle(). Options come from the
    `providers:` section of dialback.yaml.
    """

    name: str = ""

    def __init__(self, options: dict | None = None):
        self.options = options or {}

    @abstractmethod
    async def handle(self, req: Request) -> None:
        """Fulfill the request by writing a response to req.writer."""
