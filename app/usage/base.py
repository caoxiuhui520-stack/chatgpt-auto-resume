"""Provider interface and factory."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, TYPE_CHECKING

from app.models import UsageSnapshot
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig

log = get_logger("usage")


class ProviderError(RuntimeError):
    """Raised when a provider cannot produce a snapshot at all.

    Providers should prefer returning a snapshot with ``error`` set for
    transient problems; raising is reserved for "this provider is unusable".
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class UsageProvider(ABC):
    """Unified quota source.

    Implementations must be safe to call repeatedly from one thread and must
    never raise for ordinary transient failures - return a snapshot with
    ``error`` populated instead. The daemon classifies the error and decides.
    """

    #: Short machine name, also used to build the reset id.
    name: str = "base"
    #: Human description for logs / diagnostics.
    description: str = ""

    @abstractmethod
    def get_usage(self) -> UsageSnapshot:
        """Read the current quota state."""

    def close(self) -> None:  # pragma: no cover - default no-op
        """Release any resources (child processes etc.)."""

    def describe(self) -> str:
        return f"{self.name} ({self.description or 'no description'})"


_REGISTRY: dict[str, Callable[["AppConfig"], UsageProvider]] = {}


def register(name: str) -> Callable[[Callable[["AppConfig"], UsageProvider]], Callable]:
    def deco(factory: Callable[["AppConfig"], UsageProvider]) -> Callable:
        _REGISTRY[name] = factory
        return factory

    return deco


def registered() -> list[str]:
    return sorted(_REGISTRY)


def build_provider(name: str, cfg: "AppConfig") -> UsageProvider:
    """Instantiate a provider by name. Raises ProviderError when unknown."""
    # Import for side effects: each module registers itself.
    from app.usage import codex_app_server, fake, minibar_adapter  # noqa: F401

    factory = _REGISTRY.get(name)
    if factory is None:
        raise ProviderError(
            "unknown_provider",
            f"unknown usage provider {name!r}; available: {', '.join(registered())}",
        )
    provider = factory(cfg)
    log.info("usage provider ready: %s", provider.describe())
    return provider
