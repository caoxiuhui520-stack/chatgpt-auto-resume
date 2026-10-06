"""Safe prompt variable substitution.

Only a fixed whitelist of variables is expanded, with a plain string replace.
There is no expression evaluation and no ``eval`` - untrusted content is never
executed.
"""

from __future__ import annotations

import re
from typing import Mapping

_VAR = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

#: The only variables a preset may use.
ALLOWED_VARIABLES = {
    "conversation_title",
    "project_name",
    "current_time",
    "quota_reset_time",
    "last_resume_time",
}

_MISSING = "(unavailable)"


def render_prompt(template: str, context: Mapping[str, str]) -> str:
    """Substitute ``{{var}}`` from ``context``. Unknown/blank vars render as a
    neutral placeholder so the prompt never exposes a raw template token."""
    def repl(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in ALLOWED_VARIABLES:
            return match.group(0)  # leave unknown tokens untouched
        value = context.get(name)
        if not value:
            return _MISSING
        return str(value)

    return _VAR.sub(repl, template)


def available_variables() -> list[str]:
    return sorted(ALLOWED_VARIABLES)
