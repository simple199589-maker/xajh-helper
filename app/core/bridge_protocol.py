# -*- coding: utf-8 -*-
"""Versioned shared-memory contract for xajh_bridge.dll."""
from __future__ import annotations

from enum import IntEnum, IntFlag


BRIDGE_MAGIC = 0x47524258
PROTOCOL_VERSION = 2
# Increment when native behavior changes without changing the shared layout.
# A healthy older DLL may otherwise be silently reused until the game exits.
BRIDGE_BUILD_ID = 2026082603
LEGACY_SHARED_SIZE = 184
SHARED_SIZE = 200


class BridgeStatus(IntEnum):
    IDLE = 0
    PENDING = 1
    OK = 2
    ERROR = 3


class BridgeCommand(IntEnum):
    IDLE = 0
    PING = 1
    SET_TARGET = 2
    PICK_ITEM = 3
    HOST_MOVE = 4
    CHOICE_OBJECT = 5
    PICKUP_NOTE = 6
    REHOOK = 7
    UNLOAD = 8
    AUTO_CLICK_MATTER = 9
    AUTO_CLICK_DYN_MATTER = 10
    CAPTURE_SCREEN = 11
    UI_CLICK = 12
    AQ_SUBMIT = 13
    TASK_ACCEPT = 14
    TASK_COMPLETE = 15
    INSTANCE_ENTER = 16
    UI_KEY = 17
    CAST_SKILL = 18
    KEY_FORCE = 19
    KEY_TRACE = 20
    CANCEL_SESSION = 21
    ONSKILL_STOPPED = 22
    KEY_DIAG = 23
    KEY_HOLD = 24
    CHANGE_LINE = 25
    USE_ITEM_IN_PACKAGE = 26
    TEAM_INVITE = 27
    NPC_TALK_SELECT = 28
    NPC_HOST_SELECT = 29
    HOST_CONTEXT = 30
    AUTOPLAY_STOP = 31
    ITEM_USE_TRACE = 32
    HOST_SNAPSHOT = 33
    SKILL_ACTION_TRACE = 34
    AUTOPLAY_START_BYPASS = 35
    SKILL_INTERRUPT_67 = 36
    AUTOPLAY_SEED_FOLLOW = 37
    YOUFENG_INTERNAL_CHAIN = 38
    YOUFENG_NEXT_GATE = 39
    YOUFENG_MASH_CAST = 40
    YOUFENG_FRESH_GATE = 41
    YOUFENG_EXACT_CANCEL_GATE = 42
    YOUFENG_PHASE_GATE = 43
    YOUFENG_QINGGONG_GATE = 44
    SKILL_ACTION_EXPERIMENT = 45
    DUMMY_DAMAGE_STAT = 46
    # Arm/disarm passive CG skip hooks (PlayCG / PlayBlackEdge).
    CG_SKIP = 47
    # Execute the game-confirmed team follow on/off callbacks on its UI thread.
    TEAM_FOLLOW = 48
    # Bypass the client SessionSend send-rate gate (0xCC6CC0 head<cap reject).
    # mode: 1=arm (patch 0xCC6D51 jae->jmp), 2=status, 0=disarm (restore bytes).
    SESSION_SEND_BYPASS = 49
    # Read-only monotonic combat activity counters for dungeon 纯站街 detection.
    # mode: 1=arm/reset, 2=status, 0=disarm. ret=attack_seq, tid=self_damage_seq;
    # note carries full state (attack_seq/self_damage_seq/total/host_target/resolved).
    COMBAT_MONITOR = 50
    # Dump UTF-16 caption text under Win_InstanceConfig / Win_TrackFrame on the
    # game UI thread (bypasses the scene-settle CRT gate). id_lo: 0=InstanceConfig,
    # 1=TrackFrame, 2=both. err returns the concatenated caption lines.
    DUMP_UI_TEXT = 51
    # Native selected-target submit trace / narrow dungeon guard at 0x7493B0.
    # mode: 1=install/reset trace-only, 2=status/latest, 3=enable guard,
    # 4=disable guard, 0=stop+dump TSV.
    TARGET_SUBMIT_TRACE = 52
    # Configure native dungeon guard TID set: mode=1 clear, 2 add id_lo, 3 status.
    DUNGEON_TARGET_RULES = 53
    JIANGLONG_RUNTIME_RESOLVE = 54
    QUICK_TEAM_FOLLOW = 55
    # Read-only live NPC/Matter lookup for 武尊堂 challenge card.
    OBJECT_SCAN = 56


class BridgeCapability(IntFlag):
    NONE = 0
    TARGET = 1 << 0
    PICKUP = 1 << 1
    MOVE = 1 << 2
    INTERACT = 1 << 3
    CAPTURE = 1 << 4
    UI_INPUT = 1 << 5
    TASK = 1 << 6
    INSTANCE = 1 << 7
    KEY_HOOK = 1 << 8
    SESSION = 1 << 9
    LINE = 1 << 10
    NPC_TALK = 1 << 11
    AUTOPLAY = 1 << 12


DEFAULT_CAPABILITIES = (
    BridgeCapability.TARGET
    | BridgeCapability.PICKUP
    | BridgeCapability.MOVE
    | BridgeCapability.INTERACT
    | BridgeCapability.CAPTURE
    | BridgeCapability.UI_INPUT
    | BridgeCapability.TASK
    | BridgeCapability.INSTANCE
    | BridgeCapability.KEY_HOOK
    | BridgeCapability.SESSION
    | BridgeCapability.LINE
    | BridgeCapability.NPC_TALK
    | BridgeCapability.AUTOPLAY
)


OFF_MAGIC = 0
OFF_SEQ = 4
OFF_CMD = 8
OFF_STATUS = 12
OFF_RET = 16
OFF_BASE = 20
OFF_HWND = 24
OFF_X = 28
OFF_Y = 32
OFF_Z = 36
OFF_MODE = 40
OFF_ID_LO = 44
OFF_ID_HI = 48
OFF_TID = 52
OFF_ERR = 56
OFF_PROTOCOL_VERSION = 184
OFF_STRUCT_SIZE = 188
OFF_CAPABILITIES = 192
OFF_ACK_SEQ = 196


COMMAND_CAPABILITY = {
    BridgeCommand.SET_TARGET: BridgeCapability.TARGET,
    BridgeCommand.CHOICE_OBJECT: BridgeCapability.TARGET,
    BridgeCommand.PICK_ITEM: BridgeCapability.PICKUP,
    BridgeCommand.PICKUP_NOTE: BridgeCapability.PICKUP,
    BridgeCommand.HOST_MOVE: BridgeCapability.MOVE,
    BridgeCommand.AUTO_CLICK_MATTER: BridgeCapability.INTERACT,
    BridgeCommand.AUTO_CLICK_DYN_MATTER: BridgeCapability.INTERACT,
    BridgeCommand.CAPTURE_SCREEN: BridgeCapability.CAPTURE,
    BridgeCommand.UI_CLICK: BridgeCapability.UI_INPUT,
    BridgeCommand.UI_KEY: BridgeCapability.UI_INPUT,
    BridgeCommand.AQ_SUBMIT: BridgeCapability.UI_INPUT,
    BridgeCommand.TASK_ACCEPT: BridgeCapability.TASK,
    BridgeCommand.TASK_COMPLETE: BridgeCapability.TASK,
    BridgeCommand.INSTANCE_ENTER: BridgeCapability.INSTANCE,
    BridgeCommand.CHANGE_LINE: BridgeCapability.LINE,
    BridgeCommand.NPC_TALK_SELECT: "npc.talk.select",
    BridgeCommand.TEAM_INVITE: BridgeCapability.TASK,
    BridgeCommand.TEAM_FOLLOW: BridgeCapability.UI_INPUT,
    BridgeCommand.NPC_TALK_SELECT: BridgeCapability.NPC_TALK | BridgeCapability.UI_INPUT,
    BridgeCommand.NPC_HOST_SELECT: BridgeCapability.NPC_TALK | BridgeCapability.UI_INPUT,
    # Memory + InjectKey + soft SendInput (no IAT). Uses UI_INPUT not KEY_HOOK.
    BridgeCommand.KEY_FORCE: BridgeCapability.UI_INPUT,
    # Solution-2: IAT/inline force map (default no SoftSend).
    BridgeCommand.KEY_HOLD: BridgeCapability.KEY_HOOK | BridgeCapability.UI_INPUT,
    BridgeCommand.KEY_TRACE: BridgeCapability.KEY_HOOK,
    BridgeCommand.CANCEL_SESSION: BridgeCapability.SESSION,
    BridgeCommand.ONSKILL_STOPPED: BridgeCapability.SESSION,
    BridgeCommand.CAST_SKILL: BridgeCapability.SESSION,
    # Main-thread plg::UseItemInPackage (export, no fixed-RVA)
    BridgeCommand.USE_ITEM_IN_PACKAGE: BridgeCapability.INTERACT,
    # CECAutoPlay::StopAutoPlay must run on the game UI thread.
    BridgeCommand.AUTOPLAY_STOP: BridgeCapability.AUTOPLAY,
    # Fixed-build native UseItem return counter for a selected package slot.
    BridgeCommand.ITEM_USE_TRACE: BridgeCapability.INTERACT,
    # Atomic host lifecycle + scene/position sample on the game UI thread.
    BridgeCommand.HOST_SNAPSHOT: BridgeCapability.SESSION,
    # Short-lived read-only action-request + active-session detours.
    BridgeCommand.SKILL_ACTION_TRACE: BridgeCapability.SESSION,
    # CDlgAutoPlayFrame::Btn_Start on the game UI thread. Native code keeps the
    # global feature-open gate and bypasses only skill/leader rejection edges.
    BridgeCommand.AUTOPLAY_START_BYPASS: BridgeCapability.AUTOPLAY,
    # Exact local perform interruption used by the behavior-key recovery path.
    BridgeCommand.SKILL_INTERRUPT_67: BridgeCapability.SESSION,
    # Repair dungeon mode's local follow-target snapshot after a leader bypass.
    BridgeCommand.AUTOPLAY_SEED_FOLLOW: BridgeCapability.AUTOPLAY,
    # Exact E07 action down/up plus bounded local tail cleanup for 有凤来仪.
    BridgeCommand.YOUFENG_INTERNAL_CHAIN: BridgeCapability.SESSION,
    # Bounded same-skill current-action gate window; no interrupt action/key.
    BridgeCommand.YOUFENG_NEXT_GATE: BridgeCapability.SESSION,
    # Start the next 有凤 through ActionCast's native mash branch; no E07.
    BridgeCommand.YOUFENG_MASH_CAST: BridgeCapability.SESSION,
    # Clear only the completed cast identity, then arm the bounded next gate.
    BridgeCommand.YOUFENG_FRESH_GATE: BridgeCapability.SESSION,
    # Cancel the exact server perform id, clear old identity, then normal recast.
    BridgeCommand.YOUFENG_EXACT_CANCEL_GATE: BridgeCapability.SESSION,
    # Send only 0x69/0x6A for 有凤's own config, then use the fresh gate.
    BridgeCommand.YOUFENG_PHASE_GATE: BridgeCapability.SESSION,
    # Invoke the native Space/QingGong start path inside the next-cast hook.
    BridgeCommand.YOUFENG_QINGGONG_GATE: BridgeCapability.SESSION,
    BridgeCommand.SKILL_ACTION_EXPERIMENT: BridgeCapability.SESSION,
    # Read-only one-minute damage accumulator for one exact object id.
    BridgeCommand.DUMMY_DAMAGE_STAT: BridgeCapability.SESSION,
    # Passive CG/cinematic skip hooks (PlayCG / PlayBlackEdge).
    BridgeCommand.CG_SKIP: BridgeCapability.UI_INPUT,
    # Patch the fixed-build SessionSend gate in 0xCC6CC0 (skill.probe build).
    BridgeCommand.SESSION_SEND_BYPASS: BridgeCapability.SESSION,
    # Read-only combat activity counters (SkillActionRequest + damage splitter).
    BridgeCommand.COMBAT_MONITOR: BridgeCapability.SESSION,
    # Read-only selected-target submit trace (fixed RVA, no target mutation).
    BridgeCommand.TARGET_SUBMIT_TRACE: BridgeCapability.TARGET,
    BridgeCommand.DUNGEON_TARGET_RULES: BridgeCapability.TARGET,
    BridgeCommand.JIANGLONG_RUNTIME_RESOLVE: BridgeCapability.SESSION,
    BridgeCommand.QUICK_TEAM_FOLLOW: BridgeCapability.UI_INPUT,
    BridgeCommand.OBJECT_SCAN: BridgeCapability.TARGET,
}

# Commands below depend on build-pinned native RVAs rather than PE exports.
COMMAND_BUILD_CAPABILITY = {
    BridgeCommand.CHOICE_OBJECT: "target.set",
    BridgeCommand.PICKUP_NOTE: "matter.pickup.live",
    BridgeCommand.AUTO_CLICK_MATTER: "matter.interact",
    BridgeCommand.AUTO_CLICK_DYN_MATTER: "matter.interact.dyn.live",
    BridgeCommand.CAPTURE_SCREEN: "ui.action",
    BridgeCommand.AQ_SUBMIT: "ui.action",
    BridgeCommand.TASK_ACCEPT: "task.accept.live",
    BridgeCommand.TASK_COMPLETE: "task.complete.live",
    BridgeCommand.INSTANCE_ENTER: "instance.action",
    BridgeCommand.CHANGE_LINE: "line.action",
    BridgeCommand.CANCEL_SESSION: "session.cancel",
    BridgeCommand.ONSKILL_STOPPED: "skill.probe",
    BridgeCommand.CAST_SKILL: "skill.probe",
    # InjectKey + input this offsets pinned to known fixed-RVA build.
    BridgeCommand.KEY_FORCE: "ui.action",
    BridgeCommand.KEY_HOLD: "ui.action",
    BridgeCommand.ITEM_USE_TRACE: "item.use.trace",
    BridgeCommand.HOST_SNAPSHOT: "session.cancel",
    BridgeCommand.SKILL_ACTION_TRACE: "skill.probe",
    BridgeCommand.AUTOPLAY_START_BYPASS: "ui.action",
    BridgeCommand.SKILL_INTERRUPT_67: "skill.probe",
    BridgeCommand.AUTOPLAY_SEED_FOLLOW: "ui.action",
    BridgeCommand.YOUFENG_INTERNAL_CHAIN: "skill.probe",
    BridgeCommand.YOUFENG_NEXT_GATE: "skill.probe",
    BridgeCommand.YOUFENG_MASH_CAST: "skill.probe",
    BridgeCommand.YOUFENG_FRESH_GATE: "skill.probe",
    BridgeCommand.YOUFENG_EXACT_CANCEL_GATE: "skill.probe",
    BridgeCommand.YOUFENG_PHASE_GATE: "skill.probe",
    BridgeCommand.YOUFENG_QINGGONG_GATE: "skill.probe",
    BridgeCommand.SKILL_ACTION_EXPERIMENT: "skill.probe",
    BridgeCommand.DUMMY_DAMAGE_STAT: "damage.stat",
    BridgeCommand.CG_SKIP: "ui.action",
    BridgeCommand.TEAM_FOLLOW: "ui.action",
    BridgeCommand.SESSION_SEND_BYPASS: "skill.probe",
    BridgeCommand.COMBAT_MONITOR: "skill.probe",
    BridgeCommand.TARGET_SUBMIT_TRACE: "target.set",
    BridgeCommand.DUNGEON_TARGET_RULES: "target.set",
    BridgeCommand.JIANGLONG_RUNTIME_RESOLVE: "skill.probe",
    BridgeCommand.QUICK_TEAM_FOLLOW: "ui.action",
    BridgeCommand.OBJECT_SCAN: "target.set",
}


def required_capability(command: int) -> BridgeCapability:
    try:
        return COMMAND_CAPABILITY.get(BridgeCommand(int(command)), BridgeCapability.NONE)
    except ValueError:
        return BridgeCapability.NONE


def required_build_capability(command: int) -> str | None:
    try:
        return COMMAND_BUILD_CAPABILITY.get(BridgeCommand(int(command)))
    except ValueError:
        return None
