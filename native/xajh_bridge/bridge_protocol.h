#pragma once

#include <stdint.h>

#define BRIDGE_MAGIC 0x47524258u
#define BRIDGE_PROTOCOL_VERSION 2u
#ifndef BRIDGE_BUILD_ID
#define BRIDGE_BUILD_ID 2026083001u
#endif

enum BridgeCmdId {
  CMD_IDLE = 0,
  CMD_PING = 1,
  CMD_SET_TARGET = 2,
  CMD_PICK_ITEM = 3,
  CMD_HOST_MOVE = 4,
  CMD_CHOICE_OBJECT = 5,
  CMD_PICKUP_NOTE = 6,
  CMD_REHOOK = 7,
  CMD_UNLOAD = 8,
  CMD_AUTO_CLICK_MATTER = 9,
  CMD_AUTO_CLICK_DYN_MATTER = 10,
  CMD_CAPTURE_SCREEN = 11,
  CMD_UI_CLICK = 12,
  CMD_AQ_SUBMIT = 13,
  CMD_TASK_ACCEPT = 14,
  CMD_TASK_COMPLETE = 15,
  CMD_INSTANCE_ENTER = 16,
  CMD_UI_KEY = 17,
  CMD_CAST_SKILL = 18,
  CMD_KEY_FORCE = 19,
  CMD_KEY_TRACE = 20,
  CMD_CANCEL_SESSION = 21,
  CMD_ONSKILL_STOPPED = 22,
  CMD_KEY_DIAG = 23,
  CMD_KEY_HOLD = 24,
  CMD_CHANGE_LINE = 25,
  CMD_USE_ITEM_IN_PACKAGE = 26,
  CMD_TEAM_INVITE = 27,
  CMD_NPC_TALK_SELECT = 28,
  CMD_NPC_HOST_SELECT = 29,
  CMD_HOST_CONTEXT = 30,
  CMD_AUTOPLAY_STOP = 31,
  // Observe fixed-build native UseItem(this, pack, slot, flag) returns.
  // id_lo=package, id_hi=slot, mode: 1=start/reset 2=read 0=stop.
  CMD_ITEM_USE_TRACE = 32,
  // Atomic UI-thread host lifecycle + scene/position snapshot.
  // ret=1 host present, tid=-1/0/1 unknown/alive/dead,
  // mode=scene id, x/y/z=current scene position.
  CMD_HOST_SNAPSHOT = 33,
  // Short-lived trace for SkillActionRequest plus SetCurActiveSkill.
  // mode: 1=trace, 3=legacy point suppression, 4=continuation suppression,
  // 2=status, 0=stop.
  CMD_SKILL_ACTION_TRACE = 34,
  // Invoke CDlgAutoPlayFrame::Btn_Start on the UI thread while bypassing only
  // the empty-skill and dungeon-leader rejection branches.
  CMD_AUTOPLAY_START_BYPASS = 35,
  // UI-thread perform interruption used by the behavior-key recovery path.
  // id_lo/id_hi are the required current skill/config identity.
  CMD_SKILL_INTERRUPT_67 = 36,
  // Seed dungeon-mode's cached follow target with a real non-self party member.
  // id_lo/id_hi = member player id. No team state or network packet is changed.
  CMD_AUTOPLAY_SEED_FOLLOW = 37,
  // Reproduce the E07 action transaction used by X, then perform the bounded
  // local tail cleanup used by Space. No X/Space key state is synthesized.
  CMD_YOUFENG_INTERNAL_CHAIN = 38,
  // Temporarily allow the matching skill through the current-action rejection
  // in 0x7546F0. This does not submit E07 or synthesize X/Space.
  CMD_YOUFENG_NEXT_GATE = 39,
  // Submit 有凤 itself through ActionCast's native current-action mash path.
  // No E07 action and no X/Space state is involved.
  CMD_YOUFENG_MASH_CAST = 40,
  // Call OnSkillStopped only to clear the completed 有凤 cast identity, then
  // permit the next normal key cast through the same bounded current gate.
  CMD_YOUFENG_FRESH_GATE = 41,
  // Cancel opcode 0x21 with cast+0x6C's exact current perform id, clear only
  // the old cast identity, then arm the next normal-input gate.
  CMD_YOUFENG_EXACT_CANCEL_GATE = 42,
  // Send only native phase-down/up packets for config 0x34D, clear the old
  // local identity, then arm the normal-input gate. No E07 action is submitted.
  CMD_YOUFENG_PHASE_GATE = 43,
  // On the next matching normal cast rejection, call the native Space/QingGong
  // start path once, then allow the original cast only when that call succeeds.
  // modes 5..8 are bounded start/stop pulses (24/0/4/8ms respectively).
  // Physical Space input and binding remain untouched.
  CMD_YOUFENG_QINGGONG_GATE = 44,
  // Generic, identity-checked action experiment for long server-driven skills.
  // mode: 1=QingGong edge, 2=forced edge, 3=long edge,
  // 4=exact CancelSession(cast+0x6C), 5=arm one matching current-action gate,
  // 6=exact KuangFeng pre-perform pulse, 7=post-second-perform pulse,
  // 8=arm 10 exact native post-perform pulses, 9=stop/status,
  // pulse modes use 0ms QingGong start/stop; 10=read-only snapshot;
  // 11=ultimate tail observer, 12=stop, 13=observer + exact local CD clear
  // plus persistent exact next-action gate for repeated ultimate requests.
  CMD_SKILL_ACTION_EXPERIMENT = 45,
  // Read-only damage accumulator for one exact object id.
  // id_lo/id_hi = target id; mode: 1=start/reset, 2=status, 0=stop.
  CMD_DUMMY_DAMAGE_STAT = 46,
  // Passive passive CG/cinematic skip on PlayCG/PlayBlackEdge.
  // mode: 1=arm/install, 2=status, 0=disarm (hooks stay but skip off).
  CMD_CG_SKIP = 47,
  // Execute the TeamFollow dialog's actual on/off callbacks on the UI thread.
  // id_lo: 1=enable, 0=disable.
  CMD_TEAM_FOLLOW = 48,
  // Bypass the client SessionSend send-rate gate (0xCC6CC0 head<cap reject).
  // mode: 1=arm (patch 0xCC6D51 jae->jmp), 2=status, 0=disarm (restore bytes).
  CMD_SESSION_SEND_BYPASS = 49,
  // Read-only monotonic combat activity counters for dungeon 纯站街 detection.
  // mode: 1=arm/install (resets counters), 2=status (refreshes host target),
  // 0=disarm (hooks stay but counters stop). ret=attack_seq, tid=self_damage_seq,
  // err string carries full state: a/d/total/t(host target)/r(resolved).
  CMD_COMBAT_MONITOR = 50,
  // Dump UTF-16 caption text found under a UI dialog (UI-thread direct read,
  // bypasses the scene-settle CRT gate). id_lo selects the dialog:
  // 0 = Win_InstanceConfig, 1 = Win_TrackFrame, 2 = both.
  // err returns the concatenated caption lines found (best-effort).
  CMD_DUMP_UI_TEXT = 51,
  // Native selected-target submit trace / narrow dungeon guard (0x7493B0).
  // mode: 1=install/reset trace-only, 2=status/latest sample,
  // 3=install/reset+enable the caller/TID/distance guard,
  // 4=disable guard while leaving trace state unchanged, 0=stop+dump TSV.
  CMD_TARGET_SUBMIT_TRACE = 52,
  // Configure the native dungeon TID deny set without rebuilding the DLL.
  // mode=1 clear, mode=2 add id_lo, mode=3 status.
  CMD_DUNGEON_TARGET_RULES = 53,
  CMD_JIANGLONG_RUNTIME_RESOLVE = 54,
  // UI-thread right-panel specified follow: SetTarget(id64) + Btn_Follow.
  CMD_QUICK_TEAM_FOLLOW = 55,
  // Read-only live NPC/Matter lookup for the 武尊堂 challenge card.
  CMD_OBJECT_SCAN = 56,
  // UI 线程驱动攻击组件 tick（副本 Alert 粘滞自愈）。
  CMD_AUTOPLAY_DRIVE_ATTACK = 57,
};

enum BridgeStatus { ST_IDLE = 0, ST_PENDING = 1, ST_OK = 2, ST_ERR = 3 };

enum BridgeCapabilityBits {
  CAP_TARGET = 1u << 0,
  CAP_PICKUP = 1u << 1,
  CAP_MOVE = 1u << 2,
  CAP_INTERACT = 1u << 3,
  CAP_CAPTURE = 1u << 4,
  CAP_UI_INPUT = 1u << 5,
  CAP_TASK = 1u << 6,
  CAP_INSTANCE = 1u << 7,
  CAP_KEY_HOOK = 1u << 8,
  CAP_SESSION = 1u << 9,
  CAP_LINE = 1u << 10,
  CAP_NPC_TALK = 1u << 11,
  CAP_AUTOPLAY = 1u << 12,
};

static const uint32_t kBridgeCapabilities =
    CAP_TARGET | CAP_PICKUP | CAP_MOVE | CAP_INTERACT | CAP_CAPTURE |
    CAP_UI_INPUT | CAP_TASK | CAP_INSTANCE | CAP_KEY_HOOK | CAP_SESSION |
    CAP_LINE | CAP_NPC_TALK | CAP_AUTOPLAY;

#pragma pack(push, 1)
struct BridgeShared {
  volatile uint32_t magic;
  volatile uint32_t seq;
  volatile uint32_t cmd;
  volatile int32_t status;
  volatile int32_t ret;
  volatile uint32_t module_base;
  volatile uint32_t hwnd;
  volatile float x;
  volatile float y;
  volatile float z;
  volatile int32_t mode;
  volatile uint32_t id_lo;
  volatile uint32_t id_hi;
  volatile int32_t tid;
  char err[128];
  volatile uint32_t protocol_version;
  volatile uint32_t struct_size;
  volatile uint32_t capabilities;
  volatile uint32_t ack_seq;
};
#pragma pack(pop)

static_assert(sizeof(BridgeShared) == 200, "BridgeShared protocol size mismatch");
