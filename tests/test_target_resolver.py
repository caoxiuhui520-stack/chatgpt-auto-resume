"""Target identity resolution: id-primary, title-fallback, never guess."""

from __future__ import annotations

from app.config import TargetConfig
from app.discovery.models import ConversationInfo, ProjectInfo
from app.target import TargetResolver


class _FakeDiscovery:
    """Minimal discovery source for the resolver tests."""

    def __init__(self, conversations=None):
        self._items = list(conversations or [])

    def list_conversations(self, project_id=None):
        return list(self._items)

    def get_by_id(self, cid):
        for c in self._items:
            if c.id == cid:
                return c
        return None

    def find_by_title(self, title):
        needle = (title or "").strip()
        return [c for c in self._items if (c.title or "").strip() == needle]

    def get_current_conversation(self):
        return None


def _target(conversation_id="", title=""):
    return TargetConfig(conversation_id=conversation_id, conversation_title=title)


def _conv(cid, title):
    return ConversationInfo(id=cid, title=title)


def test_not_configured():
    m = TargetResolver().resolve(_target("", ""), "x", "y", None)
    assert m.status == "not_configured"


def test_id_match():
    m = TargetResolver().resolve(_target(conversation_id="abc"), "abc", "Some title", None)
    assert m.status == "matched"
    assert m.matched_by == "conversation_id"


def test_id_mismatch_when_current_id_known():
    m = TargetResolver().resolve(_target(conversation_id="abc"), "def", "Some title", None)
    assert m.status == "mismatch"


def test_id_target_with_unknown_current_and_matching_title_unique():
    disc = _FakeDiscovery([_conv("abc", "Project X")])
    m = TargetResolver().resolve(
        _target(conversation_id="abc", title="Project X"), "", "Project X", disc
    )
    assert m.status == "matched"
    assert m.matched_by == "conversation_title"


def test_id_target_with_ambiguous_title_is_ambiguous():
    disc = _FakeDiscovery([_conv("abc", "Project X"), _conv("def", "Project X")])
    m = TargetResolver().resolve(
        _target(conversation_id="abc", title="Project X"), "", "Project X", disc
    )
    assert m.status == "ambiguous"


def test_title_only_match_by_title_unique():
    disc = _FakeDiscovery([_conv("abc", "Project X")])
    m = TargetResolver().resolve(_target(title="Project X"), "", "Project X", disc)
    assert m.status == "matched"


def test_title_only_ambiguous():
    disc = _FakeDiscovery([_conv("abc", "X"), _conv("def", "X")])
    m = TargetResolver().resolve(_target(title="X"), "", "X", disc)
    assert m.status == "ambiguous"


def test_title_mismatch():
    m = TargetResolver().resolve(_target(title="Target"), "", "Other", None)
    assert m.status == "mismatch"


def test_title_unknown_without_discovery():
    m = TargetResolver().resolve(_target(title="Target"), "", "Target", None)
    assert m.status == "unknown"
