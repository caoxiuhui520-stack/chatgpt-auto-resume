"""Usage providers.

The core daemon only ever talks to :class:`UsageProvider`. Nothing outside this
package knows whether the numbers came from a local JSON-RPC app-server, an
HTTP endpoint, or a test fixture - that is the whole point of the abstraction.
"""

from app.usage.base import ProviderError, UsageProvider, build_provider, register

__all__ = ["UsageProvider", "ProviderError", "build_provider", "register"]
