"""
Unit tests for bluesky_queueserver.manager.config_service.

No real network: the client is driven by a locally-assembled
``httpx.MockTransport`` so each test can dictate the sequence of responses
(and exceptions) the client sees.
"""

from __future__ import annotations

import json
from typing import Any, Callable, List

import httpx
import pytest

from bluesky_queueserver.manager.config_service import (
    ConfigServiceClient,
    ConfigServiceConflict,
    ConfigServiceError,
    ConfigServiceHTTPError,
    ConfigServiceNotFound,
    ConfigServiceProtocolError,
    ConfigServiceSettings,
    ConfigServiceState,
    ConfigServiceUnreachable,
    build_staleness_plan,
    fetch_staleness_plan,
    sync_devices_on_env_open,
)


def _settings(**overrides: Any) -> ConfigServiceSettings:
    base = dict(
        enabled=True,
        url="http://cs.test",
        timeout=1.0,
        max_attempts=3,
        backoff_ms=(1, 1),  # tight backoff to keep tests fast
        service_name="bluesky-queueserver",
    )
    base.update(overrides)
    return ConfigServiceSettings(**base)


class _Responder:
    """Queue-backed request handler for MockTransport."""

    def __init__(self, handlers: List[Callable[[httpx.Request], Any]]):
        self._handlers = list(handlers)
        self.calls: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if not self._handlers:
            raise AssertionError(f"No handler left for request: {request.method} {request.url}")
        handler = self._handlers.pop(0)
        result = handler(request)
        if isinstance(result, Exception):
            raise result
        return result


def _json_response(status: int, body: Any) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body).encode(), headers={"content-type": "application/json"})
    return handler


def _status(status: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _req: httpx.Response(status)


def _raise(exc: Exception) -> Callable[[httpx.Request], Exception]:
    return lambda _req: exc


async def _aclient(handlers: List[Callable[[httpx.Request], Any]], **settings_overrides: Any):
    responder = _Responder(handlers)
    transport = httpx.MockTransport(responder)
    client = ConfigServiceClient(_settings(**settings_overrides), transport=transport)
    return client, responder


# ===== ConfigServiceSettings parsing =====


def test_settings_defaults_when_section_missing():
    s = ConfigServiceSettings.from_config_dict(None)
    assert s.enabled is False


def test_settings_enabled_requires_url():
    with pytest.raises(ValueError, match="url"):
        ConfigServiceSettings.from_config_dict({"enabled": True})


def test_settings_enabled_with_url_uses_tuning_defaults():
    s = ConfigServiceSettings.from_config_dict(
        {"enabled": True, "url": "http://cs.test:8004"}
    )
    assert s.enabled is True
    assert s.url == "http://cs.test:8004"
    assert s.max_attempts == 3
    assert s.backoff_ms == (200, 400)


def test_settings_overrides():
    s = ConfigServiceSettings.from_config_dict(
        {
            "enabled": True,
            "url": "http://cs.prod:8080",
            "timeout": 5,
            "max_attempts": 5,
            "backoff_ms": [100, 250, 500],
            "service_name": "qserver-beamline-1",
        }
    )
    assert s.url == "http://cs.prod:8080"
    assert s.timeout == 5.0
    assert s.max_attempts == 5
    assert s.backoff_ms == (100, 250, 500)
    assert s.service_name == "qserver-beamline-1"


def test_settings_max_attempts_must_be_positive():
    with pytest.raises(ValueError):
        ConfigServiceSettings.from_config_dict(
            {"enabled": True, "url": "http://cs.test", "max_attempts": 0}
        )


def test_disabled_settings_reject_client_construction():
    with pytest.raises(ValueError):
        ConfigServiceClient(ConfigServiceSettings(enabled=False))


# ===== Happy-path wrappers =====


@pytest.mark.asyncio
async def test_get_devices_info_returns_body():
    client, _ = await _aclient([_json_response(200, {"motor1": {"name": "motor1"}})])
    async with client:
        body = await client.get_devices_info()
    assert body == {"motor1": {"name": "motor1"}}


@pytest.mark.asyncio
async def test_is_registry_empty_true_on_empty_dict():
    client, _ = await _aclient([_json_response(200, {})])
    async with client:
        assert await client.is_registry_empty() is True


@pytest.mark.asyncio
async def test_is_registry_empty_false_on_populated():
    client, _ = await _aclient([_json_response(200, {"m1": {}})])
    async with client:
        assert await client.is_registry_empty() is False


@pytest.mark.asyncio
async def test_get_changes_since_parses_full_payload():
    payload = {
        "current_version": 42,
        "service_epoch": "abc",
        "reset_occurred": False,
        "changes": [],
    }
    client, responder = await _aclient([_json_response(200, payload)])
    async with client:
        body = await client.get_changes_since(10)
    assert body == payload
    assert responder.calls[0].url.params["since_version"] == "10"


@pytest.mark.asyncio
async def test_get_changes_since_rejects_missing_fields():
    client, _ = await _aclient([_json_response(200, {"current_version": 1})])
    async with client:
        with pytest.raises(ConfigServiceProtocolError):
            await client.get_changes_since(0)


@pytest.mark.asyncio
async def test_upsert_device_uses_post_on_first_create():
    resp = {"success": True}
    client, responder = await _aclient([_json_response(201, resp)])
    async with client:
        out = await client.upsert_device({"name": "m1"}, {"device_class": "ophyd.EpicsMotor"})
    assert out == resp
    assert responder.calls[0].method == "POST"
    assert responder.calls[0].url.path == "/api/v1/devices"


@pytest.mark.asyncio
async def test_upsert_device_falls_back_to_put_on_409():
    handlers = [
        _json_response(409, {"detail": "exists"}),
        _json_response(200, {"success": True, "updated": True}),
    ]
    client, responder = await _aclient(handlers)
    async with client:
        out = await client.upsert_device({"name": "m1"}, None)
    assert out == {"success": True, "updated": True}
    assert [c.method for c in responder.calls] == ["POST", "PUT"]
    assert responder.calls[1].url.path == "/api/v1/devices/m1"


@pytest.mark.asyncio
async def test_lock_devices_sends_correct_body():
    client, responder = await _aclient([_json_response(200, {"success": True})])
    async with client:
        await client.lock_devices(["a", "b"], item_id="item-1", plan_name="count")
    body = json.loads(responder.calls[0].content.decode())
    assert body["device_names"] == ["a", "b"]
    assert body["item_id"] == "item-1"
    assert body["plan_name"] == "count"
    assert body["locked_by_service"] == "bluesky-queueserver"


@pytest.mark.asyncio
async def test_unlock_devices_sends_correct_body():
    client, responder = await _aclient([_json_response(200, {"success": True})])
    async with client:
        await client.unlock_devices(["a"], item_id="item-1")
    body = json.loads(responder.calls[0].content.decode())
    assert body == {"device_names": ["a"], "item_id": "item-1"}


# ===== Retry behavior =====


@pytest.mark.asyncio
async def test_retries_on_connect_error_then_succeeds():
    handlers = [
        _raise(httpx.ConnectError("refused")),
        _json_response(200, {"ok": True}),
    ]
    client, responder = await _aclient(handlers)
    async with client:
        body = await client.get_devices_info()
    assert body == {"ok": True}
    assert len(responder.calls) == 2


@pytest.mark.asyncio
async def test_retries_on_503_then_succeeds():
    handlers = [_status(503), _json_response(200, {"ok": True})]
    client, responder = await _aclient(handlers)
    async with client:
        body = await client.get_devices_info()
    assert body == {"ok": True}
    assert len(responder.calls) == 2


@pytest.mark.asyncio
async def test_retry_exhaustion_raises_unreachable():
    handlers = [_status(503), _status(503), _status(503)]
    client, responder = await _aclient(handlers)
    async with client:
        with pytest.raises(ConfigServiceUnreachable):
            await client.get_devices_info()
    assert len(responder.calls) == 3


@pytest.mark.asyncio
async def test_no_retry_on_400():
    handlers = [_json_response(400, {"detail": "bad"})]
    client, responder = await _aclient(handlers)
    async with client:
        with pytest.raises(ConfigServiceHTTPError) as excinfo:
            await client.get_devices_info()
    assert excinfo.value.status_code == 400
    assert len(responder.calls) == 1


@pytest.mark.asyncio
async def test_no_retry_on_500():
    handlers = [_json_response(500, {"detail": "boom"})]
    client, responder = await _aclient(handlers)
    async with client:
        with pytest.raises(ConfigServiceHTTPError) as excinfo:
            await client.get_devices_info()
    assert excinfo.value.status_code == 500
    assert len(responder.calls) == 1


@pytest.mark.asyncio
async def test_no_retry_on_404():
    client, _ = await _aclient([_json_response(404, {"detail": "missing"})])
    async with client:
        with pytest.raises(ConfigServiceNotFound):
            await client.delete_device("ghost")


@pytest.mark.asyncio
async def test_no_retry_on_409_conflict():
    client, _ = await _aclient([_json_response(409, {"detail": "locked"})])
    async with client:
        with pytest.raises(ConfigServiceConflict):
            await client.lock_devices(["a"], item_id="i", plan_name="p")


@pytest.mark.asyncio
async def test_timeout_retries_then_raises():
    handlers = [
        _raise(httpx.ReadTimeout("slow")),
        _raise(httpx.ReadTimeout("slow")),
        _raise(httpx.ReadTimeout("slow")),
    ]
    client, responder = await _aclient(handlers)
    async with client:
        with pytest.raises(ConfigServiceUnreachable):
            await client.get_devices_info()
    assert len(responder.calls) == 3


@pytest.mark.asyncio
async def test_backoff_schedule_uses_last_entry_for_extra_attempts():
    # With 5 attempts but only 2 backoff entries, attempts 3/4/5 use the last entry
    handlers = [_raise(httpx.ConnectError("x")) for _ in range(5)]
    client, responder = await _aclient(handlers, max_attempts=5, backoff_ms=(1, 1))
    async with client:
        with pytest.raises(ConfigServiceUnreachable):
            await client.get_devices_info()
    assert len(responder.calls) == 5


# ===== sync_devices_on_env_open =====


def _device_payload(name: str, prefix: str = "XF:01-Mtr{M1}") -> dict:
    return {
        "metadata": {
            "name": name,
            "device_label": "motor",
            "ophyd_class": "EpicsMotor",
        },
        "spec": {
            "name": name,
            "device_class": "ophyd.EpicsMotor",
            "args": [prefix],
            "kwargs": {"name": name},
        },
    }


@pytest.mark.asyncio
async def test_sync_bootstraps_when_registry_empty():
    changes_payload = {
        "current_version": 3,
        "service_epoch": "2026-01-01",
        "reset_occurred": False,
        "changes": [],
    }
    handlers = [
        _json_response(200, {}),                      # /devices-info: empty
        _json_response(201, {"success": True}),       # POST m1
        _json_response(201, {"success": True}),       # POST m2
        _json_response(200, changes_payload),         # /devices/changes
    ]
    client, responder = await _aclient(handlers)
    async with client:
        state = await sync_devices_on_env_open(
            client,
            expected_device_names=["m1", "m2"],
            device_data={"m1": _device_payload("m1"), "m2": _device_payload("m2")},
        )
    assert state == ConfigServiceState(cursor=3, epoch="2026-01-01")
    # GET /devices-info → 2 parallel POSTs (order undefined under gather) → GET /changes
    methods = [c.method for c in responder.calls]
    assert methods[0] == "GET"
    assert methods[-1] == "GET"
    assert sorted(methods[1:-1]) == ["POST", "POST"]
    post_urls = [c.url.path for c in responder.calls if c.method == "POST"]
    assert all(u == "/api/v1/devices" for u in post_urls)


@pytest.mark.asyncio
async def test_sync_skips_bootstrap_when_registry_non_empty():
    changes_payload = {
        "current_version": 42,
        "service_epoch": "2026-01-01",
        "reset_occurred": False,
        "changes": [],
    }
    handlers = [
        _json_response(200, {"m1": {"name": "m1"}}),  # /devices-info: populated
        _json_response(200, changes_payload),         # /devices/changes
    ]
    client, responder = await _aclient(handlers)
    async with client:
        state = await sync_devices_on_env_open(
            client,
            expected_device_names=["m1"],
            device_data={"m1": _device_payload("m1")},
        )
    assert state == ConfigServiceState(cursor=42, epoch="2026-01-01")
    assert [c.method for c in responder.calls] == ["GET", "GET"]


@pytest.mark.asyncio
async def test_sync_raises_if_introspection_missed_a_device():
    client, responder = await _aclient([])
    async with client:
        with pytest.raises(ConfigServiceError, match="missing_device"):
            await sync_devices_on_env_open(
                client,
                expected_device_names=["m1", "missing_device"],
                device_data={"m1": _device_payload("m1")},
            )
    assert responder.calls == []


@pytest.mark.asyncio
async def test_sync_propagates_bootstrap_failure():
    handlers = [
        _json_response(200, {}),                       # empty
        _json_response(500, {"detail": "db gone"}),    # POST fails hard
    ]
    client, _ = await _aclient(handlers)
    async with client:
        with pytest.raises(ConfigServiceHTTPError):
            await sync_devices_on_env_open(
                client,
                expected_device_names=["m1"],
                device_data={"m1": _device_payload("m1")},
            )


# ===== Layer 2.6: get_instantiation_specs + prefetched_info =====


@pytest.mark.asyncio
async def test_get_instantiation_specs_returns_body():
    specs = {
        "m1": {"name": "m1", "device_class": "ophyd.EpicsMotor", "args": ["XF:M1"], "kwargs": {"name": "m1"}},
    }
    client, responder = await _aclient([_json_response(200, specs)])
    async with client:
        body = await client.get_instantiation_specs()
    assert body == specs
    assert responder.calls[0].url.path == "/api/v1/devices/instantiation"


@pytest.mark.asyncio
async def test_get_instantiation_specs_rejects_non_dict():
    client, _ = await _aclient([_json_response(200, ["not", "a", "dict"])])
    async with client:
        with pytest.raises(ConfigServiceProtocolError):
            await client.get_instantiation_specs()


@pytest.mark.asyncio
async def test_sync_with_prefetched_info_empty_bootstraps_without_probe():
    """prefetched_info={} → skip GET /devices-info, go straight to upsert+changes."""
    changes_payload = {
        "current_version": 1,
        "service_epoch": "2026-04",
        "reset_occurred": False,
        "changes": [],
    }
    handlers = [
        _json_response(201, {"success": True}),   # POST m1 (bootstrap)
        _json_response(200, changes_payload),     # GET /devices/changes
    ]
    client, responder = await _aclient(handlers)
    async with client:
        state = await sync_devices_on_env_open(
            client,
            expected_device_names=["m1"],
            device_data={"m1": _device_payload("m1")},
            prefetched_info={},
        )
    assert state == ConfigServiceState(cursor=1, epoch="2026-04")
    methods = [c.method for c in responder.calls]
    assert methods == ["POST", "GET"]  # no /devices-info probe
    paths = [c.url.path for c in responder.calls]
    assert "/api/v1/devices-info" not in paths


@pytest.mark.asyncio
async def test_sync_with_prefetched_info_populated_skips_bootstrap_and_probe():
    """prefetched_info populated → no probe, no upserts, only /changes."""
    changes_payload = {
        "current_version": 9,
        "service_epoch": "2026-04",
        "reset_occurred": False,
        "changes": [],
    }
    handlers = [_json_response(200, changes_payload)]
    client, responder = await _aclient(handlers)
    async with client:
        state = await sync_devices_on_env_open(
            client,
            expected_device_names=["m1"],
            device_data={"m1": _device_payload("m1")},
            prefetched_info={"m1": {"name": "m1"}},
        )
    assert state == ConfigServiceState(cursor=9, epoch="2026-04")
    assert [c.method for c in responder.calls] == ["GET"]
    assert responder.calls[0].url.path == "/api/v1/devices/changes"


# ===== Layer 2.7: pre-plan staleness plan =====


def _changes_response(
    *, current_version: int, service_epoch: str, reset_occurred: bool = False, changes=None
):
    return {
        "current_version": current_version,
        "service_epoch": service_epoch,
        "reset_occurred": reset_occurred,
        "changes": list(changes or []),
    }


def _upsert(name: str, *, prefix: str = "XF:M1") -> dict:
    return {
        "device_name": name,
        "op": "upsert",
        "version": 7,
        "spec": {
            "name": name,
            "device_class": "ophyd.EpicsMotor",
            "args": [prefix],
            "kwargs": {"name": name},
            "active": True,
        },
    }


def _delete(name: str) -> dict:
    return {"device_name": name, "op": "delete", "version": 8}


def test_build_staleness_plan_noop_when_no_changes():
    response = _changes_response(current_version=5, service_epoch="e1")
    plan = build_staleness_plan(response, saved_epoch="e1")
    assert plan.is_noop
    assert plan.replace_overlay is False
    assert plan.upserts == {}
    assert plan.deletes == []
    assert plan.new_state == ConfigServiceState(cursor=5, epoch="e1")


def test_build_staleness_plan_incremental_upsert_and_delete():
    response = _changes_response(
        current_version=9,
        service_epoch="e1",
        changes=[_upsert("m1"), _upsert("m2"), _delete("m3")],
    )
    plan = build_staleness_plan(response, saved_epoch="e1")
    assert plan.replace_overlay is False
    assert plan.is_noop is False
    assert set(plan.upserts) == {"m1", "m2"}
    assert plan.upserts["m1"]["device_class"] == "ophyd.EpicsMotor"
    assert plan.deletes == ["m3"]
    assert plan.new_state.cursor == 9


def test_build_staleness_plan_full_on_reset_occurred():
    response = _changes_response(
        current_version=9, service_epoch="e1", reset_occurred=True, changes=[_upsert("m1")]
    )
    plan = build_staleness_plan(response, saved_epoch="e1")
    # reset means "discard local state"; upserts from the changes list are
    # irrelevant — the fetch step populates the full registry.
    assert plan.replace_overlay is True
    assert plan.is_noop is False
    assert plan.upserts == {}
    assert plan.deletes == []


def test_build_staleness_plan_full_on_epoch_mismatch():
    response = _changes_response(
        current_version=9, service_epoch="e2", changes=[_upsert("m1")]
    )
    plan = build_staleness_plan(response, saved_epoch="e1")
    assert plan.replace_overlay is True
    assert plan.new_state.epoch == "e2"


def test_build_staleness_plan_rejects_upsert_without_spec():
    bad_change = {"device_name": "m1", "op": "upsert", "version": 1}  # spec missing
    response = _changes_response(current_version=5, service_epoch="e1", changes=[bad_change])
    with pytest.raises(ConfigServiceProtocolError, match="missing 'spec'"):
        build_staleness_plan(response, saved_epoch="e1")


def test_build_staleness_plan_rejects_unknown_op():
    bad_change = {"device_name": "m1", "op": "patch", "version": 1}
    response = _changes_response(current_version=5, service_epoch="e1", changes=[bad_change])
    with pytest.raises(ConfigServiceProtocolError, match="unknown change op"):
        build_staleness_plan(response, saved_epoch="e1")


@pytest.mark.asyncio
async def test_fetch_staleness_plan_incremental_hits_only_changes():
    changes = _changes_response(
        current_version=11, service_epoch="e1", changes=[_upsert("m1")]
    )
    client, responder = await _aclient([_json_response(200, changes)])
    async with client:
        plan = await fetch_staleness_plan(client, ConfigServiceState(cursor=5, epoch="e1"))
    assert plan.replace_overlay is False
    assert set(plan.upserts) == {"m1"}
    paths = [c.url.path for c in responder.calls]
    assert paths == ["/api/v1/devices/changes"]
    # cursor was forwarded as since_version
    assert responder.calls[0].url.params["since_version"] == "5"


@pytest.mark.asyncio
async def test_fetch_staleness_plan_full_also_fetches_instantiation_specs():
    changes = _changes_response(
        current_version=20, service_epoch="e2", reset_occurred=True
    )
    full_specs = {"m1": _upsert("m1")["spec"], "m2": _upsert("m2")["spec"]}
    handlers = [_json_response(200, changes), _json_response(200, full_specs)]
    client, responder = await _aclient(handlers)
    async with client:
        plan = await fetch_staleness_plan(client, ConfigServiceState(cursor=3, epoch="e1"))
    assert plan.replace_overlay is True
    assert set(plan.upserts) == {"m1", "m2"}
    paths = [c.url.path for c in responder.calls]
    assert paths == ["/api/v1/devices/changes", "/api/v1/devices/instantiation"]


@pytest.mark.asyncio
async def test_fetch_staleness_plan_noop_only_hits_changes_endpoint():
    changes = _changes_response(current_version=7, service_epoch="e1")
    client, responder = await _aclient([_json_response(200, changes)])
    async with client:
        plan = await fetch_staleness_plan(client, ConfigServiceState(cursor=7, epoch="e1"))
    assert plan.is_noop
    # No /devices/instantiation call, no side-effects beyond the /changes probe.
    assert [c.url.path for c in responder.calls] == ["/api/v1/devices/changes"]


# ===== Layer 2.7 worker handler: full-replace drops stale overlay =====


def _call_overlay_handler(
    overlay_names, namespace, *, upserts, deletes, replace
):
    """Invoke the worker's handler against a stand-in with the two attrs
    it actually reads, so we can test the replace-overlay semantics without
    spinning up a real Process. Patches ``instantiate_device_from_spec`` to
    an identity function so we stay stdlib-only."""
    from bluesky_queueserver.manager import worker as worker_mod

    class _Stub:
        re_state = "idle"

    stub = _Stub()
    stub._re_namespace = dict(namespace)
    stub._config_service_overlay_names = set(overlay_names)

    real_instantiate = worker_mod.instantiate_device_from_spec
    worker_mod.instantiate_device_from_spec = lambda spec: f"instance({spec['name']})"
    try:
        result = worker_mod.RunEngineWorker._command_update_device_overlay_handler(
            stub, upserts=upserts, deletes=deletes, replace=replace
        )
    finally:
        worker_mod.instantiate_device_from_spec = real_instantiate
    return result, stub


def test_overlay_handler_full_replace_drops_stale_overlay_but_keeps_profile_devices():
    # Profile owns 'profile_only'; previous config-service overlay owned
    # {'m1', 'm2'}. A full replace with upserts={'m1', 'm3'} should:
    #   - keep 'profile_only' (never overlaid)
    #   - drop 'm2' implicitly (overlaid before, absent from new registry)
    #   - replace 'm1' with the new spec's instance
    #   - add 'm3'
    namespace = {
        "profile_only": "profile_instance",
        "m1": "old_m1",
        "m2": "old_m2",
    }
    result, stub = _call_overlay_handler(
        overlay_names={"m1", "m2"},
        namespace=namespace,
        upserts={"m1": _upsert("m1")["spec"], "m3": _upsert("m3")["spec"]},
        deletes=[],
        replace=True,
    )
    assert result["status"] == "accepted"
    assert stub._re_namespace == {
        "profile_only": "profile_instance",
        "m1": "instance(m1)",
        "m3": "instance(m3)",
    }
    assert stub._config_service_overlay_names == {"m1", "m3"}


@pytest.mark.asyncio
async def test_manager_helper_returns_same_client_across_calls():
    # The long-lived ConfigServiceClient refactor relies on the manager's
    # lazy-init helper returning the SAME client on every call, so httpx
    # keep-alive actually amortizes across prefetch/sync/staleness/unlock.
    # Regression guard: a future maintainer who instead writes
    # `ConfigServiceClient(self._settings)` here would silently defeat the
    # whole optimization.
    from bluesky_queueserver.manager.manager import RunEngineManager

    class _Stub:
        _config_service_settings = _settings()
        _config_service_client = None

    stub = _Stub()
    c1 = await RunEngineManager._get_config_service_client(stub)
    c2 = await RunEngineManager._get_config_service_client(stub)
    try:
        assert c1 is c2
    finally:
        await c1.aclose()


def test_overlay_handler_incremental_respects_explicit_deletes_only():
    namespace = {
        "profile_only": "profile_instance",
        "m1": "old_m1",
        "m2": "old_m2",
    }
    result, stub = _call_overlay_handler(
        overlay_names={"m1", "m2"},
        namespace=namespace,
        upserts={"m1": _upsert("m1")["spec"]},  # updated spec for m1
        deletes=["m2"],                          # m2 explicitly removed
        replace=False,
    )
    assert result["status"] == "accepted"
    assert stub._re_namespace == {
        "profile_only": "profile_instance",
        "m1": "instance(m1)",
    }
    # incremental merges: m1 stays, m2 drops.
    assert stub._config_service_overlay_names == {"m1"}

