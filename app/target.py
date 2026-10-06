"""Target identity resolution.

The task lock no longer trusts a bare title: the conversation id is the
primary identity, and the title is only a fallback for display and diagnosis.
The central rule from the safety brief is preserved here in code:

    *never guess*. If a title matches more than one conversation, the answer
    is AMBIGUOUS and a real send is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.discovery.models import ConversationInfo, ConversationMatch

if TYPE_CHECKING:  # pragma: no cover
    from app.config import TargetConfig
    from app.discovery.provider import ConversationDiscoveryProvider


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

        if not target.conversation_id and not target.conversation_title.strip():
            return ConversationMatch(
                status="not_configured",
                reason="no target conversation configured",
            )

        target_id = target.conversation_id.strip()
        target_title = target.conversation_title.strip()

        # -- identity-based path (primary) ---------------------------------
        if target_id:
            if current_id and current_id == target_id:
                return ConversationMatch(
                    status="matched",
                    target=self._target_info(target),
                    current=self._current_info(discovery, current_id, current_title),
                    reason="conversation id matches",
                    matched_by="conversation_id",
                )
            if current_id and current_id != target_id:
                # Both ids known and different: definite mismatch.
                return ConversationMatch(
                    status="mismatch",
                    target=self._target_info(target),
                    current=self._current_info(discovery, current_id, current_title),
                    reason=f"open conversation id {current_id} is not target {target_id}",
                    matched_by="",
                )
            # No current id available: fall back to the title, but only when
            # it is unambiguous and consistent with the target id.
            if not current_title:
                return ConversationMatch(
                    status="unknown",
                    target=self._target_info(target),
                    reason="cannot determine the open conversation",
                    matched_by="",
                )
            return self._resolve_by_title(target, current_title, discovery)

        # -- title-only path -------------------------------------------------
        if current_id:
            current = self._current_info(discovery, current_id, current_title)
            if current and (current.title or "").strip() == target_title:
                return ConversationMatch(
                    status="matched",
                    target=self._target_info(target),
                    current=current,
                    reason="open conversation title matches target",
                    matched_by="conversation_title",
                )
            return ConversationMatch(
                status="mismatch",
                target=self._target_info(target),
                current=current,
                reason=f"open conversation {current_title or current_id!r} does not match target {target_title!r}",
                matched_by="",
            )
        if current_title:
            return self._resolve_by_title(target, current_title, discovery)
        return ConversationMatch(
            status="unknown",
            target=self._target_info(target),
            reason="cannot determine the open conversation",
            matched_by="",
        )

    def _resolve_by_title(
        self,
        target: "TargetConfig",
        current_title: str,
        discovery: "ConversationDiscoveryProvider | None",
    ) -> ConversationMatch:
        target_title = target.conversation_title.strip()
        target_id = target.conversation_id.strip()

        if current_title != target_title:
            return ConversationMatch(
                status="mismatch",
                target=self._target_info(target),
                current=ConversationInfo(id="", title=current_title),
                reason=f"open conversation {current_title!r} is not target {target_title!r}",
                matched_by="",
            )

        if discovery is None:
            return ConversationMatch(
                status="unknown",
                target=self._target_info(target),
                current=ConversationInfo(id="", title=current_title),
                reason="no discovery source to verify title uniqueness",
                matched_by="",
            )

        matches = discovery.find_by_title(current_title)
        if len(matches) == 0:
            return ConversationMatch(
                status="unknown",
                target=self._target_info(target),
                current=ConversationInfo(id="", title=current_title),
                reason="title not found in the local conversation list",
                matched_by="",
            )
        if len(matches) > 1:
            return ConversationMatch(
                status="ambiguous",
                target=self._target_info(target),
                current=ConversationInfo(id="", title=current_title),
                reason=f"title {current_title!r} matches {len(matches)} conversations",
                matched_by="",
            )
        resolved = matches[0]
        if target_id and resolved.id != target_id:
            return ConversationMatch(
                status="mismatch",
                target=self._target_info(target),
                current=resolved,
                reason=f"title maps to {resolved.id}, not target {target_id}",
                matched_by="",
            )
        return ConversationMatch(
            status="matched",
            target=self._target_info(target),
            current=resolved,
            reason="title uniquely maps to the target conversation",
            matched_by="conversation_title",
        )

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
