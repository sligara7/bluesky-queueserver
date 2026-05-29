import asyncio
import io
import logging
import pprint
from typing import Optional

import pydantic
from bluesky_queueserver.manager.conversions import simplify_plan_descriptions, spreadsheet_to_plan_list
from fastapi import APIRouter, Depends, File, Form, Request, Security, UploadFile, WebSocket, WebSocketDisconnect
from packaging import version

if version.parse(pydantic.__version__) < version.parse("2.0.0"):
    from pydantic import BaseSettings
else:
    from pydantic_settings import BaseSettings

from ..authentication import get_current_principal, get_current_principal_websocket
from ..console_output import ConsoleOutputEventStream, StreamingResponseFromClass
from ..re_manager_schemas import (
    ConfigGetResponse,
    ConsoleOutputResponse,
    ConsoleOutputUidResponse,
    ConsoleOutputUpdateResponse,
    DevicesAllowedResponse,
    DevicesExistingResponse,
    FunctionExecuteResponse,
    HistoryGetResponse,
    ItemGetResponse,
    ItemResponse,
    ItemsBatchResponse,
    LockResponse,
    PermissionsGetResponse,
    PlansAllowedResponse,
    PlansExistingResponse,
    QueueGetResponse,
    ReMetadataResponse,
    RunsResponse,
    StatusResponse,
    SuccessMsgResponse,
    TaskResultResponse,
    TaskStatusResponse,
    TaskUidResponse,
)
from ..resources import SERVER_RESOURCES as SR
from ..settings import get_settings
from ..utils import (
    get_api_access_manager,
    get_current_username,
    get_resource_access_manager,
    process_exception,
    validate_payload_keys,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


@router.get(
    "/",
    response_model=StatusResponse,
    response_model_exclude_unset=True,
    summary="Ping the RE Manager (root alias)",
    description=(
        "Returns a minimal response from RE Manager. Same handler as `/api/ping`. "
        "Useful as a basic reachability/liveness check. Required scope: `read:status`."
    ),
    tags=["Status"],
)
@router.get(
    "/ping",
    response_model=StatusResponse,
    response_model_exclude_unset=True,
    summary="Ping the RE Manager",
    description=(
        "Returns a minimal response from RE Manager — a lightweight way to confirm the "
        "server is reachable and the manager process is responsive. Required scope: `read:status`."
    ),
    tags=["Status"],
)
async def ping_handler(payload: dict = {}, principal=Security(get_current_principal, scopes=["read:status"])):
    """
    May be called to get some response from the server. Currently returns status of RE Manager.
    """
    try:
        msg = await SR.RM.ping(**payload)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/status",
    response_model=StatusResponse,
    response_model_exclude_unset=True,
    summary="Get RE Manager status",
    description=(
        "Returns a status snapshot of RE Manager — manager state, environment state, the "
        "currently running item (if any), worker process status, queue/history counts, "
        "plus the UIDs clients use for change detection when polling. "
        "Required scope: `read:status`."
    ),
    tags=["Status"],
)
async def status_handler(
    request: Request,
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["read:status"]),
):
    """
    Returns status of RE Manager.
    """
    request.state.endpoint = "status"
    # logger.info(f"payload = {payload} principal={principal}")
    try:
        msg = await SR.RM.status(**payload)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/config/get",
    response_model=ConfigGetResponse,
    response_model_exclude_unset=True,
    summary="Get manager configuration",
    description=(
        "Returns the manager's client-visible configuration dictionary (the subset of "
        "settings considered safe to expose). Required scope: `read:config`."
    ),
    tags=["Config"],
)
async def queue_config_get(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["read:config"]),
):
    """
    Get manager configuration.
    """
    try:
        msg = await SR.RM.config_get(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/autostart",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Enable or disable queue autostart",
    description=(
        "When autostart is enabled, the queue starts automatically once the environment "
        "is opened, and the manager resumes the queue automatically after a plan pauses. "
        "Parameter: `enable` (bool). Required scope: `write:queue:control`."
    ),
    tags=["Queue"],
)
async def queue_autostart_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:control"]),
):
    """
    Enable or disable queue autostart.
    """
    try:
        msg = await SR.RM.queue_autostart(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/mode/set",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Set queue execution mode",
    description=(
        "Configure queue-level execution options such as `loop` (re-run the queue "
        "indefinitely) or `ignore_failures` (continue after a failed plan). "
        "Parameter: `mode` (dict of option names to values). "
        "Required scope: `write:queue:control`."
    ),
    tags=["Queue"],
)
async def queue_mode_set_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:control"]),
):
    """
    Set queue mode.
    """
    try:
        msg = await SR.RM.queue_mode_set(**payload)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/queue/get",
    response_model=QueueGetResponse,
    response_model_exclude_unset=True,
    summary="Get queue contents",
    description=(
        "Returns the current queue — the list of queued items, the currently running "
        "item (if any), the `plan_queue_uid` used for change detection, and the active "
        "`running_item_uid`. Required scope: `read:queue`."
    ),
    tags=["Queue"],
)
async def queue_get_handler(payload: dict = {}, principal=Security(get_current_principal, scopes=["read:queue"])):
    """
    Returns the contents of the current queue.
    """
    try:
        msg = await SR.RM.queue_get(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/clear",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Clear the queue",
    description=(
        "Remove all items from the queue. The currently running plan is not affected. "
        "Required scope: `write:queue:edit`."
    ),
    tags=["Queue"],
)
async def queue_clear_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:edit"])
):
    """
    Clear the plan queue.
    """
    try:
        msg = await SR.RM.queue_clear(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/start",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Start queue execution",
    description=(
        "Begin executing items from the queue. Additional items can be added to the queue "
        "while it is running. If the queue is empty, the request succeeds and nothing runs. "
        "Required scope: `write:queue:control`."
    ),
    tags=["Queue"],
)
async def queue_start_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:control"])
):
    """
    Start execution of the loaded queue. Additional runs can be added to the queue while
    it is executed. If the queue is empty, then nothing will happen.
    """
    try:
        msg = await SR.RM.queue_start(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/stop",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Request queue stop after current plan",
    description=(
        "Request the queue to stop after the currently running plan completes. The running "
        "plan itself is not interrupted. Rejected if no plan is currently running. "
        "Use `/queue/stop/cancel` to back out of a pending stop request. "
        "Required scope: `write:queue:control`."
    ),
    tags=["Queue"],
)
async def queue_stop(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:control"])
):
    """
    Activate the sequence of stopping the queue. The currently running plan will be completed,
    but the next plan will not be started. The request will be rejected if no plans are currently
    running
    """
    try:
        msg = await SR.RM.queue_stop(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/stop/cancel",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Cancel a pending queue-stop request",
    description=(
        "Cancel a previously-issued `/queue/stop` request while the running plan has not "
        "yet completed. Always succeeds; a no-op if no stop is pending. "
        "Required scope: `write:queue:control`."
    ),
    tags=["Queue"],
)
async def queue_stop_cancel(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:control"])
):
    """
    Cancel pending request to stop the queue while the current plan is still running.
    It may be useful if the `/queue/stop` request was issued by mistake or the operator
    changed his mind. Since `/queue/stop` takes effect only after the currently running
    plan is completed, user may have time to cancel the request and continue execution of
    the queue. The command always succeeds, but it has no effect if no queue stop
    requests are pending.
    """
    try:
        msg = await SR.RM.queue_stop_cancel(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/item/add",
    response_model=ItemResponse,
    response_model_exclude_unset=True,
    summary="Add an item to the queue",
    description=(
        "Add a single plan, instruction, or function to the queue. Parameter: `item` (dict) "
        "describes what to run; `pos` / `before_uid` / `after_uid` optionally control where "
        "in the queue the item is inserted. Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_add_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Adds new plan to the queue
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        if "item" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="queue_item_add", params=payload)
        else:
            msg = await SR.RM.item_add(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/item/execute",
    response_model=ItemResponse,
    response_model_exclude_unset=True,
    summary="Execute an item immediately",
    description=(
        "Execute the supplied item once, outside the queue. The item does not join the queue "
        "and does not appear in queue listings. Parameter: `item` (dict). "
        "Required scope: `write:execute`."
    ),
    tags=["Queue Items"],
)
async def queue_item_execute_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:execute"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Immediately execute an item
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        if "item" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="queue_item_execute", params=payload)
        else:
            msg = await SR.RM.item_execute(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/item/add/batch",
    response_model=ItemsBatchResponse,
    response_model_exclude_unset=True,
    summary="Add a batch of items to the queue",
    description=(
        "Add multiple items to the queue in a single request. Parameter: `items` (list of "
        "item dicts). The server validates each item; per-item success/failure is returned "
        "in the response. Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_add_batch_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Adds new plan to the queue
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        if "items" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="queue_item_add_batch", params=payload)
        else:
            msg = await SR.RM.item_add_batch(**payload)
    except Exception:
        process_exception()

    return msg


@router.post(
    "/queue/upload/spreadsheet",
    response_model=ItemsBatchResponse,
    response_model_exclude_unset=True,
    summary="Upload a spreadsheet and enqueue the resulting plans",
    description=(
        "Multipart upload: a spreadsheet file is processed (either by a user-provided "
        "`spreadsheet_to_plan_list` function in a loaded custom module or by the default "
        "processor) and the resulting plan list is added to the queue as a batch. "
        "Form fields: `spreadsheet` (file), `data_type` (optional str — hint used by custom "
        "processors to pick a parsing strategy). Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_upload_spreadsheet(
    spreadsheet: UploadFile = File(...),
    data_type: Optional[str] = Form(None),
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    The endpoint receives uploaded spreadsheet, converts it to the list of plans and adds
    the plans to the queue.

    Parameters
    ----------
    spreadsheet : File
        uploaded excel file
    data_type : str
        user defined spreadsheet type, which determines which processing function is used to
        process the spreadsheet.

    Returns
    -------
    success : boolean
        Indicates if the spreadsheet was successfully converted to a sequence of plans.
        ``True`` value does not indicate that the plans were accepted by the RE Manager and
        successfully added to the queue.
    msg : str
        Error message in case of failure to process the spreadsheet
    item_list : list(dict)
        The list of parameter dictionaries returned by RE Manager in response to requests
        to add each plan in the list. Check ``success`` parameter in each dictionary to
        see if the plan was accepted and ``msg`` parameter for an error message in case
        the plan was rejected. The list may be empty if the spreadsheet contains no items
        or processing of the spreadsheet failed.
    """
    try:
        # Create fully functional file object. The file object returned by FastAPI is not fully functional.
        f = io.BytesIO(spreadsheet.file.read())
        # File name is also passed to the processing function (may be useful in user created
        #   processing code, since processing may differ based on extension or file name)
        f_name = spreadsheet.filename
        logger.info(f"Spreadsheet file '{f_name}' was uploaded")

        # Determine which processing function should be used
        item_list = []
        processed = False

        # Select custom module from the list of loaded modules
        custom_module = None
        for module in SR.custom_code_modules:
            if "spreadsheet_to_plan_list" in module.__dict__:
                custom_module = module
                break

        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)

        if custom_module:
            logger.info("Processing spreadsheet using function from external module ...")
            # Try applying  the custom processing function. Some additional useful data is passed to
            #   the function. Unnecessary parameters can be ignored.
            item_list = custom_module.spreadsheet_to_plan_list(
                spreadsheet_file=f, file_name=f_name, data_type=data_type, user=username
            )
            # The function is expected to return None if it rejects the file (based on 'data_type').
            #   Then try to apply the default processing function.
            processed = item_list is not None

        if not processed:
            # Apply default spreadsheet processing function.
            logger.info("Processing spreadsheet using default function ...")
            item_list = spreadsheet_to_plan_list(
                spreadsheet_file=f, file_name=f_name, data_type=data_type, user=username
            )

        if item_list is None:
            raise RuntimeError("Failed to process the spreadsheet: unsupported data type or format")

        # Since 'item_list' may be returned by user defined functions, verify the type of the list.
        if not isinstance(item_list, (tuple, list)):
            raise ValueError(
                f"Spreadsheet processing function returned value of '{type(item_list)}' "
                f"type instead of 'list' or 'tuple'"
            )

        # Ensure, that 'item_list' is sent as a list
        item_list = list(item_list)

        # Set item type for all items that don't have item type already set (item list may contain
        #   instructions, but it is responsibility of the user to set item types correctly.
        #   By default an item is considered a plan.
        for item in item_list:
            if "item_type" not in item:
                item["item_type"] = "plan"

        logger.debug("The following plans were created: %s", pprint.pformat(item_list))
    except Exception as ex:
        msg = {"success": False, "msg": str(ex), "items": [], "results": []}
    else:
        try:
            params = {"user": displayed_name, "user_group": user_group}
            params["items"] = item_list
            msg = await SR.RM.item_add_batch(**params)
        except Exception:
            process_exception()
    return msg


@router.post(
    "/queue/item/update",
    response_model=ItemResponse,
    response_model_exclude_unset=True,
    summary="Update an existing queue item",
    description=(
        "Replace or patch an existing queue item (identified by `item_uid`) with a new "
        "specification. Rejected if the item is not in the queue or is currently running. "
        "Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_update_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Update existing plan in the queue
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        msg = await SR.RM.item_update(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/item/remove",
    response_model=ItemResponse,
    response_model_exclude_unset=True,
    summary="Remove an item from the queue",
    description=(
        "Remove a single item from the queue by position (`pos`) or UID (`uid`). "
        "Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_remove_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
):
    """
    Remove plan from the queue
    """
    try:
        msg = await SR.RM.item_remove(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/item/remove/batch",
    response_model=ItemsBatchResponse,
    response_model_exclude_unset=True,
    summary="Remove a batch of items from the queue",
    description=(
        "Remove multiple items from the queue in a single request. Parameter: `uids` (list "
        "of item UIDs). Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_remove_batch_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
):
    """
    Remove a batch of plans from the queue
    """
    try:
        if "uids" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="queue_item_remove_batch", params=payload)
        else:
            msg = await SR.RM.item_remove_batch(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/item/move",
    response_model=ItemResponse,
    response_model_exclude_unset=True,
    summary="Move an item within the queue",
    description=(
        "Reposition a queue item. Source selected by `pos` or `uid`; destination by `pos_dest`, "
        "`before_uid`, or `after_uid`. Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_move_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
):
    """
    Move plan in the queue
    """
    try:
        msg = await SR.RM.item_move(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/queue/item/move/batch",
    response_model=ItemsBatchResponse,
    response_model_exclude_unset=True,
    summary="Move a batch of items within the queue",
    description=(
        "Reposition multiple items in the queue in a single request. Parameter: `uids` (list "
        "of item UIDs) plus a destination selector (`pos_dest`, `before_uid`, or `after_uid`). "
        "Required scope: `write:queue:edit`."
    ),
    tags=["Queue Items"],
)
async def queue_item_move_batch_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:queue:edit"]),
):
    """
    Move a batch of plans in the queue
    """
    try:
        msg = await SR.RM.item_move_batch(**payload)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/queue/item/get",
    response_model=ItemGetResponse,
    response_model_exclude_unset=True,
    summary="Get a single queue item",
    description=(
        "Returns details for a single queue item by position (`pos`) or UID (`uid`). "
        "Required scope: `read:queue`."
    ),
    tags=["Queue Items"],
)
async def queue_item_get_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["read:queue"])
):
    """
    Get a plan from the queue
    """
    try:
        msg = await SR.RM.item_get(**payload)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/history/get",
    response_model=HistoryGetResponse,
    response_model_exclude_unset=True,
    summary="Get plan history",
    description=(
        "Returns the list of completed plans in chronological order, plus the `plan_history_uid` "
        "for change detection. Required scope: `read:history`."
    ),
    tags=["History"],
)
async def history_get_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["read:history"])
):
    """
    Returns the plan history (list of dicts).
    """
    try:
        msg = await SR.RM.history_get(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/history/clear",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Clear plan history",
    description=(
        "Remove all entries from the plan-history buffer. "
        "Required scope: `write:history:edit`."
    ),
    tags=["History"],
)
async def history_clear_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:history:edit"])
):
    """
    Clear plan history.
    """
    try:
        msg = await SR.RM.history_clear(**payload)
    except Exception:
        process_exception()

    return msg


@router.post(
    "/environment/open",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Open the RE environment",
    description=(
        "Spawn the RE Worker subprocess and initialize the Run Engine. Required before the "
        "queue can execute plans or before scripts/functions can be uploaded. "
        "Required scope: `write:manager:control`."
    ),
    tags=["Environment"],
)
async def environment_open_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:manager:control"])
):
    """
    Creates RE environment: creates RE Worker process, starts and configures Run Engine.
    """
    try:
        msg = await SR.RM.environment_open(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/environment/close",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Close the RE environment cleanly",
    description=(
        "Orderly shutdown of the RE Worker. Rejected if a plan is currently running — call "
        "`/queue/stop` or `/re/stop` first, or use `/environment/destroy` for a forceful "
        "shutdown. Required scope: `write:manager:control`."
    ),
    tags=["Environment"],
)
async def environment_close_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:manager:control"])
):
    """
    Orderly closes of RE environment. The command returns success only if no plan is running,
    i.e. RE Manager is in the idle state. The command is rejected if a plan is running.
    """
    try:
        msg = await SR.RM.environment_close(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/environment/destroy",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Forcefully destroy the RE environment",
    description=(
        "Kill the RE Worker process without waiting for the running plan to complete. "
        "Last-resort recovery path — intended for expert operators when the worker is hung "
        "and cannot be stopped cleanly. Required scope: `write:manager:control`."
    ),
    tags=["Environment"],
)
async def environment_destroy_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:manager:control"])
):
    """
    Destroys RE environment by killing RE Worker process. This is a last resort command which
    should be made available only to expert level users.
    """
    try:
        msg = await SR.RM.environment_destroy(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/environment/update",
    response_model=TaskUidResponse,
    response_model_exclude_unset=True,
    summary="Refresh environment caches",
    description=(
        "Refresh manager-side caches of plans, devices, and namespace metadata from the "
        "running worker. Call after uploading a script that adds or redefines plans/devices "
        "so subsequent `/plans/*` and `/devices/*` responses reflect the change. "
        "Required scope: `write:queue:control`."
    ),
    tags=["Environment"],
)
async def environment_update_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:control"])
):
    """
    Updates RE environment cache.
    """
    try:
        msg = await SR.RM.environment_update(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/re/pause",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Pause the Run Engine",
    description=(
        "Pause the currently running plan. Parameter: `option` — `'deferred'` (pause at the "
        "next checkpoint, safe) or `'immediate'` (pause at the next safe point). "
        "Required scope: `write:plan:control`."
    ),
    tags=["Run Engine"],
)
async def re_pause_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["write:plan:control"]),
):
    """
    Pause Run Engine.
    """
    try:
        msg = await SR.RM.re_pause(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/re/resume",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Resume a paused plan",
    description=(
        "Resume execution of the currently paused plan. "
        "Required scope: `write:plan:control`."
    ),
    tags=["Run Engine"],
)
async def re_resume_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:plan:control"])
):
    """
    Run Engine: resume execution of a paused plan
    """
    try:
        msg = await SR.RM.re_resume(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/re/stop",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Stop a paused plan cleanly",
    description=(
        "Stop the currently paused plan. The plan is marked as successfully completed from "
        "the Run Engine's perspective. Required scope: `write:plan:control`."
    ),
    tags=["Run Engine"],
)
async def re_stop_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:plan:control"])
):
    """
    Run Engine: stop execution of a paused plan
    """
    try:
        msg = await SR.RM.re_stop(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/re/abort",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Abort a paused plan",
    description=(
        "Abort the currently paused plan. The plan is marked as failed, but Run Engine "
        "cleanup handlers still run (devices are returned to safe states). "
        "Required scope: `write:plan:control`."
    ),
    tags=["Run Engine"],
)
async def re_abort_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:plan:control"])
):
    """
    Run Engine: abort execution of a paused plan
    """
    try:
        msg = await SR.RM.re_abort(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/re/halt",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Halt a paused plan (no cleanup)",
    description=(
        "Halt the currently paused plan immediately without running cleanup handlers. More "
        "aggressive than `/re/abort` — use when cleanup itself is misbehaving. "
        "Required scope: `write:plan:control`."
    ),
    tags=["Run Engine"],
)
async def re_halt_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:plan:control"])
):
    """
    Run Engine: halt execution of a paused plan
    """
    try:
        msg = await SR.RM.re_halt(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/re/runs",
    response_model=RunsResponse,
    response_model_exclude_unset=True,
    summary="List runs produced by the current plan",
    description=(
        "Returns runs opened during the currently running plan. Parameter: `option` selects "
        "`'active'` (all), `'open'`, or `'closed'`; default `'active'`. See "
        "`/re/runs/active`, `/re/runs/open`, `/re/runs/closed` for convenience aliases. "
        "Required scope: `read:monitor`."
    ),
    tags=["Runs"],
)
async def re_runs_handler(payload: dict = {}, principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Run Engine: download the list of active, open or closed runs (runs that were opened
    during execution of the currently running plan and combines the subsets of 'open' and
    'closed' runs.) The parameter ``options`` is used to select the category of runs
    (``'active'``, ``'open'`` or ``'closed'``). By default the API returns the active runs.
    """
    try:
        msg = await SR.RM.re_runs(**payload)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/re/runs/active",
    response_model=RunsResponse,
    response_model_exclude_unset=True,
    summary="List all runs produced by the current plan",
    description=(
        "Convenience alias for `POST /re/runs` with `option='active'`. Returns runs opened "
        "during the currently running plan (both open and closed). "
        "Required scope: `read:monitor`."
    ),
    tags=["Runs"],
)
async def re_runs_active_handler(principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Run Engine: download the list of active runs (runs that were opened during execution of
    the currently running plan and combines the subsets of 'open' and 'closed' runs.)
    """
    try:
        params = {"option": "active"}
        msg = await SR.RM.re_runs(**params)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/re/runs/open",
    response_model=RunsResponse,
    response_model_exclude_unset=True,
    summary="List open runs produced by the current plan",
    description=(
        "Convenience alias for `POST /re/runs` with `option='open'`. Returns the subset of "
        "active runs that have been opened but not yet closed. "
        "Required scope: `read:monitor`."
    ),
    tags=["Runs"],
)
async def re_runs_open_handler(principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Run Engine: download the subset of active runs that includes runs that were open, but not yet closed.
    """
    try:
        params = {"option": "open"}
        msg = await SR.RM.re_runs(**params)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/re/runs/closed",
    response_model=RunsResponse,
    response_model_exclude_unset=True,
    summary="List closed runs produced by the current plan",
    description=(
        "Convenience alias for `POST /re/runs` with `option='closed'`. Returns runs from "
        "the current plan that have been closed. Required scope: `read:monitor`."
    ),
    tags=["Runs"],
)
async def re_runs_closed_handler(principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Run Engine: download the subset of active runs that includes runs that were already closed.
    """
    try:
        params = {"option": "closed"}
        msg = await SR.RM.re_runs(**params)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/re/metadata",
    response_model=ReMetadataResponse,
    response_model_exclude_unset=True,
    summary="Get metadata of the currently running plan",
    description=(
        "Returns the metadata of the plan currently executing in the Run Engine "
        "(run-specific kwargs, scan_id, etc.). Required scope: `read:monitor`."
    ),
    tags=["Runs"],
)
async def re_metadata(payload: dict = {}, principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Run Engine: download the metadata of the currently running plan.
    """
    try:
        msg = await SR.RM.re_metadata(**payload)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/plans/allowed",
    response_model=PlansAllowedResponse,
    response_model_exclude_unset=True,
    summary="List plans allowed for the current user",
    description=(
        "Returns plans the current user's resource group is permitted to execute. "
        "Parameter: `reduced` (bool, default `False`) — when `True`, plan descriptions "
        "are simplified to save bandwidth. Required scope: `read:resources`."
    ),
    tags=["Plans"],
)
async def plans_allowed_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["read:resources"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Returns the lists of allowed plans. If boolean optional parameter ``reduced``
    is ``True``(default value is ``False`), then simplify plan descriptions before
    calling the API.
    """

    try:
        validate_payload_keys(payload, optional_keys=["reduced"])

        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        user_group = resource_access_manager.get_resource_group(username)

        if "reduced" in payload:
            reduced = payload["reduced"]
            del payload["reduced"]
        else:
            reduced = False
        payload.update({"user_group": user_group})

        msg = await SR.RM.plans_allowed(**payload)
        if reduced and ("plans_allowed" in msg):
            msg["plans_allowed"] = simplify_plan_descriptions(msg["plans_allowed"])
    except Exception:
        process_exception()
    return msg


@router.get(
    "/devices/allowed",
    response_model=DevicesAllowedResponse,
    response_model_exclude_unset=True,
    summary="List devices allowed for the current user",
    description=(
        "Returns devices the current user's resource group is permitted to use. "
        "Required scope: `read:resources`."
    ),
    tags=["Devices"],
)
async def devices_allowed_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["read:resources"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Returns the lists of allowed devices.
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        user_group = resource_access_manager.get_resource_group(username)

        payload.update({"user_group": user_group})

        msg = await SR.RM.devices_allowed(**payload)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/plans/existing",
    response_model=PlansExistingResponse,
    response_model_exclude_unset=True,
    summary="List all plans registered in the worker",
    description=(
        "Returns all plans registered in the worker namespace, not filtered by user "
        "permissions. Parameter: `reduced` (bool, default `False`) — when `True`, plan "
        "descriptions are simplified to save bandwidth."
    ),
    tags=["Plans"],
)
async def plans_existing_handler(
    payload: dict = {},
):
    """
    Returns the lists of existing plans. If boolean optional parameter ``reduced``
    is ``True``(default value is ``False`), then simplify plan descriptions before
    calling the API.
    """
    try:
        validate_payload_keys(payload, optional_keys=["reduced"])

        if "reduced" in payload:
            reduced = payload["reduced"]
            del payload["reduced"]
        else:
            reduced = False

        msg = await SR.RM.plans_existing(**payload)
        if reduced and ("plans_existing" in msg):
            msg["plans_existing"] = simplify_plan_descriptions(msg["plans_existing"])
    except Exception:
        process_exception()

    return msg


@router.get(
    "/devices/existing",
    response_model=DevicesExistingResponse,
    response_model_exclude_unset=True,
    summary="List all devices registered in the worker",
    description=(
        "Returns all devices registered in the worker namespace, not filtered by user "
        "permissions. Required scope: `read:resources`."
    ),
    tags=["Devices"],
)
async def devices_existing_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["read:resources"]),
):
    """
    Returns the lists of existing devices.
    """
    try:
        msg = await SR.RM.devices_existing(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/permissions/reload",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Reload permissions from disk",
    description=(
        "Reload allowed-plans, allowed-devices, and user-group-permissions definitions from "
        "the paths configured on the manager. Use after editing the underlying files on "
        "disk. Required scope: `write:config`."
    ),
    tags=["Permissions"],
)
async def permissions_reload_handler(
    payload: dict = {},
    principal=Security(get_current_principal, scopes=["write:config"]),
):
    """
    Reloads the list of allowed plans and devices and user group permission from the default location
    or location set using command line parameters of RE Manager. Use this request to reload the data
    if the respective files were changed on disk.
    """
    try:
        msg = await SR.RM.permissions_reload(**payload)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/permissions/get",
    response_model=PermissionsGetResponse,
    response_model_exclude_unset=True,
    summary="Get user-group permissions",
    description=(
        "Returns the current user-group permissions dictionary. "
        "Required scope: `read:config`."
    ),
    tags=["Permissions"],
)
async def permissions_get_handler(principal=Security(get_current_principal, scopes=["read:config"])):
    """
    Download the dictionary of user group permissions.
    """
    try:
        msg = await SR.RM.permissions_get()
    except Exception:
        process_exception()
    return msg


@router.post(
    "/permissions/set",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Set user-group permissions",
    description=(
        "Replace the current user-group permissions. Parameter: `user_group_permissions` "
        "(dict). Required scope: `write:permissions`."
    ),
    tags=["Permissions"],
)
async def permissions_set_handler(
    payload: dict, principal=Security(get_current_principal, scopes=["write:permissions", "write:permissions"])
):
    """
    Upload the dictionary of user group permissions (parameter: ``user_group_permissions``).
    """
    try:
        if "user_group_permissions" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="permissions_set", params=payload)
        else:
            msg = await SR.RM.permissions_set(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/function/execute",
    response_model=FunctionExecuteResponse,
    response_model_exclude_unset=True,
    summary="Execute a function in the worker",
    description=(
        "Execute a function defined in the worker's startup scripts. Parameter: `item` "
        "(function-item spec with `name`, `args`, `kwargs`). Returns a `task_uid` — poll "
        "`/task/status` and `/task/result` for progress and output. "
        "Required scope: `write:execute`."
    ),
    tags=["Scripts & Functions"],
)
async def function_execute_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:execute"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
    resource_access_manager=Depends(get_resource_access_manager),
):
    """
    Execute function defined in startup scripts in RE Worker environment.
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        user_group = resource_access_manager.get_resource_group(username)
        payload.update({"user": displayed_name, "user_group": user_group})

        if "item" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="function_execute", params=payload)
        else:
            msg = await SR.RM.function_execute(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/script/upload",
    response_model=TaskUidResponse,
    response_model_exclude_unset=True,
    summary="Upload and execute a Python script in the worker",
    description=(
        "Send a Python source string to the worker for execution. Parameter: `script` (str). "
        "Side-effects (new plans, new devices, redefined functions) are visible in "
        "subsequent calls after an `/environment/update`. Returns a `task_uid`. "
        "Required scope: `write:scripts`."
    ),
    tags=["Scripts & Functions"],
)
async def script_upload_handler(
    payload: dict, principal=Security(get_current_principal, scopes=["write:scripts"])
):
    """
    Upload and execute script in RE Worker environment.
    """
    try:
        if "script" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="script_upload", params=payload)
        else:
            msg = await SR.RM.script_upload(**payload)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/task/status",
    response_model=TaskStatusResponse,
    response_model_exclude_unset=True,
    summary="Get status of one or more worker tasks",
    description=(
        "Returns the status of tasks started via `/function/execute` or `/script/upload`. "
        "Parameter: `task_uid` (str for a single task, or list of str for multiple). "
        "Required scope: `read:monitor`."
    ),
    tags=["Scripts & Functions"],
)
async def task_status(payload: dict, principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Return status of one or more running tasks.
    """
    try:
        if "task_uid" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="task_status", params=payload)
        else:
            msg = await SR.RM.task_status(**payload)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/task/result",
    response_model=TaskResultResponse,
    response_model_exclude_unset=True,
    summary="Get result of a worker task",
    description=(
        "Returns the result (or error) of a completed task, or the in-progress status if "
        "still running. Parameter: `task_uid` (str). "
        "Required scope: `read:monitor`."
    ),
    tags=["Scripts & Functions"],
)
async def task_result(payload: dict, principal=Security(get_current_principal, scopes=["read:monitor"])):
    """
    Return result of execution of a running or completed task.
    """
    try:
        if "task_uid" not in payload:
            # We can not use API, so let the server handle the parameters
            msg = await SR.RM.send_request(method="task_result", params=payload)
        else:
            msg = await SR.RM.task_result(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/kernel/interrupt",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Interrupt the worker IPython kernel",
    description=(
        "Send a keyboard-interrupt to the IPython-kernel-based worker. No-op for worker "
        "configurations that do not use an IPython kernel. "
        "Required scope: `write:queue:control`."
    ),
    tags=["Scripts & Functions"],
)
async def kernel_interrupt_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:queue:control"])
):
    """
    Interrupt IPython kernel.
    """
    try:
        msg = await SR.RM.kernel_interrupt(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/lock",
    response_model=LockResponse,
    response_model_exclude_unset=True,
    summary="Acquire the manager lock",
    description=(
        "Acquire an exclusive lock on RE Manager, preventing other users from altering "
        "locked resources. Parameters: `lock_key` (str, required to unlock later), `note` "
        "(str, description shown to other users), `scope` (list of `'environment'` and/or "
        "`'queue'`). Required scope: `write:lock`."
    ),
    tags=["Lock"],
)
async def lock_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:lock"]),
    settings: BaseSettings = Depends(get_settings),
    api_access_manager=Depends(get_api_access_manager),
):
    """
    Lock RE Manager.
    """
    try:
        username = get_current_username(
            principal=principal, settings=settings, api_access_manager=api_access_manager
        )[0]
        displayed_name = api_access_manager.get_displayed_user_name(username)
        payload.update({"user": displayed_name})

        msg = await SR.RM.lock(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/unlock",
    response_model=LockResponse,
    response_model_exclude_unset=True,
    summary="Release the manager lock",
    description=(
        "Release a previously-acquired manager lock. Parameter: `lock_key` (must match the "
        "value used at lock time). Required scope: `write:lock`."
    ),
    tags=["Lock"],
)
async def unlock_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["write:lock"]),
):
    """
    Unlock RE Manager.
    """
    try:
        msg = await SR.RM.unlock(**payload)
    except Exception:
        process_exception()
    return msg


@router.get(
    "/lock/info",
    response_model=LockResponse,
    response_model_exclude_unset=True,
    summary="Get current manager lock state",
    description=(
        "Returns the current lock state: who holds the lock, when it was acquired, the "
        "associated note, and which scopes are locked. "
        "Required scope: `read:lock`."
    ),
    tags=["Lock"],
)
async def lock_info_handler(
    payload: dict,
    principal=Security(get_current_principal, scopes=["read:lock"]),
):
    """
    Get current manager lock state.
    """
    try:
        msg = await SR.RM.lock_info(**payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/manager/stop",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Stop the RE Manager",
    description=(
        "Stop RE Manager. Unlike crash-and-restart behaviour, the manager will NOT be "
        "auto-restarted by the watchdog after a stop issued via this endpoint. "
        "Required scope: `write:manager:stop`."
    ),
    tags=["Manager"],
)
async def manager_stop_handler(
    payload: dict = {}, principal=Security(get_current_principal, scopes=["write:manager:stop"])
):
    """
    Stops of RE Manager. RE Manager will not be restarted after it is stoped.
    """
    try:
        msg = await SR.RM.send_request(method="manager_stop", params=payload)
    except Exception:
        process_exception()
    return msg


@router.post(
    "/test/manager/kill",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Kill the manager event loop (testing only)",
    description=(
        "Halt the manager event loop to test client-side timeout handling and watchdog "
        "restart behaviour. Not for production use. "
        "Required scope: `write:testing`."
    ),
    tags=["Testing"],
)
async def test_manager_kill_handler(principal=Security(get_current_principal, scopes=["write:testing"])):
    """
    The command stops event loop of RE Manager process. Used for testing of RE Manager
    stability and handling of communication timeouts.
    """
    try:
        msg = await SR.RM.send_request(method="manager_kill")
    except Exception:
        process_exception()
    return msg


@router.get(
    "/test/server/sleep",
    response_model=SuccessMsgResponse,
    response_model_exclude_unset=True,
    summary="Sleep on the server (testing only)",
    description=(
        "Sleep for `time` seconds then return success. Does not block the event loop or "
        "manager calls. Used to exercise client timeout handling. "
        "Required scope: `read:testing`."
    ),
    tags=["Testing"],
)
async def test_server_sleep_handler(
    payload: dict, principal=Security(get_current_principal, scopes=["read:testing"])
):
    """
    The API is intended for testing how the client applications and API libraries handle timeouts.
    The handler waits for the requested number of seconds and then returns the message indicating success.
    The API call is safe, since it does not block the event loop or calls to RE Manager
    """
    try:
        if "time" not in payload:
            raise IndexError(f"The required parameter 'time' is missing in the API call: {payload}")
        sleep_time = payload["time"]
        await asyncio.sleep(sleep_time)
        msg = {"success": True, "msg": ""}
    except Exception:
        process_exception()
    return msg


@router.get(
    "/stream_console_output",
    summary="Stream captured console output (Server-Sent Events)",
    description=(
        "Returns a text/event-stream of captured worker stdout/stderr. The connection "
        "stays open and the client reads lines as they arrive. "
        "Required scope: `read:console`."
    ),
    tags=["Console Output"],
)
def stream_console_output(principal=Security(get_current_principal, scopes=["read:console"])):
    queues_set = SR.console_output_loader.queues_set
    stm = ConsoleOutputEventStream(queues_set=queues_set)
    sr = StreamingResponseFromClass(stm, media_type="text/plain")
    return sr


@router.get(
    "/console_output",
    response_model=ConsoleOutputResponse,
    response_model_exclude_unset=True,
    summary="Get buffered console output",
    description=(
        "Returns the most recent lines of captured worker console output as a text blob. "
        "Parameter: `nlines` (int, default 200). Required scope: `read:console`."
    ),
    tags=["Console Output"],
)
async def console_output(payload: dict = {}, principal=Security(get_current_principal, scopes=["read:console"])):
    try:
        n_lines = payload.get("nlines", 200)
        text = await SR.console_output_loader.get_text_buffer(n_lines)
    except Exception:
        process_exception()

    # Add 'success' and 'msg' so that the API is compatible with other QServer API.
    return {"success": True, "msg": "", "text": text}


@router.get(
    "/console_output/uid",
    response_model=ConsoleOutputUidResponse,
    response_model_exclude_unset=True,
    summary="Get the console-output buffer UID",
    description=(
        "Returns the UID of the current console-output buffer. Pair with `/console_output` "
        "to detect when the buffer has been reset (for example after the environment is "
        "restarted). Required scope: `read:console`."
    ),
    tags=["Console Output"],
)
def console_output_uid(principal=Security(get_current_principal, scopes=["read:console"])):
    """
    UID of the text buffer. Use with ``console_output`` API.
    """
    try:
        uid = SR.console_output_loader.text_buffer_uid
    except Exception:
        process_exception()
    return {"success": True, "msg": "", "console_output_uid": uid}


@router.get(
    "/console_output_update",
    response_model=ConsoleOutputUpdateResponse,
    response_model_exclude_unset=True,
    summary="Fetch new console messages since last UID",
    description=(
        "Returns console-output messages accumulated since the `last_msg_uid` supplied by "
        "the caller. Initialize with `'ALL'` to receive all buffered messages; on each "
        "subsequent call, pass back the UID from the previous response. If the UID is not "
        "found in the buffer (rollover), an empty message list and a fresh UID are "
        "returned. Required scope: `read:console`."
    ),
    tags=["Console Output"],
)
def console_output_update(payload: dict, principal=Security(get_current_principal, scopes=["read:console"])):
    """
    Download the list of new messages that were accumulated at the server. The API
    accepts a required parameter ``last_msg_uid`` with UID of the last downloaded message.
    If the UID is not found in the buffer, an empty message list and valid UID is
    returned. If UID is ``"ALL"``, then all accumulated messages in the buffer is
    returned. If UID is found in the buffer, then the list of new messages is returned.

    At the client: initialize the system by sending request with ``last_msg_uid`` set
    to random string or ``"ALL"``. In each request use ``last_msg_uid`` returned by the previous
    request to download new messages.
    """
    try:
        validate_payload_keys(payload, required_keys=["last_msg_uid"])

        response = SR.console_output_loader.get_new_msgs(last_msg_uid=payload["last_msg_uid"])
        # Add 'success' and 'msg' so that the API is compatible with other QServer API.
        response.update({"success": True, "msg": ""})
    except Exception:
        process_exception()

    return response


class WebSocketMonitor:
    """
    Works for sockets that only send data to clients (not receive).

    The class monitors the status of a socket connection. The property 'is_alive' returns True
    until the socket is disconnected. The purpose of the class is to break the loop in the
    implementation of the socket that only sends data to a client when the application
    is closed. If there is no data to send, the loop continues to run indefinitely and
    prevents the application from closing properly. No better solution was found.
    """

    def __init__(self, websocket):
        self._websocket = websocket
        self._is_alive = True
        self._task_ref = None

    async def _task(self):
        while True:
            try:
                await asyncio.sleep(1)
                try:
                    # The following will raise an exception if the socket is disconnected.
                    await asyncio.wait_for(self._websocket.receive(), timeout=0.01)
                except asyncio.TimeoutError:
                    # The socket is still connected.
                    pass
            except Exception:
                self._is_alive = False
                break

    def start(self):
        self._task_ref = asyncio.create_task(self._task())

    @property
    def is_alive(self):
        return self._is_alive


@router.websocket("/console_output/ws")
async def console_output_ws(websocket: WebSocket, scopes=["read:console"]):
    principal = get_current_principal_websocket(websocket=websocket, scopes=scopes)
    if not principal:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()
    q = SR.console_output_stream.add_queue(websocket)
    wsmon = WebSocketMonitor(websocket)
    wsmon.start()
    try:
        while wsmon.is_alive:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=1)
                await websocket.send_text(msg)
            except asyncio.TimeoutError:
                pass
            except RuntimeError:  # 'send' after the client is disconnected
                pass
    except WebSocketDisconnect:
        pass
    finally:
        SR.console_output_stream.remove_queue(websocket)


@router.websocket("/status/ws")
async def status_ws(websocket: WebSocket, scopes=["read:monitor"]):
    principal = get_current_principal_websocket(websocket=websocket, scopes=scopes)
    if not principal:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()
    q = SR.system_info_stream.add_queue_status(websocket)
    wsmon = WebSocketMonitor(websocket)
    wsmon.start()

    try:
        while wsmon.is_alive:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=1)
                await websocket.send_text(msg)
            except asyncio.TimeoutError:
                pass
            except RuntimeError:  # 'send' after the client is disconnected
                pass
    except WebSocketDisconnect:
        pass
    finally:
        SR.system_info_stream.remove_queue_status(websocket)


@router.websocket("/info/ws")
async def info_ws(websocket: WebSocket, scopes=["read:monitor"]):
    principal = get_current_principal_websocket(websocket=websocket, scopes=scopes)
    if not principal:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()
    q = SR.system_info_stream.add_queue_info(websocket)
    wsmon = WebSocketMonitor(websocket)
    wsmon.start()
    try:
        while wsmon.is_alive:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=1)
                await websocket.send_text(msg)
            except asyncio.TimeoutError:
                pass
            except RuntimeError:  # 'send' after the client is disconnected
                pass
    except WebSocketDisconnect:
        pass
    finally:
        SR.system_info_stream.remove_queue_info(websocket)
