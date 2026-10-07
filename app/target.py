"""Target identity resolution - dual-factor (id AND title).

The task lock is now dual-factor: a conversation id match alone is NOT enough
to authorise a real send, because the desktop tab-state file
(``browser-sidebar-page-states.json``) can lag one tab behind. A real send
requires the cached id AND the live UIA title to agree with the target.

States (all but an authorised ``matched`` refuse a real send):

* ``not_configured``    - no target id/title configured
* ``matched``           - id AND title both agree (``authorized=True``)
* ``identity_conflict`` - id matches but title disagrees (stale cache) → NO SEND
* ``mismatch``          - ids known and different → NO SEND
* ``ambiguous``         - title matches several conversations → NO SEND
* ``unknown``           - cannot verify (no id / unreadable title) → NO SEND
* ``web_cache_target``  - target comes from the embedded web cache → NO SEND
* ``ephemeral_target``  - target is an unsaved ``client-new-thread:`` → NO SEND
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.discovery.models import (
    ConversationInfo,
    ConversationMatch,
    SOURCE_CODEX_WORK,
    SOURCE_DESKTOP_ACTIVE,
    SOURCE_WEB_CACHE,
)

if TYPE_CHECKING:  # pragma: no cover
    from app.config import TargetConfig
    from app.discovery.provider import ConversationDiscoveryProvider


def fingerprint_of(conversation_id: str, conversation_title: str) -> str:
    """Stable identity of a (id, title) pair: SHA256."""
    raw = f"{(conversation_id or '').strip()}\n{(conversation_title or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def target_fingerprint(target: "TargetConfig") -> str:
    """Stable identity of a configured target: SHA256 of id + title.

    Used to bind a Test Send certification to the exact target it verified.
    """
    return fingerprint_of(target.conversation_id, target.conversation_title)


def target_trusted(target: "TargetConfig", discovery: "ConversationDiscoveryProvider | None") -> tuple[bool, str]:
    """Whether a target id is eligible to become a production target.

    It must resolve to a Codex Work session (or a verified desktop-active
    entry), and must not be ephemeral or a web-cache entry.
    """
    tid = (getattr(target, "conversation_id", "") or "").strip()
    if not tid:
        return False, "no conversation id"
    if tid.startswith("client-new-thread:"):
        return False, "ephemeral (unsaved) conversation"
    if discovery is None:
        return True, "discovery unavailable; trusting id (will be re-checked at send)"
    known = discovery.get_by_id(tid)
    if known is None:
        # Not in the local list yet; only desktop_active is allowed to carry
        # an id the cache does not know (it is the execution truth).
        return False, "conversation id not found in the local Work session index"
    if known.source_kind == SOURCE_WEB_CACHE:
        return False, "target is a web-cache conversation, not a desktop Work thread"
    if known.is_ephemeral:
        return False, "ephemeral (unsaved) conversation"
    if known.source_kind not in (SOURCE_CODEX_WORK, SOURCE_DESKTOP_ACTIVE):
        return False, f"untrusted source {known.source_kind}"
    return True, "trusted"


@dataclass(slots=True)
class TargetResolver:
    """Resolves a configured target against the live conversation identity."""

    def resolve(
        self,
        target: "TargetConfig",
        current_id: str,
        current_title: str,
        discovery: "ConversationDiscoveryProvider | None",
    ) -> ConversationMatch:
        current_id = (current_id or "").strip()
        current_title = (current_title or "").strip()
        target_id = (target.conversation_id or "").strip()
        target_title = (target.conversation_title or "").strip()

        # -- target integrity ---------------------------------------------
        if not target_id and not target_title:
            return ConversationMatch(status="not_configured", reason="未配置目标对话")

        tinfo = self._target_info(target)

        if target_id.startswith("client-new-thread:"):
            return ConversationMatch(
                status="ephemeral_target", target=tinfo,
                reason="目标是未保存的新对话（client-new-thread），不能用于自动续跑",
            )
        if discovery is not None:
            known = discovery.get_by_id(target_id)
            if known is not None and known.source_kind == SOURCE_WEB_CACHE:
                return ConversationMatch(
                    status="web_cache_target", target=known,
                    reason="目标来自网页缓存（chatgpt.com），不是桌面 Work 线程，不能用于自动续跑",
                )

        # -- dual-factor: id AND title ------------------------------------
        if target_id:
            if current_id and current_id == target_id:
                # id matches; now require the title cross-check.
                if not current_title:
                    return ConversationMatch(
                        status="unknown", target=tinfo,
                        current=ConversationInfo(id=current_id),
                        reason="当前对话 ID 匹配，但无法读取 UIA 标题，禁止发送",
                    )
                if not target_title:
                    return ConversationMatch(
                        status="unknown", target=tinfo,
                        current=ConversationInfo(id=current_id, title=current_title),
                        reason="目标标题缺失，无法做标题双因子验证，请重新选择目标",
                    )
                if current_title != target_title:
                    return ConversationMatch(
                        status="identity_conflict", target=tinfo,
                        current=ConversationInfo(id=current_id, title=current_title),
                        reason=(
                            f"身份冲突：当前 ID 匹配但标题不一致（当前「{current_title}」"
                            f"≠ 目标「{target_title}」），可能缓存滞后，禁止发送"
                        ),
                    )
                return ConversationMatch(
                    status="matched", target=tinfo,
                    current=ConversationInfo(id=current_id, title=current_title),
                    reason="对话 ID 与标题均匹配",
                    matched_by="conversation_id",
                    authorized=True,
                )

            if current_id and current_id != target_id:
                return ConversationMatch(
                    status="mismatch", target=tinfo,
                    current=self._current_info(discovery, current_id, current_title),
                    reason=f"当前对话 ID 不是目标（{current_id} ≠ {target_id}）",
                )

            # No current id: title fallback is NOT authorised for real send.
            return ConversationMatch(
                status="unknown", target=tinfo,
                current=ConversationInfo(id="", title=current_title),
                reason="无法确定当前对话 ID；仅凭标题不能授权真实发送",
            )

        # -- legacy title-only target (diagnostic / migration / dry-run) ---
        # Not authorised for production Real Send, per the brief.
        if current_id:
            current = self._current_info(discovery, current_id, current_title)
            if current and (current.title or "").strip() == target_title:
                return ConversationMatch(
                    status="matched", target=tinfo, current=current,
                    reason="标题匹配（旧配置），但缺少对话 ID，不能授权真实发送",
                    matched_by="conversation_title",
                    authorized=False,
                )
            return ConversationMatch(status="mismatch", target=tinfo, current=current,
                                     reason=f"当前对话与目标标题「{target_title}」不匹配")
        if current_title and current_title == target_title:
            return ConversationMatch(
                status="matched", target=tinfo,
                current=ConversationInfo(id="", title=current_title),
                reason="标题匹配，但缺少对话 ID，不能授权真实发送",
                matched_by="conversation_title",
                authorized=False,
            )
        return ConversationMatch(status="unknown", target=tinfo,
                                 reason="无法确定当前对话，不能验证目标")

    @staticmethod
    def _target_info(target: "TargetConfig") -> ConversationInfo:
        return ConversationInfo(
            id=target.conversation_id,
            title=target.conversation_title,
            project_id=target.project_id,
        )

    @staticmethod
    def _current_info(
        discovery: "ConversationDiscoveryProvider | None",
        current_id: str,
        current_title: str,
    ) -> ConversationInfo:
        if discovery is not None and current_id:
            known = discovery.get_by_id(current_id)
            if known is not None:
                return known
        return ConversationInfo(id=current_id, title=current_title)
