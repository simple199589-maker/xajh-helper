from __future__ import annotations

import struct
import sys
import types
import unittest
from unittest.mock import patch

from app.core.entity_scan import EntityHit, classify_bucket, classify_name
from app.core import plg_objects
from app.core.plg_objects import CLASS_MATTER, CLASS_PLAYER, list_class_objects
from app.core.plg_interact import InteractTarget, classify_matter_name, suggest_action
from app.core.plg_objects import OBJ_POS_OFF, read_object_pos


class _Pm:
    process_handle = 123


class ObjectAndInteractTests(unittest.TestCase):
    def test_entity_name_classification(self) -> None:
        self.assertEqual(classify_name("九层妖楼宝箱", class_id=CLASS_MATTER), "matter")
        self.assertEqual(classify_name("杂货商人"), "npc")
        self.assertEqual(classify_name("陌生对象", class_id=CLASS_PLAYER), "player")

    def test_entity_buckets(self) -> None:
        rows = [
            EntityHit(kind="matter", name="箱子", address=1, dist=1.0),
            EntityHit(kind="npc", name="守卫", address=2, dist=2.0),
        ]
        buckets = classify_bucket(rows)
        self.assertEqual(buckets["matter"][0].name, "箱子")
        self.assertEqual(buckets["npc"][0].name, "守卫")

    def test_matter_classification_and_action(self) -> None:
        self.assertEqual(classify_matter_name("铜矿脉"), "gather")
        self.assertEqual(classify_matter_name("战利品宝箱"), "pickup")
        self.assertEqual(classify_matter_name("门派旗帜"), "mark")
        self.assertEqual(suggest_action("mark"), "skip")
        self.assertEqual(suggest_action("other"), "other_try")

    def test_interact_target_serialization_adds_derived_fields(self) -> None:
        data = InteractTarget("铜矿", 0x1234, kind="gather").to_dict()
        self.assertEqual(data["kind_label"], "采集")
        self.assertEqual(data["action"], "gather_try")

    def _read_pos(self, raw: bytes):
        module = types.ModuleType("pymem.memory")
        module.read_bytes = lambda handle, addr, size: raw
        pkg = types.ModuleType("pymem")
        pkg.memory = module
        with patch.dict(sys.modules, {"pymem": pkg, "pymem.memory": module}):
            return read_object_pos(_Pm(), 0x1000)

    def test_read_object_position(self) -> None:
        self.assertEqual(self._read_pos(struct.pack("<fff", 1.5, 2.5, -3.0)), (1.5, 2.5, -3.0))

    def test_read_object_position_rejects_nan_and_outlier(self) -> None:
        self.assertIsNone(self._read_pos(struct.pack("<fff", float("nan"), 0, 0)))
        self.assertIsNone(self._read_pos(struct.pack("<fff", 500001, 0, 0)))

    def test_read_object_position_rejects_null_pointer(self) -> None:
        self.assertIsNone(read_object_pos(_Pm(), 0))

    def test_name_filter_reads_tid_only_after_name_match(self) -> None:
        session = type("Session", (), {"pid": 9, "pm": _Pm()})()
        calls: list[tuple[int, int]] = []

        def _resolve(_session, export):
            if export == plg_objects.EXPORT_GET_OBJECT_NAME:
                return 0x10
            if export == plg_objects.EXPORT_GET_OBJECT_TID:
                return 0x20
            return 0

        def _remote(_pid, va, args, timeout_ms=0):
            ptr = int(args[0])
            calls.append((int(va), ptr))
            if int(va) == 0x10:
                return ptr + 0x10000
            return 778899

        def _name(_pm, ptr):
            return "目标宝箱" if int(ptr) == 0x12000 else "普通物体"

        with patch("app.core.safe_dispatch.get_dispatch"), patch(
            "app.core.plg_objects.get_object_count", return_value=2
        ), patch(
            "app.core.plg_objects.get_object_ptrs", return_value=[0x1000, 0x2000]
        ), patch(
            "app.core.plg_objects.read_object_pos",
            side_effect=[(1.0, 0.0, 0.0), (2.0, 0.0, 0.0)],
        ), patch(
            "app.core.plg_objects._resolve_va", side_effect=_resolve
        ), patch(
            "app.core.plg_objects.remote_call_cdecl_x86", side_effect=_remote
        ), patch(
            "app.core.plg_objects.read_wstr", side_effect=_name
        ), patch(
            "app.core.plg_objects.read_object_template_id",
            side_effect=lambda _session, ptr, *, class_id=None: (
                778899 if int(ptr) == 0x2000 else None
            ),
        ):
            rows = list_class_objects(
                session,
                CLASS_MATTER,
                host_pos=(0.0, 0.0, 0.0),
                want_name_keys=("目标宝箱",),
                read_name=True,
                read_tid=True,
                max_inspect=2,
            )

        self.assertEqual([(row.ptr, row.tid) for row in rows], [(0x2000, 778899)])
        self.assertEqual(calls, [(0x10, 0x1000), (0x10, 0x2000)])

    def test_read_object_template_id_uses_verified_vtable_and_offset(self) -> None:
        session = type("Session", (), {"pm": _Pm(), "module_base": 0x400000})()

        def read(_pm, addr, size):
            addr = int(addr)
            if (addr, size) == (0x30000, 4):
                return struct.pack("<I", 0x20000)
            if (addr, size) == (0x20084, 4):
                return struct.pack("<I", 0x400000 + plg_objects.PLG_OBJ_TID_FN_RVA)
            if (addr, size) == (0x30000 + plg_objects.PLG_OBJ_TID_OFF, 4):
                return struct.pack("<I", 0x18A98)
            raise AssertionError((addr, size))

        with patch.object(plg_objects, "_read_process_bytes", side_effect=read):
            self.assertEqual(
                plg_objects.read_object_template_id(session, 0x30000), 0x18A98
            )

    def test_read_object_template_id_rejects_unknown_vtable(self) -> None:
        session = type("Session", (), {"pm": _Pm(), "module_base": 0x400000})()

        def read(_pm, addr, size):
            if (int(addr), size) == (0x30000, 4):
                return struct.pack("<I", 0x20000)
            if (int(addr), size) == (0x20084, 4):
                return struct.pack("<I", 0x400000 + 0xDEAD)
            raise AssertionError("tid field must not be read for unknown vtable")

        with patch.object(plg_objects, "_read_process_bytes", side_effect=read):
            self.assertIsNone(plg_objects.read_object_template_id(session, 0x30000))

    def test_nearby_scan_uses_rpm_tid_and_never_remote_tid(self) -> None:
        session = type("Session", (), {"pid": 9, "pm": _Pm()})()

        def _reject_remote(_pid, _va, _args, timeout_ms=0):
            raise AssertionError("GetObjectTemplateID remote call must be gone")

        with patch("app.core.safe_dispatch.get_dispatch"), patch(
            "app.core.plg_objects.get_object_count", return_value=1
        ), patch(
            "app.core.plg_objects.get_object_ptrs", return_value=[0x3000]
        ), patch(
            "app.core.plg_objects.read_object_pos",
            return_value=(1.0, 0.0, 0.0),
        ), patch(
            "app.core.plg_objects.read_object_template_id",
            return_value=99,
        ), patch(
            "app.core.plg_objects._resolve_va",
            side_effect=AssertionError("name CRT was not requested"),
        ), patch(
            "app.core.plg_objects.remote_call_cdecl_x86",
            side_effect=_reject_remote,
        ):
            rows = list_class_objects(
                session,
                plg_objects.CLASS_NPC,
                host_pos=(0.0, 0.0, 0.0),
                want_tid=99,
                read_name=False,
                read_tid=True,
                max_inspect=1,
            )

        self.assertEqual([(row.ptr, row.tid) for row in rows], [(0x3000, 99)])


if __name__ == "__main__":
    unittest.main()
