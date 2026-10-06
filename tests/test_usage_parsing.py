"""Parsing of provider payloads.

The app-server fixture below is a *verbatim, sanitised* copy of what codex-cli
0.147.0 returned on this machine, so the parser is pinned to real data rather
than to a guess.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import UsageSnapshot
from app.usage.codex_app_server import parse_rate_limits
from app.usage.minibar_adapter import parse_usage_payload

NOW = datetime(2026, 10, 7, 1, 0, tzinfo=timezone.utc)

# Real response shape (identifiers/emails removed).
APP_SERVER_RESULT = {
    "rateLimits": {
        "limitId": "codex",
        "limitName": None,
        "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 1791321160},
        "secondary": {"usedPercent": 33, "windowDurationMins": 10080, "resetsAt": 1791597676},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "individualLimit": None,
        "spendControlReached": False,
        "planType": "plus",
        "rateLimitReachedType": "rate_limit_reached",
    },
    "rateLimitsByLimitId": {"codex": {"limitId": "codex", "planType": "plus"}},
    "rateLimitResetCredits": {"availableCount": 2, "credits": []},
}


def test_exhausted_quota_from_real_payload():
    snap = parse_rate_limits(APP_SERVER_RESULT, now=NOW)
    assert snap.available is False
    assert snap.five_hour_remaining_percent == 0.0
    assert snap.weekly_remaining_percent == 67.0
    assert snap.reached_type == "rate_limit_reached"
    assert snap.plan_type == "plus"
    assert snap.reset_id.startswith("codex_app_server:five_hour:")


def test_restored_quota_is_available():
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 1791330000},
            "secondary": {"usedPercent": 33, "windowDurationMins": 10080, "resetsAt": 1791597676},
            "rateLimitReachedType": None,
            "planType": "plus",
        }
    }
    snap = parse_rate_limits(result, now=NOW)
    assert snap.available is True
    assert snap.five_hour_remaining_percent == 100.0


def test_unactivated_placeholder_window_is_flagged():
    """usedPercent 0 with resetsAt == now + 5h is not a real window yet."""
    placeholder_reset = int((NOW + timedelta(minutes=300)).timestamp())
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": placeholder_reset},
            "rateLimitReachedType": None,
        }
    }
    snap = parse_rate_limits(result, now=NOW)
    assert snap.raw and snap.raw.get("unactivated_window") is True


def test_weekly_only_primary_is_moved_to_secondary():
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1791597676},
            "secondary": None,
            "rateLimitReachedType": None,
        }
    }
    snap = parse_rate_limits(result, now=NOW)
    assert snap.five_hour_remaining_percent is None
    assert snap.weekly_remaining_percent == 60.0
    assert snap.available is False, "no 5h data must never look like available quota"


def test_missing_rate_limits_raises():
    with pytest.raises(Exception):
        parse_rate_limits({}, now=NOW)


def test_resets_at_accepts_milliseconds():
    """A milliseconds-based value must not produce a year-50000 timestamp."""
    ms = 1791321160000
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 50, "windowDurationMins": 300, "resetsAt": ms},
            "rateLimitReachedType": None,
        }
    }
    snap = parse_rate_limits(result, now=NOW)
    assert snap.five_hour_reset_at is not None
    assert snap.five_hour_reset_at.year == 2026


def test_reset_id_is_stable_across_drift():
    """One second of jitter must not change the window identity."""
    base = {"rateLimits": {"primary": {"usedPercent": 0, "windowDurationMins": 300,
                                       "resetsAt": 1791330000},
                           "rateLimitReachedType": None}}
    drifted = {"rateLimits": {"primary": {"usedPercent": 0, "windowDurationMins": 300,
                                          "resetsAt": 1791330002},
                              "rateLimitReachedType": None}}
    assert parse_rate_limits(base, now=NOW).reset_id == parse_rate_limits(drifted, now=NOW).reset_id


# -- HTTP fallback ---------------------------------------------------------

HTTP_PAYLOAD = {
    "plan_type": "plus",
    "rate_limit": {
        "primary_window": {"used_percent": 100, "reset_at": 1791321160,
                           "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 33, "reset_at": 1791597676,
                             "limit_window_seconds": 604800},
    },
}


def test_http_payload_parses():
    snap = parse_usage_payload(HTTP_PAYLOAD, now=NOW)
    assert snap.available is False
    assert snap.five_hour_remaining_percent == 0.0
    assert snap.source == "codex_http"


def test_usage_snapshot_round_trip():
    snap = parse_rate_limits(APP_SERVER_RESULT, now=NOW)
    restored = UsageSnapshot.from_dict(snap.to_dict())
    assert restored.reset_id == snap.reset_id
    assert restored.available == snap.available
    assert restored.five_hour_reset_at == snap.five_hour_reset_at
    assert restored.raw is None, "raw provider payloads must not be persisted"
