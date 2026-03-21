from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable
from uuid import UUID, uuid4

from src.models.events import OptimisticConcurrencyError, StreamMetadata


class UpcasterRegistry:
    def __init__(self):
        self._upcasters: dict[str, dict[int, Callable[[dict], dict]]] = {}

    def upcaster(self, event_type: str, from_version: int, to_version: int):
        if to_version != from_version + 1:
            raise ValueError("Only one-step upcasters are supported per registration")

        def decorator(fn: Callable[[dict], dict]):
            self._upcasters.setdefault(event_type, {})[from_version] = fn
            return fn

        return decorator

    def upcast(self, event: dict) -> dict:
        version = int(event.get("event_version", 1))
        event_type = event["event_type"]
        chain = self._upcasters.get(event_type, {})
        while version in chain:
            event["payload"] = chain[version](dict(event["payload"]))
            version += 1
            event["event_version"] = version
        return event


class EventStore:
    def __init__(self, db_url: str, upcaster_registry: UpcasterRegistry | None = None):
        self.db_url = db_url
        self.upcasters = upcaster_registry
        self._pool: Any = None

    async def connect(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=10)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def stream_version(self, stream_id: str) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT current_version FROM event_streams WHERE stream_id = $1",
                stream_id,
            )
            return int(row["current_version"]) if row else -1

    async def append(
        self,
        stream_id: str,
        events: list[dict],
        expected_version: int,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[int]:
        if not events:
            return []
        if self._pool is None:
            raise RuntimeError("EventStore is not connected")

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT current_version FROM event_streams WHERE stream_id = $1 FOR UPDATE",
                    stream_id,
                )
                current = int(row["current_version"]) if row else -1
                if current != expected_version:
                    raise OptimisticConcurrencyError(
                        stream_id=stream_id,
                        expected=expected_version,
                        actual=current,
                    )

                if row is None:
                    await conn.execute(
                        """
                        INSERT INTO event_streams(stream_id, aggregate_type, current_version)
                        VALUES($1, $2, -1)
                        """,
                        stream_id,
                        stream_id.split("-")[0],
                    )

                base_metadata = dict(metadata or {})
                if correlation_id is not None:
                    base_metadata["correlation_id"] = correlation_id
                if causation_id is not None:
                    base_metadata["causation_id"] = causation_id

                positions: list[int] = []
                for offset, event in enumerate(events, start=1):
                    position = expected_version + offset
                    stored_metadata = dict(base_metadata)
                    event_id = await conn.fetchval(
                        """
                        INSERT INTO events(
                            stream_id,
                            stream_position,
                            event_type,
                            event_version,
                            payload,
                            metadata,
                            recorded_at
                        ) VALUES($1, $2, $3, $4, $5::jsonb, $6::jsonb, clock_timestamp())
                        RETURNING event_id
                        """,
                        stream_id,
                        position,
                        event["event_type"],
                        int(event.get("event_version", 1)),
                        json.dumps(event.get("payload", {})),
                        json.dumps(stored_metadata),
                    )

                    await conn.execute(
                        """
                        INSERT INTO outbox(event_id, destination, payload, attempts)
                        VALUES($1, $2, $3::jsonb, 0)
                        """,
                        event_id,
                        "internal.projections",
                        json.dumps(
                            {
                                "stream_id": stream_id,
                                "stream_position": position,
                                "event_type": event["event_type"],
                            }
                        ),
                    )
                    positions.append(position)

                await conn.execute(
                    "UPDATE event_streams SET current_version = $1 WHERE stream_id = $2",
                    expected_version + len(events),
                    stream_id,
                )

                return positions

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[dict]:
        async with self._pool.acquire() as conn:
            query = (
                "SELECT event_id, stream_id, stream_position, global_position, event_type, "
                "event_version, payload, metadata, recorded_at "
                "FROM events WHERE stream_id = $1 AND stream_position >= $2"
            )
            params: list[Any] = [stream_id, from_position]
            if to_position is not None:
                query += " AND stream_position <= $3"
                params.append(to_position)
            query += " ORDER BY stream_position ASC"
            rows = await conn.fetch(query, *params)

        result: list[dict] = []
        for row in rows:
            event = {
                "event_id": row["event_id"],
                "stream_id": row["stream_id"],
                "stream_position": int(row["stream_position"]),
                "global_position": int(row["global_position"]),
                "event_type": row["event_type"],
                "event_version": int(row["event_version"]),
                "payload": dict(row["payload"]),
                "metadata": dict(row["metadata"]),
                "recorded_at": row["recorded_at"],
            }
            if self.upcasters is not None:
                event = self.upcasters.upcast(event)
            result.append(event)
        return result

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: list[str] | None = None,
        batch_size: int = 500,
        **kwargs: Any,
    ) -> AsyncGenerator[dict, None]:
        if "from_position" in kwargs:
            from_global_position = int(kwargs["from_position"])
        cursor = from_global_position
        while True:
            async with self._pool.acquire() as conn:
                if event_types:
                    rows = await conn.fetch(
                        """
                        SELECT event_id, stream_id, stream_position, global_position, event_type,
                               event_version, payload, metadata, recorded_at
                        FROM events
                        WHERE global_position >= $1 AND event_type = ANY($2::text[])
                        ORDER BY global_position ASC
                        LIMIT $3
                        """,
                        cursor,
                        event_types,
                        batch_size,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT event_id, stream_id, stream_position, global_position, event_type,
                               event_version, payload, metadata, recorded_at
                        FROM events
                        WHERE global_position >= $1
                        ORDER BY global_position ASC
                        LIMIT $2
                        """,
                        cursor,
                        batch_size,
                    )
            if not rows:
                break

            for row in rows:
                event = {
                    "event_id": row["event_id"],
                    "stream_id": row["stream_id"],
                    "stream_position": int(row["stream_position"]),
                    "global_position": int(row["global_position"]),
                    "event_type": row["event_type"],
                    "event_version": int(row["event_version"]),
                    "payload": dict(row["payload"]),
                    "metadata": dict(row["metadata"]),
                    "recorded_at": row["recorded_at"],
                }
                if self.upcasters is not None:
                    event = self.upcasters.upcast(event)
                yield event

            cursor = int(rows[-1]["global_position"]) + 1
            if len(rows) < batch_size:
                break

    async def get_event(self, event_id: UUID) -> dict | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT event_id, stream_id, stream_position, global_position, event_type,
                       event_version, payload, metadata, recorded_at
                FROM events
                WHERE event_id = $1
                """,
                event_id,
            )
        if row is None:
            return None
        event = {
            "event_id": row["event_id"],
            "stream_id": row["stream_id"],
            "stream_position": int(row["stream_position"]),
            "global_position": int(row["global_position"]),
            "event_type": row["event_type"],
            "event_version": int(row["event_version"]),
            "payload": dict(row["payload"]),
            "metadata": dict(row["metadata"]),
            "recorded_at": row["recorded_at"],
        }
        if self.upcasters is not None:
            event = self.upcasters.upcast(event)
        return event

    async def save_checkpoint(self, projection_name: str, position: int) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO projection_checkpoints(projection_name, last_position, updated_at)
                VALUES($1, $2, NOW())
                ON CONFLICT (projection_name)
                DO UPDATE SET last_position = EXCLUDED.last_position, updated_at = NOW()
                """,
                projection_name,
                position,
            )

    async def load_checkpoint(self, projection_name: str) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
                projection_name,
            )
        return int(row["last_position"]) if row else 0

    async def archive_stream(self, stream_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE event_streams SET archived_at = NOW() WHERE stream_id = $1",
                stream_id,
            )

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT stream_id, aggregate_type, current_version, created_at, archived_at, metadata
                FROM event_streams
                WHERE stream_id = $1
                """,
                stream_id,
            )
        if row is None:
            raise KeyError(f"Stream not found: {stream_id}")

        return StreamMetadata(
            stream_id=row["stream_id"],
            aggregate_type=row["aggregate_type"],
            current_version=int(row["current_version"]),
            created_at=row["created_at"],
            archived_at=row["archived_at"],
            metadata=dict(row["metadata"]),
        )


class InMemoryEventStore:
    def __init__(self, upcaster_registry: UpcasterRegistry | None = None):
        self.upcasters = upcaster_registry
        self._streams: dict[str, list[dict]] = defaultdict(list)
        self._global: list[dict] = []
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._checkpoints: dict[str, int] = {}
        self._archived_streams: set[str] = set()
        self._stream_metadata: dict[str, dict[str, Any]] = {}

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def stream_version(self, stream_id: str) -> int:
        events = self._streams.get(stream_id, [])
        return len(events) - 1

    async def append(
        self,
        stream_id: str,
        events: list[dict],
        expected_version: int,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[int]:
        async with self._locks[stream_id]:
            current = await self.stream_version(stream_id)
            if current != expected_version:
                raise OptimisticConcurrencyError(
                    stream_id=stream_id, expected=expected_version, actual=current
                )

            meta = dict(metadata or {})
            if correlation_id is not None:
                meta["correlation_id"] = correlation_id
            if causation_id is not None:
                meta["causation_id"] = causation_id

            if stream_id not in self._stream_metadata:
                self._stream_metadata[stream_id] = {
                    "stream_id": stream_id,
                    "aggregate_type": stream_id.split("-")[0],
                    "created_at": datetime.now(timezone.utc),
                    "archived_at": None,
                    "metadata": {},
                }

            positions: list[int] = []
            for offset, event in enumerate(events, start=1):
                stream_position = expected_version + offset
                stored = {
                    "event_id": str(uuid4()),
                    "stream_id": stream_id,
                    "stream_position": stream_position,
                    "global_position": len(self._global),
                    "event_type": event["event_type"],
                    "event_version": int(event.get("event_version", 1)),
                    "payload": dict(event.get("payload", {})),
                    "metadata": dict(meta),
                    "recorded_at": datetime.now(timezone.utc),
                }
                self._streams[stream_id].append(stored)
                self._global.append(stored)
                positions.append(stream_position)

            return positions

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[dict]:
        rows = [
            dict(e)
            for e in self._streams.get(stream_id, [])
            if e["stream_position"] >= from_position
            and (to_position is None or e["stream_position"] <= to_position)
        ]
        rows.sort(key=lambda e: e["stream_position"])
        if self.upcasters is None:
            return rows
        return [self.upcasters.upcast(dict(r)) for r in rows]

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: list[str] | None = None,
        batch_size: int = 500,
        **kwargs: Any,
    ) -> AsyncGenerator[dict, None]:
        if "from_position" in kwargs:
            from_global_position = int(kwargs["from_position"])
        emitted = 0
        for event in self._global:
            if event["global_position"] < from_global_position:
                continue
            if event_types and event["event_type"] not in event_types:
                continue
            emitted += 1
            row = dict(event)
            if self.upcasters is not None:
                row = self.upcasters.upcast(row)
            yield row
            if emitted % max(batch_size, 1) == 0:
                await asyncio.sleep(0)

    async def archive_stream(self, stream_id: str) -> None:
        self._archived_streams.add(stream_id)
        meta = self._stream_metadata.setdefault(
            stream_id,
            {
                "stream_id": stream_id,
                "aggregate_type": stream_id.split("-")[0],
                "created_at": datetime.now(timezone.utc),
                "archived_at": None,
                "metadata": {},
            },
        )
        meta["archived_at"] = datetime.now(timezone.utc)

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata:
        meta = self._stream_metadata.get(stream_id)
        if meta is None:
            raise KeyError(f"Stream not found: {stream_id}")
        return StreamMetadata(
            stream_id=meta["stream_id"],
            aggregate_type=meta["aggregate_type"],
            current_version=await self.stream_version(stream_id),
            created_at=meta["created_at"],
            archived_at=meta["archived_at"],
            metadata=dict(meta.get("metadata", {})),
        )

    async def get_event(self, event_id: UUID | str) -> dict | None:
        target = str(event_id)
        for event in self._global:
            if str(event["event_id"]) == target:
                return dict(event)
        return None

    async def save_checkpoint(self, projection_name: str, position: int) -> None:
        self._checkpoints[projection_name] = int(position)

    async def load_checkpoint(self, projection_name: str) -> int:
        return self._checkpoints.get(projection_name, 0)

