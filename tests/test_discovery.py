"""Conversation discovery: value decoding, list parsing, real-data integration.

The real integration test reads the actual ChatGPT Desktop user-data directory
and is skipped when it is not present, so the suite still runs on machines
without the app installed.
"""

from __future__ import annotations

import json

import pytest

from app.discovery import paths
from app.discovery.provider import (
    _conversation_rows,
    _decode_local_storage_value,
    LocalChatGPTDiscoveryProvider,
)


def test_decode_utf16_value():
    payload = '{"pages":[]}'.encode("utf-16-le")
    assert _decode_local_storage_value(b"\x00" + payload) == '{"pages":[]}'
    assert _decode_local_storage_value(b"\x01" + payload) == '{"pages":[]}'


def test_decode_utf8_fallback():
    assert _decode_local_storage_value(b"\x00" + b"hello") == "hello"


def test_conversation_rows():
    payload = json.dumps(
        {"pages": [{"items": [{"id": "a", "title": "One"}, {"id": "b", "title": "Two"}]}]}
    )
    rows = _conversation_rows(payload)
    assert [r["id"] for r in rows] == ["a", "b"]


@pytest.mark.skipif(
    paths.resolve_user_data_dir() is None,
    reason="ChatGPT Desktop user-data not present on this machine",
)
def test_real_discovery_lists_conversations():
    provider = LocalChatGPTDiscoveryProvider()
    assert provider.available
    convos = provider.list_conversations()
    assert convos, "expected at least one conversation"
    for c in convos:
        assert c.id
        assert c.title is not None
    # The current conversation should resolve to a non-empty id when the app
    # has a tab open, or None when it does not.
    current = provider.get_current_conversation()
    if current is not None:
        assert current.id
