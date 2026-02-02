"""
Synchronized Timer Service

Server-authoritative timer system for proctored testing.
All clients receive synchronized time from the server.
"""

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Dict, Set, Optional
from enum import Enum
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# ============================================================
# MODELS
# ============================================================

class TimerState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    EXPIRED = "expired"


class TimerCreate(BaseModel):
    duration_seconds: int = Field(..., ge=1, le=86400, description="Timer duration (1 sec to 24 hours)")
    name: str = Field("Timer", description="Timer name")
    warning_thresholds: list[int] = Field(default=[300, 60, 30], description="Warning at these seconds remaining")
    auto_start: bool = Field(False, description="Start immediately on creation")


class TimerUpdate(BaseModel):
    action: str = Field(..., description="start, pause, resume, extend, stop")
    extend_seconds: Optional[int] = Field(None, ge=1, le=3600, description="Seconds to add (for extend action)")


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
    started_at: Optional[str]
    completed_at: Optional[str]


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
        warning_thresholds: list[int] = None
    ):
        self.id = timer_id
        self.name = name
        self.duration_seconds = duration_seconds
        self.remaining_seconds = duration_seconds
        self.warning_thresholds = warning_thresholds or [300, 60, 30]
        self.warnings_sent: Set[int] = set()
        
        self.state = TimerState.CREATED
        self.created_at = datetime.utcnow()
        self.started_at: Optional[datetime] = None
        self.paused_at: Optional[datetime] = None
        self.completed_at: Optional[datetime] = None
        
        # Connected WebSocket clients
        self.clients: Dict[str, WebSocket] = {}
        
        # Task for running the timer
        self._task: Optional[asyncio.Task] = None
    
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
            created_at=self.created_at.isoformat() + "Z",
            started_at=self.started_at.isoformat() + "Z" if self.started_at else None,
            completed_at=self.completed_at.isoformat() + "Z" if self.completed_at else None
        )
    
    async def add_client(self, client_id: str, websocket: WebSocket):
        """Add a client connection."""
        await websocket.accept()
        self.clients[client_id] = websocket
        
        # Send current state
        await self._send_to_client(client_id, {
            "type": "connected",
            "timer": self.to_response().model_dump()
        })
    
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
        if self.state not in [TimerState.CREATED, TimerState.PAUSED]:
            return False
        
        self.state = TimerState.RUNNING
        if not self.started_at:
            self.started_at = datetime.utcnow()
        
        await self.broadcast({
            "type": "timer_started",
            "remaining_seconds": self.remaining_seconds
        })
        
        # Start the tick loop
        self._task = asyncio.create_task(self._run())
        return True
    
    async def pause(self):
        """Pause the timer."""
        if self.state != TimerState.RUNNING:
            return False
        
        self.state = TimerState.PAUSED
        self.paused_at = datetime.utcnow()
        
        if self._task:
            self._task.cancel()
            self._task = None
        
        await self.broadcast({
            "type": "timer_paused",
            "remaining_seconds": self.remaining_seconds
        })
        return True
    
    async def resume(self):
        """Resume a paused timer."""
        if self.state != TimerState.PAUSED:
            return False
        
        return await self.start()
    
    async def extend(self, seconds: int):
        """Add time to the timer."""
        self.remaining_seconds += seconds
        self.duration_seconds += seconds
        
        await self.broadcast({
            "type": "timer_extended",
            "added_seconds": seconds,
            "remaining_seconds": self.remaining_seconds
        })
        return True
    
    async def stop(self):
        """Stop the timer (manual completion)."""
        if self.state in [TimerState.COMPLETED, TimerState.EXPIRED]:
            return False
        
        self.state = TimerState.COMPLETED
        self.completed_at = datetime.utcnow()
        
        if self._task:
            self._task.cancel()
            self._task = None
        
        await self.broadcast({
            "type": "timer_stopped",
            "remaining_seconds": self.remaining_seconds,
            "elapsed_seconds": self.elapsed_seconds
        })
        return True
    
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
                        await self.broadcast({
                            "type": "timer_warning",
                            "remaining_seconds": self.remaining_seconds,
                            "threshold": threshold
                        })
                
                # Broadcast tick
                await self.broadcast({
                    "type": "timer_tick",
                    "remaining_seconds": self.remaining_seconds,
                    "elapsed_seconds": self.elapsed_seconds
                })
            
            # Timer expired
            if self.remaining_seconds <= 0 and self.state == TimerState.RUNNING:
                self.state = TimerState.EXPIRED
                self.completed_at = datetime.utcnow()
                
                await self.broadcast({
                    "type": "timer_expired",
                    "elapsed_seconds": self.elapsed_seconds
                })
        
        except asyncio.CancelledError:
            pass


# ============================================================
# TIMER MANAGER
# ============================================================

class TimerManager:
    """Manages all active timers."""
    
    def __init__(self):
        self.timers: Dict[str, Timer] = {}
    
    def create_timer(
        self,
        duration_seconds: int,
        name: str = "Timer",
        warning_thresholds: list[int] = None,
        auto_start: bool = False
    ) -> Timer:
        timer_id = f"timer_{uuid.uuid4().hex[:8]}"
        timer = Timer(
            timer_id=timer_id,
            duration_seconds=duration_seconds,
            name=name,
            warning_thresholds=warning_thresholds
        )
        self.timers[timer_id] = timer
        return timer
    
    def get_timer(self, timer_id: str) -> Optional[Timer]:
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
    print("🚀 Synchronized Timer Service started!")
    yield
    print("👋 Timer Service stopped!")


app = FastAPI(
    title="Synchronized Timer Service",
    description="Server-authoritative timer system for proctored testing",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# REST ENDPOINTS
# ============================================================

@app.post("/timers", response_model=TimerResponse)
async def create_timer(request: TimerCreate):
    """Create a new timer."""
    timer = manager.create_timer(
        duration_seconds=request.duration_seconds,
        name=request.name,
        warning_thresholds=request.warning_thresholds,
        auto_start=request.auto_start
    )
    
    if request.auto_start:
        await timer.start()
    
    return timer.to_response()


@app.get("/timers", response_model=list[TimerResponse])
async def list_timers():
    """List all timers."""
    return [t.to_response() for t in manager.list_timers()]


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
    
    if request.action == "start":
        success = await timer.start()
    elif request.action == "pause":
        success = await timer.pause()
    elif request.action == "resume":
        success = await timer.resume()
    elif request.action == "extend":
        if request.extend_seconds:
            success = await timer.extend(request.extend_seconds)
        else:
            raise HTTPException(status_code=400, detail="extend_seconds required for extend action")
    elif request.action == "stop":
        success = await timer.stop()
    else:
        raise HTTPException(status_code=400, detail=f"Invalid action: {request.action}")
    
    if not success:
        raise HTTPException(status_code=400, detail=f"Cannot {request.action} timer in state {timer.state}")
    
    return timer.to_response()


@app.delete("/timers/{timer_id}")
async def delete_timer(timer_id: str):
    """Delete a timer."""
    timer = manager.get_timer(timer_id)
    if not timer:
        raise HTTPException(status_code=404, detail="Timer not found")
    
    # Stop if running
    if timer.state == TimerState.RUNNING:
        await timer.stop()
    
    manager.delete_timer(timer_id)
    return {"status": "deleted"}


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
    """
    timer = manager.get_timer(timer_id)
    if not timer:
        await websocket.close(code=4004, reason="Timer not found")
        return
    
    client_id = f"client_{uuid.uuid4().hex[:8]}"
    
    try:
        await timer.add_client(client_id, websocket)
        
        # Keep connection alive
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=30)
                
                # Handle ping
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                
                # Handle sync request
                elif data.get("type") == "sync":
                    await websocket.send_json({
                        "type": "sync_response",
                        "server_time": datetime.utcnow().isoformat() + "Z",
                        "remaining_seconds": timer.remaining_seconds,
                        "state": timer.state
                    })
            
            except asyncio.TimeoutError:
                # Send keepalive
                await websocket.send_json({
                    "type": "keepalive",
                    "remaining_seconds": timer.remaining_seconds
                })
    
    except WebSocketDisconnect:
        timer.remove_client(client_id)
    except Exception as e:
        print(f"WebSocket error: {e}")
        timer.remove_client(client_id)


# ============================================================
# BULK OPERATIONS (for classroom scenarios)
# ============================================================

@app.post("/timers/bulk/start")
async def bulk_start(timer_ids: list[str]):
    """Start multiple timers simultaneously."""
    results = {}
    for timer_id in timer_ids:
        timer = manager.get_timer(timer_id)
        if timer:
            success = await timer.start()
            results[timer_id] = "started" if success else f"failed (state: {timer.state})"
        else:
            results[timer_id] = "not found"
    return results


@app.post("/timers/bulk/pause")
async def bulk_pause(timer_ids: list[str]):
    """Pause multiple timers simultaneously."""
    results = {}
    for timer_id in timer_ids:
        timer = manager.get_timer(timer_id)
        if timer:
            success = await timer.pause()
            results[timer_id] = "paused" if success else f"failed (state: {timer.state})"
        else:
            results[timer_id] = "not found"
    return results


@app.post("/timers/bulk/extend")
async def bulk_extend(timer_ids: list[str], seconds: int):
    """Extend multiple timers by same amount."""
    results = {}
    for timer_id in timer_ids:
        timer = manager.get_timer(timer_id)
        if timer:
            await timer.extend(seconds)
            results[timer_id] = f"extended by {seconds}s"
        else:
            results[timer_id] = "not found"
    return results


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
