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
Single-file service (`src/server.py`, ~520 lines) with four core components:
- **Pydantic Models** — `TimerCreate`, `TimerUpdate`, `TimerResponse`, `TimerState` enum
- **Timer class** — Individual timer with async tick loop, WebSocket client management, broadcast
- **TimerManager class** — In-memory registry of all active timers
- **FastAPI app** — REST endpoints + WebSocket endpoint + bulk operations

Key design: server-authoritative timing (clients cannot manipulate time). Each timer runs its own `asyncio.Task` ticking every second. All connected WebSocket clients receive identical broadcasts.

## Running
```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
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
- **State machine:** CREATED -> RUNNING -> PAUSED/COMPLETED/EXPIRED (enforced in Timer methods)
- **Broadcast pattern:** Timer changes pushed to all WebSocket clients simultaneously
- **Keepalive:** 30-second WebSocket timeout with keepalive messages
- **Sync request:** Clients can send `{"type": "sync"}` to get server time + timer state
- **Warning thresholds:** Configurable, deduplicated via `warnings_sent` set
- **Bulk operations:** Classroom scenarios — start/pause/extend multiple timers at once

## API Endpoints
- `POST /timers` — Create timer
- `GET /timers` — List all timers
- `GET /timers/{id}` — Get timer
- `POST /timers/{id}/control` — Start/pause/resume/extend/stop
- `DELETE /timers/{id}` — Delete timer
- `POST /timers/bulk/start|pause|extend` — Bulk operations
- `WS /ws/{timer_id}` — Real-time updates

## Port
Default: 8003

## Integration
This service integrates with the **adaptivetest-platform** ecosystem:
- Used by Simple Quiz Engine for timed assessments
- Timer events can feed into Real-Time Event Pipeline
- Student Progress Tracker consumes timer completion events
