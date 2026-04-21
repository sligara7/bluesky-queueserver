"""
HTTP client for the bluesky-configuration-service.

This module is imported only when the ``config_service`` feature is enabled
in the server configuration. Nothing at module scope pulls in ``httpx`` or
touches the network; the legacy code path stays clean when the feature is
off.

Policy (see feedback_backwards_compat memory):
- Enabled vs. disabled is a binary operator choice. There is NO "enabled but
  silently degraded" runtime state.
- Transient network/upstream errors (ConnectError, ReadTimeout, 502/503/504)
  are retried up to ``max_attempts`` times with small linear backoff; each
  retry is logged at WARNING. On exhaustion, the last exception is raised.
- All other errors (4xx, 5xx other than 502/503/504, malformed JSON) raise
  immediately without retry.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_MS = (200, 400)
DEFAULT_SERVICE_NAME = "bluesky-queueserver"

_RETRYABLE_HTTP_STATUS = frozenset({502, 503, 504})


class ConfigServiceError(Exception):
    """Base class for configuration-service client errors."""


class ConfigServiceUnreachable(ConfigServiceError):
    """Network-level failures that persisted through all retry attempts."""


class ConfigServiceHTTPError(ConfigServiceError):
    """HTTP error response (status code, body)."""

    def __init__(self, status_code: int, body: Any, message: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"HTTP {status_code}: {body!r}")


class ConfigServiceConflict(ConfigServiceHTTPError):
    """409 — resource already exists, lock already held by another owner, etc."""


class ConfigServiceNotFound(ConfigServiceHTTPError):
    """404 — resource does not exist."""


class ConfigServiceProtocolError(ConfigServiceError):
    """Unexpected response shape (missing fields, wrong types)."""


@dataclasses.dataclass(frozen=True)
class ConfigServiceState:
    """State captured at env-open and advanced by the pre-plan staleness check.

    ``cursor`` is the audit-log id the client has already applied; the next
    /devices/changes call uses it as ``since_version``. ``epoch`` is the
    service-instance identifier; a mismatch on a later call means the cursor
    is invalid and the client must re-fetch the full registry.
    """

    cursor: int = 0
    epoch: str = ""


@dataclasses.dataclass(frozen=True)
class ConfigServiceSettings:
    """Parsed ``config_service`` section of the server configuration."""

    enabled: bool = False
    url: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_ms: Tuple[int, ...] = DEFAULT_BACKOFF_MS
    service_name: str = DEFAULT_SERVICE_NAME

    @classmethod
    def from_config_dict(cls, section: Optional[Dict[str, Any]]) -> "ConfigServiceSettings":
        if not section:
            return cls()
        enabled = bool(section.get("enabled", False))
        if not enabled:
            return cls(enabled=False)

        if "url" not in section or not section["url"]:
            raise ValueError(
                "config_service.enabled is true but config_service.url is not set. "
                "Set url explicitly (there is no default)."
            )
        url = section["url"]
        timeout = float(section.get("timeout", DEFAULT_TIMEOUT_SECONDS))

        max_attempts = int(section.get("max_attempts", DEFAULT_MAX_ATTEMPTS))
        if max_attempts < 1:
            raise ValueError(
                f"config_service.max_attempts must be >= 1 (got {max_attempts!r})"
            )

        raw_backoff = section.get("backoff_ms", list(DEFAULT_BACKOFF_MS))
        if not isinstance(raw_backoff, (list, tuple)):
            raise ValueError(
                "config_service.backoff_ms must be a list of integers "
                f"(got {raw_backoff!r})"
            )
        backoff_ms = tuple(int(x) for x in raw_backoff)

        service_name = str(section.get("service_name", DEFAULT_SERVICE_NAME))

        return cls(
            enabled=True,
            url=url,
            timeout=timeout,
            max_attempts=max_attempts,
            backoff_ms=backoff_ms,
            service_name=service_name,
        )


class ConfigServiceClient:
    """Async REST client for bluesky-configuration-service.

    Construct only when ``settings.enabled`` is True. The constructor
    lazy-imports ``httpx``; callers on the legacy path should never reach
    this class, so ``httpx`` remains out of the legacy dependency graph.
    """

    def __init__(
        self,
        settings: ConfigServiceSettings,
        *,
        transport: Any = None,
    ) -> None:
        if not settings.enabled:
            raise ValueError(
                "ConfigServiceClient constructed with disabled settings — "
                "this is a caller bug; guard on settings.enabled"
            )
        import httpx

        self._settings = settings
        self._httpx = httpx
        self._client = httpx.AsyncClient(
            base_url=settings.url.rstrip("/"),
            timeout=httpx.Timeout(settings.timeout),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ConfigServiceClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # Public API ---------------------------------------------------------

    async def get_devices_info(self) -> Dict[str, Any]:
        """Return registry metadata keyed by device name (no instantiation specs)."""
        return await self._request("GET", "/api/v1/devices-info")

    async def get_instantiation_specs(self) -> Dict[str, Dict[str, Any]]:
        """Return ``{name: DeviceInstantiationSpec}`` for every device in the registry."""
        body = await self._request("GET", "/api/v1/devices/instantiation")
        if not isinstance(body, dict):
            raise ConfigServiceProtocolError(
                f"/devices/instantiation returned non-dict body: {type(body).__name__}"
            )
        return body

    async def is_registry_empty(self) -> bool:
        info = await self.get_devices_info()
        if not isinstance(info, dict):
            raise ConfigServiceProtocolError(
                f"/devices-info returned non-dict body: {type(info).__name__}"
            )
        return len(info) == 0

    async def get_changes_since(self, since_version: int) -> Dict[str, Any]:
        """Return delta payload from GET /api/v1/devices/changes."""
        body = await self._request(
            "GET",
            "/api/v1/devices/changes",
            params={"since_version": since_version},
        )
        for key in ("current_version", "service_epoch", "reset_occurred", "changes"):
            if key not in body:
                raise ConfigServiceProtocolError(
                    f"/devices/changes response missing {key!r}: {body!r}"
                )
        return body

    async def upsert_device(
        self,
        metadata: Dict[str, Any],
        spec: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Create if the device name is new, otherwise update."""
        name = metadata.get("name")
        if not name:
            raise ValueError("device metadata is missing 'name'")
        payload: Dict[str, Any] = {"metadata": metadata}
        if spec is not None:
            payload["instantiation_spec"] = spec
        try:
            return await self._request(
                "POST", "/api/v1/devices", json=payload, expected_status=(200, 201)
            )
        except ConfigServiceConflict:
            return await self._request(
                "PUT", f"/api/v1/devices/{name}", json=payload
            )

    async def delete_device(self, name: str) -> Dict[str, Any]:
        return await self._request("DELETE", f"/api/v1/devices/{name}")

    async def lock_devices(
        self,
        device_names: List[str],
        *,
        item_id: str,
        plan_name: str,
    ) -> Dict[str, Any]:
        payload = {
            "device_names": list(device_names),
            "item_id": item_id,
            "plan_name": plan_name,
            "locked_by_service": self._settings.service_name,
        }
        return await self._request("POST", "/api/v1/devices/lock", json=payload)

    async def unlock_devices(
        self,
        device_names: List[str],
        *,
        item_id: str,
    ) -> Dict[str, Any]:
        payload = {
            "device_names": list(device_names),
            "item_id": item_id,
        }
        return await self._request("POST", "/api/v1/devices/unlock", json=payload)

    # Internals ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        expected_status: Tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        httpx = self._httpx
        retryable_exc = (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        )
        attempts = self._settings.max_attempts
        last_exc: Optional[BaseException] = None

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.request(
                    method, path, params=params, json=json
                )
            except retryable_exc as exc:
                last_exc = exc
                if attempt < attempts:
                    delay_ms = self._backoff_for_attempt(attempt)
                    logger.warning(
                        "config-service %s %s failed (attempt %d/%d): %s — retrying in %dms",
                        method, path, attempt, attempts, exc, delay_ms,
                    )
                    await asyncio.sleep(delay_ms / 1000.0)
                    continue
                logger.error(
                    "config-service %s %s failed after %d attempts: %s",
                    method, path, attempts, exc,
                )
                raise ConfigServiceUnreachable(
                    f"{method} {path} failed after {attempts} attempts: {exc}"
                ) from exc

            if response.status_code in _RETRYABLE_HTTP_STATUS:
                last_exc = ConfigServiceHTTPError(
                    response.status_code, _safe_body(response)
                )
                if attempt < attempts:
                    delay_ms = self._backoff_for_attempt(attempt)
                    logger.warning(
                        "config-service %s %s returned %d (attempt %d/%d) — retrying in %dms",
                        method, path, response.status_code, attempt, attempts, delay_ms,
                    )
                    await asyncio.sleep(delay_ms / 1000.0)
                    continue
                logger.error(
                    "config-service %s %s returned %d after %d attempts",
                    method, path, response.status_code, attempts,
                )
                raise ConfigServiceUnreachable(
                    f"{method} {path} returned {response.status_code} after {attempts} attempts"
                ) from last_exc

            if response.status_code in expected_status:
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    return response.json()
                except Exception as exc:
                    raise ConfigServiceProtocolError(
                        f"{method} {path} returned non-JSON body: {exc}"
                    ) from exc

            body = _safe_body(response)
            if response.status_code == 404:
                raise ConfigServiceNotFound(response.status_code, body)
            if response.status_code == 409:
                raise ConfigServiceConflict(response.status_code, body)
            raise ConfigServiceHTTPError(response.status_code, body)

        assert last_exc is not None
        raise ConfigServiceUnreachable(str(last_exc)) from last_exc

    def _backoff_for_attempt(self, attempt: int) -> int:
        schedule = self._settings.backoff_ms
        if not schedule:
            return 0
        idx = min(attempt - 1, len(schedule) - 1)
        return schedule[idx]


def _safe_body(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        try:
            return response.text
        except Exception:
            return None


async def sync_devices_on_env_open(
    client: ConfigServiceClient,
    expected_device_names: List[str],
    device_data: Dict[str, Dict[str, Any]],
    prefetched_info: Optional[Dict[str, Any]] = None,
) -> ConfigServiceState:
    """Bootstrap-if-empty and capture the version cursor.

    Raises ``ConfigServiceError`` if ``device_data`` is missing entries for
    any expected device (i.e. worker-side introspection couldn't produce
    a payload for a device the manager thinks exists). Per the no-silent-
    fallback rule, we do not proceed with partial registry contents.

    ``prefetched_info`` — when the manager already fetched /devices-info at
    the start of env-open (Layer 2.6 consume-mode), pass the result here to
    skip the redundant probe. ``None`` means "I don't know, ask the service".
    """
    missing = [name for name in expected_device_names if name not in device_data]
    if missing:
        raise ConfigServiceError(
            "config-service sync aborted: device introspection did not produce "
            "metadata/spec for the following devices (check worker logs for "
            f"per-device extraction failures): {sorted(missing)!r}"
        )

    is_empty = (
        len(prefetched_info) == 0
        if prefetched_info is not None
        else await client.is_registry_empty()
    )
    if is_empty:
        logger.info(
            "config-service registry is empty; bootstrapping %d device(s)",
            len(device_data),
        )
        await asyncio.gather(
            *(
                client.upsert_device(payload["metadata"], payload["spec"])
                for payload in device_data.values()
            )
        )
        logger.info("config-service bootstrap complete (%d device(s))", len(device_data))
    else:
        logger.info(
            "config-service registry is populated; skipping bootstrap"
        )

    changes = await client.get_changes_since(0)
    return ConfigServiceState(
        cursor=int(changes["current_version"]),
        epoch=str(changes["service_epoch"]),
    )


@dataclasses.dataclass(frozen=True)
class StalenessPlan:
    """Actionable result of a pre-plan staleness check.

    ``replace_overlay`` is True when config-service reported a reset or a
    service_epoch change — the worker's previous overlay is untrusted and
    must be dropped wholesale; ``upserts`` then carries the full registry
    (populated by ``fetch_staleness_plan`` with a follow-up
    /devices/instantiation fetch). Otherwise the plan describes an
    incremental diff: ``upserts`` + ``deletes`` are exactly what changed
    since ``state.cursor``. ``new_state`` is the cursor+epoch to commit
    after the worker accepts the overlay.
    """

    replace_overlay: bool
    upserts: Dict[str, Dict[str, Any]]
    deletes: List[str]
    new_state: ConfigServiceState

    @property
    def is_noop(self) -> bool:
        return not self.replace_overlay and not self.upserts and not self.deletes


def build_staleness_plan(
    response: Dict[str, Any], saved_epoch: str
) -> StalenessPlan:
    """Turn a /devices/changes response into an overlay-update plan.

    Pure function. ``fetch_staleness_plan`` wraps it with the full-refetch
    HTTP call when ``replace_overlay`` is True.
    """
    new_state = ConfigServiceState(
        cursor=int(response["current_version"]),
        epoch=str(response["service_epoch"]),
    )
    if response["reset_occurred"] or response["service_epoch"] != saved_epoch:
        # upserts left empty here; fetch_staleness_plan fills it from
        # /devices/instantiation so the caller sees the full registry.
        return StalenessPlan(
            replace_overlay=True, upserts={}, deletes=[], new_state=new_state
        )

    upserts: Dict[str, Dict[str, Any]] = {}
    deletes: List[str] = []
    for change in response["changes"]:
        name = change["device_name"]
        op = change["op"]
        if op == "upsert":
            spec = change.get("spec")
            if spec is None:
                raise ConfigServiceProtocolError(
                    f"upsert change for {name!r} is missing 'spec'"
                )
            upserts[name] = spec
        elif op == "delete":
            deletes.append(name)
        else:
            raise ConfigServiceProtocolError(
                f"unknown change op {op!r} for device {name!r}"
            )

    return StalenessPlan(
        replace_overlay=False,
        upserts=upserts,
        deletes=deletes,
        new_state=new_state,
    )


async def fetch_staleness_plan(
    client: ConfigServiceClient,
    state: ConfigServiceState,
) -> StalenessPlan:
    """Call /devices/changes; on epoch-mismatch or registry reset also fetch
    /devices/instantiation so the caller can apply a single atomic overlay
    replacement without a second round-trip."""
    response = await client.get_changes_since(state.cursor)
    plan = build_staleness_plan(response, state.epoch)
    if plan.replace_overlay:
        specs = await client.get_instantiation_specs()
        plan = dataclasses.replace(plan, upserts=specs)
    return plan
