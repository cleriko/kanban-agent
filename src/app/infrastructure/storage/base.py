from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator


class ObjectStorage(ABC):
    """Audio and transcripts live here, never in Postgres.

    Keys follow ``meetings/{meeting_id}/original.m4a`` so everything about one
    meeting is under a single prefix and can be deleted in one call.
    """

    @abstractmethod
    async def put(self, key: str, data: AsyncIterator[bytes], content_type: str) -> int:
        """Streams an object in. Returns the number of bytes written."""

    @abstractmethod
    async def get_path(self, key: str) -> str:
        """A local filesystem path the workers can read.

        For remote backends this downloads to a temporary file; callers treat the
        result as read-only and short-lived.
        """

    @abstractmethod
    async def delete(self, key: str) -> None: ...

    @abstractmethod
    async def delete_prefix(self, prefix: str) -> None: ...

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    @staticmethod
    def audio_key(meeting_id: str, extension: str = "m4a") -> str:
        return f"meetings/{meeting_id}/original.{extension}"

    @staticmethod
    def meeting_prefix(meeting_id: str) -> str:
        return f"meetings/{meeting_id}/"
