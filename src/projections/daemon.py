from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

class ProjectionDaemon:
    """Async daemon that polls the event store and routes events to projections."""

    def __init__(
        self,
        store,
        projections: list,
        max_retries: int = 3,
        batch_size: int = 200,
    ):
        self._store = store
        self._projections = {p.name: p for p in projections}
        self._running = False
        self._lag_ms: dict[str, float] = {p.name: 0.0 for p in projections}
        self._checkpoints: dict[str, int] = {}
        self._error_counts: dict[str, int] = {}
        self._max_retries: int = max_retries
        self._batch_size: int = max(1, int(batch_size))
        # Tracks the wall-clock time (ms) of the most recently processed event per projection
        self._last_event_time_ms: dict[str, float] = {}

    async def run_forever(self, poll_interval_ms: int = 100) -> None:
        self._running = True
        logger.info("ProjectionDaemon started with %d projections", len(self._projections))
        while self._running:
            try:
                await self._process_batch()
            except Exception as e:
                logger.error("Daemon batch error (non-fatal): %s", e)
            await asyncio.sleep(poll_interval_ms / 1000)

    async def stop(self) -> None:
        self._running = False

    async def process_once(self) -> int:
        """Process one batch. Returns number of events processed."""
        return await self._process_batch()

    def get_lag(self, projection_name: str) -> float:
        """Return lag in ms for a projection."""
        return self._lag_ms.get(projection_name, 0.0)

    def get_all_lags(self) -> dict[str, float]:
        return dict(self._lag_ms)

    async def _process_batch(self) -> int:
        # Find minimum checkpoint across all projections
        min_pos = float("inf")
        for name in self._projections:
            pos = await self._store.load_checkpoint(name)
            self._checkpoints[name] = pos
            if pos < min_pos:
                min_pos = pos
        if min_pos == float("inf"):
            min_pos = 0

        events_processed = 0
        batch_events: list[dict[str, Any]] = []
        last_global_pos = int(min_pos) - 1

        async for event in self._store.load_all(
            from_global_position=int(min_pos),
            batch_size=self._batch_size,
        ):
            batch_events.append(event)
            if len(batch_events) < self._batch_size:
                continue
            processed, last_global_pos = await self._process_events_batch(batch_events)
            events_processed += processed
            batch_events = []

        if batch_events:
            processed, last_global_pos = await self._process_events_batch(batch_events)
            events_processed += processed

        # Compute real lag: wall-clock time since the last event each projection processed
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        for name in self._projections:
            last_ts = self._last_event_time_ms.get(name)
            self._lag_ms[name] = now_ms - last_ts if last_ts is not None else 0.0

        return events_processed

    async def _process_events_batch(self, events: list[dict[str, Any]]) -> tuple[int, int]:
        events_processed = 0
        last_global_pos = -1
        for event in events:
            gpos = int(event.get("global_position", 0))
            for name, projection in self._projections.items():
                if self._checkpoints.get(name, 0) > gpos:
                    continue  # this projection is ahead, skip
                err_key = f"{name}:{gpos}"
                if self._error_counts.get(err_key, 0) >= self._max_retries:
                    logger.error(
                        "Projection %s permanently skipped event %s after %d retries",
                        name,
                        gpos,
                        self._max_retries,
                    )
                    continue  # skip permanently failed events
                try:
                    await projection.handle(event)
                    self._error_counts.pop(err_key, None)
                    # Track when this event was recorded for real lag measurement
                    recorded_at = event.get("recorded_at")
                    if recorded_at:
                        try:
                            ts = datetime.fromisoformat(str(recorded_at))
                            if ts.tzinfo is None:
                                ts = ts.replace(tzinfo=timezone.utc)
                            self._last_event_time_ms[name] = ts.timestamp() * 1000
                        except (ValueError, TypeError):
                            pass
                except Exception as e:
                    self._error_counts[err_key] = self._error_counts.get(err_key, 0) + 1
                    logger.warning(
                        "Projection %s failed on event %s (attempt %d/%d): %s",
                        name, gpos, self._error_counts[err_key], self._max_retries, e
                    )

            last_global_pos = gpos
            events_processed += 1

        # Save checkpoints after processing batch
        if events_processed > 0:
            for name in self._projections:
                await self._store.save_checkpoint(name, last_global_pos + 1)
                self._checkpoints[name] = last_global_pos + 1
        return events_processed, last_global_pos
