"""Provides fastmcp.Context, falling back to a lightweight async stub when fastmcp is not installed."""

import logging
from typing import TYPE_CHECKING

_logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from fastmcp import Context as Context  # noqa: F401
else:
    try:
        from fastmcp import Context  # type: ignore[assignment]
    except ImportError:

        class Context:  # type: ignore[no-redef]
            async def info(self, message: str) -> None:
                pass

            async def warning(self, message: str) -> None:
                pass

            async def error(self, message: str) -> None:
                pass

            async def report_progress(self, progress: float, total: float | None = None, message: str | None = None) -> None:
                pass

            async def elicit(self, message: str, response_type=None, **kwargs):
                raise NotImplementedError

            async def send_notification(self, notification) -> None:
                pass
