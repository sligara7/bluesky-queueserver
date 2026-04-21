"""
Unit tests for the device-introspection helpers that build
configuration-service payloads from live device objects.

Uses hand-rolled stand-ins (no ophyd/ophyd-async imports) so the tests
stay fast and hermetic.
"""

from __future__ import annotations

from typing import Iterable, Tuple

from bluesky_queueserver.manager.device_introspection import (
    _extract_pvs,
    build_config_service_payload,
    device_to_instantiation_spec,
    device_to_metadata_dict,
    import_device_class,
    instantiate_device_from_spec,
)


# ===== Test doubles =====


class FakeComponent:
    """Stand-in for an ophyd v1 Signal/Component exposing .pvname."""

    def __init__(self, pvname: str):
        self.pvname = pvname


class FakeOphydMotor:
    """Duck-typed like an ophyd v1 EpicsMotor for introspection purposes."""

    component_names = ("user_readback", "user_setpoint")

    def __init__(self, *, prefix: str, name: str):
        self.prefix = prefix
        self.name = name
        self.user_readback = FakeComponent(f"{prefix}.RBV")
        self.user_setpoint = FakeComponent(f"{prefix}.VAL")
        self._ophyd_labels_ = {"motors", "hutch-a"}

    def read(self):  # Readable
        return {}

    def set(self, value):  # Movable
        return None

    def stop(self):  # Stoppable
        return None


class FakeAsyncSignal:
    """Stand-in for an ophyd-async Signal exposing .source."""

    def __init__(self, source: str):
        self.source = source


class FakeAsyncDevice:
    """Stand-in for an ophyd-async Device with ``children()``."""

    def __init__(self, prefix: str, name: str):
        self.prefix = prefix
        self.name = name
        self._children = [
            ("readback", FakeAsyncSignal(f"ca://{prefix}:RBV")),
            ("setpoint", FakeAsyncSignal(f"pva://{prefix}:SP")),
        ]

    def children(self) -> Iterable[Tuple[str, object]]:
        return iter(self._children)


class FakePrefixOnly:
    """Device with only a prefix and no component/children introspection."""

    def __init__(self, prefix: str, name: str):
        self.prefix = prefix
        self.name = name


class FakeNothing:
    """Device with nothing introspectable — for degraded-path tests."""

    def __init__(self, name: str):
        self.name = name


class FakeBrokenDevice:
    """Device whose attribute access raises — for resilience tests."""

    @property
    def component_names(self):
        raise RuntimeError("iocs not responding")

    @property
    def prefix(self):
        raise RuntimeError("iocs not responding")


class FakePositionalMotor:
    """Device whose ctor accepts ``(prefix, *, name)`` — matches the shape of
    real ophyd.EpicsMotor that the narrow spec was designed around. Used by
    the reverse-path round-trip test.
    """

    def __init__(self, prefix, *, name):
        self.prefix = prefix
        self.name = name


# ===== _extract_pvs =====


def test_extract_pvs_from_ophyd_v1_components():
    m = FakeOphydMotor(prefix="XF:01-Mtr{M1}", name="m1")
    pvs = _extract_pvs(m)
    assert pvs == {
        "user_readback": "XF:01-Mtr{M1}.RBV",
        "user_setpoint": "XF:01-Mtr{M1}.VAL",
    }


def test_extract_pvs_from_ophyd_async_children_strips_protocol():
    d = FakeAsyncDevice(prefix="XF:01-Det{D1}", name="d1")
    pvs = _extract_pvs(d)
    assert pvs == {
        "readback": "XF:01-Det{D1}:RBV",
        "setpoint": "XF:01-Det{D1}:SP",
    }


def test_extract_pvs_fallback_to_prefix():
    d = FakePrefixOnly(prefix="XF:01-Misc{X}", name="x")
    assert _extract_pvs(d) == {"prefix": "XF:01-Misc{X}"}


def test_extract_pvs_returns_empty_for_uninstrumented_device():
    assert _extract_pvs(FakeNothing(name="nothing")) == {}


def test_extract_pvs_propagates_broken_attribute_exceptions():
    import pytest
    with pytest.raises(RuntimeError, match="iocs not responding"):
        _extract_pvs(FakeBrokenDevice())


# ===== device_to_metadata_dict =====


def test_metadata_captures_class_and_module():
    m = FakeOphydMotor(prefix="XF:01-Mtr{M1}", name="m1")
    meta = device_to_metadata_dict("m1", m)
    assert meta["name"] == "m1"
    assert meta["ophyd_class"] == "FakeOphydMotor"
    assert meta["module"].startswith("test_device_introspection") or meta["module"].startswith(
        "bluesky_queueserver"
    )
    assert meta["is_movable"] is True
    assert meta["is_readable"] is True
    assert meta["is_stoppable"] is True
    assert meta["pvs"] == {
        "user_readback": "XF:01-Mtr{M1}.RBV",
        "user_setpoint": "XF:01-Mtr{M1}.VAL",
    }
    assert meta["labels"] == ["hutch-a", "motors"]


def test_metadata_label_inference_from_class_name():
    m = FakeOphydMotor(prefix="XF:01-Mtr{M1}", name="m1")
    assert device_to_metadata_dict("m1", m)["device_label"] == "motor"


def test_metadata_for_uninstrumented_device_is_still_well_formed():
    d = FakeNothing(name="z")
    meta = device_to_metadata_dict("z", d)
    assert meta["name"] == "z"
    assert meta["ophyd_class"] == "FakeNothing"
    assert meta["pvs"] == {}
    assert meta["labels"] == []


# ===== device_to_instantiation_spec =====


def test_instantiation_spec_includes_prefix_when_present():
    m = FakeOphydMotor(prefix="XF:01-Mtr{M1}", name="m1")
    spec = device_to_instantiation_spec("m1", m)
    assert spec["name"] == "m1"
    assert spec["device_class"].endswith(".FakeOphydMotor")
    assert spec["args"] == ["XF:01-Mtr{M1}"]
    assert spec["kwargs"] == {"name": "m1"}
    assert spec["active"] is True


def test_instantiation_spec_has_empty_args_when_no_prefix():
    d = FakeNothing(name="z")
    spec = device_to_instantiation_spec("z", d)
    assert spec["args"] == []
    assert spec["kwargs"] == {"name": "z"}


# ===== build_config_service_payload =====


def test_build_payload_contains_entry_per_device():
    devices = {
        "m1": FakeOphydMotor(prefix="XF:01-Mtr{M1}", name="m1"),
        "d1": FakeAsyncDevice(prefix="XF:01-Det{D1}", name="d1"),
    }
    payload = build_config_service_payload(devices)
    assert set(payload.keys()) == {"m1", "d1"}
    assert payload["m1"]["metadata"]["name"] == "m1"
    assert payload["m1"]["spec"]["args"] == ["XF:01-Mtr{M1}"]
    assert payload["d1"]["spec"]["args"] == ["XF:01-Det{D1}"]


def test_build_payload_skips_device_that_raises_during_extraction():
    devices = {
        "good": FakeOphydMotor(prefix="XF:01-Mtr{M1}", name="good"),
        "bad": FakeBrokenDevice(),
    }
    payload = build_config_service_payload(devices)
    assert "good" in payload
    assert "bad" not in payload
    assert payload["good"]["spec"]["args"] == ["XF:01-Mtr{M1}"]


# ===== import_device_class / instantiate_device_from_spec (Layer 2.6 reverse path) =====


def test_import_device_class_happy_path():
    cls = import_device_class(f"{FakeOphydMotor.__module__}.FakeOphydMotor")
    assert cls is FakeOphydMotor


def test_import_device_class_missing_module():
    import pytest
    with pytest.raises(ImportError, match="no_such_module"):
        import_device_class("no_such_module.NoClass")


def test_import_device_class_missing_attr():
    import pytest
    with pytest.raises(ImportError, match="NoSuchClass"):
        import_device_class(f"{FakeOphydMotor.__module__}.NoSuchClass")


def test_import_device_class_rejects_path_without_dot():
    import pytest
    with pytest.raises(ImportError, match="no module"):
        import_device_class("nodot")


def test_instantiate_roundtrip_preserves_class_and_args():
    # Needs a class with a positional-prefix ctor — the narrow spec captures
    # prefix as args[0], matching real ophyd.EpicsMotor's signature.
    original = FakePositionalMotor("XF:01-Mtr{M1}", name="m1")
    spec = device_to_instantiation_spec("m1", original)
    revived = instantiate_device_from_spec(spec)
    assert isinstance(revived, FakePositionalMotor)
    assert revived.prefix == "XF:01-Mtr{M1}"
    assert revived.name == "m1"


def test_instantiate_from_spec_missing_device_class_key():
    import pytest
    with pytest.raises(ValueError, match="device_class"):
        instantiate_device_from_spec({"args": [], "kwargs": {}})


def test_instantiate_from_spec_propagates_constructor_failure():
    """Fidelity gap: narrow spec (prefix+name) can miss required kwargs. When
    the stored class demands a kwarg the spec didn't capture, instantiation
    must fail loudly — not silently return a half-configured device."""
    import pytest

    class StrictDevice:
        def __init__(self, *, prefix, name, required_kwarg):
            self.prefix = prefix
            self.name = name
            self.required = required_kwarg

    spec = {
        "name": "s1",
        "device_class": f"{__name__}.test_instantiate_from_spec_propagates_constructor_failure.<locals>.StrictDevice",
        "args": [],
        "kwargs": {"name": "s1"},
        "active": True,
    }
    # Local classes aren't importable by a dotted path, so this path triggers
    # an ImportError rather than a TypeError — still the hard-fail we want.
    with pytest.raises(ImportError):
        instantiate_device_from_spec(spec)
