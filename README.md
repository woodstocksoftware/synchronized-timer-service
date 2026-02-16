# Synchronized Timer Service

Server-authoritative timer system for proctored testing. All clients receive synchronized time from the server — no client-side manipulation possible.

![Python](https://img.shields.io/badge/Python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green)
![WebSocket](https://img.shields.io/badge/WebSocket-Real--time-orange)
![Tests](https://img.shields.io/badge/Tests-46_passing-brightgreen)
![Coverage](https://img.shields.io/badge/Coverage-82%25-yellow)
![License](https://img.shields.io/badge/License-MIT-blue)

## Why This Exists

Timed assessments need tamper-proof timing. Browser-side timers can be paused, manipulated, or desynchronized across students. This service keeps the clock on the server — every connected client sees the same time, updated in real-time via WebSocket.

Built for proctored testing scenarios where:
- All students must start and end at the same time
- Instructors need to pause/extend time for the whole class
- The timer must survive browser refreshes and network interruptions
- Client-side clock manipulation must be impossible

## Features

- **Server-authoritative** — Server is the single source of truth for time
- **Real-time sync** — WebSocket broadcasts to all connected clients every second
- **Full timer controls** — Start, pause, resume, extend, stop
- **Warning thresholds** — Configurable alerts (e.g., 5 min, 1 min, 30 sec remaining)
- **Bulk operations** — Start, pause, or extend multiple timers simultaneously
- **Sync on demand** — Clients can request current server time for drift correction
- **Anti-cheat** — Clients receive time, they cannot set it

## Architecture

```
                    ┌─────────────────────────┐
                    │   Timer Service (8003)   │
                    │                         │
                    │  ┌───────────────────┐  │
                    │  │  TimerManager     │  │
                    │  │  ┌─────┐ ┌─────┐  │  │
                    │  │  │Timer│ │Timer│  │  │
                    │  │  │(5m) │ │(30m)│  │  │
                    │  │  └──┬──┘ └──┬──┘  │  │
                    │  └─────┼───────┼─────┘  │
                    └────────┼───────┼────────┘
                  REST API   │  WS   │
              ┌──────────────┼───────┼──────────────┐
              │              │       │              │
              ▼              ▼       ▼              ▼
        ┌──────────┐   ┌──────────┐  ┌──────────┐  ┌──────────┐
        │ Instructor│   │Student 1 │  │Student 2 │  │Student N │
        │ (control) │   │(display) │  │(display) │  │(display) │
        └──────────┘   └──────────┘  └──────────┘  └──────────┘
```

**How it works:**
1. Instructor creates a timer via REST API
2. Students connect to the timer via WebSocket
3. Timer ticks on the server (1-second intervals via `asyncio.Task`)
4. Every tick broadcasts `remaining_seconds` to all connected clients
5. Warning thresholds trigger special messages at configured times
6. Timer expiry broadcasts `timer_expired` — client apps auto-submit

## Quick Start

```bash
# Clone and setup
git clone https://github.com/woodstocksoftware/synchronized-timer-service.git
cd synchronized-timer-service

# Create virtual environment
python3.12 -m venv venv
source venv/bin/activate

# Install
pip install -r requirements.txt

# Run
python -m uvicorn src.server:app --reload --port 8003
```

API docs available at http://localhost:8003/docs (Swagger UI).

## API Reference

### REST Endpoints

#### Create Timer
```http
POST /timers
```
```json
{
  "duration_seconds": 300,
  "name": "Quiz Timer",
  "warning_thresholds": [60, 30, 10],
  "auto_start": false
}
```

#### List All Timers
```http
GET /timers
```

#### Get Timer Status
```http
GET /timers/{timer_id}
```

**Response:**
```json
{
  "id": "timer_a1b2c3d4",
  "name": "Quiz Timer",
  "state": "running",
  "duration_seconds": 300,
  "remaining_seconds": 245,
  "elapsed_seconds": 55,
  "warning_thresholds": [60, 30, 10],
  "connected_clients": 24,
  "created_at": "2025-01-15T10:00:00Z",
  "started_at": "2025-01-15T10:00:05Z",
  "completed_at": null
}
```

#### Control Timer
```http
POST /timers/{timer_id}/control
```

| Action | Body | Description |
|--------|------|-------------|
| `start` | `{"action": "start"}` | Start the timer |
| `pause` | `{"action": "pause"}` | Pause the timer |
| `resume` | `{"action": "resume"}` | Resume from pause |
| `extend` | `{"action": "extend", "extend_seconds": 300}` | Add time |
| `stop` | `{"action": "stop"}` | Stop (manual completion) |

#### Delete Timer
```http
DELETE /timers/{timer_id}
```

#### Bulk Operations

```http
POST /timers/bulk/start    # Body: ["timer_id1", "timer_id2"]
POST /timers/bulk/pause    # Body: ["timer_id1", "timer_id2"]
POST /timers/bulk/extend?seconds=300  # Body: ["timer_id1", "timer_id2"]
```

### WebSocket

```
ws://localhost:8003/ws/{timer_id}
```

#### Server Messages

| Type | Fields | When |
|------|--------|------|
| `connected` | `timer` (full state) | On connection |
| `timer_started` | `remaining_seconds` | Timer starts |
| `timer_tick` | `remaining_seconds`, `elapsed_seconds` | Every second |
| `timer_warning` | `remaining_seconds`, `threshold` | Threshold reached |
| `timer_paused` | `remaining_seconds` | Timer paused |
| `timer_extended` | `added_seconds`, `remaining_seconds` | Time added |
| `timer_stopped` | `remaining_seconds`, `elapsed_seconds` | Manually stopped |
| `timer_expired` | `elapsed_seconds` | Reached zero |
| `keepalive` | `remaining_seconds` | Every 30s of inactivity |

#### Client Messages

| Type | Description |
|------|-------------|
| `ping` | Heartbeat (server responds with `pong`) |
| `sync` | Request current server time and timer state |

### Timer States

```
CREATED ──► RUNNING ──► EXPIRED
               │
               ├──► PAUSED ──► RUNNING (resume)
               │
               └──► COMPLETED (manual stop)
```

## Usage Examples

### Create and Start a Timer
```bash
# Create a 5-minute quiz timer
TIMER=$(curl -s -X POST http://localhost:8003/timers \
  -H "Content-Type: application/json" \
  -d '{"duration_seconds": 300, "name": "Quiz Timer", "warning_thresholds": [60, 30, 10]}')

TIMER_ID=$(echo $TIMER | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Start it
curl -X POST http://localhost:8003/timers/$TIMER_ID/control \
  -H "Content-Type: application/json" \
  -d '{"action": "start"}'
```

### Connect via WebSocket (JavaScript)
```javascript
const ws = new WebSocket('ws://localhost:8003/ws/timer_a1b2c3d4');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);

  switch (data.type) {
    case 'timer_tick':
      updateDisplay(data.remaining_seconds);
      break;
    case 'timer_warning':
      showWarning(`${data.remaining_seconds} seconds remaining!`);
      break;
    case 'timer_expired':
      autoSubmitAssessment();
      break;
    case 'timer_paused':
      showPausedOverlay();
      break;
  }
};

// Request sync if client suspects drift
ws.send(JSON.stringify({ type: 'sync' }));
```

### Connect via WebSocket (Python)
```python
import asyncio
import websockets
import json

async def connect_timer(timer_id: str):
    async with websockets.connect(f"ws://localhost:8003/ws/{timer_id}") as ws:
        async for message in ws:
            data = json.loads(message)
            if data["type"] == "timer_tick":
                print(f"Time remaining: {data['remaining_seconds']}s")
            elif data["type"] == "timer_expired":
                print("Time's up!")
                break

asyncio.run(connect_timer("timer_a1b2c3d4"))
```

### Classroom Scenario — Bulk Start
```bash
# Create timers for each student
T1=$(curl -s -X POST http://localhost:8003/timers -H "Content-Type: application/json" \
  -d '{"duration_seconds": 1800, "name": "Student A"}' | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

T2=$(curl -s -X POST http://localhost:8003/timers -H "Content-Type: application/json" \
  -d '{"duration_seconds": 1800, "name": "Student B"}' | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Start all at once
curl -X POST http://localhost:8003/timers/bulk/start \
  -H "Content-Type: application/json" \
  -d "[\"$T1\", \"$T2\"]"

# Give everyone 5 extra minutes
curl -X POST "http://localhost:8003/timers/bulk/extend?seconds=300" \
  -H "Content-Type: application/json" \
  -d "[\"$T1\", \"$T2\"]"
```

## Testing

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=src --cov-report=term-missing

# Lint
ruff check src/ tests/
ruff format --check src/ tests/
```

**Current:** 46 tests passing, 82% coverage.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| Port | `8003` | Uvicorn listen port |
| CORS | `*` (all origins) | Cross-origin policy |
| Max duration | 86,400s (24h) | Maximum timer length |
| Max extension | 3,600s (1h) | Maximum time extension |
| Keepalive interval | 30s | WebSocket keepalive frequency |
| Default warnings | 300s, 60s, 30s | Warning threshold defaults |

## Part of the AdaptiveTest Suite

| Component | Description |
|-----------|-------------|
| [Question Bank MCP](https://github.com/woodstocksoftware/question-bank-mcp) | Question management |
| [Student Progress Tracker](https://github.com/woodstocksoftware/student-progress-tracker) | Performance analytics |
| [Simple Quiz Engine](https://github.com/woodstocksoftware/simple-quiz-engine) | Real-time quizzes |
| [Learning Curriculum Builder](https://github.com/woodstocksoftware/learning-curriculum-builder) | Curriculum design |
| [Real-Time Event Pipeline](https://github.com/woodstocksoftware/realtime-event-pipeline) | Event routing |
| [Adaptive Question Selector](https://github.com/woodstocksoftware/adaptive-question-selector) | IRT adaptation |
| **Synchronized Timer Service** | **Server-authoritative timing** |

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Ensure tests pass (`pytest`) and lint is clean (`ruff check .`)
4. Submit a pull request

## License

MIT

---

Built by [Jim Williams](https://linkedin.com/in/woodstocksoftware) | [GitHub](https://github.com/woodstocksoftware)
