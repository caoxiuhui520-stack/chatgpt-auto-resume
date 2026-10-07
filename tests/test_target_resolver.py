"""Target identity resolution - dual-factor (id AND title).

Covers the identity-verification rules from the hardening brief: a bare id
match is never enough; an id match with a title conflict is IDENTITY_CONFLICT
and a real send is refused.
"""

from __future__ import annotations

from app.config import TargetConfig
from app.discovery.models import (
    ConversationInfo,
    SOURCE_CODEX_WORK,
    SOURCE_WEB_CACHE,
)
from app.target import TargetResolver, target_trusted


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

    def get_conversation_by_id(self, cid):
        return self.get_by_id(cid)

    def find_by_title(self, title):
        needle = (title or "").strip()
        return [c for c in self._items if (c.title or "").strip() == needle]

    def get_current_conversation(self):
        return None


def _target(conversation_id="", title=""):
    return TargetConfig(conversation_id=conversation_id, conversation_title=title)


def _work(cid, title):
    return ConversationInfo(id=cid, title=title, source_kind=SOURCE_CODEX_WORK)


# --- identity states -------------------------------------------------------


def test_not_configured():
    m = TargetResolver().resolve(_target("", ""), "x", "y", None)
    assert m.status == "not_configured"
    assert m.ok is False


def test_dual_factor_matched():  # TEST-04
    disc = _FakeDiscovery([_work("abc", "Project X")])
    m = TargetResolver().resolve(_target("abc", "Project X"), "abc", "Project X", disc)
    assert m.status == "matched"
    assert m.authorized is True
    assert m.ok is True
    assert m.matched_by == "conversation_id"


def test_identity_conflict_when_title_disagrees():  # TEST-03
    disc = _FakeDiscovery([_work("abc", "Project X")])
    m = TargetResolver().resolve(_target("abc", "Project X"), "abc", "Something Else", disc)
    assert m.status == "identity_conflict"
    assert m.ok is False


def test_mismatch_when_id_differs():  # TEST-05
    disc = _FakeDiscovery([_work("abc", "Project X"), _work("def", "Other")])
    m = TargetResolver().resolve(_target("abc", "Project X"), "def", "Project X", disc)
    assert m.status == "mismatch"
    assert m.ok is False


def test_unknown_when_id_matches_but_title_unreadable():
    disc = _FakeDiscovery([_work("abc", "Project X")])
    m = TargetResolver().resolve(_target("abc", "Project X"), "abc", "", disc)
    assert m.status == "unknown"
    assert m.ok is False


def test_unknown_when_no_current_id():  # TEST-06: title fallback is not authorised
    disc = _FakeDiscovery([_work("abc", "Project X")])
    m = TargetResolver().resolve(_target("abc", "Project X"), "", "Project X", disc)
    assert m.status == "unknown"
    assert m.ok is False


def test_unknown_when_target_title_missing():
    disc = _FakeDiscovery([_work("abc", "Project X")])
    m = TargetResolver().resolve(_target("abc", ""), "abc", "Project X", disc)
    assert m.status == "unknown"
    assert m.ok is False


def test_web_cache_target_refused():
    web = ConversationInfo(id="w1", title="Web chat", source_kind=SOURCE_WEB_CACHE)
    disc = _FakeDiscovery([web])
    m = TargetResolver().resolve(_target("w1", "Web chat"), "w1", "Web chat", disc)
    assert m.status == "web_cache_target"
    assert m.ok is False


def test_ephemeral_target_refused():
    m = TargetResolver().resolve(
        _target("client-new-thread:abc", "New"), "client-new-thread:abc", "New", None
    )
    assert m.status == "ephemeral_target"
    assert m.ok is False


def test_title_only_target_is_matched_but_not_authorized():
    disc = _FakeDiscovery([_work("abc", "Project X")])
    m = TargetResolver().resolve(_target(title="Project X"), "", "Project X", disc)
    assert m.status == "matched"
    assert m.authorized is False
    assert m.ok is False  # not authorised for a real send


# --- target trust ----------------------------------------------------------


def test_target_trusted_work_session():
    disc = _FakeDiscovery([_work("abc", "X")])
    ok, why = target_trusted(_target("abc", "X"), disc)
    assert ok is True, why


def test_target_trusted_rejects_web_cache():
    disc = _FakeDiscovery([ConversationInfo(id="w", title="W", source_kind=SOURCE_WEB_CACHE)])
    ok, why = target_trusted(_target("w", "W"), disc)
    assert ok is False
    assert "web-cache" in why


def test_target_trusted_rejects_ephemeral():
    ok, why = target_trusted(_target("client-new-thread:x", "X"), None)
    assert ok is False


def test_target_trusted_rejects_unknown_id():
    disc = _FakeDiscovery([_work("abc", "X")])
    ok, why = target_trusted(_target("zzz", "X"), disc)
    assert ok is False
