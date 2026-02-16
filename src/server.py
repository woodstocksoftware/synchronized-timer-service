"""
Synchronized Timer Service

Server-authoritative timer system for proctored testing.
All clients receive synchronized time from the server.
"""

import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import StrEnum

from fastapi import FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# ============================================================
# CONSTANTS
# ============================================================

DEFAULT_WARNING_THRESHOLDS = [300, 60, 30]
MAX_TIMERS = 1000
MAX_CLIENTS_PER_TIMER = 100


# ============================================================
# MODELS
# ============================================================


class TimerState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    EXPIRED = "expired"


class TimerAction(StrEnum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    EXTEND = "extend"
    STOP = "stop"


# Valid actions per state for better error messages
VALID_ACTIONS: dict[TimerState, list[TimerAction]] = {
    TimerState.CREATED: [TimerAction.START, TimerAction.EXTEND, TimerAction.STOP],
    TimerState.RUNNING: [TimerAction.PAUSE, TimerAction.EXTEND, TimerAction.STOP],
    TimerState.PAUSED: [TimerAction.RESUME, TimerAction.EXTEND, TimerAction.STOP],
    TimerState.COMPLETED: [],
    TimerState.EXPIRED: [],
}


def _format_ts(dt: datetime | None) -> str | None:
    """Format a UTC datetime as ISO 8601 with Z suffix."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TimerCreate(BaseModel):
    duration_seconds: int = Field(
        ..., ge=1, le=86400, description="Timer duration (1 sec to 24 hours)"
    )
    name: str = Field("Timer", min_length=1, max_length=200, description="Timer name")
    warning_thresholds: list[int] = Field(
        default=DEFAULT_WARNING_THRESHOLDS,
        max_length=20,
        description="Warning at these seconds remaining",
    )
    auto_start: bool = Field(False, description="Start immediately on creation")

    @field_validator("warning_thresholds")
    @classmethod
    def validate_thresholds(cls, v: list[int]) -> list[int]:
        for t in v:
            if t < 1 or t > 86400:
                raise ValueError(f"Threshold {t} must be between 1 and 86400")
        return v


class TimerUpdate(BaseModel):
    action: TimerAction = Field(..., description="Timer control action")
    extend_seconds: int | None = Field(
        None, ge=1, le=3600, description="Seconds to add (for extend action)"
    )


class TimerResponse(BaseModel):
    id: str
    name: str
    state: TimerState
    duration_seconds: int
    remaining_seconds: int
    elapsed_seconds: int
    warning_thresholds: list[int]
    connected_clients: int
    created_at: str
    started_at: str | None
    completed_at: str | None


class BulkTimerIds(BaseModel):
    timer_ids: list[str] = Field(..., max_length=100)


class BulkExtendRequest(BaseModel):
    timer_ids: list[str] = Field(..., max_length=100)
    seconds: int = Field(..., ge=1, le=3600, description="Seconds to add")


class BulkResultItem(BaseModel):
    timer_id: str
    success: bool
    status: str


# ============================================================
# TIMER CLASS
# ============================================================


class Timer:
    """Server-authoritative timer."""

    def __init__(
        self,
        timer_id: str,
        duration_seconds: int,
        name: str = "Timer",
        warning_thresholds: list[int] | None = None,
    ):
        self.id = timer_id
        self.name = name
        self.duration_seconds = duration_seconds
        self.remaining_seconds = duration_seconds
        self.warning_thresholds = warning_thresholds or DEFAULT_WARNING_THRESHOLDS
        self.warnings_sent: set[int] = set()

        self.state = TimerState.CREATED
        self.created_at = datetime.now(UTC)
        self.started_at: datetime | None = None
        self.paused_at: datetime | None = None
        self.completed_at: datetime | None = None

        # Connected WebSocket clients
        self.clients: dict[str, WebSocket] = {}

        # Task for running the timer
        self._task: asyncio.Task | None = None

        # Lock for state transitions
        self._lock = asyncio.Lock()

    @property
    def elapsed_seconds(self) -> int:
        return self.duration_seconds - self.remaining_seconds

    def to_response(self) -> TimerResponse:
        return TimerResponse(
            id=self.id,
            name=self.name,
            state=self.state,
            duration_seconds=self.duration_seconds,
            remaining_seconds=self.remaining_seconds,
            elapsed_seconds=self.elapsed_seconds,
            warning_thresholds=self.warning_thresholds,
            connected_clients=len(self.clients),
            created_at=_format_ts(self.created_at),
            started_at=_format_ts(self.started_at),
            completed_at=_format_ts(self.completed_at),
        )

    async def add_client(self, client_id: str, websocket: WebSocket):
        """Add a client connection."""
        if len(self.clients) >= MAX_CLIENTS_PER_TIMER:
            await websocket.close(code=4029, reason="Too many connections")
            return False

        await websocket.accept()
        self.clients[client_id] = websocket

        # Send current state
        await self._send_to_client(
            client_id, {"type": "connected", "timer": self.to_response().model_dump()}
        )
        return True

    def remove_client(self, client_id: str):
        """Remove a client connection."""
        if client_id in self.clients:
            del self.clients[client_id]

    async def broadcast(self, message: dict):
        """Send message to all connected clients."""
        disconnected = []

        for client_id, ws in self.clients.items():
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.append(client_id)

        for client_id in disconnected:
            self.remove_client(client_id)

    async def _send_to_client(self, client_id: str, message: dict):
        """Send message to specific client."""
        if client_id in self.clients:
            try:
                await self.clients[client_id].send_json(message)
            except Exception:
                self.remove_client(client_id)

    async def start(self):
        """Start the timer."""
        async with self._lock:
            if self.state not in [TimerState.CREATED, TimerState.PAUSED]:
                return False

            self.state = TimerState.RUNNING
            if not self.started_at:
                self.started_at = datetime.now(UTC)

            await self.broadcast(
                {"type": "timer_started", "remaining_seconds": self.remaining_seconds}
            )

            # Start the tick loop
            self._task = asyncio.create_task(self._run())
            return True

    async def pause(self):
        """Pause the timer."""
        async with self._lock:
            if self.state != TimerState.RUNNING:
                return False

            self.state = TimerState.PAUSED
            self.paused_at = datetime.now(UTC)

            if self._task:
                self._task.cancel()
                self._task = None

            await self.broadcast(
                {"type": "timer_paused", "remaining_seconds": self.remaining_seconds}
            )
            return True

    async def resume(self):
        """Resume a paused timer."""
        if self.state != TimerState.PAUSED:
            return False

        return await self.start()

    async def extend(self, seconds: int):
        """Add time to the timer."""
        async with self._lock:
            if self.state in [TimerState.COMPLETED, TimerState.EXPIRED]:
                return False

            self.remaining_seconds += seconds
            self.duration_seconds += seconds

            await self.broadcast(
                {
                    "type": "timer_extended",
                    "added_seconds": seconds,
                    "remaining_seconds": self.remaining_seconds,
                }
            )
            return True

    async def stop(self):
        """Stop the timer (manual completion)."""
        async with self._lock:
            if self.state in [TimerState.COMPLETED, TimerState.EXPIRED]:
                return False

            self.state = TimerState.COMPLETED
            self.completed_at = datetime.now(UTC)

            if self._task:
                self._task.cancel()
                self._task = None

            await self.broadcast(
                {
                    "type": "timer_stopped",
                    "remaining_seconds": self.remaining_seconds,
                    "elapsed_seconds": self.elapsed_seconds,
                }
            )
            return True

    async def cleanup(self):
        """Cancel task and disconnect all clients. Called before deletion."""
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

        await self.broadcast({"type": "timer_deleted"})

        for _client_id, ws in list(self.clients.items()):
            with contextlib.suppress(Exception):
                await ws.close(code=4000, reason="Timer deleted")
        self.clients.clear()

    async def _run(self):
        """Main timer loop - ticks every second."""
        try:
            while self.remaining_seconds > 0 and self.state == TimerState.RUNNING:
                await asyncio.sleep(1)
                self.remaining_seconds -= 1

                # Check for warnings
                for threshold in self.warning_thresholds:
                    if self.remaining_seconds == threshold and threshold not in self.warnings_sent:
                        self.warnings_sent.add(threshold)
                        await self.broadcast(
                            {
                                "type": "timer_warning",
                                "remaining_seconds": self.remaining_seconds,
                                "threshold": threshold,
                            }
                        )

                # Broadcast tick
                await self.broadcast(
                    {
                        "type": "timer_tick",
                        "remaining_seconds": self.remaining_seconds,
                        "elapsed_seconds": self.elapsed_seconds,
                    }
                )

            # Timer expired
            if self.remaining_seconds <= 0 and self.state == TimerState.RUNNING:
                self.state = TimerState.EXPIRED
                self.completed_at = datetime.now(UTC)

                await self.broadcast(
                    {"type": "timer_expired", "elapsed_seconds": self.elapsed_seconds}
                )

        except asyncio.CancelledError:
            pass


# ============================================================
# TIMER MANAGER
# ============================================================


class TimerManager:
    """Manages all active timers."""

    def __init__(self):
        self.timers: dict[str, Timer] = {}

    def create_timer(
        self,
        duration_seconds: int,
        name: str = "Timer",
        warning_thresholds: list[int] | None = None,
    ) -> Timer:
        if len(self.timers) >= MAX_TIMERS:
            raise ValueError("Maximum timer limit reached")

        timer_id = f"timer_{uuid.uuid4().hex[:8]}"
        timer = Timer(
            timer_id=timer_id,
            duration_seconds=duration_seconds,
            name=name,
            warning_thresholds=warning_thresholds,
        )
        self.timers[timer_id] = timer
        return timer

    def get_timer(self, timer_id: str) -> Timer | None:
        return self.timers.get(timer_id)

    def delete_timer(self, timer_id: str) -> bool:
        if timer_id in self.timers:
            del self.timers[timer_id]
            return True
        return False

    def list_timers(self) -> list[Timer]:
        return list(self.timers.values())


# Global manager
manager = TimerManager()


# ============================================================
# FASTAPI APP
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Synchronized Timer Service started")
    yield
    logger.info("Synchronized Timer Service stopped")


app = FastAPI(
    title="Synchronized Timer Service",
    description="Server-authoritative timer system for proctored testing",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# HEALTH CHECK
# ============================================================


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "timers_active": len(manager.timers)}


# ============================================================
# REST ENDPOINTS
# ============================================================


@app.post("/timers", response_model=TimerResponse, status_code=201)
async def create_timer(request: TimerCreate):
    """Create a new timer."""
    try:
        timer = manager.create_timer(
            duration_seconds=request.duration_seconds,
            name=request.name,
            warning_thresholds=request.warning_thresholds,
        )
    except ValueError as e:
        raise HTTPException(status_code=429, detail=str(e)) from None

    if request.auto_start:
        await timer.start()

    return timer.to_response()


@app.get("/timers", response_model=list[TimerResponse])
async def list_timers(
    state: TimerState | None = Query(None, description="Filter by timer state"),
):
    """List all timers, optionally filtered by state."""
    timers = manager.list_timers()
    if state is not None:
        timers = [t for t in timers if t.state == state]
    return [t.to_response() for t in timers]


@app.get("/timers/{timer_id}", response_model=TimerResponse)
async def get_timer(timer_id: str):
    """Get timer status."""
    timer = manager.get_timer(timer_id)
    if not timer:
        raise HTTPException(status_code=404, detail="Timer not found")
    return timer.to_response()


@app.post("/timers/{timer_id}/control", response_model=TimerResponse)
async def control_timer(timer_id: str, request: TimerUpdate):
    """Control a timer (start, pause, resume, extend, stop)."""
    timer = manager.get_timer(timer_id)
    if not timer:
        raise HTTPException(status_code=404, detail="Timer not found")

    success = False

    if request.action == TimerAction.START:
        success = await timer.start()
    elif request.action == TimerAction.PAUSE:
        success = await timer.pause()
    elif request.action == TimerAction.RESUME:
        success = await timer.resume()
    elif request.action == TimerAction.EXTEND:
        if request.extend_seconds:
            success = await timer.extend(request.extend_seconds)
        else:
            raise HTTPException(status_code=400, detail="extend_seconds required for extend action")
    elif request.action == TimerAction.STOP:
        success = await timer.stop()

    if not success:
        valid = ", ".join(VALID_ACTIONS.get(timer.state, []))
        detail = f"Cannot {request.action} timer in state '{timer.state}'. Valid actions: [{valid}]"
        raise HTTPException(status_code=400, detail=detail)

    return timer.to_response()


@app.delete("/timers/{timer_id}", status_code=204)
async def delete_timer(timer_id: str):
    """Delete a timer."""
    timer = manager.get_timer(timer_id)
    if not timer:
        raise HTTPException(status_code=404, detail="Timer not found")

    await timer.cleanup()
    manager.delete_timer(timer_id)
    return Response(status_code=204)


# ============================================================
# WEBSOCKET ENDPOINT
# ============================================================


@app.websocket("/ws/{timer_id}")
async def websocket_endpoint(websocket: WebSocket, timer_id: str):
    """
    WebSocket connection to receive real-time timer updates.

    Messages received:
    - connected: Initial state when connected
    - timer_started: Timer started
    - timer_tick: Every second with remaining/elapsed time
    - timer_warning: When warning threshold reached
    - timer_paused: Timer paused
    - timer_extended: Time added
    - timer_stopped: Timer manually stopped
    - timer_expired: Timer reached zero
    - timer_deleted: Timer was deleted
    """
    timer = manager.get_timer(timer_id)
    if not timer:
        await websocket.close(code=4004, reason="Timer not found")
        return

    client_id = f"client_{uuid.uuid4().hex[:8]}"

    try:
        accepted = await timer.add_client(client_id, websocket)
        if not accepted:
            return

        # Keep connection alive
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=30)

                # Handle ping
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})

                # Handle sync request
                elif data.get("type") == "sync":
                    await websocket.send_json(
                        {
                            "type": "sync_response",
                            "server_time": _format_ts(datetime.now(UTC)),
                            "remaining_seconds": timer.remaining_seconds,
                            "state": timer.state,
                        }
                    )

                else:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": f"Unknown message type: {data.get('type')}",
                        }
                    )

            except TimeoutError:
                # Send keepalive
                await websocket.send_json(
                    {"type": "keepalive", "remaining_seconds": timer.remaining_seconds}
                )

    except WebSocketDisconnect:
        timer.remove_client(client_id)
    except Exception:
        logger.warning("WebSocket error for client %s on timer %s", client_id, timer_id)
        timer.remove_client(client_id)


# ============================================================
# BULK OPERATIONS (for classroom scenarios)
# ============================================================


@app.post("/timers/bulk/start", response_model=list[BulkResultItem])
async def bulk_start(request: BulkTimerIds):
    """Start multiple timers simultaneously."""
    results = []
    for timer_id in request.timer_ids:
        timer = manager.get_timer(timer_id)
        if timer:
            success = await timer.start()
            status = "started" if success else f"failed (state: {timer.state})"
            results.append(BulkResultItem(timer_id=timer_id, success=success, status=status))
        else:
            results.append(BulkResultItem(timer_id=timer_id, success=False, status="not found"))
    return results


@app.post("/timers/bulk/pause", response_model=list[BulkResultItem])
async def bulk_pause(request: BulkTimerIds):
    """Pause multiple timers simultaneously."""
    results = []
    for timer_id in request.timer_ids:
        timer = manager.get_timer(timer_id)
        if timer:
            success = await timer.pause()
            status = "paused" if success else f"failed (state: {timer.state})"
            results.append(BulkResultItem(timer_id=timer_id, success=success, status=status))
        else:
            results.append(BulkResultItem(timer_id=timer_id, success=False, status="not found"))
    return results


@app.post("/timers/bulk/resume", response_model=list[BulkResultItem])
async def bulk_resume(request: BulkTimerIds):
    """Resume multiple paused timers simultaneously."""
    results = []
    for timer_id in request.timer_ids:
        timer = manager.get_timer(timer_id)
        if timer:
            success = await timer.resume()
            status = "resumed" if success else f"failed (state: {timer.state})"
            results.append(BulkResultItem(timer_id=timer_id, success=success, status=status))
        else:
            results.append(BulkResultItem(timer_id=timer_id, success=False, status="not found"))
    return results


@app.post("/timers/bulk/stop", response_model=list[BulkResultItem])
async def bulk_stop(request: BulkTimerIds):
    """Stop multiple timers simultaneously."""
    results = []
    for timer_id in request.timer_ids:
        timer = manager.get_timer(timer_id)
        if timer:
            success = await timer.stop()
            status = "stopped" if success else f"failed (state: {timer.state})"
            results.append(BulkResultItem(timer_id=timer_id, success=success, status=status))
        else:
            results.append(BulkResultItem(timer_id=timer_id, success=False, status="not found"))
    return results


@app.post("/timers/bulk/extend", response_model=list[BulkResultItem])
async def bulk_extend(request: BulkExtendRequest):
    """Extend multiple timers by same amount."""
    results = []
    for timer_id in request.timer_ids:
        timer = manager.get_timer(timer_id)
        if timer:
            success = await timer.extend(request.seconds)
            status = (
                f"extended by {request.seconds}s" if success else f"failed (state: {timer.state})"
            )
            results.append(BulkResultItem(timer_id=timer_id, success=success, status=status))
        else:
            results.append(BulkResultItem(timer_id=timer_id, success=False, status="not found"))
    return results


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8003)
