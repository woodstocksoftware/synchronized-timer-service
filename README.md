# Synchronized Timer Service

Server-authoritative timer system for proctored testing. All clients receive synchronized time from the server.

![Python](https://img.shields.io/badge/Python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109-green)
![WebSocket](https://img.shields.io/badge/WebSocket-Real--time-orange)

## Features

- **Server-authoritative** - Server is single source of truth
- **Real-time sync** - WebSocket broadcast to all clients
- **Timer controls** - Start, pause, resume, extend, stop
- **Warning thresholds** - Alerts at configurable times
- **Bulk operations** - Control multiple timers at once
- **Anti-cheat** - Clients cannot manipulate time

## Architecture
```
                    ┌─────────────────────┐
                    │   Timer Service     │
                    │   (Server Clock)    │
                    └──────────┬──────────┘
                               │
            ┌──────────────────┼──────────────────┐
            │                  │                  │
            ▼                  ▼                  ▼
     ┌──────────┐       ┌──────────┐       ┌──────────┐
     │ Student 1│       │ Student 2│       │ Teacher  │
     └──────────┘       └──────────┘       └──────────┘
```

## Quick Start
```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m uvicorn src.server:app --reload --port 8003
```

## API Endpoints

### REST

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/timers` | POST | Create timer |
| `/timers` | GET | List all timers |
| `/timers/{id}` | GET | Get timer status |
| `/timers/{id}/control` | POST | Control timer |
| `/timers/{id}` | DELETE | Delete timer |
| `/timers/bulk/start` | POST | Start multiple |
| `/timers/bulk/pause` | POST | Pause multiple |
| `/timers/bulk/extend` | POST | Extend multiple |

### WebSocket

| Endpoint | Description |
|----------|-------------|
| `/ws/{timer_id}` | Real-time timer updates |

## WebSocket Messages

| Type | Description |
|------|-------------|
| `connected` | Initial state on connect |
| `timer_started` | Timer started |
| `timer_tick` | Every second update |
| `timer_warning` | Warning threshold reached |
| `timer_paused` | Timer paused |
| `timer_extended` | Time added |
| `timer_stopped` | Manually stopped |
| `timer_expired` | Time reached zero |

## Usage Example

### Create Timer
```bash
curl -X POST http://localhost:8003/timers \
  -H "Content-Type: application/json" \
  -d '{
    "duration_seconds": 300,
    "name": "Quiz Timer",
    "warning_thresholds": [60, 30, 10]
  }'
```

### Connect WebSocket
```javascript
const ws = new WebSocket('ws://localhost:8003/ws/timer_abc123');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  
  if (data.type === 'timer_tick') {
    updateDisplay(data.remaining_seconds);
  } else if (data.type === 'timer_warning') {
    showWarning(data.remaining_seconds);
  } else if (data.type === 'timer_expired') {
    submitQuiz();
  }
};
```

### Control Timer
```bash
# Start
curl -X POST http://localhost:8003/timers/{id}/control \
  -d '{"action": "start"}'

# Pause
curl -X POST http://localhost:8003/timers/{id}/control \
  -d '{"action": "pause"}'

# Extend by 5 minutes
curl -X POST http://localhost:8003/timers/{id}/control \
  -d '{"action": "extend", "extend_seconds": 300}'
```

## Timer States

| State | Description |
|-------|-------------|
| `created` | Timer created, not started |
| `running` | Timer actively counting down |
| `paused` | Timer paused |
| `completed` | Manually stopped |
| `expired` | Reached zero |

## Part of Ed-Tech Suite

| Component | Repository |
|-----------|------------|
| [Question Bank MCP](https://github.com/woodstocksoftware/question-bank-mcp) | Question management |
| [Student Progress Tracker](https://github.com/woodstocksoftware/student-progress-tracker) | Performance analytics |
| [Simple Quiz Engine](https://github.com/woodstocksoftware/simple-quiz-engine) | Real-time quizzes |
| [Learning Curriculum Builder](https://github.com/woodstocksoftware/learning-curriculum-builder) | Curriculum design |
| [Real-Time Event Pipeline](https://github.com/woodstocksoftware/realtime-event-pipeline) | Event routing |
| [Adaptive Question Selector](https://github.com/woodstocksoftware/adaptive-question-selector) | IRT adaptation |
| **Synchronized Timer Service** | Server-authoritative timing |

## License

MIT

---

Built by [Jim Williams](https://linkedin.com/in/woodstocksoftware) | [GitHub](https://github.com/woodstocksoftware)
