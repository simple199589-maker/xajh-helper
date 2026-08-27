from __future__ import annotations

import ctypes
import inspect
import re
import threading
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from app.core.bridge_protocol import (
    BRIDGE_BUILD_ID,
    BRIDGE_MAGIC,
    OFF_CMD,
    OFF_MAGIC,
    OFF_STATUS,
    OFF_ACK_SEQ,
    OFF_CAPABILITIES,
    OFF_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    SHARED_SIZE,
    BridgeCommand,
    BridgeCapability,
    BridgeStatus,
    required_build_capability,
    required_capability,
)
from app.core.xajh_bridge import (
    CMD_AUTOPLAY_SEED_FOLLOW,
    CMD_AUTOPLAY_START_BYPASS,
    CMD_AUTOPLAY_STOP,
    CMD_HOST_CONTEXT,
    CMD_HOST_SNAPSHOT,
    CMD_DUMMY_DAMAGE_STAT,
    CMD_COMBAT_MONITOR,
    CMD_SKILL_INTERRUPT_67,
    CMD_SKILL_ACTION_EXPERIMENT,
    CMD_YOUFENG_EXACT_CANCEL_GATE,
    CMD_YOUFENG_INTERNAL_CHAIN,
    CMD_YOUFENG_FRESH_GATE,
    CMD_YOUFENG_NEXT_GATE,
    CMD_YOUFENG_MASH_CAST,
    CMD_YOUFENG_PHASE_GATE,
    CMD_YOUFENG_QINGGONG_GATE,
    CMD_SKILL_ACTION_TRACE,
    CMD_PING,
    XajhBridge,
    _inject_stage_dir,
    inject_bridge,
    last_inject_failure,
    stage_bridge_for_inject,
)


ROOT = Path(__file__).resolve().parents[1]


class BridgeProtocolTests(unittest.TestCase):
    def test_object_scan_command_contract(self) -> None:
        from app.core.xajh_bridge import CMD_OBJECT_SCAN

        self.assertEqual(int(BridgeCommand.OBJECT_SCAN), 56)
        self.assertEqual(CMD_OBJECT_SCAN, 56)
        self.assertEqual(
            required_capability(BridgeCommand.OBJECT_SCAN),
            BridgeCapability.TARGET,
        )
        self.assertEqual(
            required_build_capability(BridgeCommand.OBJECT_SCAN),
            "target.set",
        )

        import app.core.xajh_bridge as bridge_mod

        source = inspect.getsource(bridge_mod.default_bridge_paths)
        self.assertIn('f"xajh_bridge_{int(BRIDGE_BUILD_ID)}.dll"', source)
        self.assertIn('candidate = bin_dir / "xajh_bridge.dll"', source)

    def test_staging_dir_is_under_current_software_root(self) -> None:
        root = Path("C:/XAJH-Test")
        with patch("common.paths.app_root", return_value=root):
            self.assertEqual(_inject_stage_dir(), root / "runtime" / "native" / "bin")

    def test_all_modes_stage_to_build_specific_dll_name(self) -> None:
        import app.core.xajh_bridge as bridge_mod

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src_dll = root / "xajh_bridge.dll"
            src_inj = root / "xajh_inject.exe"
            src_dll.write_bytes(b"current-bridge")
            src_inj.write_bytes(b"injector")
            stage = root / "stage"
            with (
                patch.object(bridge_mod, "default_bridge_paths", return_value=(src_dll, src_inj)),
                patch.object(bridge_mod, "_inject_stage_dir", return_value=stage),
                patch("common.paths.is_frozen", return_value=False),
            ):
                dll, inj = stage_bridge_for_inject()
            self.assertEqual(dll.name, f"xajh_bridge_{BRIDGE_BUILD_ID}.dll")
            self.assertEqual(dll.read_bytes(), src_dll.read_bytes())
            self.assertEqual(inj.name, "xajh_inject.exe")

            # Same-size/newer stale stages must still be replaced by content.
            dll.write_bytes(b"x" * len(src_dll.read_bytes()))
            self.assertNotEqual(dll.read_bytes(), src_dll.read_bytes())
            with (
                patch.object(bridge_mod, "default_bridge_paths", return_value=(src_dll, src_inj)),
                patch.object(bridge_mod, "_inject_stage_dir", return_value=stage),
                patch("common.paths.is_frozen", return_value=False),
            ):
                dll_hash_fixed, _ = stage_bridge_for_inject()
            self.assertEqual(dll_hash_fixed.read_bytes(), src_dll.read_bytes())

            # A legacy fixed-name stage must not affect the new build.
            (stage / "xajh_bridge.dll").write_bytes(b"old-bridge")
            with (
                patch.object(bridge_mod, "default_bridge_paths", return_value=(src_dll, src_inj)),
                patch.object(bridge_mod, "_inject_stage_dir", return_value=stage),
                patch("common.paths.is_frozen", return_value=False),
            ):
                dll2, _ = stage_bridge_for_inject()
            self.assertEqual(dll2.read_bytes(), b"current-bridge")

    def test_injector_failure_preserves_native_stage(self) -> None:
        import subprocess
        import app.core.xajh_bridge as bridge_mod

        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="inject begin\nLoadLibrary returned NULL (inject failed)",
            stderr="",
        )
        with (
            patch.object(bridge_mod, "stage_bridge_for_inject", return_value=(ROOT / "native/bin/xajh_bridge.dll", ROOT / "native/bin/xajh_inject.exe")),
            patch.object(bridge_mod, "prepare_bridge_shm", return_value=True),
            patch.object(bridge_mod, "_pid_alive", return_value=True),
            patch("subprocess.run", return_value=completed),
        ):
            self.assertFalse(inject_bridge(54321))
        self.assertEqual(last_inject_failure(54321, clear=True), "LOADLIBRARY_FAIL")
    def test_v2_keeps_legacy_prefix(self) -> None:
        self.assertEqual(PROTOCOL_VERSION, 2)
        self.assertEqual(OFF_PROTOCOL_VERSION, 184)
        self.assertEqual(OFF_ACK_SEQ, 196)
        self.assertEqual(SHARED_SIZE, 200)
        self.assertIsInstance(BRIDGE_BUILD_ID, int)
        self.assertGreaterEqual(BRIDGE_BUILD_ID, 2026080101)

    def test_cpp_command_numbers_match_python(self) -> None:
        text = (ROOT / "native/xajh_bridge/bridge_protocol.h").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"BRIDGE_BUILD_ID {BRIDGE_BUILD_ID}u", text)
        pairs = dict(
            (name, int(value))
            for name, value in re.findall(r"CMD_([A-Z0-9_]+)\s*=\s*(\d+)", text)
        )
        for command in BridgeCommand:
            self.assertEqual(pairs[command.name], int(command))
        self.assertIn("sizeof(BridgeShared) == 200", text)

    def test_native_crash_capture_writes_on_fresh_stack(self) -> None:
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("GameCrashWriterThreadProc", native)
        self.assertIn(
            "CreateThread(nullptr, 0, GameCrashWriterThreadProc", native
        )
        self.assertIn("g_crash_snapshot.context = *ep->ContextRecord", native)
        crash_writer = native.split("static void WriteGameCrashDump", 1)[1].split(
            "static DWORD WINAPI GameCrashWriterThreadProc", 1
        )[0]
        self.assertNotIn("CaptureStackBackTrace", crash_writer)

    def test_inline_detour_remove_never_frees_live_trampoline(self) -> None:
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("VirtualFree(hk->trampoline", native)
        self.assertIn("Keep the passive detour installed", native)

    def test_dummy_damage_counter_is_target_scoped_and_read_only(self) -> None:
        self.assertEqual(int(BridgeCommand.DUMMY_DAMAGE_STAT), 46)
        self.assertEqual(CMD_DUMMY_DAMAGE_STAT, 46)
        self.assertEqual(
            required_build_capability(BridgeCommand.DUMMY_DAMAGE_STAT),
            "damage.stat",
        )
        result = XajhBridge(1).dummy_damage_stat(0x0200000089ABCDEF, mode=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("kNoteSplitDamageProcess = 0x0079BA80u", native)
        self.assertIn("if (cmd == CMD_DUMMY_DAMAGE_STAT)", native)
        self.assertIn("if (!settled || !target_matches || damage <= 0", native)
        self.assertIn("InterlockedExchangeAdd64(&g_dummy_damage_total", native)

    def test_combat_monitor_is_read_only_self_filtered_counter(self) -> None:
        self.assertEqual(int(BridgeCommand.COMBAT_MONITOR), 50)
        self.assertEqual(CMD_COMBAT_MONITOR, 50)
        self.assertEqual(
            required_capability(BridgeCommand.COMBAT_MONITOR),
            BridgeCapability.SESSION,
        )
        self.assertEqual(
            required_build_capability(BridgeCommand.COMBAT_MONITOR),
            "skill.probe",
        )

    def test_target_submit_trace_and_narrow_guard_are_build_pinned(self) -> None:
        self.assertEqual(int(BridgeCommand.TARGET_SUBMIT_TRACE), 52)
        self.assertEqual(int(BridgeCommand.DUNGEON_TARGET_RULES), 53)
        from app.core.xajh_bridge import CMD_TARGET_SUBMIT_TRACE

        self.assertEqual(CMD_TARGET_SUBMIT_TRACE, 52)
        from app.core.xajh_bridge import CMD_DUNGEON_TARGET_RULES
        self.assertEqual(CMD_DUNGEON_TARGET_RULES, 53)
        self.assertEqual(
            required_capability(BridgeCommand.TARGET_SUBMIT_TRACE),
            BridgeCapability.TARGET,
        )
        self.assertEqual(
            required_build_capability(BridgeCommand.TARGET_SUBMIT_TRACE),
            "target.set",
        )
        bridge = XajhBridge(1)
        with patch.object(bridge, "call") as call:
            bridge.target_submit_trace(mode=1, timeout_ms=789)
        call.assert_called_once_with(
            CMD_TARGET_SUBMIT_TRACE,
            mode=1,
            timeout_ms=789,
        )
        with patch.object(bridge, "call") as call:
            bridge.dungeon_target_rules(mode=2, tid=0x18A98, timeout_ms=321)
        call.assert_called_once_with(
            CMD_DUNGEON_TARGET_RULES,
            id_lo=0x18A98,
            mode=2,
            timeout_ms=321,
        )
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("kNoteTargetSubmit = 0x007493B0u", native)
        self.assertIn("kNoteTargetQueuePromote = 0x0073E300u", native)
        self.assertIn("kNoteTargetStateApply = 0x00730430u", native)
        self.assertIn("kNoteTargetQueueRefresh = 0x007E2780u", native)
        self.assertIn("Hook_TargetSubmitTrace", native)
        self.assertIn("Hook_TargetQueuePromote", native)
        self.assertIn("Hook_TargetStateApply", native)
        self.assertIn("Hook_TargetQueueRefresh", native)
        self.assertIn("SanitizeArmedDungeonTarget", native)
        self.assertIn("arm_hits=%ld", native)
        self.assertIn("queue_hits=%ld", native)
        self.assertIn("xajh_target route=", native)
        self.assertIn("OutputDebugStringA(line)", native)
        self.assertIn("TARGET_SUBMIT_TRACE hook install/signature failed", native)
        self.assertIn("CMD_DUNGEON_TARGET_RULES", native)
        self.assertIn("IsConfiguredDungeonTargetTid", native)
        handler = native.split("if (cmd == CMD_TARGET_SUBMIT_TRACE)", 1)[1].split(
            "if (cmd == CMD_AUTOPLAY_STOP)", 1
        )[0]
        self.assertIn("mode=3 enables the proven automatic", handler)
        self.assertNotIn("SetTarget", handler)
        self.assertNotIn("AddToIgnore", handler)
        result = XajhBridge(1).combat_monitor(mode=2)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (cmd == CMD_COMBAT_MONITOR)", native)
        self.assertIn("g_combat_attack_seq", native)
        self.assertIn("g_combat_self_damage_seq", native)
        self.assertIn("COMBAT_MONITOR hook install failed", native)
        self.assertIn("cmd == CMD_COMBAT_MONITOR ||", native)
        self.assertIn("COMBAT_MONITOR a=%ld d=%ld", native)
        header = (ROOT / "native/xajh_bridge/bridge_protocol.h").read_text(
            encoding="utf-8"
        )
        self.assertIn("CMD_COMBAT_MONITOR = 50", header)

    def test_host_context_is_a_main_thread_lifecycle_probe(self) -> None:
        self.assertEqual(int(BridgeCommand.HOST_CONTEXT), 30)
        self.assertEqual(CMD_HOST_CONTEXT, 30)
        result = XajhBridge(1).host_context()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (cmd == CMD_HOST_CONTEXT)", native)
        self.assertIn("HOST_CONTEXT va=0", native)
        self.assertIn("cmd == CMD_HOST_CONTEXT", native)
        self.assertIn("g_input_this = nullptr", native)

    def test_host_snapshot_is_atomic_main_thread_scene_sample(self) -> None:
        self.assertEqual(int(BridgeCommand.HOST_SNAPSHOT), 33)
        self.assertEqual(CMD_HOST_SNAPSHOT, 33)
        result = XajhBridge(1).host_snapshot()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (cmd == CMD_HOST_SNAPSHOT)", native)
        self.assertIn("HOST_SNAPSHOT no_host", native)
        self.assertIn("GetCurrentScenePosition@plg", native)
        snapshot_block = native.split("if (cmd == CMD_HOST_SNAPSHOT)", 1)[1].split(
            "if (cmd == CMD_AUTOPLAY_STOP)", 1
        )[0]
        self.assertEqual(snapshot_block.count("get_host()"), 1)
        self.assertIn("kHostDeadStateOff = 0x1B8u", native)
        self.assertIn("g_shm->tid = -1", snapshot_block)
        self.assertIn("g_shm->tid = (host_state & 2u) ? 1 : 0", snapshot_block)
        self.assertIn('"HOST_SNAPSHOT ok dead=%d"', snapshot_block)
        source = inspect.getsource(XajhBridge._call_unlocked)
        self.assertIn("x=self._f32(OFF_X)", source)
        self.assertIn("mode=self._i32(OFF_MODE)", source)
        self.assertIn("tid=self._i32(OFF_TID)", source)

    def test_jianglong_runtime_resolver_is_read_only_session_command(self) -> None:
        self.assertEqual(int(BridgeCommand.JIANGLONG_RUNTIME_RESOLVE), 54)
        self.assertEqual(
            required_build_capability(BridgeCommand.JIANGLONG_RUNTIME_RESOLVE),
            "skill.probe",
        )
        result = XajhBridge(1).resolve_jianglong_runtime_config()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(encoding="utf-8")
        self.assertIn("if (cmd == CMD_JIANGLONG_RUNTIME_RESOLVE)", native)
        block = native.split("if (cmd == CMD_JIANGLONG_RUNTIME_RESOLVE)", 1)[1].split(
            "if (cmd == CMD_CAST_SKILL)", 1
        )[0]
        self.assertIn("0x00002305u", block)
        self.assertIn("+ 0xEA4", block)
        self.assertNotIn("InjectKey", block)
        self.assertNotIn("send_raw_packet", block)
    def test_skill_action_trace_is_short_lived_exact_profile_probe(self) -> None:
        self.assertEqual(int(BridgeCommand.SKILL_ACTION_TRACE), 34)
        self.assertEqual(CMD_SKILL_ACTION_TRACE, 34)
        self.assertEqual(
            required_build_capability(BridgeCommand.SKILL_ACTION_TRACE),
            "skill.probe",
        )
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (cmd == CMD_SKILL_ACTION_TRACE)", native)
        self.assertIn("Hook_SkillActionRequestTrace", native)
        self.assertIn("Hook_SetCurActiveSkillTrace", native)
        self.assertIn("Hook_OnPerformSkillTrace", native)
        self.assertIn("Hook_HostStartSessionTrace", native)
        self.assertIn("kNoteHostStartSession = 0x007550B0u", native)
        self.assertIn("e.session_args[11]", native)
        self.assertIn('"session_send"', native)
        self.assertIn("FnPerformStopTrace", native)
        self.assertIn("stop(mgr, 0x65)", native)
        self.assertIn("Hook_ActionCanCastTrace", native)
        self.assertIn("e.caller == NoteToLive(kNoteActionCanCastReturn)", native)
        self.assertIn("ProcessPendingSkillRestop();", native)
        self.assertIn("SKTRACE_RESTOP", native)
        self.assertIn("kNoteOnPerformSetActiveReturn", native)
        self.assertIn("g_skill_refill_suppress_on", native)
        self.assertIn("mode == 1 || mode == 3 || mode == 4", native)
        self.assertIn("g_skill_refill_suppress_mode", native)
        self.assertIn("suppress_mode == 4", native)
        self.assertIn("target_matches;", native)
        self.assertNotIn("target_matches && (e.cast_4a0_before & 1u)", native)
        self.assertIn("auto_continue ? 2", native)
        self.assertNotIn("Hook_CastOuterTrace", native)
        self.assertNotIn("Hook_CastCoreTrace", native)
        self.assertIn("kMaxSkillActionTrace = 8192", native)
        self.assertIn("StopSkillActionTraceHooks", native)
        self.assertIn("SKILL_ACTION_TRACE armed", native)
        result = XajhBridge(1).skill_action_trace(mode=1)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")

    def test_skill_interrupt_67_is_exact_ui_thread_command(self) -> None:
        self.assertEqual(int(BridgeCommand.SKILL_INTERRUPT_67), 36)
        self.assertEqual(CMD_SKILL_INTERRUPT_67, 36)
        self.assertEqual(
            required_build_capability(BridgeCommand.SKILL_INTERRUPT_67),
            "skill.probe",
        )
        result = XajhBridge(1).skill_interrupt_67(0x9563, 0x34D)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (cmd == CMD_SKILL_INTERRUPT_67)", native)
        self.assertIn("stop(mgr, 0x67u)", native)
        block = native.split("if (cmd == CMD_SKILL_INTERRUPT_67)", 1)[1].split(
            "if (cmd ==", 1
        )[0]
        self.assertNotIn("InjectKey", block)
        self.assertNotIn("KEY_HOLD", block)

    def test_youfeng_internal_chain_is_guarded_no_key_transaction(self) -> None:
        self.assertEqual(int(BridgeCommand.YOUFENG_INTERNAL_CHAIN), 38)
        self.assertEqual(CMD_YOUFENG_INTERNAL_CHAIN, 38)
        self.assertEqual(
            required_build_capability(BridgeCommand.YOUFENG_INTERNAL_CHAIN),
            "skill.probe",
        )
        result = XajhBridge(1).youfeng_internal_chain(0x9563, 0x34D)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (cmd == CMD_YOUFENG_INTERNAL_CHAIN)", native)
        self.assertIn("RunE07ActionPhase(cast, 1u)", native)
        self.assertIn("RunE07ActionPhase(cast, 2u)", native)
        self.assertIn("stop(mgr, 0x65u)", native)
        self.assertIn("stop(mgr, 0x67u)", native)
        self.assertIn("gate1 < 1600u", native)
        block = native.split("if (cmd == CMD_YOUFENG_INTERNAL_CHAIN)", 1)[1].split(
            "if (cmd ==", 1
        )[0]
        self.assertNotIn("InjectKey", block)
        self.assertNotIn("KEY_HOLD", block)

    def test_youfeng_next_gate_is_bounded_and_has_no_interrupt_action(self) -> None:
        self.assertEqual(int(BridgeCommand.YOUFENG_NEXT_GATE), 39)
        self.assertEqual(CMD_YOUFENG_NEXT_GATE, 39)
        self.assertEqual(
            required_build_capability(BridgeCommand.YOUFENG_NEXT_GATE),
            "skill.probe",
        )
        result = XajhBridge(1).youfeng_next_gate(0x9563, 0x34D)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (cmd == CMD_YOUFENG_NEXT_GATE)", native)
        self.assertIn("Hook_YoufengCurrentActionGate", native)
        self.assertIn("candidate + 0xEA4", native)
        self.assertIn("GetTickCount() + 450u", native)
        self.assertIn("ProcessPendingYoufengNextGate();", native)
        block = native.split("if (cmd == CMD_YOUFENG_NEXT_GATE)", 1)[1].split(
            "if (cmd ==", 1
        )[0]
        self.assertNotIn("RunE07ActionPhase", block)
        self.assertNotIn("stop(mgr", block)
        self.assertNotIn("InjectKey", block)

    def test_youfeng_mash_cast_submits_target_not_e07(self) -> None:
        self.assertEqual(int(BridgeCommand.YOUFENG_MASH_CAST), 40)
        self.assertEqual(CMD_YOUFENG_MASH_CAST, 40)
        result = XajhBridge(1).youfeng_mash_cast(0x9563, 0x34D)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        block = native.split("if (cmd == CMD_YOUFENG_MASH_CAST)", 1)[1].split(
            "if (cmd ==", 1
        )[0]
        self.assertIn("RunActionPhase(cast, lo, hi, 1u)", block)
        self.assertIn("g_youfeng_mash_config", block)
        self.assertNotIn("RunE07ActionPhase", block)
        self.assertNotIn("stop(mgr", block)
        self.assertNotIn("InjectKey", block)
        self.assertIn("event_buf[0] = skill", native)
        self.assertIn("RunActionPhase(cast, skill, config, 2u)", native)
        self.assertIn("ProcessPendingYoufengMash();", native)

    def test_youfeng_fresh_gate_only_clears_identity_before_gate(self) -> None:
        self.assertEqual(int(BridgeCommand.YOUFENG_FRESH_GATE), 41)
        self.assertEqual(CMD_YOUFENG_FRESH_GATE, 41)
        self.assertEqual(
            required_build_capability(BridgeCommand.YOUFENG_FRESH_GATE),
            "skill.probe",
        )
        result = XajhBridge(1).youfeng_fresh_gate(0x9563, 0x34D)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        block = native.split("if (cmd == CMD_YOUFENG_FRESH_GATE)", 1)[1].split(
            "if (cmd ==", 1
        )[0]
        self.assertIn("on_stop(cast, event_buf, 0u, 0u, 0u, 1u)", block)
        self.assertIn("after_skill != 0u || after_config != 0u", block)
        self.assertIn("g_youfeng_next_gate_allow_cleared, 1", block)
        self.assertIn("GetTickCount() + 450u", block)
        self.assertNotIn("RunActionPhase", block)
        self.assertNotIn("RunE07ActionPhase", block)
        self.assertNotIn("stop(mgr", block)
        self.assertNotIn("CancelSession", block)
        self.assertNotIn("InjectKey", block)

    def test_youfeng_exact_cancel_gate_uses_live_perform_id_only(self) -> None:
        self.assertEqual(int(BridgeCommand.YOUFENG_EXACT_CANCEL_GATE), 42)
        self.assertEqual(CMD_YOUFENG_EXACT_CANCEL_GATE, 42)
        result = XajhBridge(1).youfeng_exact_cancel_gate(0x9563, 0x34D)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        block = native.split(
            "if (cmd == CMD_YOUFENG_EXACT_CANCEL_GATE)", 1
        )[1].split("if (cmd ==", 1)[0]
        self.assertIn("id_perform = *(uint32_t*)(cast + 0x6C)", block)
        self.assertIn("perform_age > 2500L", block)
        self.assertIn("cancel((uint8_t*)net + kCancelSessionThisOff, 1u,", block)
        self.assertIn("id_perform)", block)
        self.assertIn("SKTRACE_EXACT_CANCEL", block)
        self.assertIn("on_stop(cast, event_buf, 0u, 0u, 0u, 1u)", block)
        self.assertNotIn("RunActionPhase", block)
        self.assertNotIn("RunE07ActionPhase", block)
        self.assertNotIn("stop(mgr", block)
        self.assertNotIn("InjectKey", block)

    def test_youfeng_phase_gate_sends_only_target_config_phase_pair(self) -> None:
        self.assertEqual(int(BridgeCommand.YOUFENG_PHASE_GATE), 43)
        self.assertEqual(CMD_YOUFENG_PHASE_GATE, 43)
        self.assertEqual(
            required_build_capability(BridgeCommand.YOUFENG_PHASE_GATE),
            "skill.probe",
        )
        result = XajhBridge(1).youfeng_phase_gate(0x9563, 0x34D)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        block = native.split("if (cmd == CMD_YOUFENG_PHASE_GATE)", 1)[1].split(
            "if (cmd ==", 1
        )[0]
        self.assertIn("phase_down((uint8_t*)net + kActionPhaseThisOff, hi)", block)
        self.assertIn("SKTRACE_PHASE_GATE", block)
        self.assertIn("on_stop(cast, event_buf, 0u, 0u, 0u, 1u)", block)
        self.assertNotIn("RunActionPhase", block)
        self.assertNotIn("RunE07ActionPhase", block)
        self.assertNotIn("CancelSession", block)
        self.assertNotIn("stop(mgr", block)
        self.assertNotIn("InjectKey", block)
        self.assertIn("phase_up((uint8_t*)net + kActionPhaseThisOff, config)", native)
        self.assertIn("ProcessPendingYoufengPhase();", native)

    def test_youfeng_qinggong_gate_calls_native_space_path_inside_hook(self) -> None:
        self.assertEqual(int(BridgeCommand.YOUFENG_QINGGONG_GATE), 44)
        self.assertEqual(CMD_YOUFENG_QINGGONG_GATE, 44)
        self.assertEqual(
            required_build_capability(BridgeCommand.YOUFENG_QINGGONG_GATE),
            "skill.probe",
        )
        result = XajhBridge(1).youfeng_qinggong_gate(0x9563, 0x34D)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        command = native.split(
            "if (cmd == CMD_YOUFENG_QINGGONG_GATE)", 1
        )[1].split("if (cmd ==", 1)[0]
        hook = native.split("static int __fastcall Hook_YoufengActionCast", 1)[
            1
        ].split("static bool InstallYoufengActionCastHook", 1)[0]
        self.assertIn("kNoteCmdStartCurrentQingGong = 0x0074FBC0u", native)
        self.assertIn("kNoteCmdStartQingGong = 0x0074F480u", native)
        self.assertIn("kNoteCmdStopCurrentQingGong = 0x0074F890u", native)
        self.assertIn("start_qinggong(qinggong, 1u, 1u)", native)
        self.assertIn("g_youfeng_next_gate_qinggong, 1", command)
        self.assertIn("StartCurrentQingGongFromCast(", hook)
        self.assertIn("stop_qinggong(qinggong)", native)
        self.assertIn("if (stopped) Sleep(edge_mode == 4u ? 35u : 12u)", native)
        self.assertIn("if (started) Sleep(35u)", hook)
        self.assertIn("InstallYoufengActionCastHook()", command)
        self.assertIn("if (mode >= 2)", command)
        self.assertIn("mode > 8", command)
        self.assertIn("edge_mode >= 5u && edge_mode <= 8u", native)
        self.assertIn("edge_mode == 5u ? 24u", native)
        self.assertIn("edge_mode == 7u ? 4u", native)
        self.assertIn("edge_mode == 8u ? 8u", native)
        self.assertIn("post_stopped = stop_qinggong(qinggong)", native)
        self.assertIn("mode < 5 || post_stopped != 0", command)
        self.assertIn("InstallQingGongTraceHooks()", command)
        self.assertIn("Hook_QingGongStartTrace", native)
        self.assertIn("Hook_QingGongStopTrace", native)
        self.assertNotIn("InjectKey", command)
        self.assertNotIn("RunActionPhase", command)
        self.assertNotIn("RunE07ActionPhase", command)

    def test_generic_skill_action_experiment_is_identity_checked(self) -> None:
        self.assertEqual(int(BridgeCommand.SKILL_ACTION_EXPERIMENT), 45)
        self.assertEqual(CMD_SKILL_ACTION_EXPERIMENT, 45)
        self.assertEqual(
            required_build_capability(BridgeCommand.SKILL_ACTION_EXPERIMENT),
            "skill.probe",
        )
        result = XajhBridge(1).skill_action_experiment(0x12609, 0x2307, mode=10)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        command = native.split(
            "if (cmd == CMD_SKILL_ACTION_EXPERIMENT)", 1
        )[1].split("if (cmd ==", 1)[0]
        self.assertIn("mode == 6 && lo == 0x952Au && hi == 0x176Du", command)
        self.assertIn("perform_type == 4u", command)
        self.assertIn("busy == 0u", command)
        self.assertIn("id_perform == 0xFFFFFFFFu", command)
        self.assertIn("mode == 7 && lo == 0x952Au && hi == 0x176Du", command)
        self.assertIn("gate0 == 5u && gate1 == 350u", command)
        self.assertIn("gate2 == hi", command)
        self.assertIn("id_perform != 0u && id_perform != 0xFFFFFFFFu", command)
        self.assertIn("gate0 == 0u", command)
        self.assertIn("gate1 == 0u", command)
        self.assertIn("gate2 == 0u", command)
        self.assertIn("(mode == 6 || mode == 7) ? !kuangfeng_pulse", command)
        self.assertIn("(mode == 6 || mode == 7) ? 6u : 4u", command)
        self.assertIn("(mode != 6 && mode != 7) || post_stopped", command)
        self.assertIn("mode == 8", command)
        self.assertIn("InstallSkillActionTraceHooks()", command)
        self.assertIn("g_kuangfeng_tail_auto_remaining, 10", command)
        self.assertIn("GetTickCount() + 12000u", command)
        self.assertIn("mode == 9", command)
        self.assertIn("StartCurrentQingGongFromCast(", command)
        self.assertIn("cancel((uint8_t*)net + kCancelSessionThisOff, 1u,", command)
        self.assertIn("id_perform)", command)
        self.assertIn("SKTRACE_QINGGONG_GATE", command)
        self.assertIn("SKTRACE_EXACT_CANCEL", command)
        self.assertIn("InstallYoufengNextGateHook()", command)
        self.assertIn("g_youfeng_next_gate_skill, (LONG)lo", command)
        self.assertIn("g_youfeng_next_gate_config, (LONG)hi", command)
        self.assertNotIn("OnSkillStopped", command)
        self.assertNotIn("RunActionPhase", command)
        self.assertNotIn("InjectKey", command)
        self.assertNotIn("RunE07ActionPhase", command)
        hook = native.split("static void __fastcall Hook_OnPerformSkillTrace", 1)[
            1
        ].split("static bool InstallSkillActionTraceHooks", 1)[0]
        self.assertIn("a02 == 1u && a04 == 350u", hook)
        self.assertIn("a03 != 0u &&", hook)
        self.assertIn("g_kuangfeng_tail_pending_perform, (LONG)a03", hook)
        self.assertIn("GetTickCount() + 320u", hook)
        pending = native.split("static void ProcessPendingKuangfengTail", 1)[
            1
        ].split("static void ReadSkillTraceFields", 1)[0]
        self.assertIn("cast != expected_cast", pending)
        self.assertIn("gate0 == 5u && gate1 == 350u", pending)
        self.assertIn("id_perform == expected_perform", pending)
        self.assertIn("StartCurrentQingGongFromCast(", pending)
        self.assertIn("started && post_stopped", pending)
        self.assertIn("DropKuangfengTailPending(1)", pending)
        self.assertIn("DropKuangfengTailPending(2)", pending)
        self.assertIn("g_kuangfeng_tail_pending_dropped", command)
        self.assertIn("ProcessPendingKuangfengTail();", native)
        self.assertNotIn("RunE07ActionPhase", hook)
        self.assertNotIn("InjectKey", hook)
        self.assertNotIn("RunE07ActionPhase", pending)
        self.assertNotIn("InjectKey", pending)
        self.assertIn("mode != 11 && mode != 12 && mode != 13", command)
        self.assertIn("mode == 11 || mode == 13", command)
        self.assertIn("mode == 12", command)
        self.assertIn("lo != 0x9563u || hi != 0x195Eu", command)
        self.assertIn("YOUFENG_ULTIMATE_TAIL armed exact 0x9563/0x195E", command)
        self.assertIn("g_youfeng_ultimate_tail_pending_dropped", command)
        ultimate_hook = hook.split(
            "g_youfeng_ultimate_tail_auto_on", 1
        )[1]
        self.assertIn("e.skill_0 == 0x9563u", ultimate_hook)
        self.assertIn("e.skill_10 == 0x195Eu", ultimate_hook)
        self.assertIn("a02 == 1u && a04 == 1600u", ultimate_hook)
        ultimate_pending = native.split(
            "static void ProcessPendingYoufengUltimateTail", 1
        )[1].split("static void ReadSkillTraceFields", 1)[0]
        self.assertIn("cast != expected_cast", ultimate_pending)
        self.assertIn("busy == 1u && perform_type == 4u", ultimate_pending)
        self.assertIn("gate0 == 5u && gate1 == 1600u", ultimate_pending)
        self.assertIn("gate2 == 0x195Eu", ultimate_pending)
        self.assertIn("id_perform == expected_perform", ultimate_pending)
        self.assertIn("StartCurrentQingGongFromCast(", ultimate_pending)
        self.assertIn("started && post_stopped", ultimate_pending)
        self.assertIn("ProcessPendingYoufengUltimateTail();", native)
        self.assertIn("ProcessYoufengUltimateCooldownClear();", native)
        self.assertIn("resolved_key != 0xAu", native)
        self.assertIn("*(uint32_t*)(record + 4) != 10000u", native)
        self.assertIn("allow_cleared_mode == 2", native)
        self.assertIn("target_identity || cleared_identity", native)
        self.assertIn("due=0 is the explicit persistent mode", native)
        self.assertIn("const bool next_gate_ok = mode != 13 || InstallYoufengNextGateHook();", command)
        self.assertIn("g_youfeng_next_gate_skill, 0x9563", command)
        self.assertIn("g_youfeng_next_gate_config, 0x195E", command)
        self.assertIn("g_youfeng_next_gate_allow_cleared, 2", command)
        self.assertIn("g_youfeng_next_gate_due, 0", command)
        self.assertIn("g_youfeng_next_gate_on, 1", command)
        self.assertIn("if (ultimate_chain_on) StopYoufengNextGateHook();", command)
        self.assertIn("gate=%ld", command)
        self.assertNotIn("RunActionPhase", ultimate_pending)
        self.assertNotIn("InjectKey", ultimate_pending)

    def test_autoplay_stop_is_a_main_thread_command(self) -> None:
        self.assertEqual(int(BridgeCommand.AUTOPLAY_STOP), 31)
        self.assertEqual(CMD_AUTOPLAY_STOP, 31)
        result = XajhBridge(1).autoplay_stop()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (cmd == CMD_AUTOPLAY_STOP)", native)
        self.assertIn("AUTOPLAY_STOP ok", native)

    def test_autoplay_start_bypass_is_exact_ui_thread_command(self) -> None:
        self.assertEqual(int(BridgeCommand.AUTOPLAY_START_BYPASS), 35)
        self.assertEqual(CMD_AUTOPLAY_START_BYPASS, 35)
        self.assertEqual(
            required_capability(BridgeCommand.AUTOPLAY_START_BYPASS),
            BridgeCapability.AUTOPLAY,
        )
        result = XajhBridge(1).autoplay_start_bypass()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (cmd == CMD_AUTOPLAY_START_BYPASS)", native)
        self.assertIn("kNoteAutoPlayFrameBtnStart = 0x00AFE5C0u", native)
        self.assertIn("kNoteAutoPlaySkillJe1Disp = 0x00C55D88u", native)
        self.assertIn("kNoteAutoPlaySkillJe2Disp = 0x00C55D8Eu", native)
        self.assertIn("kNoteAutoPlayLeaderJne = 0x00C55DA2u", native)
        self.assertIn("__finally", native)

    def test_autoplay_seed_follow_is_local_ui_thread_command(self) -> None:
        self.assertEqual(int(BridgeCommand.AUTOPLAY_SEED_FOLLOW), 37)
        self.assertEqual(CMD_AUTOPLAY_SEED_FOLLOW, 37)
        self.assertEqual(
            required_capability(BridgeCommand.AUTOPLAY_SEED_FOLLOW),
            BridgeCapability.AUTOPLAY,
        )
        result = XajhBridge(1).autoplay_seed_follow(0x123456789)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "bridge not open")
        empty = XajhBridge(1).autoplay_seed_follow(0)
        self.assertFalse(empty.ok)
        self.assertEqual(empty.error, "AUTOPLAY_FOLLOW_TARGET_ID_0")
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("if (cmd == CMD_AUTOPLAY_SEED_FOLLOW)", native)
        block = native.split("if (cmd == CMD_AUTOPLAY_SEED_FOLLOW)", 1)[1].split(
            "if (cmd ==", 1
        )[0]
        self.assertIn("kTeamMemberIdOff", block)
        self.assertIn("set_state(manager, 4u)", block)
        self.assertNotIn("kNotePktSend", block)

    def test_cast_skill_facade_requires_skill_id_and_open_bridge(self) -> None:
        """Live cast path accepts skill_id; rejects empty id without submitting."""
        empty = XajhBridge(1).cast_skill(0)
        self.assertFalse(empty.ok)
        self.assertEqual(empty.error, "CAST_SKILL_ID_0")
        # Without shared memory open, submit fails closed (not stale-symbol).
        result = XajhBridge(1).cast_skill(0xE07)
        self.assertFalse(result.ok)
        self.assertNotEqual(result.error, "UNSUPPORTED_STALE_SYMBOL")

    def test_on_skill_stopped_is_command_22(self) -> None:
        """CMD_ONSKILL_STOPPED must be 22 and match both header and Python."""
        from app.core.bridge_protocol import BridgeCommand

        self.assertEqual(int(BridgeCommand.ONSKILL_STOPPED), 22)
        text = (
            Path(__file__)
            .resolve()
            .parents[1]
            / "native/xajh_bridge/bridge_protocol.h"
        ).read_text(encoding="utf-8")
        self.assertIn("CMD_ONSKILL_STOPPED = 22", text)

    def test_on_skill_stopped_is_guarded_by_build_capability(self) -> None:
        from app.core.bridge_protocol import BridgeCommand, required_build_capability

        self.assertEqual(
            required_build_capability(BridgeCommand.ONSKILL_STOPPED),
            "skill.probe",
        )
        # CANCEL_SESSION keeps the broader session.cancel profile capability.
        self.assertEqual(
            required_build_capability(BridgeCommand.CANCEL_SESSION),
            "session.cancel",
        )
        self.assertEqual(SHARED_SIZE, 200)
        self.assertEqual(PROTOCOL_VERSION, 2)

    def test_legacy_mapping_never_reads_v2_tail(self) -> None:
        bridge = XajhBridge(1)
        bridge._view = 1
        bridge._mapped_size = 184
        self.assertEqual(bridge.protocol_version, 0)
        bridge._view = 0

    def test_legacy_bridge_only_allows_diagnostic_ping(self) -> None:
        raw = ctypes.create_string_buffer(184)
        bridge = XajhBridge(1)
        bridge._view = ctypes.addressof(raw)
        bridge._mapped_size = 184
        bridge._set_u32(OFF_MAGIC, BRIDGE_MAGIC)
        out = bridge._call_unlocked(BridgeCommand.HOST_MOVE)
        bridge._view = 0
        self.assertFalse(out.ok)
        self.assertEqual(out.error, "LEGACY_BRIDGE_RESTART_GAME")

    def test_pending_business_slot_is_never_overwritten(self) -> None:
        raw = ctypes.create_string_buffer(SHARED_SIZE)
        bridge = XajhBridge(1)
        bridge._view = ctypes.addressof(raw)
        bridge._mapped_size = SHARED_SIZE
        bridge._set_u32(OFF_MAGIC, BRIDGE_MAGIC)
        bridge._set_u32(OFF_PROTOCOL_VERSION, PROTOCOL_VERSION)
        bridge._set_u32(OFF_CAPABILITIES, int(BridgeCapability.UI_INPUT))
        bridge._set_u32(OFF_CMD, int(BridgeCommand.UI_CLICK))
        bridge._set_i32(OFF_STATUS, int(BridgeStatus.PENDING))
        out = bridge._call_unlocked(BridgeCommand.UI_CLICK)
        self.assertFalse(out.ok)
        self.assertIn("still pending", out.error)
        self.assertEqual(bridge._i32(OFF_STATUS), int(BridgeStatus.PENDING))
        bridge._view = 0

    def test_pending_is_published_after_all_arguments(self) -> None:
        source = inspect.getsource(XajhBridge._call_unlocked)
        publish = source.index(
            "self._set_i32(OFF_STATUS, int(BridgeStatus.PENDING))"
        )
        for write in (
            "self._set_f32(OFF_Z, z)",
            "self._set_i32(OFF_MODE, mode)",
            "self._set_u32(OFF_ID_LO, id_lo)",
            "self._set_u32(OFF_ID_HI, id_hi)",
            "self._set_i32(OFF_TID, tid)",
            "ctypes.memset(self._view + OFF_ERR, 0, 128)",
        ):
            self.assertLess(source.index(write), publish)

    def test_bridge_timeout_keeps_waiting_for_late_ack(self) -> None:
        raw = ctypes.create_string_buffer(SHARED_SIZE)
        logs: list[str] = []
        bridge = XajhBridge(123, log=logs.append)
        bridge._view = ctypes.addressof(raw)
        bridge._mapped_size = SHARED_SIZE
        bridge._set_u32(OFF_MAGIC, BRIDGE_MAGIC)
        bridge._set_u32(OFF_PROTOCOL_VERSION, PROTOCOL_VERSION)
        bridge._set_u32(OFF_CAPABILITIES, int(BridgeCapability.UI_INPUT))

        def complete_late() -> None:
            time.sleep(0.05)
            bridge._set_u32(OFF_ACK_SEQ, 1)
            bridge._set_i32(OFF_STATUS, int(BridgeStatus.OK))

        worker = threading.Thread(target=complete_late)
        worker.start()
        try:
            with patch("app.core.xajh_bridge._pid_alive", return_value=True):
                out = bridge._call_unlocked(BridgeCommand.UI_CLICK, timeout_ms=1)
        finally:
            worker.join(1)
            bridge._view = 0
        self.assertTrue(out.ok)
        self.assertTrue(any("holding until completion" in line for line in logs))

    def test_stale_ping_times_out_without_waiting_forever(self) -> None:
        raw = ctypes.create_string_buffer(SHARED_SIZE)
        bridge = XajhBridge(123)
        bridge._view = ctypes.addressof(raw)
        bridge._mapped_size = SHARED_SIZE
        bridge._set_u32(OFF_MAGIC, BRIDGE_MAGIC)
        bridge._set_u32(OFF_PROTOCOL_VERSION, PROTOCOL_VERSION)
        with patch("app.core.xajh_bridge._pid_alive", return_value=True):
            out = bridge._call_unlocked(CMD_PING, timeout_ms=1)
        bridge._view = 0
        self.assertFalse(out.ok)
        self.assertEqual(out.error, "BRIDGE_PING_TIMEOUT")

    def test_snapshot_timeout_returns_without_clearing_pending_slot(self) -> None:
        raw = ctypes.create_string_buffer(SHARED_SIZE)
        bridge = XajhBridge(123)
        bridge._view = ctypes.addressof(raw)
        bridge._mapped_size = SHARED_SIZE
        bridge._set_u32(OFF_MAGIC, BRIDGE_MAGIC)
        bridge._set_u32(OFF_PROTOCOL_VERSION, PROTOCOL_VERSION)
        bridge._set_u32(OFF_CAPABILITIES, int(BridgeCapability.SESSION))
        with patch(
            "app.core.xajh_bridge.required_build_capability", return_value=None
        ), patch("app.core.xajh_bridge._pid_alive", return_value=True):
            out = bridge._call_unlocked(BridgeCommand.HOST_SNAPSHOT, timeout_ms=1)
        self.assertFalse(out.ok)
        self.assertEqual(out.error, "BRIDGE_SNAPSHOT_TIMEOUT")
        self.assertEqual(bridge._i32(OFF_STATUS), int(BridgeStatus.PENDING))
        bridge._view = 0

    def test_unsafe_key_trace_is_rejected_without_mapping(self) -> None:
        bridge = XajhBridge(1)
        result = bridge.key_trace()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "UNSUPPORTED_UNSAFE_HOOK")

    def test_key_force_maps_to_ui_input_memory_path(self) -> None:
        self.assertEqual(
            required_build_capability(BridgeCommand.KEY_FORCE),
            "ui.action",
        )
        self.assertEqual(
            required_capability(BridgeCommand.KEY_FORCE),
            BridgeCapability.UI_INPUT,
        )

    def test_unverified_actions_require_independent_live_capabilities(self) -> None:
        self.assertEqual(
            required_build_capability(BridgeCommand.PICKUP_NOTE),
            "matter.pickup.live",
        )
        self.assertEqual(
            required_build_capability(BridgeCommand.TASK_ACCEPT),
            "task.accept.live",
        )
        self.assertEqual(
            required_build_capability(BridgeCommand.TASK_COMPLETE),
            "task.complete.live",
        )
        self.assertEqual(
            required_build_capability(BridgeCommand.AUTO_CLICK_DYN_MATTER),
            "matter.interact.dyn.live",
        )
        self.assertEqual(
            required_build_capability(BridgeCommand.CANCEL_SESSION),
            "session.cancel",
        )
        self.assertEqual(
            required_build_capability(BridgeCommand.KEY_FORCE),
            "ui.action",
        )

    def test_cancel_session_facade_submits_only_the_dedicated_command(self) -> None:
        bridge = XajhBridge(123)
        with patch.object(bridge, "call") as call:
            bridge.cancel_session(hwnd=456, timeout_ms=789)
        call.assert_called_once_with(
            int(BridgeCommand.CANCEL_SESSION),
            hwnd=456,
            timeout_ms=789,
        )

    def test_on_skill_stopped_facade_submits_only_the_dedicated_command(self) -> None:
        bridge = XajhBridge(123)
        with patch.object(bridge, "call") as call:
            bridge.on_skill_stopped(hwnd=456, timeout_ms=789)
        call.assert_called_once_with(
            int(BridgeCommand.ONSKILL_STOPPED),
            hwnd=456,
            timeout_ms=789,
            mode=0,
        )
        source = inspect.getsource(XajhBridge.on_skill_stopped)
        self.assertIn("cast+0x10/+0x14/+0x18", source)
        self.assertIn("0x582350", source)
        self.assertNotIn("read-only local state flush", source)

    def test_native_crash_guards_remain_enabled(self) -> None:
        dll = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        injector = (ROOT / "native/xajh_bridge/injector.cpp").read_text(
            encoding="utf-8"
        )
        header = (ROOT / "native/xajh_bridge/bridge_protocol.h").read_text(
            encoding="utf-8"
        )
        self.assertIn("UNLOAD_DISABLED_RESTART_GAME", dll)
        self.assertIn("UNSUPPORTED_UNSAFE_HOOK", dll)
        self.assertIn("UNSUPPORTED_CLIENT_BUILD", dll)
        self.assertEqual(dll.count("UNSUPPORTED_UNVERIFIED_ACTION"), 2)
        self.assertIn("TASK_COMPLETE ret=%u taskNpcId=0x%X", dll)
        self.assertIn("TASK_ACCEPT ok ret=%u id=%u", dll)
        task_accept = dll.split("if (cmd == CMD_TASK_ACCEPT)", 1)[1].split(
            "if (cmd == CMD_TASK_COMPLETE)", 1
        )[0]
        self.assertNotIn("UNSUPPORTED_UNVERIFIED_ACTION", task_accept)
        self.assertIn("push task_id", task_accept)
        self.assertIn("ResolveTaskMgrThis", task_accept)
        task_complete = dll.split("if (cmd == CMD_TASK_COMPLETE)", 1)[1].split(
            "if (cmd == CMD_REHOOK)", 1
        )[0]
        self.assertNotIn("UNSUPPORTED_UNVERIFIED_ACTION", task_complete)
        self.assertIn("push task_npc_id", task_complete)
        self.assertIn("mov ecx, task_npc_id", task_complete)
        self.assertIn("mov call_ret, eax", task_complete)
        self.assertIn("kKnownTimestamp = 0x56736608u", dll)
        self.assertIn("kKnownImageSize = 0x038A5000u", dll)
        self.assertIn("if (cmd == CMD_CANCEL_SESSION)", dll)
        self.assertIn("if (cmd == CMD_CHANGE_LINE)", dll)
        self.assertIn("kNoteChangeLine = 0x00CCBC70u", dll)
        self.assertIn("kNoteCancelSession = 0x00CC7010u", dll)
        self.assertEqual(int(BridgeCommand.CHANGE_LINE), 25)
        self.assertEqual(
            required_capability(BridgeCommand.CHANGE_LINE),
            BridgeCapability.LINE,
        )
        self.assertIn("kHostSessionStateOff = 0x41Cu", dll)
        self.assertIn("queued opcode=0x21", dll)
        self.assertIn("forced queue opcode=0x21", dll)
        self.assertNotIn("CANCEL_SESSION already idle", dll)
        cancel = dll.split("if (cmd == CMD_CANCEL_SESSION)", 1)[1].split(
            "if (cmd == CMD_ONSKILL_STOPPED)", 1
        )[0]
        self.assertIn("ResolveNetRoot", cancel)
        self.assertIn("FnCancelSession", cancel)
        # CMD_ONSKILL_STOPPED identity-match source contract (not a runtime proof).
        self.assertIn("if (cmd == CMD_ONSKILL_STOPPED)", dll)
        self.assertIn("cmd == CMD_ONSKILL_STOPPED", dll)
        onskill = dll.split("if (cmd == CMD_ONSKILL_STOPPED)", 1)[1].split(
            "if (cmd == CMD_TASK_ACCEPT)", 1
        )[0]
        self.assertIn("cast_this + 0x10", onskill)
        self.assertIn("cast_this + 0x14", onskill)
        self.assertIn("cast_this + 0x18", onskill)
        self.assertIn("identity=0", onskill)
        self.assertIn("event_buf", onskill)
        self.assertIn("fn(cast_this, event_buf, 0u, 0u, 0u, cont)", onskill)
        self.assertIn("0x00754BA0u", onskill)
        self.assertIn("mode == 1 || mode == 2", onskill)
        self.assertIn("cleared=%d", onskill)
        self.assertIn("ONSKILL_STOPPED SEH", onskill)
        self.assertIn("CommandRequiresFixedRva", dll)
        self.assertIn("UNSUPPORTED_CLIENT_BUILD", dll)
        self.assertIn(
            "InterlockedCompareExchange(&g_command_running, 1, 0)", dll
        )
        self.assertIn("InterlockedExchange(&g_command_running, 0)", dll)
        self.assertIn("path allocation retained", injector)
        caps = header.split("kBridgeCapabilities", 1)[1].split(";", 1)[0]
        # KEY_HOLD advertises CAP_KEY_HOOK; KEY_TRACE remains fail-closed in Python/DLL.
        self.assertIn("CAP_KEY_HOOK", caps)
        self.assertIn("CAP_LINE", caps)

        # KEY_FORCE is memory+InjectKey+PostMessage (active); KEY_TRACE remains disabled (#if 0).
        key_force = dll.split("if (cmd == CMD_KEY_FORCE)", 1)[1].split(
            "if (cmd == CMD_KEY_TRACE)", 1
        )[0]
        self.assertIn("FormatForceDiag", key_force)
        self.assertIn("SyncForcedShiftState", key_force)
        self.assertNotIn("InstallKeyStateHooks", key_force)
        self.assertIn("MaintainForcedShift", dll)
        self.assertIn("SoftSendShiftGaks", dll)
        self.assertIn("PostShiftKeyMsg", dll)
        self.assertIn("InjectShiftKeyEvent", dll)
        self.assertIn("CallUpdateKeys", dll)
        self.assertIn("FormatForceDiag", dll)
        self.assertIn("KEY_FORCE ON f=0x%X", dll)
        self.assertIn("KEY_FORCE HOLD f=0x%X", dll)
        self.assertIn("level_only", key_force)
        self.assertIn("cmd == CMD_KEY_FORCE", dll)
        # Background SoftSend must NOT gate on foreground (GAKS is process-global).
        soft = dll.split("static int SoftSendShiftGaks", 1)[1].split(
            "static int SyncForcedShiftState", 1
        )[0]
        self.assertNotIn("GetForegroundWindow()", soft)
        self.assertIn("SendInput", soft)
        # PostMessage path also must not FG-steal (background reticle).
        post = dll.split("static int PostShiftKeyMsg", 1)[1].split(
            "static int SyncForcedShiftState", 1
        )[0]
        self.assertNotIn("SetForegroundWindow", post)
        self.assertIn("PostMessageW", post)
        # Maintain re-seeds via SyncForcedShiftState level_only (gate+UpdateKeys).
        maintain = dll.split("static void MaintainForcedShift", 1)[1].split(
            "static bool PatchIatSlot", 1
        )[0]
        self.assertIn("SyncForcedShiftState(true", maintain)

        trace = dll.index("if (cmd == CMD_KEY_TRACE)")
        aq_submit = dll.index("if (cmd == CMD_AQ_SUBMIT)")
        disabled_end = dll.index("#endif", trace)
        self.assertLess(disabled_end, aq_submit)
        self.assertIn("UNSUPPORTED_UNSAFE_HOOK", dll[trace:disabled_end])
        self.assertNotIn("FreeLibraryAndExitThread", dll)


    def test_cast_skill_live_path_source(self) -> None:
        """CMD_CAST_SKILL must use live 0x53E110, not stale 0x755F10."""
        from pathlib import Path as _P
        dll = _P("native/xajh_bridge/dllmain.cpp").read_text(encoding="utf-8", errors="replace")
        self.assertEqual(int(BridgeCommand.CAST_SKILL), 18)
        self.assertIn("if (cmd == CMD_CAST_SKILL)", dll)
        self.assertIn("cmd == CMD_CAST_SKILL", dll)
        cast = dll.split("if (cmd == CMD_CAST_SKILL)", 1)[1].split("if (cmd ==", 1)[0]
        self.assertIn("kNoteCastOuter", cast)
        self.assertIn("0x53E110", dll)
        self.assertIn("FnCastOuter", cast)
        self.assertIn("CAST_SKILL SEH", cast)
        self.assertNotIn("UNSUPPORTED_STALE_SYMBOL", cast)
        self.assertIn("kNoteSkillById", cast)
        # Python must not hard-fail before submit
        py = _P("app/core/xajh_bridge.py").read_text(encoding="utf-8")
        # remove early stale reject
        self.assertNotIn("0x755F10 is not a callable entry", py)
        self.assertIn("def cast_skill", py)
        self.assertEqual(
            required_build_capability(BridgeCommand.CAST_SKILL),
            "skill.probe",
        )

    def test_session_send_bypass_command_source(self) -> None:
        """CMD_SESSION_SEND_BYPASS must be signature-verified and reversible."""
        native = (ROOT / "native/xajh_bridge/dllmain.cpp").read_text(
            encoding="utf-8"
        )
        self.assertEqual(int(BridgeCommand.SESSION_SEND_BYPASS), 49)
        self.assertIn("CMD_SESSION_SEND_BYPASS = 49", (ROOT / "native/xajh_bridge/bridge_protocol.h").read_text(encoding="utf-8"))
        self.assertIn("if (cmd == CMD_SESSION_SEND_BYPASS)", native)
        self.assertIn("ApplySessionSendBypass()", native)
        self.assertIn("RestoreSessionSendBypass()", native)
        self.assertIn("kNoteSessionSendGate = 0x00CC6D51u", native)
        self.assertIn("kSessionSendGateSignature", native)
        self.assertIn("0x8B, 0x0E, 0x8B, 0x56, 0x04, 0x3B, 0xCA, 0x73, 0x40", native)
        self.assertIn("cmd == CMD_SESSION_SEND_BYPASS", native)
        self.assertIn("*p = 0xEB;", native)
        self.assertIn("*p = 0x73;", native)
        self.assertEqual(
            required_build_capability(BridgeCommand.SESSION_SEND_BYPASS),
            "skill.probe",
        )
        self.assertEqual(
            required_capability(BridgeCommand.SESSION_SEND_BYPASS),
            BridgeCapability.SESSION,
        )
        py = (ROOT / "app/core/xajh_bridge.py").read_text(encoding="utf-8")
        self.assertIn("def session_send_bypass", py)
        self.assertIn("CMD_SESSION_SEND_BYPASS", py)


if __name__ == "__main__":
    unittest.main()
