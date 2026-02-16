"""Tests for the Synchronized Timer Service."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from src.server import (
    MAX_TIMERS,
    Timer,
    TimerManager,
    TimerState,
    app,
    manager,
)

# ============================================================
# FIXTURES
# ============================================================


@pytest.fixture(autouse=True)
def _clear_timers():
    """Clear all timers before each test."""
    manager.timers.clear()
    yield
    # Stop any running timers
    for timer in manager.timers.values():
        if timer._task and not timer._task.done():
            timer._task.cancel()
    manager.timers.clear()


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ============================================================
# TIMER UNIT TESTS
# ============================================================


class TestTimer:
    def test_create_timer(self):
        timer = Timer("test_1", duration_seconds=300, name="Quiz")
        assert timer.id == "test_1"
        assert timer.name == "Quiz"
        assert timer.duration_seconds == 300
        assert timer.remaining_seconds == 300
        assert timer.state == TimerState.CREATED
        assert timer.elapsed_seconds == 0

    def test_default_warning_thresholds(self):
        timer = Timer("test_1", duration_seconds=300)
        assert timer.warning_thresholds == [300, 60, 30]

    def test_custom_warning_thresholds(self):
        timer = Timer("test_1", duration_seconds=300, warning_thresholds=[120, 60])
        assert timer.warning_thresholds == [120, 60]

    def test_elapsed_seconds(self):
        timer = Timer("test_1", duration_seconds=300)
        timer.remaining_seconds = 200
        assert timer.elapsed_seconds == 100

    def test_to_response(self):
        timer = Timer("test_1", duration_seconds=300, name="Quiz")
        resp = timer.to_response()
        assert resp.id == "test_1"
        assert resp.name == "Quiz"
        assert resp.state == TimerState.CREATED
        assert resp.duration_seconds == 300
        assert resp.remaining_seconds == 300
        assert resp.connected_clients == 0
        assert resp.started_at is None
        assert resp.completed_at is None

    def test_timestamp_format(self):
        timer = Timer("test_1", duration_seconds=300)
        resp = timer.to_response()
        # Should be proper ISO 8601 with Z suffix, no +00:00
        assert resp.created_at.endswith("Z")
        assert "+00:00" not in resp.created_at

    def test_has_lock(self):
        timer = Timer("test_1", duration_seconds=300)
        assert timer._lock is not None


class TestTimerStateTransitions:
    async def test_start_from_created(self):
        timer = Timer("test_1", duration_seconds=5)
        result = await timer.start()
        assert result is True
        assert timer.state == TimerState.RUNNING
        assert timer.started_at is not None
        if timer._task:
            timer._task.cancel()

    async def test_start_from_running_fails(self):
        timer = Timer("test_1", duration_seconds=5)
        await timer.start()
        result = await timer.start()
        assert result is False
        if timer._task:
            timer._task.cancel()

    async def test_pause_running_timer(self):
        timer = Timer("test_1", duration_seconds=5)
        await timer.start()
        result = await timer.pause()
        assert result is True
        assert timer.state == TimerState.PAUSED
        assert timer.paused_at is not None

    async def test_pause_non_running_fails(self):
        timer = Timer("test_1", duration_seconds=5)
        result = await timer.pause()
        assert result is False

    async def test_resume_paused_timer(self):
        timer = Timer("test_1", duration_seconds=5)
        await timer.start()
        await timer.pause()
        result = await timer.resume()
        assert result is True
        assert timer.state == TimerState.RUNNING
        if timer._task:
            timer._task.cancel()

    async def test_resume_non_paused_fails(self):
        timer = Timer("test_1", duration_seconds=5)
        result = await timer.resume()
        assert result is False

    async def test_stop_running_timer(self):
        timer = Timer("test_1", duration_seconds=5)
        await timer.start()
        result = await timer.stop()
        assert result is True
        assert timer.state == TimerState.COMPLETED
        assert timer.completed_at is not None

    async def test_stop_created_timer(self):
        timer = Timer("test_1", duration_seconds=5)
        result = await timer.stop()
        assert result is True
        assert timer.state == TimerState.COMPLETED

    async def test_stop_completed_timer_fails(self):
        timer = Timer("test_1", duration_seconds=5)
        await timer.stop()
        result = await timer.stop()
        assert result is False

    async def test_extend_timer(self):
        timer = Timer("test_1", duration_seconds=300)
        result = await timer.extend(60)
        assert result is True
        assert timer.remaining_seconds == 360
        assert timer.duration_seconds == 360

    async def test_extend_completed_timer_fails(self):
        timer = Timer("test_1", duration_seconds=300)
        await timer.stop()
        result = await timer.extend(60)
        assert result is False
        assert timer.remaining_seconds == 300

    async def test_extend_expired_timer_fails(self):
        timer = Timer("test_1", duration_seconds=2)
        await timer.start()
        await asyncio.sleep(3)
        assert timer.state == TimerState.EXPIRED
        result = await timer.extend(60)
        assert result is False

    async def test_timer_expires(self):
        timer = Timer("test_1", duration_seconds=2)
        await timer.start()
        await asyncio.sleep(3)
        assert timer.state == TimerState.EXPIRED
        assert timer.remaining_seconds == 0
        assert timer.completed_at is not None

    async def test_cleanup(self):
        timer = Timer("test_1", duration_seconds=300)
        await timer.start()
        assert timer._task is not None
        await timer.cleanup()
        assert timer._task is None
        assert len(timer.clients) == 0


# ============================================================
# TIMER MANAGER TESTS
# ============================================================


class TestTimerManager:
    def test_create_timer(self):
        mgr = TimerManager()
        timer = mgr.create_timer(duration_seconds=300, name="Test")
        assert timer.id.startswith("timer_")
        assert timer.duration_seconds == 300
        assert timer.name == "Test"

    def test_get_timer(self):
        mgr = TimerManager()
        timer = mgr.create_timer(duration_seconds=300)
        found = mgr.get_timer(timer.id)
        assert found is timer

    def test_get_nonexistent_timer(self):
        mgr = TimerManager()
        assert mgr.get_timer("nonexistent") is None

    def test_delete_timer(self):
        mgr = TimerManager()
        timer = mgr.create_timer(duration_seconds=300)
        assert mgr.delete_timer(timer.id) is True
        assert mgr.get_timer(timer.id) is None

    def test_delete_nonexistent(self):
        mgr = TimerManager()
        assert mgr.delete_timer("nonexistent") is False

    def test_list_timers(self):
        mgr = TimerManager()
        mgr.create_timer(duration_seconds=300, name="A")
        mgr.create_timer(duration_seconds=600, name="B")
        timers = mgr.list_timers()
        assert len(timers) == 2

    def test_max_timers_limit(self):
        mgr = TimerManager()
        for i in range(MAX_TIMERS):
            mgr.create_timer(duration_seconds=60, name=f"Timer {i}")
        with pytest.raises(ValueError, match="Maximum timer limit"):
            mgr.create_timer(duration_seconds=60)


# ============================================================
# API ENDPOINT TESTS
# ============================================================


class TestHealthCheck:
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["timers_active"] == 0

    async def test_health_with_timers(self, client):
        await client.post("/timers", json={"duration_seconds": 300})
        resp = await client.get("/health")
        assert resp.json()["timers_active"] == 1


class TestCreateTimerAPI:
    async def test_create_timer(self, client):
        resp = await client.post(
            "/timers",
            json={
                "duration_seconds": 300,
                "name": "Quiz Timer",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Quiz Timer"
        assert data["duration_seconds"] == 300
        assert data["state"] == "created"

    async def test_create_timer_auto_start(self, client):
        resp = await client.post(
            "/timers",
            json={
                "duration_seconds": 300,
                "auto_start": True,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["state"] == "running"
        # Clean up
        timer = manager.get_timer(data["id"])
        if timer and timer._task:
            timer._task.cancel()

    async def test_create_timer_invalid_duration(self, client):
        resp = await client.post("/timers", json={"duration_seconds": 0})
        assert resp.status_code == 422

    async def test_create_timer_too_long(self, client):
        resp = await client.post("/timers", json={"duration_seconds": 100000})
        assert resp.status_code == 422

    async def test_create_timer_name_too_long(self, client):
        resp = await client.post(
            "/timers",
            json={
                "duration_seconds": 300,
                "name": "A" * 201,
            },
        )
        assert resp.status_code == 422

    async def test_create_timer_empty_name(self, client):
        resp = await client.post(
            "/timers",
            json={
                "duration_seconds": 300,
                "name": "",
            },
        )
        assert resp.status_code == 422

    async def test_create_timer_invalid_threshold(self, client):
        resp = await client.post(
            "/timers",
            json={
                "duration_seconds": 300,
                "warning_thresholds": [0],
            },
        )
        assert resp.status_code == 422

    async def test_create_timer_too_many_thresholds(self, client):
        resp = await client.post(
            "/timers",
            json={
                "duration_seconds": 300,
                "warning_thresholds": list(range(1, 22)),
            },
        )
        assert resp.status_code == 422

    async def test_create_timer_timestamp_format(self, client):
        resp = await client.post("/timers", json={"duration_seconds": 300})
        data = resp.json()
        assert data["created_at"].endswith("Z")
        assert "+00:00" not in data["created_at"]

    async def test_max_timers_returns_429(self, client):
        # Fill up to limit
        for i in range(MAX_TIMERS):
            manager.create_timer(duration_seconds=60, name=f"T{i}")
        resp = await client.post("/timers", json={"duration_seconds": 300})
        assert resp.status_code == 429


class TestListTimersAPI:
    async def test_list_empty(self, client):
        resp = await client.get("/timers")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_timers(self, client):
        await client.post("/timers", json={"duration_seconds": 300})
        await client.post("/timers", json={"duration_seconds": 600})
        resp = await client.get("/timers")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_list_timers_filter_by_state(self, client):
        r1 = await client.post("/timers", json={"duration_seconds": 300})
        r2 = await client.post("/timers", json={"duration_seconds": 300, "auto_start": True})
        tid2 = r2.json()["id"]

        # Filter for running only
        resp = await client.get("/timers?state=running")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == tid2

        # Filter for created only
        resp = await client.get("/timers?state=created")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["id"] == r1.json()["id"]

        # Clean up
        timer = manager.get_timer(tid2)
        if timer and timer._task:
            timer._task.cancel()


class TestGetTimerAPI:
    async def test_get_timer(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        resp = await client.get(f"/timers/{timer_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == timer_id

    async def test_get_nonexistent(self, client):
        resp = await client.get("/timers/nonexistent")
        assert resp.status_code == 404


class TestControlTimerAPI:
    async def test_start_timer(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        resp = await client.post(f"/timers/{timer_id}/control", json={"action": "start"})
        assert resp.status_code == 200
        assert resp.json()["state"] == "running"
        timer = manager.get_timer(timer_id)
        if timer and timer._task:
            timer._task.cancel()

    async def test_pause_timer(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        await client.post(f"/timers/{timer_id}/control", json={"action": "start"})
        resp = await client.post(f"/timers/{timer_id}/control", json={"action": "pause"})
        assert resp.status_code == 200
        assert resp.json()["state"] == "paused"

    async def test_resume_timer(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        await client.post(f"/timers/{timer_id}/control", json={"action": "start"})
        await client.post(f"/timers/{timer_id}/control", json={"action": "pause"})
        resp = await client.post(f"/timers/{timer_id}/control", json={"action": "resume"})
        assert resp.status_code == 200
        assert resp.json()["state"] == "running"
        timer = manager.get_timer(timer_id)
        if timer and timer._task:
            timer._task.cancel()

    async def test_extend_timer(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        resp = await client.post(
            f"/timers/{timer_id}/control",
            json={"action": "extend", "extend_seconds": 60},
        )
        assert resp.status_code == 200
        assert resp.json()["remaining_seconds"] == 360

    async def test_extend_without_seconds(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        resp = await client.post(f"/timers/{timer_id}/control", json={"action": "extend"})
        assert resp.status_code == 400

    async def test_extend_completed_fails(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        await client.post(f"/timers/{timer_id}/control", json={"action": "stop"})
        resp = await client.post(
            f"/timers/{timer_id}/control",
            json={"action": "extend", "extend_seconds": 60},
        )
        assert resp.status_code == 400

    async def test_stop_timer(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        await client.post(f"/timers/{timer_id}/control", json={"action": "start"})
        resp = await client.post(f"/timers/{timer_id}/control", json={"action": "stop"})
        assert resp.status_code == 200
        assert resp.json()["state"] == "completed"

    async def test_invalid_action(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        resp = await client.post(f"/timers/{timer_id}/control", json={"action": "invalid"})
        assert resp.status_code == 422  # Pydantic validates enum

    async def test_invalid_state_transition(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        resp = await client.post(f"/timers/{timer_id}/control", json={"action": "pause"})
        assert resp.status_code == 400
        # Error should mention valid actions
        assert "Valid actions" in resp.json()["detail"]

    async def test_control_nonexistent(self, client):
        resp = await client.post("/timers/nonexistent/control", json={"action": "start"})
        assert resp.status_code == 404


class TestDeleteTimerAPI:
    async def test_delete_timer(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        resp = await client.delete(f"/timers/{timer_id}")
        assert resp.status_code == 204

    async def test_delete_running_timer(self, client):
        create_resp = await client.post(
            "/timers",
            json={
                "duration_seconds": 300,
                "auto_start": True,
            },
        )
        timer_id = create_resp.json()["id"]
        resp = await client.delete(f"/timers/{timer_id}")
        assert resp.status_code == 204

    async def test_delete_nonexistent(self, client):
        resp = await client.delete("/timers/nonexistent")
        assert resp.status_code == 404


class TestBulkOperationsAPI:
    async def test_bulk_start(self, client):
        r1 = await client.post("/timers", json={"duration_seconds": 300})
        r2 = await client.post("/timers", json={"duration_seconds": 300})
        ids = [r1.json()["id"], r2.json()["id"]]
        resp = await client.post("/timers/bulk/start", json={"timer_ids": ids})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert all(item["success"] for item in data)
        for tid in ids:
            timer = manager.get_timer(tid)
            if timer and timer._task:
                timer._task.cancel()

    async def test_bulk_pause(self, client):
        r1 = await client.post("/timers", json={"duration_seconds": 300})
        tid = r1.json()["id"]
        await client.post(f"/timers/{tid}/control", json={"action": "start"})
        resp = await client.post("/timers/bulk/pause", json={"timer_ids": [tid]})
        assert resp.status_code == 200
        assert resp.json()[0]["success"] is True

    async def test_bulk_resume(self, client):
        r1 = await client.post("/timers", json={"duration_seconds": 300})
        tid = r1.json()["id"]
        await client.post(f"/timers/{tid}/control", json={"action": "start"})
        await client.post(f"/timers/{tid}/control", json={"action": "pause"})
        resp = await client.post("/timers/bulk/resume", json={"timer_ids": [tid]})
        assert resp.status_code == 200
        assert resp.json()[0]["success"] is True
        timer = manager.get_timer(tid)
        if timer and timer._task:
            timer._task.cancel()

    async def test_bulk_stop(self, client):
        r1 = await client.post("/timers", json={"duration_seconds": 300})
        tid = r1.json()["id"]
        await client.post(f"/timers/{tid}/control", json={"action": "start"})
        resp = await client.post("/timers/bulk/stop", json={"timer_ids": [tid]})
        assert resp.status_code == 200
        assert resp.json()[0]["success"] is True
        assert resp.json()[0]["status"] == "stopped"

    async def test_bulk_extend(self, client):
        r1 = await client.post("/timers", json={"duration_seconds": 300})
        tid = r1.json()["id"]
        resp = await client.post("/timers/bulk/extend", json={"timer_ids": [tid], "seconds": 60})
        assert resp.status_code == 200
        assert resp.json()[0]["success"] is True

    async def test_bulk_extend_completed_fails(self, client):
        r1 = await client.post("/timers", json={"duration_seconds": 300})
        tid = r1.json()["id"]
        await client.post(f"/timers/{tid}/control", json={"action": "stop"})
        resp = await client.post("/timers/bulk/extend", json={"timer_ids": [tid], "seconds": 60})
        assert resp.status_code == 200
        assert resp.json()[0]["success"] is False

    async def test_bulk_start_nonexistent(self, client):
        resp = await client.post("/timers/bulk/start", json={"timer_ids": ["nonexistent"]})
        assert resp.status_code == 200
        assert resp.json()[0]["success"] is False
        assert resp.json()[0]["status"] == "not found"

    async def test_bulk_response_structure(self, client):
        r1 = await client.post("/timers", json={"duration_seconds": 300})
        tid = r1.json()["id"]
        resp = await client.post("/timers/bulk/start", json={"timer_ids": [tid]})
        item = resp.json()[0]
        assert "timer_id" in item
        assert "success" in item
        assert "status" in item
        timer = manager.get_timer(tid)
        if timer and timer._task:
            timer._task.cancel()
