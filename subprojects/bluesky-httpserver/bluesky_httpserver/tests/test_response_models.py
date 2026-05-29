"""Phase U3b — validate the per-route ``response_model`` declarations against live responses.

These tests drive real requests through a running httpserver + RE Manager (the existing
``fastapi_server`` / ``re_manager`` fixtures) and confirm the actual response shapes satisfy
the Pydantic models in ``bluesky_httpserver.re_manager_schemas``. Because every model uses
``extra="forbid"``, a server-side ResponseValidationError (drift between a model and the
manager's real dict) surfaces as an HTTP 500 — caught here both by ``_validates`` (the error
body fails the model round-trip) and by the explicit drift-sentinel tests that compare live
keys to the model's declared fields.

The models are intentionally introspected against real responses rather than hand-asserted,
so they cannot silently drift from the manager.
"""

import pprint

import pytest
from bluesky_queueserver.manager.tests.common import (  # noqa: F401
    re_manager,
    re_manager_cmd,
    re_manager_factory,
    re_manager_pc_copy,
)

from bluesky_httpserver.re_manager_schemas import (
    ConfigGetResponse,
    ConsoleOutputResponse,
    ConsoleOutputUidResponse,
    ConsoleOutputUpdateResponse,
    DevicesAllowedResponse,
    DevicesExistingResponse,
    HistoryGetResponse,
    HistoryItem,
    ItemGetResponse,
    ItemResponse,
    ItemsBatchResponse,
    LockResponse,
    PermissionsGetResponse,
    PlansAllowedResponse,
    PlansExistingResponse,
    QueueGetResponse,
    QueueItem,
    RunsResponse,
    StatusResponse,
    SuccessMsgResponse,
    TaskResultResponse,
    TaskStatusResponse,
    TaskUidResponse,
)
from bluesky_httpserver.tests.conftest import (  # noqa: F401
    add_plans_to_queue,
    fastapi_server,
    request_to_json,
    wait_for_environment_to_be_created,
    wait_for_manager_state_idle,
    wait_for_queue_execution_to_complete,
)


def _validates(model, resp):
    """Round-trip a live response through its model (``extra="forbid"``).

    Fails if the response carries an unexpected key, is missing a required field, or is an
    error body (e.g. a 500 from a server-side ResponseValidationError) that slipped through.
    """
    assert "detail" not in resp, f"error response leaked instead of {model.__name__}:\n{pprint.pformat(resp)}"
    return model(**resp)


# --------------------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/ping", "/status"])
def test_status_response_model(re_manager, fastapi_server, path):  # noqa: F811
    resp = request_to_json("get", path)
    _validates(StatusResponse, resp)


def test_status_drift_sentinel(re_manager, fastapi_server):  # noqa: F811
    """Live status keys must exactly match the model's declared fields (fail loud on drift)."""
    resp = request_to_json("get", "/status")
    assert set(resp.keys()) == set(StatusResponse.__fields__), pprint.pformat(resp)


# --------------------------------------------------------------------------------------
# Queue control (bare {success, msg})
# --------------------------------------------------------------------------------------


def test_success_msg_response_models(re_manager, fastapi_server):  # noqa: F811
    # All return SuccessMsgResponse on both success and the no-environment failure path.
    _validates(SuccessMsgResponse, request_to_json("post", "/queue/autostart", json={"enable": False}))
    _validates(SuccessMsgResponse, request_to_json("post", "/queue/mode/set", json={"mode": {"loop": True}}))
    _validates(SuccessMsgResponse, request_to_json("post", "/queue/clear"))
    _validates(SuccessMsgResponse, request_to_json("post", "/queue/stop"))
    _validates(SuccessMsgResponse, request_to_json("post", "/queue/stop/cancel"))
    _validates(SuccessMsgResponse, request_to_json("post", "/history/clear"))
    # queue/start with no environment -> success=False, still a SuccessMsgResponse envelope.
    resp = request_to_json("post", "/queue/start")
    model = _validates(SuccessMsgResponse, resp)
    assert model.success is False


# --------------------------------------------------------------------------------------
# Queue get
# --------------------------------------------------------------------------------------


def test_queue_get_response_model(re_manager, fastapi_server):  # noqa: F811
    # Empty queue: items == [], running_item == {} (validates as an empty QueueItem).
    resp_empty = request_to_json("get", "/queue/get")
    _validates(QueueGetResponse, resp_empty)

    # Populated queue: every item validates as a QueueItem. Add over HTTP (the zmq
    # add_plans_to_queue helper leaves /queue/get serving a stale empty snapshot until an
    # HTTP write bumps plan_queue_uid -- a pre-existing httpserver quirk, not a model issue).
    for _ in range(3):
        request_to_json(
            "post",
            "/queue/item/add",
            json={"item": {"name": "count", "args": [["det1", "det2"]], "item_type": "plan"}},
        )
    resp = request_to_json("get", "/queue/get")
    model = _validates(QueueGetResponse, resp)
    assert len(model.items) == 3
    assert all(isinstance(item, QueueItem) and item.item_uid for item in model.items)


# --------------------------------------------------------------------------------------
# Queue items — single
# --------------------------------------------------------------------------------------


def test_item_add_response_model(re_manager, fastapi_server):  # noqa: F811
    resp = request_to_json(
        "post",
        "/queue/item/add",
        json={"item": {"name": "count", "args": [["det1", "det2"]], "item_type": "plan"}},
    )
    model = _validates(ItemResponse, resp)
    assert model.success is True
    assert model.qsize == 1
    assert model.item.name == "count"


def test_item_add_failure_response_model(re_manager, fastapi_server):  # noqa: F811
    # Unknown plan -> success=False, qsize=None, item=None. Must still validate.
    resp = request_to_json(
        "post",
        "/queue/item/add",
        json={"item": {"name": "nonexistent_plan", "item_type": "plan"}},
    )
    model = _validates(ItemResponse, resp)
    assert model.success is False
    assert model.qsize is None
    # On failure the manager echoes back the *rejected* item (not None); it must still
    # validate as a QueueItem.
    assert model.item is not None
    assert model.item.name == "nonexistent_plan"


def test_item_get_response_models(re_manager, fastapi_server):  # noqa: F811
    add_plans_to_queue()
    # Success: get an existing item.
    resp = request_to_json("get", "/queue/item/get", json={"pos": "front"})
    model = _validates(ItemGetResponse, resp)
    assert model.success is True
    assert model.item.item_uid

    # Failure: bad uid -> item == {} (empty dict). Must validate as an empty QueueItem.
    resp_fail = request_to_json("get", "/queue/item/get", json={"uid": "nonexistent-uid"})
    model_fail = _validates(ItemGetResponse, resp_fail)
    assert model_fail.success is False


def test_item_remove_and_move_response_models(re_manager, fastapi_server):  # noqa: F811
    add_plans_to_queue()
    _validates(ItemResponse, request_to_json("post", "/queue/item/move", json={"pos": "front", "pos_dest": "back"}))
    _validates(ItemResponse, request_to_json("post", "/queue/item/remove", json={"pos": "back"}))


# --------------------------------------------------------------------------------------
# Queue items — batch
# --------------------------------------------------------------------------------------


def test_item_add_batch_response_model(re_manager, fastapi_server):  # noqa: F811
    items = [
        {"name": "count", "args": [["det1", "det2"]], "item_type": "plan"},
        {"name": "count", "args": [["det1"]], "item_type": "plan"},
    ]
    resp = request_to_json("post", "/queue/item/add/batch", json={"items": items})
    model = _validates(ItemsBatchResponse, resp)
    assert model.success is True
    assert model.qsize == 2
    assert model.results is not None and len(model.results) == 2
    assert all(isinstance(r, SuccessMsgResponse) for r in model.results)


def test_item_batch_remove_and_move_response_models(re_manager, fastapi_server):  # noqa: F811
    add_plans_to_queue()
    resp_get = request_to_json("get", "/queue/get")
    uids = [item["item_uid"] for item in resp_get["items"]]

    _validates(
        ItemsBatchResponse,
        request_to_json("post", "/queue/item/move/batch", json={"uids": uids[:1], "pos_dest": "back"}),
    )
    _validates(
        ItemsBatchResponse,
        request_to_json("post", "/queue/item/remove/batch", json={"uids": uids}),
    )


# --------------------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------------------


def test_history_get_empty_response_model(re_manager, fastapi_server):  # noqa: F811
    resp = request_to_json("get", "/history/get")
    model = _validates(HistoryGetResponse, resp)
    assert model.items == []


def test_history_drift_sentinel_with_executed_plan(re_manager, fastapi_server):  # noqa: F811
    """Execute one plan, then confirm the populated history item (incl. ``result``) validates."""
    resp_add = request_to_json(
        "post",
        "/queue/item/add",
        json={"item": {"name": "count", "args": [["det1"]], "kwargs": {"num": 1}, "item_type": "plan"}},
    )
    assert resp_add["success"] is True, pprint.pformat(resp_add)

    assert request_to_json("post", "/environment/open")["success"] is True
    if not wait_for_environment_to_be_created(20):
        # The RE environment depends on the worker startup profile loading cleanly; if it
        # cannot (e.g. an incompatible bluesky/queueserver version in the test env), there is
        # no executed plan to populate history. Skip loudly rather than assert a model bug.
        status = request_to_json("get", "/status")
        pytest.skip(f"RE environment did not open (worker startup failed): {status.get('worker_environment_state')}")

    assert request_to_json("post", "/queue/start")["success"] is True
    assert wait_for_queue_execution_to_complete(20), "queue did not finish"

    resp = request_to_json("get", "/history/get")
    model = _validates(HistoryGetResponse, resp)
    assert len(model.items) == 1
    hist_item = model.items[0]
    assert isinstance(hist_item, HistoryItem)
    assert hist_item.result is not None  # executed items carry a result block

    request_to_json("post", "/environment/close")
    wait_for_manager_state_idle(20)


# --------------------------------------------------------------------------------------
# Follow-on groups: Config / Environment / Run Engine / Runs / Plans / Devices /
# Permissions / Scripts & Functions / Lock / Console Output.
#
# These are exercised against an idle manager (no open environment) -- each returns a valid
# envelope on the no-environment / empty path, which must still satisfy its model. The
# destructive Manager routes (/manager/stop, /test/manager/kill) are intentionally NOT
# called here (they would tear down the shared fixture manager); they reuse
# SuccessMsgResponse, covered by the bare-envelope cases below.
# --------------------------------------------------------------------------------------

# (method, path, model, json_payload-or-None) -- all safe against an idle manager.
_FOLLOWON_CASES = [
    ("get", "/config/get", ConfigGetResponse, None),
    # Environment control: no env open -> success=False, still the right envelope.
    ("post", "/environment/close", SuccessMsgResponse, None),
    ("post", "/environment/destroy", SuccessMsgResponse, None),
    ("post", "/environment/update", TaskUidResponse, None),
    # Run Engine control: no running plan -> success=False envelopes.
    ("post", "/re/pause", SuccessMsgResponse, None),
    ("post", "/re/resume", SuccessMsgResponse, None),
    ("post", "/re/stop", SuccessMsgResponse, None),
    ("post", "/re/abort", SuccessMsgResponse, None),
    ("post", "/re/halt", SuccessMsgResponse, None),
    # Runs: empty run list. /re/runs is POST (payload optional); the views are GET.
    ("post", "/re/runs", RunsResponse, None),
    ("get", "/re/runs/active", RunsResponse, None),
    ("get", "/re/runs/open", RunsResponse, None),
    ("get", "/re/runs/closed", RunsResponse, None),
    # NOTE: /re/metadata (ReMetadataResponse) is omitted -- the installed bluesky_queueserver_api
    # REManagerAPI client in this test env has no `re_metadata` method, so the endpoint 500s
    # before the response_model is reached. The model is still wired; validate it in an env whose
    # API client exposes re_metadata.
    # Plans / Devices: populated from the test profile.
    ("get", "/plans/allowed", PlansAllowedResponse, None),
    ("get", "/plans/existing", PlansExistingResponse, None),
    ("get", "/devices/allowed", DevicesAllowedResponse, None),
    ("get", "/devices/existing", DevicesExistingResponse, None),
    # Permissions.
    ("post", "/permissions/reload", SuccessMsgResponse, None),
    ("get", "/permissions/get", PermissionsGetResponse, None),
    # Scripts & Functions: task lookups with a bogus uid -> still a valid envelope.
    ("get", "/task/status", TaskStatusResponse, {"task_uid": "nonexistent-uid"}),
    ("get", "/task/result", TaskResultResponse, {"task_uid": "nonexistent-uid"}),
    ("post", "/kernel/interrupt", SuccessMsgResponse, None),
    # Lock: read-only lock info (lock/unlock share LockResponse, exercised via lock_info).
    # The handler requires a (possibly empty) request body.
    ("get", "/lock/info", LockResponse, {}),
    # Console output (no env required).
    ("get", "/console_output", ConsoleOutputResponse, None),
    ("get", "/console_output/uid", ConsoleOutputUidResponse, None),
    ("get", "/console_output_update", ConsoleOutputUpdateResponse, {"last_msg_uid": "ALL"}),
    ("get", "/test/server/sleep", SuccessMsgResponse, {"time": 0}),
]


def test_followon_response_models(re_manager, fastapi_server):  # noqa: F811
    """Validate every follow-on route's live response against its model (one idle manager)."""
    failures = []
    for method, path, model, payload in _FOLLOWON_CASES:
        kwargs = {"json": payload} if payload is not None else {}
        resp = request_to_json(method, path, **kwargs)
        try:
            _validates(model, resp)
        except Exception as ex:  # collect all mismatches in one run rather than stopping early
            failures.append(f"{method.upper()} {path} -> {model.__name__}: {ex}\n{pprint.pformat(resp)}")
    assert not failures, "\n\n".join(failures)


def test_lock_info_drift_sentinel(re_manager, fastapi_server):  # noqa: F811
    """The structured lock_info block keys must match the LockInfo model (fail loud on drift)."""
    from bluesky_httpserver.re_manager_schemas import LockInfo

    resp = request_to_json("get", "/lock/info", json={})
    model = _validates(LockResponse, resp)
    assert model.lock_info is not None
    # /lock/info returns the full structured block (not the {} failure shape) on the happy path.
    assert set(resp["lock_info"].keys()) == set(LockInfo.__fields__), pprint.pformat(resp["lock_info"])
