"""
Integration tests for bluesky_queueserver.manager.config_service.

Unlike ``test_config_service.py`` (which drives the client with an
``httpx.MockTransport`` that the test itself shapes), this module boots a
real ``configuration_service.main.create_app`` FastAPI instance in-process
and wires ``ConfigServiceClient`` to it via ``httpx.ASGITransport``. The
client therefore speaks the actual REST contract to the real handlers,
exercising route strings, status-code semantics, Pydantic request/response
shapes, and SQLite-backed audit-log behavior that unit mocks cannot catch.

The FastAPI app is constructed with ``load_strategy="empty"`` and a
per-test SQLite file under ``tmp_path`` so each test starts with a clean
registry. No network sockets are opened; no external config-service
process is required.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("configuration_service")

from configuration_service.config import Settings  # noqa: E402
from configuration_service.main import create_app  # noqa: E402

from bluesky_queueserver.manager.config_service import (  # noqa: E402
    ConfigServiceClient,
    ConfigServiceConflict,
    ConfigServiceHTTPError,
    ConfigServiceSettings,
    ConfigServiceState,
    build_staleness_plan,
    fetch_staleness_plan,
    sync_devices_on_env_open,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _cs_settings() -> ConfigServiceSettings:
    # http://testserver is the convention for ASGITransport; httpx uses it
    # only as base_url string — no DNS lookup happens.
    return ConfigServiceSettings(
        enabled=True,
        url="http://testserver",
        timeout=5.0,
        max_attempts=1,  # integration: no point retrying against in-proc ASGI
        backoff_ms=(1,),
        service_name="bluesky-queueserver-tests",
    )


@asynccontextmanager
async def _asgi_lifespan(app):
    """Drive ASGI lifespan startup/shutdown around a block.

    httpx.ASGITransport does not run the lifespan protocol; without it the
    configuration-service dependency ``get_state`` raises HTTP 503 because
    the app's startup handler is what populates ``state_container``. This
    context manager pumps the lifespan messages so the app is fully ready
    when tests issue requests and cleanly shut down afterward.
    """
    recv: asyncio.Queue = asyncio.Queue()
    sent: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await recv.get()

    async def send(message):
        await sent.put(message)

    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
    runner = asyncio.create_task(app(scope, receive, send))

    await recv.put({"type": "lifespan.startup"})
    msg = await sent.get()
    if msg["type"] != "lifespan.startup.complete":
        raise RuntimeError(f"ASGI lifespan startup failed: {msg!r}")

    try:
        yield
    finally:
        await recv.put({"type": "lifespan.shutdown"})
        msg = await sent.get()
        if msg["type"] != "lifespan.shutdown.complete":
            raise RuntimeError(f"ASGI lifespan shutdown failed: {msg!r}")
        await runner


@pytest.fixture
def cs_app(tmp_path: Path):
    """Fresh configuration-service FastAPI app backed by tmp_path SQLite."""
    settings = Settings(load_strategy="empty", db_path=tmp_path / "cs.db")
    return create_app(settings)


@pytest_asyncio.fixture
async def cs_client(cs_app) -> AsyncIterator[ConfigServiceClient]:
    """ConfigServiceClient wired to the in-process FastAPI app via ASGI."""
    async with _asgi_lifespan(cs_app):
        transport = httpx.ASGITransport(app=cs_app)
        client = ConfigServiceClient(_cs_settings(), transport=transport)
        try:
            yield client
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Payload helpers — shapes must match what device_introspection produces.
# ---------------------------------------------------------------------------


def _metadata(name: str, *, prefix: str = "XF:M1", label: str = "motor") -> Dict[str, Any]:
    return {
        "name": name,
        "device_label": label,
        "ophyd_class": "EpicsMotor",
        "module": "ophyd",
        "is_movable": True,
        "is_readable": True,
        "pvs": {"prefix": prefix},
        "labels": [],
    }


def _spec(name: str, *, prefix: str = "XF:M1") -> Dict[str, Any]:
    return {
        "name": name,
        "device_class": "ophyd.EpicsMotor",
        "args": [prefix],
        "kwargs": {"name": name},
        "active": True,
    }


def _device_data(name: str, *, prefix: str = "XF:M1") -> Dict[str, Dict[str, Any]]:
    """One entry of the ``device_data`` dict that ``sync_devices_on_env_open`` expects."""
    return {name: {"metadata": _metadata(name, prefix=prefix), "spec": _spec(name, prefix=prefix)}}


# ---------------------------------------------------------------------------
# Basic round-trips: POST / GET /devices-info / GET /devices/instantiation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_registry_reports_empty(cs_client: ConfigServiceClient):
    assert await cs_client.is_registry_empty() is True
    assert await cs_client.get_devices_info() == {}
    assert await cs_client.get_instantiation_specs() == {}


@pytest.mark.asyncio
async def test_upsert_creates_then_get_instantiation_specs_returns_it(
    cs_client: ConfigServiceClient,
):
    await cs_client.upsert_device(_metadata("m1"), _spec("m1"))

    info = await cs_client.get_devices_info()
    assert "m1" in info
    assert info["m1"]["ophyd_class"] == "EpicsMotor"

    specs = await cs_client.get_instantiation_specs()
    assert "m1" in specs
    assert specs["m1"]["device_class"] == "ophyd.EpicsMotor"
    assert specs["m1"]["args"] == ["XF:M1"]


@pytest.mark.asyncio
async def test_upsert_conflict_falls_back_to_put(cs_client: ConfigServiceClient):
    """Second upsert of the same name must round-trip POST-409 → PUT.

    The MockTransport unit test asserts the client retries on a shaped
    409; this asserts the real server actually responds with 409 for a
    duplicate POST and accepts the PUT fallback.
    """
    await cs_client.upsert_device(_metadata("m1", prefix="XF:A"), _spec("m1", prefix="XF:A"))

    # Sanity: raw POST would now 409.
    with pytest.raises(ConfigServiceConflict):
        await cs_client._request(  # type: ignore[attr-defined]
            "POST",
            "/api/v1/devices",
            json={"metadata": _metadata("m1", prefix="XF:B"), "instantiation_spec": _spec("m1", prefix="XF:B")},
            expected_status=(200, 201),
        )

    # But upsert_device handles it and PUTs the new args.
    await cs_client.upsert_device(_metadata("m1", prefix="XF:B"), _spec("m1", prefix="XF:B"))
    specs = await cs_client.get_instantiation_specs()
    assert specs["m1"]["args"] == ["XF:B"]


@pytest.mark.asyncio
async def test_delete_device_roundtrip(cs_client: ConfigServiceClient):
    await cs_client.upsert_device(_metadata("m1"), _spec("m1"))
    await cs_client.delete_device("m1")

    info = await cs_client.get_devices_info()
    assert "m1" not in info


# ---------------------------------------------------------------------------
# Bootstrap path: sync_devices_on_env_open against a real empty registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_bootstraps_empty_registry_and_captures_cursor(
    cs_client: ConfigServiceClient,
):
    device_data = {**_device_data("m1"), **_device_data("m2", prefix="XF:M2")}

    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1", "m2"],
        device_data=device_data,
    )

    assert isinstance(state, ConfigServiceState)
    assert state.cursor > 0  # audit log advanced
    assert state.epoch  # epoch assigned on first write

    info = await cs_client.get_devices_info()
    assert set(info.keys()) == {"m1", "m2"}


@pytest.mark.asyncio
async def test_sync_skips_bootstrap_when_registry_populated(
    cs_client: ConfigServiceClient,
):
    # Seed the registry with a different device so the sync path detects
    # populated-and-skip without touching POST.
    await cs_client.upsert_device(_metadata("seed"), _spec("seed"))

    device_data = _device_data("m1")
    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1"],
        device_data=device_data,
    )

    # m1 was NOT posted; only 'seed' is still in the registry.
    info = await cs_client.get_devices_info()
    assert "m1" not in info
    assert "seed" in info
    # Cursor should reflect the seed write.
    assert state.cursor > 0


# ---------------------------------------------------------------------------
# Lock / unlock endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_and_unlock_devices(cs_client: ConfigServiceClient):
    await cs_client.upsert_device(_metadata("m1"), _spec("m1"))
    await cs_client.upsert_device(_metadata("m2", prefix="XF:M2"), _spec("m2", prefix="XF:M2"))

    result = await cs_client.lock_devices(
        ["m1", "m2"], item_id="env:abc", plan_name="__environment__"
    )
    assert result["success"] is True
    assert set(result["locked_devices"]) == {"m1", "m2"}

    # Second lock attempt under a different item_id must conflict.
    with pytest.raises(ConfigServiceConflict):
        await cs_client.lock_devices(
            ["m1"], item_id="env:xyz", plan_name="__environment__"
        )

    # Unlock with the wrong owner should not succeed — the client surfaces
    # the HTTP 403 as a ConfigServiceHTTPError (not Conflict, not NotFound).
    with pytest.raises(ConfigServiceHTTPError) as exc_info:
        await cs_client.unlock_devices(["m1", "m2"], item_id="env:wrong")
    assert exc_info.value.status_code == 403

    # And the original owner can unlock both atomically.
    unlock_result = await cs_client.unlock_devices(["m1", "m2"], item_id="env:abc")
    assert unlock_result["success"] is True
    assert set(unlock_result["unlocked_devices"]) == {"m1", "m2"}


# ---------------------------------------------------------------------------
# get_changes_since + build_staleness_plan + fetch_staleness_plan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changes_since_noop_after_sync(cs_client: ConfigServiceClient):
    device_data = _device_data("m1")
    state = await sync_devices_on_env_open(
        cs_client, expected_device_names=["m1"], device_data=device_data
    )

    plan = await fetch_staleness_plan(cs_client, state)
    assert plan.is_noop
    assert plan.new_state.epoch == state.epoch
    assert plan.new_state.cursor == state.cursor


@pytest.mark.asyncio
async def test_changes_since_reports_upsert(cs_client: ConfigServiceClient):
    device_data = _device_data("m1")
    state = await sync_devices_on_env_open(
        cs_client, expected_device_names=["m1"], device_data=device_data
    )

    # Mutate after the cursor: add m2.
    await cs_client.upsert_device(_metadata("m2", prefix="XF:M2"), _spec("m2", prefix="XF:M2"))

    plan = await fetch_staleness_plan(cs_client, state)
    assert not plan.is_noop
    assert not plan.replace_overlay
    assert set(plan.upserts.keys()) == {"m2"}
    assert plan.upserts["m2"]["args"] == ["XF:M2"]
    assert plan.deletes == []
    assert plan.new_state.cursor > state.cursor
    assert plan.new_state.epoch == state.epoch


@pytest.mark.asyncio
async def test_changes_since_reports_delete(cs_client: ConfigServiceClient):
    device_data = {**_device_data("m1"), **_device_data("m2", prefix="XF:M2")}
    state = await sync_devices_on_env_open(
        cs_client, expected_device_names=["m1", "m2"], device_data=device_data
    )

    await cs_client.delete_device("m2")

    plan = await fetch_staleness_plan(cs_client, state)
    assert not plan.replace_overlay
    assert plan.upserts == {}
    assert plan.deletes == ["m2"]
    assert plan.new_state.cursor > state.cursor


@pytest.mark.asyncio
async def test_changes_since_reset_triggers_full_replace(
    cs_client: ConfigServiceClient,
):
    """After POST /registry/clear, the next /devices/changes must surface
    reset_occurred=True; fetch_staleness_plan must then also pull the full
    /devices/instantiation payload so the caller can atomically replace its
    overlay."""
    device_data = {**_device_data("m1"), **_device_data("m2", prefix="XF:M2")}
    state = await sync_devices_on_env_open(
        cs_client, expected_device_names=["m1", "m2"], device_data=device_data
    )

    # Wipe via the admin endpoint, then repopulate with a different device.
    wipe = await cs_client._request("POST", "/api/v1/registry/clear")  # type: ignore[attr-defined]
    assert wipe["success"] is True
    await cs_client.upsert_device(_metadata("m3", prefix="XF:M3"), _spec("m3", prefix="XF:M3"))

    plan = await fetch_staleness_plan(cs_client, state)
    assert plan.replace_overlay is True
    # Full refetch — upserts is the current registry, not just the delta.
    assert set(plan.upserts.keys()) == {"m3"}
    assert plan.new_state.cursor > state.cursor


# ---------------------------------------------------------------------------
# build_staleness_plan against real /devices/changes payloads (pure check)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_staleness_plan_on_real_response(
    cs_client: ConfigServiceClient,
):
    """Confirm the real server's DeviceChangesResponse shape is compatible
    with ``build_staleness_plan`` — i.e. the field names, types, and op
    values match. This catches contract drift between the two repos."""
    await cs_client.upsert_device(_metadata("m1"), _spec("m1"))
    initial = await cs_client.get_changes_since(0)

    # Drive the pure function with the real response; should see one upsert.
    plan = build_staleness_plan(initial, saved_epoch=initial["service_epoch"])
    assert plan.replace_overlay is False
    assert set(plan.upserts.keys()) == {"m1"}
    assert plan.deletes == []
    assert plan.new_state.epoch == initial["service_epoch"]


# ---------------------------------------------------------------------------
# Prefetched-info branch of sync (Layer 2.6 consume-mode)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_with_prefetched_empty_info_bootstraps_without_probe(
    cs_client: ConfigServiceClient,
):
    device_data = _device_data("m1")

    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1"],
        device_data=device_data,
        prefetched_info={},  # caller already knows the registry is empty
    )
    assert state.cursor > 0
    info = await cs_client.get_devices_info()
    assert "m1" in info


@pytest.mark.asyncio
async def test_sync_with_prefetched_populated_info_skips_bootstrap(
    cs_client: ConfigServiceClient,
):
    await cs_client.upsert_device(_metadata("seed"), _spec("seed"))

    device_data = _device_data("m1")
    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1"],
        device_data=device_data,
        prefetched_info={"seed": _metadata("seed")},  # already populated per caller
    )

    info = await cs_client.get_devices_info()
    # m1 was NOT posted because the caller reported non-empty registry.
    assert "m1" not in info
    assert state.cursor > 0
