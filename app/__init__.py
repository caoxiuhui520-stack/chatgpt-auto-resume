"""ChatGPT Auto Resume.

Monitors the local Codex / ChatGPT usage quota and, once the 5-hour window
has genuinely reset, safely sends a pre-configured "continue" prompt into an
already-running ChatGPT Desktop conversation.

The program never bypasses or extends any quota limit. It only resumes work
*after* the provider reports fresh quota.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
