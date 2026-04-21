"""
Introspect live ophyd / ophyd-async device objects to produce payloads
compatible with bluesky-configuration-service's ``DeviceMetadata`` and
``DeviceInstantiationSpec`` models.

Ported from bluesky-experiment-execution-service's device_sync.py
introspection helpers. No network calls here — this module only inspects
Python objects in the worker namespace.

Extraction is best-effort per device; failures are logged and omitted. If a
device cannot be introspected, its entry in the output dict is missing — the
consumer (manager-side config-service sync) decides how to handle that.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def _infer_device_label(obj: Any) -> str:
    cls_name = type(obj).__name__.lower()
    if "motor" in cls_name or "axis" in cls_name or "positioner" in cls_name:
        return "motor"
    if "det" in cls_name or "area" in cls_name or "camera" in cls_name:
        return "detector"
    if "signal" in cls_name:
        return "signal"
    if "fly" in cls_name:
        return "flyer"
    try:
        from bluesky.protocols import Flyable, Movable, Readable
    except ImportError:
        return "device"
    if isinstance(obj, Movable):
        return "motor"
    if isinstance(obj, Flyable):
        return "flyer"
    if isinstance(obj, Readable):
        return "readable"
    return "device"


def _check_protocol(device: Any, proto_name: str) -> bool:
    try:
        import bluesky.protocols as bp
    except ImportError:
        return False
    proto = getattr(bp, proto_name, None)
    if proto is None:
        return False
    return isinstance(device, proto)


def _extract_ophyd_async_pvs(
    device: Any, path_prefix: str, pvs: Dict[str, str], max_depth: int = 3
) -> None:
    if max_depth <= 0:
        return
    children = getattr(device, "children", None)
    if children is None or not callable(children):
        return
    for child_name, child in children():
        child_path = f"{path_prefix}{child_name}" if path_prefix else child_name
        source = getattr(child, "source", None)
        if source and isinstance(source, str) and "://" in source:
            pvs[child_path] = source.split("://", 1)[1]
        else:
            _extract_ophyd_async_pvs(child, f"{child_path}.", pvs, max_depth - 1)


def _extract_pvs(obj: Any) -> Dict[str, str]:
    """Multi-strategy PV discovery.

    Tries ophyd-v1 components, then ophyd-async children(), then a top-level
    .prefix. Unexpected errors from a live device (e.g. IOC disconnection
    during introspection) propagate to the caller so they can be logged
    against the specific device in build_config_service_payload.
    """
    pvs: Dict[str, str] = {}

    for comp_name in getattr(obj, "component_names", ()):
        comp = getattr(obj, comp_name, None)
        if comp is None:
            continue
        pv = getattr(comp, "pvname", None)
        if pv:
            pvs[comp_name] = pv

    if not pvs:
        _extract_ophyd_async_pvs(obj, "", pvs)

    if not pvs:
        prefix = getattr(obj, "prefix", None)
        if prefix and isinstance(prefix, str):
            pvs["prefix"] = prefix

    return pvs


def _extract_labels(obj: Any) -> List[str]:
    raw = getattr(obj, "_ophyd_labels_", None)
    if raw is None:
        return []
    if isinstance(raw, set):
        return sorted(raw)
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def device_to_metadata_dict(name: str, device: Any) -> Dict[str, Any]:
    """Build a ``DeviceMetadata``-shaped dict from a live device object."""
    cls = type(device)
    return {
        "name": name,
        "device_label": _infer_device_label(device),
        "ophyd_class": cls.__name__,
        "module": cls.__module__,
        "is_movable": _check_protocol(device, "Movable") or hasattr(device, "set"),
        "is_flyable": _check_protocol(device, "Flyable")
        or (hasattr(device, "kickoff") and hasattr(device, "complete")),
        "is_readable": _check_protocol(device, "Readable") or hasattr(device, "read"),
        "is_triggerable": _check_protocol(device, "Triggerable") or hasattr(device, "trigger"),
        "is_stageable": _check_protocol(device, "Stageable")
        or (hasattr(device, "stage") and hasattr(device, "unstage")),
        "is_configurable": _check_protocol(device, "Configurable")
        or hasattr(device, "read_configuration"),
        "is_pausable": _check_protocol(device, "Pausable")
        or (hasattr(device, "pause") and hasattr(device, "resume")),
        "is_stoppable": _check_protocol(device, "Stoppable") or hasattr(device, "stop"),
        "is_subscribable": _check_protocol(device, "Subscribable")
        or hasattr(device, "subscribe"),
        "is_checkable": _check_protocol(device, "Checkable") or hasattr(device, "check_value"),
        "writes_external_assets": _check_protocol(device, "WritesExternalAssets")
        or hasattr(device, "collect_asset_docs"),
        "pvs": _extract_pvs(device),
        "labels": _extract_labels(device),
    }


def device_to_instantiation_spec(name: str, device: Any) -> Dict[str, Any]:
    """Build a ``DeviceInstantiationSpec``-shaped dict from a live device object.

    Constructor args are recovered best-effort from ``device.prefix``
    (the canonical ophyd EPICS case). Devices without a ``prefix`` attribute
    produce ``args=[]`` — the caller decides whether that's acceptable.
    """
    cls = type(device)
    prefix = getattr(device, "prefix", None)
    args: List[Any] = [prefix] if (prefix and isinstance(prefix, str)) else []
    return {
        "name": name,
        "device_class": f"{cls.__module__}.{cls.__name__}",
        "args": args,
        "kwargs": {"name": name},
        "active": True,
    }


def import_device_class(class_path: str) -> type:
    """Import a class from its fully qualified ``module.ClassName`` path.

    Raises ImportError on missing module or attribute. Consume-mode env-load
    is strict: any failure fails env-open loudly (see
    feedback_backwards_compat.md — no silent fallbacks).
    """
    if "." not in class_path:
        raise ImportError(f"Invalid class path (no module): {class_path!r}")
    module_name, class_name = class_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    try:
        return getattr(module, class_name)
    except AttributeError:
        raise ImportError(f"Class {class_name!r} not found in module {module_name!r}")


def instantiate_device_from_spec(spec: Dict[str, Any]) -> Any:
    """Instantiate a live device object from a ``DeviceInstantiationSpec`` dict.

    Inverse of :func:`device_to_instantiation_spec`. Uses the spec's
    ``device_class`` as a dotted import path and calls the class with
    ``args`` + ``kwargs``. Fidelity is bounded by what the spec captured —
    see project_spec_fidelity_followup.md for the known narrow-spec gap.
    """
    try:
        class_path = spec["device_class"]
    except KeyError:
        raise ValueError(f"spec missing required key 'device_class': {spec!r}")
    args = list(spec.get("args", []))
    kwargs = dict(spec.get("kwargs", {}))
    device_class = import_device_class(class_path)
    return device_class(*args, **kwargs)


def build_config_service_payload(
    devices_in_nspace: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    """Produce ``{name: {"metadata": ..., "spec": ...}}`` for every top-level
    device in the worker namespace.

    A device that raises during introspection is skipped with a WARNING log;
    the rest still produce entries.
    """
    result: Dict[str, Dict[str, Any]] = {}
    for name, device in devices_in_nspace.items():
        try:
            metadata = device_to_metadata_dict(name, device)
            spec = device_to_instantiation_spec(name, device)
        except Exception as exc:
            logger.warning(
                "config-service introspection skipped device %r: %s", name, exc
            )
            continue
        result[name] = {"metadata": metadata, "spec": spec}
    return result
