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
registry. Device payloads are produced by the real worker-side helper
``build_config_service_payload`` against ``FakeOphydMotor`` fixtures from
``test_device_introspection`` — that way this module also pins the
introspection → config-service payload contract.

No network sockets are opened; no external config-service process is
required.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator, Dict

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("configuration_service")
pytest.importorskip("asgi_lifespan")

from asgi_lifespan import LifespanManager  # noqa: E402
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
from bluesky_queueserver.manager.device_introspection import (  # noqa: E402
    build_config_service_payload,
)
from bluesky_queueserver.manager.tests.test_device_introspection import (  # noqa: E402
    FakeOphydMotor,
)


def _cs_settings() -> ConfigServiceSettings:
    return ConfigServiceSettings(
        enabled=True,
        url="http://testserver",
        timeout=5.0,
        max_attempts=1,
        backoff_ms=(1,),
        service_name="bluesky-queueserver-tests",
    )


def _payload(name: str, prefix: str = "XF:M1") -> Dict[str, Dict[str, Any]]:
    """``{name: {"metadata": ..., "spec": ...}}`` for a single fake device,
    produced by the real introspection pipeline so payload shapes stay in
    lockstep with what the worker sends in production."""
    return build_config_service_payload({name: FakeOphydMotor(prefix=prefix, name=name)})


def _payloads(*names_and_prefixes) -> Dict[str, Dict[str, Any]]:
    devices = {
        name: FakeOphydMotor(prefix=prefix, name=name)
        for name, prefix in names_and_prefixes
    }
    return build_config_service_payload(devices)


@pytest.fixture
def cs_app(tmp_path: Path):
    """Fresh configuration-service FastAPI app backed by tmp_path SQLite."""
    settings = Settings(
        load_strategy="empty",
        database_url=f"sqlite+pysqlite:///{tmp_path / 'cs.db'}",
    )
    return create_app(settings)


@pytest_asyncio.fixture
async def cs_client(cs_app) -> AsyncIterator[ConfigServiceClient]:
    """ConfigServiceClient wired to the in-process FastAPI app via ASGI.

    ``LifespanManager`` is needed because ``httpx.ASGITransport`` does not
    run the lifespan protocol; without startup the app's ``get_state``
    dependency raises HTTP 503.
    """
    async with LifespanManager(cs_app):
        transport = httpx.ASGITransport(app=cs_app)
        client = ConfigServiceClient(_cs_settings(), transport=transport)
        try:
            yield client
        finally:
            await client.aclose()


@pytest_asyncio.fixture
async def raw_http(cs_app) -> AsyncIterator[httpx.AsyncClient]:
    """Raw httpx client against the same ASGI app, for calls intentionally
    outside ``ConfigServiceClient``'s wrapped API (test-only admin
    endpoints and deliberate contract probes)."""
    async with LifespanManager(cs_app):
        transport = httpx.ASGITransport(app=cs_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


@pytest.mark.asyncio
async def test_empty_registry_reports_empty(cs_client: ConfigServiceClient):
    assert await cs_client.is_registry_empty() is True
    assert await cs_client.get_devices_info() == {}
    assert await cs_client.get_instantiation_specs() == {}


@pytest.mark.asyncio
async def test_upsert_creates_then_get_instantiation_specs_returns_it(
    cs_client: ConfigServiceClient,
):
    data = _payload("m1", prefix="XF:M1")["m1"]
    await cs_client.upsert_device(data["metadata"], data["spec"])

    info = await cs_client.get_devices_info()
    assert "m1" in info
    assert info["m1"]["ophyd_class"] == "FakeOphydMotor"

    specs = await cs_client.get_instantiation_specs()
    assert "m1" in specs
    assert specs["m1"]["device_class"].endswith(".FakeOphydMotor")
    assert specs["m1"]["args"] == ["XF:M1"]


@pytest.mark.asyncio
async def test_upsert_conflict_falls_back_to_put(
    cs_client: ConfigServiceClient, raw_http: httpx.AsyncClient
):
    """Second upsert of the same name must round-trip POST-409 → PUT.
    The MockTransport unit test asserts the client retries on a shaped
    409; this asserts the real server actually responds with 409 for a
    duplicate POST and accepts the PUT fallback."""
    a = _payload("m1", prefix="XF:A")["m1"]
    b = _payload("m1", prefix="XF:B")["m1"]
    await cs_client.upsert_device(a["metadata"], a["spec"])

    response = await raw_http.post(
        "/api/v1/devices",
        json={"metadata": b["metadata"], "instantiation_spec": b["spec"]},
    )
    assert response.status_code == 409

    await cs_client.upsert_device(b["metadata"], b["spec"])
    specs = await cs_client.get_instantiation_specs()
    assert specs["m1"]["args"] == ["XF:B"]


@pytest.mark.asyncio
async def test_delete_device_roundtrip(cs_client: ConfigServiceClient):
    data = _payload("m1")["m1"]
    await cs_client.upsert_device(data["metadata"], data["spec"])
    await cs_client.delete_device("m1")

    info = await cs_client.get_devices_info()
    assert "m1" not in info


@pytest.mark.asyncio
async def test_sync_bootstraps_empty_registry_and_captures_cursor(
    cs_client: ConfigServiceClient,
):
    device_data = _payloads(("m1", "XF:M1"), ("m2", "XF:M2"))

    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1", "m2"],
        device_data=device_data,
    )

    assert isinstance(state, ConfigServiceState)
    assert state.cursor > 0
    assert state.epoch

    info = await cs_client.get_devices_info()
    assert set(info.keys()) == {"m1", "m2"}


@pytest.mark.asyncio
async def test_sync_skips_bootstrap_when_registry_populated(
    cs_client: ConfigServiceClient,
):
    # Seed a different device so sync detects non-empty and skips POST.
    seed = _payload("seed")["seed"]
    await cs_client.upsert_device(seed["metadata"], seed["spec"])

    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1"],
        device_data=_payload("m1"),
    )

    info = await cs_client.get_devices_info()
    assert "m1" not in info
    assert "seed" in info
    assert state.cursor > 0


@pytest.mark.asyncio
async def test_lock_and_unlock_devices(cs_client: ConfigServiceClient):
    for name, prefix in [("m1", "XF:M1"), ("m2", "XF:M2")]:
        data = _payload(name, prefix=prefix)[name]
        await cs_client.upsert_device(data["metadata"], data["spec"])

    result = await cs_client.lock_devices(
        ["m1", "m2"], item_id="env:abc", plan_name="__environment__"
    )
    assert result["success"] is True
    assert set(result["locked_devices"]) == {"m1", "m2"}

    with pytest.raises(ConfigServiceConflict):
        await cs_client.lock_devices(
            ["m1"], item_id="env:xyz", plan_name="__environment__"
        )

    # Wrong-owner unlock surfaces as HTTP 403 — neither Conflict nor NotFound.
    with pytest.raises(ConfigServiceHTTPError) as exc_info:
        await cs_client.unlock_devices(["m1", "m2"], item_id="env:wrong")
    assert exc_info.value.status_code == 403

    unlock_result = await cs_client.unlock_devices(["m1", "m2"], item_id="env:abc")
    assert unlock_result["success"] is True
    assert set(unlock_result["unlocked_devices"]) == {"m1", "m2"}


@pytest.mark.asyncio
async def test_changes_since_noop_after_sync(cs_client: ConfigServiceClient):
    state = await sync_devices_on_env_open(
        cs_client, expected_device_names=["m1"], device_data=_payload("m1")
    )

    plan = await fetch_staleness_plan(cs_client, state)
    assert plan.is_noop
    assert plan.new_state.epoch == state.epoch
    assert plan.new_state.cursor == state.cursor


@pytest.mark.asyncio
async def test_changes_since_reports_upsert(cs_client: ConfigServiceClient):
    state = await sync_devices_on_env_open(
        cs_client, expected_device_names=["m1"], device_data=_payload("m1")
    )

    m2 = _payload("m2", prefix="XF:M2")["m2"]
    await cs_client.upsert_device(m2["metadata"], m2["spec"])

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
    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1", "m2"],
        device_data=_payloads(("m1", "XF:M1"), ("m2", "XF:M2")),
    )

    await cs_client.delete_device("m2")

    plan = await fetch_staleness_plan(cs_client, state)
    assert not plan.replace_overlay
    assert plan.upserts == {}
    assert plan.deletes == ["m2"]
    assert plan.new_state.cursor > state.cursor


@pytest.mark.asyncio
async def test_changes_since_reset_triggers_full_replace(
    cs_client: ConfigServiceClient, raw_http: httpx.AsyncClient
):
    """After POST /registry/clear, the next /devices/changes must surface
    reset_occurred=True; fetch_staleness_plan must then also pull the full
    /devices/instantiation payload so the caller can atomically replace
    its overlay with the full current registry (not just the delta)."""
    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1", "m2"],
        device_data=_payloads(("m1", "XF:M1"), ("m2", "XF:M2")),
    )

    wipe = await raw_http.post("/api/v1/registry/clear")
    assert wipe.status_code == 200
    assert wipe.json()["success"] is True

    m3 = _payload("m3", prefix="XF:M3")["m3"]
    await cs_client.upsert_device(m3["metadata"], m3["spec"])

    plan = await fetch_staleness_plan(cs_client, state)
    assert plan.replace_overlay is True
    assert set(plan.upserts.keys()) == {"m3"}
    assert plan.new_state.cursor > state.cursor


@pytest.mark.asyncio
async def test_build_staleness_plan_on_real_response(
    cs_client: ConfigServiceClient,
):
    """Confirm the real server's DeviceChangesResponse shape is compatible
    with ``build_staleness_plan`` — i.e. the field names, types, and op
    values match. This catches contract drift between the two repos."""
    data = _payload("m1")["m1"]
    await cs_client.upsert_device(data["metadata"], data["spec"])
    initial = await cs_client.get_changes_since(0)

    plan = build_staleness_plan(initial, saved_epoch=initial["service_epoch"])
    assert plan.replace_overlay is False
    assert set(plan.upserts.keys()) == {"m1"}
    assert plan.deletes == []
    assert plan.new_state.epoch == initial["service_epoch"]


@pytest.mark.asyncio
async def test_sync_with_prefetched_empty_info_bootstraps_without_probe(
    cs_client: ConfigServiceClient,
):
    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1"],
        device_data=_payload("m1"),
        prefetched_info={},
    )
    assert state.cursor > 0
    info = await cs_client.get_devices_info()
    assert "m1" in info


@pytest.mark.asyncio
async def test_sync_with_prefetched_populated_info_skips_bootstrap(
    cs_client: ConfigServiceClient,
):
    seed = _payload("seed")["seed"]
    await cs_client.upsert_device(seed["metadata"], seed["spec"])

    # Passing a non-empty prefetched_info tells sync to skip the POST
    # fan-out regardless of what the real /devices-info would say.
    state = await sync_devices_on_env_open(
        cs_client,
        expected_device_names=["m1"],
        device_data=_payload("m1"),
        prefetched_info={"seed": seed["metadata"]},
    )

    info = await cs_client.get_devices_info()
    assert "m1" not in info
    assert state.cursor > 0
