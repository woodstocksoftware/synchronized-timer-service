"""Tests for the Synchronized Timer Service."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from src.server import Timer, TimerManager, TimerState, app, manager

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

    async def test_timer_expires(self):
        timer = Timer("test_1", duration_seconds=2)
        await timer.start()
        await asyncio.sleep(3)
        assert timer.state == TimerState.EXPIRED
        assert timer.remaining_seconds == 0
        assert timer.completed_at is not None


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


# ============================================================
# API ENDPOINT TESTS
# ============================================================


class TestCreateTimerAPI:
    async def test_create_timer(self, client):
        resp = await client.post(
            "/timers",
            json={
                "duration_seconds": 300,
                "name": "Quiz Timer",
            },
        )
        assert resp.status_code == 200
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
        assert resp.status_code == 200
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
        assert resp.status_code == 400

    async def test_invalid_state_transition(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        resp = await client.post(f"/timers/{timer_id}/control", json={"action": "pause"})
        assert resp.status_code == 400

    async def test_control_nonexistent(self, client):
        resp = await client.post("/timers/nonexistent/control", json={"action": "start"})
        assert resp.status_code == 404


class TestDeleteTimerAPI:
    async def test_delete_timer(self, client):
        create_resp = await client.post("/timers", json={"duration_seconds": 300})
        timer_id = create_resp.json()["id"]
        resp = await client.delete(f"/timers/{timer_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

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
        assert resp.status_code == 200

    async def test_delete_nonexistent(self, client):
        resp = await client.delete("/timers/nonexistent")
        assert resp.status_code == 404


class TestBulkOperationsAPI:
    async def test_bulk_start(self, client):
        r1 = await client.post("/timers", json={"duration_seconds": 300})
        r2 = await client.post("/timers", json={"duration_seconds": 300})
        ids = [r1.json()["id"], r2.json()["id"]]
        resp = await client.post("/timers/bulk/start", json=ids)
        assert resp.status_code == 200
        for tid in ids:
            assert resp.json()[tid] == "started"
            timer = manager.get_timer(tid)
            if timer and timer._task:
                timer._task.cancel()

    async def test_bulk_pause(self, client):
        r1 = await client.post("/timers", json={"duration_seconds": 300})
        tid = r1.json()["id"]
        await client.post(f"/timers/{tid}/control", json={"action": "start"})
        resp = await client.post("/timers/bulk/pause", json=[tid])
        assert resp.status_code == 200
        assert resp.json()[tid] == "paused"

    async def test_bulk_extend(self, client):
        r1 = await client.post("/timers", json={"duration_seconds": 300})
        tid = r1.json()["id"]
        resp = await client.post("/timers/bulk/extend?seconds=60", json=[tid])
        assert resp.status_code == 200

    async def test_bulk_start_nonexistent(self, client):
        resp = await client.post("/timers/bulk/start", json=["nonexistent"])
        assert resp.status_code == 200
        assert resp.json()["nonexistent"] == "not found"
