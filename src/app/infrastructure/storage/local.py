from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

from .base import ObjectStorage


class LocalObjectStorage(ObjectStorage):
    """Filesystem-backed storage. The sensible default for a single VPS: no extra
    service to run, and the workers read the file directly with no download step."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Refuse anything that could escape the root.
        candidate = (self._root / key).resolve()
        root = self._root.resolve()
        if not str(candidate).startswith(str(root)):
            raise ValueError(f"invalid object key: {key!r}")
        return candidate

    async def put(self, key: str, data: AsyncIterator[bytes], content_type: str) -> int:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")

        written = 0
        # Write through a thread so a large upload never blocks the event loop.
        handle = await asyncio.to_thread(open, temporary, "wb")
        try:
            async for chunk in data:
                if not chunk:
                    continue
                await asyncio.to_thread(handle.write, chunk)
                written += len(chunk)
        finally:
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
