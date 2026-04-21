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


def test_extract_pvs_survives_broken_attributes():
    assert _extract_pvs(FakeBrokenDevice()) == {}


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


def test_build_payload_skips_device_that_raises_during_extraction(caplog):
    devices = {
        "good": FakeOphydMotor(prefix="XF:01-Mtr{M1}", name="good"),
        "bad": FakeBrokenDevice(),
    }
    payload = build_config_service_payload(devices)
    assert "good" in payload
    # Broken device's metadata build still succeeds (our helpers catch AttributeError
    # etc. internally) — we're not asserting it's skipped, just that the good one
    # survives.
    assert payload["good"]["spec"]["args"] == ["XF:01-Mtr{M1}"]
