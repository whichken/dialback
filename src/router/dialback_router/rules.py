"""Rule engine: matches hosts to providers, resolves the active era."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from . import providers
from .era import EraProfile, valid_date


@dataclass
class Rule:
    provider: str
    host: str | None = None       # wildcard pattern, e.g. "*.geocities.com"

@dataclass
class RuleEngine:
    era: str
    default_provider: str
    profile: EraProfile | None = None
    era_dir: str | None = None
    rules: list[Rule] = field(default_factory=list)
    _instances: dict = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: dict) -> "RuleEngine":
        rules = [
            Rule(provider=r["provider"], host=(r.get("match") or {}).get("host"))
            for r in config.get("rules", [])
        ]
        engine = cls(
            era=str(config.get("era", "1997")),
            default_provider=config.get("default_provider", "passthrough"),
            rules=rules,
        )
        engine._provider_config = config.get("providers", {})
        providers.set_provider_config(engine._provider_config)
        engine.era_dir = config.get("era_dir")
        providers.set_provider_config(engine._provider_config)
        era_dir = config.get("era_dir")
        if era_dir:
            try:
                engine.profile = EraProfile.load(era_dir, engine.era)
            except FileNotFoundError:
                pass
        return engine

    def switch_era(self, name: str) -> None:
        """Runtime era change; providers are stateless w.r.t. era so this is safe."""
        if not self.era_dir:
            raise ValueError("no era_dir configured")
        self.profile = EraProfile.load(self.era_dir, name)
        self.era = name

    def set_date(self, date_iso: str) -> None:
        """Dial to an arbitrary date (YYYY-MM-DD)."""
        if not valid_date(date_iso):
            raise ValueError(f"date out of range: {date_iso}")
        self.profile = EraProfile.for_date(date_iso)
        self.era = self.profile.name

    def select(self, host: str | None) -> tuple[str, "providers.Provider"]:
        """Returns (provider_name, provider_instance) for a request host."""
        name = self.default_provider
        if host:
            for rule in self.rules:
                if rule.host and fnmatch.fnmatch(host.lower(), rule.host.lower()):
                    name = rule.provider
                    break

        inst = self._instances.get(name)
        if inst is None:
            options = self._provider_options(name)
            inst = providers.create(name, options)
            self._instances[name] = inst
        return name, inst

    def _provider_options(self, name: str) -> dict:
        return getattr(self, "_provider_config", {}).get(name, {})
