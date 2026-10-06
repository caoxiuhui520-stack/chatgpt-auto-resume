"""Controller interface + factory.

The resume logic depends on this interface, never on pywinauto directly. That
is what lets the whole pipeline be tested against a fake controller without a
real ChatGPT window, and what keeps the fragile UI-automation code isolated in
one place.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

from app.models import ResumeResult
from app.utils.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from app.config import AppConfig

log = get_logger("chatgpt")


@dataclass(slots=True)
class WindowInfo:
    handle: int
    title: str
    pid: int
    process_name: str = ""
    minimized: bool = False


class ChatGptController(ABC):
    """Everything the resume flow needs from the ChatGPT desktop app."""

    name: str = "base"

    @abstractmethod
    def is_running(self) -> bool:
        """Is a ChatGPT process currently alive?"""

    @abstractmethod
    def find_window(self) -> WindowInfo | None:
        """Return the main window, restoring it if it is minimised."""

    @abstractmethod
    def is_busy(self) -> bool | None:
        """True when a response is still being generated.

        ``None`` means "cannot tell" - the caller must treat that as busy,
        because a false positive (sending while it is working) is much worse
        than a false negative.
        """

    @abstractmethod
    def conversation_title(self) -> str:
        """Best-effort title of the currently open conversation ('' if unknown)."""

    @abstractmethod
    def send_prompt(self, text: str, *, dry_run: bool = True) -> ResumeResult:
        """Type and submit the prompt. Must honour ``dry_run`` absolutely."""

    @abstractmethod
    def verify_sent(self, timeout: float = 3.0) -> tuple[bool, str]:
        """POST_SEND_VERIFY: positive evidence that the last send was delivered.

        Returns (confirmed, reason). ``confirmed=False`` is SEND_UNCERTAIN and
        must never be answered with an automatic retry.
        """

    def start(self) -> bool:
        """Try to launch the app. Returns True if a window showed up."""
        return False

    def close(self) -> None:  # pragma: no cover
        """Release UI resources."""

    def describe(self) -> str:
        return self.name


_REGISTRY: dict[str, Callable[["AppConfig"], ChatGptController]] = {}


def register(name: str) -> Callable[[Callable[["AppConfig"], ChatGptController]], Callable]:
    def deco(factory: Callable[["AppConfig"], ChatGptController]) -> Callable:
        _REGISTRY[name] = factory
        return factory

    return deco


def build_controller(name: str, cfg: "AppConfig") -> ChatGptController:
    from app.chatgpt import fake, ui_controller  # noqa: F401

    factory = _REGISTRY.get(name)
    if factory is None:
        raise KeyError(f"unknown chatgpt controller {name!r}; available: {sorted(_REGISTRY)}")
    return factory(cfg)
