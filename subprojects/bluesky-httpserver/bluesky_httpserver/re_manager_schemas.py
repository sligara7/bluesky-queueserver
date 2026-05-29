"""Pydantic response models for the RE Manager command routes (``routers/core_api.py``).

Each route returns the raw dict produced by the manager command handler. These models
declare the response shapes so generated SDKs and the static ``openapi.json`` carry real
type information instead of an untyped object.

Two deliberate choices:

* ``extra = "forbid"`` on every model. The manager dicts are the source of truth; if a
  handler grows a new key, the endpoint fails hard with a ResponseValidationError instead
  of FastAPI silently dropping the field. The integration tests exercise the live shapes
  so drift is caught at test time, not in production.
* Scalar ``item`` / ``running_item`` fields are ``Optional`` and ``QueueItem`` fields all
  carry defaults, because the manager returns ``None`` or an empty ``{}`` for these on the
  failure / idle path (e.g. ``item_get`` returns ``item={}`` when the lookup fails). An
  empty dict therefore validates as an all-``None`` ``QueueItem``.

First cut covers the Status, Queue, Queue Items and History tag groups (22 routes). The
remaining groups will be modelled in a follow-up.

The inner ``class Config: extra = "forbid"`` form sets forbid-extra under both pydantic v1
and v2, matching the version-portable style of ``schemas.py``.
"""

from typing import Dict, List, Optional, Union

import pydantic


class RMResponse(pydantic.BaseModel):
    """Base for RE Manager response models: reject unexpected keys (fail-hard on drift)."""

    class Config:
        extra = "forbid"


# --------------------------------------------------------------------------------------
# Shared sub-structures
# --------------------------------------------------------------------------------------


class PlanQueueMode(RMResponse):
    loop: bool
    ignore_failures: bool


class LockState(RMResponse):
    environment: bool
    queue: bool


class QueueItem(RMResponse):
    """A queue or history item.

    ``item_uid`` / ``item_type`` / ``name`` are present on a populated item but every field
    is optional so that an empty ``{}`` (returned by some handlers on failure/idle) validates.
    ``args`` / ``kwargs`` / ``meta`` are user-supplied and ``properties`` is added internally;
    they are kept free-form to avoid drift on user-arbitrary content.
    """

    item_uid: Optional[str] = None
    item_type: Optional[str] = None
    name: Optional[str] = None
    args: Optional[List] = None
    kwargs: Optional[dict] = None
    meta: Optional[dict] = None
    user: Optional[str] = None
    user_group: Optional[str] = None
    properties: Optional[dict] = None


class HistoryItem(QueueItem):
    """A history item: a queue item plus the execution ``result`` block.

    ``result`` is kept free-form in this first cut (tightening it to a typed model is a
    noted follow-up)."""

    result: Optional[dict] = None


# --------------------------------------------------------------------------------------
# Envelope responses
# --------------------------------------------------------------------------------------


class SuccessMsgResponse(RMResponse):
    """Bare ``{success, msg}`` envelope shared by control routes that return no payload."""

    success: bool
    msg: str


class StatusResponse(RMResponse):
    """RE Manager status snapshot (``manager.py`` ``_status_update``). All keys always present."""

    msg: str
    time: str
    items_in_queue: int
    items_in_history: int
    running_item_uid: Optional[str] = None
    manager_state: str
    queue_stop_pending: bool
    queue_autostart_enabled: bool
    worker_environment_exists: bool
    worker_environment_state: str
    worker_background_tasks: int
    re_state: Optional[str] = None
    ip_kernel_state: Optional[str] = None
    ip_kernel_captured: Optional[bool] = None
    pause_pending: bool
    status_uid: str
    run_list_uid: str
    plan_queue_uid: str
    plan_history_uid: str
    devices_existing_uid: str
    plans_existing_uid: str
    devices_allowed_uid: str
    plans_allowed_uid: str
    plan_queue_mode: PlanQueueMode
    task_results_uid: str
    lock_info_uid: str
    lock: LockState


class QueueGetResponse(RMResponse):
    success: bool
    msg: str
    items: List[QueueItem] = []
    running_item: Optional[QueueItem] = None
    plan_queue_uid: str


class ItemResponse(RMResponse):
    """Single-item mutations: item_add, item_execute, item_update, item_remove, item_move."""

    success: bool
    msg: str
    qsize: Optional[int] = None
    item: Optional[QueueItem] = None


class ItemGetResponse(RMResponse):
    success: bool
    msg: str
    item: Optional[QueueItem] = None


class ItemsBatchResponse(RMResponse):
    """Batch mutations: item_add_batch, item_remove_batch, item_move_batch.

    ``results`` (per-item ``{success, msg}``) is only populated by item_add_batch."""

    success: bool
    msg: str
    qsize: Optional[int] = None
    items: List[QueueItem] = []
    results: Optional[List[SuccessMsgResponse]] = None


class HistoryGetResponse(RMResponse):
    success: bool
    msg: str
    items: List[HistoryItem] = []
    plan_history_uid: str


# ======================================================================================
# Follow-on groups: Config / Environment / Run Engine / Runs / Plans / Devices /
# Permissions / Scripts & Functions / Lock / Manager / Testing.
#
# SuccessMsgResponse (above) is reused for every bare {success, msg} route:
#   environment_open/close/destroy, re_pause/resume/stop/abort/halt,
#   permissions_reload, permissions_set, kernel_interrupt, manager_stop,
#   /test/manager/kill, /test/manager/test.
#
# Large nested payloads (config, plans_allowed/existing, devices_allowed/existing,
# user_group_permissions, re_metadata, run_list elements) stay free-form ``dict``/``list``:
# they are deep, plan/device-defined, and modelling them would add a large drift surface
# for little SDK value. The envelope + uid fields are what SDK generators need.
# ======================================================================================


class ConfigGetResponse(RMResponse):
    success: bool
    msg: str
    config: Optional[dict] = None


class TaskUidResponse(RMResponse):
    """Routes that return a task handle: environment_update, script_upload."""

    success: bool
    msg: str
    task_uid: Optional[str] = None


class FunctionExecuteResponse(RMResponse):
    success: bool
    msg: str
    # ``item`` is the echoed function item; kept free-form (function items are only produced
    # against a live worker and are not exercised by the offline test suite).
    item: Optional[dict] = None
    task_uid: Optional[str] = None


class TaskStatusResponse(RMResponse):
    """``task_uid``/``status`` are polymorphic: a single str when one UID was queried, or a
    list/dict keyed by UID when several were queried."""

    success: bool
    msg: str
    task_uid: Optional[Union[str, List[str]]] = None
    status: Optional[Union[str, Dict[str, str]]] = None


class TaskResultResponse(RMResponse):
    success: bool
    msg: str
    task_uid: Optional[str] = None
    status: Optional[str] = None
    result: Optional[dict] = None


class RunsResponse(RMResponse):
    """re_runs (and its active/open/closed views). ``run_list`` elements are free-form run dicts."""

    success: bool
    msg: str
    run_list: List[dict] = []
    run_list_uid: Optional[str] = None


class ReMetadataResponse(RMResponse):
    success: bool
    msg: str
    re_metadata: Optional[dict] = None


class PlansAllowedResponse(RMResponse):
    success: bool
    msg: str
    plans_allowed: Optional[dict] = None
    plans_allowed_uid: Optional[str] = None


class PlansExistingResponse(RMResponse):
    success: bool
    msg: str
    plans_existing: Optional[dict] = None
    plans_existing_uid: Optional[str] = None


class DevicesAllowedResponse(RMResponse):
    success: bool
    msg: str
    devices_allowed: Optional[dict] = None
    devices_allowed_uid: Optional[str] = None


class DevicesExistingResponse(RMResponse):
    success: bool
    msg: str
    devices_existing: Optional[dict] = None
    devices_existing_uid: Optional[str] = None


class PermissionsGetResponse(RMResponse):
    success: bool
    msg: str
    user_group_permissions: Optional[dict] = None


class LockInfo(RMResponse):
    """The structured lock-info block (``_format_lock_info``). All fields optional so the
    empty ``{}`` returned on the failure path validates as an all-``None`` instance."""

    environment: Optional[bool] = None
    queue: Optional[bool] = None
    user: Optional[str] = None
    time: Optional[float] = None
    time_str: Optional[str] = None
    note: Optional[str] = None
    emergency_lock_key_is_set: Optional[bool] = None


class LockResponse(RMResponse):
    """lock, unlock, lock_info."""

    success: bool
    msg: str
    lock_info: Optional[LockInfo] = None
    lock_info_uid: Optional[str] = None


# Console Output -- only the non-streaming JSON routes. The SSE (/stream_console_output)
# and WebSocket (/console_output/ws, /status/ws) routes return streams, not JSON
# envelopes, so they cannot carry a response_model.


class ConsoleOutputResponse(RMResponse):
    success: bool
    msg: str
    text: Optional[str] = None


class ConsoleOutputUidResponse(RMResponse):
    success: bool
    msg: str
    console_output_uid: Optional[str] = None


class ConsoleOutputUpdateResponse(RMResponse):
    success: bool
    msg: str
    last_msg_uid: Optional[str] = None
    console_output_msgs: List[dict] = []
