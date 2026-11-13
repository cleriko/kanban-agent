from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath

from .base import ObjectStorage


class LocalObjectStorage(ObjectStorage):
    """Filesystem-backed storage. The sensible default for a single VPS: no extra
    service to run, and the workers read the file directly with no download step."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are built from ids, and ids can arrive from a client. Reject traversal
        # before resolving, then confirm the result is a real descendant — a plain
        # string prefix check accepts a sibling like "<root>-evil".
        parts = PurePosixPath(key).parts
        if not key or PurePosixPath(key).is_absolute() or ".." in parts:
            raise ValueError(f"invalid object key: {key!r}")
        root = self._root.resolve()
        candidate = (root / key).resolve()
        if candidate == root or not candidate.is_relative_to(root):
            raise ValueError(f"invalid object key: {key!r}")
        return candidate

    async def put(self, key: str, data: AsyncIterator[bytes], content_type: str) -> int:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique per upload: two concurrent PUTs for one meeting must not share a
        # temp file and interleave their bytes.
        temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.part")

        written = 0
        # Write through a thread so a large upload never blocks the event loop.
        handle = await asyncio.to_thread(open, temporary, "wb")
        try:
            async for chunk in data:
                if not chunk:
                    continue
                await asyncio.to_thread(handle.write, chunk)
                written += len(chunk)
        except BaseException:
            # Aborted uploads (client disconnect, sniff rejection, size cap) must not
            # leave a partial file behind.
            await asyncio.to_thread(handle.close)
            await asyncio.to_thread(temporary.unlink, True)
            raise
        else:
            await asyncio.to_thread(handle.close)

        # Rename last, so a reader never sees a partial object.
        await asyncio.to_thread(temporary.replace, path)
        return written

    async def get_path(self, key: str) -> str:
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(key)
        return str(path)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(path.unlink, True)

    async def delete_prefix(self, prefix: str) -> None:
        path = self._path(prefix)
        if path.is_dir():
            await asyncio.to_thread(shutil.rmtree, path, True)

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()
