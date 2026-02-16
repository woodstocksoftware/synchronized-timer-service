# CLAUDE.md — Synchronized Timer Service

## What This Is
Server-authoritative timer microservice for proctored testing. Part of the AdaptiveTest ed-tech suite by Woodstock Software. Public portfolio repo.

## Tech Stack
- **Language:** Python 3.12
- **Framework:** FastAPI 0.109+ (async)
- **Real-time:** WebSocket via FastAPI native support
- **Validation:** Pydantic v2
- **Server:** Uvicorn (ASGI)
- **Linting:** Ruff
- **Testing:** pytest + pytest-asyncio + httpx (async test client)

## Architecture
Single-file service (`src/server.py`, ~700 lines) with four core components:
- **Pydantic Models** — `TimerCreate`, `TimerUpdate`, `TimerResponse`, `TimerState`, `TimerAction` enums, `BulkTimerIds`, `BulkExtendRequest`, `BulkResultItem`
- **Timer class** — Individual timer with async tick loop, asyncio.Lock for state transitions, WebSocket client management, broadcast
- **TimerManager class** — In-memory registry of all active timers (max 1000)
- **FastAPI app** — REST endpoints + WebSocket endpoint + bulk operations + health check

Key design: server-authoritative timing (clients cannot manipulate time). Each timer runs its own `asyncio.Task` ticking every second. All connected WebSocket clients receive identical broadcasts. Max 100 clients per timer.

## Running
```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -e .
python -m uvicorn src.server:app --reload --port 8003
```

API docs at http://localhost:8003/docs (Swagger UI).

## Testing
```bash
pip install -e ".[dev]"
pytest
pytest --cov=src
```

## Linting
```bash
ruff check src/ tests/
ruff format src/ tests/
```

## Key Patterns
- **State machine:** CREATED -> RUNNING -> PAUSED/COMPLETED/EXPIRED (enforced via asyncio.Lock)
- **Broadcast pattern:** Timer changes pushed to all WebSocket clients simultaneously
- **Keepalive:** 30-second WebSocket timeout with keepalive messages
- **Sync request:** Clients can send `{"type": "sync"}` to get server time + timer state
- **Warning thresholds:** Configurable (max 20, values 1-86400), deduplicated via `warnings_sent` set
- **Bulk operations:** Classroom scenarios — start/pause/resume/stop/extend multiple timers at once
- **Resource limits:** MAX_TIMERS=1000, MAX_CLIENTS_PER_TIMER=100
- **Cleanup on delete:** Timer.cleanup() cancels tasks, broadcasts timer_deleted, closes all WS clients

## API Endpoints
- `GET /health` — Health check
- `POST /timers` — Create timer (201)
- `GET /timers` — List all timers (supports `?state=` filter)
- `GET /timers/{id}` — Get timer
- `POST /timers/{id}/control` — Start/pause/resume/extend/stop (action is TimerAction enum)
- `DELETE /timers/{id}` — Delete timer (204, cleans up clients)
- `POST /timers/bulk/start|pause|resume|stop|extend` — Bulk operations (structured BulkResultItem responses)
- `WS /ws/{timer_id}` — Real-time updates

## Port
Default: 8003

## Integration
This service integrates with the **adaptivetest-platform** ecosystem:
- Used by Simple Quiz Engine for timed assessments
- Timer events can feed into Real-Time Event Pipeline
- Student Progress Tracker consumes timer completion events
