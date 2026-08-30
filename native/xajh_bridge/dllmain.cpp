// xajh_bridge.dll — inject into xajh.exe (x86) and run game calls on the UI thread.
// Protocol: shared memory Local\XajhBridge_<pid> + PostMessage(WM_APP+0x51).
// @author by ak
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <psapi.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <float.h>
#include <wchar.h>
#include <intrin.h>
#include "bridge_protocol.h"

#pragma comment(lib, "user32.lib")
#pragma comment(lib, "kernel32.lib")
#pragma comment(lib, "psapi.lib")

#define BRIDGE_WM (WM_APP + 0x51)

static BridgeShared* g_shm = nullptr;
static bool g_shm_v2 = false;
static HANDLE g_map = nullptr;
static HWND g_hwnd = nullptr;
static HMODULE g_self = nullptr;
static HMODULE g_game = nullptr;
static volatile LONG g_ready = 0;
static volatile LONG g_attach_done = 0;
// Recurring UI-thread timer: poll shm pending cmds (NO SetWindowLongPtr).
static const UINT_PTR kDispatchTimerId = 0x58424A48u; /* 'XBJH' */
static const UINT kDispatchPeriodMs = 30;
static volatile LONG g_timer_armed = 0;
// Some game calls may pump messages. Prevent the recurring timer from
// re-entering RunCommand and executing the same PENDING slot twice.
static volatile LONG g_command_running = 0;
// Attach policy written by helper into shm.mode BEFORE inject (business cmds
// overwrite mode later). Standard keeps ideal late-enum fallback; green_compat
// never EnumWindows inside foreign green clients.
static const int32_t kAttachModeStandard = 0;
static const int32_t kAttachModeGreenSafe = 0x4743; /* 'GC' */
static volatile LONG g_green_safe = 0;

static DWORD WINAPI AttachThreadProc(LPVOID p);
static DWORD WINAPI ReadyWatchdogProc(LPVOID p);
static VOID CALLBACK DispatchTimerProc(HWND hwnd, UINT msg, UINT_PTR id,
                                       DWORD time);
static HWND FindGameHwnd();
static void CloseShared();
static bool OpenShared();
// Read Python-supplied hwnd from shm (must belong to this process).
static HWND PreferredHwndFromShm();
// Arm recurring SetTimer on hwnd (UI thread runs RunCommand on pending).
static bool ArmDispatchTimer(HWND hwnd);
static void KillDispatchTimer();
static void MarkReady(const char* note);

// Prefer notes absolute VA when preferred base is 0x400000.
static const uint32_t kDefaultBase = 0x400000u;
static const uint16_t kKnownMachine = IMAGE_FILE_MACHINE_I386;
static const uint32_t kKnownTimestamp = 0x56736608u;
static const uint32_t kKnownImageSize = 0x038A5000u;
// Absolute preferred VA of ChoiceObject thiscall (SEH); live = base+(va-0x400000).
// Real function is at 0x88F2A0 (RVA 0x48F2A0). Old 0xC8F2A0 was wrong and SEH'd.
static const uint32_t kNoteChoiceObject = 0x0088F2A0u;
static const uint32_t kNotePickupMatter = 0x007267F0u;
// Lua AutoClickMatter binder (SEH+Lua): do NOT call as cdecl(id,tid).
// Native matter interact used by AutoClickMatter path (disasm-confirmed):
//   this = *([call 0x4AE400] + 0x1A84)
//   bool __thiscall (this, id_lo, id_hi, tid) @ 0x7496B0
//   success path starts cast/session via 0x752D50 (type 0xB).
static const uint32_t kNoteGetHostSide = 0x004AE400u;
static const uint32_t kNoteMatterInteract = 0x007496B0u;
// Exact selected-target submission. This writes host+0x19E8/+0x19EC after
// notifying the target-change path.
static const uint32_t kNoteTargetSubmit = 0x007493B0u;
// Return address immediately after the type-1 object submission in 0x754280.
// Live A/B: manual click uses 0x751EDB; the dungeon auto re-lock of 北疆疯丐
// uses this path. Keep the guard limited to this one proven automatic edge.
static const uint32_t kNoteTargetSubmitDungeonAutoCaller = 0x007542AEu;
// Internal queued-target promotion. Unlike 0x7493B0 this path does not submit
// an id through the selected-target API: it copies controller+0x3C8/+0x3CC
// straight into host+0x19E8/+0x19EC. This is the confirmed bypass behind the
// later-stage distant target re-lock.
static const uint32_t kNoteTargetQueuePromote = 0x0073E300u;
// Target-state record apply. It copies record+0x31/+0x35 into both queued
// (+0x3C8/+0x3CC) and current (+0x19E8/+0x19EC) target fields, bypassing the
// submit and queued-promotion paths.
static const uint32_t kNoteTargetStateApply = 0x00730430u;
// Queue refresh immediately before 0x73E300 consumes controller+0x3C8.
static const uint32_t kNoteTargetQueueRefresh = 0x007E2780u;
static const uint32_t kNoteTargetQueueRefreshCaller = 0x0073E321u;
// AUI GetDlgItem thiscall (ecx=dlg, stack=[name]); live = base+(va-0x400000).
static const uint32_t kNoteAuiGetDlgItem = 0x00E918F0u;
// AUI label/list caption: live Txt_* often stores wchar* near +0xB8.. (RE in task_api).
static const uint32_t kAuiCaptionOffs[] = {0xB8u, 0xBCu, 0xC0u, 0xB0u, 0xA8u,
                                           0xC4u, 0xD0u, 0xD4u};// CaptureScreen worker: builds Screenshots path + saves frame (disasm 0x82BBF0).
static const uint32_t kNoteCaptureScreen = 0x0082BBF0u;
// CDlgActivityQuestion Btn_Ok command handler (reads Edt_Input, packet 0xCC99E0).
// thiscall: void __thiscall(CDlgActivityQuestion* this, int unused); ret 4.
static const uint32_t kNoteAqSubmit = 0x008CD2F0u;
// Task accept (jieTask body); this = *[*global + 0x2C].
static const uint32_t kNoteTaskAccept = 0x00CF0550u;
// Task complete family (needs NPC).
static const uint32_t kNoteTaskComplete = 0x00AB9B40u;
static const uint32_t kHostSideThisOff = 0x1A84u;
static const uint32_t kHostDeadStateOff = 0x1B8u;
static const uint32_t kTaskMgrThisOff = 0x2Cu;
// Preferred absolute global for game root (rebased like note VAs).
static const uint32_t kNoteTaskGameRootGlobal = 0x015282D8u;
// Instance enter packet builder: thiscall 0xCC6F80(this, host_lo, host_hi, inst, mode, flag).
// Call sites (Btn_Enter / list):
//   host ids  = GetHostSide()@0x4AE400 +0x140/+0x144
//   enter this = GetNetRoot()@0x4AE440 (*global+0x2C) +0x1C8  (NOT host-side)
// Packet opcode word 0x58 (also buildable via cdecl 0xCC81B0).
static const uint32_t kNoteInstanceEnter = 0x00CC6F80u;
static const uint32_t kNoteInstanceEnterPkt = 0x00CC81B0u;
static const uint32_t kNoteGetNetRoot = 0x004AE440u;
// Team invite c2s 0x1261: thiscall 0xCEC4E0(host_from_4AE440, id_lo, id_hi).
static const uint32_t kNoteTeamInvite = 0x00CEC4E0u;
// Actual Win_TeamFollow button callbacks. They each accept one ignored stack
// argument and dispatch the native c2s 0x1286 builder.
static const uint32_t kNoteTeamFollowEnable = 0x009A0680u;
static const uint32_t kNoteTeamFollowDisable = 0x009A06A0u;
// Right-panel specified follow: SetTarget(id64) then Btn_Follow.
static const uint32_t kNoteQuickTeamFollow = 0x009A0120u;
// Zeroed c2s 0x1261 send (thiscall host, pkt*, flag) — prefer over CEC4E0
static const uint32_t kNotePktSend = 0x00CD04D0u;
static const uint32_t kTeamInviteVt = 0x012C62F0u;
static const uint32_t kTeamInviteType = 0x1261u;
// CDlgNPC list-select handler (event 0x80000011) — UI thread only.
static const uint32_t kNoteNpcSelect = 0x00AA3330u;
static const uint32_t kNoteNpcHostSelect = 0x00AA5590u;  // cdecl AA5590(index, unused)
static const uint32_t kNoteHostGlobal = 0x019561F0u;      // Win_NPCTemplate host table
static const uint32_t kNoteListManGlobal = 0x019561F4u;
static const uint32_t kNoteStrBack = 0x012993D4u;
static const uint32_t kDlgOptTypeOff = 0x168u;
static const uint32_t kListSelectedIdOff = 0x12Cu;
static const uint32_t kTeamInviteHostIdLoOff = 0x240u;
static const uint32_t kTeamInviteHostIdHiOff = 0x244u;
// Lua CancelSession (0x69EF50) checks host+0x41C, then calls this method on
// GetNetRoot()+0x1C8. It queues opcode 0x21 and waits for normal server ack.
static const uint32_t kNoteCancelSession = 0x00CC7010u;
// Native action phase packets: opcode 0x69/0x6A plus a u16 config id.
static const uint32_t kNoteActionPhaseDown = 0x00CC53E0u;
static const uint32_t kNoteActionPhaseUp = 0x00CC5470u;
// Change scene line: cdecl 0xCCBC70(line_id_u8) -> packet opcode 0x7A + u8 id.
static const uint32_t kNoteChangeLine = 0x00CCBC70u;
static const uint32_t kHostSessionStateOff = 0x41Cu;
static const uint32_t kHostPerformMgrOff = 0x270u;
static const uint32_t kCancelSessionThisOff = 0x1C8u;
static const uint32_t kActionPhaseThisOff = 0x1C8u;
// OnSkillStopped (CECHostSkillHdl::OnSkillStopped thiscall ret 0x14).
// VA 0x75F750, ecx = cast-this, 5 stack args.
// Thin wrapper 0x761960: push 1; push 0..0; push arg0; call 0x75F750; ret 4. Lab mode can force cont=0 and/or call 0x754BA0.
static const uint32_t kNoteOnSkillStopped = 0x0075F750u;
static const uint32_t kSkillCastThisOff = 0x1A88u;
// ChoiceObject is thiscall on scene root from 0x4AE470 (not cdecl).
static const uint32_t kNoteGetChoiceThis = 0x004AE470u;
static const uint32_t kInstanceEnterThisOff = 0x1C8u;
static const uint32_t kHostPlayerIdLoOff = 0x140u;
static const uint32_t kHostPlayerIdHiOff = 0x144u;
// Cast skill (notes 使用技能.md): this = *[call 0x4AEFA0 + 0x1A88]; thiscall 0x755F10.
// Live cast path (hand_probe / exact profile): NOT stale 0x755F10 mid-fn.
// GetPkgSide@0x4AE420 -> [side+0x24]=skill_mgr; SkillById@0x533250(thiscall);
// CastOuter@0x53E110 cdecl(skill*, -1, 0, 0) -> 0x75F000.
static const uint32_t kNoteGetPkgSide = 0x004AE420u;
static const uint32_t kNoteSkillById = 0x00533250u;
static const uint32_t kNoteCastOuter = 0x0053E110u;
// Exact ultimate cooldown path:
// GetPkgSide()+0x3C -> cooldown manager; metadata(config)+0x8C -> key;
// ResolveCooldownRecord(manager, config, &key) -> {remaining,total}.
static const uint32_t kNoteSkillMetadataByConfig = 0x0056FAE0u;
static const uint32_t kNoteResolveCooldownRecord = 0x005378A0u;
static const uint32_t kPkgCooldownManagerOff = 0x3Cu;
static const uint32_t kNoteSetCurActiveSkill = 0x00758DF0u;
static const uint32_t kNoteSkillActionRequest = 0x00762F10u;
// Final client-to-server skill-session submit wrapper, thiscall + 12 dwords.
static const uint32_t kNoteHostStartSession = 0x007550B0u;
// Real sender called by HostStartSession. SessionSend gate here:
//   0xCC6D4A mov ecx,[esi]     ; head/used
//   0xCC6D4C mov edx,[esi+4]   ; cap/limit
//   0xCC6D4F cmp ecx,edx
//   0xCC6D51 jae +0x40         ; (73 40) -> allow; else head<cap -> return 2
// Patch byte 0xCC6D51 73->EB to always allow (SessionSend bypass).
static const uint32_t kNoteSessionSendGate = 0x00CC6D51u;
// GNET::ARCFourSecurity::Update(this, Octets*). At entry an outbound Octets
// still contains plaintext; the function applies RC4 in place.
static const uint32_t kNoteARCFourUpdate = 0x00DAFAF0u;
static const uint32_t kNoteOnPerformSkill = 0x00761330u;
static const uint32_t kNoteCastCore = 0x0075F000u;
// Per-target split-damage settlement. The return value is true only when one
// previously unplayed damage slice was applied by this invocation.
static const uint32_t kNoteSplitDamageProcess = 0x0079BA80u;
// CECAutoPlayTargetIgnorePolicy lives on the attack sub-system object that is
// created once the in-game hang enters the attack state. Located by scanning
// autoplay's pointer fields for this vtable; current target id64 at +0x38/+0x3C.
static const uint32_t kNoteIgnorePolicyVtable = 0x012BEC9Cu;
static const uint32_t kIgnorePolicyTargetLoOff = 0x38u;
static const uint32_t kIgnorePolicyTargetHiOff = 0x3Cu;
// CGApi passive skip (RE 2026-07-30):
//   PlayCG thiscall(self, cg_id) @ 0x846EF0
//   StopCG thiscall(self) @ 0x8464A0
//   PlayBlackEdge thiscall(self, f32, f32, i32) ret 0xC @ 0x8449B0
//   StopBlackEdge thiscall(self, param) ret 4 @ 0x844A00
//   ShowGameUI thiscall(self, show) ret 4 @ 0x843E50
//   CG manager = *[call 0x4AE3B0 + 0x218]
static const uint32_t kNotePlayCG = 0x00846EF0u;
static const uint32_t kNoteStopCG = 0x008464A0u;
static const uint32_t kNotePlayBlackEdge = 0x008449B0u;
static const uint32_t kNoteStopBlackEdge = 0x00844A00u;
static const uint32_t kNoteShowGameUI = 0x00843E50u;
static const uint32_t kNoteGetCgApi = 0x004AE3B0u;  // returns side*; CG mgr at +0x218
static const uint32_t kCgApiMgrOff = 0x218u;
// Final CanCast rejection used when the perform manager still exposes the
// previous current action. Returning false lets 0x75F000 continue normally.
static const uint32_t kNoteCurrentActionGate = 0x007546F0u;
// Native Space path: CECHostOPHdl::CmdStartCurrentQingGong(this, true).
// It resolves the current direction/type, runs the normal QingGong condition
// check, and submits opcode 0x90 without synthesizing keyboard input.
static const uint32_t kNoteCmdStartCurrentQingGong = 0x0074FBC0u;
// Lower native path used after the real Space handler has resolved type=1.
// bool __thiscall CECHostOPHdl::CmdStartQingGong(this, type, start).
static const uint32_t kNoteCmdStartQingGong = 0x0074F480u;
// Stop the type held in CECHostOPHdl+0x270 through opcode 0x91, then clear it.
static const uint32_t kNoteCmdStopCurrentQingGong = 0x0074F890u;
// Full action wrapper used by the real X binding. It owns the post-core
// request/dispatch path that a bare 0x75F000 call does not execute.
static const uint32_t kNoteActionCast = 0x00764820u;
static const uint32_t kNoteActionContextGlobal = 0x014BC958u;
static const uint32_t kNoteActionCanCastReturn = 0x00764F7Fu;
static const uint32_t kNoteOnPerformSetActiveReturn = 0x0076159Au;
static const uint32_t kNoteNewSessionSetActiveReturn = 0x007639F0u;
// Legacy notes (disabled path kept for xref only).
static const uint32_t kNoteGetSkillSide = 0x004AEFA0u;
static const uint32_t kNoteCastSkill = 0x00755F10u;  // STALE mid-function
static const uint32_t kSkillThisOff = 0x1A88u;
// Skill bar slot -> skill id: call 0x4AEFB0; [eax+0xC]; [eax+0xC]; push 0, slot; call 0x53F820; [eax+0x18].
static const uint32_t kNoteGetSkillBarRoot = 0x004AEFB0u;
static const uint32_t kNoteSkillBarLookup = 0x0053F820u;
// user32 IAT RVAs in xajh.exe (ImageBase 0x400000) — PE import parse 2026-07-17
static const uint32_t kIatRvaGetAsyncKeyState = 0x00E28BE0u;
static const uint32_t kIatRvaGetKeyState = 0x00E28B50u;
// Input object helpers (KEY_TRACE + disasm 2026-07-17):
//   IsKeyTable thiscall 0x4BEE00(this, vk) -> byte [this+0x2C+vk]
//   UpdateKeys 0x4BEFF0 writes [this+0x134] mod bits from GAKS (bit0=Shift)
//   GetModMask 0x4BF0B0 returns live Shift/Ctrl/Alt bits
//   InjectKey  0x4BF9C0 thiscall(input, msg, wParam=vk, lParam, lParamHi)
//             msg 0x100=WM_KEYDOWN / 0x101=WM_KEYUP (jump table sets table)
//   input this: *(*(game_root)+0x24)+0x78  where game_root global 0x15282D8
//   (same mid ptr as 0x4AE3B0; host side is mid+0x8C via 0x4AE400)
static const uint32_t kNoteIsKeyTable = 0x004BEE00u;
static const uint32_t kNoteUpdateKeys = 0x004BEFF0u;
static const uint32_t kNoteGetModMask = 0x004BF0B0u;
static const uint32_t kNoteInjectKey = 0x004BF9C0u;
static const uint32_t kNoteCheckModBind = 0x004C13D0u;
static const uint32_t kNoteGameRootGlobal = 0x015282D8u;
static const uint32_t kGameRootMidOff = 0x24u;
static const uint32_t kMidInputThisOff = 0x78u;
// CECAutoPlay = *(*game_root + 0x24 + 0x220). StopAutoPlay touches UI/game
// state, so it is invoked only by DispatchTimerProc on the game UI thread.
static const uint32_t kMidAutoplayThisOff = 0x220u;
static const uint32_t kNoteStopAutoplay = 0x00C5B0C0u;
// CDlgAutoPlayFrame::Btn_Start writes the active settings sub-page, calls the
// CECAutoPlay gate at 0xC55D50, then follows the normal packet/UI path. These
// three patch sites bypass only its empty-skill and dungeon-leader rejects.
// The earlier global feature-open rejection at 0xC55D63 remains untouched.
static const uint32_t kNoteAutoPlayFrameBtnStart = 0x00AFE5C0u;
static const uint32_t kNoteAutoPlayFrameVtable = 0x012A5144u;
static const uint32_t kNoteAutoPlaySkillJe1Disp = 0x00C55D88u;
static const uint32_t kNoteAutoPlaySkillJe2Disp = 0x00C55D8Eu;
static const uint32_t kNoteAutoPlayLeaderJne = 0x00C55DA2u;
// Dungeon autoplay is a follow-leader state machine. A captain bypass that
// starts with self as the cached target is valid/running but intentionally idle.
// Seed a real member into its target snapshot, refresh through the native
// snapshot method, then enter the native StateFollowTarget state.
static const uint32_t kNoteAutoPlayTargetSnapshotVtable = 0x012C23A0u;
static const uint32_t kNoteAutoPlayRefreshTarget = 0x00C6A2D0u;
static const uint32_t kNoteAutoPlaySetState = 0x00C60DD0u;
static const uint32_t kNoteAutoPlayAttackDrive = 0x00C53BC0u;
static const uint32_t kNoteAutoPlayFollowStateVtable = 0x012BEEFCu;
static const uint32_t kNoteAutoPlayEnterFollow = 0x00C603E0u;
static const uint32_t kHostTeamOff = 0x18CCu;
static const uint32_t kTeamLeaderIdOff = 0x10u;
static const uint32_t kTeamMemberPtrsOff = 0x34u;
static const uint32_t kTeamMemberCountOff = 0x38u;
static const uint32_t kTeamMemberIdOff = 0x18u;
static const uint32_t kInputKeyTableOff = 0x2Cu;
static const uint32_t kInputModMaskOff = 0x134u;
// [root+0x4D4] is the focus/key-poll gate. Game focus handler (0x48932C) overwrites
// it from window focus state each message — force-on alone races and loses.
static const uint32_t kGameRootKeyGateOff = 0x4D4u;
// Shadow copy written with gate; used only for diagnostics.
static const uint32_t kNoteKeyGateShadow = 0x014B4BECu;

// Forced key mask: bit set => report key as down to GetAsyncKeyState/GetKeyState.
// Bit 0 = Shift group (VK_SHIFT / L / R). Legacy KEY_FORCE path.
// g_force_vk[] = process-local force bits for CMD_KEY_HOLD (solution-2, no SoftSend).
static volatile LONG g_force_shift = 0;
static volatile uint8_t g_force_vk[256] = {0};
static volatile LONG g_force_any = 0;
static volatile LONG g_hold_allow_softsend = 0; /* session opt-in SoftSend for HOLD */
static volatile LONG g_key_hooks_installed = 0;
static volatile LONG g_inline_hooks_installed = 0;
static void* g_input_this = nullptr;
typedef SHORT(WINAPI* FnGetAsyncKeyState)(int vKey);
typedef SHORT(WINAPI* FnGetKeyState)(int nVirtKey);
static FnGetAsyncKeyState g_real_GetAsyncKeyState = nullptr;
static FnGetKeyState g_real_GetKeyState = nullptr;

// Track patched IAT slots so unload can restore them.
static const int kMaxIatPatches = 64;
struct IatPatch {
  uint32_t* slot;
  uint32_t orig;
};
static IatPatch g_iat_patches[kMaxIatPatches];
static int g_iat_patch_count = 0;

// ---- KEY_TRACE: record unique callers while user holds real Shift ----
// ---- KEY_DIAG: lightweight WH_GETMESSAGE hook for Shift entry-point probe ----
// Hot path: only touch Shift-group (or filtered) vk; dedup by ret_addr+vk+api.
static volatile LONG g_key_trace_on = 0;
static volatile LONG g_key_trace_filter_vk = 0; /* 0 = Shift group */
static char g_key_trace_path[MAX_PATH] = {0};
static CRITICAL_SECTION g_key_trace_cs;
static volatile LONG g_key_trace_cs_ready = 0;

static const int kMaxTraceEntries = 160;
static const int kTraceFrames = 10;
struct KeyTraceEntry {
  uint32_t ret_addr;
  uint32_t frames[kTraceFrames];
  uint32_t count;
  uint16_t vk;
  uint16_t api; /* 0=GetAsyncKeyState 1=GetKeyState */
  SHORT sample_real;
  uint8_t nframes;
  uint8_t saw_down;
};
static KeyTraceEntry g_key_trace[kMaxTraceEntries];
static int g_key_trace_count = 0;
static volatile LONG g_key_trace_hits = 0;
static volatile LONG g_key_trace_dropped = 0;

// Forward declaration: SetErr writes to g_shm->err.
static void SetErr(const char* msg);

// ---- KEY_DIAG: WH_GETMESSAGE hook for Shift WM_KEYDOWN probe ----
static volatile LONG g_diag_on = 0;
static volatile LONG g_diag_msgs = 0;       // total GetMessage/PeekMessage returns
static volatile LONG g_diag_shift = 0;      // WM_KEYDOWN for VK_SHIFT/LSHIFT
static volatile LONG g_diag_syskey = 0;     // WM_SYSKEYDOWN for Shift
static HHOOK g_diag_hook = nullptr;

static LRESULT CALLBACK DiagGetMsgProc(int code, WPARAM wParam, LPARAM lParam) {
  if (code == HC_ACTION && InterlockedCompareExchange(&g_diag_on, 0, 0)) {
    MSG* pMsg = (MSG*)lParam;
    if (pMsg) {
      InterlockedIncrement(&g_diag_msgs);
      if (pMsg->message == 0x100 /* WM_KEYDOWN */ ||
          pMsg->message == 0x104 /* WM_SYSKEYDOWN */) {
        UINT vk = (UINT)(pMsg->wParam & 0xFFu);
        if (vk == 0x10 || vk == 0xA0 || vk == 0xA1) {
          InterlockedIncrement(&g_diag_shift);
          if (pMsg->message == 0x104) InterlockedIncrement(&g_diag_syskey);
        }
      }
    }
  }
  return CallNextHookEx(g_diag_hook, code, wParam, lParam);
}

static bool InstallMsgDiagHook() {
  if (InterlockedCompareExchange(&g_diag_on, 0, 0)) return true; // already on
  if (!g_hwnd || !IsWindow(g_hwnd)) {
    if (g_shm) SetErr("KEY_DIAG no hwnd");
    return false;
  }
  DWORD tid = GetWindowThreadProcessId(g_hwnd, nullptr);
  if (!tid || tid == GetCurrentThreadId()) {
    // If our timer runs on the game thread we can still hook, but prefer a
    // distinct target.  tid==0 signals current thread which is fine in-process.
  }
  // In-process thread-specific hook: hMod=NULL because target thread is ours.
  g_diag_hook = SetWindowsHookExW(WH_GETMESSAGE, DiagGetMsgProc, nullptr, tid);
  if (!g_diag_hook) {
    if (g_shm) {
      char buf[80];
      _snprintf_s(buf, _TRUNCATE, "KEY_DIAG SetWindowsHookEx fail gle=%lu",
                  (unsigned long)GetLastError());
      SetErr(buf);
    }
    return false;
  }
  // Reset counters
  InterlockedExchange(&g_diag_msgs, 0);
  InterlockedExchange(&g_diag_shift, 0);
  InterlockedExchange(&g_diag_syskey, 0);
  InterlockedExchange(&g_diag_on, 1);
  return true;
}

static void RemoveMsgDiagHook() {
  InterlockedExchange(&g_diag_on, 0);
  if (g_diag_hook) {
    UnhookWindowsHookEx(g_diag_hook);
    g_diag_hook = nullptr;
  }
}

static void EnsureKeyTraceCs() {
  if (InterlockedCompareExchange(&g_key_trace_cs_ready, 1, 0) == 0) {
    InitializeCriticalSection(&g_key_trace_cs);
  }
}

static bool IsTraceVk(int vKey) {
  LONG filt = InterlockedCompareExchange(&g_key_trace_filter_vk, 0, 0);
  if (filt == 0) {
    return vKey == 0x10 || vKey == 0xA0 || vKey == 0xA1;
  }
  return vKey == (int)filt;
}

static void FormatAddrMod(char* out, size_t out_n, uint32_t addr) {
  if (!out || !out_n) return;
  out[0] = 0;
  HMODULE mod = nullptr;
  if (!GetModuleHandleExA(
          GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
              GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
          (LPCSTR)(uintptr_t)addr, &mod) ||
      !mod) {
    _snprintf_s(out, out_n, _TRUNCATE, "0x%08X", addr);
    return;
  }
  char path[MAX_PATH] = {0};
  GetModuleFileNameA(mod, path, MAX_PATH);
  const char* base = path;
  for (const char* p = path; *p; ++p) {
    if (*p == '\\' || *p == '/') base = p + 1;
  }
  uint32_t mbase = (uint32_t)(uintptr_t)mod;
  _snprintf_s(out, out_n, _TRUNCATE, "0x%08X %s+0x%X", addr, base,
              addr - mbase);
}


// ---- Game crash capture (unhandled exception -> <software>/logs/xajh_helper_error_YYYYMMDD.log) ----
// Portable only: resolve logs next to the staged bridge DLL (…/runtime/native/bin -> app root/logs).
// Never write %TEMP% / %LOCALAPPDATA%.
static volatile LONG g_crash_dump_written = 0;
static LPTOP_LEVEL_EXCEPTION_FILTER g_prev_uef = nullptr;

struct GameCrashSnapshot {
  EXCEPTION_RECORD record;
  CONTEXT context;
  DWORD pid;
  DWORD tid;
};
static GameCrashSnapshot g_crash_snapshot = {};

static const char* CrashCodeName(DWORD code) {
  switch (code) {
    case 0xC0000005: return "ACCESS_VIOLATION";
    case 0xC0000006: return "IN_PAGE_ERROR";
    case 0xC000001D: return "ILLEGAL_INSTRUCTION";
    case 0xC0000094: return "INTEGER_DIVIDE_BY_ZERO";
    case 0xC0000096: return "PRIVILEGED_INSTRUCTION";
    case 0xC00000FD: return "STACK_OVERFLOW";
    case 0xC0000409: return "STACK_BUFFER_OVERRUN";
    case 0xC000041D: return "FATAL_USER_CALLBACK_EXCEPTION";
    case 0x80000003: return "BREAKPOINT";
    default: return "EXCEPTION";
  }
}

// Fill out_dir with software logs path ending in backslash. Returns false on failure.
static bool ResolveSoftwareLogsDir(char* out_dir, size_t out_n) {
  if (!out_dir || out_n < 8) return false;
  out_dir[0] = 0;
  char mod[MAX_PATH] = {0};
  HMODULE self = g_self ? g_self : nullptr;
  if (!GetModuleFileNameA(self, mod, MAX_PATH) || !mod[0]) return false;

  // Strip filename -> directory of DLL (…/runtime/native/bin)
  char* slash = nullptr;
  for (char* p = mod; *p; ++p) {
    if (*p == '\\' || *p == '/') slash = p;
  }
  if (!slash) return false;
  *slash = 0;

  // Walk up: bin -> native -> runtime -> app_root (3 parents)
  for (int up = 0; up < 3; ++up) {
    slash = nullptr;
    for (char* p = mod; *p; ++p) {
      if (*p == '\\' || *p == '/') slash = p;
    }
    if (!slash) return false;
    *slash = 0;
  }
  if (!mod[0]) return false;
  _snprintf_s(out_dir, out_n, _TRUNCATE, "%s\\logs\\", mod);
  // Ensure directory exists (best-effort).
  CreateDirectoryA(out_dir, nullptr);
  // CreateDirectory needs path without trailing slash sometimes; try both.
  {
    char bare[MAX_PATH] = {0};
    _snprintf_s(bare, _TRUNCATE, "%s\\logs", mod);
    CreateDirectoryA(bare, nullptr);
  }
  return true;
}

static void WriteGameCrashDump(const GameCrashSnapshot* snapshot) {
  if (!snapshot) return;

  char dir[MAX_PATH] = {0};
  if (!ResolveSoftwareLogsDir(dir, sizeof(dir))) {
    // Fallback: next to the DLL itself (still on the software volume).
    char mod[MAX_PATH] = {0};
    if (GetModuleFileNameA(g_self, mod, MAX_PATH) && mod[0]) {
      char* slash = nullptr;
      for (char* p = mod; *p; ++p) {
        if (*p == '\\' || *p == '/') slash = p;
      }
      if (slash) {
        *slash = 0;
        _snprintf_s(dir, _TRUNCATE, "%s\\", mod);
      }
    }
  }
  if (!dir[0]) return;

  // Unified error log under software logs/ (same file helper uses).
  SYSTEMTIME st = {};
  GetLocalTime(&st);
  char path[MAX_PATH] = {0};
  DWORD pid = snapshot->pid;
  _snprintf_s(path, _TRUNCATE, "%sxajh_helper_error_%04u%02u%02u.log", dir,
              (unsigned)st.wYear, (unsigned)st.wMonth, (unsigned)st.wDay);

  FILE* fp = nullptr;
  if (fopen_s(&fp, path, "a") != 0 || !fp) return;

  const EXCEPTION_RECORD* er = &snapshot->record;
  DWORD code = er->ExceptionCode;
  uint32_t fault = (uint32_t)(uintptr_t)er->ExceptionAddress;
  char fault_mod[160] = {0};
  FormatAddrMod(fault_mod, sizeof(fault_mod), fault);

  fprintf(fp, "=== XAJH GAME_CRASH kind=游戏崩溃 ===\n");
  fprintf(fp, "pid=%u tid=%u\n", (unsigned)pid, (unsigned)snapshot->tid);
  fprintf(fp, "exception_code=0x%08X(%s)\n", (unsigned)code, CrashCodeName(code));
  fprintf(fp, "fault_addr=0x%08X\n", fault);
  fprintf(fp, "crash_point=%s\n", fault_mod);
  if (code == 0xC0000005 && er->NumberParameters >= 2) {
    const ULONG_PTR av_kind = er->ExceptionInformation[0];
    const char* av_op = av_kind == 0 ? "read"
                        : av_kind == 1 ? "write"
                        : av_kind == 8 ? "execute"
                                       : "unknown";
    fprintf(fp, "av_op=%s av_addr=0x%08X\n",
            av_op,
            (unsigned)er->ExceptionInformation[1]);
  }
  if (g_shm) {
    fprintf(fp,
            "shm cmd=%u status=%d ret=%d seq=%u ack=%u hwnd=0x%X base=0x%X err=%s\n",
            (unsigned)g_shm->cmd, (int)g_shm->status, (int)g_shm->ret,
            (unsigned)g_shm->seq, g_shm_v2 ? (unsigned)g_shm->ack_seq : 0u,
            (unsigned)g_shm->hwnd, (unsigned)g_shm->module_base, g_shm->err);
  } else {
    fprintf(fp, "shm=<null>\n");
  }
  fprintf(fp, "bridge_build=%u\n", (unsigned)BRIDGE_BUILD_ID);

  const CONTEXT& ctx = snapshot->context;
  fprintf(fp, "context eip=0x%08X esp=0x%08X ebp=0x%08X flags=0x%08X\n",
          (unsigned)ctx.Eip, (unsigned)ctx.Esp, (unsigned)ctx.Ebp,
          (unsigned)ctx.ContextFlags);
  uint32_t frame_ip = ctx.Eip;
  uint32_t frame_bp = ctx.Ebp;
  fprintf(fp, "stack_frames=original_context\n");
  for (unsigned i = 0; i < 24 && frame_ip; ++i) {
    char m[160] = {0};
    FormatAddrMod(m, sizeof(m), frame_ip);
    fprintf(fp, "  #%02u %s ebp=0x%08X\n", i, m, frame_bp);
    if (!frame_bp) break;
    uint32_t next_bp = 0;
    uint32_t return_ip = 0;
    bool frame_ok = false;
    __try {
      next_bp = *(uint32_t*)(uintptr_t)frame_bp;
      return_ip = *(uint32_t*)(uintptr_t)(frame_bp + 4u);
      frame_ok = true;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      frame_ok = false;
    }
    if (!frame_ok || !return_ip || next_bp <= frame_bp ||
        next_bp - frame_bp > 0x100000u)
      break;
    frame_bp = next_bp;
    frame_ip = return_ip;
  }
  fflush(fp);
  fclose(fp);

  // Best-effort: leave a short marker in shm.err if still mapped.
  if (g_shm) {
    char buf[128];
    _snprintf_s(buf, _TRUNCATE, "GAME_CRASH %s", fault_mod);
    SetErr(buf);
  }
}

static DWORD WINAPI GameCrashWriterThreadProc(LPVOID param) {
  __try {
    WriteGameCrashDump((const GameCrashSnapshot*)param);
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  return 0;
}

static LONG WINAPI BridgeUnhandledExceptionFilter(EXCEPTION_POINTERS* ep) {
  __try {
    if (ep && ep->ExceptionRecord && ep->ContextRecord &&
        InterlockedCompareExchange(&g_crash_dump_written, 1, 0) == 0) {
      // The faulting thread may have exhausted or corrupted its stack. Copy the
      // OS-owned exception state into static storage, then do all formatting and
      // file I/O on a fresh thread with a clean stack.
      g_crash_snapshot.record = *ep->ExceptionRecord;
      g_crash_snapshot.context = *ep->ContextRecord;
      g_crash_snapshot.pid = GetCurrentProcessId();
      g_crash_snapshot.tid = GetCurrentThreadId();
      HANDLE writer = CreateThread(nullptr, 0, GameCrashWriterThreadProc,
                                   &g_crash_snapshot, 0, nullptr);
      if (writer) {
        WaitForSingleObject(writer, 2000);
        CloseHandle(writer);
      }
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  if (g_prev_uef) {
    return g_prev_uef(ep);
  }
  return EXCEPTION_CONTINUE_SEARCH;
}

static void InstallGameCrashCapture() {
  // Only once per process; keep previous filter chain.
  static volatile LONG installed = 0;
  if (InterlockedCompareExchange(&installed, 1, 0) != 0) return;
  g_prev_uef = SetUnhandledExceptionFilter(BridgeUnhandledExceptionFilter);
}

static void KeyTraceRecord(int vKey, SHORT real, int api, void* ret_addr) {
  if (!InterlockedCompareExchange(&g_key_trace_on, 0, 0)) return;
  if (!IsTraceVk(vKey)) return;
  InterlockedIncrement(&g_key_trace_hits);

  EnsureKeyTraceCs();
  EnterCriticalSection(&g_key_trace_cs);
  __try {
    uint32_t ra = (uint32_t)(uintptr_t)ret_addr;
    int found = -1;
    for (int i = 0; i < g_key_trace_count; ++i) {
      if (g_key_trace[i].ret_addr == ra && g_key_trace[i].vk == (uint16_t)vKey &&
          g_key_trace[i].api == (uint16_t)api) {
        found = i;
        break;
      }
    }
    if (found >= 0) {
      g_key_trace[found].count++;
      g_key_trace[found].sample_real = real;
      if (real & 0x8000) g_key_trace[found].saw_down = 1;
    } else if (g_key_trace_count < kMaxTraceEntries) {
      KeyTraceEntry* e = &g_key_trace[g_key_trace_count++];
      e->ret_addr = ra;
      e->vk = (uint16_t)vKey;
      e->api = (uint16_t)api;
      e->count = 1;
      e->sample_real = real;
      e->saw_down = (real & 0x8000) ? 1 : 0;
      e->nframes = 0;
      void* frames[kTraceFrames] = {0};
      // skip this frame; CaptureStackBackTrace available via kernel32
      USHORT n = CaptureStackBackTrace(1, kTraceFrames, frames, nullptr);
      if (n > kTraceFrames) n = kTraceFrames;
      e->nframes = (uint8_t)n;
      for (USHORT i = 0; i < n; ++i) {
        e->frames[i] = (uint32_t)(uintptr_t)frames[i];
      }
    } else {
      InterlockedIncrement(&g_key_trace_dropped);
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    // ignore
  }
  LeaveCriticalSection(&g_key_trace_cs);
}

static void KeyTraceReset() {
  EnsureKeyTraceCs();
  EnterCriticalSection(&g_key_trace_cs);
  g_key_trace_count = 0;
  memset(g_key_trace, 0, sizeof(g_key_trace));
  InterlockedExchange(&g_key_trace_hits, 0);
  InterlockedExchange(&g_key_trace_dropped, 0);
  LeaveCriticalSection(&g_key_trace_cs);
}

static bool KeyTraceDumpFile(char* out_path, size_t out_n) {
  char dir[MAX_PATH] = {0};
  DWORD n = GetTempPathA(MAX_PATH, dir);
  if (!n || n >= MAX_PATH) {
    strcpy_s(dir, "C:\\Windows\\Temp\\");
  }
  DWORD pid = GetCurrentProcessId();
  _snprintf_s(out_path, out_n, _TRUNCATE, "%sxajh_key_trace_%u.log", dir,
              (unsigned)pid);

  FILE* fp = nullptr;
  if (fopen_s(&fp, out_path, "w") != 0 || !fp) return false;

  fprintf(fp,
          "=== KEY_TRACE dump pid=%u patches=%d force_shift=%ld hits=%ld "
          "unique=%d dropped=%ld ===\n",
          (unsigned)pid, g_iat_patch_count,
          InterlockedCompareExchange(&g_force_shift, 0, 0),
          InterlockedCompareExchange(&g_key_trace_hits, 0, 0), g_key_trace_count,
          InterlockedCompareExchange(&g_key_trace_dropped, 0, 0));
  fprintf(fp,
          "instruction: hold real Shift until reticle shows, then STOP trace.\n"
          "entries sorted by count desc; saw_down=1 means real key was down.\n\n");

  EnsureKeyTraceCs();
  EnterCriticalSection(&g_key_trace_cs);
  // simple selection sort by count desc
  for (int i = 0; i < g_key_trace_count; ++i) {
    for (int j = i + 1; j < g_key_trace_count; ++j) {
      if (g_key_trace[j].count > g_key_trace[i].count) {
        KeyTraceEntry tmp = g_key_trace[i];
        g_key_trace[i] = g_key_trace[j];
        g_key_trace[j] = tmp;
      }
    }
  }
  for (int i = 0; i < g_key_trace_count; ++i) {
    KeyTraceEntry* e = &g_key_trace[i];
    char ra_s[160];
    FormatAddrMod(ra_s, sizeof(ra_s), e->ret_addr);
    fprintf(fp,
            "[#%d] api=%s vk=0x%02X count=%u saw_down=%u sample_real=0x%04X\n"
            "  ret %s\n",
            i + 1, e->api ? "GetKeyState" : "GetAsyncKeyState", e->vk, e->count,
            (unsigned)e->saw_down, (unsigned)(uint16_t)e->sample_real, ra_s);
    for (int f = 0; f < (int)e->nframes; ++f) {
      char fs[160];
      FormatAddrMod(fs, sizeof(fs), e->frames[f]);
      fprintf(fp, "  #%d %s\n", f, fs);
    }
    fprintf(fp, "\n");
  }
  LeaveCriticalSection(&g_key_trace_cs);

  fprintf(fp, "=== end ===\n");
  fclose(fp);
  return true;
}


// ---- Solution-2: process-local force map (no system SoftSend by default) ----
// @author by ak
static bool IsForcedVk(int vKey) {
  unsigned u = (unsigned)vKey & 0xFFu;
  if (g_force_vk[u]) return true;
  if (InterlockedCompareExchange(&g_force_shift, 0, 0)) {
    if (u == 0x10u || u == 0xA0u || u == 0xA1u) return true;
  }
  return false;
}

static void RecountForceAny() {
  LONG any = InterlockedCompareExchange(&g_force_shift, 0, 0) ? 1 : 0;
  if (!any) {
    for (int i = 0; i < 256; ++i) {
      if (g_force_vk[i]) {
        any = 1;
        break;
      }
    }
  }
  InterlockedExchange(&g_force_any, any);
}

static int CountForcedVks() {
  int n = 0;
  for (int i = 1; i < 256; ++i) if (g_force_vk[i]) n++;
  return n;
}

static SHORT WINAPI Hook_GetAsyncKeyState(int vKey) {
  void* ra = _ReturnAddress();
  FnGetAsyncKeyState real = g_real_GetAsyncKeyState;
  SHORT r = real ? real(vKey) : (SHORT)0;
  KeyTraceRecord(vKey, r, 0, ra);
  if (IsForcedVk(vKey)) {
    return (SHORT)0x8001; /* down + toggled LSB */
  }
  return r;
}

static SHORT WINAPI Hook_GetKeyState(int nVirtKey) {
  void* ra = _ReturnAddress();
  FnGetKeyState real = g_real_GetKeyState;
  SHORT r = real ? real(nVirtKey) : (SHORT)0;
  KeyTraceRecord(nVirtKey, r, 1, ra);
  if (IsForcedVk(nVirtKey)) {
    return (SHORT)0x8001;
  }
  return r;
}

// ---- Inline hooks: IsKeyTable / GetModMask (reticle needs key table, not only GAKS) ----
// Original prolog sizes (capstone):
//   0x4BEE00: 4+4+3 = 11 bytes (mov eax,[esp+4]; mov al,[eax+ecx+2C]; ret 4)
//   0x4BF0B0: 5+1+2+7 = 15 bytes (mov eax,[glob]; push esi; xor esi,esi; cmp ...)
// Trampoline: stolen bytes + jmp back to orig+stolen.

#pragma pack(push, 1)
struct InlineHookRec {
  uint8_t* target;
  uint8_t* trampoline;
  uint8_t stolen[16];
  int stolen_n;
  uint8_t active;
};
#pragma pack(pop)

static InlineHookRec g_hk_iskey = {};
static InlineHookRec g_hk_modmask = {};
// Native inventory UseItem call trace. This is deliberately short-lived: the
// development panel arms it for one measurement window then removes it.
static InlineHookRec g_hk_item_use = {};
static InlineHookRec g_hk_dummy_damage = {};
static InlineHookRec g_hk_cg_play = {};
static InlineHookRec g_hk_cg_black_edge = {};
// Skill action trace is also short-lived. It observes the real game input path
// and never initiates a cast or edits cast/skill state.
static InlineHookRec g_hk_action_request_trace = {};
static InlineHookRec g_hk_action_cancast_trace = {};
static InlineHookRec g_hk_set_active_trace = {};
static InlineHookRec g_hk_on_perform_trace = {};
static InlineHookRec g_hk_host_start_session_trace = {};
static InlineHookRec g_hk_arcfour_update_trace = {};
static InlineHookRec g_hk_youfeng_next_gate = {};
static InlineHookRec g_hk_youfeng_action_cast = {};
static InlineHookRec g_hk_qinggong_start_trace = {};
static InlineHookRec g_hk_qinggong_stop_trace = {};
static InlineHookRec g_hk_target_submit_trace = {};
static InlineHookRec g_hk_target_queue_promote = {};
static InlineHookRec g_hk_target_state_apply = {};
static InlineHookRec g_hk_target_queue_refresh = {};
static volatile LONG g_item_trace_on = 0;
static volatile LONG g_item_trace_pack = -1;
static volatile LONG g_item_trace_slot = -1;
static volatile LONG g_item_trace_attempts = 0;
static volatile LONG g_item_trace_successes = 0;
static volatile LONG g_dummy_damage_on = 0;
static volatile LONG g_session_send_bypass_on = 0;
static volatile LONG g_session_send_bypass_hits = 0;
static volatile LONG g_cg_skip_on = 0;
static volatile LONG g_cg_skip_busy = 0;
static volatile LONG g_cg_skip_hits = 0;
static volatile LONG g_cg_skip_black_hits = 0;
static volatile LONG g_cg_skip_play_hits = 0;
static volatile LONG g_dummy_damage_target_lo = 0;
static volatile LONG g_dummy_damage_target_hi = 0;
static volatile LONG g_dummy_damage_started_tick = 0;
static volatile LONG g_dummy_damage_done = 0;
static volatile LONG g_dummy_damage_hits = 0;
static volatile LONG g_dummy_damage_last = 0;
__declspec(align(8)) static volatile LONG64 g_dummy_damage_total = 0;
static const DWORD kDummyDamageWindowMs = 60000u;

// Selected-target submission has a very small fan-in. Keep a bounded ring so
// the live status command can identify the active caller and stop can retain
// the immediately preceding sequence without adding a hot-path allocation.
static const int kMaxTargetSubmitTrace = 64;
struct TargetSubmitTraceEntry {
  uint32_t seq;
  uint32_t tick;
  uint32_t thread_id;
  uint32_t caller;
  uint32_t self_ptr;
  uint32_t target_lo;
  uint32_t target_hi;
  uint32_t route;
};
static TargetSubmitTraceEntry g_target_submit_trace[kMaxTargetSubmitTrace] = {};
static volatile LONG g_target_submit_trace_on = 0;
static volatile LONG g_target_submit_trace_count = 0;
static volatile LONG g_target_submit_guard_on = 0;
static volatile LONG g_target_submit_guard_hits = 0;
static volatile LONG g_target_queue_guard_hits = 0;
static volatile LONG g_target_state_guard_hits = 0;
static volatile LONG g_target_queue_refresh_guard_hits = 0;
static volatile LONG g_target_arm_sanitize_hits = 0;
// Configuration is supplied once by the assistant before dungeon autoplay
// starts. Candidate hooks only read this fixed in-process array.
static const uint32_t kMaxDungeonTargetTids = 64u;
static uint32_t g_dungeon_target_tids[kMaxDungeonTargetTids] = {};
static volatile LONG g_dungeon_target_tid_count = 0;

static void AppendTraceHex(char* out, uint32_t* pos, uint32_t cap,
                           uint32_t value) {
  static const char kHex[] = "0123456789ABCDEF";
  if (!out || !pos || *pos + 8u >= cap) return;
  for (int shift = 28; shift >= 0; shift -= 4)
    out[(*pos)++] = kHex[(value >> shift) & 0xFu];
}

// Debug-only visibility for every traced target write. This stays on the
// existing native trace switch and uses no game call, allocation, or file I/O.
static void EmitTargetTraceDebug(const TargetSubmitTraceEntry* e,
                                 bool skipped) {
  if (!e || !InterlockedCompareExchange(&g_target_submit_trace_on, 0, 0))
    return;
  char line[192] = "xajh_target route=";
  uint32_t p = 18;
  const char* route = e->route == 5 ? "arm" :
                      e->route == 4 ? "refresh" :
                      e->route == 3 ? "state" :
                      e->route == 2 ? "queue" : "submit";
  while (*route && p + 1u < sizeof(line)) line[p++] = *route++;
  const char* action = " action=select skip=";
  while (*action && p + 1u < sizeof(line)) line[p++] = *action++;
  line[p++] = skipped ? '1' : '0';
  const char* id = " id=";
  while (*id && p + 1u < sizeof(line)) line[p++] = *id++;
  AppendTraceHex(line, &p, sizeof(line), e->target_hi);
  if (p + 1u < sizeof(line)) line[p++] = ':';
  AppendTraceHex(line, &p, sizeof(line), e->target_lo);
  const char* caller = " caller=0x";
  while (*caller && p + 1u < sizeof(line)) line[p++] = *caller++;
  AppendTraceHex(line, &p, sizeof(line), e->caller);
  if (p + 2u < sizeof(line)) {
    line[p++] = '\r';
    line[p++] = '\n';
  }
  line[p] = 0;
  OutputDebugStringA(line);
}

// Dungeon 纯站街 combat monitor: read-only monotonic counters armed while the
// ordinary dungeon runner waits for the character to clear the instance. The
// attack seq is driven by the host's SkillActionRequest detour; self damage is
// counted only when the host's attack target (IgnorePolicy, alive while the
// in-game hang attacks) matches the damage splitter's current target.
static volatile LONG g_combat_monitor_on = 0;
static volatile LONG g_combat_resolved = 0;
static volatile LONG g_combat_attack_seq = 0;
static volatile LONG g_combat_self_damage_seq = 0;
__declspec(align(8)) static volatile LONG64 g_combat_self_damage_total = 0;
// Host attack target id64 read/written as one atomic aligned 64-bit value so
// the damage splitter never observes a torn (lo,hi) pair mid target switch.
__declspec(align(8)) static volatile LONG64 g_combat_host_target = 0;

static const int kMaxSkillActionTrace = 8192;
enum SkillActionTraceKind {
  SKTRACE_CORE = 2,
  SKTRACE_ACTIVE = 3,
  SKTRACE_REQUEST = 6,
  SKTRACE_PERFORM = 7,
  SKTRACE_RESTOP = 8,
  SKTRACE_NEXT_GATE = 9,
  SKTRACE_FRESH_GATE = 10,
  SKTRACE_SESSION_SEND = 11,
  SKTRACE_EXACT_CANCEL = 12,
  SKTRACE_PHASE_GATE = 13,
  SKTRACE_QINGGONG_GATE = 14,
  SKTRACE_QINGGONG_START = 15,
  SKTRACE_QINGGONG_STOP = 16,
};
struct SkillActionTraceEntry {
  uint32_t seq;
  uint32_t kind;
  uint32_t tick;
  uint32_t thread_id;
  uint32_t caller;
  uint32_t self_ptr;
  uint32_t skill_ptr;
  uint32_t arg1;
  uint32_t arg2;
  uint32_t arg3;
  int32_t ret;
  uint32_t skill_0;
  uint32_t skill_4;
  uint32_t skill_10;
  uint32_t skill_18;
  uint32_t skill_ea4;
  uint32_t cast_10_before;
  uint32_t cast_18_before;
  uint32_t cast_4a0_before;
  uint32_t cast_10_after;
  uint32_t cast_18_after;
  uint32_t cast_4a0_after;
  uint32_t session_args[12];
};
static SkillActionTraceEntry g_skill_action_trace[kMaxSkillActionTrace] = {};
static const int kMaxSkillPacketTrace = 2048;
static const int kMaxSkillPacketBytes = 96;
struct SkillPacketTraceEntry {
  uint32_t seq;
  uint32_t tick;
  uint32_t thread_id;
  uint32_t caller;
  uint32_t self_ptr;
  uint32_t length;
  uint32_t captured;
  uint8_t payload[kMaxSkillPacketBytes];
};
static SkillPacketTraceEntry g_skill_packet_trace[kMaxSkillPacketTrace] = {};
static volatile LONG g_skill_action_trace_on = 0;
static volatile LONG g_skill_action_trace_count = 0;
static volatile LONG g_skill_action_trace_dropped = 0;
static volatile LONG g_skill_packet_trace_count = 0;
static volatile LONG g_skill_packet_trace_dropped = 0;
static volatile LONG g_skill_refill_suppress_on = 0;
static volatile LONG g_skill_refill_suppress_mode = 0;
static volatile LONG g_skill_refill_target_skill = 0;
static volatile LONG g_skill_refill_target_config = 0;
static volatile LONG g_skill_refill_restop_pending = 0;
static volatile LONG g_skill_refill_restop_cast = 0;
static volatile LONG g_kuangfeng_tail_auto_on = 0;
static volatile LONG g_kuangfeng_tail_auto_remaining = 0;
static volatile LONG g_kuangfeng_tail_auto_hits = 0;
static volatile LONG g_kuangfeng_tail_auto_due = 0;
static volatile LONG g_kuangfeng_tail_pending = 0;
static volatile LONG g_kuangfeng_tail_pending_cast = 0;
static volatile LONG g_kuangfeng_tail_pending_perform = 0;
static volatile LONG g_kuangfeng_tail_pending_due = 0;
static volatile LONG g_kuangfeng_tail_pending_dropped = 0;
static volatile LONG g_kuangfeng_tail_pending_last_reason = 0;
static volatile LONG g_youfeng_ultimate_tail_auto_on = 0;
static volatile LONG g_youfeng_ultimate_tail_auto_hits = 0;
static volatile LONG g_youfeng_ultimate_tail_pending = 0;
static volatile LONG g_youfeng_ultimate_tail_pending_cast = 0;
static volatile LONG g_youfeng_ultimate_tail_pending_perform = 0;
static volatile LONG g_youfeng_ultimate_tail_pending_due = 0;
static volatile LONG g_youfeng_ultimate_tail_pending_dropped = 0;
static volatile LONG g_youfeng_ultimate_tail_pending_last_reason = 0;
static volatile LONG g_youfeng_ultimate_cd_clear_on = 0;
static volatile LONG g_youfeng_ultimate_cd_clear_hits = 0;
static volatile LONG g_youfeng_ultimate_cd_clear_failures = 0;
static volatile LONG g_youfeng_ultimate_cd_clear_last_reason = 0;
static volatile LONG g_youfeng_ultimate_cd_clear_last_remaining = 0;
static volatile LONG g_youfeng_ultimate_cd_clear_last_total = 0;
static volatile LONG g_youfeng_ultimate_cd_clear_last_key = 0;
static volatile LONG g_youfeng_ultimate_cd_clear_last_record = 0;
static volatile LONG g_youfeng_chain_phase = 0;
static volatile LONG g_youfeng_chain_due = 0;
static volatile LONG g_youfeng_chain_cast = 0;
static volatile LONG g_youfeng_chain_host = 0;
static volatile LONG g_youfeng_chain_mgr = 0;
static volatile LONG g_youfeng_next_gate_on = 0;
static volatile LONG g_youfeng_next_gate_due = 0;
static volatile LONG g_youfeng_next_gate_hits = 0;
static volatile LONG g_youfeng_next_gate_skill = 0;
static volatile LONG g_youfeng_next_gate_config = 0;
static volatile LONG g_youfeng_next_gate_allow_cleared = 0;
static volatile LONG g_youfeng_next_gate_qinggong = 0;
static volatile LONG g_youfeng_mash_phase = 0;
static volatile LONG g_youfeng_mash_due = 0;
static volatile LONG g_youfeng_mash_cast = 0;
static volatile LONG g_youfeng_mash_host = 0;
static volatile LONG g_youfeng_mash_skill = 0;
static volatile LONG g_youfeng_mash_config = 0;
static volatile LONG g_youfeng_phase_up_pending = 0;
static volatile LONG g_youfeng_phase_due = 0;
static volatile LONG g_youfeng_phase_net = 0;
static volatile LONG g_youfeng_phase_config = 0;

static void* AllocNearExec(void* near_to, size_t size) {
  // Prefer same 2GB window; fall back to any.
  SYSTEM_INFO si;
  GetSystemInfo(&si);
  uintptr_t base = (uintptr_t)near_to;
  uintptr_t step = 0x10000;
  for (int i = 0; i < 0x400; ++i) {
    for (int sign = -1; sign <= 1; sign += 2) {
      uintptr_t try_a = base + (uintptr_t)(sign * (int)(step * (i + 1)));
      try_a &= ~(uintptr_t)0xFFF;
      if (try_a < (uintptr_t)si.lpMinimumApplicationAddress ||
          try_a > (uintptr_t)si.lpMaximumApplicationAddress)
        continue;
      void* p = VirtualAlloc((void*)try_a, size, MEM_COMMIT | MEM_RESERVE,
                            PAGE_EXECUTE_READWRITE);
      if (p) return p;
    }
  }
  return VirtualAlloc(nullptr, size, MEM_COMMIT | MEM_RESERVE,
                      PAGE_EXECUTE_READWRITE);
}

static bool WriteRelJmp(uint8_t* from, uint8_t* to) {
  DWORD old = 0;
  if (!VirtualProtect(from, 5, PAGE_EXECUTE_READWRITE, &old)) return false;
  from[0] = 0xE9;
  *(int32_t*)(from + 1) = (int32_t)((intptr_t)to - (intptr_t)(from + 5));
  FlushInstructionCache(GetCurrentProcess(), from, 5);
  VirtualProtect(from, 5, old, &old);
  return true;
}

static void* ResolveInputThis() {
  __try {
    uint32_t base = g_shm ? g_shm->module_base : 0;
    if (!base) base = (uint32_t)(uintptr_t)g_game;
    if (!base) base = kDefaultBase;
    uint32_t glob_va =
        base + (kNoteGameRootGlobal - kDefaultBase);
    uint32_t* glob = (uint32_t*)(uintptr_t)glob_va;
    if (!glob || !*glob) return nullptr;
    uint32_t mid = *(uint32_t*)(uintptr_t)(*glob + kGameRootMidOff);
    if (!mid) return nullptr;
    uint32_t inp = *(uint32_t*)(uintptr_t)(mid + kMidInputThisOff);
    return (void*)(uintptr_t)inp;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return nullptr;
  }
}

// Ensure game key gate [root+0x4D4] is on so UpdateKeys polls GAKS.
// NOTE: focus handler at 0x48932C overwrites this from real focus; we re-seed
// every maintain tick while force is on.
static int EnsureKeyGateOn() {
  __try {
    uint32_t base = g_shm ? g_shm->module_base : 0;
    if (!base) base = (uint32_t)(uintptr_t)g_game;
    if (!base) base = kDefaultBase;
    uint32_t root =
        *(uint32_t*)(uintptr_t)(base + (kNoteGameRootGlobal - kDefaultBase));
    if (!root) return 0;
    uint8_t* gate = (uint8_t*)(uintptr_t)(root + kGameRootKeyGateOff);
    uint8_t was = *gate;
    *gate = 1;
    // Mirror shadow used by focus path (best-effort).
    uint32_t shadow_va = base + (kNoteKeyGateShadow - kDefaultBase);
    *(uint8_t*)(uintptr_t)shadow_va = 1;
    return was ? 1 : 2;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return 0;
  }
}

// Call game UpdateKeys(this) so [this+0x134] is rebuilt from current GAKS and
// the clear-loop sees Shift down (does not wipe table[0x10]).
static int CallUpdateKeys(void* input_this) {
  if (!input_this) return 0;
  uint32_t base = g_shm ? g_shm->module_base : 0;
  if (!base) base = (uint32_t)(uintptr_t)g_game;
  if (!base) base = kDefaultBase;
  uint32_t va = base + (kNoteUpdateKeys - kDefaultBase);
  __try {
    void* fn = (void*)(uintptr_t)va;
    __asm {
      mov ecx, input_this
      call fn
    }
    return 1;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return -1;
  }
}

// Live GetModMask() for diagnostics (same as free-aim gate reads).
static uint32_t CallGetModMask() {
  uint32_t base = g_shm ? g_shm->module_base : 0;
  if (!base) base = (uint32_t)(uintptr_t)g_game;
  if (!base) base = kDefaultBase;
  uint32_t va = base + (kNoteGetModMask - kDefaultBase);
  uint32_t bits = 0;
  __try {
    void* fn = (void*)(uintptr_t)va;
    __asm {
      call fn
      mov bits, eax
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    bits = 0xFFFFFFFFu;
  }
  return bits;
}

// CheckModBind(controller, bind_id) — bind id 1 is free aim / Shift.
typedef uint8_t(__thiscall* FnCheckModBind)(void* self, uint32_t bind_id);

static int CallCheckModBind1(void* controller) {
  if (!controller) return -1;
  uint32_t base = g_shm ? g_shm->module_base : 0;
  if (!base) base = (uint32_t)(uintptr_t)g_game;
  if (!base) base = kDefaultBase;
  FnCheckModBind fn = (FnCheckModBind)(uintptr_t)(
      base + (kNoteCheckModBind - kDefaultBase));
  __try {
    return fn(controller, 1u) ? 1 : 0;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return -2;
  }
}

static void FormatControllerDiag(char* buf, size_t n) {
  if (!buf || !n) return;
  void* inp = ResolveInputThis();
  void* ctrl = nullptr;
  int bind1 = -1;
  int f243d = -1, f243e = -1, f2490 = -1, f24a8 = -1;
  uint32_t base = g_shm ? g_shm->module_base : 0;
  if (!base) base = (uint32_t)(uintptr_t)g_game;
  if (!base) base = kDefaultBase;
  uint32_t root = 0, mode_ptr = 0;
  int mode_a = -1, mode_b = -1;
  __try {
    if (inp) ctrl = *(void**)((uint8_t*)inp + 0x140);
    if (ctrl) {
      uint8_t* c = (uint8_t*)ctrl;
      f243d = c[0x243D];
      f243e = c[0x243E];
      f2490 = c[0x2490];
      f24a8 = c[0x24A8];
      bind1 = CallCheckModBind1(ctrl);
    }
    root = *(uint32_t*)(uintptr_t)(
        base + (kNoteGameRootGlobal - kDefaultBase));
    if (root) {
      uint32_t root20 = *(uint32_t*)(uintptr_t)(root + 0x20);
      if (root20) {
        mode_ptr = *(uint32_t*)(uintptr_t)(root20 + 0xE48);
        if (mode_ptr) {
          mode_a = *(uint8_t*)(uintptr_t)(mode_ptr + 1);
          mode_b = *(uint8_t*)(uintptr_t)(mode_ptr + 2);
        }
      }
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  _snprintf_s(buf, n, _TRUNCATE,
      "CTRL_DIAG bind1=%d ctrl=%08X f=%d/%d/%d/%d mode=%08X:%d/%d",
      bind1, (unsigned)(uintptr_t)ctrl, f243d, f243e, f2490, f24a8,
      mode_ptr, mode_a, mode_b);
}

static int ReadKeyGate() {
  __try {
    uint32_t base = g_shm ? g_shm->module_base : 0;
    if (!base) base = (uint32_t)(uintptr_t)g_game;
    if (!base) base = kDefaultBase;
    uint32_t root =
        *(uint32_t*)(uintptr_t)(base + (kNoteGameRootGlobal - kDefaultBase));
    if (!root) return -1;
    return *(uint8_t*)(uintptr_t)(root + kGameRootKeyGateOff);
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return -2;
  }
}

// Sample process GAKS + input table + GetModMask after force for status note.
// Packs into g_shm->ret high bits when needed; primarily formats err string.
static void FormatForceDiag(char* buf, size_t n, int flags, bool only_level) {
  if (!buf || !n) return;
  void* inp = ResolveInputThis();
  if (!inp) inp = g_input_this;
  int gate = ReadKeyGate();
  SHORT gaks10 = GetAsyncKeyState(0x10);
  SHORT gaksA0 = GetAsyncKeyState(0xA0);
  uint8_t t10 = 0, tA0 = 0;
  uint32_t mask = 0;
  if (inp) {
    __try {
      uint8_t* t = (uint8_t*)inp;
      t10 = t[kInputKeyTableOff + 0x10];
      tA0 = t[kInputKeyTableOff + 0xA0];
      mask = *(uint32_t*)(t + kInputModMaskOff);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
    }
  }
  uint32_t mod = CallGetModMask();
  _snprintf_s(
      buf, n, _TRUNCATE,
      only_level
          ? "KEY_FORCE HOLD f=0x%X gate=%d gaks=%04X/%04X tab=%u/%u mask=%X mod=%X"
          : "KEY_FORCE ON f=0x%X gate=%d gaks=%04X/%04X tab=%u/%u mask=%X mod=%X",
      flags, gate, (unsigned)(uint16_t)gaks10, (unsigned)(uint16_t)gaksA0,
      (unsigned)t10, (unsigned)tA0, mask, mod);
}

// thiscall InjectKey @ 0x4BF9C0(input, msg, wParam, lParam, lParamHi) — ret 0x14
typedef uint8_t(__fastcall* FnInjectKey)(void* thisptr, void* edx, uint32_t msg,
                                         uint32_t wparam, uint32_t lparam,
                                         uint32_t lparam_hi, uint32_t unused);

static int InjectShiftKeyEvent(void* input_this, bool down) {
  if (!input_this) return 0;
  uint32_t base = g_shm ? g_shm->module_base : 0;
  if (!base) base = (uint32_t)(uintptr_t)g_game;
  if (!base) base = kDefaultBase;
  uint32_t va = base + (kNoteInjectKey - kDefaultBase);
  // Caller at 49B7C9:
  //   push arg5; push arg4; push lParam(ebp); push wParam(ebx); push msg(edi)
  //   mov ecx, [esi+0x78]; call 4BF9C0
  // Inject both VK_SHIFT(0x10) and VK_LSHIFT(0xA0): free-aim bind table may use
  // either; IsKeyTable is per-vk.
  __try {
    void* fn = (void*)(uintptr_t)va;
    int ok_n = 0;
    const uint32_t msgs[2] = {down ? 0x100u : 0x101u, down ? 0x100u : 0x101u};
    const uint32_t vks[2] = {0x10u, 0xA0u};
    const uint32_t lps[2] = {
        down ? 0x002A0001u : 0xC02A0001u,  // scan 0x2A LSHIFT
        down ? 0x002A0001u : 0xC02A0001u,
    };
    for (int i = 0; i < 2; ++i) {
      uint32_t msg = msgs[i];
      uint32_t vk = vks[i];
      uint32_t lparam = lps[i];
      uint8_t ok = 0;
      __asm {
        push 0
        push 0
        push lparam
        push vk
        push msg
        mov ecx, input_this
        call fn
        mov ok, al
      }
      if (ok) ok_n++;
    }
    return ok_n > 0 ? 1 : 0;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return -1;
  }
}

static void ApplyForcedShiftOnInput(void* input_this) {
  if (!input_this) return;
  __try {
    uint8_t* t = (uint8_t*)input_this;
    // Key table: vk 0x10 / 0xA0 / 0xA1
    t[kInputKeyTableOff + 0x10] = 1;
    t[kInputKeyTableOff + 0xA0] = 1;
    t[kInputKeyTableOff + 0xA1] = 1;
    uint32_t* mask = (uint32_t*)(t + kInputModMaskOff);
    *mask |= 1u;  // bit0 = Shift
    g_input_this = input_this;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
}

static void ClearForcedShiftOnInput(void* input_this) {
  if (!input_this) return;
  __try {
    uint8_t* t = (uint8_t*)input_this;
    t[kInputKeyTableOff + 0x10] = 0;
    t[kInputKeyTableOff + 0xA0] = 0;
    t[kInputKeyTableOff + 0xA1] = 0;
    uint32_t* mask = (uint32_t*)(t + kInputModMaskOff);
    *mask &= ~1u;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
}

// thiscall IsKeyTable: ecx=this, [esp+4]=vk; return al
static void ApplyForcedVkOnInput(void* input_this, uint32_t vk) {
  if (!input_this) return;
  __try {
    uint8_t* t = (uint8_t*)input_this;
    uint32_t u = vk & 0xFFu;
    t[kInputKeyTableOff + u] = 1;
    // Keep left/generic modifiers coherent for Shift/Ctrl/Alt groups.
    if (u == 0x10u || u == 0xA0u || u == 0xA1u) {
      t[kInputKeyTableOff + 0x10] = 1;
      t[kInputKeyTableOff + 0xA0] = 1;
      t[kInputKeyTableOff + 0xA1] = 1;
      uint32_t* mask = (uint32_t*)(t + kInputModMaskOff);
      *mask |= 1u;
    } else if (u == 0x11u || u == 0xA2u || u == 0xA3u) {
      t[kInputKeyTableOff + 0x11] = 1;
      t[kInputKeyTableOff + 0xA2] = 1;
      uint32_t* mask = (uint32_t*)(t + kInputModMaskOff);
      *mask |= 2u;
    } else if (u == 0x12u || u == 0xA4u || u == 0xA5u) {
      t[kInputKeyTableOff + 0x12] = 1;
      t[kInputKeyTableOff + 0xA4] = 1;
      uint32_t* mask = (uint32_t*)(t + kInputModMaskOff);
      *mask |= 4u;
    }
    g_input_this = input_this;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
}

static void ApplyAllForcedOnInput(void* input_this) {
  if (!input_this) return;
  if (InterlockedCompareExchange(&g_force_shift, 0, 0)) {
    ApplyForcedShiftOnInput(input_this);
  }
  for (int i = 1; i < 256; ++i) {
    if (g_force_vk[i]) ApplyForcedVkOnInput(input_this, (uint32_t)i);
  }
}

static void ClearForcedVkOnInput(void* input_this, uint32_t vk) {
  if (!input_this) return;
  __try {
    uint8_t* t = (uint8_t*)input_this;
    uint32_t u = vk & 0xFFu;
    t[kInputKeyTableOff + u] = 0;
    if (u == 0x10u || u == 0xA0u || u == 0xA1u) {
      if (!IsForcedVk(0x10) && !IsForcedVk(0xA0) && !IsForcedVk(0xA1) &&
          !InterlockedCompareExchange(&g_force_shift, 0, 0)) {
        t[kInputKeyTableOff + 0x10] = 0;
        t[kInputKeyTableOff + 0xA0] = 0;
        t[kInputKeyTableOff + 0xA1] = 0;
        uint32_t* mask = (uint32_t*)(t + kInputModMaskOff);
        *mask &= ~1u;
      }
    } else if (u == 0x11u || u == 0xA2u || u == 0xA3u) {
      if (!IsForcedVk(0x11) && !IsForcedVk(0xA2) && !IsForcedVk(0xA3)) {
        t[kInputKeyTableOff + 0x11] = 0;
        t[kInputKeyTableOff + 0xA2] = 0;
        uint32_t* mask = (uint32_t*)(t + kInputModMaskOff);
        *mask &= ~2u;
      }
    } else if (u == 0x12u || u == 0xA4u || u == 0xA5u) {
      if (!IsForcedVk(0x12) && !IsForcedVk(0xA4) && !IsForcedVk(0xA5)) {
        t[kInputKeyTableOff + 0x12] = 0;
        t[kInputKeyTableOff + 0xA4] = 0;
        uint32_t* mask = (uint32_t*)(t + kInputModMaskOff);
        *mask &= ~4u;
      }
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
}

static uint8_t __fastcall Hook_IsKeyTable(void* thisptr, void* /*edx*/,
                                          uint32_t vk) {
  if (thisptr) g_input_this = thisptr;
  if (IsForcedVk((int)vk)) {
    ApplyForcedVkOnInput(thisptr, vk);
    if (InterlockedCompareExchange(&g_force_shift, 0, 0) &&
        (vk == 0x10u || vk == 0xA0u || vk == 0xA1u)) {
      ApplyForcedShiftOnInput(thisptr);
    }
    return 1;
  }
  // Original: al = byte [this + 0x2C + vk]
  __try {
    if (!thisptr) return 0;
    return ((uint8_t*)thisptr)[kInputKeyTableOff + (vk & 0xFFu)];
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return 0;
  }
}

// GetModMask is __cdecl/no-arg style in disasm: uses only globals + GAKS, no this.
// Actually prolog has no ecx use for this — pure GAKS. We return orig|1 when force.
// Implement as naked-style via __stdcall wrapper after trampoline?
// Stolen 5 bytes: mov eax,[0x15282D8] — need full original via trampoline.
// Simpler: detour entire function body with our C function that reimplements.
static uint32_t __cdecl Hook_GetModMask_Impl() {
  uint32_t bits = 0;
  FnGetAsyncKeyState real = g_real_GetAsyncKeyState;
  auto down = [&](int vk) -> bool {
    if (IsForcedVk(vk)) return true;
    if (!real) return false;
    return (real(vk) & 0x8000) != 0;
  };
  // Mirror 0x4BF0B0: gate on [root+0x4D4]
  __try {
    uint32_t base = g_shm ? g_shm->module_base : 0;
    if (!base) base = (uint32_t)(uintptr_t)g_game;
    if (!base) base = kDefaultBase;
    uint32_t root = *(uint32_t*)(uintptr_t)(base + (kNoteGameRootGlobal - kDefaultBase));
    if (root && *(uint8_t*)(uintptr_t)(root + 0x4D4)) {
      if (down(0x10)) bits |= 1;
      if (down(0x11)) bits |= 2;
      if (down(0x12)) bits |= 4;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    if (down(0x10)) bits |= 1;
    if (down(0x11)) bits |= 2;
    if (down(0x12)) bits |= 4;
  }
  if (IsForcedVk(0x10) || IsForcedVk(0xA0) || IsForcedVk(0xA1) ||
      InterlockedCompareExchange(&g_force_shift, 0, 0))
    bits |= 1;
  if (IsForcedVk(0x11) || IsForcedVk(0xA2) || IsForcedVk(0xA3)) bits |= 2;
  if (IsForcedVk(0x12) || IsForcedVk(0xA4) || IsForcedVk(0xA5)) bits |= 4;
  return bits;
}

static bool InstallInlineDetour(InlineHookRec* hk, uint8_t* target, int steal_n,
                                void* detour) {
  if (!hk || !target || steal_n < 5 || steal_n > 16 || !detour) return false;
  if (hk->active) return true;
  uint8_t* tramp = hk->trampoline;
  const bool reuse = tramp && hk->target == target && hk->stolen_n == steal_n;
  if (reuse) {
    __try {
      if (memcmp(target, hk->stolen, steal_n) != 0) return false;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      return false;
    }
  } else {
    // Build trampoline: stolen + jmp back. A retired trampoline is never
    // replaced because another game thread may still have its IP inside it.
    if (tramp) return false;
    tramp = (uint8_t*)AllocNearExec(target, 32);
    if (!tramp) return false;
    memcpy(hk->stolen, target, steal_n);
    memcpy(tramp, target, steal_n);
    tramp[steal_n] = 0xE9;
    *(int32_t*)(tramp + steal_n + 1) = (int32_t)(
        (intptr_t)(target + steal_n) - (intptr_t)(tramp + steal_n + 5));
  }
  // Patch target: jmp detour + nops
  DWORD old = 0;
  if (!VirtualProtect(target, steal_n, PAGE_EXECUTE_READWRITE, &old)) {
    if (!reuse) VirtualFree(tramp, 0, MEM_RELEASE);
    return false;
  }
  target[0] = 0xE9;
  *(int32_t*)(target + 1) =
      (int32_t)((intptr_t)detour - (intptr_t)(target + 5));
  for (int i = 5; i < steal_n; ++i) target[i] = 0x90;
  FlushInstructionCache(GetCurrentProcess(), target, steal_n);
  VirtualProtect(target, steal_n, old, &old);
  hk->target = target;
  hk->trampoline = tramp;
  hk->stolen_n = steal_n;
  hk->active = 1;
  return true;
}

static void RemoveInlineDetour(InlineHookRec* hk) {
  if (!hk || !hk->active || !hk->target) return;
  DWORD old = 0;
  if (VirtualProtect(hk->target, hk->stolen_n, PAGE_EXECUTE_READWRITE, &old)) {
    memcpy(hk->target, hk->stolen, hk->stolen_n);
    FlushInstructionCache(GetCurrentProcess(), hk->target, hk->stolen_n);
    VirtualProtect(hk->target, hk->stolen_n, old, &old);
  }
  // Never free executable memory here. A thread can already have branched to
  // the detour/trampoline before the original bytes were restored.
  hk->active = 0;
}

// Naked entry for IsKeyTable: game uses thiscall; our Hook_IsKeyTable is __fastcall
// compatible (ecx=this, stack arg=vk).
// For GetModMask: pure rewrite — no need trampoline original if we reimplement.

static bool InstallInputInlineHooks() {
  if (InterlockedCompareExchange(&g_inline_hooks_installed, 0, 0)) return true;
  uint32_t base = g_shm ? g_shm->module_base : 0;
  if (!base) base = (uint32_t)(uintptr_t)g_game;
  if (!base) base = kDefaultBase;
  uint8_t* iskey =
      (uint8_t*)(uintptr_t)(base + (kNoteIsKeyTable - kDefaultBase));
  uint8_t* modmask =
      (uint8_t*)(uintptr_t)(base + (kNoteGetModMask - kDefaultBase));
  // Verify prologs match expected bytes before patching.
  __try {
    // 4BEE00: 8B 44 24 04
    if (iskey[0] != 0x8B || iskey[1] != 0x44 || iskey[2] != 0x24 ||
        iskey[3] != 0x04) {
      return false;
    }
    // 4BF0B0: A1 xx xx xx xx
    if (modmask[0] != 0xA1) {
      return false;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  // IsKeyTable: steal 8 bytes (two insns), jmp to Hook_IsKeyTable.
  // After hook: ret 4 is NOT in stolen — we must ret 4 ourselves.
  // Hook_IsKeyTable is __fastcall(this,edx,vk) — MSVC will emit ret 4 for
  // third stack arg... actually __fastcall with one stack arg uses ret 4. OK.
  if (!InstallInlineDetour(&g_hk_iskey, iskey, 8, (void*)&Hook_IsKeyTable)) {
    return false;
  }
  // GetModMask: steal 5 bytes, jump to reimplementation.
  // Our impl ends with ret (cdecl, 0 args). Original also ret 0. OK.
  if (!InstallInlineDetour(&g_hk_modmask, modmask, 5,
                           (void*)&Hook_GetModMask_Impl)) {
    RemoveInlineDetour(&g_hk_iskey);
    return false;
  }
  InterlockedExchange(&g_inline_hooks_installed, 1);
  return true;
}

static void RemoveInputInlineHooks() {
  if (!InterlockedCompareExchange(&g_inline_hooks_installed, 0, 0)) return;
  RemoveInlineDetour(&g_hk_modmask);
  RemoveInlineDetour(&g_hk_iskey);
  InterlockedExchange(&g_inline_hooks_installed, 0);
}

// Free-aim gate (0x4C13D0) needs BOTH:
//   GetModMask()  -> live GetAsyncKeyState(VK_SHIFT) bit0  (gate [root+0x4D4] on)
//   IsKeyTable(vk) -> input key table [this+0x2C+vk]
// UpdateKeys clears table[vk] whenever GAKS says UP, so memory-only hold races
// every frame. SoftSend updates process-wide async key state (even background).
// SoftSend must also run on OFF, or sticky Shift never lifts.
// Focus path 0x48932C overwrites [root+0x4D4] from real focus — re-seed often.

// SoftSend any VK (optional pollution). Solution-2 default is OFF.
// @author by ak
static int SoftSendVkGaks(UINT vk, bool down) {
  if (!vk) return 0;
  const UINT sc = MapVirtualKeyW(vk, MAPVK_VK_TO_VSC);
  INPUT in[2];
  ZeroMemory(in, sizeof(in));
  in[0].type = INPUT_KEYBOARD;
  in[0].ki.wVk = (WORD)vk;
  in[0].ki.wScan = (WORD)(sc & 0xFFu);
  in[0].ki.dwFlags = down ? 0 : KEYEVENTF_KEYUP;
  in[1].type = INPUT_KEYBOARD;
  in[1].ki.wVk = 0;
  in[1].ki.wScan = (WORD)(sc & 0xFFu);
  in[1].ki.dwFlags = (down ? 0 : KEYEVENTF_KEYUP) | KEYEVENTF_SCANCODE;
  if (SendInput(2, in, sizeof(INPUT)) > 0) return 1;
  keybd_event((BYTE)vk, (BYTE)(sc & 0xFFu), down ? 0 : KEYEVENTF_KEYUP, 0);
  return 1;
}

static int InjectVkKeyEvent(void* input_this, UINT vk, bool down) {
  if (!input_this || !vk) return 0;
  uint32_t base = g_shm ? g_shm->module_base : 0;
  if (!base) base = (uint32_t)(uintptr_t)g_game;
  if (!base) base = kDefaultBase;
  uint32_t va = base + (kNoteInjectKey - kDefaultBase);
  const UINT sc = MapVirtualKeyW(vk, MAPVK_VK_TO_VSC);
  uint32_t msg = down ? 0x100u : 0x101u;
  uint32_t lparam = 1u | ((sc & 0xFFu) << 16);
  if (!down) lparam |= (1u << 30) | (1u << 31);
  __try {
    void* fn = (void*)(uintptr_t)va;
    uint8_t ok = 0;
    uint32_t wparam = (uint32_t)vk;
    __asm {
      push 0
      push 0
      push lparam
      push wparam
      push msg
      mov ecx, input_this
      call fn
      mov ok, al
    }
    return ok ? 1 : 0;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return -1;
  }
}

// flags: bit0=input bit1=InjectKey bit2=SoftSend bit3=UpdateKeys bit4=hooks
static int SyncForcedVkState(UINT vk, bool on, bool level_only, bool allow_softsend) {
  EnsureKeyGateOn();
  void* inp = ResolveInputThis();
  if (!inp) inp = g_input_this;
  int flags = inp ? 1 : 0;
  if (InterlockedCompareExchange(&g_key_hooks_installed, 0, 0) ||
      InterlockedCompareExchange(&g_inline_hooks_installed, 0, 0)) {
    flags |= 16;
  }
  if (on) {
    if (!level_only) {
      if (allow_softsend) {
        if (SoftSendVkGaks(vk, true) > 0) flags |= 4;
      }
      if (inp) {
        int inj = InjectVkKeyEvent(inp, vk, true);
        if (inj > 0) flags |= 2;
      }
    } else if (allow_softsend) {
      SHORT st = GetAsyncKeyState((int)vk);
      // Prefer hooked view: if force on, skip SoftSend unless real high bit needed.
      if ((st & 0x8000) == 0 && !IsForcedVk((int)vk)) {
        if (SoftSendVkGaks(vk, true) > 0) flags |= 4;
      }
    }
    ApplyForcedVkOnInput(inp, vk);
    if (CallUpdateKeys(inp) > 0) flags |= 8;
    // Multi-key: UpdateKeys may rewrite the whole table; re-seed every forced VK.
    ApplyAllForcedOnInput(inp);
    return flags;
  }
  if (!level_only) {
    if (allow_softsend) {
      if (SoftSendVkGaks(vk, false) > 0) flags |= 4;
    }
    if (inp) {
      int inj = InjectVkKeyEvent(inp, vk, false);
      if (inj > 0) flags |= 2;
    }
  }
  ClearForcedVkOnInput(inp, vk);
  if (CallUpdateKeys(inp) > 0) flags |= 8;
  // Keep remaining forced keys alive after clearing one.
  ApplyAllForcedOnInput(inp);
  return flags;
}

static void MaintainForcedKeys() {
  if (!InterlockedCompareExchange(&g_force_any, 0, 0) &&
      !InterlockedCompareExchange(&g_force_shift, 0, 0))
    return;
  EnsureKeyGateOn();
  void* inp = ResolveInputThis();
  if (!inp) inp = g_input_this;
  const bool soft =
      InterlockedCompareExchange(&g_hold_allow_softsend, 0, 0) != 0;
  ApplyAllForcedOnInput(inp);
  if (soft) {
    for (int i = 1; i < 256; ++i) {
      if (!g_force_vk[i]) continue;
      // Only SoftSend if real GAKS (unhooked) would be needed — call real if avail.
      SHORT st = 0;
      if (g_real_GetAsyncKeyState)
        st = g_real_GetAsyncKeyState(i);
      else
        st = GetAsyncKeyState(i);
      // With hooks installed GetAsyncKeyState is forced; use real.
      if (g_real_GetAsyncKeyState && (st & 0x8000) == 0) {
        SoftSendVkGaks((UINT)i, true);
      }
    }
  }
  if (inp) CallUpdateKeys(inp);
  ApplyAllForcedOnInput(inp);
}

static void FormatHoldDiag(char* buf, size_t n, UINT vk, int flags, bool only_level) {
  if (!buf || !n) return;
  void* inp = ResolveInputThis();
  if (!inp) inp = g_input_this;
  int gate = ReadKeyGate();
  SHORT gaks = 0;
  if (g_real_GetAsyncKeyState)
    gaks = g_real_GetAsyncKeyState((int)vk);
  else
    gaks = GetAsyncKeyState((int)vk);
  SHORT gaks_hook = GetAsyncKeyState((int)vk);
  uint8_t tab = 0;
  uint32_t mask = 0;
  if (inp) {
    __try {
      uint8_t* t = (uint8_t*)inp;
      tab = t[kInputKeyTableOff + (vk & 0xFFu)];
      mask = *(uint32_t*)(t + kInputModMaskOff);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
    }
  }
  uint32_t mod = CallGetModMask();
  int hooks = InterlockedCompareExchange(&g_key_hooks_installed, 0, 0) ||
                      InterlockedCompareExchange(&g_inline_hooks_installed, 0, 0)
                  ? 1
                  : 0;
  int nforce = CountForcedVks();
  _snprintf_s(
      buf, n, _TRUNCATE,
      only_level
          ? "KEY_HOLD HOLD vk=0x%02X f=0x%X gate=%d hooks=%d real=%04X view=%04X tab=%u mask=%X mod=%X n=%d"
          : "KEY_HOLD ON vk=0x%02X f=0x%X gate=%d hooks=%d real=%04X view=%04X tab=%u mask=%X mod=%X n=%d",
      (unsigned)(vk & 0xFFu), flags, gate, hooks, (unsigned)(uint16_t)gaks,
      (unsigned)(uint16_t)gaks_hook, (unsigned)tab, mask, mod, nforce);
}

static int SoftSendShiftGaks(bool down) {
  // Prefer LSHIFT so both VK_SHIFT and VK_LSHIFT GAKS go high.
  const UINT vk = 0xA0u; /* LSHIFT */
  const UINT sc = MapVirtualKeyW(vk, MAPVK_VK_TO_VSC);
  INPUT in[2];
  ZeroMemory(in, sizeof(in));
  // VK path
  in[0].type = INPUT_KEYBOARD;
  in[0].ki.wVk = (WORD)vk;
  in[0].ki.wScan = (WORD)(sc & 0xFFu);
  in[0].ki.dwFlags = down ? 0 : KEYEVENTF_KEYUP;
  // Scan path (some polls ignore VK-only)
  in[1].type = INPUT_KEYBOARD;
  in[1].ki.wVk = 0;
  in[1].ki.wScan = (WORD)(sc & 0xFFu);
  in[1].ki.dwFlags = (down ? 0 : KEYEVENTF_KEYUP) | KEYEVENTF_SCANCODE;
  if (SendInput(2, in, sizeof(INPUT)) > 0) return 1;
  keybd_event((BYTE)vk, (BYTE)(sc & 0xFFu), down ? 0 : KEYEVENTF_KEYUP, 0);
  return 1;
}

// Post WM_KEY* to game hwnd (and thread) without SetForegroundWindow.
// KEY_DIAG showed SoftSend/InjectKey leave shift_wm=0 — free-aim entry needs
// a real message-queue edge even when the client is background / minimized.
// @author by ak
static int PostShiftKeyMsg(bool down) {
  HWND h = g_hwnd;
  if ((!h || !IsWindow(h)) && g_shm && g_shm->hwnd) {
    h = (HWND)(uintptr_t)g_shm->hwnd;
  }
  if (!h || !IsWindow(h)) return 0;
  const UINT sc = MapVirtualKeyW(0xA0u, MAPVK_VK_TO_VSC);
  const UINT msg = down ? 0x100u /* WM_KEYDOWN */ : 0x101u /* WM_KEYUP */;
  LPARAM lp = (LPARAM)(1u | ((sc & 0xFFu) << 16));
  if (!down) lp |= (1u << 30) | (1u << 31);
  int ok = 0;
  if (PostMessageW(h, msg, (WPARAM)0x10u, lp)) ok++;
  if (PostMessageW(h, msg, (WPARAM)0xA0u, lp)) ok++;
  DWORD tid = 0;
  GetWindowThreadProcessId(h, &tid);
  if (tid) {
    // Best-effort: some pumps read thread queue more than hwnd queue.
    PostThreadMessageW(tid, msg, (WPARAM)0x10u, lp);
    PostThreadMessageW(tid, msg, (WPARAM)0xA0u, lp);
  }
  return ok > 0 ? 1 : 0;
}

// Memory hold + optional single edge. Reticle is edge-sensitive: InjectKey /
// SoftSend / PostMessage only on true OFF->ON / ON->OFF transitions.
// level_only=true: re-seed gate/table/mask + UpdateKeys (timer / re-assert).
// Return bitmask: bit0=input_this, bit1=InjectKey, bit2=SoftSend,
//                 bit3=UpdateKeys, bit4=PostMessage (WM_KEY* to game hwnd).
static int SyncForcedShiftState(bool on, bool level_only) {
  EnsureKeyGateOn();
  void* inp = ResolveInputThis();
  if (!inp) inp = g_input_this;
  int flags = inp ? 1 : 0;
  if (on) {
    if (!level_only) {
      // SoftSend FIRST so GAKS is down before UpdateKeys clear-loop.
      if (SoftSendShiftGaks(true) > 0) flags |= 4;
      // Message-queue edge (background/minimized safe; no FG steal).
      if (PostShiftKeyMsg(true) > 0) flags |= 16;
    } else {
      // Maintain: if GAKS already down, skip SendInput to reduce FG pollution.
      SHORT st = GetAsyncKeyState(0x10);
      if ((st & 0x8000) == 0) {
        if (SoftSendShiftGaks(true) > 0) flags |= 4;
      }
    }
    ApplyForcedShiftOnInput(inp);
    if (!level_only) {
      int inj = InjectShiftKeyEvent(inp, true);
      if (inj > 0) flags |= 2;
      ApplyForcedShiftOnInput(inp);
    }
    if (CallUpdateKeys(inp) > 0) flags |= 8;
    // UpdateKeys may recompute mask from GAKS; re-apply table in case of race.
    ApplyForcedShiftOnInput(inp);
    return flags;
  }
  if (!level_only) {
    if (SoftSendShiftGaks(false) > 0) flags |= 4;
    if (PostShiftKeyMsg(false) > 0) flags |= 16;
    int inj = InjectShiftKeyEvent(inp, false);
    if (inj > 0) flags |= 2;
  }
  ClearForcedShiftOnInput(inp);
  if (CallUpdateKeys(inp) > 0) flags |= 8;
  return flags;
}

// Timer: re-seed gate + table + UpdateKeys. SoftSend only if GAKS dropped.
// Never re-fire InjectKey edges (double-toggles aim).
static void MaintainForcedShift() {
  if (InterlockedCompareExchange(&g_force_shift, 0, 0)) {
    SyncForcedShiftState(true, /*level_only=*/true);
  }
  MaintainForcedKeys();
}

static bool PatchIatSlot(uint32_t* slot, uint32_t new_fn, uint32_t* out_old) {
  if (!slot || !new_fn) return false;
  DWORD old_prot = 0;
  if (!VirtualProtect(slot, sizeof(uint32_t), PAGE_EXECUTE_READWRITE, &old_prot)) {
    if (!VirtualProtect(slot, sizeof(uint32_t), PAGE_READWRITE, &old_prot)) {
      return false;
    }
  }
  if (out_old) *out_old = *slot;
  *slot = new_fn;
  FlushInstructionCache(GetCurrentProcess(), slot, sizeof(uint32_t));
  VirtualProtect(slot, sizeof(uint32_t), old_prot, &old_prot);
  return true;
}

static bool RecordPatch(uint32_t* slot, uint32_t orig) {
  if (g_iat_patch_count >= kMaxIatPatches) return false;
  // de-dup
  for (int i = 0; i < g_iat_patch_count; ++i) {
    if (g_iat_patches[i].slot == slot) return true;
  }
  g_iat_patches[g_iat_patch_count].slot = slot;
  g_iat_patches[g_iat_patch_count].orig = orig;
  g_iat_patch_count++;
  return true;
}

// Patch one PE module's IAT entries that currently point at known real fns.
static int PatchModuleIatForKeyFns(HMODULE mod, uint32_t hook_async,
                                   uint32_t hook_key) {
  if (!mod || !g_real_GetAsyncKeyState || !g_real_GetKeyState) return 0;
  uint8_t* base = (uint8_t*)mod;
  __try {
    IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    IMAGE_NT_HEADERS32* nt =
        (IMAGE_NT_HEADERS32*)(base + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return 0;
    IMAGE_DATA_DIRECTORY dir =
        nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir.VirtualAddress || !dir.Size) return 0;
    IMAGE_IMPORT_DESCRIPTOR* imp =
        (IMAGE_IMPORT_DESCRIPTOR*)(base + dir.VirtualAddress);
    int patched = 0;
    for (; imp->Name; ++imp) {
      const char* dll = (const char*)(base + imp->Name);
      // Only user32 (and wow variants)
      bool is_user32 = false;
      if (dll) {
        // case-insensitive compare "user32"
        char a[16] = {0};
        for (int i = 0; i < 15 && dll[i]; ++i) {
          char c = dll[i];
          if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
          a[i] = c;
        }
        if (a[0] == 'u' && a[1] == 's' && a[2] == 'e' && a[3] == 'r' &&
            a[4] == '3' && a[5] == '2') {
          is_user32 = true;
        }
      }
      if (!is_user32) continue;

      // Prefer IAT (FirstThunk); OriginalFirstThunk is names.
      IMAGE_THUNK_DATA32* thunk =
          (IMAGE_THUNK_DATA32*)(base + imp->FirstThunk);
      for (; thunk->u1.Function; ++thunk) {
        uint32_t* slot = (uint32_t*)&thunk->u1.Function;
        uint32_t cur = *slot;
        uint32_t want_hook = 0;
        if (cur == (uint32_t)(uintptr_t)g_real_GetAsyncKeyState ||
            cur == hook_async) {
          want_hook = hook_async;
        } else if (cur == (uint32_t)(uintptr_t)g_real_GetKeyState ||
                   cur == hook_key) {
          want_hook = hook_key;
        }
        if (!want_hook) continue;
        if (cur == want_hook) {
          // already hooked
          RecordPatch(slot, (uint32_t)(uintptr_t)(
                                want_hook == hook_async
                                    ? (void*)g_real_GetAsyncKeyState
                                    : (void*)g_real_GetKeyState));
          continue;
        }
        uint32_t oldv = 0;
        if (PatchIatSlot(slot, want_hook, &oldv)) {
          RecordPatch(slot, oldv);
          patched++;
        }
      }
    }
    return patched;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return 0;
  }
}

static bool InstallKeyStateHooks() {
  if (InterlockedCompareExchange(&g_key_hooks_installed, 0, 0)) {
    return true;
  }

  // Resolve real exports from user32 (not from possibly-stale IAT).
  HMODULE u32 = GetModuleHandleW(L"user32.dll");
  if (!u32) u32 = LoadLibraryW(L"user32.dll");
  if (!u32) return false;
  if (!g_real_GetAsyncKeyState) {
    g_real_GetAsyncKeyState =
        (FnGetAsyncKeyState)GetProcAddress(u32, "GetAsyncKeyState");
  }
  if (!g_real_GetKeyState) {
    g_real_GetKeyState = (FnGetKeyState)GetProcAddress(u32, "GetKeyState");
  }
  if (!g_real_GetAsyncKeyState || !g_real_GetKeyState) return false;

  uint32_t hook_async = (uint32_t)(uintptr_t)&Hook_GetAsyncKeyState;
  uint32_t hook_key = (uint32_t)(uintptr_t)&Hook_GetKeyState;

  // 1) Explicit xajh.exe IAT RVAs (primary, known good).
  uint32_t base = g_shm ? g_shm->module_base : 0;
  if (!base) base = (uint32_t)(uintptr_t)g_game;
  if (!base) base = kDefaultBase;
  {
    uint32_t* iat_async =
        (uint32_t*)(uintptr_t)(base + kIatRvaGetAsyncKeyState);
    uint32_t* iat_key = (uint32_t*)(uintptr_t)(base + kIatRvaGetKeyState);
    __try {
      uint32_t cur_a = *iat_async;
      uint32_t cur_k = *iat_key;
      // If still import-name thunk (low / points into .rdata names) skip —
      // loader should have bound; at runtime these are real addrs.
      if (cur_a > 0x10000u) {
        uint32_t oldv = 0;
        if (cur_a != hook_async &&
            PatchIatSlot(iat_async, hook_async, &oldv)) {
          RecordPatch(iat_async, oldv);
        }
      }
      if (cur_k > 0x10000u) {
        uint32_t oldv = 0;
        if (cur_k != hook_key && PatchIatSlot(iat_key, hook_key, &oldv)) {
          RecordPatch(iat_key, oldv);
        }
      }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      // continue with module scan
    }
  }

  // 2) Patch every loaded module that imports user32 key APIs
  //    (plugins / renderer may poll Shift too).
  HMODULE mods[512];
  DWORD needed = 0;
  int total = 0;
  if (EnumProcessModules(GetCurrentProcess(), mods, sizeof(mods), &needed)) {
    int n = (int)(needed / sizeof(HMODULE));
    if (n > 512) n = 512;
    for (int i = 0; i < n; ++i) {
      total += PatchModuleIatForKeyFns(mods[i], hook_async, hook_key);
    }
  }

  if (g_iat_patch_count <= 0 && total <= 0) {
    return false;
  }
  // Game reticle path also reads key table [input+0x2C+vk] / GetModMask —
  // IAT force alone is not enough (KEY_TRACE 2026-07-17).
  if (!InstallInputInlineHooks()) {
    // Still keep IAT; SyncForcedShiftState may write table via ResolveInputThis.
  }
  InterlockedExchange(&g_key_hooks_installed, 1);
  return true;
}

static void RemoveKeyStateHooks() {
  if (!InterlockedCompareExchange(&g_key_hooks_installed, 0, 0)) return;
  InterlockedExchange(&g_key_trace_on, 0);
  InterlockedExchange(&g_force_shift, 0);
  for (int i = 0; i < 256; ++i) g_force_vk[i] = 0;
  RecountForceAny();
  SyncForcedShiftState(false, /*level_only=*/false);
  RemoveInputInlineHooks();
  for (int i = 0; i < g_iat_patch_count; ++i) {
    if (g_iat_patches[i].slot && g_iat_patches[i].orig) {
      PatchIatSlot(g_iat_patches[i].slot, g_iat_patches[i].orig, nullptr);
    }
  }
  g_iat_patch_count = 0;
  InterlockedExchange(&g_key_hooks_installed, 0);
}

static void SetErr(const char* msg) {
  if (!g_shm) return;
  strncpy_s(g_shm->err, msg ? msg : "error", _TRUNCATE);
}

static uint32_t NoteToLive(uint32_t note_va) {
  uint32_t base = g_shm ? g_shm->module_base : 0;
  if (!base) base = (uint32_t)(uintptr_t)g_game;
  if (!base) base = kDefaultBase;
  if (note_va < kDefaultBase) return 0;
  return base + (note_va - kDefaultBase);
}

static bool PatchCodeByteExact(uint32_t note_va, uint8_t expected,
                               uint8_t replacement) {
  uint32_t live = NoteToLive(note_va);
  if (!live) return false;
  uint8_t* p = (uint8_t*)(uintptr_t)live;
  __try {
    if (*p != expected) return false;
    DWORD old_protect = 0;
    if (!VirtualProtect(p, 1, PAGE_EXECUTE_READWRITE, &old_protect)) return false;
    *p = replacement;
    FlushInstructionCache(GetCurrentProcess(), p, 1);
    DWORD ignored = 0;
    VirtualProtect(p, 1, old_protect, &ignored);
    return true;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
}

// SessionSend gate signature at 0xCC6D4A (9 bytes). Verify exact bytes before
// patching so a changed client build fails closed instead of corrupting code.
static const uint8_t kSessionSendGateSignature[] = {
    0x8B, 0x0E, 0x8B, 0x56, 0x04, 0x3B, 0xCA, 0x73, 0x40};
static const int kSessionSendGateSigLen =
    (int)sizeof(kSessionSendGateSignature);

static bool SessionSendGateSignatureMatches() {
  uint32_t live = NoteToLive(kNoteSessionSendGate - 7);
  if (!live) return false;
  const uint8_t* p = (const uint8_t*)(uintptr_t)live;
  __try {
    return memcmp(p, kSessionSendGateSignature,
                  kSessionSendGateSigLen) == 0;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
}

// Patch 0xCC6D51 jae(0x73)->jmp(0xEB); offset byte 0x40 stays the same.
static bool ApplySessionSendBypass() {
  uint32_t live = NoteToLive(kNoteSessionSendGate);
  if (!live || !SessionSendGateSignatureMatches()) return false;
  uint8_t* p = (uint8_t*)(uintptr_t)live;
  __try {
    DWORD old_protect = 0;
    if (!VirtualProtect(p, 1, PAGE_EXECUTE_READWRITE, &old_protect))
      return false;
    *p = 0xEB;
    FlushInstructionCache(GetCurrentProcess(), p, 1);
    DWORD ignored = 0;
    VirtualProtect(p, 1, old_protect, &ignored);
    InterlockedIncrement(&g_session_send_bypass_hits);
    return true;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
}

static bool RestoreSessionSendBypass() {
  uint32_t live = NoteToLive(kNoteSessionSendGate);
  if (!live) return false;
  uint8_t* p = (uint8_t*)(uintptr_t)live;
  __try {
    DWORD old_protect = 0;
    if (!VirtualProtect(p, 1, PAGE_EXECUTE_READWRITE, &old_protect))
      return false;
    *p = 0x73;
    FlushInstructionCache(GetCurrentProcess(), p, 1);
    DWORD ignored = 0;
    VirtualProtect(p, 1, old_protect, &ignored);
    return true;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
}

static bool IsKnownFixedRvaBuild() {  if (!g_game) return false;
  __try {
    const uint8_t* base = (const uint8_t*)g_game;
    const IMAGE_DOS_HEADER* dos = (const IMAGE_DOS_HEADER*)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew <= 0) return false;
    const IMAGE_NT_HEADERS32* nt =
        (const IMAGE_NT_HEADERS32*)(base + dos->e_lfanew);
    return nt->Signature == IMAGE_NT_SIGNATURE &&
           nt->FileHeader.Machine == kKnownMachine &&
           nt->FileHeader.TimeDateStamp == kKnownTimestamp &&
           nt->OptionalHeader.Magic == IMAGE_NT_OPTIONAL_HDR32_MAGIC &&
           nt->OptionalHeader.SizeOfImage == kKnownImageSize;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
}

static bool CommandRequiresFixedRva(uint32_t cmd) {
  return cmd == CMD_CHOICE_OBJECT || cmd == CMD_PICKUP_NOTE ||
         cmd == CMD_AUTO_CLICK_MATTER || cmd == CMD_AUTO_CLICK_DYN_MATTER ||
         cmd == CMD_CAPTURE_SCREEN || cmd == CMD_AQ_SUBMIT ||
         cmd == CMD_TASK_ACCEPT || cmd == CMD_TASK_COMPLETE ||
         cmd == CMD_INSTANCE_ENTER || cmd == CMD_CHANGE_LINE ||
         cmd == CMD_TEAM_INVITE || cmd == CMD_TEAM_FOLLOW ||
         cmd == CMD_QUICK_TEAM_FOLLOW ||
         cmd == CMD_OBJECT_SCAN ||
         cmd == CMD_NPC_TALK_SELECT ||
         cmd == CMD_NPC_HOST_SELECT ||
         cmd == CMD_HOST_CONTEXT ||
         cmd == CMD_HOST_SNAPSHOT ||
         cmd == CMD_AUTOPLAY_STOP ||
         cmd == CMD_ITEM_USE_TRACE ||
         cmd == CMD_DUMMY_DAMAGE_STAT ||
         cmd == CMD_SKILL_ACTION_TRACE ||
         cmd == CMD_AUTOPLAY_START_BYPASS ||
         cmd == CMD_AUTOPLAY_SEED_FOLLOW ||
         cmd == CMD_CG_SKIP ||
         cmd == CMD_SKILL_INTERRUPT_67 ||
         cmd == CMD_SESSION_SEND_BYPASS ||
         cmd == CMD_COMBAT_MONITOR ||
         cmd == CMD_TARGET_SUBMIT_TRACE ||
         cmd == CMD_DUNGEON_TARGET_RULES ||
         cmd == CMD_AUTOPLAY_DRIVE_ATTACK ||
         cmd == CMD_JIANGLONG_RUNTIME_RESOLVE ||
         cmd == CMD_YOUFENG_INTERNAL_CHAIN ||
         cmd == CMD_YOUFENG_NEXT_GATE ||
         cmd == CMD_YOUFENG_MASH_CAST ||
         cmd == CMD_YOUFENG_FRESH_GATE ||
         cmd == CMD_YOUFENG_EXACT_CANCEL_GATE ||
         cmd == CMD_YOUFENG_PHASE_GATE ||
         cmd == CMD_YOUFENG_QINGGONG_GATE ||
         cmd == CMD_SKILL_ACTION_EXPERIMENT ||
         cmd == CMD_CANCEL_SESSION ||
         cmd == CMD_ONSKILL_STOPPED ||
         cmd == CMD_CAST_SKILL ||
         cmd == CMD_KEY_FORCE || cmd == CMD_KEY_HOLD ||
         cmd == CMD_DUMP_UI_TEXT;
}

static FARPROC GetExport(const char* name) {
  if (!g_game) return nullptr;
  return GetProcAddress(g_game, name);
}

// plg::SetTarget(bool __cdecl(int64))
typedef int(__cdecl* FnSetTarget)(uint32_t lo, uint32_t hi);
typedef void*(__cdecl* FnGetGameUIDlg)(const char* name);
typedef int(__cdecl* FnGetObjectCount)(int class_id);
typedef int(__cdecl* FnGetObjects)(int class_id, void** objects, uint32_t capacity);
typedef const wchar_t*(__cdecl* FnGetObjectName)(void* object);
typedef int(__cdecl* FnGetObjectTemplateId)(void* object);
typedef long long(__cdecl* FnGetObjectId)(void* object);
// plg::PickItem(void __cdecl(int64))
typedef void(__cdecl* FnPickItem)(uint32_t lo, uint32_t hi);
// plg::UseItemInPackage(bool __cdecl(int pack, int slot))
typedef int(__cdecl* FnUseItemInPackage)(int pack, int slot);
// plg::HostMoveToScenePosition(int,float,float,float)
typedef int(__cdecl* FnHostMove)(int mode, float x, float y, float z);
// plg::GetCurrentScenePosition(int*,float*,float*,float*)
typedef void(__cdecl* FnGetScenePosition)(int* scene, float* x, float* y,
                                           float* z);
// plg::GetObjectTargetID(void*) -> int64 (object's internal target/task id).
typedef unsigned __int64(__cdecl* FnObjectTargetId)(void* obj);
// Live cast outer: int __cdecl(skill*, a1=-1, a2=0, a3=0) @ 0x53E110
typedef int(__cdecl* FnCastOuter)(void* skill, uint32_t a1, uint32_t a2,
                                  uint32_t a3);
// Pkg side: void* __cdecl() @ 0x4AE420; skill mgr = *[side+0x24]
typedef void*(__cdecl* FnGetPkgSide)(void);
typedef void*(__cdecl* FnSkillMetadataByConfig)(uint32_t config_id);
typedef uint8_t*(__thiscall* FnResolveCooldownRecord)(
    void* self, uint32_t config_id, uint32_t* resolved_key);
// Skill by config id: void* __thiscall(mgr, skill_id) ret 4 @ 0x533250
typedef void*(__thiscall* FnSkillById)(void* self, uint32_t skill_id);
// Legacy typedefs (unused after CAST_SKILL rewire)
typedef void*(__cdecl* FnGetSkillSide)();
typedef void*(__cdecl* FnGetSkillBarRoot)();
typedef void*(__thiscall* FnSkillBarLookup)(void* self, uint32_t slot,
                                            uint32_t flag);
// ChoiceObject / pickup: cdecl two dwords (pickup only; Choice is thiscall).
typedef int(__cdecl* FnI64)(uint32_t a, uint32_t b);
// ChoiceObject: bool __thiscall(this, id_lo, id_hi); SEH frame inside.
typedef uint8_t(__thiscall* FnChoiceObject)(void* self, uint32_t id_lo,
                                            uint32_t id_hi);
// [[[g]+0x24]+0x8c] host-side object (no args).
typedef void*(__cdecl* FnGetHostSide)(void);
typedef uint8_t(__thiscall* FnCancelSession)(void* self, uint32_t clear_buff,
                                             uint32_t id_perform);
typedef uint8_t(__thiscall* FnActionPhasePacket)(void* self,
                                                 uint32_t config_id);
typedef void(__thiscall* FnOnSkillStopped)(void* self, void* out_struct,
                                            uint32_t param1, uint32_t param2,
                                            uint32_t param3, uint32_t param4);
typedef int(__thiscall* FnActionCast)(
    void* self, uint32_t a01, void* event_ptr, uint32_t phase, uint32_t a04,
    uint32_t a05, void* action_context, uint32_t a07, uint32_t a08,
    uint32_t a09, uint32_t a10, uint32_t a11, uint32_t a12, uint32_t a13,
    uint32_t a14);
// bool __thiscall MatterInteract(this, id_lo, id_hi, tid); callee cleans 0xC.
typedef uint8_t(__thiscall* FnMatterInteract)(void* self, uint32_t id_lo,
                                              uint32_t id_hi, uint32_t tid);
// void __cdecl CaptureScreen worker (no args; SEH frame inside).
typedef void(__cdecl* FnCaptureScreen)(void);
// ActivityQuestion Btn_Ok submit (thiscall, one stack arg, ret 4).
typedef void(__thiscall* FnAqSubmit)(void* self, uint32_t arg0);
// CECAutoPlay::StopAutoPlay(this, reason_u8); callee ret 4.
typedef int(__thiscall* FnStopAutoplay)(void* self, uint32_t reason);
typedef void(__thiscall* FnAutoPlayFrameBtnStart)(void* self,
                                                  uint32_t unused);
typedef void(__thiscall* FnAutoPlayRefreshTarget)(void* snapshot);
typedef void(__thiscall* FnAutoPlaySetState)(void* manager, uint32_t state);
typedef void(__thiscall* FnAutoPlayEnterFollow)(void* state,
                                                void* previous_state);
// Instance enter: void __thiscall(this, host_lo, host_hi, inst_id, mode, flag); ret 0x14.
typedef uint8_t(__thiscall* FnTeamInvite)(void* self, uint32_t id_lo,
                                          uint32_t id_hi);
// Win_TeamFollow callbacks: ret 4; the passed command pointer is ignored.
typedef uint32_t(__stdcall* FnTeamFollowUi)(void* command);
typedef uint32_t(__thiscall* FnQuickTeamFollow)(void* command, uint32_t arg0);
typedef void(__thiscall* FnInstanceEnter)(void* self, uint32_t host_lo,
                                          uint32_t host_hi, uint32_t inst_id,
                                          uint32_t mode, uint32_t flag);
// Direct 0x58 packet builder (cdecl 5 args; uses GetNetRoot for send).
typedef void(__cdecl* FnInstanceEnterPkt)(uint32_t host_lo, uint32_t host_hi,
                                          uint32_t inst_id, uint32_t mode,
                                          uint32_t flag);
// Change line: void __cdecl(uint8_t line_id); builds opcode 0x7A packet.
typedef void(__cdecl* FnChangeLine)(uint32_t line_id);

static void* ResolveNetRoot() {
  // 0x4AE440: return *global + 0x2C (task/net root used by enter this + send).
  uint32_t va = NoteToLive(kNoteGetNetRoot);
  if (!va) return nullptr;
  FnGetHostSide fn = (FnGetHostSide)(uintptr_t)va;
  __try {
    return fn();
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return nullptr;
  }
}

static void* ResolveChoiceThis() {
  // 0x4AE470: *((*global+0x24)+8)+0x324 — ChoiceObject this.
  uint32_t va = NoteToLive(kNoteGetChoiceThis);
  if (!va) return nullptr;
  FnGetHostSide fn = (FnGetHostSide)(uintptr_t)va;
  __try {
    return fn();
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return nullptr;
  }
}

static void* ResolveTaskMgrThis() {
  // *NoteToLive(global) -> root; this = *(root + 0x2C)
  uint32_t gva = NoteToLive(kNoteTaskGameRootGlobal);
  if (!gva) return nullptr;
  __try {
    uint32_t root = *(uint32_t*)(uintptr_t)gva;
    if (!root) return nullptr;
    uint32_t th = *(uint32_t*)(uintptr_t)(root + kTaskMgrThisOff);
    return (void*)(uintptr_t)th;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return nullptr;
  }
}

// CECInventory::UseItem(this, pack, slot, flag) @ 0x74BF50. The game calls
// it through CheckHPItem as well as manual/background item paths. __fastcall
// preserves the original thiscall layout (ECX=this, 3 stack args, ret 12).
typedef uint32_t(__fastcall* FnNativeUseItem)(void* self, void* edx,
                                              uint32_t pack, uint32_t slot,
                                              uint32_t flag);

static uint32_t __fastcall Hook_NativeUseItem(void* self, void* edx,
                                              uint32_t pack, uint32_t slot,
                                              uint32_t flag) {
  FnNativeUseItem original =
      (FnNativeUseItem)(g_hk_item_use.trampoline);
  uint32_t ret = 0;
  if (original) ret = original(self, edx, pack, slot, flag);
  if (InterlockedCompareExchange(&g_item_trace_on, 0, 0) &&
      (int32_t)pack == InterlockedCompareExchange(&g_item_trace_pack, 0, 0) &&
      (int32_t)slot == InterlockedCompareExchange(&g_item_trace_slot, 0, 0)) {
    InterlockedIncrement(&g_item_trace_attempts);
    if (ret & 0xFFu) InterlockedIncrement(&g_item_trace_successes);
  }
  return ret;
}

static bool InstallItemUseTraceHook() {
  if (g_hk_item_use.active) return true;
  uint32_t va = NoteToLive(0x0074BF50u);
  uint8_t* target = (uint8_t*)(uintptr_t)va;
  __try {
    // Prolog: push -1; push handler (2+5 bytes). Steal complete instructions.
    if (!target || target[0] != 0x6A || target[1] != 0xFF || target[2] != 0x68) {
      return false;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  return InstallInlineDetour(&g_hk_item_use, target, 7,
                             (void*)&Hook_NativeUseItem);
}

static void StopItemUseTraceHook() {
  InterlockedExchange(&g_item_trace_on, 0);
  // Keep the passive detour installed for process lifetime. Hot unpatching can
  // tear instructions while another game thread is executing this function.
}

static void FormatItemUseTrace(char* buf, size_t n) {
  if (!buf || !n) return;
  _snprintf_s(buf, n, _TRUNCATE,
      "ITEM_USE_TRACE pack=%ld slot=%ld attempts=%ld success=%ld armed=%d",
      InterlockedCompareExchange(&g_item_trace_pack, 0, 0),
      InterlockedCompareExchange(&g_item_trace_slot, 0, 0),
      InterlockedCompareExchange(&g_item_trace_attempts, 0, 0),
      InterlockedCompareExchange(&g_item_trace_successes, 0, 0),
      g_hk_item_use.active ? 1 : 0);
}

// 0x7493B0 is a thiscall: ecx=target host/controller, stack[0]=id64 pair.
// __fastcall accepts the same ECX plus an unused EDX register and preserves
// the one stack argument, so forwarding to the trampoline restores the exact
// original ABI. The native function returns a byte (0=reject, non-zero=accept)
// at the manual-selection caller, so preserve that return value here.
typedef uint8_t(__thiscall* FnTargetSubmit)(void* self,
                                            const uint32_t* target_pair);

static bool IsTargetGuardPtr(uint32_t value) {
  return value >= 0x00010000u && value < 0xFFFF0000u;
}

static bool IsConfiguredDungeonTargetTid(uint32_t tid) {
  LONG count = InterlockedCompareExchange(&g_dungeon_target_tid_count, 0, 0);
  if (count < 0) return false;
  if ((uint32_t)count > kMaxDungeonTargetTids)
    count = (LONG)kMaxDungeonTargetTids;
  for (LONG i = 0; i < count; ++i) {
    if (g_dungeon_target_tids[i] == tid) return true;
  }
  return false;
}

static bool AddDungeonTargetTid(uint32_t tid) {
  if (!tid) return false;
  LONG count = InterlockedCompareExchange(&g_dungeon_target_tid_count, 0, 0);
  if (count < 0 || (uint32_t)count > kMaxDungeonTargetTids) return false;
  for (LONG i = 0; i < count; ++i) {
    if (g_dungeon_target_tids[i] == tid) return true;
  }
  if ((uint32_t)count >= kMaxDungeonTargetTids) return false;
  g_dungeon_target_tids[count] = tid;
  MemoryBarrier();
  InterlockedExchange(&g_dungeon_target_tid_count, count + 1);
  return true;
}

static void ClearDungeonTargetTids() {
  // Publish an empty set before clearing payload so the hook cannot consume a
  // partially rewritten list if configuration is changed while it is armed.
  InterlockedExchange(&g_dungeon_target_tid_count, 0);
  MemoryBarrier();
  memset(g_dungeon_target_tids, 0, sizeof(g_dungeon_target_tids));
}

// Candidate-side guard. Metadata comes from the live AOI hash table and object
// fields; no client getter, CRT call, bridge callback, or target mutation is
// used. Callers decide which automatic route is eligible for this predicate.
static bool IsRejectedDungeonTargetCandidate(const uint32_t* target_pair) {
  if (!target_pair) return false;

  __try {
    const uint32_t target_lo = target_pair[0];
    const uint32_t target_hi = target_pair[1];
    if (!target_lo && !target_hi) return false;
    const uint32_t root_global = NoteToLive(kNoteTaskGameRootGlobal);
    const uint32_t root = *(uint32_t*)(uintptr_t)root_global;
    if (!IsTargetGuardPtr(root)) return false;
    const uint32_t mid = *(uint32_t*)(uintptr_t)(root + 0x24u);
    if (!IsTargetGuardPtr(mid)) return false;
    const uint32_t host = *(uint32_t*)(uintptr_t)(mid + 0x8Cu);
    if (!IsTargetGuardPtr(host)) return false;
    const float hx = *(float*)(uintptr_t)(host + 0x158u);
    const float hy = *(float*)(uintptr_t)(host + 0x15Cu);
    const float hz = *(float*)(uintptr_t)(host + 0x160u);
    if (!_finite(hx) || !_finite(hy) || !_finite(hz) ||
        hx < -500000.0f || hx > 500000.0f ||
        hy < -500000.0f || hy > 500000.0f ||
        hz < -500000.0f || hz > 500000.0f)
      return false;

    const uint32_t scene = *(uint32_t*)(uintptr_t)(mid + 0x0Cu);
    if (!IsTargetGuardPtr(scene)) return false;
    const uint32_t manager = *(uint32_t*)(uintptr_t)(scene + 0x74u);
    if (!IsTargetGuardPtr(manager)) return false;
    const uint32_t count = *(uint32_t*)(uintptr_t)(manager + 0x18u);
    const uint32_t buckets = *(uint32_t*)(uintptr_t)(manager + 0x1Cu);
    const uint32_t bucket_count = *(uint32_t*)(uintptr_t)(manager + 0x28u);
    if (count == 0 || count > 512u || !IsTargetGuardPtr(buckets) ||
        bucket_count == 0 || bucket_count > 4096u)
      return false;

    const uint32_t node_limit = count + 32u;
    uint32_t visited = 0;
    for (uint32_t i = 0; i < bucket_count; ++i) {
      uint32_t node = *(uint32_t*)(uintptr_t)(buckets + i * 4u);
      while (IsTargetGuardPtr(node) && visited < node_limit) {
        ++visited;
        const uint32_t next = *(uint32_t*)(uintptr_t)node;
        const uint32_t obj = *(uint32_t*)(uintptr_t)(node + 4u);
        node = next;
        if (!IsTargetGuardPtr(obj) ||
            *(uint32_t*)(uintptr_t)obj != 0x01260AF4u)
          continue;
        const uint32_t oid_lo = *(uint32_t*)(uintptr_t)(obj + 0x140u);
        const uint32_t oid_hi = *(uint32_t*)(uintptr_t)(obj + 0x144u);
        if (oid_lo != target_lo || oid_hi != target_hi) continue;
        const uint32_t tid = *(uint32_t*)(uintptr_t)(obj + 0x4F8u);
        // The assistant configures the deny set once before autoplay starts.
        // Unknown TIDs fail open; no game API is used to make this decision.
        if (!IsConfiguredDungeonTargetTid(tid))
          return false;
        // Both fields are dynamic per-object liveness state, not list data.
        if (*(uint32_t*)(uintptr_t)(obj + 0x2F8u) == 0 ||
            *(uint32_t*)(uintptr_t)(obj + 0x2FCu) == 0xFFFFFFFFu)
          return false;
        const float ox = *(float*)(uintptr_t)(obj + 0x158u);
        const float oy = *(float*)(uintptr_t)(obj + 0x15Cu);
        const float oz = *(float*)(uintptr_t)(obj + 0x160u);
        if (!_finite(ox) || !_finite(oy) || !_finite(oz) ||
            ox < -500000.0f || ox > 500000.0f ||
            oy < -500000.0f || oy > 500000.0f ||
            oz < -500000.0f || oz > 500000.0f)
          return false;
        const float dx = ox - hx;
        const float dz = oz - hz;
        // Dungeon reachability and the rest of the assistant use horizontal
        // XZ distance. Y is floor/terrain height and must not make a target
        // that is beside the player look like a distant candidate.
        return (dx * dx + dz * dz) > (30.0f * 30.0f);
      }
      if (visited >= node_limit) break;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    // A scene/object replacement race must fail open; never fault the game.
    return false;
  }
  return false;
}

static bool ShouldRejectDungeonAutoSubmit(uint32_t caller,
                                          const uint32_t* target_pair) {
  return caller == NoteToLive(kNoteTargetSubmitDungeonAutoCaller) &&
         IsRejectedDungeonTargetCandidate(target_pair);
}

static void StoreTargetSubmitTrace(const TargetSubmitTraceEntry* src) {
  if (!src || !InterlockedCompareExchange(&g_target_submit_trace_on, 0, 0))
    return;
  const LONG seq = InterlockedIncrement(&g_target_submit_trace_count);
  if (seq <= 0) return;
  TargetSubmitTraceEntry e = *src;
  e.seq = (uint32_t)seq;
  const LONG index = (seq - 1) & (kMaxTargetSubmitTrace - 1);
  // Publish seq last. Readers reject a slot if a concurrent writer has not
  // finished its payload yet.
  g_target_submit_trace[index].seq = 0;
  g_target_submit_trace[index].tick = e.tick;
  g_target_submit_trace[index].thread_id = e.thread_id;
  g_target_submit_trace[index].caller = e.caller;
  g_target_submit_trace[index].self_ptr = e.self_ptr;
  g_target_submit_trace[index].target_lo = e.target_lo;
  g_target_submit_trace[index].target_hi = e.target_hi;
  g_target_submit_trace[index].route = e.route;
  MemoryBarrier();
  g_target_submit_trace[index].seq = e.seq;
}

// A map/scene load can install the distant target before the assistant gets a
// chance to arm the guard. Clean that one stale seed at arm time only. This is
// direct in-process memory state, not SetTarget, a client call, or a poll.
static bool SanitizeArmedDungeonTarget() {
  __try {
    const uint32_t root_global = NoteToLive(kNoteTaskGameRootGlobal);
    const uint32_t root = *(uint32_t*)(uintptr_t)root_global;
    if (!IsTargetGuardPtr(root)) return false;
    const uint32_t mid = *(uint32_t*)(uintptr_t)(root + 0x24u);
    if (!IsTargetGuardPtr(mid)) return false;
    uint8_t* host = *(uint8_t**)(uintptr_t)(mid + 0x8Cu);
    if (!IsTargetGuardPtr((uint32_t)(uintptr_t)host)) return false;
    const uint32_t target_pair[2] = {
        *(uint32_t*)(host + 0x19E8u),
        *(uint32_t*)(host + 0x19ECu),
    };
    if (!IsRejectedDungeonTargetCandidate(target_pair)) return false;
    // Do not clear a target that another game thread replaced while AOI was
    // being inspected. Clear both current and an identical queued seed.
    if (*(uint32_t*)(host + 0x19E8u) != target_pair[0] ||
        *(uint32_t*)(host + 0x19ECu) != target_pair[1])
      return false;
    *(uint32_t*)(host + 0x19E8u) = 0;
    *(uint32_t*)(host + 0x19ECu) = 0;
    if (*(uint32_t*)(host + 0x3C8u) == target_pair[0] &&
        *(uint32_t*)(host + 0x3CCu) == target_pair[1]) {
      *(uint32_t*)(host + 0x3C8u) = 0;
      *(uint32_t*)(host + 0x3CCu) = 0;
    }
    MemoryBarrier();
    TargetSubmitTraceEntry e = {};
    e.tick = GetTickCount();
    e.thread_id = GetCurrentThreadId();
    e.self_ptr = (uint32_t)(uintptr_t)host;
    e.target_lo = target_pair[0];
    e.target_hi = target_pair[1];
    e.route = 5;
    StoreTargetSubmitTrace(&e);
    EmitTargetTraceDebug(&e, true);
    InterlockedIncrement(&g_target_arm_sanitize_hits);
    return true;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
}

static uint8_t __fastcall Hook_TargetSubmitTrace(void* self, void* /*edx*/,
                                                  const uint32_t* target_pair) {
  TargetSubmitTraceEntry e = {};
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.caller = (uint32_t)(uintptr_t)_ReturnAddress();
  e.self_ptr = (uint32_t)(uintptr_t)self;
  e.route = 1;
  __try {
    if (target_pair) {
      e.target_lo = target_pair[0];
      e.target_hi = target_pair[1];
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    e.target_lo = 0;
    e.target_hi = 0;
  }
  StoreTargetSubmitTrace(&e);
  const bool guard_on =
      InterlockedCompareExchange(&g_target_submit_guard_on, 0, 0) != 0;
  const bool skipped =
      guard_on && ShouldRejectDungeonAutoSubmit(e.caller, target_pair);
  EmitTargetTraceDebug(&e, skipped);
  if (skipped) {
    InterlockedIncrement(&g_target_submit_guard_hits);
    return 0;
  }
  FnTargetSubmit original =
      (FnTargetSubmit)g_hk_target_submit_trace.trampoline;
  return original ? original(self, target_pair) : 0;
}

static bool InstallTargetSubmitTraceHook() {
  if (g_hk_target_submit_trace.active) return true;
  uint8_t* target =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteTargetSubmit);
  static const uint8_t kExpected[16] = {
      0x56, 0x8B, 0x74, 0x24, 0x08, 0x8B, 0x06, 0x8B,
      0x56, 0x04, 0x57, 0x8B, 0xF9, 0x8B, 0xC8, 0x0B,
  };
  __try {
    if (!target || memcmp(target, kExpected, sizeof(kExpected)) != 0)
      return false;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  // push esi; mov esi,[esp+8]; mov eax,[esi] are complete instructions.
  return InstallInlineDetour(&g_hk_target_submit_trace, target, 7,
                             (void*)&Hook_TargetSubmitTrace);
}

// 0x73E300 is a thiscall with one stack argument. It promotes the queued
// candidate at [self+0x0C]+0x3C8/+0x3CC into the current selected target at
// +0x19E8/+0x19EC, bypassing 0x7493B0 completely. Returning success after
// clearing only a rejected queued candidate preserves the caller's expected
// result while preventing a stale distant id from being promoted every tick.
typedef uint8_t(__thiscall* FnTargetQueuePromote)(void* self,
                                                  uint32_t reason);

static uint8_t __fastcall Hook_TargetQueuePromote(void* self, void* /*edx*/,
                                                   uint32_t reason) {
  TargetSubmitTraceEntry e = {};
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.caller = (uint32_t)(uintptr_t)_ReturnAddress();
  e.self_ptr = (uint32_t)(uintptr_t)self;
  e.route = 2;
  uint8_t* controller = nullptr;
  __try {
    if (self) {
      controller = *(uint8_t**)((uint8_t*)self + 0x0Cu);
      if (IsTargetGuardPtr((uint32_t)(uintptr_t)controller)) {
        e.target_lo = *(uint32_t*)(controller + 0x3C8u);
        e.target_hi = *(uint32_t*)(controller + 0x3CCu);
      }
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    controller = nullptr;
    e.target_lo = 0;
    e.target_hi = 0;
  }
  StoreTargetSubmitTrace(&e);
  // 0x7E2780 can replace this cache after the entry snapshot. The actual
  // rejection is therefore performed by Hook_TargetQueueRefresh after that
  // callback returns, immediately before 0x73E32A consumes the cache.
  EmitTargetTraceDebug(&e, false);
  FnTargetQueuePromote original =
      (FnTargetQueuePromote)g_hk_target_queue_promote.trampoline;
  return original ? original(self, reason) : 0;
}

static bool InstallTargetQueuePromoteHook() {
  if (g_hk_target_queue_promote.active) return true;
  uint8_t* target =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteTargetQueuePromote);
  static const uint8_t kExpected[7] = {
      0x83, 0xEC, 0x64, 0x53, 0x56, 0x8B, 0xF1,
  };
  __try {
    if (!target || memcmp(target, kExpected, sizeof(kExpected)) != 0)
      return false;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  return InstallInlineDetour(&g_hk_target_queue_promote, target, 7,
                             (void*)&Hook_TargetQueuePromote);
}

// 0x7E2780 is a thiscall with one stack event argument. For its exact
// 0x73E300 caller, the original callback can refresh controller+0x3C8/+0x3CC.
// Read it after forwarding, while execution is still before the subsequent
// 0x73E32A current-target write.
typedef uint32_t(__thiscall* FnTargetQueueRefresh)(void* self,
                                                   const void* event);

static uint32_t __fastcall Hook_TargetQueueRefresh(void* self, void* /*edx*/,
                                                    const void* event) {
  FnTargetQueueRefresh original =
      (FnTargetQueueRefresh)g_hk_target_queue_refresh.trampoline;
  const uint32_t ret = original ? original(self, event) : 0;
  const uint32_t caller = (uint32_t)(uintptr_t)_ReturnAddress();
  if (caller != NoteToLive(kNoteTargetQueueRefreshCaller)) return ret;

  TargetSubmitTraceEntry e = {};
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.caller = caller;
  e.self_ptr = (uint32_t)(uintptr_t)self;
  e.route = 4;
  uint8_t* controller = nullptr;
  __try {
    if (self) {
      controller = *(uint8_t**)((uint8_t*)self + 0x0Cu);
      if (IsTargetGuardPtr((uint32_t)(uintptr_t)controller)) {
        e.target_lo = *(uint32_t*)(controller + 0x3C8u);
        e.target_hi = *(uint32_t*)(controller + 0x3CCu);
      }
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    controller = nullptr;
    e.target_lo = 0;
    e.target_hi = 0;
  }
  StoreTargetSubmitTrace(&e);
  const uint32_t target_pair[2] = {e.target_lo, e.target_hi};
  const bool skipped =
      InterlockedCompareExchange(&g_target_submit_guard_on, 0, 0) != 0 &&
      controller && IsRejectedDungeonTargetCandidate(target_pair);
  bool blocked = false;
  if (skipped) {
    __try {
      // Keep the refreshed cache empty so 0x73E32A copies the normal no-target
      // pair instead of the distant candidate into current selection.
      if (*(uint32_t*)(controller + 0x3C8u) == e.target_lo &&
          *(uint32_t*)(controller + 0x3CCu) == e.target_hi) {
        *(uint32_t*)(controller + 0x3C8u) = 0;
        *(uint32_t*)(controller + 0x3CCu) = 0;
        MemoryBarrier();
        InterlockedIncrement(&g_target_queue_refresh_guard_hits);
        blocked = true;
      }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      // Fail open if the controller is replaced during the callback return.
    }
  }
  EmitTargetTraceDebug(&e, blocked);
  return ret;
}

static bool InstallTargetQueueRefreshHook() {
  if (g_hk_target_queue_refresh.active) return true;
  uint8_t* target =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteTargetQueueRefresh);
  static const uint8_t kExpected[10] = {
      0x8B, 0x44, 0x24, 0x04, 0x8B,
      0x40, 0x08, 0x8B, 0x49, 0x04,
  };
  __try {
    if (!target || memcmp(target, kExpected, sizeof(kExpected)) != 0)
      return false;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  return InstallInlineDetour(&g_hk_target_queue_refresh, target, 10,
                             (void*)&Hook_TargetQueueRefresh);
}

// 0x730430 is a thiscall with two stack arguments. Its first argument is the
// target-state record whose unaligned +0x31/+0x35 id pair is copied directly
// into both target fields. Reject before forwarding so the object never enters
// either memory-side candidate slot.
typedef uint32_t(__thiscall* FnTargetStateApply)(void* self,
                                                 const uint8_t* state,
                                                 uint32_t flags);

static uint32_t __fastcall Hook_TargetStateApply(void* self, void* /*edx*/,
                                                 const uint8_t* state,
                                                 uint32_t flags) {
  TargetSubmitTraceEntry e = {};
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.caller = (uint32_t)(uintptr_t)_ReturnAddress();
  e.self_ptr = (uint32_t)(uintptr_t)self;
  e.route = 3;
  __try {
    if (IsTargetGuardPtr((uint32_t)(uintptr_t)state)) {
      e.target_lo = *(const uint32_t*)(state + 0x31u);
      e.target_hi = *(const uint32_t*)(state + 0x35u);
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    e.target_lo = 0;
    e.target_hi = 0;
  }
  StoreTargetSubmitTrace(&e);
  const uint32_t target_pair[2] = {e.target_lo, e.target_hi};
  const bool skipped =
      InterlockedCompareExchange(&g_target_submit_guard_on, 0, 0) != 0 &&
      IsRejectedDungeonTargetCandidate(target_pair);
  EmitTargetTraceDebug(&e, skipped);
  if (skipped) {
    InterlockedIncrement(&g_target_state_guard_hits);
    return 0;
  }
  FnTargetStateApply original =
      (FnTargetStateApply)g_hk_target_state_apply.trampoline;
  return original ? original(self, state, flags) : 0;
}

static bool InstallTargetStateApplyHook() {
  if (g_hk_target_state_apply.active) return true;
  uint8_t* target = (uint8_t*)(uintptr_t)NoteToLive(kNoteTargetStateApply);
  static const uint8_t kExpected[14] = {
      0x64, 0xA1, 0x00, 0x00, 0x00, 0x00, 0x6A,
      0xFF, 0x68, 0xB3, 0x2D, 0x12, 0x01, 0x50,
  };
  __try {
    if (!target || memcmp(target, kExpected, sizeof(kExpected)) != 0)
      return false;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  // The stolen SEH prolog has no relative branch and executes unchanged in
  // the trampoline before returning to 0x73043E.
  return InstallInlineDetour(&g_hk_target_state_apply, target, 14,
                             (void*)&Hook_TargetStateApply);
}

// ---- 方案1：跟随快照自引用修正（2026-08-30）----
// 队长身份开挂时，跟随快照(CECRunToTeamLeaderPolicy)的目标会被解析回
// 队长自己（自引用），跟随打怪机器停在 StateAlert。此 hook 在"解析跟随
// 链一跳"(0xC6A2E0) 的结果等于自己时，替换为 seed follow 配置的队员目标。
static const uint32_t kNoteAutoPlayFollowChain = 0x00C6A2E0u;
static uint32_t g_follow_seed_lo = 0;
static uint32_t g_follow_seed_hi = 0;
static volatile LONG g_follow_seed_keep_hits = 0;
static InlineHookRec g_hk_follow_chain = {};
typedef void(__thiscall* FnResolveFollowChain)(void* out);

// 原函数 0xC6A2E0 共 0x58 字节，头部 5 字节为 `push esi; mov esi, ecx;
// call 0xC662E0`——E8 相对调用不可被框架搬迁（trampoline 不复位 rel32），
// 因此 hook 整体替换函数：以下 C 复刻其全部逻辑（三个调用均换绝对地址），
// 末尾做方案1 替换。stolen 的 5 字节被 JMP 覆盖，属死代码不再执行。
static void __fastcall Hook_ResolveFollowChain(void* out, void* /*edx*/) {
  typedef void*(__thiscall* FnCtor)(void*);                // 0xC662E0
  typedef void*(__thiscall* FnStep)(void*);                // 0x5F9010
  typedef void*(__cdecl* FnObjById)(uint32_t, uint32_t);   // 0x4AE4A0
  typedef uint64_t(__thiscall* FnVm80)(void*);

  // 链式解析：Ctor(out)→a；Step(a)→b；[b+0x18/1C]=id64；ObjById→obj；
  // obj->vm80()→目标 id64。每跳返回值是下一跳的 this/输入（2026-08-30 修正：
  // 原复刻把 out 误传给 Step，导致 ACCESS_VIOLATION）。
  uint32_t lo = 0;
  uint32_t hi = 0;
  void* a = ((FnCtor)NoteToLive(0x00C662E0u))(out);
  void* b = a ? ((FnStep)NoteToLive(0x005F9010u))(a) : nullptr;
  if (b) {
    const uint32_t id_lo = *(uint32_t*)((uint8_t*)b + 0x18);
    const uint32_t id_hi = *(uint32_t*)((uint8_t*)b + 0x1C);
    void* obj = ((FnObjById)NoteToLive(0x004AE4A0u))(id_lo, id_hi);
    if (obj) {
      void* vtbl = *(void**)obj;
      FnVm80 vm = (FnVm80)(*(void**)((uint8_t*)vtbl + 0x80));
      const uint64_t rid = vm(obj);
      lo = (uint32_t)(rid & 0xFFFFFFFFu);
      hi = (uint32_t)((rid >> 32) & 0xFFFFFFFFu);
    }
  }
  // ---- 方案1：解析结果==自己 且 有 seed 队员 → 替换 ----
  __try {
    if ((lo | hi)) {
      FnGetHostSide get_host =
          (FnGetHostSide)(uintptr_t)NoteToLive(kNoteGetHostSide);
      uint8_t* host = get_host ? (uint8_t*)get_host() : nullptr;
      if (host) {
        const uint32_t self_lo = *(uint32_t*)(host + kHostPlayerIdLoOff);
        const uint32_t self_hi = *(uint32_t*)(host + kHostPlayerIdLoOff + 4u);
        if (lo == self_lo && hi == self_hi &&
            (g_follow_seed_lo | g_follow_seed_hi)) {
          lo = g_follow_seed_lo;
          hi = g_follow_seed_hi;
          InterlockedIncrement(&g_follow_seed_keep_hits);
        }
      }
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  *(uint32_t*)out = lo;
  *(uint32_t*)((uint8_t*)out + 4) = hi;
}

static bool InstallFollowChainHook() {
  if (g_hk_follow_chain.active) return true;
  uint8_t* target = (uint8_t*)(uintptr_t)NoteToLive(kNoteAutoPlayFollowChain);
  // 头 5 字节：56 8B F1 E8 <rel32>（push esi; mov esi, ecx; call 0xC662E0）
  static const uint8_t kExpected[4] = {0x56, 0x8B, 0xF1, 0xE8};
  __try {
    if (!target || memcmp(target, kExpected, sizeof(kExpected)) != 0)
      return false;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  return InstallInlineDetour(&g_hk_follow_chain, target, 5,
                             (void*)&Hook_ResolveFollowChain);
}

static bool ReadLatestTargetSubmitTrace(TargetSubmitTraceEntry* out) {
  if (!out) return false;
  const LONG total =
      InterlockedCompareExchange(&g_target_submit_trace_count, 0, 0);
  if (total <= 0) return false;
  const uint32_t want = (uint32_t)total;
  const TargetSubmitTraceEntry e =
      g_target_submit_trace[(want - 1u) & (kMaxTargetSubmitTrace - 1)];
  if (e.seq != want) return false;
  *out = e;
  return true;
}

static bool DumpTargetSubmitTrace(char* path_out, size_t path_n) {
  if (path_out && path_n) path_out[0] = 0;
  char dir[MAX_PATH] = {0};
  if (!ResolveSoftwareLogsDir(dir, sizeof(dir))) return false;
  SYSTEMTIME st = {};
  GetLocalTime(&st);
  char path[MAX_PATH] = {0};
  _snprintf_s(path, _TRUNCATE,
              "%starget_submit_trace_%04u%02u%02u_%02u%02u%02u_%lu.tsv",
              dir, (unsigned)st.wYear, (unsigned)st.wMonth,
              (unsigned)st.wDay, (unsigned)st.wHour, (unsigned)st.wMinute,
              (unsigned)st.wSecond, (unsigned long)GetCurrentProcessId());
  FILE* f = nullptr;
  if (fopen_s(&f, path, "wb") != 0 || !f) return false;
  fprintf(f,
          "seq\ttick\tthread\tcaller\tself\ttarget_lo\ttarget_hi\troute\n");
  const LONG total =
      InterlockedCompareExchange(&g_target_submit_trace_count, 0, 0);
  const LONG first = total > kMaxTargetSubmitTrace
                         ? total - kMaxTargetSubmitTrace + 1
                         : 1;
  for (LONG seq = first; seq <= total; ++seq) {
    const TargetSubmitTraceEntry e =
        g_target_submit_trace[((uint32_t)seq - 1u) &
                              (kMaxTargetSubmitTrace - 1)];
    if (e.seq != (uint32_t)seq) continue;
    fprintf(f, "%u\t%u\t%u\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t%u\n",
            e.seq, e.tick, e.thread_id, e.caller, e.self_ptr, e.target_lo,
            e.target_hi, e.route);
  }
  fclose(f);
  if (path_out && path_n)
    _snprintf_s(path_out, path_n, _TRUNCATE, "%s", path);
  return true;
}

static void FormatTargetSubmitTrace(char* buf, size_t n) {
  if (!buf || !n) return;
  const LONG total =
      InterlockedCompareExchange(&g_target_submit_trace_count, 0, 0);
  TargetSubmitTraceEntry e = {};
  const bool have_latest = ReadLatestTargetSubmitTrace(&e);
  _snprintf_s(buf, n, _TRUNCATE,
              "TARGET_SUBMIT_TRACE on=%d submit=%d queue=%d refresh=%d state=%d n=%ld kept=%d latest=%s route=%u id=%08X:%08X caller=%08X thread=%u",
              InterlockedCompareExchange(&g_target_submit_trace_on, 0, 0)
                  ? 1
                  : 0,
              g_hk_target_submit_trace.active ? 1 : 0,
              g_hk_target_queue_promote.active ? 1 : 0,
              g_hk_target_queue_refresh.active ? 1 : 0,
              g_hk_target_state_apply.active ? 1 : 0, total,
              total > kMaxTargetSubmitTrace ? kMaxTargetSubmitTrace : total,
              have_latest ? "yes" : "none", e.route, e.target_hi, e.target_lo,
              e.caller, e.thread_id);
}

// 0x79BA80 is a thiscall with three stack arguments. Its true return means a
// single split entry was settled. Reading the entry before forwarding avoids
// racing the function's post-settlement index advance.
typedef uint8_t(__thiscall* FnSplitDamageProcess)(
    void* self, uint32_t split_group, uint32_t played_delta,
    void* damage_desc);

static uint8_t __fastcall Hook_SplitDamageProcess(
    void* self, void* /*edx*/, uint32_t split_group, uint32_t played_delta,
    void* damage_desc) {
  FnSplitDamageProcess original =
      (FnSplitDamageProcess)g_hk_dummy_damage.trampoline;
  const bool dummy_active =
      InterlockedCompareExchange(&g_dummy_damage_on, 0, 0);
  const bool combat_active =
      InterlockedCompareExchange(&g_combat_monitor_on, 0, 0);
  bool target_matches = false;
  bool combat_self = false;
  int32_t damage = 0;
  uint32_t target_lo = 0;
  uint32_t target_hi = 0;
  __try {
    if ((dummy_active || combat_active) && self && damage_desc) {
      uint8_t* owner = (uint8_t*)self;
      target_lo = *(uint32_t*)(owner + 0x10u);
      target_hi = *(uint32_t*)(owner + 0x14u);
      if (dummy_active) {
        target_matches =
            target_lo == (uint32_t)InterlockedCompareExchange(
                             &g_dummy_damage_target_lo, 0, 0) &&
            target_hi == (uint32_t)InterlockedCompareExchange(
                             &g_dummy_damage_target_hi, 0, 0);
      }
      if (combat_active) {
        // Self-source filter: only while the host has a live attack target
        // does the damage splitter's target match the host's target. The host
        // target is one atomic 64-bit value (no torn lo/hi pair).
        const LONG64 ht = InterlockedCompareExchange64(
            &g_combat_host_target, 0, 0);
        const uint32_t ht_lo = (uint32_t)ht;
        const uint32_t ht_hi = (uint32_t)((uint64_t)ht >> 32);
        combat_self =
            (ht_lo || ht_hi) && target_lo == ht_lo && target_hi == ht_hi;
      }
      if (target_matches || combat_self) {
        uint8_t* desc = (uint8_t*)damage_desc;
        const int32_t index = (int8_t)desc[0xA0u];
        const int32_t count = (uint8_t)desc[0xB4u];
        uint8_t* entries = *(uint8_t**)(desc + 0xA8u);
        if (entries && index >= 0 && index < count) {
          damage = *(int32_t*)(entries + (size_t)index * 24u);
        }
      }
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    target_matches = false;
    combat_self = false;
    damage = 0;
  }

  const uint8_t settled = original
      ? original(self, split_group, played_delta, damage_desc)
      : 0u;
  if (settled && combat_active && combat_self && damage > 0) {
    InterlockedExchangeAdd64(&g_combat_self_damage_total, (LONG64)damage);
    InterlockedIncrement(&g_combat_self_damage_seq);
  }
  if (!settled || !target_matches || damage <= 0 || !dummy_active)
    return settled;

  const DWORD now = GetTickCount();
  DWORD started = (DWORD)InterlockedCompareExchange(
      &g_dummy_damage_started_tick, 0, 0);
  if (!started) {
    InterlockedCompareExchange(&g_dummy_damage_started_tick, (LONG)now, 0);
    started = (DWORD)InterlockedCompareExchange(
        &g_dummy_damage_started_tick, 0, 0);
  }
  if ((DWORD)(now - started) >= kDummyDamageWindowMs) {
    InterlockedExchange(&g_dummy_damage_done, 1);
    return settled;
  }
  InterlockedExchangeAdd64(&g_dummy_damage_total, (LONG64)damage);
  InterlockedIncrement(&g_dummy_damage_hits);
  InterlockedExchange(&g_dummy_damage_last, damage);
  return settled;
}

static bool InstallDummyDamageHook() {
  if (g_hk_dummy_damage.active) return true;
  uint8_t* target =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteSplitDamageProcess);
  __try {
    // 79BA80: sub esp, 0xD0 (one complete six-byte instruction).
    if (!target || target[0] != 0x81 || target[1] != 0xEC ||
        target[2] != 0xD0 || target[3] != 0x00 || target[4] != 0x00 ||
        target[5] != 0x00)
      return false;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  return InstallInlineDetour(&g_hk_dummy_damage, target, 6,
                             (void*)&Hook_SplitDamageProcess);
}

static void FormatDummyDamageStat(char* buf, size_t n) {
  if (!buf || !n) return;
  const DWORD now = GetTickCount();
  const DWORD started = (DWORD)InterlockedCompareExchange(
      &g_dummy_damage_started_tick, 0, 0);
  DWORD elapsed = started ? (DWORD)(now - started) : 0u;
  if (started && elapsed >= kDummyDamageWindowMs) {
    elapsed = kDummyDamageWindowMs;
    InterlockedExchange(&g_dummy_damage_done, 1);
  }
  const LONG64 total = InterlockedCompareExchange64(
      &g_dummy_damage_total, 0, 0);
  _snprintf_s(
      buf, n, _TRUNCATE,
      "DUMMY_DAMAGE t=%08X:%08X a=%ld s=%d d=%ld ms=%u n=%ld total=%I64d last=%ld",
      (unsigned)(uint32_t)InterlockedCompareExchange(
          &g_dummy_damage_target_hi, 0, 0),
      (unsigned)(uint32_t)InterlockedCompareExchange(
          &g_dummy_damage_target_lo, 0, 0),
      InterlockedCompareExchange(&g_dummy_damage_on, 0, 0),
      started ? 1 : 0,
      InterlockedCompareExchange(&g_dummy_damage_done, 0, 0),
      (unsigned)elapsed,
      InterlockedCompareExchange(&g_dummy_damage_hits, 0, 0), total,
      InterlockedCompareExchange(&g_dummy_damage_last, 0, 0));
}

// Refresh the host's current attack target (0 when the hang is not attacking).
// The IgnorePolicy object only exists while the in-game hang actively attacks;
// this mirrors the read-side scan used by the ignore guard. Pure RPM, no CRT.
static void RefreshCombatHostTarget() {
  InterlockedExchange64(&g_combat_host_target, 0);
  uint32_t gva = NoteToLive(kNoteGameRootGlobal);
  if (!gva) return;
  __try {
    uint32_t root = *(uint32_t*)(uintptr_t)gva;
    if (!root) return;
    uint32_t mid = *(uint32_t*)(uintptr_t)(root + kGameRootMidOff);
    if (!mid) return;
    uint32_t autoplay = *(uint32_t*)(uintptr_t)(mid + kMidAutoplayThisOff);
    if (autoplay < 0x10000u || autoplay >= 0xFFFF0000u) return;
    const uint32_t vtable = NoteToLive(kNoteIgnorePolicyVtable);
    uint32_t* fields = (uint32_t*)(uintptr_t)autoplay;
    for (int off = 0x4; off < 0x800; off += 4) {
      const uint32_t cand = fields[off / 4];
      if (cand < 0x10000u || cand >= 0xFFFF0000u) continue;
      if (*(uint32_t*)(uintptr_t)cand != vtable) continue;
      const uint32_t lo =
          *(uint32_t*)((uint8_t*)cand + kIgnorePolicyTargetLoOff);
      const uint32_t hi =
          *(uint32_t*)((uint8_t*)cand + kIgnorePolicyTargetHiOff);
      InterlockedExchange64(&g_combat_host_target,
                            ((LONG64)hi << 32) | (LONG64)lo);
      return;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    InterlockedExchange64(&g_combat_host_target, 0);
  }
}

// Combat monitor detours reuse the existing SkillActionRequest and damage
// splitters. The action-request hook is shared with SKILL_ACTION_TRACE; the
// damage splitter is shared with DUMMY_DAMAGE_STAT. Both are install-once.
static int __fastcall Hook_SkillActionRequestTrace(
    void* self, void* edx, uint32_t a01, uint32_t a02, uint32_t a03,
    uint32_t a04, uint32_t a05, uint32_t a06, uint32_t a07, uint32_t a08,
    uint32_t a09, uint32_t a10, uint32_t a11, uint32_t a12, uint32_t a13,
    uint32_t a14, uint32_t a15, uint32_t a16, uint32_t a17, uint32_t a18,
    uint32_t a19, uint32_t a20, uint32_t a21);
static bool InstallCombatMonitorHooks() {
  if (g_hk_action_request_trace.active && g_hk_dummy_damage.active) return true;
  if (!g_hk_action_request_trace.active) {
    uint8_t* request =
        (uint8_t*)(uintptr_t)NoteToLive(kNoteSkillActionRequest);
    __try {
      // 762F10: sub esp,50; cmp dword ptr [esp+6C],2
      if (!request || request[0] != 0x83 || request[1] != 0xEC ||
          request[2] != 0x50 || request[3] != 0x83 || request[4] != 0x7C ||
          request[5] != 0x24 || request[6] != 0x6C || request[7] != 0x02)
        return false;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      return false;
    }
    if (!InstallInlineDetour(&g_hk_action_request_trace, request, 8,
                             (void*)&Hook_SkillActionRequestTrace))
      return false;
  }
  if (!g_hk_dummy_damage.active && !InstallDummyDamageHook()) return false;
  return true;
}

static void FormatCombatMonitor(char* buf, size_t n) {
  if (!buf || !n) return;
  const LONG64 ht = InterlockedCompareExchange64(&g_combat_host_target, 0, 0);
  _snprintf_s(buf, n, _TRUNCATE,
      "COMBAT_MONITOR a=%ld d=%ld total=%I64d t=%08X:%08X r=%d",
      InterlockedCompareExchange(&g_combat_attack_seq, 0, 0),
      InterlockedCompareExchange(&g_combat_self_damage_seq, 0, 0),
      InterlockedCompareExchange64(&g_combat_self_damage_total, 0, 0),
      (unsigned)(uint32_t)((uint64_t)ht >> 32),
      (unsigned)(uint32_t)ht,
      InterlockedCompareExchange(&g_combat_resolved, 0, 0));
}


// ---- CG skip: passive detour on PlayCG / PlayBlackEdge ----
// When armed, each start call still runs, then we immediately StopCG /
// StopBlackEdge + ShowGameUI. No Esc polling, no CRT dialog scan.
typedef uint8_t(__thiscall* FnPlayCG)(void* self, int cg_id);
typedef void(__thiscall* FnStopCG)(void* self);
typedef void(__thiscall* FnPlayBlackEdge)(void* self, float top, float bottom,
                                          int duration_ms);
typedef void(__thiscall* FnStopBlackEdge)(void* self, int param);
typedef void(__thiscall* FnShowGameUI)(void* self, uint8_t show);

static void CgSkipAfterStart(void* self, int which) {
  if (!self) return;
  if (!InterlockedCompareExchange(&g_cg_skip_on, 0, 0)) return;
  if (InterlockedCompareExchange(&g_cg_skip_busy, 1, 0) != 0) return;
  __try {
    // Full StopCG cleans camera/subtitle/black-edge/fade when a CG is active.
    FnStopCG stop_cg = (FnStopCG)(uintptr_t)NoteToLive(kNoteStopCG);
    if (stop_cg) stop_cg(self);
    // Black-edge-only cinematics may not set CG active bit; stop bars directly.
    FnStopBlackEdge stop_be =
        (FnStopBlackEdge)(uintptr_t)NoteToLive(kNoteStopBlackEdge);
    if (stop_be) stop_be(self, 0);
    FnShowGameUI show_ui =
        (FnShowGameUI)(uintptr_t)NoteToLive(kNoteShowGameUI);
    if (show_ui) show_ui(self, 1);
    InterlockedIncrement(&g_cg_skip_hits);
    if (which == 1) InterlockedIncrement(&g_cg_skip_play_hits);
    if (which == 2) InterlockedIncrement(&g_cg_skip_black_hits);
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  InterlockedExchange(&g_cg_skip_busy, 0);
}

// MSVC thiscall as fastcall: ecx=this, edx unused, stack args follow.
static uint8_t __fastcall Hook_PlayCG(void* self, void* /*edx*/, int cg_id) {
  FnPlayCG original = (FnPlayCG)g_hk_cg_play.trampoline;
  uint8_t ok = 0;
  __try {
    ok = original ? original(self, cg_id) : 0;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    ok = 0;
  }
  CgSkipAfterStart(self, 1);
  return ok;
}

static void __fastcall Hook_PlayBlackEdge(void* self, void* /*edx*/, float top,
                                          float bottom, int duration_ms) {
  FnPlayBlackEdge original = (FnPlayBlackEdge)g_hk_cg_black_edge.trampoline;
  __try {
    if (original) original(self, top, bottom, duration_ms);
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  CgSkipAfterStart(self, 2);
}

static bool InstallCgSkipHooks() {
  if (!g_hk_cg_play.active) {
    uint8_t* target = (uint8_t*)(uintptr_t)NoteToLive(kNotePlayCG);
    __try {
      // 846EF0: mov eax, fs:[0]  (6 bytes complete insn)
      if (!target || target[0] != 0x64 || target[1] != 0xA1 ||
          target[2] != 0x00 || target[3] != 0x00 || target[4] != 0x00 ||
          target[5] != 0x00)
        return false;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      return false;
    }
    if (!InstallInlineDetour(&g_hk_cg_play, target, 6, (void*)&Hook_PlayCG))
      return false;
  }
  if (!g_hk_cg_black_edge.active) {
    uint8_t* target = (uint8_t*)(uintptr_t)NoteToLive(kNotePlayBlackEdge);
    __try {
      // 8449B0: push esi; mov esi, ecx; cmp dword ptr [esi], 0  (6 bytes)
      if (!target || target[0] != 0x56 || target[1] != 0x8B ||
          target[2] != 0xF1 || target[3] != 0x83 || target[4] != 0x3E ||
          target[5] != 0x00)
        return false;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      return false;
    }
    if (!InstallInlineDetour(&g_hk_cg_black_edge, target, 6,
                             (void*)&Hook_PlayBlackEdge))
      return false;
  }
  return g_hk_cg_play.active && g_hk_cg_black_edge.active;
}


static void* ResolveCgManager() {
  typedef void*(__cdecl* FnGetCgApi)();
  FnGetCgApi get_api = (FnGetCgApi)(uintptr_t)NoteToLive(kNoteGetCgApi);
  if (!get_api) return nullptr;
  __try {
    void* side = get_api();
    if (!side) return nullptr;
    return *(void**)((uint8_t*)side + kCgApiMgrOff);
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return nullptr;
  }
}

// One-shot: stop whatever cinematic is already on screen when arming.
static void ForceStopCurrentCg() {
  void* self = ResolveCgManager();
  if (!self) return;
  CgSkipAfterStart(self, 0);
}

static void FormatCgSkip(char* buf, size_t n) {
  if (!buf || !n) return;
  _snprintf_s(
      buf, n, _TRUNCATE,
      "CG_SKIP on=%ld busy=%ld hits=%ld play=%ld black=%ld hk=%d/%d",
      InterlockedCompareExchange(&g_cg_skip_on, 0, 0),
      InterlockedCompareExchange(&g_cg_skip_busy, 0, 0),
      InterlockedCompareExchange(&g_cg_skip_hits, 0, 0),
      InterlockedCompareExchange(&g_cg_skip_play_hits, 0, 0),
      InterlockedCompareExchange(&g_cg_skip_black_hits, 0, 0),
      g_hk_cg_play.active ? 1 : 0, g_hk_cg_black_edge.active ? 1 : 0);
}

typedef int(__thiscall* FnSkillActionRequestTrace)(
    void* self, uint32_t a01, uint32_t a02, uint32_t a03, uint32_t a04,
    uint32_t a05, uint32_t a06, uint32_t a07, uint32_t a08, uint32_t a09,
    uint32_t a10, uint32_t a11, uint32_t a12, uint32_t a13, uint32_t a14,
    uint32_t a15, uint32_t a16, uint32_t a17, uint32_t a18, uint32_t a19,
    uint32_t a20, uint32_t a21);
typedef int(__thiscall* FnCastCoreTrace)(
    void* self, uint32_t a01, uint32_t a02, uint32_t a03, uint32_t a04,
    uint32_t a05, uint32_t a06, uint32_t a07, uint32_t a08, uint32_t a09,
    uint32_t a10, uint32_t a11, uint32_t a12, uint32_t a13, uint32_t a14,
    uint32_t a15, uint32_t a16, uint32_t a17, uint32_t a18, uint32_t a19);
typedef void(__thiscall* FnSetCurActiveSkillTrace)(
    void* self, uint32_t a1, void* event_ptr, uint32_t a3, uint32_t a4);
typedef void(__thiscall* FnOnPerformSkillTrace)(
    void* self, void* event_ptr, uint32_t a02, uint32_t a03, uint32_t a04,
    uint32_t a05, uint32_t a06, uint32_t a07, uint32_t a08, uint32_t a09);
typedef int(__thiscall* FnPerformStopTrace)(void* self, uint32_t reason);
typedef int(__thiscall* FnHostStartSessionTrace)(
    void* self, uint32_t a01, uint32_t a02, uint32_t a03, uint32_t a04,
    uint32_t a05, uint32_t a06, uint32_t a07, uint32_t a08, uint32_t a09,
    uint32_t a10, uint32_t a11, uint32_t a12);
typedef void*(__thiscall* FnARCFourUpdateTrace)(void* self, void* octets);
typedef uint8_t(__thiscall* FnCurrentActionGate)(void* self, void* skill,
                                                 void** current_out);
typedef uint8_t(__thiscall* FnCmdStartCurrentQingGong)(void* self,
                                                       uint32_t start);
typedef uint8_t(__thiscall* FnCmdStartQingGong)(void* self, uint32_t type,
                                                uint32_t start);
typedef uint8_t(__thiscall* FnCmdStopCurrentQingGong)(void* self);
static void ReadCastTraceFields(void* cast_this, bool after,
                                SkillActionTraceEntry* e);
static void ReadSkillTraceFields(void* skill, SkillActionTraceEntry* e);
static void StoreSkillActionTrace(const SkillActionTraceEntry* src);

static void StoreSkillPacketTrace(const SkillPacketTraceEntry* src) {
  if (!src || !InterlockedCompareExchange(&g_skill_action_trace_on, 0, 0))
    return;
  LONG idx = InterlockedIncrement(&g_skill_packet_trace_count) - 1;
  if (idx < 0 || idx >= kMaxSkillPacketTrace) {
    InterlockedIncrement(&g_skill_packet_trace_dropped);
    return;
  }
  SkillPacketTraceEntry e = *src;
  e.seq = (uint32_t)(idx + 1);
  g_skill_packet_trace[idx] = e;
}

static void* __fastcall Hook_ARCFourUpdateTrace(
    void* self, void* /*edx*/, void* octets) {
  FnARCFourUpdateTrace original =
      (FnARCFourUpdateTrace)g_hk_arcfour_update_trace.trampoline;
  if (InterlockedCompareExchange(&g_skill_action_trace_on, 0, 0)) {
    SkillPacketTraceEntry e = {};
    e.tick = GetTickCount();
    e.thread_id = GetCurrentThreadId();
    e.caller = (uint32_t)(uintptr_t)_ReturnAddress();
    e.self_ptr = (uint32_t)(uintptr_t)self;
    __try {
      uint8_t* desc = (uint8_t*)octets;
      uint8_t* begin = desc ? *(uint8_t**)(desc + 4) : nullptr;
      uint8_t* end = desc ? *(uint8_t**)(desc + 8) : nullptr;
      const uintptr_t begin_u = (uintptr_t)begin;
      const uintptr_t end_u = (uintptr_t)end;
      if (begin && end_u >= begin_u) {
        const uintptr_t span = end_u - begin_u;
        e.length = span > 0xFFFFFFFFu ? 0xFFFFFFFFu : (uint32_t)span;
        e.captured = e.length < (uint32_t)kMaxSkillPacketBytes
                         ? e.length
                         : (uint32_t)kMaxSkillPacketBytes;
        if (e.captured) memcpy(e.payload, begin, e.captured);
      }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      e.length = 0;
      e.captured = 0;
    }
    StoreSkillPacketTrace(&e);
  }
  return original ? original(self, octets) : octets;
}

static int __fastcall Hook_HostStartSessionTrace(
    void* self, void* /*edx*/, uint32_t a01, uint32_t a02, uint32_t a03,
    uint32_t a04, uint32_t a05, uint32_t a06, uint32_t a07, uint32_t a08,
    uint32_t a09, uint32_t a10, uint32_t a11, uint32_t a12) {
  FnHostStartSessionTrace original =
      (FnHostStartSessionTrace)g_hk_host_start_session_trace.trampoline;
  if (!InterlockedCompareExchange(&g_skill_action_trace_on, 0, 0)) {
    return original ? original(self, a01, a02, a03, a04, a05, a06, a07,
                               a08, a09, a10, a11, a12)
                    : -1;
  }
  SkillActionTraceEntry e = {};
  e.kind = SKTRACE_SESSION_SEND;
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.caller = (uint32_t)(uintptr_t)_ReturnAddress();
  e.self_ptr = (uint32_t)(uintptr_t)self;
  const uint32_t args[12] = {a01, a02, a03, a04, a05, a06,
                             a07, a08, a09, a10, a11, a12};
  memcpy(e.session_args, args, sizeof(args));
  e.ret = original ? original(self, a01, a02, a03, a04, a05, a06, a07,
                              a08, a09, a10, a11, a12)
                   : -1;
  StoreSkillActionTrace(&e);
  return e.ret;
}

static uint8_t __fastcall Hook_YoufengCurrentActionGate(
    void* self, void* /*edx*/, void* skill, void** current_out) {
  FnCurrentActionGate original =
      (FnCurrentActionGate)g_hk_youfeng_next_gate.trampoline;
  uint8_t blocked = original ? original(self, skill, current_out) : 1u;
  if (!blocked ||
      !InterlockedCompareExchange(&g_youfeng_next_gate_on, 0, 0))
    return blocked;

  bool match = false;
  __try {
    uint8_t* cast = (uint8_t*)self;
    uint8_t* candidate = (uint8_t*)skill;
    const uint32_t expected_skill = (uint32_t)InterlockedCompareExchange(
        &g_youfeng_next_gate_skill, 0, 0);
    const uint32_t expected_config = (uint32_t)InterlockedCompareExchange(
        &g_youfeng_next_gate_config, 0, 0);
    const LONG allow_cleared_mode = InterlockedCompareExchange(
        &g_youfeng_next_gate_allow_cleared, 0, 0);
    const bool target_identity = cast &&
        *(uint32_t*)(cast + 0x10) == expected_skill &&
        *(uint32_t*)(cast + 0x18) == expected_config;
    const bool cleared_identity = cast &&
        *(uint32_t*)(cast + 0x10) == 0u &&
        *(uint32_t*)(cast + 0x18) == 0u;
    const bool cast_matches = cast &&
        (allow_cleared_mode == 2
             ? (target_identity || cleared_identity)
             : (allow_cleared_mode == 1 ? cleared_identity
                                        : target_identity));
    match = cast && candidate && expected_skill && expected_config &&
            cast_matches &&
            *(uint32_t*)(candidate + 0xEA4) == expected_config;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    match = false;
  }
  if (!match) return blocked;
  InterlockedIncrement(&g_youfeng_next_gate_hits);
  SkillActionTraceEntry e = {};
  e.kind = SKTRACE_NEXT_GATE;
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.caller = (uint32_t)(uintptr_t)_ReturnAddress();
  e.self_ptr = (uint32_t)(uintptr_t)self;
  e.skill_ptr = (uint32_t)(uintptr_t)skill;
  e.ret = blocked;
  ReadSkillTraceFields(skill, &e);
  ReadCastTraceFields(self, false, &e);
  ReadCastTraceFields(self, true, &e);
  StoreSkillActionTrace(&e);
  return 0u;
}

static uint8_t StartCurrentQingGongFromCast(
    void* cast_ptr, void** qinggong_out, uint32_t* previous_type_out,
    uint8_t* stopped_out, uint8_t* post_stopped_out, uint32_t edge_mode,
    uint32_t* elapsed_out) {
  const DWORD began = GetTickCount();
  uint8_t started = 0;
  uint8_t stopped = 0;
  uint8_t post_stopped = 0;
  uint32_t previous_type = 0xFFFFFFFFu;
  void* qinggong = nullptr;
  __try {
    uint8_t* cast = (uint8_t*)cast_ptr;
    uint8_t* host = cast ? *(uint8_t**)(cast + 0x08) : nullptr;
    qinggong = host ? *(void**)(host + kHostSideThisOff) : nullptr;
    if (qinggong) previous_type = *(uint32_t*)((uint8_t*)qinggong + 0x270);
    FnCmdStopCurrentQingGong stop_qinggong =
        (FnCmdStopCurrentQingGong)(uintptr_t)
            NoteToLive(kNoteCmdStopCurrentQingGong);
    FnCmdStartQingGong start_qinggong =
        (FnCmdStartQingGong)(uintptr_t)NoteToLive(kNoteCmdStartQingGong);
    const bool force_stop = edge_mode == 3u;
    if (qinggong && stop_qinggong &&
        (force_stop || (previous_type >= 1u && previous_type <= 8u))) {
      stopped = stop_qinggong(qinggong);
      if (stopped) Sleep(edge_mode == 4u ? 35u : 12u);
    }
    if (qinggong && start_qinggong)
      started = start_qinggong(qinggong, 1u, 1u);
    // Pulse mode preserves the real 0x90 edge but closes it before the next
    // skill key, so sub-second loops cannot keep accumulating QingGong lift.
    if (started && edge_mode >= 5u && edge_mode <= 8u && qinggong &&
        stop_qinggong) {
      const uint32_t pulse_ms =
          edge_mode == 5u ? 24u : (edge_mode == 7u ? 4u :
                                   (edge_mode == 8u ? 8u : 0u));
      if (pulse_ms) Sleep(pulse_ms);
      post_stopped = stop_qinggong(qinggong);
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    started = 0;
  }
  if (qinggong_out) *qinggong_out = qinggong;
  if (previous_type_out) *previous_type_out = previous_type;
  if (stopped_out) *stopped_out = stopped;
  if (post_stopped_out) *post_stopped_out = post_stopped;
  if (elapsed_out) *elapsed_out = GetTickCount() - began;
  return started;
}

static int __fastcall Hook_YoufengActionCast(
    void* self, void* /*edx*/, uint32_t a01, void* skill, uint32_t phase,
    uint32_t a04, uint32_t a05, void* action_context, uint32_t a07,
    uint32_t a08, uint32_t a09, uint32_t a10, uint32_t a11,
    uint32_t a12, uint32_t a13, uint32_t a14) {
  FnActionCast original = (FnActionCast)g_hk_youfeng_action_cast.trampoline;
  bool match = false;
  if (InterlockedCompareExchange(&g_youfeng_next_gate_on, 0, 0) &&
      InterlockedCompareExchange(&g_youfeng_next_gate_qinggong, 0, 0)) {
    __try {
      uint8_t* cast = (uint8_t*)self;
      uint8_t* candidate = (uint8_t*)skill;
      const uint32_t expected_skill = (uint32_t)InterlockedCompareExchange(
          &g_youfeng_next_gate_skill, 0, 0);
      const uint32_t expected_config = (uint32_t)InterlockedCompareExchange(
          &g_youfeng_next_gate_config, 0, 0);
      match = cast && candidate && expected_skill && expected_config &&
          *(uint32_t*)(cast + 0x10) == expected_skill &&
          *(uint32_t*)(cast + 0x18) == expected_config &&
          *(uint32_t*)(candidate + 0xEA4) == expected_config;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      match = false;
    }
  }

  if (match) {
    // Consume before entering QingGong because it calls 0x75F000 recursively.
    InterlockedExchange(&g_youfeng_next_gate_on, 0);
    InterlockedExchange(&g_youfeng_next_gate_due, 0);
    InterlockedExchange(&g_youfeng_next_gate_qinggong, 0);
    InterlockedIncrement(&g_youfeng_next_gate_hits);

    SkillActionTraceEntry qg = {};
    qg.kind = SKTRACE_QINGGONG_GATE;
    qg.tick = GetTickCount();
    qg.thread_id = GetCurrentThreadId();
    qg.caller = (uint32_t)(uintptr_t)_ReturnAddress();
    qg.self_ptr = (uint32_t)(uintptr_t)self;
    qg.skill_ptr = (uint32_t)(uintptr_t)skill;
    qg.arg3 = 1u;  // ActionCast-entry hook path.
    ReadSkillTraceFields(skill, &qg);
    ReadCastTraceFields(self, false, &qg);
    void* qinggong = nullptr;
    uint32_t previous_type = 0xFFFFFFFFu;
    uint32_t elapsed = 0;
    uint8_t stopped = 0;
    uint8_t post_stopped = 0;
    const uint8_t started = StartCurrentQingGongFromCast(
        self, &qinggong, &previous_type, &stopped, &post_stopped, 1u,
        &elapsed);
    qg.arg1 = (uint32_t)started;
    qg.arg2 = (uint32_t)(uintptr_t)qinggong;
    qg.session_args[0] = previous_type;
    qg.session_args[1] = (uint32_t)stopped;
    qg.session_args[2] = elapsed;
    qg.session_args[3] = (uint32_t)post_stopped;
    qg.ret = (int32_t)started;
    ReadCastTraceFields(self, true, &qg);
    StoreSkillActionTrace(&qg);
    if (started) Sleep(35u);
  }

  return original
      ? original(self, a01, skill, phase, a04, a05, action_context, a07,
                 a08, a09, a10, a11, a12, a13, a14)
      : -1;
}

static bool InstallYoufengActionCastHook() {
  if (g_hk_youfeng_action_cast.active) return true;
  uint8_t* target = (uint8_t*)(uintptr_t)NoteToLive(kNoteActionCast);
  __try {
    // 764820: fld dword ptr [0x122E218] (6-byte absolute instruction).
    if (!target || target[0] != 0xD9 || target[1] != 0x05 ||
        target[2] != 0x18 || target[3] != 0xE2 ||
        target[4] != 0x22 || target[5] != 0x01)
      return false;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  return InstallInlineDetour(&g_hk_youfeng_action_cast, target, 6,
                             (void*)&Hook_YoufengActionCast);
}

static bool InstallYoufengNextGateHook() {
  if (g_hk_youfeng_next_gate.active) return true;
  uint8_t* target =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteCurrentActionGate);
  __try {
    // 7546F0: push ecx; mov ecx,[ecx+8]; lea eax,[esp]
    if (!target || target[0] != 0x51 || target[1] != 0x8B ||
        target[2] != 0x49 || target[3] != 0x08 || target[4] != 0x8D ||
        target[5] != 0x04 || target[6] != 0x24)
      return false;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  return InstallInlineDetour(&g_hk_youfeng_next_gate, target, 7,
                             (void*)&Hook_YoufengCurrentActionGate);
}

static void StopYoufengNextGateHook() {
  InterlockedExchange(&g_youfeng_next_gate_on, 0);
  InterlockedExchange(&g_youfeng_next_gate_due, 0);
  InterlockedExchange(&g_youfeng_next_gate_skill, 0);
  InterlockedExchange(&g_youfeng_next_gate_config, 0);
  InterlockedExchange(&g_youfeng_next_gate_allow_cleared, 0);
  InterlockedExchange(&g_youfeng_next_gate_qinggong, 0);
  // Keep the passive detour installed; g_youfeng_next_gate_on gates behavior.
}

static void ProcessPendingYoufengNextGate() {
  if (!InterlockedCompareExchange(&g_youfeng_next_gate_on, 0, 0)) return;
  const DWORD due = (DWORD)InterlockedCompareExchange(
      &g_youfeng_next_gate_due, 0, 0);
  // due=0 is the explicit persistent mode used only by ultimate lab mode 13.
  if (due == 0u) return;
  if ((LONG)(GetTickCount() - due) >= 0) {
    InterlockedExchange(&g_youfeng_next_gate_on, 0);
    InterlockedExchange(&g_youfeng_next_gate_qinggong, 0);
  }
}

static bool RestopSuppressedSkillPerform(void* self) {
  bool restopped = false;
  __try {
    uint8_t* cast = (uint8_t*)self;
    uint8_t* host = cast ? *(uint8_t**)(cast + 0x08) : nullptr;
    uint8_t* mgr = host ? *(uint8_t**)(host + 0x270) : nullptr;
    uint8_t* side = mgr ? *(uint8_t**)(mgr + 0x0C) : nullptr;
    if (side) {
      side[0x40] = 0;
      side[0x41] = 0;
    }
    uint8_t* curr = mgr ? *(uint8_t**)(mgr + 0x08) : nullptr;
    const uint32_t perform_type = curr ? *(uint32_t*)(curr + 0x04) : 0;
    if (mgr && curr && perform_type != 2) {
      uint8_t* vt = *(uint8_t**)mgr;
      FnPerformStopTrace stop =
          vt ? *(FnPerformStopTrace*)(vt + 0x10) : nullptr;
      if (stop) {
        stop(mgr, 0x65);
        restopped = true;
      }
    }
    if (host) {
      *(uint32_t*)(host + 0x41C) = 0;
      *(uint32_t*)(host + 0x420) = 0;
      *(uint32_t*)(host + 0x424) = 0;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    restopped = false;
  }
  return restopped;
}

static void ProcessPendingSkillRestop() {
  if (!InterlockedExchange(&g_skill_refill_restop_pending, 0)) return;
  void* cast = (void*)(uintptr_t)(uint32_t)InterlockedCompareExchange(
      &g_skill_refill_restop_cast, 0, 0);
  if (!cast ||
      !InterlockedCompareExchange(&g_skill_refill_suppress_on, 0, 0))
    return;
  SkillActionTraceEntry e = {};
  e.kind = SKTRACE_RESTOP;
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.self_ptr = (uint32_t)(uintptr_t)cast;
  ReadCastTraceFields(cast, false, &e);
  e.ret = RestopSuppressedSkillPerform(cast) ? 1 : 0;
  ReadCastTraceFields(cast, true, &e);
  StoreSkillActionTrace(&e);
}

static int RunActionPhase(void* cast, uint32_t skill, uint32_t config,
                          uint32_t phase) {
  uint32_t fn_va = NoteToLive(kNoteActionCast);
  uint32_t context_va = NoteToLive(kNoteActionContextGlobal);
  if (!cast || !fn_va || !context_va) return -1;
  uint32_t event_buf[8] = {0};
  event_buf[0] = skill;
  event_buf[2] = config;
  FnActionCast fn = (FnActionCast)(uintptr_t)fn_va;
  return fn(cast, 1u, event_buf, phase, 0u, 0u,
            (void*)(uintptr_t)context_va, 0u, 0u, 0xFFFFFFFFu,
            0xFFFFFFFFu, 0u, 1u, 0u, 0u);
}

static int RunE07ActionPhase(void* cast, uint32_t phase) {
  return RunActionPhase(cast, 0u, 0x0E07u, phase);
}

static void ClearYoufengChainPending() {
  InterlockedExchange(&g_youfeng_chain_phase, 0);
  InterlockedExchange(&g_youfeng_chain_due, 0);
  InterlockedExchange(&g_youfeng_chain_cast, 0);
  InterlockedExchange(&g_youfeng_chain_host, 0);
  InterlockedExchange(&g_youfeng_chain_mgr, 0);
}

static void ProcessPendingYoufengChain() {
  LONG phase = InterlockedCompareExchange(&g_youfeng_chain_phase, 0, 0);
  if (phase == 0) return;
  DWORD now = GetTickCount();
  DWORD due = (DWORD)InterlockedCompareExchange(&g_youfeng_chain_due, 0, 0);
  if ((LONG)(now - due) < 0) return;

  uint8_t* cast = (uint8_t*)(uintptr_t)(uint32_t)
      InterlockedCompareExchange(&g_youfeng_chain_cast, 0, 0);
  uint8_t* host = (uint8_t*)(uintptr_t)(uint32_t)
      InterlockedCompareExchange(&g_youfeng_chain_host, 0, 0);
  uint8_t* mgr = (uint8_t*)(uintptr_t)(uint32_t)
      InterlockedCompareExchange(&g_youfeng_chain_mgr, 0, 0);
  bool context_ok = false;
  __try {
    context_ok = cast && host && mgr &&
                 *(uint8_t**)(host + kSkillCastThisOff) == cast &&
                 *(uint8_t**)(host + kHostPerformMgrOff) == mgr;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    context_ok = false;
  }
  if (!context_ok) {
    ClearYoufengChainPending();
    return;
  }

  if (phase == 1) {
    __try {
      RunE07ActionPhase(cast, 2u);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      ClearYoufengChainPending();
      return;
    }
    InterlockedExchange(&g_youfeng_chain_due, (LONG)(now + 47u));
    InterlockedExchange(&g_youfeng_chain_phase, 2);
    return;
  }

  __try {
    uint32_t skill = *(uint32_t*)(cast + 0x10);
    uint32_t config = *(uint32_t*)(cast + 0x18);
    uint32_t busy = *(uint32_t*)(cast + 0x4A0);
    uint8_t* current = *(uint8_t**)(mgr + 0x08);
    uint32_t perform_type = current ? *(uint32_t*)(current + 0x04) : 0;
    uint32_t gate0 = *(uint32_t*)(host + kHostSessionStateOff);
    uint32_t gate1 = *(uint32_t*)(host + kHostSessionStateOff + 4);
    uint32_t gate2 = *(uint32_t*)(host + kHostSessionStateOff + 8);
    if ((skill == 0u || skill == 0xFFFFFFFFu) &&
        (config == 0u || config == 0xFFFFFFFFu) && busy == 0u &&
        (perform_type == 2u || perform_type == 3u) &&
        gate0 == 0u && gate1 == 0u && gate2 == 0u) {
      ClearYoufengChainPending();
      return;
    }
    if (skill != 0u || config != 0x0E07u || perform_type != 4u) {
      ClearYoufengChainPending();
      return;
    }

    uint8_t* vt = *(uint8_t**)mgr;
    FnPerformStopTrace stop = vt ? *(FnPerformStopTrace*)(vt + 0x10) : nullptr;
    uint32_t on_stop_va = NoteToLive(kNoteOnSkillStopped);
    if (!stop || !on_stop_va) {
      ClearYoufengChainPending();
      return;
    }
    stop(mgr, 0x65u);
    uint32_t event_buf[8] = {0};
    event_buf[2] = 0x0E07u;
    FnOnSkillStopped on_stop = (FnOnSkillStopped)(uintptr_t)on_stop_va;
    on_stop(cast, event_buf, 0u, 0u, 0u, 1u);
    stop(mgr, 0x67u);
    *(uint32_t*)(host + kHostSessionStateOff) = 0;
    *(uint32_t*)(host + kHostSessionStateOff + 4) = 0;
    *(uint32_t*)(host + kHostSessionStateOff + 8) = 0;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  ClearYoufengChainPending();
}

static void ClearYoufengMashPending() {
  InterlockedExchange(&g_youfeng_mash_phase, 0);
  InterlockedExchange(&g_youfeng_mash_due, 0);
  InterlockedExchange(&g_youfeng_mash_cast, 0);
  InterlockedExchange(&g_youfeng_mash_host, 0);
  InterlockedExchange(&g_youfeng_mash_skill, 0);
  InterlockedExchange(&g_youfeng_mash_config, 0);
}

static void ProcessPendingYoufengMash() {
  if (InterlockedCompareExchange(&g_youfeng_mash_phase, 0, 0) != 1) return;
  const DWORD due =
      (DWORD)InterlockedCompareExchange(&g_youfeng_mash_due, 0, 0);
  if ((LONG)(GetTickCount() - due) < 0) return;
  uint8_t* cast = (uint8_t*)(uintptr_t)(uint32_t)
      InterlockedCompareExchange(&g_youfeng_mash_cast, 0, 0);
  uint8_t* host = (uint8_t*)(uintptr_t)(uint32_t)
      InterlockedCompareExchange(&g_youfeng_mash_host, 0, 0);
  const uint32_t config = (uint32_t)InterlockedCompareExchange(
      &g_youfeng_mash_config, 0, 0);
  const uint32_t skill = (uint32_t)InterlockedCompareExchange(
      &g_youfeng_mash_skill, 0, 0);
  bool context_ok = false;
  __try {
    context_ok = cast && host && skill && config &&
                 *(uint8_t**)(host + kSkillCastThisOff) == cast;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    context_ok = false;
  }
  if (context_ok) {
    __try {
      RunActionPhase(cast, skill, config, 2u);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
    }
  }
  ClearYoufengMashPending();
}

static void ClearYoufengPhasePending() {
  InterlockedExchange(&g_youfeng_phase_up_pending, 0);
  InterlockedExchange(&g_youfeng_phase_due, 0);
  InterlockedExchange(&g_youfeng_phase_net, 0);
  InterlockedExchange(&g_youfeng_phase_config, 0);
}

static void ProcessPendingYoufengPhase() {
  if (!InterlockedCompareExchange(&g_youfeng_phase_up_pending, 0, 0)) return;
  const DWORD due = (DWORD)InterlockedCompareExchange(
      &g_youfeng_phase_due, 0, 0);
  if ((LONG)(GetTickCount() - due) < 0) return;
  void* net = (void*)(uintptr_t)(uint32_t)InterlockedCompareExchange(
      &g_youfeng_phase_net, 0, 0);
  const uint32_t config = (uint32_t)InterlockedCompareExchange(
      &g_youfeng_phase_config, 0, 0);
  bool context_ok = false;
  __try {
    context_ok = net && config == 0x034Du && ResolveNetRoot() == net;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    context_ok = false;
  }
  if (context_ok) {
    FnActionPhasePacket phase_up = (FnActionPhasePacket)(uintptr_t)
        NoteToLive(kNoteActionPhaseUp);
    if (phase_up) {
      __try {
        phase_up((uint8_t*)net + kActionPhaseThisOff, config);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
      }
    }
  }
  ClearYoufengPhasePending();
}

static void ClearKuangfengTailPending() {
  InterlockedExchange(&g_kuangfeng_tail_pending, 0);
  InterlockedExchange(&g_kuangfeng_tail_pending_cast, 0);
  InterlockedExchange(&g_kuangfeng_tail_pending_perform, 0);
  InterlockedExchange(&g_kuangfeng_tail_pending_due, 0);
}

static void DropKuangfengTailPending(LONG reason) {
  InterlockedIncrement(&g_kuangfeng_tail_pending_dropped);
  InterlockedExchange(&g_kuangfeng_tail_pending_last_reason, reason);
  ClearKuangfengTailPending();
}

static void ProcessPendingKuangfengTail() {
  if (!InterlockedCompareExchange(&g_kuangfeng_tail_pending, 0, 0)) return;
  if (!InterlockedCompareExchange(&g_kuangfeng_tail_auto_on, 0, 0)) {
    ClearKuangfengTailPending();
    return;
  }

  const DWORD now = GetTickCount();
  const DWORD auto_due = (DWORD)InterlockedCompareExchange(
      &g_kuangfeng_tail_auto_due, 0, 0);
  const DWORD pending_due = (DWORD)InterlockedCompareExchange(
      &g_kuangfeng_tail_pending_due, 0, 0);
  if ((LONG)(now - auto_due) >= 0 || (LONG)(now - pending_due) >= 0) {
    if ((LONG)(now - auto_due) >= 0) {
      ClearKuangfengTailPending();
      InterlockedExchange(&g_kuangfeng_tail_auto_on, 0);
    } else {
      DropKuangfengTailPending(1);
    }
    return;
  }

  uint8_t* expected_cast = (uint8_t*)(uintptr_t)(uint32_t)
      InterlockedCompareExchange(&g_kuangfeng_tail_pending_cast, 0, 0);
  const uint32_t expected_perform = (uint32_t)InterlockedCompareExchange(
      &g_kuangfeng_tail_pending_perform, 0, 0);
  uint8_t* cast = nullptr;
  uint8_t* host = nullptr;
  uint8_t* mgr = nullptr;
  uint32_t skill = 0;
  uint32_t config = 0;
  uint32_t busy = 0;
  uint32_t perform_type = 0;
  uint32_t gate0 = 0;
  uint32_t gate1 = 0;
  uint32_t gate2 = 0;
  uint32_t id_perform = 0;
  __try {
    FnGetHostSide get_host =
        (FnGetHostSide)(uintptr_t)NoteToLive(kNoteGetHostSide);
    host = get_host ? (uint8_t*)get_host() : nullptr;
    cast = host ? *(uint8_t**)(host + kSkillCastThisOff) : nullptr;
    mgr = host ? *(uint8_t**)(host + kHostPerformMgrOff) : nullptr;
    if (cast) {
      skill = *(uint32_t*)(cast + 0x10);
      config = *(uint32_t*)(cast + 0x18);
      busy = *(uint32_t*)(cast + 0x4A0);
      id_perform = *(uint32_t*)(cast + 0x6C);
    }
    uint8_t* current = mgr ? *(uint8_t**)(mgr + 0x08) : nullptr;
    if (current) perform_type = *(uint32_t*)(current + 0x04);
    if (host) {
      gate0 = *(uint32_t*)(host + kHostSessionStateOff);
      gate1 = *(uint32_t*)(host + kHostSessionStateOff + 4);
      gate2 = *(uint32_t*)(host + kHostSessionStateOff + 8);
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    cast = nullptr;
    host = nullptr;
    mgr = nullptr;
  }

  if (!cast || cast != expected_cast || !host || !mgr ||
      skill != 0x952Au || config != 0x176Du ||
      (id_perform != expected_perform && id_perform != 0u &&
       id_perform != 0xFFFFFFFFu)) {
    DropKuangfengTailPending(2);
    return;
  }

  const bool ready =
      busy == 1u && perform_type == 4u && gate0 == 5u && gate1 == 350u &&
      gate2 == 0x176Du && id_perform == expected_perform &&
      expected_perform != 0u && expected_perform != 0xFFFFFFFFu;
  if (!ready) return;

  void* live_qinggong = nullptr;
  uint32_t previous_type = 0xFFFFFFFFu;
  uint32_t elapsed = 0;
  uint8_t stopped = 0;
  uint8_t post_stopped = 0;
  const uint8_t started = StartCurrentQingGongFromCast(
      cast, &live_qinggong, &previous_type, &stopped, &post_stopped, 6u,
      &elapsed);
  SkillActionTraceEntry qg = {};
  qg.kind = SKTRACE_QINGGONG_GATE;
  qg.tick = GetTickCount();
  qg.thread_id = GetCurrentThreadId();
  qg.self_ptr = (uint32_t)(uintptr_t)cast;
  qg.skill_0 = 0x952Au;
  qg.skill_10 = 0x176Du;
  qg.skill_ea4 = 0x176Du;
  qg.arg1 = started;
  qg.arg2 = (uint32_t)(uintptr_t)live_qinggong;
  qg.arg3 = 8u;
  qg.ret = started && post_stopped ? 1 : 0;
  qg.session_args[0] = previous_type;
  qg.session_args[1] = stopped;
  qg.session_args[2] = elapsed;
  qg.session_args[3] = post_stopped;
  ReadCastTraceFields(cast, false, &qg);
  ReadCastTraceFields(cast, true, &qg);
  StoreSkillActionTrace(&qg);
  ClearKuangfengTailPending();
  if (started && post_stopped) {
    InterlockedIncrement(&g_kuangfeng_tail_auto_hits);
    if (InterlockedDecrement(&g_kuangfeng_tail_auto_remaining) <= 0)
      InterlockedExchange(&g_kuangfeng_tail_auto_on, 0);
  } else {
    InterlockedExchange(&g_kuangfeng_tail_auto_on, 0);
  }
}

static bool ValidateYoufengUltimateCooldownPath() {
  uint8_t* get_pkg_code =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteGetPkgSide);
  uint8_t* metadata_code =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteSkillMetadataByConfig);
  uint8_t* resolve_code =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteResolveCooldownRecord);
  __try {
    if (!get_pkg_code || get_pkg_code[0] != 0xA1 ||
        get_pkg_code[5] != 0x85 || get_pkg_code[6] != 0xC0 ||
        !metadata_code || metadata_code[0] != 0x8B ||
        metadata_code[1] != 0x44 || metadata_code[2] != 0x24 ||
        metadata_code[3] != 0x04 || metadata_code[4] != 0x85 ||
        metadata_code[5] != 0xC0 || !resolve_code ||
        resolve_code[0] != 0x8B || resolve_code[1] != 0x44 ||
        resolve_code[2] != 0x24 || resolve_code[3] != 0x04 ||
        resolve_code[4] != 0x83 || resolve_code[5] != 0xEC ||
        resolve_code[6] != 0x08)
      return false;
    FnSkillMetadataByConfig get_metadata =
        (FnSkillMetadataByConfig)(uintptr_t)metadata_code;
    uint8_t* metadata =
        get_metadata ? (uint8_t*)get_metadata(0x195Eu) : nullptr;
    return metadata && *(uint32_t*)(metadata + 0x8C) == 0xAu;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
}

static uint8_t* ResolveYoufengUltimateCooldownRecord(uint32_t* key_out,
                                                     uint32_t* reason_out) {
  if (key_out) *key_out = 0;
  if (reason_out) *reason_out = 0;
  __try {
    FnGetPkgSide get_pkg =
        (FnGetPkgSide)(uintptr_t)NoteToLive(kNoteGetPkgSide);
    FnSkillMetadataByConfig get_metadata =
        (FnSkillMetadataByConfig)(uintptr_t)
            NoteToLive(kNoteSkillMetadataByConfig);
    FnResolveCooldownRecord resolve_record =
        (FnResolveCooldownRecord)(uintptr_t)
            NoteToLive(kNoteResolveCooldownRecord);
    if (!get_pkg || !get_metadata || !resolve_record) {
      if (reason_out) *reason_out = 1;
      return nullptr;
    }
    uint8_t* metadata = (uint8_t*)get_metadata(0x195Eu);
    const uint32_t metadata_key =
        metadata ? *(uint32_t*)(metadata + 0x8C) : 0u;
    if (!metadata || metadata_key != 0xAu) {
      if (reason_out) *reason_out = 2;
      return nullptr;
    }
    uint8_t* pkg_side = (uint8_t*)get_pkg();
    void* manager =
        pkg_side ? *(void**)(pkg_side + kPkgCooldownManagerOff) : nullptr;
    if (!manager) {
      if (reason_out) *reason_out = 3;
      return nullptr;
    }
    uint32_t resolved_key = 0;
    uint8_t* record =
        resolve_record(manager, 0x195Eu, &resolved_key);
    if (key_out) *key_out = resolved_key;
    if (!record || resolved_key != metadata_key || resolved_key != 0xAu) {
      if (reason_out) *reason_out = 4;
      return nullptr;
    }
    if (*(uint32_t*)(record + 4) != 10000u) {
      if (reason_out) *reason_out = 5;
      return nullptr;
    }
    return record;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    if (reason_out) *reason_out = 6;
    return nullptr;
  }
}

static void ProcessYoufengUltimateCooldownClear() {
  if (!InterlockedCompareExchange(&g_youfeng_ultimate_cd_clear_on, 0, 0))
    return;
  uint32_t key = 0;
  uint32_t reason = 0;
  uint8_t* record =
      ResolveYoufengUltimateCooldownRecord(&key, &reason);
  if (!record) {
    InterlockedIncrement(&g_youfeng_ultimate_cd_clear_failures);
    InterlockedExchange(&g_youfeng_ultimate_cd_clear_last_reason,
                        (LONG)reason);
    return;
  }
  __try {
    const uint32_t remaining = *(uint32_t*)record;
    const uint32_t total = *(uint32_t*)(record + 4);
    InterlockedExchange(&g_youfeng_ultimate_cd_clear_last_key, (LONG)key);
    InterlockedExchange(&g_youfeng_ultimate_cd_clear_last_record,
                        (LONG)(uint32_t)(uintptr_t)record);
    InterlockedExchange(&g_youfeng_ultimate_cd_clear_last_remaining,
                        (LONG)remaining);
    InterlockedExchange(&g_youfeng_ultimate_cd_clear_last_total, (LONG)total);
    InterlockedExchange(&g_youfeng_ultimate_cd_clear_last_reason, 0);
    if (key == 0xAu && total == 10000u && remaining != 0u) {
      *(uint32_t*)record = 0u;
      InterlockedIncrement(&g_youfeng_ultimate_cd_clear_hits);
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    InterlockedIncrement(&g_youfeng_ultimate_cd_clear_failures);
    InterlockedExchange(&g_youfeng_ultimate_cd_clear_last_reason, 6);
  }
}

static void ClearYoufengUltimateTailPending() {
  InterlockedExchange(&g_youfeng_ultimate_tail_pending, 0);
  InterlockedExchange(&g_youfeng_ultimate_tail_pending_cast, 0);
  InterlockedExchange(&g_youfeng_ultimate_tail_pending_perform, 0);
  InterlockedExchange(&g_youfeng_ultimate_tail_pending_due, 0);
}

static void DropYoufengUltimateTailPending(LONG reason) {
  InterlockedIncrement(&g_youfeng_ultimate_tail_pending_dropped);
  InterlockedExchange(&g_youfeng_ultimate_tail_pending_last_reason, reason);
  ClearYoufengUltimateTailPending();
}

static void ProcessPendingYoufengUltimateTail() {
  if (!InterlockedCompareExchange(&g_youfeng_ultimate_tail_pending, 0, 0))
    return;
  if (!InterlockedCompareExchange(&g_youfeng_ultimate_tail_auto_on, 0, 0)) {
    ClearYoufengUltimateTailPending();
    return;
  }

  const DWORD now = GetTickCount();
  const DWORD pending_due = (DWORD)InterlockedCompareExchange(
      &g_youfeng_ultimate_tail_pending_due, 0, 0);
  if ((LONG)(now - pending_due) >= 0) {
    DropYoufengUltimateTailPending(1);
    return;
  }

  uint8_t* expected_cast = (uint8_t*)(uintptr_t)(uint32_t)
      InterlockedCompareExchange(&g_youfeng_ultimate_tail_pending_cast, 0, 0);
  const uint32_t expected_perform = (uint32_t)InterlockedCompareExchange(
      &g_youfeng_ultimate_tail_pending_perform, 0, 0);
  uint8_t* cast = nullptr;
  uint8_t* host = nullptr;
  uint8_t* mgr = nullptr;
  uint32_t skill = 0;
  uint32_t config = 0;
  uint32_t busy = 0;
  uint32_t perform_type = 0;
  uint32_t gate0 = 0;
  uint32_t gate1 = 0;
  uint32_t gate2 = 0;
  uint32_t id_perform = 0;
  __try {
    FnGetHostSide get_host =
        (FnGetHostSide)(uintptr_t)NoteToLive(kNoteGetHostSide);
    host = get_host ? (uint8_t*)get_host() : nullptr;
    cast = host ? *(uint8_t**)(host + kSkillCastThisOff) : nullptr;
    mgr = host ? *(uint8_t**)(host + kHostPerformMgrOff) : nullptr;
    if (cast) {
      skill = *(uint32_t*)(cast + 0x10);
      config = *(uint32_t*)(cast + 0x18);
      busy = *(uint32_t*)(cast + 0x4A0);
      id_perform = *(uint32_t*)(cast + 0x6C);
    }
    uint8_t* current = mgr ? *(uint8_t**)(mgr + 0x08) : nullptr;
    if (current) perform_type = *(uint32_t*)(current + 0x04);
    if (host) {
      gate0 = *(uint32_t*)(host + kHostSessionStateOff);
      gate1 = *(uint32_t*)(host + kHostSessionStateOff + 4);
      gate2 = *(uint32_t*)(host + kHostSessionStateOff + 8);
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    cast = nullptr;
    host = nullptr;
    mgr = nullptr;
  }

  if (!cast || cast != expected_cast || !host || !mgr ||
      skill != 0x9563u || config != 0x195Eu ||
      (id_perform != expected_perform && id_perform != 0u &&
       id_perform != 0xFFFFFFFFu)) {
    DropYoufengUltimateTailPending(2);
    return;
  }

  const bool ready =
      busy == 1u && perform_type == 4u && gate0 == 5u && gate1 == 1600u &&
      gate2 == 0x195Eu && id_perform == expected_perform &&
      expected_perform != 0u && expected_perform != 0xFFFFFFFFu;
  if (!ready) return;

  void* live_qinggong = nullptr;
  uint32_t previous_type = 0xFFFFFFFFu;
  uint32_t elapsed = 0;
  uint8_t stopped = 0;
  uint8_t post_stopped = 0;
  const uint8_t started = StartCurrentQingGongFromCast(
      cast, &live_qinggong, &previous_type, &stopped, &post_stopped, 6u,
      &elapsed);
  SkillActionTraceEntry qg = {};
  qg.kind = SKTRACE_QINGGONG_GATE;
  qg.tick = GetTickCount();
  qg.thread_id = GetCurrentThreadId();
  qg.self_ptr = (uint32_t)(uintptr_t)cast;
  qg.skill_0 = 0x9563u;
  qg.skill_10 = 0x195Eu;
  qg.skill_ea4 = 0x195Eu;
  qg.arg1 = started;
  qg.arg2 = (uint32_t)(uintptr_t)live_qinggong;
  qg.arg3 = 11u;
  qg.ret = started && post_stopped ? 1 : 0;
  qg.session_args[0] = previous_type;
  qg.session_args[1] = stopped;
  qg.session_args[2] = elapsed;
  qg.session_args[3] = post_stopped;
  ReadCastTraceFields(cast, false, &qg);
  ReadCastTraceFields(cast, true, &qg);
  StoreSkillActionTrace(&qg);
  ClearYoufengUltimateTailPending();
  if (started && post_stopped) {
    InterlockedIncrement(&g_youfeng_ultimate_tail_auto_hits);
  } else {
    InterlockedExchange(&g_youfeng_ultimate_tail_pending_last_reason, 3);
    InterlockedExchange(&g_youfeng_ultimate_tail_auto_on, 0);
  }
}

static void ReadSkillTraceFields(void* skill, SkillActionTraceEntry* e) {
  if (!skill || !e) return;
  __try {
    uint8_t* p = (uint8_t*)skill;
    e->skill_0 = *(uint32_t*)(p + 0x0);
    e->skill_4 = *(uint32_t*)(p + 0x4);
    e->skill_10 = *(uint32_t*)(p + 0x10);
    e->skill_18 = *(uint32_t*)(p + 0x18);
    e->skill_ea4 = *(uint32_t*)(p + 0xEA4);
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
}

static void ReadCastTraceFields(void* cast_this, bool after,
                                SkillActionTraceEntry* e) {
  if (!cast_this || !e) return;
  __try {
    uint8_t* p = (uint8_t*)cast_this;
    uint32_t v10 = *(uint32_t*)(p + 0x10);
    uint32_t v18 = *(uint32_t*)(p + 0x18);
    uint32_t v4a0 = *(uint32_t*)(p + 0x4A0);
    if (after) {
      e->cast_10_after = v10;
      e->cast_18_after = v18;
      e->cast_4a0_after = v4a0;
    } else {
      e->cast_10_before = v10;
      e->cast_18_before = v18;
      e->cast_4a0_before = v4a0;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
}

static void StoreSkillActionTrace(const SkillActionTraceEntry* src) {
  if (!src || !InterlockedCompareExchange(&g_skill_action_trace_on, 0, 0))
    return;
  LONG idx = InterlockedIncrement(&g_skill_action_trace_count) - 1;
  if (idx < 0 || idx >= kMaxSkillActionTrace) {
    InterlockedIncrement(&g_skill_action_trace_dropped);
    return;
  }
  SkillActionTraceEntry e = *src;
  e.seq = (uint32_t)(idx + 1);
  g_skill_action_trace[idx] = e;
}

static void ReadQingGongTraceFields(void* self, bool after,
                                    SkillActionTraceEntry* e) {
  if (!self || !e) return;
  __try {
    uint8_t* p = (uint8_t*)self;
    const uint32_t q270 = *(uint32_t*)(p + 0x270);
    const uint32_t q274 = *(uint32_t*)(p + 0x274);
    if (after) {
      e->session_args[2] = q270;
      e->session_args[3] = q274;
    } else {
      e->session_args[0] = q270;
      e->session_args[1] = q274;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
}

static uint8_t __fastcall Hook_QingGongStartTrace(
    void* self, void* /*edx*/, uint32_t type, uint32_t start) {
  FnCmdStartQingGong original =
      (FnCmdStartQingGong)g_hk_qinggong_start_trace.trampoline;
  SkillActionTraceEntry e = {};
  e.kind = SKTRACE_QINGGONG_START;
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.caller = (uint32_t)(uintptr_t)_ReturnAddress();
  e.self_ptr = (uint32_t)(uintptr_t)self;
  e.arg1 = type;
  e.arg2 = start;
  ReadQingGongTraceFields(self, false, &e);
  const uint8_t ret = original ? original(self, type, start) : 0u;
  e.ret = (int32_t)ret;
  ReadQingGongTraceFields(self, true, &e);
  StoreSkillActionTrace(&e);
  return ret;
}

static uint8_t __fastcall Hook_QingGongStopTrace(void* self, void* /*edx*/) {
  FnCmdStopCurrentQingGong original =
      (FnCmdStopCurrentQingGong)g_hk_qinggong_stop_trace.trampoline;
  SkillActionTraceEntry e = {};
  e.kind = SKTRACE_QINGGONG_STOP;
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.caller = (uint32_t)(uintptr_t)_ReturnAddress();
  e.self_ptr = (uint32_t)(uintptr_t)self;
  ReadQingGongTraceFields(self, false, &e);
  const uint8_t ret = original ? original(self) : 0u;
  e.ret = (int32_t)ret;
  ReadQingGongTraceFields(self, true, &e);
  StoreSkillActionTrace(&e);
  return ret;
}

static bool InstallQingGongTraceHooks() {
  if (g_hk_qinggong_start_trace.active && g_hk_qinggong_stop_trace.active)
    return true;
  uint8_t* start =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteCmdStartQingGong);
  uint8_t* stop =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteCmdStopCurrentQingGong);
  __try {
    if (!start || start[0] != 0x81 || start[1] != 0xEC ||
        start[2] != 0xAC || start[3] != 0x01 ||
        start[4] != 0x00 || start[5] != 0x00 ||
        !stop || stop[0] != 0x81 || stop[1] != 0xEC ||
        stop[2] != 0x90 || stop[3] != 0x01 ||
        stop[4] != 0x00 || stop[5] != 0x00)
      return false;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  if (!g_hk_qinggong_start_trace.active &&
      !InstallInlineDetour(&g_hk_qinggong_start_trace, start, 6,
                           (void*)&Hook_QingGongStartTrace))
    return false;
  if (!g_hk_qinggong_stop_trace.active &&
      !InstallInlineDetour(&g_hk_qinggong_stop_trace, stop, 6,
                           (void*)&Hook_QingGongStopTrace))
    return false;
  return true;
}

static int __fastcall Hook_SkillActionRequestTrace(
    void* self, void* /*edx*/, uint32_t a01, uint32_t a02, uint32_t a03,
    uint32_t a04, uint32_t a05, uint32_t a06, uint32_t a07, uint32_t a08,
    uint32_t a09, uint32_t a10, uint32_t a11, uint32_t a12, uint32_t a13,
    uint32_t a14, uint32_t a15, uint32_t a16, uint32_t a17, uint32_t a18,
    uint32_t a19, uint32_t a20, uint32_t a21) {
  FnSkillActionRequestTrace original =
      (FnSkillActionRequestTrace)g_hk_action_request_trace.trampoline;
  const bool trace_active = InterlockedCompareExchange(
      &g_skill_action_trace_on, 0, 0);
  const bool combat_active =
      InterlockedCompareExchange(&g_combat_monitor_on, 0, 0);
  if (!trace_active && !combat_active) {
    return original
               ? original(self, a01, a02, a03, a04, a05, a06, a07, a08,
                          a09, a10, a11, a12, a13, a14, a15, a16, a17,
                          a18, a19, a20, a21)
               : -1;
  }
  SkillActionTraceEntry e = {};
  e.kind = SKTRACE_REQUEST;
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.caller = (uint32_t)(uintptr_t)_ReturnAddress();
  e.self_ptr = (uint32_t)(uintptr_t)self;
  e.skill_ptr = a10;
  e.arg1 = a07;  // early mode gate: mode==2 returns 0x70
  e.arg2 = a02;  // optional precomputed CanCast result pointer
  e.arg3 = a01;
  ReadSkillTraceFields((void*)(uintptr_t)a10, &e);
  ReadCastTraceFields(self, false, &e);
  int ret = original
                ? original(self, a01, a02, a03, a04, a05, a06, a07, a08,
                           a09, a10, a11, a12, a13, a14, a15, a16, a17,
                           a18, a19, a20, a21)
                : -1;
  if (combat_active) {
    // A host skill action request is itself attack activity; also refresh the
    // host attack target so the damage splitter can keep self-filtering.
    InterlockedIncrement(&g_combat_attack_seq);
    RefreshCombatHostTarget();
  }
  e.ret = ret;
  ReadCastTraceFields(self, true, &e);
  StoreSkillActionTrace(&e);
  return ret;
}

static int __fastcall Hook_ActionCanCastTrace(
    void* self, void* /*edx*/, uint32_t a01, uint32_t a02, uint32_t a03,
    uint32_t a04, uint32_t a05, uint32_t a06, uint32_t a07, uint32_t a08,
    uint32_t a09, uint32_t a10, uint32_t a11, uint32_t a12, uint32_t a13,
    uint32_t a14, uint32_t a15, uint32_t a16, uint32_t a17, uint32_t a18,
    uint32_t a19) {
  FnCastCoreTrace original =
      (FnCastCoreTrace)g_hk_action_cancast_trace.trampoline;
  if (!InterlockedCompareExchange(&g_skill_action_trace_on, 0, 0)) {
    return original
               ? original(self, a01, a02, a03, a04, a05, a06, a07, a08,
                          a09, a10, a11, a12, a13, a14, a15, a16, a17,
                          a18, a19)
               : -1;
  }
  SkillActionTraceEntry e = {};
  e.kind = SKTRACE_CORE;
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.caller = (uint32_t)(uintptr_t)_ReturnAddress();
  e.self_ptr = (uint32_t)(uintptr_t)self;
  e.skill_ptr = a01;
  e.arg1 = a02;
  e.arg2 = a03;
  e.arg3 = a04;
  ReadSkillTraceFields((void*)(uintptr_t)a01, &e);
  ReadCastTraceFields(self, false, &e);
  int ret = original
                ? original(self, a01, a02, a03, a04, a05, a06, a07, a08,
                           a09, a10, a11, a12, a13, a14, a15, a16, a17,
                           a18, a19)
                : -1;
  e.ret = ret;
  ReadCastTraceFields(self, true, &e);
  if (e.caller == NoteToLive(kNoteActionCanCastReturn)) {
    StoreSkillActionTrace(&e);
  }
  return ret;
}

static void __fastcall Hook_SetCurActiveSkillTrace(
    void* self, void* /*edx*/, uint32_t a1, void* event_ptr, uint32_t a3,
    uint32_t a4) {
  FnSetCurActiveSkillTrace original =
      (FnSetCurActiveSkillTrace)g_hk_set_active_trace.trampoline;
  if (!InterlockedCompareExchange(&g_skill_action_trace_on, 0, 0) &&
      !InterlockedCompareExchange(&g_skill_refill_suppress_on, 0, 0)) {
    if (original) original(self, a1, event_ptr, a3, a4);
    return;
  }
  SkillActionTraceEntry e = {};
  e.kind = SKTRACE_ACTIVE;
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.caller = (uint32_t)(uintptr_t)_ReturnAddress();
  e.self_ptr = (uint32_t)(uintptr_t)self;
  e.skill_ptr = (uint32_t)(uintptr_t)event_ptr;
  e.arg1 = a1;
  e.arg2 = a3;
  e.arg3 = a4;
  ReadCastTraceFields(self, false, &e);
  __try {
    if (event_ptr) {
      uint32_t* event_words = (uint32_t*)event_ptr;
      e.skill_0 = event_words[0];
      e.skill_4 = event_words[1];
      e.skill_10 = event_words[2];
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  bool suppress = false;
  bool auto_continue = false;
  const uint32_t session_start = NoteToLive(kNoteNewSessionSetActiveReturn);
  const uint32_t perform_refill = NoteToLive(kNoteOnPerformSetActiveReturn);
  if (e.caller == session_start) {
    const LONG suppress_mode = InterlockedCompareExchange(
        &g_skill_refill_suppress_mode, 0, 0);
    const uint32_t target_skill = (uint32_t)InterlockedCompareExchange(
        &g_skill_refill_target_skill, 0, 0);
    const uint32_t target_config = (uint32_t)InterlockedCompareExchange(
        &g_skill_refill_target_config, 0, 0);
    const bool target_matches =
        (!target_skill || e.skill_0 == target_skill) &&
        (!target_config || e.skill_10 == target_config) &&
        (!target_skill || e.cast_10_before == target_skill) &&
        (!target_config || e.cast_18_before == target_config);
    // A real fresh input reaches SetCurActiveSkill with an empty cast
    // identity. Multi-stage and charged skills can start another internal
    // session with busy either 0 or 1, but the old target identity is still
    // installed. Keep suppression through every such continuation.
    auto_continue =
        suppress_mode == 4 &&
        InterlockedCompareExchange(&g_skill_refill_suppress_on, 0, 0) &&
        target_matches;
    if (!auto_continue) {
      InterlockedExchange(&g_skill_refill_suppress_on, 0);
      InterlockedExchange(&g_skill_refill_restop_pending, 0);
    }
  } else if (e.caller == perform_refill &&
             InterlockedCompareExchange(&g_skill_refill_suppress_on, 0, 0)) {
    const uint32_t target_skill = (uint32_t)InterlockedCompareExchange(
        &g_skill_refill_target_skill, 0, 0);
    const uint32_t target_config = (uint32_t)InterlockedCompareExchange(
        &g_skill_refill_target_config, 0, 0);
    suppress = (!target_skill || e.skill_0 == target_skill) &&
               (!target_config || e.skill_10 == target_config);
  }
  if (original && !suppress) original(self, a1, event_ptr, a3, a4);
  // ret=2 marks a charge/channel auto-continuation session that deliberately
  // did not disarm suppression. ret=1 remains a suppressed perform refill.
  e.ret = auto_continue ? 2 : (suppress ? 1 : 0);
  ReadCastTraceFields(self, true, &e);
  StoreSkillActionTrace(&e);
}

static void __fastcall Hook_OnPerformSkillTrace(
    void* self, void* /*edx*/, void* event_ptr, uint32_t a02, uint32_t a03,
    uint32_t a04, uint32_t a05, uint32_t a06, uint32_t a07, uint32_t a08,
    uint32_t a09) {
  FnOnPerformSkillTrace original =
      (FnOnPerformSkillTrace)g_hk_on_perform_trace.trampoline;
  if (!InterlockedCompareExchange(&g_skill_action_trace_on, 0, 0) &&
      !InterlockedCompareExchange(&g_skill_refill_suppress_on, 0, 0) &&
      !InterlockedCompareExchange(&g_kuangfeng_tail_auto_on, 0, 0) &&
      !InterlockedCompareExchange(&g_youfeng_ultimate_tail_auto_on, 0, 0)) {
    if (original)
      original(self, event_ptr, a02, a03, a04, a05, a06, a07, a08, a09);
    return;
  }
  SkillActionTraceEntry e = {};
  e.kind = SKTRACE_PERFORM;
  e.tick = GetTickCount();
  e.thread_id = GetCurrentThreadId();
  e.caller = (uint32_t)(uintptr_t)_ReturnAddress();
  e.self_ptr = (uint32_t)(uintptr_t)self;
  e.skill_ptr = (uint32_t)(uintptr_t)event_ptr;
  e.arg1 = a02;
  e.arg2 = a03;
  e.arg3 = a04;
  ReadCastTraceFields(self, false, &e);
  __try {
    if (event_ptr) {
      uint32_t* event_words = (uint32_t*)event_ptr;
      e.skill_0 = event_words[0];
      e.skill_4 = event_words[1];
      e.skill_10 = event_words[2];
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  const uint32_t target_skill = (uint32_t)InterlockedCompareExchange(
      &g_skill_refill_target_skill, 0, 0);
  const uint32_t target_config = (uint32_t)InterlockedCompareExchange(
      &g_skill_refill_target_config, 0, 0);
  const bool suppress =
      InterlockedCompareExchange(&g_skill_refill_suppress_on, 0, 0) &&
      (!target_skill || e.skill_0 == target_skill) &&
      (!target_config || e.skill_10 == target_config);
  if (suppress) {
    InterlockedExchange(&g_skill_refill_restop_cast,
                        (LONG)(uint32_t)(uintptr_t)self);
    InterlockedExchange(&g_skill_refill_restop_pending, 1);
  }
  if (original && !suppress) {
    original(self, event_ptr, a02, a03, a04, a05, a06, a07, a08, a09);
  }
  e.ret = suppress ? 1 : 0;
  ReadCastTraceFields(self, true, &e);
  StoreSkillActionTrace(&e);

  if (!suppress &&
      InterlockedCompareExchange(&g_kuangfeng_tail_auto_on, 0, 0)) {
    const DWORD due = (DWORD)InterlockedCompareExchange(
        &g_kuangfeng_tail_auto_due, 0, 0);
    if ((LONG)(GetTickCount() - due) >= 0) {
      InterlockedExchange(&g_kuangfeng_tail_auto_on, 0);
    } else {
      bool match = false;
      __try {
        uint8_t* cast = (uint8_t*)self;
        match = cast && e.skill_0 == 0x952Au && e.skill_10 == 0x176Du &&
                a02 == 1u && a04 == 350u &&
                *(uint32_t*)(cast + 0x10) == 0x952Au &&
                *(uint32_t*)(cast + 0x18) == 0x176Du && a03 != 0u &&
                a03 != 0xFFFFFFFFu;
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        match = false;
      }
      if (match) {
        if (InterlockedCompareExchange(&g_kuangfeng_tail_pending, 1, 0) == 0) {
          InterlockedExchange(&g_kuangfeng_tail_pending_cast,
                              (LONG)(uint32_t)(uintptr_t)self);
          InterlockedExchange(&g_kuangfeng_tail_pending_perform, (LONG)a03);
          InterlockedExchange(&g_kuangfeng_tail_pending_due,
                              (LONG)(GetTickCount() + 320u));
        }
      }
    }
  }
  if (!suppress &&
      InterlockedCompareExchange(&g_youfeng_ultimate_tail_auto_on, 0, 0)) {
    bool match = false;
    __try {
      uint8_t* cast = (uint8_t*)self;
      match = cast && e.skill_0 == 0x9563u && e.skill_10 == 0x195Eu &&
              a02 == 1u && a04 == 1600u &&
              *(uint32_t*)(cast + 0x10) == 0x9563u &&
              *(uint32_t*)(cast + 0x18) == 0x195Eu && a03 != 0u &&
              a03 != 0xFFFFFFFFu;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      match = false;
    }
    if (match &&
        InterlockedCompareExchange(&g_youfeng_ultimate_tail_pending, 1, 0) == 0) {
      InterlockedExchange(&g_youfeng_ultimate_tail_pending_cast,
                          (LONG)(uint32_t)(uintptr_t)self);
      InterlockedExchange(&g_youfeng_ultimate_tail_pending_perform, (LONG)a03);
      InterlockedExchange(&g_youfeng_ultimate_tail_pending_due,
                          (LONG)(GetTickCount() + 400u));
    }
  }
}

static bool InstallSkillActionTraceHooks() {
  if (g_hk_action_request_trace.active && g_hk_set_active_trace.active &&
      g_hk_on_perform_trace.active && g_hk_action_cancast_trace.active &&
      g_hk_host_start_session_trace.active &&
      g_hk_arcfour_update_trace.active)
    return true;
  uint8_t* request =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteSkillActionRequest);
  uint8_t* core = (uint8_t*)(uintptr_t)NoteToLive(kNoteCastCore);
  uint8_t* active =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteSetCurActiveSkill);
  uint8_t* perform =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteOnPerformSkill);
  uint8_t* session_send =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteHostStartSession);
  uint8_t* arcfour_update =
      (uint8_t*)(uintptr_t)NoteToLive(kNoteARCFourUpdate);
  __try {
    // 762F10: sub esp,50; cmp dword ptr [esp+6C],2
    if (!request || request[0] != 0x83 || request[1] != 0xEC ||
        request[2] != 0x50 || request[3] != 0x83 || request[4] != 0x7C ||
        request[5] != 0x24 || request[6] != 0x6C || request[7] != 0x02)
      return false;
    // 75F000: sub esp,10; mov eax,[esp+40]
    if (!core || core[0] != 0x83 || core[1] != 0xEC || core[2] != 0x10 ||
        core[3] != 0x8B || core[4] != 0x44 || core[5] != 0x24 ||
        core[6] != 0x40)
      return false;
    // 758DF0: mov eax,fs:[0]
    if (!active || active[0] != 0x64 || active[1] != 0xA1 ||
        active[2] != 0x00 || active[3] != 0x00 || active[4] != 0x00 ||
        active[5] != 0x00)
      return false;
    // 761330: push -1; push 0x1124350
    if (!perform || perform[0] != 0x6A || perform[1] != 0xFF ||
        perform[2] != 0x68 || perform[3] != 0x50 ||
        perform[4] != 0x43 || perform[5] != 0x12 ||
        perform[6] != 0x01)
      return false;
    // 7550B0: mov eax,[esp+30]; mov edx,[esp+2C]
    if (!session_send || session_send[0] != 0x8B ||
        session_send[1] != 0x44 || session_send[2] != 0x24 ||
        session_send[3] != 0x30 || session_send[4] != 0x8B ||
        session_send[5] != 0x54 || session_send[6] != 0x24 ||
        session_send[7] != 0x2C)
      return false;
    // DAFAF0: push ecx; mov eax,[esp+8]; mov edx,[eax+8]
    if (!arcfour_update || arcfour_update[0] != 0x51 ||
        arcfour_update[1] != 0x8B || arcfour_update[2] != 0x44 ||
        arcfour_update[3] != 0x24 || arcfour_update[4] != 0x08 ||
        arcfour_update[5] != 0x8B || arcfour_update[6] != 0x50 ||
        arcfour_update[7] != 0x08)
      return false;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  if (!InstallInlineDetour(&g_hk_action_request_trace, request, 8,
                           (void*)&Hook_SkillActionRequestTrace))
    return false;
  if (!InstallInlineDetour(&g_hk_action_cancast_trace, core, 7,
                           (void*)&Hook_ActionCanCastTrace)) {
    return false;
  }
  if (!InstallInlineDetour(&g_hk_set_active_trace, active, 6,
                           (void*)&Hook_SetCurActiveSkillTrace)) {
    return false;
  }
  if (!InstallInlineDetour(&g_hk_on_perform_trace, perform, 7,
                           (void*)&Hook_OnPerformSkillTrace)) {
    return false;
  }
  if (!InstallInlineDetour(&g_hk_host_start_session_trace, session_send, 8,
                           (void*)&Hook_HostStartSessionTrace)) {
    return false;
  }
  if (!InstallInlineDetour(&g_hk_arcfour_update_trace, arcfour_update, 8,
                           (void*)&Hook_ARCFourUpdateTrace)) {
    return false;
  }
  return true;
}

static void StopSkillActionTraceHooks() {
  InterlockedExchange(&g_skill_action_trace_on, 0);
  InterlockedExchange(&g_skill_refill_suppress_on, 0);
  InterlockedExchange(&g_skill_refill_suppress_mode, 0);
  InterlockedExchange(&g_skill_refill_restop_pending, 0);
  // The hooks become transparent through the flags above. Keep their code and
  // trampolines resident to avoid racing live game threads during hot unpatch.
}

static const char* SkillActionTraceKindName(uint32_t kind) {
  switch (kind) {
    case SKTRACE_REQUEST: return "request";
    case SKTRACE_PERFORM: return "perform";
    case SKTRACE_CORE: return "core";
    case SKTRACE_RESTOP: return "restop";
    case SKTRACE_NEXT_GATE: return "next_gate";
    case SKTRACE_FRESH_GATE: return "fresh_gate";
    case SKTRACE_SESSION_SEND: return "session_send";
    case SKTRACE_EXACT_CANCEL: return "exact_cancel";
    case SKTRACE_PHASE_GATE: return "phase_gate";
    case SKTRACE_QINGGONG_GATE: return "qinggong_gate";
    case SKTRACE_QINGGONG_START: return "qinggong_start";
    case SKTRACE_QINGGONG_STOP: return "qinggong_stop";
    default: return "active";
  }
}

static bool DumpSkillActionTrace(char* path_out, size_t path_n) {
  if (path_out && path_n) path_out[0] = 0;
  char dir[MAX_PATH] = {0};
  if (!ResolveSoftwareLogsDir(dir, sizeof(dir))) return false;
  SYSTEMTIME st = {};
  GetLocalTime(&st);
  char path[MAX_PATH] = {0};
  _snprintf_s(path, _TRUNCATE,
              "%sskill_action_trace_%04u%02u%02u_%02u%02u%02u_%lu.tsv",
              dir, (unsigned)st.wYear, (unsigned)st.wMonth,
              (unsigned)st.wDay, (unsigned)st.wHour, (unsigned)st.wMinute,
              (unsigned)st.wSecond, (unsigned long)GetCurrentProcessId());
  FILE* f = nullptr;
  if (fopen_s(&f, path, "wb") != 0 || !f) return false;
  fprintf(f,
          "seq\tkind\ttick\tthread\tcaller\tself\tskill\ta1\ta2\ta3\tret"
          "\ts0\ts4\ts10\ts18\tsea4\tc10b\tc18b\tc4a0b\tc10a\tc18a\tc4a0a"
          "\tss0\tss1\tss2\tss3\tss4\tss5\tss6\tss7\tss8\tss9\tss10\tss11\n");
  LONG n = InterlockedCompareExchange(&g_skill_action_trace_count, 0, 0);
  if (n > kMaxSkillActionTrace) n = kMaxSkillActionTrace;
  for (LONG i = 0; i < n; ++i) {
    const SkillActionTraceEntry& e = g_skill_action_trace[i];
    fprintf(f,
            "%u\t%s\t%u\t%u\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t%d"
            "\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X"
            "\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\t0x%08X\n",
            e.seq, SkillActionTraceKindName(e.kind),
            e.tick,
            e.thread_id, e.caller, e.self_ptr, e.skill_ptr, e.arg1, e.arg2,
            e.arg3, e.ret, e.skill_0, e.skill_4, e.skill_10, e.skill_18,
            e.skill_ea4, e.cast_10_before, e.cast_18_before,
            e.cast_4a0_before, e.cast_10_after, e.cast_18_after,
            e.cast_4a0_after, e.session_args[0], e.session_args[1],
            e.session_args[2], e.session_args[3], e.session_args[4],
            e.session_args[5], e.session_args[6], e.session_args[7],
            e.session_args[8], e.session_args[9], e.session_args[10],
            e.session_args[11]);
  }
  fclose(f);
  char packet_path[MAX_PATH] = {0};
  _snprintf_s(packet_path, _TRUNCATE,
              "%sskill_action_trace_%04u%02u%02u_%02u%02u%02u_%lu.packets.tsv",
              dir, (unsigned)st.wYear, (unsigned)st.wMonth,
              (unsigned)st.wDay, (unsigned)st.wHour, (unsigned)st.wMinute,
              (unsigned)st.wSecond, (unsigned long)GetCurrentProcessId());
  FILE* pf = nullptr;
  if (fopen_s(&pf, packet_path, "wb") != 0 || !pf) return false;
  fprintf(pf, "seq\ttick\tthread\tcaller\tself\tlength\tcaptured\tpayload\n");
  LONG pn = InterlockedCompareExchange(&g_skill_packet_trace_count, 0, 0);
  if (pn > kMaxSkillPacketTrace) pn = kMaxSkillPacketTrace;
  for (LONG i = 0; i < pn; ++i) {
    const SkillPacketTraceEntry& e = g_skill_packet_trace[i];
    fprintf(pf, "%u\t%u\t%u\t0x%08X\t0x%08X\t%u\t%u\t",
            e.seq, e.tick, e.thread_id, e.caller, e.self_ptr, e.length,
            e.captured);
    for (uint32_t j = 0; j < e.captured; ++j)
      fprintf(pf, "%02X", (unsigned)e.payload[j]);
    fputc('\n', pf);
  }
  fclose(pf);
  if (path_out && path_n) _snprintf_s(path_out, path_n, _TRUNCATE, "%s", path);
  return true;
}

static void FormatSkillActionTrace(char* buf, size_t n) {
  if (!buf || !n) return;
  LONG total = InterlockedCompareExchange(&g_skill_action_trace_count, 0, 0);
  LONG dropped = InterlockedCompareExchange(&g_skill_action_trace_dropped, 0, 0);
  _snprintf_s(buf, n, _TRUNCATE,
              "SKILL_ACTION_TRACE n=%ld dropped=%ld packets=%ld packet_drop=%ld armed=%d request=%d core_action=%d active=%d perform=%d session_send=%d rc4=%d suppress=%ld mode=%ld",
              total, dropped,
              InterlockedCompareExchange(&g_skill_packet_trace_count, 0, 0),
              InterlockedCompareExchange(&g_skill_packet_trace_dropped, 0, 0),
              g_skill_action_trace_on ? 1 : 0,
              g_hk_action_request_trace.active ? 1 : 0,
              g_hk_action_cancast_trace.active ? 1 : 0,
              g_hk_set_active_trace.active ? 1 : 0,
              g_hk_on_perform_trace.active ? 1 : 0,
              g_hk_host_start_session_trace.active ? 1 : 0,
              g_hk_arcfour_update_trace.active ? 1 : 0,
              InterlockedCompareExchange(&g_skill_refill_suppress_on, 0, 0),
              InterlockedCompareExchange(&g_skill_refill_suppress_mode, 0, 0));
}

static void* ResolveAutoplayThis() {
  uint32_t gva = NoteToLive(kNoteGameRootGlobal);
  if (!gva) return nullptr;
  __try {
    uint32_t root = *(uint32_t*)(uintptr_t)gva;
    if (!root) return nullptr;
    uint32_t mid = *(uint32_t*)(uintptr_t)(root + kGameRootMidOff);
    if (!mid) return nullptr;
    uint32_t autoplay = *(uint32_t*)(uintptr_t)(mid + kMidAutoplayThisOff);
    return (void*)(uintptr_t)autoplay;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return nullptr;
  }
}

static void* ResolveInteractThis() {
  uint32_t va = NoteToLive(kNoteGetHostSide);
  if (!va) return nullptr;
  FnGetHostSide get_side = (FnGetHostSide)(uintptr_t)va;
  void* side = nullptr;
  __try {
    side = get_side();
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return nullptr;
  }
  if (!side) return nullptr;
  __try {
    return *(void**)((uint8_t*)side + kHostSideThisOff);
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return nullptr;
  }
}

// -- AUI caption dump helpers (UI-thread direct read) -----------------------

// AUI GetDlgItem thiscall: ecx=dlg, stack=[const char* name] -> AUIObject*.
typedef void* (__fastcall* FnAuiGetDlgItem)(void* dlg, void* edx, const char* name);

static bool ReadCString(void* addr, char* out, uint32_t cap) {
  if (!addr || !out || cap == 0) return false;
  __try {
    const char* p = (const char*)addr;
    uint32_t i = 0;
    while (i + 1 < cap && p[i]) {
      out[i] = p[i];
      ++i;
    }
    out[i] = 0;
    return i > 0;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
}

// True when the wchar buffer looks like real display text (CJK / printable),
// not raw 0xCC heap noise or garbage pairs.
static bool WcharLooksLikeText(const wchar_t* w, uint32_t n) {
  if (!w || n == 0) return false;
  uint32_t ok = 0;
  for (uint32_t i = 0; i < n; ++i) {
    wchar_t c = w[i];
    if (c == 0) break;
    if (c == L'\n' || c == L'\r' || c == L'\t') continue;
    if ((c >= 0x4E00 && c <= 0x9FFF) || (c >= 0x3000 && c <= 0x303F) ||
        (c >= 0xFF00 && c <= 0xFFEF) || (c >= 0x20 && c <= 0x7E)) {
      ++ok;
    }
  }
  return ok >= 2;
}

// Read a caption near ctrl+off. Handles inline UTF-16, pointer-to-UTF-16 and
// ACString (ptr to a buffer whose first dword may itself be a pointer).
static bool ReadAuiCaption(void* ctrl, uint32_t off, char* out, uint32_t cap) {
  if (!ctrl || !out || cap == 0) return false;
  __try {
    const uint8_t* base = (const uint8_t*)ctrl;
    uint32_t maybe = *(uint32_t*)(base + off);
    void* cands[4] = {nullptr, nullptr, nullptr, nullptr};
    uint32_t nc = 0;
    // 1) direct inline wchar at the field
    cands[nc++] = (void*)(uintptr_t)&base[off];
    // 2) field is a pointer to wchar
    if (maybe >= 0x10000u && maybe < 0x7FFE0000u) {
      cands[nc++] = (void*)(uintptr_t)maybe;
      // 3) pointer to ACString whose first dword is the wchar pointer
      uint32_t p2 = *(uint32_t*)(uintptr_t)maybe;
      if (p2 >= 0x10000u && p2 < 0x7FFE0000u) {
        cands[nc++] = (void*)(uintptr_t)p2;
      }
    }
    for (uint32_t ci = 0; ci < nc; ++ci) {
      const wchar_t* w = (const wchar_t*)cands[ci];
      uint32_t n = 0;
      __try {
        while (n < 64 && w[n] != 0) ++n;
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        continue;
      }
      if (n == 0 || !WcharLooksLikeText(w, n)) continue;
      int need = WideCharToMultiByte(CP_UTF8, 0, w, (int)n, nullptr, 0, nullptr,
                                     nullptr);
      if (need <= 0 || (uint32_t)need >= cap) continue;
      WideCharToMultiByte(CP_UTF8, 0, w, (int)n, out, (int)(cap - 1), nullptr,
                          nullptr);
      out[need] = 0;
      return true;
    }
    return false;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
}

// Append a line to the shared err buffer (UTF-8), truncating safely.
static void AppendErr(const char* s) {
  if (!g_shm || !s) return;
  char buf[160];
  _snprintf_s(buf, sizeof(buf), _TRUNCATE, "%s%s",
              g_shm->err[0] ? "\n" : "", s);
  strncat_s(g_shm->err, sizeof(g_shm->err), buf, _TRUNCATE);
}

static void* DumpUiDlg(const char* dlg_name) {
  FnGetGameUIDlg get_dlg =
      (FnGetGameUIDlg)GetExport("?GetGameUIDlg@plg@@YAPAVAUIDialog@@PBD@Z");
  if (!get_dlg) return nullptr;
  void* dlg = nullptr;
  __try {
    dlg = get_dlg(dlg_name);
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return nullptr;
  }
  return dlg;
}

// Dump readable UTF-16 text found across a dialog object and (shallow) its
// child controls. Instead of trusting fixed caption offsets, scan the object
// buffer inline and follow plausible pointers.
static void DumpDlgText(void* dlg, uint32_t depth) {
  if (!dlg || depth > 2) return;
  char tmp[160];
  const uint8_t* base = (const uint8_t*)dlg;
  // 1) inline UTF-16 scan over the first 0x600 bytes (2-byte aligned).
  __try {
    for (uint32_t off = 0; off + 2 <= 0x600; off += 2) {
      const wchar_t* w = (const wchar_t*)(base + off);
      uint32_t n = 0;
      while (n < 64 && w[n] != 0) ++n;
      if (n < 3) continue;
      if (!WcharLooksLikeText(w, n)) continue;
      int need = WideCharToMultiByte(CP_UTF8, 0, w, (int)n, nullptr, 0, nullptr,
                                     nullptr);
      if (need <= 0 || (uint32_t)need >= (uint32_t)sizeof(tmp)) continue;
      WideCharToMultiByte(CP_UTF8, 0, w, (int)n, tmp, (int)(sizeof(tmp) - 1),
                          nullptr, nullptr);
      tmp[need] = 0;
      AppendErr(tmp);
      off += (uint32_t)n * 2;  // skip consumed text
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  // 2) follow pointer fields (4-byte aligned) to readable UTF-16.
  __try {
    for (uint32_t off = 0; off + 4 <= 0x600; off += 4) {
      uint32_t maybe = *(uint32_t*)(base + off);
      if (maybe < 0x10000u || maybe >= 0x7FFE0000u) continue;
      const wchar_t* w = (const wchar_t*)(uintptr_t)maybe;
      uint32_t n = 0;
      __try {
        while (n < 64 && w[n] != 0) ++n;
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        continue;
      }
      if (n < 3 || !WcharLooksLikeText(w, n)) continue;
      int need = WideCharToMultiByte(CP_UTF8, 0, w, (int)n, nullptr, 0, nullptr,
                                     nullptr);
      if (need <= 0 || (uint32_t)need >= (uint32_t)sizeof(tmp)) continue;
      WideCharToMultiByte(CP_UTF8, 0, w, (int)n, tmp, (int)(sizeof(tmp) - 1),
                          nullptr, nullptr);
      tmp[need] = 0;
      AppendErr(tmp);
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  // 3) named child controls via GetDlgItem (shallow).
  uint32_t va = NoteToLive(kNoteAuiGetDlgItem);
  if (!va) return;
  FnAuiGetDlgItem get_item = (FnAuiGetDlgItem)(uintptr_t)va;
  const char* names[] = {"Lst_Main",    "Lst_Content", "Lst_Item",
                         "Txt_Content", "Txt_Talk",    "Txt_Info",
                         "Txt_Task",    "Lab_Task",    "Txt_Stage"};
  for (uint32_t ni = 0;
       ni < sizeof(names) / sizeof(names[0]) && depth == 1; ++ni) {
    void* ctrl = nullptr;
    __try {
      ctrl = get_item(dlg, nullptr, names[ni]);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      ctrl = nullptr;
    }
    if (!ctrl) continue;
    DumpDlgText(ctrl, depth + 1);
  }
}

static void RunCommand() {
  if (!g_shm || g_shm->magic != BRIDGE_MAGIC) return;
  const uint32_t incoming_cmd = g_shm->cmd;
  char command_text[128] = {};
  if (incoming_cmd == CMD_OBJECT_SCAN) {
    strncpy_s(command_text, g_shm->err, _TRUNCATE);
  }
  g_shm->status = ST_PENDING;
  g_shm->ret = 0;
  g_shm->err[0] = 0;

  const uint32_t cmd = incoming_cmd;
  const uint32_t lo = g_shm->id_lo;
  const uint32_t hi = g_shm->id_hi;

  __try {
    if (cmd == CMD_PING) {
      g_shm->ret = (int32_t)BRIDGE_BUILD_ID;
      g_shm->status = ST_OK;
      {
        char pong[64];
        _snprintf_s(pong, sizeof(pong), _TRUNCATE, "pong build=%u",
                    (unsigned)BRIDGE_BUILD_ID);
        SetErr(pong);
      }
      return;
    }

    if (!g_game) {
      g_game = GetModuleHandleW(L"xajh.exe");
      if (!g_game) g_game = GetModuleHandleW(nullptr);
    }

    if (CommandRequiresFixedRva(cmd) && !IsKnownFixedRvaBuild()) {
      SetErr("UNSUPPORTED_CLIENT_BUILD");
      g_shm->status = ST_ERR;
      return;
    }
    if (g_shm->module_base == 0 && g_game) {
      g_shm->module_base = (uint32_t)(uintptr_t)g_game;
    }

    if (cmd == CMD_OBJECT_SCAN) {
      // Read-only cursor-based AOI snapshot. mode low byte selects the filter:
      // 0=all, 1=template TID (g_shm->tid), 2=UTF-8 name substring (err input).
      // mode high bytes carry the cursor. Each successful row returns ret=1 and
      // the next cursor in mode; ret=0 means the snapshot is exhausted.
      FnGetObjectCount get_count =
          (FnGetObjectCount)GetExport("?GetObjectCount@plg@@YAHW4OBJECT_CLASSID@1@@Z");
      FnGetObjects get_objects =
          (FnGetObjects)GetExport("?GetObjects@plg@@YAHW4OBJECT_CLASSID@1@PAXI@Z");
      FnGetObjectName get_name =
          (FnGetObjectName)GetExport("?GetObjectName@plg@@YAPB_WPAX@Z");
      FnGetObjectTemplateId get_tid =
          (FnGetObjectTemplateId)GetExport("?GetObjectTemplateID@plg@@YAHPAX@Z");
      FnGetObjectId get_id =
          (FnGetObjectId)GetExport("?GetObjectID@plg@@YA_JPAX@Z");
      if (!get_count || !get_objects || !get_name || !get_tid || !get_id) {
        SetErr("OBJECT_SCAN export missing");
        g_shm->status = ST_ERR;
        return;
      }
      const uint32_t selector = g_shm->mode & 0xFFu;
      const uint32_t cursor = g_shm->mode >> 8;
      const int wanted_tid = (int)g_shm->tid;
      wchar_t wanted_name[128] = {};
      if (selector == 2 && command_text[0]) {
        MultiByteToWideChar(CP_UTF8, 0, command_text, -1, wanted_name,
                            (int)(sizeof(wanted_name) / sizeof(wanted_name[0])));
      }
      g_shm->id_lo = 0;
      g_shm->id_hi = 0;
      g_shm->tid = 0;
      g_shm->x = 0.0f;
      g_shm->y = 0.0f;
      g_shm->z = 0.0f;
      void* found = nullptr;
      int found_class = -1;
      uint32_t next_cursor = cursor;
      __try {
        uint32_t seen = 0;
        // class 2 is NPC/monster; class 1 is Matter. The cursor spans both
        // lists so callers can fetch one stable snapshot without CRT storms.
        for (int class_id = 2; class_id >= 1 && !found; --class_id) {
          int count = get_count(class_id);
          if (count <= 0) continue;
          if (count > 512) count = 512;
          void* objects[512] = {};
          int returned = get_objects(class_id, objects, (uint32_t)count);
          if (returned < 0) returned = 0;
          if (returned > count) returned = count;
          for (int i = 0; i < returned; ++i, ++seen) {
            void* object = objects[i];
            if (!object || seen < cursor) continue;
            const wchar_t* name = get_name(object);
            const int object_tid = get_tid(object);
            if (selector == 1 && object_tid != wanted_tid) continue;
            if (selector == 2 && (!name || !wanted_name[0] || !wcsstr(name, wanted_name))) continue;
            const long long object_id = get_id(object);
            if (!object_id) continue;
            found = object;
            found_class = class_id;
            next_cursor = seen + 1;
            const uint64_t oid = (uint64_t)object_id;
            g_shm->id_lo = (uint32_t)(oid & 0xFFFFFFFFu);
            g_shm->id_hi = (uint32_t)(oid >> 32);
            g_shm->tid = object_tid;
            const float* pos = (const float*)((const uint8_t*)object + 0x158);
            g_shm->x = pos[0];
            g_shm->y = pos[1];
            g_shm->z = pos[2];
            break;
          }
        }
        if (!found) next_cursor = seen;
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("OBJECT_SCAN read exception");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->mode = (int32_t)next_cursor;
      if (!found) {
        SetErr("OBJECT_SCAN end");
        g_shm->status = ST_OK;
        return;
      }
      g_shm->ret = 1;
      g_shm->status = ST_OK;
      char note[160];
      sprintf_s(note, "OBJECT_SCAN ok class=%d obj=%08X:%08X tid=%d next=%u",
                found_class, (unsigned)g_shm->id_hi, (unsigned)g_shm->id_lo,
                (int)g_shm->tid, (unsigned)next_cursor);
      SetErr(note);
      return;
    }

    if (cmd == CMD_HOST_CONTEXT) {
      // Lifecycle-only probe.  Run GetHostSide on the game's UI thread rather
      // than creating a foreign remote thread while gameplay is active.
      uint32_t va = NoteToLive(kNoteGetHostSide);
      if (!va) {
        SetErr("HOST_CONTEXT va=0");
        g_shm->status = ST_ERR;
        return;
      }
      FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)va;
      void* host = get_host();
      if (!host) {
        // Role selection destroys the input/controller graph.  Never let the
        // timer maintain forced keys through the cached pointer from the old
        // character instance.
        InterlockedExchange(&g_force_shift, 0);
        for (int i = 0; i < 256; ++i) g_force_vk[i] = 0;
        RecountForceAny();
        g_input_this = nullptr;
      }
      g_shm->ret = host ? 1 : 0;
      g_shm->status = ST_OK;
      return;
    }

    if (cmd == CMD_HOST_SNAPSHOT) {
      // Keep lifecycle check and scene read in one UI-thread dispatch. Role
      // selection cannot destroy the host graph in between these two calls.
      // tid is a tri-state death result: -1=unknown, 0=alive, 1=dead.
      g_shm->tid = -1;
      g_shm->id_lo = 0;
      g_shm->id_hi = 0;
      uint32_t va = NoteToLive(kNoteGetHostSide);
      if (!va) {
        SetErr("HOST_SNAPSHOT host_va=0");
        g_shm->status = ST_ERR;
        return;
      }
      FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)va;
      void* host = get_host();
      if (!host) {
        InterlockedExchange(&g_force_shift, 0);
        for (int i = 0; i < 256; ++i) g_force_vk[i] = 0;
        RecountForceAny();
        g_input_this = nullptr;
        g_shm->ret = 0;
        g_shm->status = ST_OK;
        SetErr("HOST_SNAPSHOT no_host");
        return;
      }
      const uint32_t host_state =
          *(uint32_t*)((uint8_t*)host + kHostDeadStateOff);
      // Expose the injected character identity through the bridge response.
      // The UI can bind SessionStore before opening any role-scoped page or
      // startup runner, without relying on a foreign remote CRT call.
      __try {
        g_shm->id_lo = *(uint32_t*)((uint8_t*)host + kHostPlayerIdLoOff);
        g_shm->id_hi = *(uint32_t*)((uint8_t*)host + kHostPlayerIdHiOff);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("HOST_SNAPSHOT host id read fail");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->tid = (host_state & 2u) ? 1 : 0;
      FnGetScenePosition get_scene = (FnGetScenePosition)GetExport(
          "?GetCurrentScenePosition@plg@@YAXPAHPAM11@Z");
      if (!get_scene) {
        SetErr("HOST_SNAPSHOT scene export missing");
        g_shm->status = ST_ERR;
        return;
      }
      int scene = 0;
      float x = 0.0f, y = 0.0f, z = 0.0f;
      get_scene(&scene, &x, &y, &z);
      g_shm->mode = scene;
      g_shm->x = x;
      g_shm->y = y;
      g_shm->z = z;
      g_shm->ret = 1;
      g_shm->status = ST_OK;
      char buf[64];
      sprintf_s(buf, "HOST_SNAPSHOT ok dead=%d", (int)g_shm->tid);
      SetErr(buf);
      return;
    }

    if (cmd == CMD_DUMP_UI_TEXT) {
      // Direct UI-thread read of dungeon stage target text. Bypasses the
      // scene-settle CRT gate because it runs inside the game process.
      g_shm->ret = 0;
      g_shm->err[0] = 0;
      const uint32_t sel = lo & 0xFFu;
      const char* dlg_names[2] = {"Win_InstanceConfig", "Win_TrackFrame"};
      int found = 0;
      for (uint32_t d = 0; d < 2; ++d) {
        if (sel != 2 && sel != d) continue;
        void* dlg = DumpUiDlg(dlg_names[d]);
        if (!dlg) {
          AppendErr("NO_DLG");
          continue;
        }
        DumpDlgText(dlg, 1);
        ++found;
      }
      g_shm->ret = found;
      if (!g_shm->err[0]) SetErr("EMPTY");
      g_shm->status = ST_OK;
      return;
    }

    if (cmd == CMD_TARGET_SUBMIT_TRACE) {
      // Trace is passive by default. mode=3 enables the proven automatic
      // candidate routes (submit + queue refresh + state apply).
      const int mode = g_shm->mode;
      if (mode == 1 || mode == 3) {
        InterlockedExchange(&g_target_submit_trace_on, 0);
        InterlockedExchange(&g_target_submit_guard_on, 0);
        InterlockedExchange(&g_target_submit_guard_hits, 0);
        InterlockedExchange(&g_target_queue_guard_hits, 0);
        InterlockedExchange(&g_target_state_guard_hits, 0);
        InterlockedExchange(&g_target_queue_refresh_guard_hits, 0);
        InterlockedExchange(&g_target_arm_sanitize_hits, 0);
        memset(g_target_submit_trace, 0, sizeof(g_target_submit_trace));
        InterlockedExchange(&g_target_submit_trace_count, 0);
        if (!InstallTargetSubmitTraceHook() ||
            !InstallTargetQueuePromoteHook() ||
            !InstallTargetQueueRefreshHook() ||
            !InstallTargetStateApplyHook()) {
          SetErr("TARGET_SUBMIT_TRACE hook install/signature failed");
          g_shm->status = ST_ERR;
          return;
        }
        InterlockedExchange(&g_target_submit_trace_on, 1);
        if (mode == 3) {
          InterlockedExchange(&g_target_submit_guard_on, 1);
          SanitizeArmedDungeonTarget();
        }
      }

      if (mode == 4)
        InterlockedExchange(&g_target_submit_guard_on, 0);

      if (mode == 0) {
        InterlockedExchange(&g_target_submit_trace_on, 0);
        InterlockedExchange(&g_target_submit_guard_on, 0);
      }

      TargetSubmitTraceEntry latest = {};
      const bool have_latest = ReadLatestTargetSubmitTrace(&latest);
      const LONG count =
          InterlockedCompareExchange(&g_target_submit_trace_count, 0, 0);
      g_shm->ret = (int32_t)count;
      if (have_latest) {
        g_shm->id_lo = latest.target_lo;
        g_shm->id_hi = latest.target_hi;
        g_shm->tid = (int32_t)latest.thread_id;
        g_shm->mode = (int32_t)latest.caller;
      }

      if (mode == 0) {
        char path[MAX_PATH] = {0};
        if (!DumpTargetSubmitTrace(path, sizeof(path))) {
          SetErr("TARGET_SUBMIT_TRACE dump failed");
          g_shm->status = ST_ERR;
          return;
        }
        char buf[128] = {0};
        _snprintf_s(buf, _TRUNCATE,
                    "TARGET_SUBMIT_TRACE stopped n=%ld dump=%s", count,
                    path);
        SetErr(buf);
      } else {
        char buf[128] = {0};
        FormatTargetSubmitTrace(buf, sizeof(buf));
        char note[192] = {0};
        _snprintf_s(note, sizeof(note), _TRUNCATE,
                    "%s guard=%d submit_hits=%ld queue_hits=%ld refresh_hits=%ld state_hits=%ld arm_hits=%ld", buf,
                    InterlockedCompareExchange(&g_target_submit_guard_on, 0, 0)
                        ? 1
                        : 0,
                    InterlockedCompareExchange(&g_target_submit_guard_hits, 0,
                                               0),
                    InterlockedCompareExchange(&g_target_queue_guard_hits, 0,
                                               0),
                    InterlockedCompareExchange(
                        &g_target_queue_refresh_guard_hits, 0, 0),
                    InterlockedCompareExchange(&g_target_state_guard_hits, 0,
                                               0),
                    InterlockedCompareExchange(&g_target_arm_sanitize_hits, 0,
                                               0));
        SetErr(note);
      }
      g_shm->status = ST_OK;
      return;
    }

    if (cmd == CMD_DUNGEON_TARGET_RULES) {
      const int mode = g_shm->mode;
      if (mode == 1) {
        ClearDungeonTargetTids();
      } else if (mode == 2) {
        if (!AddDungeonTargetTid(lo)) {
          SetErr(lo ? "DUNGEON_TARGET_RULES full"
                    : "DUNGEON_TARGET_RULES tid=0");
          g_shm->status = ST_ERR;
          return;
        }
      } else if (mode != 3) {
        SetErr("DUNGEON_TARGET_RULES mode invalid");
        g_shm->status = ST_ERR;
        return;
      }
      const LONG count =
          InterlockedCompareExchange(&g_dungeon_target_tid_count, 0, 0);
      g_shm->ret = count;
      char note[96] = {0};
      _snprintf_s(note, sizeof(note), _TRUNCATE,
                  "DUNGEON_TARGET_RULES count=%ld max=%u", count,
                  (unsigned)kMaxDungeonTargetTids);
      SetErr(note);
      g_shm->status = ST_OK;
      return;
    }

    if (cmd == CMD_AUTOPLAY_STOP) {
      // Function stop only: no 0x16 packet and no Alt+R. The function updates
      // local autoplay/UI state immediately; server acknowledgement is not
      // part of this operation.
      void* autoplay = ResolveAutoplayThis();
      if (!autoplay) {
        SetErr("AUTOPLAY_STOP this=null");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t va = NoteToLive(kNoteStopAutoplay);
      if (!va) {
        SetErr("AUTOPLAY_STOP va=0");
        g_shm->status = ST_ERR;
        return;
      }
      FnStopAutoplay fn = (FnStopAutoplay)(uintptr_t)va;
      uint32_t call_ret = (uint32_t)fn(autoplay, lo & 0xFFu);
      g_shm->ret = (int32_t)call_ret;
      g_shm->status = ST_OK;
      char buf[96];
      sprintf_s(buf, "AUTOPLAY_STOP ok ret=0x%X reason=%u", (unsigned)call_ret,
                (unsigned)(lo & 0xFFu));
      SetErr(buf);
      return;
    }

    if (cmd == CMD_AUTOPLAY_START_BYPASS) {
      FnGetGameUIDlg get_dlg =
          (FnGetGameUIDlg)GetExport("?GetGameUIDlg@plg@@YAPAVAUIDialog@@PBD@Z");
      void* frame = get_dlg ? get_dlg("Win_AutoPlayFrame") : nullptr;
      if (!frame) {
        SetErr("AUTOPLAY_START_BYPASS frame=null");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t expected_vtable = NoteToLive(kNoteAutoPlayFrameVtable);
      if (!expected_vtable || *(uint32_t*)frame != expected_vtable) {
        SetErr("AUTOPLAY_START_BYPASS frame type mismatch");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t fn_va = NoteToLive(kNoteAutoPlayFrameBtnStart);
      if (!fn_va) {
        SetErr("AUTOPLAY_START_BYPASS handler va=0");
        g_shm->status = ST_ERR;
        return;
      }

      bool skill1 = false;
      bool skill2 = false;
      bool leader = false;
      bool called = false;
      bool restored = true;
      __try {
        // A zero short-jump displacement falls through instead of entering the
        // two empty-skill rejects while preserving instruction boundaries.
        skill1 = PatchCodeByteExact(kNoteAutoPlaySkillJe1Disp, 0x38u, 0x00u);
        skill2 = skill1 &&
                 PatchCodeByteExact(kNoteAutoPlaySkillJe2Disp, 0x32u, 0x00u);
        // Turn the leader-check JNE-success into an unconditional success jump.
        leader = skill2 &&
                 PatchCodeByteExact(kNoteAutoPlayLeaderJne, 0x75u, 0xEBu);
        if (leader) {
          FnAutoPlayFrameBtnStart fn =
              (FnAutoPlayFrameBtnStart)(uintptr_t)fn_va;
          fn(frame, 0);
          called = true;
        }
      } __finally {
        if (leader) {
          restored = PatchCodeByteExact(kNoteAutoPlayLeaderJne, 0xEBu, 0x75u) &&
                     restored;
        }
        if (skill2) {
          restored = PatchCodeByteExact(kNoteAutoPlaySkillJe2Disp, 0x00u, 0x32u) &&
                     restored;
        }
        if (skill1) {
          restored = PatchCodeByteExact(kNoteAutoPlaySkillJe1Disp, 0x00u, 0x38u) &&
                     restored;
        }
      }
      if (!called || !restored) {
        SetErr(!restored ? "AUTOPLAY_START_BYPASS restore failed"
                         : "AUTOPLAY_START_BYPASS signature mismatch");
        g_shm->status = ST_ERR;
        return;
      }
      void* autoplay = ResolveAutoplayThis();
      uint32_t running = autoplay ? *((uint8_t*)autoplay + 0x08) : 0;
      g_shm->ret = (int32_t)(running ? 1 : 0);
      g_shm->status = ST_OK;
      char buf[96];
      sprintf_s(buf, "AUTOPLAY_START_BYPASS dispatched running=%u restored=1",
                (unsigned)running);
      SetErr(buf);
      return;
    }

    if (cmd == CMD_AUTOPLAY_DRIVE_ATTACK) {
      // 副本 Alert 粘滞自愈：UI 线程驱动攻击组件 tick（等价 StateAttack
      // 态每帧的驱动调用），让攻击系统对已锁定目标出手。
      void* autoplay = ResolveAutoplayThis();
      if (!autoplay || !*((uint8_t*)autoplay + 0x08)) {
        SetErr("AUTOPLAY_DRIVE_ATTACK autoplay off");
        g_shm->status = ST_ERR;
        return;
      }
      uint8_t* mgr = (uint8_t*)autoplay + 0x578u;
      void* comp = *(void**)(mgr + 0x3Cu);
      if (!comp) {
        SetErr("AUTOPLAY_DRIVE_ATTACK attack component null");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t drive_va = NoteToLive(kNoteAutoPlayAttackDrive);
      if (!drive_va) {
        SetErr("AUTOPLAY_DRIVE_ATTACK drive va=0");
        g_shm->status = ST_ERR;
        return;
      }
      typedef void(__thiscall* FnDrive)(void*);
      ((FnDrive)(uintptr_t)drive_va)(comp);
      g_shm->ret = 1;
      SetErr("AUTOPLAY_DRIVE_ATTACK ok");
      g_shm->status = ST_OK;
      return;
    }
    if (cmd == CMD_AUTOPLAY_SEED_FOLLOW) {
      void* autoplay = ResolveAutoplayThis();
      if (!autoplay || !*((uint8_t*)autoplay + 0x08)) {
        SetErr("AUTOPLAY_SEED_FOLLOW autoplay off");
        g_shm->status = ST_ERR;
        return;
      }
      if (*((uint8_t*)autoplay + 0x1C) != 1u) {
        SetErr("AUTOPLAY_SEED_FOLLOW mode is not dungeon");
        g_shm->status = ST_ERR;
        return;
      }
      if ((lo | hi) == 0u) {
        SetErr("AUTOPLAY_SEED_FOLLOW target=0");
        g_shm->status = ST_ERR;
        return;
      }

      uint32_t host_va = NoteToLive(kNoteGetHostSide);
      FnGetHostSide get_host = host_va
                                   ? (FnGetHostSide)(uintptr_t)host_va
                                   : nullptr;
      void* host = get_host ? get_host() : nullptr;
      if (!host) {
        SetErr("AUTOPLAY_SEED_FOLLOW host=null");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t self_lo = *(uint32_t*)((uint8_t*)host + kHostPlayerIdLoOff);
      uint32_t self_hi = *(uint32_t*)((uint8_t*)host + kHostPlayerIdHiOff);
      if (self_lo == lo && self_hi == hi) {
        SetErr("AUTOPLAY_SEED_FOLLOW target=self");
        g_shm->status = ST_ERR;
        return;
      }

      void* team = *(void**)((uint8_t*)host + kHostTeamOff);
      if (!team) {
        SetErr("AUTOPLAY_SEED_FOLLOW team=null");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t leader_lo = *(uint32_t*)((uint8_t*)team + kTeamLeaderIdOff);
      uint32_t leader_hi =
          *(uint32_t*)((uint8_t*)team + kTeamLeaderIdOff + 4u);
      if (leader_lo != self_lo || leader_hi != self_hi) {
        SetErr("AUTOPLAY_SEED_FOLLOW host is not leader");
        g_shm->status = ST_ERR;
        return;
      }

      void** members =
          *(void***)((uint8_t*)team + kTeamMemberPtrsOff);
      uint32_t count = *(uint32_t*)((uint8_t*)team + kTeamMemberCountOff);
      bool found = false;
      if (members && count > 0u && count <= 12u) {
        for (uint32_t i = 0; i < count; ++i) {
          uint8_t* member = (uint8_t*)members[i];
          if (!member) continue;
          uint32_t member_lo = *(uint32_t*)(member + kTeamMemberIdOff);
          uint32_t member_hi = *(uint32_t*)(member + kTeamMemberIdOff + 4u);
          if (member_lo == lo && member_hi == hi) {
            found = true;
            break;
          }
        }
      }
      if (!found) {
        SetErr("AUTOPLAY_SEED_FOLLOW target not in team");
        g_shm->status = ST_ERR;
        return;
      }
      // 方案1：记录 seed 队员目标；跟随链解析到自己时由 hook 替换，
      // 保证队长身份的跟随上下文不自引用（Alert 粘滞根修）。
      g_follow_seed_lo = lo;
      g_follow_seed_hi = hi;
      if (!InstallFollowChainHook()) {
        SetErr("AUTOPLAY_SEED_FOLLOW follow-chain hook install failed");
        g_shm->status = ST_ERR;
        return;
      }

      uint8_t* manager = (uint8_t*)autoplay + 0x578u;
      void* snapshot = *(void**)(manager + 0x34u);
      uint32_t snapshot_vt =
          snapshot ? *(uint32_t*)snapshot : 0u;
      if (!snapshot ||
          snapshot_vt != NoteToLive(kNoteAutoPlayTargetSnapshotVtable)) {
        SetErr("AUTOPLAY_SEED_FOLLOW snapshot unavailable");
        g_shm->status = ST_ERR;
        return;
      }

      *(uint32_t*)((uint8_t*)snapshot + 0x08u) = lo;
      *(uint32_t*)((uint8_t*)snapshot + 0x0Cu) = hi;
      FnAutoPlayRefreshTarget refresh = (FnAutoPlayRefreshTarget)(uintptr_t)
          NoteToLive(kNoteAutoPlayRefreshTarget);
      FnAutoPlaySetState set_state = (FnAutoPlaySetState)(uintptr_t)
          NoteToLive(kNoteAutoPlaySetState);
      if (!refresh || !set_state) {
        SetErr("AUTOPLAY_SEED_FOLLOW native va=0");
        g_shm->status = ST_ERR;
        return;
      }
      refresh(snapshot);

      void* before_state = *(void**)(manager + 0x2Cu);
      set_state(manager, 4u);  // StateFollowTarget
      void* follow_state = *(void**)(manager + 0x2Cu);
      if (!follow_state ||
          *(uint32_t*)follow_state !=
              NoteToLive(kNoteAutoPlayFollowStateVtable)) {
        SetErr("AUTOPLAY_SEED_FOLLOW transition failed");
        g_shm->status = ST_ERR;
        return;
      }
      // SetState intentionally skips a transition to the already-current state.
      // Re-enter it once so a prior self-target follow is issued again.
      if (follow_state == before_state) {
        FnAutoPlayEnterFollow enter_follow =
            (FnAutoPlayEnterFollow)(uintptr_t)
                NoteToLive(kNoteAutoPlayEnterFollow);
        if (!enter_follow) {
          SetErr("AUTOPLAY_SEED_FOLLOW enter va=0");
          g_shm->status = ST_ERR;
          return;
        }
        enter_follow(follow_state, before_state);
      }

      g_shm->ret = 1;
      g_shm->status = ST_OK;
      char buf[128];
      sprintf_s(buf,
                "AUTOPLAY_SEED_FOLLOW ok target=%08X:%08X state=%p",
                (unsigned)hi, (unsigned)lo, follow_state);
      SetErr(buf);
      return;
    }

    if (cmd == CMD_ITEM_USE_TRACE) {
      // mode=1 configures/resets; mode=2 reads; mode=0 reads then unhooks.
      const int mode = g_shm->mode;
      if (mode == 1) {
        StopItemUseTraceHook();
        InterlockedExchange(&g_item_trace_pack, (LONG)lo);
        InterlockedExchange(&g_item_trace_slot, (LONG)hi);
        InterlockedExchange(&g_item_trace_attempts, 0);
        InterlockedExchange(&g_item_trace_successes, 0);
        if (!InstallItemUseTraceHook()) {
          SetErr("ITEM_USE_TRACE hook install failed");
          g_shm->status = ST_ERR;
          return;
        }
        InterlockedExchange(&g_item_trace_on, 1);
      }
      LONG success = InterlockedCompareExchange(&g_item_trace_successes, 0, 0);
      g_shm->ret = (int32_t)success;
      char buf[128];
      FormatItemUseTrace(buf, sizeof(buf));
      SetErr(buf);
      g_shm->status = ST_OK;
      if (mode == 0) StopItemUseTraceHook();
      return;
    }

    if (cmd == CMD_DUMMY_DAMAGE_STAT) {
      // The first positive settled slice starts the fixed 60-second window.
      const int mode = g_shm->mode;
      if (mode == 1) {
        InterlockedExchange(&g_dummy_damage_on, 0);
        InterlockedExchange(&g_dummy_damage_target_lo, (LONG)lo);
        InterlockedExchange(&g_dummy_damage_target_hi, (LONG)hi);
        InterlockedExchange(&g_dummy_damage_started_tick, 0);
        InterlockedExchange(&g_dummy_damage_done, 0);
        InterlockedExchange(&g_dummy_damage_hits, 0);
        InterlockedExchange(&g_dummy_damage_last, 0);
        InterlockedExchange64(&g_dummy_damage_total, 0);
        if ((!lo && !hi) || !InstallDummyDamageHook()) {
          SetErr((!lo && !hi) ? "DUMMY_DAMAGE target id is zero"
                              : "DUMMY_DAMAGE hook install failed");
          g_shm->status = ST_ERR;
          return;
        }
        InterlockedExchange(&g_dummy_damage_on, 1);
      }
      if (mode == 0) InterlockedExchange(&g_dummy_damage_on, 0);
      char buf[128];
      FormatDummyDamageStat(buf, sizeof(buf));
      g_shm->ret = (int32_t)InterlockedCompareExchange(
          &g_dummy_damage_hits, 0, 0);
      SetErr(buf);
      g_shm->status = ST_OK;
      return;
    }

    if (cmd == CMD_COMBAT_MONITOR) {
      // Read-only dungeon 纯站街 counters. mode=1 arm/reset, 2=status (refresh
      // host target), 0=disarm (detours stay but counting stops).
      const int mode = g_shm->mode;
      if (mode == 1) {
        InterlockedExchange(&g_combat_monitor_on, 0);
        InterlockedExchange(&g_combat_attack_seq, 0);
        InterlockedExchange(&g_combat_self_damage_seq, 0);
        InterlockedExchange64(&g_combat_self_damage_total, 0);
        InterlockedExchange64(&g_combat_host_target, 0);
        if (!InstallCombatMonitorHooks()) {
          InterlockedExchange(&g_combat_resolved, 0);
          SetErr("COMBAT_MONITOR hook install failed");
          g_shm->status = ST_ERR;
          return;
        }
        RefreshCombatHostTarget();
        InterlockedExchange(&g_combat_resolved, 1);
        InterlockedExchange(&g_combat_monitor_on, 1);
      }
      if (mode == 0) InterlockedExchange(&g_combat_monitor_on, 0);
      RefreshCombatHostTarget();
      char buf[192];
      FormatCombatMonitor(buf, sizeof(buf));
      g_shm->ret = (int32_t)InterlockedCompareExchange(
          &g_combat_attack_seq, 0, 0);
      g_shm->tid = (int32_t)InterlockedCompareExchange(
          &g_combat_self_damage_seq, 0, 0);
      SetErr(buf);
      g_shm->status = ST_OK;
      return;
    }


    if (cmd == CMD_CG_SKIP) {
      // mode=1 arm+install, mode=2 status, mode=0 disarm (keep hooks),
      // mode=3 arm+install and force-stop current CG (explicit only).
      // Default arm must NOT call ForceStopCurrentCg: save/hot-toggle used to
      // hit that path and could AV if CG manager state was mid-transition.
      const int mode = g_shm->mode;
      if (mode == 1 || mode == 3) {
        InterlockedExchange(&g_cg_skip_hits, 0);
        InterlockedExchange(&g_cg_skip_play_hits, 0);
        InterlockedExchange(&g_cg_skip_black_hits, 0);
        InterlockedExchange(&g_cg_skip_busy, 0);
        if (!InstallCgSkipHooks()) {
          InterlockedExchange(&g_cg_skip_on, 0);
          SetErr("CG_SKIP hook install failed");
          g_shm->status = ST_ERR;
          return;
        }
        InterlockedExchange(&g_cg_skip_on, 1);
        if (mode == 3) {
          // Explicit one-shot clear for already-playing cinematics only.
          ForceStopCurrentCg();
        }
      } else if (mode == 0) {
        InterlockedExchange(&g_cg_skip_on, 0);
      }
      char buf[160];
      FormatCgSkip(buf, sizeof(buf));
      g_shm->ret = (int32_t)InterlockedCompareExchange(&g_cg_skip_hits, 0, 0);
      SetErr(buf);
      g_shm->status = ST_OK;
      return;
    }

    if (cmd == CMD_SESSION_SEND_BYPASS) {
      // mode=1 arm (jae->jmp at 0xCC6D51), mode=2 status, mode=0 disarm.
      // Signature-verified so a changed client build fails closed.
      const int mode = g_shm->mode;
      char buf[160];
      if (mode == 1) {
        if (!ApplySessionSendBypass()) {
          SetErr("SESSION_SEND_BYPASS patch failed (signature/build mismatch)");
          g_shm->status = ST_ERR;
          return;
        }
        InterlockedExchange(&g_session_send_bypass_on, 1);
      } else if (mode == 0) {
        if (!RestoreSessionSendBypass()) {
          SetErr("SESSION_SEND_BYPASS restore failed");
          g_shm->status = ST_ERR;
          return;
        }
        InterlockedExchange(&g_session_send_bypass_on, 0);
      }
      int on = InterlockedCompareExchange(&g_session_send_bypass_on, 0, 0);
      int hits = InterlockedCompareExchange(&g_session_send_bypass_hits, 0, 0);
      sprintf_s(buf, _TRUNCATE, "SESSION_SEND_BYPASS on=%d hits=%d mode=%d",
                on ? 1 : 0, hits, mode);
      g_shm->ret = (int32_t)hits;
      SetErr(buf);
      g_shm->status = ST_OK;
      return;
    }

    if (cmd == CMD_SKILL_ACTION_TRACE) {
      // Read-only, short-lived exact-profile trace.
      // mode=1 start/reset, mode=2 status, mode=0 stop/dump/unhook.
      const int mode = g_shm->mode;
      if (mode == 1 || mode == 3 || mode == 4) {
        StopSkillActionTraceHooks();
        memset(g_skill_action_trace, 0, sizeof(g_skill_action_trace));
        memset(g_skill_packet_trace, 0, sizeof(g_skill_packet_trace));
        InterlockedExchange(&g_skill_action_trace_count, 0);
        InterlockedExchange(&g_skill_action_trace_dropped, 0);
        InterlockedExchange(&g_skill_packet_trace_count, 0);
        InterlockedExchange(&g_skill_packet_trace_dropped, 0);
        if (!InstallSkillActionTraceHooks()) {
          SetErr("SKILL_ACTION_TRACE hook install failed");
          g_shm->status = ST_ERR;
          return;
        }
        InterlockedExchange(&g_skill_refill_target_skill, (LONG)lo);
        InterlockedExchange(&g_skill_refill_target_config, (LONG)hi);
        InterlockedExchange(&g_skill_refill_suppress_mode, mode);
        InterlockedExchange(&g_skill_refill_suppress_on,
                            (mode == 3 || mode == 4) ? 1 : 0);
        InterlockedExchange(&g_skill_action_trace_on, 1);
        g_shm->ret = 0;
        g_shm->status = ST_OK;
        char trace_note[128];
        sprintf_s(trace_note,
                  "SKILL_ACTION_TRACE armed request=762F10 active=758DF0 perform=761330 suppress=%d skill=0x%X config=0x%X",
                  (mode == 3 || mode == 4) ? 1 : 0, (unsigned)lo,
                  (unsigned)hi);
        SetErr(trace_note);
        return;
      }
      if (mode == 2) {
        char buf[256];
        FormatSkillActionTrace(buf, sizeof(buf));
        g_shm->ret =
            (int32_t)InterlockedCompareExchange(&g_skill_action_trace_count, 0, 0);
        g_shm->status = ST_OK;
        SetErr(buf);
        return;
      }
      InterlockedExchange(&g_skill_action_trace_on, 0);
      StopSkillActionTraceHooks();
      char path[MAX_PATH] = {0};
      LONG count = InterlockedCompareExchange(&g_skill_action_trace_count, 0, 0);
      if (!DumpSkillActionTrace(path, sizeof(path))) {
        SetErr("SKILL_ACTION_TRACE dump failed");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->ret = (int32_t)count;
      g_shm->status = ST_OK;
      SetErr(path);
      return;
    }

    if (cmd == CMD_SKILL_INTERRUPT_67) {
      // Space's behavior path reaches mgr.vt+0x10(reason=0x67) before the
      // later HostStopSession/OnSkillStopped cleanup. Invoke only that exact
      // local branch, with no key state, jump action, packet, or field write.
      if (!lo || !hi) {
        SetErr("SKILL_INTERRUPT_67 expected identity missing");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t hva = NoteToLive(kNoteGetHostSide);
      if (!hva) {
        SetErr("SKILL_INTERRUPT_67 host va=0");
        g_shm->status = ST_ERR;
        return;
      }
      void* host = nullptr;
      void* cast_this = nullptr;
      void* mgr = nullptr;
      uint32_t skill = 0;
      uint32_t config = 0;
      uint32_t perform_type = 0;
      FnPerformStopTrace stop = nullptr;
      __try {
        FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)hva;
        host = get_host();
        if (host) {
          cast_this = *(void**)((uint8_t*)host + kSkillCastThisOff);
          mgr = *(void**)((uint8_t*)host + kHostPerformMgrOff);
        }
        if (cast_this) {
          skill = *(uint32_t*)((uint8_t*)cast_this + 0x10);
          config = *(uint32_t*)((uint8_t*)cast_this + 0x18);
        }
        uint8_t* current = mgr ? *(uint8_t**)((uint8_t*)mgr + 0x08) : nullptr;
        if (current) perform_type = *(uint32_t*)(current + 0x04);
        uint8_t* vt = mgr ? *(uint8_t**)mgr : nullptr;
        if (vt) stop = *(FnPerformStopTrace*)(vt + 0x10);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        host = nullptr;
        cast_this = nullptr;
        mgr = nullptr;
        stop = nullptr;
      }
      if (!host || !cast_this || !mgr || !stop) {
        SetErr("SKILL_INTERRUPT_67 context null");
        g_shm->status = ST_ERR;
        return;
      }
      if (skill != lo || config != hi || perform_type != 4u) {
        char buf[128];
        sprintf_s(buf,
                  "SKILL_INTERRUPT_67 reject live=0x%X/0x%X type=%u expected=0x%X/0x%X",
                  (unsigned)skill, (unsigned)config, (unsigned)perform_type,
                  (unsigned)lo, (unsigned)hi);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      int call_ret = 0;
      __try {
        call_ret = stop(mgr, 0x67u);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("SKILL_INTERRUPT_67 stop SEH");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->ret = call_ret;
      g_shm->status = ST_OK;
      char buf[112];
      sprintf_s(buf,
                "SKILL_INTERRUPT_67 ok ret=%d skill=0x%X config=0x%X type=%u",
                call_ret, (unsigned)skill, (unsigned)config,
                (unsigned)perform_type);
      SetErr(buf);
      return;
    }

    if (cmd == CMD_YOUFENG_INTERNAL_CHAIN) {
      if (!lo || !hi) {
        SetErr("YOUFENG_INTERNAL_CHAIN expected identity missing");
        g_shm->status = ST_ERR;
        return;
      }
      if (InterlockedCompareExchange(&g_youfeng_chain_phase, 0, 0) != 0) {
        SetErr("YOUFENG_INTERNAL_CHAIN already pending");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t hva = NoteToLive(kNoteGetHostSide);
      if (!hva || !NoteToLive(kNoteActionCast) ||
          !NoteToLive(kNoteActionContextGlobal)) {
        SetErr("YOUFENG_INTERNAL_CHAIN native va=0");
        g_shm->status = ST_ERR;
        return;
      }
      uint8_t* host = nullptr;
      uint8_t* cast = nullptr;
      uint8_t* mgr = nullptr;
      uint32_t skill = 0;
      uint32_t config = 0;
      uint32_t perform_type = 0;
      uint32_t gate1 = 0;
      __try {
        FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)hva;
        host = (uint8_t*)get_host();
        cast = host ? *(uint8_t**)(host + kSkillCastThisOff) : nullptr;
        mgr = host ? *(uint8_t**)(host + kHostPerformMgrOff) : nullptr;
        if (cast) {
          skill = *(uint32_t*)(cast + 0x10);
          config = *(uint32_t*)(cast + 0x18);
        }
        uint8_t* current = mgr ? *(uint8_t**)(mgr + 0x08) : nullptr;
        if (current) perform_type = *(uint32_t*)(current + 0x04);
        if (host) gate1 = *(uint32_t*)(host + kHostSessionStateOff + 4);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        host = nullptr;
        cast = nullptr;
        mgr = nullptr;
      }
      if (!host || !cast || !mgr || skill != lo || config != hi ||
          perform_type != 4u || gate1 < 1600u) {
        char buf[144];
        sprintf_s(buf,
                  "YOUFENG_INTERNAL_CHAIN reject live=0x%X/0x%X type=%u gate1=%u expected=0x%X/0x%X",
                  (unsigned)skill, (unsigned)config, (unsigned)perform_type,
                  (unsigned)gate1, (unsigned)lo, (unsigned)hi);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      int action_ret = -1;
      __try {
        action_ret = RunE07ActionPhase(cast, 1u);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("YOUFENG_INTERNAL_CHAIN action down SEH");
        g_shm->status = ST_ERR;
        return;
      }
      if (action_ret != 105) {
        char buf[96];
        sprintf_s(buf, "YOUFENG_INTERNAL_CHAIN action down rejected ret=%d",
                  action_ret);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      InterlockedExchange(&g_youfeng_chain_cast,
                          (LONG)(uint32_t)(uintptr_t)cast);
      InterlockedExchange(&g_youfeng_chain_host,
                          (LONG)(uint32_t)(uintptr_t)host);
      InterlockedExchange(&g_youfeng_chain_mgr,
                          (LONG)(uint32_t)(uintptr_t)mgr);
      InterlockedExchange(&g_youfeng_chain_due, (LONG)(GetTickCount() + 94u));
      InterlockedExchange(&g_youfeng_chain_phase, 1);
      g_shm->ret = action_ret;
      g_shm->status = ST_OK;
      SetErr("YOUFENG_INTERNAL_CHAIN queued E07 down/up + local tail; no X/Space key");
      return;
    }

    if (cmd == CMD_YOUFENG_NEXT_GATE) {
      const int mode = g_shm->mode;
      if (mode == 0) {
        const LONG hits = InterlockedCompareExchange(
            &g_youfeng_next_gate_hits, 0, 0);
        StopYoufengNextGateHook();
        g_shm->ret = (int32_t)hits;
        g_shm->status = ST_OK;
        SetErr("YOUFENG_NEXT_GATE closed");
        return;
      }
      if (mode == 2) {
        const LONG hits = InterlockedCompareExchange(
            &g_youfeng_next_gate_hits, 0, 0);
        const LONG armed = InterlockedCompareExchange(
            &g_youfeng_next_gate_on, 0, 0);
        g_shm->ret = (int32_t)hits;
        g_shm->status = ST_OK;
        char buf[96];
        sprintf_s(buf, "YOUFENG_NEXT_GATE armed=%ld hits=%ld", armed, hits);
        SetErr(buf);
        return;
      }
      if (mode != 1 || lo != 0x9563u || hi != 0x034Du) {
        SetErr("YOUFENG_NEXT_GATE exact 0x9563/0x34D mode 1 required");
        g_shm->status = ST_ERR;
        return;
      }
      if (InterlockedCompareExchange(&g_youfeng_next_gate_on, 0, 0)) {
        SetErr("YOUFENG_NEXT_GATE already armed");
        g_shm->status = ST_ERR;
        return;
      }

      uint8_t* host = nullptr;
      uint8_t* cast = nullptr;
      uint8_t* mgr = nullptr;
      uint32_t skill = 0;
      uint32_t config = 0;
      uint32_t perform_type = 0;
      uint32_t gate1 = 0;
      __try {
        FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)
            NoteToLive(kNoteGetHostSide);
        host = get_host ? (uint8_t*)get_host() : nullptr;
        cast = host ? *(uint8_t**)(host + kSkillCastThisOff) : nullptr;
        mgr = host ? *(uint8_t**)(host + kHostPerformMgrOff) : nullptr;
        if (cast) {
          skill = *(uint32_t*)(cast + 0x10);
          config = *(uint32_t*)(cast + 0x18);
        }
        uint8_t* current = mgr ? *(uint8_t**)(mgr + 0x08) : nullptr;
        if (current) perform_type = *(uint32_t*)(current + 0x04);
        if (host) gate1 = *(uint32_t*)(host + kHostSessionStateOff + 4);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        host = nullptr;
        cast = nullptr;
        mgr = nullptr;
      }
      if (!host || !cast || !mgr || skill != lo || config != hi ||
          perform_type != 4u || gate1 < 1600u) {
        char buf[144];
        sprintf_s(buf,
                  "YOUFENG_NEXT_GATE reject live=0x%X/0x%X type=%u gate1=%u",
                  (unsigned)skill, (unsigned)config, (unsigned)perform_type,
                  (unsigned)gate1);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      if (!InstallYoufengNextGateHook()) {
        SetErr("YOUFENG_NEXT_GATE hook install failed");
        g_shm->status = ST_ERR;
        return;
      }
      InterlockedExchange(&g_youfeng_next_gate_hits, 0);
      InterlockedExchange(&g_youfeng_next_gate_skill, (LONG)lo);
      InterlockedExchange(&g_youfeng_next_gate_config, (LONG)hi);
      InterlockedExchange(&g_youfeng_next_gate_allow_cleared, 0);
      InterlockedExchange(&g_youfeng_next_gate_due,
                          (LONG)(GetTickCount() + 450u));
      InterlockedExchange(&g_youfeng_next_gate_on, 1);
      g_shm->ret = 1;
      g_shm->status = ST_OK;
      SetErr("YOUFENG_NEXT_GATE armed 450ms; no E07/X/Space/stop");
      return;
    }

    if (cmd == CMD_YOUFENG_FRESH_GATE) {
      if (lo != 0x9563u || hi != 0x034Du) {
        SetErr("YOUFENG_FRESH_GATE exact 0x9563/0x34D required");
        g_shm->status = ST_ERR;
        return;
      }
      if (InterlockedCompareExchange(&g_youfeng_next_gate_on, 0, 0)) {
        SetErr("YOUFENG_FRESH_GATE gate already armed");
        g_shm->status = ST_ERR;
        return;
      }

      uint8_t* host = nullptr;
      uint8_t* cast = nullptr;
      uint8_t* mgr = nullptr;
      uint32_t skill = 0;
      uint32_t config = 0;
      uint32_t perform_type = 0;
      uint32_t gate1 = 0;
      __try {
        FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)
            NoteToLive(kNoteGetHostSide);
        host = get_host ? (uint8_t*)get_host() : nullptr;
        cast = host ? *(uint8_t**)(host + kSkillCastThisOff) : nullptr;
        mgr = host ? *(uint8_t**)(host + kHostPerformMgrOff) : nullptr;
        if (cast) {
          skill = *(uint32_t*)(cast + 0x10);
          config = *(uint32_t*)(cast + 0x18);
        }
        uint8_t* current = mgr ? *(uint8_t**)(mgr + 0x08) : nullptr;
        if (current) perform_type = *(uint32_t*)(current + 0x04);
        if (host) gate1 = *(uint32_t*)(host + kHostSessionStateOff + 4);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        host = nullptr;
        cast = nullptr;
        mgr = nullptr;
      }
      if (!host || !cast || !mgr || skill != lo || config != hi ||
          perform_type != 4u || gate1 < 1600u) {
        char buf[152];
        sprintf_s(buf,
                  "YOUFENG_FRESH_GATE reject live=0x%X/0x%X type=%u gate1=%u",
                  (unsigned)skill, (unsigned)config, (unsigned)perform_type,
                  (unsigned)gate1);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      if (!InstallYoufengNextGateHook()) {
        SetErr("YOUFENG_FRESH_GATE hook install failed");
        g_shm->status = ST_ERR;
        return;
      }

      uint32_t event_buf[8] = {0};
      event_buf[0] = lo;
      event_buf[2] = hi;
      FnOnSkillStopped on_stop = (FnOnSkillStopped)(uintptr_t)
          NoteToLive(kNoteOnSkillStopped);
      if (!on_stop) {
        SetErr("YOUFENG_FRESH_GATE OnSkillStopped va=0");
        g_shm->status = ST_ERR;
        return;
      }
      __try {
        on_stop(cast, event_buf, 0u, 0u, 0u, 1u);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("YOUFENG_FRESH_GATE OnSkillStopped SEH");
        g_shm->status = ST_ERR;
        return;
      }

      uint32_t after_skill = 0xFFFFFFFFu;
      uint32_t after_config = 0xFFFFFFFFu;
      __try {
        after_skill = *(uint32_t*)(cast + 0x10);
        after_config = *(uint32_t*)(cast + 0x18);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
      }
      if (after_skill != 0u || after_config != 0u) {
        char buf[128];
        sprintf_s(buf,
                  "YOUFENG_FRESH_GATE identity clear failed 0x%X/0x%X",
                  (unsigned)after_skill, (unsigned)after_config);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }

      SkillActionTraceEntry trace = {};
      trace.kind = SKTRACE_FRESH_GATE;
      trace.tick = GetTickCount();
      trace.thread_id = GetCurrentThreadId();
      trace.self_ptr = (uint32_t)(uintptr_t)cast;
      trace.ret = 1;
      trace.cast_10_before = lo;
      trace.cast_18_before = hi;
      trace.cast_10_after = after_skill;
      trace.cast_18_after = after_config;
      StoreSkillActionTrace(&trace);

      InterlockedExchange(&g_youfeng_next_gate_hits, 0);
      InterlockedExchange(&g_youfeng_next_gate_skill, (LONG)lo);
      InterlockedExchange(&g_youfeng_next_gate_config, (LONG)hi);
      InterlockedExchange(&g_youfeng_next_gate_allow_cleared, 1);
      InterlockedExchange(&g_youfeng_next_gate_due,
                          (LONG)(GetTickCount() + 450u));
      InterlockedExchange(&g_youfeng_next_gate_on, 1);
      g_shm->ret = 1;
      g_shm->status = ST_OK;
      SetErr("YOUFENG_FRESH_GATE armed 450ms after identity clear; no E07/X/Space/stop");
      return;
    }

    if (cmd == CMD_YOUFENG_EXACT_CANCEL_GATE) {
      if (lo != 0x9563u || hi != 0x034Du) {
        SetErr("YOUFENG_EXACT_CANCEL_GATE exact 0x9563/0x34D required");
        g_shm->status = ST_ERR;
        return;
      }
      if (InterlockedCompareExchange(&g_youfeng_next_gate_on, 0, 0)) {
        SetErr("YOUFENG_EXACT_CANCEL_GATE gate already armed");
        g_shm->status = ST_ERR;
        return;
      }

      uint8_t* host = nullptr;
      uint8_t* cast = nullptr;
      uint8_t* mgr = nullptr;
      uint32_t skill = 0;
      uint32_t config = 0;
      uint32_t perform_type = 0;
      uint32_t gate1 = 0;
      uint32_t id_perform = 0;
      __try {
        FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)
            NoteToLive(kNoteGetHostSide);
        host = get_host ? (uint8_t*)get_host() : nullptr;
        cast = host ? *(uint8_t**)(host + kSkillCastThisOff) : nullptr;
        mgr = host ? *(uint8_t**)(host + kHostPerformMgrOff) : nullptr;
        if (cast) {
          skill = *(uint32_t*)(cast + 0x10);
          config = *(uint32_t*)(cast + 0x18);
          id_perform = *(uint32_t*)(cast + 0x6C);
        }
        uint8_t* current = mgr ? *(uint8_t**)(mgr + 0x08) : nullptr;
        if (current) perform_type = *(uint32_t*)(current + 0x04);
        if (host) gate1 = *(uint32_t*)(host + kHostSessionStateOff + 4);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        host = nullptr;
        cast = nullptr;
        mgr = nullptr;
      }
      const LONG perform_age = (LONG)(GetTickCount() - id_perform);
      if (!host || !cast || !mgr || skill != lo || config != hi ||
          perform_type != 4u || gate1 < 1600u || id_perform == 0u ||
          id_perform == 0xFFFFFFFFu || perform_age < -250L ||
          perform_age > 2500L) {
        char buf[192];
        sprintf_s(
            buf,
            "YOUFENG_EXACT_CANCEL_GATE reject live=0x%X/0x%X type=%u gate1=%u id=%u age=%ld",
            (unsigned)skill, (unsigned)config, (unsigned)perform_type,
            (unsigned)gate1, (unsigned)id_perform, perform_age);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      if (!InstallYoufengNextGateHook()) {
        SetErr("YOUFENG_EXACT_CANCEL_GATE hook install failed");
        g_shm->status = ST_ERR;
        return;
      }

      void* net = ResolveNetRoot();
      FnCancelSession cancel = (FnCancelSession)(uintptr_t)
          NoteToLive(kNoteCancelSession);
      FnOnSkillStopped on_stop = (FnOnSkillStopped)(uintptr_t)
          NoteToLive(kNoteOnSkillStopped);
      if (!net || !cancel || !on_stop) {
        SetErr("YOUFENG_EXACT_CANCEL_GATE native entry missing");
        g_shm->status = ST_ERR;
        return;
      }
      uint8_t queued = 0;
      __try {
        queued = cancel((uint8_t*)net + kCancelSessionThisOff, 1u,
                        id_perform);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("YOUFENG_EXACT_CANCEL_GATE CancelSession SEH");
        g_shm->status = ST_ERR;
        return;
      }
      if (!queued) {
        SetErr("YOUFENG_EXACT_CANCEL_GATE cancel queue rejected");
        g_shm->status = ST_ERR;
        return;
      }

      uint32_t event_buf[8] = {0};
      event_buf[0] = lo;
      event_buf[2] = hi;
      __try {
        on_stop(cast, event_buf, 0u, 0u, 0u, 1u);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("YOUFENG_EXACT_CANCEL_GATE OnSkillStopped SEH");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t after_skill = 0xFFFFFFFFu;
      uint32_t after_config = 0xFFFFFFFFu;
      __try {
        after_skill = *(uint32_t*)(cast + 0x10);
        after_config = *(uint32_t*)(cast + 0x18);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
      }
      if (after_skill != 0u || after_config != 0u) {
        SetErr("YOUFENG_EXACT_CANCEL_GATE identity clear failed");
        g_shm->status = ST_ERR;
        return;
      }

      SkillActionTraceEntry trace = {};
      trace.kind = SKTRACE_EXACT_CANCEL;
      trace.tick = GetTickCount();
      trace.thread_id = GetCurrentThreadId();
      trace.self_ptr = (uint32_t)(uintptr_t)cast;
      trace.arg1 = id_perform;
      trace.arg2 = (uint32_t)perform_age;
      trace.ret = queued;
      trace.cast_10_before = lo;
      trace.cast_18_before = hi;
      trace.cast_10_after = after_skill;
      trace.cast_18_after = after_config;
      StoreSkillActionTrace(&trace);

      InterlockedExchange(&g_youfeng_next_gate_hits, 0);
      InterlockedExchange(&g_youfeng_next_gate_skill, (LONG)lo);
      InterlockedExchange(&g_youfeng_next_gate_config, (LONG)hi);
      InterlockedExchange(&g_youfeng_next_gate_allow_cleared, 1);
      InterlockedExchange(&g_youfeng_next_gate_due,
                          (LONG)(GetTickCount() + 450u));
      InterlockedExchange(&g_youfeng_next_gate_on, 1);
      g_shm->ret = (int32_t)id_perform;
      g_shm->status = ST_OK;
      SetErr("YOUFENG_EXACT_CANCEL_GATE queued exact 0x21 + identity clear; no E07/X/Space/stop");
      return;
    }

    if (cmd == CMD_YOUFENG_PHASE_GATE) {
      if (lo != 0x9563u || hi != 0x034Du) {
        SetErr("YOUFENG_PHASE_GATE exact 0x9563/0x34D required");
        g_shm->status = ST_ERR;
        return;
      }
      if (InterlockedCompareExchange(&g_youfeng_next_gate_on, 0, 0) ||
          InterlockedCompareExchange(&g_youfeng_phase_up_pending, 0, 0)) {
        SetErr("YOUFENG_PHASE_GATE already pending");
        g_shm->status = ST_ERR;
        return;
      }

      uint8_t* host = nullptr;
      uint8_t* cast = nullptr;
      uint8_t* mgr = nullptr;
      uint32_t skill = 0;
      uint32_t config = 0;
      uint32_t perform_type = 0;
      uint32_t gate1 = 0;
      __try {
        FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)
            NoteToLive(kNoteGetHostSide);
        host = get_host ? (uint8_t*)get_host() : nullptr;
        cast = host ? *(uint8_t**)(host + kSkillCastThisOff) : nullptr;
        mgr = host ? *(uint8_t**)(host + kHostPerformMgrOff) : nullptr;
        if (cast) {
          skill = *(uint32_t*)(cast + 0x10);
          config = *(uint32_t*)(cast + 0x18);
        }
        uint8_t* current = mgr ? *(uint8_t**)(mgr + 0x08) : nullptr;
        if (current) perform_type = *(uint32_t*)(current + 0x04);
        if (host) gate1 = *(uint32_t*)(host + kHostSessionStateOff + 4);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        host = nullptr;
        cast = nullptr;
        mgr = nullptr;
      }
      if (!host || !cast || !mgr || skill != lo || config != hi ||
          perform_type != 4u || gate1 < 1600u) {
        char buf[152];
        sprintf_s(buf,
                  "YOUFENG_PHASE_GATE reject live=0x%X/0x%X type=%u gate1=%u",
                  (unsigned)skill, (unsigned)config, (unsigned)perform_type,
                  (unsigned)gate1);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      if (!InstallYoufengNextGateHook()) {
        SetErr("YOUFENG_PHASE_GATE gate hook install failed");
        g_shm->status = ST_ERR;
        return;
      }

      void* net = ResolveNetRoot();
      FnActionPhasePacket phase_down = (FnActionPhasePacket)(uintptr_t)
          NoteToLive(kNoteActionPhaseDown);
      FnOnSkillStopped on_stop = (FnOnSkillStopped)(uintptr_t)
          NoteToLive(kNoteOnSkillStopped);
      if (!net || !phase_down || !on_stop) {
        SetErr("YOUFENG_PHASE_GATE native entry missing");
        g_shm->status = ST_ERR;
        return;
      }
      uint8_t sent = 0;
      __try {
        sent = phase_down((uint8_t*)net + kActionPhaseThisOff, hi);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("YOUFENG_PHASE_GATE phase-down SEH");
        g_shm->status = ST_ERR;
        return;
      }
      if (!sent) {
        SetErr("YOUFENG_PHASE_GATE phase-down throttled");
        g_shm->status = ST_ERR;
        return;
      }
      InterlockedExchange(&g_youfeng_phase_net,
                          (LONG)(uint32_t)(uintptr_t)net);
      InterlockedExchange(&g_youfeng_phase_config, (LONG)hi);
      InterlockedExchange(&g_youfeng_phase_due,
                          (LONG)(GetTickCount() + 95u));
      InterlockedExchange(&g_youfeng_phase_up_pending, 1);

      uint32_t event_buf[8] = {0};
      event_buf[0] = lo;
      event_buf[2] = hi;
      __try {
        on_stop(cast, event_buf, 0u, 0u, 0u, 1u);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("YOUFENG_PHASE_GATE OnSkillStopped SEH");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t after_skill = 0xFFFFFFFFu;
      uint32_t after_config = 0xFFFFFFFFu;
      __try {
        after_skill = *(uint32_t*)(cast + 0x10);
        after_config = *(uint32_t*)(cast + 0x18);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
      }
      if (after_skill != 0u || after_config != 0u) {
        SetErr("YOUFENG_PHASE_GATE identity clear failed");
        g_shm->status = ST_ERR;
        return;
      }

      SkillActionTraceEntry trace = {};
      trace.kind = SKTRACE_PHASE_GATE;
      trace.tick = GetTickCount();
      trace.thread_id = GetCurrentThreadId();
      trace.self_ptr = (uint32_t)(uintptr_t)cast;
      trace.arg1 = 0x69u;
      trace.arg2 = 0x6Au;
      trace.ret = sent;
      trace.cast_10_before = lo;
      trace.cast_18_before = hi;
      trace.cast_10_after = after_skill;
      trace.cast_18_after = after_config;
      StoreSkillActionTrace(&trace);

      InterlockedExchange(&g_youfeng_next_gate_hits, 0);
      InterlockedExchange(&g_youfeng_next_gate_skill, (LONG)lo);
      InterlockedExchange(&g_youfeng_next_gate_config, (LONG)hi);
      InterlockedExchange(&g_youfeng_next_gate_allow_cleared, 1);
      InterlockedExchange(&g_youfeng_next_gate_due,
                          (LONG)(GetTickCount() + 450u));
      InterlockedExchange(&g_youfeng_next_gate_on, 1);
      g_shm->ret = sent;
      g_shm->status = ST_OK;
      SetErr("YOUFENG_PHASE_GATE sent 0x69/0x34D + queued 0x6A; no E07/X/Space/stop");
      return;
    }

    if (cmd == CMD_SKILL_ACTION_EXPERIMENT) {
      const int mode = g_shm->mode;
      if (mode != 1 && mode != 2 && mode != 3 && mode != 4 && mode != 5 &&
          mode != 6 && mode != 7 && mode != 8 && mode != 9 && mode != 10 &&
          mode != 11 && mode != 12 && mode != 13) {
        SetErr("SKILL_ACTION_EXPERIMENT mode 1..13 required");
        g_shm->status = ST_ERR;
        return;
      }

      uint8_t* host = nullptr;
      uint8_t* cast = nullptr;
      uint8_t* mgr = nullptr;
      void* qinggong = nullptr;
      uint32_t skill = 0;
      uint32_t config = 0;
      uint32_t busy = 0;
      uint32_t perform_type = 0;
      uint32_t gate0 = 0;
      uint32_t gate1 = 0;
      uint32_t gate2 = 0;
      uint32_t id_perform = 0;
      uint32_t q270 = 0xFFFFFFFFu;
      uint32_t q274 = 0xFFFFFFFFu;
      __try {
        FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)
            NoteToLive(kNoteGetHostSide);
        host = get_host ? (uint8_t*)get_host() : nullptr;
        cast = host ? *(uint8_t**)(host + kSkillCastThisOff) : nullptr;
        mgr = host ? *(uint8_t**)(host + kHostPerformMgrOff) : nullptr;
        qinggong = host ? *(void**)(host + kHostSideThisOff) : nullptr;
        if (cast) {
          skill = *(uint32_t*)(cast + 0x10);
          config = *(uint32_t*)(cast + 0x18);
          id_perform = *(uint32_t*)(cast + 0x6C);
          busy = *(uint32_t*)(cast + 0x4A0);
        }
        uint8_t* current = mgr ? *(uint8_t**)(mgr + 0x08) : nullptr;
        if (current) perform_type = *(uint32_t*)(current + 0x04);
        if (host) {
          gate0 = *(uint32_t*)(host + kHostSessionStateOff);
          gate1 = *(uint32_t*)(host + kHostSessionStateOff + 4);
          gate2 = *(uint32_t*)(host + kHostSessionStateOff + 8);
        }
        if (qinggong) {
          q270 = *(uint32_t*)((uint8_t*)qinggong + 0x270);
          q274 = *(uint32_t*)((uint8_t*)qinggong + 0x274);
        }
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        host = nullptr;
        cast = nullptr;
        mgr = nullptr;
        qinggong = nullptr;
      }

      if (mode == 10) {
        char buf[192];
        sprintf_s(
            buf,
            "SKILL_ACTION_EXPERIMENT snapshot id=0x%X/0x%X busy=%u type=%u gate=%u/%u/%u perf=%u qg=%u/%u",
            (unsigned)skill, (unsigned)config, (unsigned)busy,
            (unsigned)perform_type, (unsigned)gate0, (unsigned)gate1,
            (unsigned)gate2, (unsigned)id_perform, (unsigned)q270,
            (unsigned)q274);
        SetErr(buf);
        g_shm->ret = (int32_t)id_perform;
        g_shm->status = host && cast && mgr && qinggong ? ST_OK : ST_ERR;
        return;
      }

      if (mode == 12) {
        const LONG ultimate_chain_on = InterlockedExchange(
            &g_youfeng_ultimate_cd_clear_on, 0);
        if (ultimate_chain_on) StopYoufengNextGateHook();
        InterlockedExchange(&g_youfeng_ultimate_tail_auto_on, 0);
        ClearYoufengUltimateTailPending();
        const LONG hits = InterlockedCompareExchange(
            &g_youfeng_ultimate_tail_auto_hits, 0, 0);
        const LONG dropped = InterlockedCompareExchange(
            &g_youfeng_ultimate_tail_pending_dropped, 0, 0);
        const LONG reason = InterlockedCompareExchange(
            &g_youfeng_ultimate_tail_pending_last_reason, 0, 0);
        const LONG clears = InterlockedCompareExchange(
            &g_youfeng_ultimate_cd_clear_hits, 0, 0);
        const LONG cd_fail = InterlockedCompareExchange(
            &g_youfeng_ultimate_cd_clear_failures, 0, 0);
        const LONG cd_reason = InterlockedCompareExchange(
            &g_youfeng_ultimate_cd_clear_last_reason, 0, 0);
        const LONG cd_key = InterlockedCompareExchange(
            &g_youfeng_ultimate_cd_clear_last_key, 0, 0);
        const LONG cd_total = InterlockedCompareExchange(
            &g_youfeng_ultimate_cd_clear_last_total, 0, 0);
        const LONG cd_remaining = InterlockedCompareExchange(
            &g_youfeng_ultimate_cd_clear_last_remaining, 0, 0);
        const LONG cd_record = InterlockedCompareExchange(
            &g_youfeng_ultimate_cd_clear_last_record, 0, 0);
        const LONG gate_hits = InterlockedCompareExchange(
            &g_youfeng_next_gate_hits, 0, 0);
        char buf[176];
        sprintf_s(buf,
                  "ULT stop tail=%ld gate=%ld drop=%ld tr=%ld cd=%ld fail=%ld cr=%ld key=%lX total=%ld rem=%ld rec=%08lX",
                  hits, gate_hits, dropped, reason, clears, cd_fail,
                  cd_reason, cd_key, cd_total, cd_remaining, cd_record);
        SetErr(buf);
        g_shm->ret = (int32_t)hits;
        g_shm->status = ST_OK;
        return;
      }

      if (mode == 11 || mode == 13) {
        if (lo != 0x9563u || hi != 0x195Eu ||
            InterlockedCompareExchange(&g_youfeng_ultimate_tail_auto_on, 0, 0)) {
          SetErr("YOUFENG_ULTIMATE_TAIL exact target required or already armed");
          g_shm->status = ST_ERR;
          return;
        }
        if (mode == 13 && !ValidateYoufengUltimateCooldownPath()) {
          SetErr("YOUFENG_ULTIMATE_CHAIN cooldown resolver validation failed");
          g_shm->status = ST_ERR;
          return;
        }
        const bool skill_hooks_ok = InstallSkillActionTraceHooks();
        const bool qinggong_hooks_ok = InstallQingGongTraceHooks();
        const bool next_gate_ok = mode != 13 || InstallYoufengNextGateHook();
        if (!skill_hooks_ok || !qinggong_hooks_ok || !next_gate_ok) {
          char hook_err[192];
          sprintf_s(
              hook_err,
              "YOUFENG_ULTIMATE_TAIL hook install failed skill=%d qinggong=%d gate=%d",
              skill_hooks_ok ? 1 : 0, qinggong_hooks_ok ? 1 : 0,
              next_gate_ok ? 1 : 0);
          SetErr(hook_err);
          g_shm->status = ST_ERR;
          return;
        }
        InterlockedExchange(&g_youfeng_ultimate_tail_auto_hits, 0);
        InterlockedExchange(&g_youfeng_ultimate_tail_pending_dropped, 0);
        InterlockedExchange(&g_youfeng_ultimate_tail_pending_last_reason, 0);
        InterlockedExchange(&g_youfeng_ultimate_cd_clear_hits, 0);
        InterlockedExchange(&g_youfeng_ultimate_cd_clear_failures, 0);
        InterlockedExchange(&g_youfeng_ultimate_cd_clear_last_reason, 0);
        InterlockedExchange(&g_youfeng_ultimate_cd_clear_last_remaining, 0);
        InterlockedExchange(&g_youfeng_ultimate_cd_clear_last_total, 0);
        InterlockedExchange(&g_youfeng_ultimate_cd_clear_last_key, 0);
        InterlockedExchange(&g_youfeng_ultimate_cd_clear_last_record, 0);
        ClearYoufengUltimateTailPending();
        if (mode == 13) {
          InterlockedExchange(&g_youfeng_next_gate_hits, 0);
          InterlockedExchange(&g_youfeng_next_gate_skill, 0x9563);
          InterlockedExchange(&g_youfeng_next_gate_config, 0x195E);
          InterlockedExchange(&g_youfeng_next_gate_allow_cleared, 2);
          InterlockedExchange(&g_youfeng_next_gate_qinggong, 0);
          InterlockedExchange(&g_youfeng_next_gate_due, 0);
          InterlockedExchange(&g_youfeng_next_gate_on, 1);
        }
        InterlockedExchange(&g_youfeng_ultimate_cd_clear_on,
                            mode == 13 ? 1 : 0);
        InterlockedExchange(&g_youfeng_ultimate_tail_auto_on, 1);
        SetErr(mode == 13
                   ? "YOUFENG_ULTIMATE_CHAIN armed tail + exact CD + persistent action gate"
                   : "YOUFENG_ULTIMATE_TAIL armed exact 0x9563/0x195E");
        g_shm->ret = 1;
        g_shm->status = ST_OK;
        return;
      }

      if (mode == 9) {
        InterlockedExchange(&g_kuangfeng_tail_auto_on, 0);
        ClearKuangfengTailPending();
        const LONG hits = InterlockedCompareExchange(
            &g_kuangfeng_tail_auto_hits, 0, 0);
        const LONG remaining = InterlockedCompareExchange(
            &g_kuangfeng_tail_auto_remaining, 0, 0);
        const LONG dropped = InterlockedCompareExchange(
            &g_kuangfeng_tail_pending_dropped, 0, 0);
        const LONG reason = InterlockedCompareExchange(
            &g_kuangfeng_tail_pending_last_reason, 0, 0);
        char buf[160];
        sprintf_s(buf,
                  "SKILL_ACTION_EXPERIMENT auto-tail stopped hits=%ld remaining=%ld dropped=%ld reason=%ld",
                  hits, remaining, dropped, reason);
        SetErr(buf);
        g_shm->ret = (int32_t)hits;
        g_shm->status = ST_OK;
        return;
      }

      if (mode == 8) {
        if (lo != 0x952Au || hi != 0x176Du ||
            InterlockedCompareExchange(&g_kuangfeng_tail_auto_on, 0, 0)) {
          SetErr("SKILL_ACTION_EXPERIMENT auto-tail exact target required or already armed");
          g_shm->status = ST_ERR;
          return;
        }
        if (!InstallSkillActionTraceHooks() || !InstallQingGongTraceHooks()) {
          SetErr("SKILL_ACTION_EXPERIMENT auto-tail hook install failed");
          g_shm->status = ST_ERR;
          return;
        }
        InterlockedExchange(&g_kuangfeng_tail_auto_hits, 0);
        InterlockedExchange(&g_kuangfeng_tail_auto_remaining, 10);
        InterlockedExchange(&g_kuangfeng_tail_pending_dropped, 0);
        InterlockedExchange(&g_kuangfeng_tail_pending_last_reason, 0);
        ClearKuangfengTailPending();
        InterlockedExchange(&g_kuangfeng_tail_auto_due,
                            (LONG)(GetTickCount() + 12000u));
        InterlockedExchange(&g_kuangfeng_tail_auto_on, 1);
        SetErr("SKILL_ACTION_EXPERIMENT auto-tail armed exact 10 hits/12s");
        g_shm->ret = 1;
        g_shm->status = ST_OK;
        return;
      }

      const bool kuangfeng_early_pulse =
          mode == 6 && lo == 0x952Au && hi == 0x176Du &&
          perform_type == 4u && busy == 0u && id_perform == 0xFFFFFFFFu &&
          gate0 == 0u && gate1 == 0u && gate2 == 0u;
      const bool kuangfeng_tail_pulse =
          mode == 7 && lo == 0x952Au && hi == 0x176Du &&
          perform_type == 4u && busy == 1u && gate0 == 5u && gate1 == 350u &&
          gate2 == hi && id_perform != 0u && id_perform != 0xFFFFFFFFu;
      const bool kuangfeng_pulse =
          kuangfeng_early_pulse || kuangfeng_tail_pulse;
      if (!lo || !hi || !host || !cast || !mgr || !qinggong ||
          skill != lo || config != hi ||
          ((mode == 6 || mode == 7) ? !kuangfeng_pulse
                                    : perform_type != 4u)) {
        char buf[176];
        sprintf_s(
            buf,
            "SKILL_ACTION_EXPERIMENT reject live=0x%X/0x%X type=%u gate=%u/%u/%u expected=0x%X/0x%X",
            (unsigned)skill, (unsigned)config, (unsigned)perform_type,
            (unsigned)gate0, (unsigned)gate1, (unsigned)gate2,
            (unsigned)lo, (unsigned)hi);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }

      if (mode == 4) {
        void* net = ResolveNetRoot();
        FnCancelSession cancel = (FnCancelSession)(uintptr_t)
            NoteToLive(kNoteCancelSession);
        if (!net || !cancel || !id_perform || id_perform == 0xFFFFFFFFu) {
          SetErr("SKILL_ACTION_EXPERIMENT exact cancel context missing");
          g_shm->status = ST_ERR;
          return;
        }
        uint8_t queued = 0;
        __try {
          queued = cancel((uint8_t*)net + kCancelSessionThisOff, 1u,
                          id_perform);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
          SetErr("SKILL_ACTION_EXPERIMENT exact cancel SEH");
          g_shm->status = ST_ERR;
          return;
        }
        SkillActionTraceEntry trace = {};
        trace.kind = SKTRACE_EXACT_CANCEL;
        trace.tick = GetTickCount();
        trace.thread_id = GetCurrentThreadId();
        trace.self_ptr = (uint32_t)(uintptr_t)cast;
        trace.arg1 = id_perform;
        trace.ret = queued;
        ReadCastTraceFields(cast, false, &trace);
        ReadCastTraceFields(cast, true, &trace);
        StoreSkillActionTrace(&trace);
        g_shm->ret = queued ? (int32_t)id_perform : 0;
        g_shm->status = queued ? ST_OK : ST_ERR;
        SetErr(queued
                   ? "SKILL_ACTION_EXPERIMENT exact 0x21 queued; local state untouched"
                   : "SKILL_ACTION_EXPERIMENT exact 0x21 rejected");
        return;
      }

      if (mode == 5) {
        if (InterlockedCompareExchange(&g_youfeng_next_gate_on, 0, 0)) {
          SetErr("SKILL_ACTION_EXPERIMENT current gate already armed");
          g_shm->status = ST_ERR;
          return;
        }
        if (!InstallYoufengNextGateHook()) {
          SetErr("SKILL_ACTION_EXPERIMENT current gate hook install failed");
          g_shm->status = ST_ERR;
          return;
        }
        InterlockedExchange(&g_youfeng_next_gate_hits, 0);
        InterlockedExchange(&g_youfeng_next_gate_skill, (LONG)lo);
        InterlockedExchange(&g_youfeng_next_gate_config, (LONG)hi);
        InterlockedExchange(&g_youfeng_next_gate_allow_cleared, 0);
        InterlockedExchange(&g_youfeng_next_gate_qinggong, 0);
        InterlockedExchange(&g_youfeng_next_gate_due,
                            (LONG)(GetTickCount() + 450u));
        InterlockedExchange(&g_youfeng_next_gate_on, 1);
        g_shm->ret = 1;
        g_shm->status = ST_OK;
        SetErr("SKILL_ACTION_EXPERIMENT armed one matching current-action gate; local state untouched");
        return;
      }

      if (!InstallQingGongTraceHooks()) {
        SetErr("SKILL_ACTION_EXPERIMENT qinggong hook install failed");
        g_shm->status = ST_ERR;
        return;
      }
      const uint32_t edge_mode =
          mode == 1 ? 2u
                    : (mode == 2 ? 3u
                                 : ((mode == 6 || mode == 7) ? 6u : 4u));
      void* live_qinggong = nullptr;
      uint32_t previous_type = 0xFFFFFFFFu;
      uint32_t elapsed = 0;
      uint8_t stopped = 0;
      uint8_t post_stopped = 0;
      const uint8_t started = StartCurrentQingGongFromCast(
          cast, &live_qinggong, &previous_type, &stopped, &post_stopped,
          edge_mode, &elapsed);
      SkillActionTraceEntry trace = {};
      trace.kind = SKTRACE_QINGGONG_GATE;
      trace.tick = GetTickCount();
      trace.thread_id = GetCurrentThreadId();
      trace.self_ptr = (uint32_t)(uintptr_t)cast;
      trace.skill_0 = lo;
      trace.skill_10 = hi;
      trace.skill_ea4 = hi;
      trace.arg1 = started;
      trace.arg2 = (uint32_t)(uintptr_t)live_qinggong;
      trace.arg3 = (uint32_t)mode;
      trace.ret = started;
      trace.session_args[0] = previous_type;
      trace.session_args[1] = stopped;
      trace.session_args[2] = elapsed;
      trace.session_args[3] = post_stopped;
      ReadCastTraceFields(cast, false, &trace);
      ReadCastTraceFields(cast, true, &trace);
      StoreSkillActionTrace(&trace);
      char buf[176];
      sprintf_s(
          buf,
          "SKILL_ACTION_EXPERIMENT qg mode=%d prev=%u stop=%u start=%u elapsed=%u id=0x%X/0x%X",
          mode, (unsigned)previous_type, (unsigned)stopped,
          (unsigned)started, (unsigned)elapsed, (unsigned)skill,
          (unsigned)config);
      SetErr(buf);
      const bool succeeded =
          started && ((mode != 6 && mode != 7) || post_stopped);
      g_shm->ret = succeeded ? 1 : 0;
      g_shm->status = succeeded ? ST_OK : ST_ERR;
      return;
    }

    if (cmd == CMD_YOUFENG_QINGGONG_GATE) {
      const int mode = g_shm->mode;
      if (mode == 10) {
        uint8_t* host = nullptr;
        uint8_t* cast = nullptr;
        uint8_t* qinggong = nullptr;
        uint32_t q270 = 0xFFFFFFFFu;
        uint32_t q274 = 0xFFFFFFFFu;
        uint32_t skill = 0;
        uint32_t config = 0;
        __try {
          FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)
              NoteToLive(kNoteGetHostSide);
          host = get_host ? (uint8_t*)get_host() : nullptr;
          cast = host ? *(uint8_t**)(host + kSkillCastThisOff) : nullptr;
          qinggong = host ? *(uint8_t**)(host + kHostSideThisOff) : nullptr;
          if (cast) {
            skill = *(uint32_t*)(cast + 0x10);
            config = *(uint32_t*)(cast + 0x18);
          }
          if (qinggong) {
            q270 = *(uint32_t*)(qinggong + 0x270);
            q274 = *(uint32_t*)(qinggong + 0x274);
          }
        } __except (EXCEPTION_EXECUTE_HANDLER) {
          host = nullptr;
        }
        char buf[160];
        sprintf_s(buf,
                  "YOUFENG_QINGGONG_GATE snapshot host=0x%X cast=0x%X qg=0x%X id=0x%X/0x%X q270=%u q274=%u",
                  (unsigned)(uintptr_t)host, (unsigned)(uintptr_t)cast,
                  (unsigned)(uintptr_t)qinggong, (unsigned)skill,
                  (unsigned)config, (unsigned)q270, (unsigned)q274);
        SetErr(buf);
        g_shm->ret = (int32_t)q270;
        g_shm->status = host && qinggong ? ST_OK : ST_ERR;
        return;
      }
      if (mode < 1 || mode > 8 || lo != 0x9563u || hi != 0x034Du) {
        SetErr("YOUFENG_QINGGONG_GATE mode 1..8 exact 0x9563/0x34D required");
        g_shm->status = ST_ERR;
        return;
      }
      if (InterlockedCompareExchange(&g_youfeng_next_gate_on, 0, 0)) {
        SetErr("YOUFENG_QINGGONG_GATE gate already armed");
        g_shm->status = ST_ERR;
        return;
      }

      uint8_t* host = nullptr;
      uint8_t* cast = nullptr;
      uint8_t* mgr = nullptr;
      void* qinggong = nullptr;
      uint32_t skill = 0;
      uint32_t config = 0;
      uint32_t perform_type = 0;
      uint32_t gate1 = 0;
      __try {
        FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)
            NoteToLive(kNoteGetHostSide);
        host = get_host ? (uint8_t*)get_host() : nullptr;
        cast = host ? *(uint8_t**)(host + kSkillCastThisOff) : nullptr;
        mgr = host ? *(uint8_t**)(host + kHostPerformMgrOff) : nullptr;
        qinggong = host ? *(void**)(host + kHostSideThisOff) : nullptr;
        if (cast) {
          skill = *(uint32_t*)(cast + 0x10);
          config = *(uint32_t*)(cast + 0x18);
        }
        uint8_t* current = mgr ? *(uint8_t**)(mgr + 0x08) : nullptr;
        if (current) perform_type = *(uint32_t*)(current + 0x04);
        if (host) gate1 = *(uint32_t*)(host + kHostSessionStateOff + 4);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        host = nullptr;
        cast = nullptr;
        mgr = nullptr;
        qinggong = nullptr;
      }
      uint8_t* start_qinggong =
          (uint8_t*)(uintptr_t)NoteToLive(kNoteCmdStartQingGong);
      bool qinggong_entry_ok = false;
      __try {
        // 74F480: sub esp,1AC; push ebx; push edi.
        qinggong_entry_ok =
            (g_hk_qinggong_start_trace.active &&
             g_hk_qinggong_stop_trace.active) ||
            (start_qinggong && start_qinggong[0] == 0x81 &&
             start_qinggong[1] == 0xEC && start_qinggong[2] == 0xAC &&
             start_qinggong[3] == 0x01 && start_qinggong[4] == 0x00 &&
             start_qinggong[5] == 0x00);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        qinggong_entry_ok = false;
      }
      if (!host || !cast || !mgr || !qinggong || !qinggong_entry_ok ||
          skill != lo || config != hi || perform_type != 4u ||
          gate1 < 1600u) {
        char buf[192];
        sprintf_s(
            buf,
            "YOUFENG_QINGGONG_GATE reject live=0x%X/0x%X type=%u gate1=%u qg=0x%X entry=%u",
            (unsigned)skill, (unsigned)config, (unsigned)perform_type,
            (unsigned)gate1, (unsigned)(uintptr_t)qinggong,
            qinggong_entry_ok ? 1u : 0u);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      if (!InstallQingGongTraceHooks()) {
        SetErr("YOUFENG_QINGGONG_GATE qinggong trace hook install failed");
        g_shm->status = ST_ERR;
        return;
      }
      if (mode >= 2) {
        SkillActionTraceEntry qg = {};
        qg.kind = SKTRACE_QINGGONG_GATE;
        qg.tick = GetTickCount();
        qg.thread_id = GetCurrentThreadId();
        qg.self_ptr = (uint32_t)(uintptr_t)cast;
        qg.skill_0 = lo;
        qg.skill_10 = hi;
        qg.skill_ea4 = hi;
        qg.arg3 = 2u;  // Pre-cast wrapper path.
        ReadCastTraceFields(cast, false, &qg);
        void* live_qinggong = nullptr;
        uint32_t previous_type = 0xFFFFFFFFu;
        uint32_t elapsed = 0;
        uint8_t stopped = 0;
        uint8_t post_stopped = 0;
        const uint8_t started = StartCurrentQingGongFromCast(
            cast, &live_qinggong, &previous_type, &stopped, &post_stopped,
            (uint32_t)mode, &elapsed);
        qg.arg1 = (uint32_t)started;
        qg.arg2 = (uint32_t)(uintptr_t)live_qinggong;
        qg.ret = (int32_t)started;
        qg.session_args[0] = previous_type;
        qg.session_args[1] = (uint32_t)stopped;
        qg.session_args[2] = elapsed;
        qg.session_args[3] = (uint32_t)post_stopped;
        ReadCastTraceFields(cast, true, &qg);
        StoreSkillActionTrace(&qg);
        const bool pulse_ok = mode < 5 || post_stopped != 0;
        g_shm->ret = pulse_ok ? (int32_t)started : 0;
        g_shm->status = started && pulse_ok ? ST_OK : ST_ERR;
        char buf[160];
        sprintf_s(buf,
                  "YOUFENG_QINGGONG_GATE pre-cast mode=%d prev=%u stop=%u start=%u post_stop=%u elapsed=%u; Space untouched",
                  mode, (unsigned)previous_type, (unsigned)stopped,
                  (unsigned)started, (unsigned)post_stopped,
                  (unsigned)elapsed);
        SetErr(buf);
        return;
      }
      if (!InstallYoufengActionCastHook()) {
        SetErr("YOUFENG_QINGGONG_GATE ActionCast hook install failed");
        g_shm->status = ST_ERR;
        return;
      }

      InterlockedExchange(&g_youfeng_next_gate_hits, 0);
      InterlockedExchange(&g_youfeng_next_gate_skill, (LONG)lo);
      InterlockedExchange(&g_youfeng_next_gate_config, (LONG)hi);
      InterlockedExchange(&g_youfeng_next_gate_allow_cleared, 0);
      InterlockedExchange(&g_youfeng_next_gate_qinggong, 1);
      InterlockedExchange(&g_youfeng_next_gate_due,
                          (LONG)(GetTickCount() + 650u));
      InterlockedExchange(&g_youfeng_next_gate_on, 1);
      g_shm->ret = 1;
      g_shm->status = ST_OK;
      SetErr("YOUFENG_QINGGONG_GATE armed 74F480 type1 before next ActionCast; Space untouched");
      return;
    }

    if (cmd == CMD_YOUFENG_MASH_CAST) {
      if (lo != 0x9563u || hi != 0x034Du) {
        SetErr("YOUFENG_MASH_CAST exact 0x9563/0x34D required");
        g_shm->status = ST_ERR;
        return;
      }
      if (InterlockedCompareExchange(&g_youfeng_mash_phase, 0, 0) != 0) {
        SetErr("YOUFENG_MASH_CAST already pending");
        g_shm->status = ST_ERR;
        return;
      }
      uint8_t* host = nullptr;
      uint8_t* cast = nullptr;
      uint8_t* mgr = nullptr;
      uint32_t skill = 0;
      uint32_t config = 0;
      uint32_t perform_type = 0;
      uint32_t gate1 = 0;
      __try {
        FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)
            NoteToLive(kNoteGetHostSide);
        host = get_host ? (uint8_t*)get_host() : nullptr;
        cast = host ? *(uint8_t**)(host + kSkillCastThisOff) : nullptr;
        mgr = host ? *(uint8_t**)(host + kHostPerformMgrOff) : nullptr;
        if (cast) {
          skill = *(uint32_t*)(cast + 0x10);
          config = *(uint32_t*)(cast + 0x18);
        }
        uint8_t* current = mgr ? *(uint8_t**)(mgr + 0x08) : nullptr;
        if (current) perform_type = *(uint32_t*)(current + 0x04);
        if (host) gate1 = *(uint32_t*)(host + kHostSessionStateOff + 4);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        host = nullptr;
        cast = nullptr;
        mgr = nullptr;
      }
      if (!host || !cast || !mgr || skill != lo || config != hi ||
          perform_type != 4u || gate1 < 1600u) {
        char buf[144];
        sprintf_s(buf,
                  "YOUFENG_MASH_CAST reject live=0x%X/0x%X type=%u gate1=%u",
                  (unsigned)skill, (unsigned)config, (unsigned)perform_type,
                  (unsigned)gate1);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      int action_ret = -1;
      __try {
        action_ret = RunActionPhase(cast, lo, hi, 1u);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("YOUFENG_MASH_CAST action down SEH");
        g_shm->status = ST_ERR;
        return;
      }
      if (action_ret != 105) {
        char buf[104];
        sprintf_s(buf, "YOUFENG_MASH_CAST action down rejected ret=%d",
                  action_ret);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      InterlockedExchange(&g_youfeng_mash_cast,
                          (LONG)(uint32_t)(uintptr_t)cast);
      InterlockedExchange(&g_youfeng_mash_host,
                          (LONG)(uint32_t)(uintptr_t)host);
      InterlockedExchange(&g_youfeng_mash_skill, (LONG)lo);
      InterlockedExchange(&g_youfeng_mash_config, (LONG)hi);
      InterlockedExchange(&g_youfeng_mash_due, (LONG)(GetTickCount() + 45u));
      InterlockedExchange(&g_youfeng_mash_phase, 1);
      g_shm->ret = action_ret;
      g_shm->status = ST_OK;
      SetErr("YOUFENG_MASH_CAST queued 0x34D down/up; no E07/X/Space/stop");
      return;
    }

    if (cmd == CMD_SET_TARGET) {
      FnSetTarget fn = (FnSetTarget)GetExport("?SetTarget@plg@@YA_N_J@Z");
      if (!fn) {
        SetErr("SetTarget export missing");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->ret = fn(lo, hi);
      g_shm->status = ST_OK;
      return;
    }

    if (cmd == CMD_PICK_ITEM) {
      FnPickItem fn = (FnPickItem)GetExport("?PickItem@plg@@YAX_J@Z");
      if (!fn) {
        SetErr("PickItem export missing");
        g_shm->status = ST_ERR;
        return;
      }
      fn(lo, hi);
      g_shm->ret = 0;
      g_shm->status = ST_OK;
      return;
    }

    if (cmd == CMD_USE_ITEM_IN_PACKAGE) {
      // UI-thread UseItem — same path as bag right-click (not CRT).
      // Shared: id_lo=package_index, id_hi=slot.
      FnUseItemInPackage fn =
          (FnUseItemInPackage)GetExport("?UseItemInPackage@plg@@YA_NHH@Z");
      if (!fn) {
        SetErr("UseItemInPackage export missing");
        g_shm->status = ST_ERR;
        return;
      }
      int pack = (int)lo;
      int slot = (int)hi;
      int call_ret = 0;
      __try {
        call_ret = fn(pack, slot);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("USE_ITEM_IN_PACKAGE SEH");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->ret = call_ret;
      g_shm->status = ST_OK;
      char buf[80];
      sprintf_s(buf, "USE_ITEM pack=%d slot=%d ret=%d", pack, slot, call_ret);
      SetErr(buf);
      return;
    }

    if (cmd == CMD_HOST_MOVE) {
      FnHostMove fn =
          (FnHostMove)GetExport("?HostMoveToScenePosition@plg@@YAHHMMM@Z");
      if (!fn) {
        SetErr("HostMove export missing");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->ret = fn(g_shm->mode, g_shm->x, g_shm->y, g_shm->z);
      g_shm->status = ST_OK;
      return;
    }

    if (cmd == CMD_CHOICE_OBJECT) {
      // Disasm call site 0xAEC92E: this = call 0x4AE470; thiscall @ 0x88F2A0(lo, hi).
      // Note VA 0xC8F2A0 rebases to live 0x88F2A0 — do NOT call as bare cdecl.
      uint32_t va = NoteToLive(kNoteChoiceObject);
      if (!va) {
        SetErr("ChoiceObject va=0");
        g_shm->status = ST_ERR;
        return;
      }
      void* self = ResolveChoiceThis();
      if (!self) {
        SetErr("ChoiceObject this=null");
        g_shm->status = ST_ERR;
        return;
      }
      FnChoiceObject fn = (FnChoiceObject)(uintptr_t)va;
      uint8_t ret = 0;
      __try {
        ret = fn(self, lo, hi);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("ChoiceObject SEH");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->ret = (int32_t)ret;
      g_shm->status = ST_OK;
      SetErr("ChoiceObject ok");
      return;
    }

    if (cmd == CMD_PICKUP_NOTE) {
      SetErr("UNSUPPORTED_UNVERIFIED_ACTION");
      g_shm->status = ST_ERR;
      return;
#if 0
      uint32_t va = NoteToLive(kNotePickupMatter);
      if (!va) {
        SetErr("Pickup va=0");
        g_shm->status = ST_ERR;
        return;
      }
      FnI64 fn = (FnI64)(uintptr_t)va;
      g_shm->ret = fn(lo, hi);
      g_shm->status = ST_OK;
      return;
#endif
    }

    if (cmd == CMD_AUTO_CLICK_DYN_MATTER) {
      SetErr("UNSUPPORTED_UNVERIFIED_ACTION");
      g_shm->status = ST_ERR;
      return;
    }

    if (cmd == CMD_AUTO_CLICK_MATTER) {
      // Open-chest / gather cast-bar. UI thread only.
      // Native path behind Lua AutoClickMatter(id, tid):
      //   thiscall 0x7496B0(this, id_lo, id_hi, tid)
      // Do not call the Lua binder at 0x6B2BB0 (expects lua_State* -> SEH).
      void* self = ResolveInteractThis();
      if (!self) {
        SetErr("MatterInteract this=null");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t va = NoteToLive(kNoteMatterInteract);
      if (!va) {
        SetErr("MatterInteract va=0");
        g_shm->status = ST_ERR;
        return;
      }
      FnMatterInteract fn = (FnMatterInteract)(uintptr_t)va;
      const uint32_t tid = (uint32_t)g_shm->tid;
      // hi must be object tag (often 0x02000000 for matter); lo is instance id.
      const uint8_t ok = fn(self, lo, hi, tid);
      g_shm->ret = (int32_t)ok;
      if (ok) {
        g_shm->status = ST_OK;
        SetErr("MatterInteract ok");
      } else {
        g_shm->status = ST_ERR;
        SetErr("MatterInteract false");
      }
      return;
    }

    if (cmd == CMD_CAPTURE_SCREEN) {
      // Save full frame via game path: <root>\Screenshots\YYYY-mm-dd HH-MM-SS.jpg
      // Must run on UI / render-friendly thread (this WndProc).
      uint32_t va = NoteToLive(kNoteCaptureScreen);
      if (!va) {
        SetErr("CaptureScreen va=0");
        g_shm->status = ST_ERR;
        return;
      }
      FnCaptureScreen fn = (FnCaptureScreen)(uintptr_t)va;
      fn();
      g_shm->ret = 1;
      g_shm->status = ST_OK;
      SetErr("CaptureScreen ok");
      return;
    }

    if (cmd == CMD_UI_CLICK) {
      // Background mouse sequence on game hwnd (works minimized when AUI
      // processes posted input). id_lo=cx, id_hi=cy client coords.
      // mode = button hold ms (0 -> default ~55ms) for human-like press.
      // tid = button: 0 left, 1 right.
      HWND h = g_hwnd;
      if ((!h || !IsWindow(h)) && g_shm && g_shm->hwnd) {
        h = (HWND)(uintptr_t)g_shm->hwnd;
      }
      if (!h || !IsWindow(h)) {
        SetErr("UI_CLICK no hwnd");
        g_shm->status = ST_ERR;
        return;
      }
      const int cx = (int)(lo & 0xFFFFu);
      const int cy = (int)(hi & 0xFFFFu);
      int hold_ms = 55;
      if (g_shm) {
        const int m = (int)g_shm->mode;
        if (m > 0 && m < 800) {
          hold_ms = m;
        }
      }
      const int btn = g_shm ? (int)g_shm->tid : 0; /* 0=L, 1=R */
      const LPARAM lp = (LPARAM)((cy << 16) | (cx & 0xFFFF));
      const bool right = (btn == 1);
      const WPARAM mk = right ? 0x0002u /* MK_RBUTTON */ : 0x0001u /* MK_LBUTTON */;
      const UINT msg_down = right ? 0x0204u /* WM_RBUTTONDOWN */ : 0x0201u /* WM_LBUTTONDOWN */;
      const UINT msg_up = right ? 0x0205u /* WM_RBUTTONUP */ : 0x0202u /* WM_LBUTTONUP */;
      PostMessageW(h, 0x0200 /* WM_MOUSEMOVE */, 0, lp);
      Sleep(8 + (DWORD)(hold_ms / 12));
      PostMessageW(h, msg_down, mk, lp);
      Sleep((DWORD)hold_ms);
      PostMessageW(h, msg_up, 0, lp);
      Sleep(6 + (DWORD)(hold_ms / 16));
      PostMessageW(h, 0x0200 /* WM_MOUSEMOVE */, 0, lp);
      g_shm->ret = 1;
      g_shm->status = ST_OK;
      SetErr(right ? "UI_CLICK R ok" : "UI_CLICK L ok");
      return;
    }

    if (cmd == CMD_UI_KEY) {
      // Key inject for stance / skill binds.
      // Many clients poll GetAsyncKeyState; pure PostMessage does nothing.
      // Strategy (all on UI thread):
      //   1) optional soft-foreground (AttachThreadInput) so DI/foreground gates pass
      //   2) SendInput scan-code + VK (updates async key state)
      //   3) keybd_event fallback
      //   4) PostMessage WM_KEY* to game hwnd
      // id_lo=vk, id_hi=action: 0=down, 1=up, 2=press; mode=hold_ms for press.
      HWND h = g_hwnd;
      if ((!h || !IsWindow(h)) && g_shm && g_shm->hwnd) {
        h = (HWND)(uintptr_t)g_shm->hwnd;
      }
      const UINT vk_in = (UINT)(lo & 0xFFu);
      if (!vk_in) {
        SetErr("UI_KEY vk=0");
        g_shm->status = ST_ERR;
        return;
      }
      // Map generic modifiers to left variants (more reliable scan codes).
      UINT vk = vk_in;
      if (vk == 0x10u /* VK_SHIFT */) vk = 0xA0u;   /* LSHIFT */
      if (vk == 0x11u /* VK_CONTROL */) vk = 0xA2u; /* LCONTROL */
      if (vk == 0x12u /* VK_MENU */) vk = 0xA4u;    /* LMENU */
      const int action = (int)(hi & 0xFFu); /* 0 down, 1 up, 2 press */
      int hold_ms = 40;
      if (g_shm) {
        // Low 12 bits = hold_ms; bit 0x1000 = no_focus (do not steal FG).
        const int m = (int)g_shm->mode & 0x0FFF;
        if (m > 0 && m < 2000) {
          hold_ms = m;
        }
      }
      const UINT sc = MapVirtualKeyW(vk, MAPVK_VK_TO_VSC);
      const LPARAM lp_down =
          (LPARAM)(1u | ((sc & 0xFFu) << 16));
      const LPARAM lp_up =
          (LPARAM)(1u | ((sc & 0xFFu) << 16) | (1u << 30) | (1u << 31));
      const bool is_ext =
          (vk == 0xA3u /* RCONTROL */ || vk == 0xA5u /* RMENU */ ||
           vk == 0x2Du /* INSERT */ || vk == 0x2Eu /* DELETE */ ||
           vk == 0x21u /* PRIOR */ || vk == 0x22u /* NEXT */ ||
           vk == 0x23u /* END */ || vk == 0x24u /* HOME */ ||
           (vk >= 0x25u && vk <= 0x28u) /* arrows */);

      // Soft-foreground: some engines ignore async keys unless hwnd is FG.
      // Best-effort; NEVER force FG for Escape (yaolu cancel/close) so background
      // automation does not steal the user's focus.
      // mode bit 0x1000 = no_focus (set by Python ui_key for Esc / cancel).
      DWORD fg_tid = 0;
      DWORD our_tid = GetCurrentThreadId();
      bool attached = false;
      const bool no_focus =
          (g_shm && (((int)g_shm->mode) & 0x1000)) || (vk == 0x1Bu /* VK_ESCAPE */);
      if (!no_focus && h && IsWindow(h)) {
        HWND fg = GetForegroundWindow();
        if (fg != h) {
          fg_tid = GetWindowThreadProcessId(fg ? fg : h, nullptr);
          if (fg_tid && fg_tid != our_tid) {
            if (AttachThreadInput(our_tid, fg_tid, TRUE)) {
              attached = true;
            }
          }
          // AllowSetForegroundWindow is best-effort from in-process.
          BringWindowToTop(h);
          SetForegroundWindow(h);
          SetFocus(h);
        }
      }

      auto send_one = [&](bool key_up) -> UINT {
        INPUT ins[2];
        ZeroMemory(ins, sizeof(ins));
        // 0: scan-code path (DirectInput-friendly)
        ins[0].type = INPUT_KEYBOARD;
        ins[0].ki.wVk = 0;
        ins[0].ki.wScan = (WORD)(sc & 0xFFu);
        ins[0].ki.dwFlags =
            KEYEVENTF_SCANCODE | (key_up ? KEYEVENTF_KEYUP : 0) |
            (is_ext ? KEYEVENTF_EXTENDEDKEY : 0);
        // 1: VK path (GetAsyncKeyState / Win32)
        ins[1].type = INPUT_KEYBOARD;
        ins[1].ki.wVk = (WORD)vk;
        ins[1].ki.wScan = (WORD)(sc & 0xFFu);
        ins[1].ki.dwFlags =
            (key_up ? KEYEVENTF_KEYUP : 0) |
            (is_ext ? KEYEVENTF_EXTENDEDKEY : 0);
        UINT n = SendInput(2, ins, sizeof(INPUT));
        // Legacy fallback
        DWORD ke_flags = key_up ? KEYEVENTF_KEYUP : 0;
        if (is_ext) ke_flags |= KEYEVENTF_EXTENDEDKEY;
        keybd_event((BYTE)vk, (BYTE)(sc & 0xFFu), ke_flags, 0);
        // Also toggle generic modifier so VK_SHIFT bit is set for binds.
        if (vk == 0xA0u /* LSHIFT */ || vk == 0xA1u /* RSHIFT */) {
          keybd_event((BYTE)0x10 /* VK_SHIFT */, (BYTE)(sc & 0xFFu), ke_flags,
                      0);
        }
        return n;
      };

      auto post_msg = [&](bool key_up) {
        if (!h || !IsWindow(h)) return;
        if (key_up) {
          PostMessageW(h, 0x0101 /* WM_KEYUP */, (WPARAM)vk, lp_up);
          if (vk == 0xA0u || vk == 0xA1u) {
            PostMessageW(h, 0x0101, (WPARAM)0x10, lp_up);
          }
        } else {
          PostMessageW(h, 0x0100 /* WM_KEYDOWN */, (WPARAM)vk, lp_down);
          if (vk == 0xA0u || vk == 0xA1u) {
            PostMessageW(h, 0x0100, (WPARAM)0x10, lp_down);
          }
        }
      };

      UINT n = 0;
      if (action == 0) {
        n = send_one(false);
        post_msg(false);
      } else if (action == 1) {
        n = send_one(true);
        post_msg(true);
      } else {
        n = send_one(false);
        post_msg(false);
        Sleep((DWORD)hold_ms);
        n += send_one(true);
        post_msg(true);
      }

      if (attached && fg_tid) {
        AttachThreadInput(our_tid, fg_tid, FALSE);
      }

      if (n == 0) {
        SetErr("UI_KEY SendInput=0");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->ret = (int32_t)n;
      g_shm->status = ST_OK;
      SetErr("UI_KEY ok");
      return;
    }

    if (cmd == CMD_JIANGLONG_RUNTIME_RESOLVE) {
      // Read only: locate the character's loaded Jianglong skill object.
      // The returned skill id is used later by CAST_SKILL; this command never
      // invokes input, packet emission, or a client action path.
      const uint32_t pkg_va = NoteToLive(kNoteGetPkgSide);
      const uint32_t look_va = NoteToLive(kNoteSkillById);
      if (!pkg_va || !look_va) {
        SetErr("JIANGLONG_RUNTIME_RESOLVE va=0");
        g_shm->status = ST_ERR;
        return;
      }
      void* mgr = nullptr;
      __try {
        FnGetPkgSide get_pkg = (FnGetPkgSide)(uintptr_t)pkg_va;
        void* pkg = get_pkg();
        if (pkg) mgr = *(void**)((uint8_t*)pkg + 0x24);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        mgr = nullptr;
      }
      if (!mgr) {
        SetErr("JIANGLONG_RUNTIME_RESOLVE mgr=null");
        g_shm->status = ST_ERR;
        return;
      }
      if (g_shm->mode == 1) {
        // Standalone diagnostic: ordinal N among loaded SkillById records.
        // It reads object fields only and never invokes any action/session path.
        const uint32_t wanted = lo ? lo : 1u;
        FnSkillById probe_look = (FnSkillById)(uintptr_t)look_va;
        uint32_t seen = 0;
        for (uint32_t skill_id = 1; skill_id <= 0x4000u; ++skill_id) {
          void* skill = nullptr;
          uint32_t config = 0;
          uint32_t s0 = 0, s4 = 0, s10 = 0, s18 = 0;
          __try {
            skill = probe_look(mgr, skill_id);
            if (skill) {
              uint8_t* p = (uint8_t*)skill;
              s0 = *(uint32_t*)(p + 0x0);
              s4 = *(uint32_t*)(p + 0x4);
              s10 = *(uint32_t*)(p + 0x10);
              s18 = *(uint32_t*)(p + 0x18);
              config = s18;
            }
          } __except (EXCEPTION_EXECUTE_HANDLER) {
            skill = nullptr;
          }
          if (!skill) continue;
          ++seen;
          if (seen != wanted) continue;
          g_shm->ret = (int32_t)config;
          g_shm->tid = (int32_t)skill_id;
          g_shm->id_hi = (uint32_t)(uintptr_t)skill;
          char buf[176];
          sprintf_s(buf, _TRUNCATE,
                    "JIANGLONG_RUNTIME_PROBE ordinal=%u sid=0x%X cfg=0x%X skill=0x%X fields=%X/%X/%X/%X",
                    (unsigned)seen, (unsigned)skill_id, (unsigned)config,
                    (unsigned)(uintptr_t)skill, (unsigned)s0, (unsigned)s4,
                    (unsigned)s10, (unsigned)s18);
          SetErr(buf);
          g_shm->status = ST_OK;
          return;
        }
        char buf[96];
        sprintf_s(buf, _TRUNCATE,
                  "JIANGLONG_RUNTIME_PROBE ordinal=%u unavailable total=%u",
                  (unsigned)wanted, (unsigned)seen);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      const uint32_t first = lo ? lo : 1u;
      const uint32_t last = hi ? hi : 0x4000u;
      if (last < first || last - first > 0x10000u) {
        SetErr("JIANGLONG_RUNTIME_RESOLVE invalid range");
        g_shm->status = ST_ERR;
        return;
      }
      // Jianglong's packet fields are character/session specific. Do not derive
      // them here: locate the loaded skill by its stable action tag, then let
      // CAST_SKILL enter the native client path and create its own session.
      // Jianglong's native session uses type 0x00012607u and action 0x00002305u.
      FnSkillById look = (FnSkillById)(uintptr_t)look_va;
      const uint32_t kJianglongActionTag = 0x00002305u;
      uint32_t found_skill_id = 0, found_skill_ptr = 0, inspected = 0;
      for (uint32_t skill_id = first; skill_id <= last; ++skill_id) {
        void* skill = nullptr;
        uint32_t action_tag = 0, child_ptr = 0, child_tag = 0;
        __try {
          skill = look(mgr, skill_id);
          if (skill) {
            uint8_t* p = (uint8_t*)skill;
            action_tag = *(uint32_t*)(p + 0xEA4);
            child_ptr = *(uint32_t*)(p + 0x4);
            if (child_ptr >= 0x10000u && child_ptr < 0x80000000u) {
              child_tag = *(uint32_t*)(uintptr_t)child_ptr;
            }
          }
        } __except (EXCEPTION_EXECUTE_HANDLER) {
          skill = nullptr;
          action_tag = child_ptr = child_tag = 0;
        }
        if (!skill) continue;
        ++inspected;
        if (action_tag != kJianglongActionTag) continue;
        if (child_ptr && child_tag != kJianglongActionTag) continue;
        found_skill_id = skill_id;
        found_skill_ptr = (uint32_t)(uintptr_t)skill;
        break;
      }
      g_shm->ret = (int32_t)found_skill_id;
      g_shm->id_hi = found_skill_ptr;
      if (!found_skill_id) {
        char buf[128];
        sprintf_s(buf, _TRUNCATE,
                  "JIANGLONG_RUNTIME_RESOLVE none scanned=%u range=0x%X..0x%X",
                  (unsigned)inspected, (unsigned)first, (unsigned)last);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      char buf[160];
      sprintf_s(buf, _TRUNCATE,
                "JIANGLONG_RUNTIME_RESOLVE skill=0x%X action=0x2305 scanned=%u ptr=0x%X",
                (unsigned)found_skill_id, (unsigned)inspected,
                (unsigned)found_skill_ptr);
      SetErr(buf);
      g_shm->status = ST_OK;
      return;
    }    if (cmd == CMD_CAST_SKILL) {
      // Live generic cast / interrupt:
      //   id_lo = skill config id (hand_probe X-block = 0xE07)
      //   mode  = 0 skill-id
      // Path: GetPkgSide@0x4AE420 -> [+0x24] mgr -> SkillById@0x533250
      //       optional skill-bar scan for id fields
      //       CastOuter@0x53E110(skill,-1,0,0) -> 0x75F000
      // Reports cast-this +10/+18 before/after so lab can detect no-op ret=0.
      const uint32_t skill_id = lo;
      if (!skill_id) {
        SetErr("CAST_SKILL skill_id=0");
        g_shm->status = ST_ERR;
        return;
      }
      const uint32_t cast_mode = g_shm ? (uint32_t)g_shm->mode : 0u;
      if (cast_mode != 0u && cast_mode != 2u) {
        SetErr("CAST_SKILL mode 0/2 required");
        g_shm->status = ST_ERR;
        return;
      }
      const uint32_t pkg_va = NoteToLive(kNoteGetPkgSide);
      const uint32_t look_va = NoteToLive(kNoteSkillById);
      const uint32_t cast_va = NoteToLive(kNoteCastOuter);
      const uint32_t bar_root_va = NoteToLive(kNoteGetSkillBarRoot);
      const uint32_t bar_look_va = NoteToLive(kNoteSkillBarLookup);
      const uint32_t host_va = NoteToLive(kNoteGetHostSide);
      if (!pkg_va || !look_va || !cast_va) {
        SetErr("CAST_SKILL va=0");
        g_shm->status = ST_ERR;
        return;
      }

      void* mgr = nullptr;
      __try {
        FnGetPkgSide get_pkg = (FnGetPkgSide)(uintptr_t)pkg_va;
        void* pkg = get_pkg();
        if (pkg) mgr = *(void**)((uint8_t*)pkg + 0x24);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        mgr = nullptr;
      }
      if (!mgr) {
        SetErr("CAST_SKILL mgr=null");
        g_shm->status = ST_ERR;
        return;
      }

      void* skill = nullptr;
      const char* src = "byid";
      __try {
        FnSkillById look = (FnSkillById)(uintptr_t)look_va;
        skill = look(mgr, skill_id);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        skill = nullptr;
      }

      // Read common id-ish fields from a skill-like object.
      uint32_t skill_plus4 = 0, skill_cfg = 0, skill_d0 = 0, skill_d10 = 0;
      int id_match = 0;
      if (skill) {
        __try {
          skill_plus4 = *(uint32_t*)((uint8_t*)skill + 4);
          skill_cfg = *(uint32_t*)((uint8_t*)skill + 0x18);
          skill_d0 = *(uint32_t*)((uint8_t*)skill + 0);
          skill_d10 = *(uint32_t*)((uint8_t*)skill + 0x10);
          const uint32_t offs[8] = {0, 8, 0xC, 0x10, 0x14, 0x18, 0x1C, 0x20};
          for (int i = 0; i < 8; ++i) {
            if (*(uint32_t*)((uint8_t*)skill + offs[i]) == skill_id) {
              id_match = 1;
              break;
            }
          }
        } __except (EXCEPTION_EXECUTE_HANDLER) {
          skill_plus4 = 0;
          id_match = 0;
        }
      }

      // If book lookup is weak/mismatched, scan skill bar slots for +0x18==id.
      if (cast_mode == 0u && (!skill || !id_match) && bar_root_va && bar_look_va) {
        void* bar_root = nullptr;
        __try {
          FnGetSkillBarRoot get_root = (FnGetSkillBarRoot)(uintptr_t)bar_root_va;
          void* a = get_root();
          if (a) {
            void* b = *(void**)((uint8_t*)a + 0x0C);
            if (b) bar_root = *(void**)((uint8_t*)b + 0x0C);
          }
        } __except (EXCEPTION_EXECUTE_HANDLER) {
          bar_root = nullptr;
        }
        if (bar_root) {
          for (uint32_t slot = 0; slot < 32u; ++slot) {
            void* cand = nullptr;
            __try {
              FnSkillBarLookup lookb = (FnSkillBarLookup)(uintptr_t)bar_look_va;
              cand = lookb(bar_root, slot, 0u);
            } __except (EXCEPTION_EXECUTE_HANDLER) {
              cand = nullptr;
            }
            if (!cand) continue;
            uint32_t c18 = 0, c4 = 0, match = 0;
            __try {
              c18 = *(uint32_t*)((uint8_t*)cand + 0x18);
              c4 = *(uint32_t*)((uint8_t*)cand + 4);
              const uint32_t offs[8] = {0, 8, 0xC, 0x10, 0x14, 0x18, 0x1C, 0x20};
              for (int i = 0; i < 8; ++i) {
                if (*(uint32_t*)((uint8_t*)cand + offs[i]) == skill_id) {
                  match = 1;
                  break;
                }
              }
            } __except (EXCEPTION_EXECUTE_HANDLER) {
              c18 = 0;
              c4 = 0;
              match = 0;
            }
            if (match || c18 == skill_id) {
              skill = cand;
              src = "bar";
              skill_plus4 = c4;
              skill_cfg = c18;
              id_match = 1;
              __try {
                skill_d0 = *(uint32_t*)((uint8_t*)cand + 0);
                skill_d10 = *(uint32_t*)((uint8_t*)cand + 0x10);
              } __except (EXCEPTION_EXECUTE_HANDLER) {
              }
              break;
            }
          }
        }
      }

      if (!skill) {
        SetErr("CAST_SKILL skill=null (id not in book/bar?)");
        g_shm->status = ST_ERR;
        return;
      }
      if (!skill_plus4) {
        SetErr("CAST_SKILL skill+4=0");
        g_shm->status = ST_ERR;
        return;
      }

      void* cast_this = nullptr;
      if (host_va) {
        __try {
          FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)host_va;
          void* host = get_host();
          if (host) cast_this = *(void**)((uint8_t*)host + kSkillCastThisOff);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
          cast_this = nullptr;
        }
      }
      uint32_t b10 = 0, b18 = 0, b4a0 = 0;
      if (cast_this) {
        __try {
          b10 = *(uint32_t*)((uint8_t*)cast_this + 0x10);
          b18 = *(uint32_t*)((uint8_t*)cast_this + 0x18);
          b4a0 = *(uint32_t*)((uint8_t*)cast_this + 0x4A0);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
        }
      }

      int32_t cret = -1;
      int32_t release_ret = -1;
      uint32_t action_tag = 0;
      __try {
        action_tag = *(uint32_t*)((uint8_t*)skill + 0xEA4);
        if (cast_mode == 2u) {
          // Full internal action path. Type/tag are fixed skill semantics; the
          // client still creates all character/session-specific packet fields.
          const uint32_t action_type = hi;
          const uint32_t expected_tag = (uint32_t)g_shm->tid;
          if (!cast_this || !action_type || !expected_tag ||
              action_tag != expected_tag) {
            SetErr("CAST_SKILL_ACTION identity/context rejected");
            g_shm->status = ST_ERR;
            return;
          }
          cret = RunActionPhase(cast_this, action_type, action_tag, 1u);
          if (cret == 105) {
            Sleep(45);
            release_ret = RunActionPhase(cast_this, action_type, action_tag, 2u);
          }
        } else {
          FnCastOuter cast_fn = (FnCastOuter)(uintptr_t)cast_va;
          cret = cast_fn(skill, 0xFFFFFFFFu, 0u, 0u);
        }
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("CAST_SKILL SEH");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t a10 = 0, a18 = 0, a4a0 = 0;
      if (cast_this) {
        __try {
          a10 = *(uint32_t*)((uint8_t*)cast_this + 0x10);
          a18 = *(uint32_t*)((uint8_t*)cast_this + 0x18);
          a4a0 = *(uint32_t*)((uint8_t*)cast_this + 0x4A0);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
        }
      }
      const int mutated =
          (a10 != b10) || (a18 != b18) || (a4a0 != b4a0) || (a18 == skill_id);

      g_shm->ret = cret;
      const bool action_ok = cast_mode != 2u ||
                             (cret == 105 && release_ret == 105);
      g_shm->status = action_ok ? ST_OK : ST_ERR;
      char buf[240];
      sprintf_s(buf,
                "CAST_SKILL mode=%u id=0x%X src=%s match=%d cfg=0x%X action=0x%X "
                "ret=%d release=%d mut=%d b10=0x%X b18=0x%X a10=0x%X a18=0x%X",
                (unsigned)cast_mode, (unsigned)skill_id, src, id_match,
                (unsigned)skill_cfg, (unsigned)action_tag, (int)cret,
                (int)release_ret, mutated, (unsigned)b10, (unsigned)b18,
                (unsigned)a10, (unsigned)a18);
      SetErr(buf);
      return;
    }
    if (cmd == CMD_KEY_HOLD) {
      // Solution-2: process-local key force via IAT + inline hooks (default no SoftSend).
      // id_lo=vk (1..0xFE), id_hi=0 off / 1 on / 2 status-only / 3 clear-all
      // mode bit0=level_only keep-alive
      // mode bit1=allow_softsend (opt-in system pollution)
      // mode bit2=install hooks only (no force change when id_hi=2)
      // @author by ak
      const UINT vk = (UINT)(lo & 0xFFu);
      const int action = (int)(hi & 0xFFu);
      const int mode = g_shm ? (int)g_shm->mode : 0;
      const bool level_only = (mode & 1) != 0;
      const bool allow_soft = (mode & 2) != 0;
      InterlockedExchange(&g_hold_allow_softsend, allow_soft ? 1 : 0);

      if (action == 3) {
        // clear all forced vks
        for (int i = 0; i < 256; ++i) g_force_vk[i] = 0;
        RecountForceAny();
        void* inp = ResolveInputThis();
        if (inp) {
          // leave table; next UpdateKeys will clear if GAKS up
          CallUpdateKeys(inp);
        }
        g_shm->ret = 0;
        g_shm->status = ST_OK;
        SetErr("KEY_HOLD CLEAR_ALL ok");
        return;
      }

      if (action == 2 || (mode & 4)) {
        // ensure hooks + status
        if (!InstallKeyStateHooks()) {
          SetErr("KEY_HOLD hook install fail");
          g_shm->status = ST_ERR;
          return;
        }
        char buf[180];
        FormatHoldDiag(buf, sizeof(buf), vk ? vk : 0x20u, 0x10, true);
        g_shm->ret = g_iat_patch_count > 0 ? g_iat_patch_count : 1;
        g_shm->status = ST_OK;
        SetErr(buf);
        return;
      }

      if (!vk || vk > 0xFEu) {
        SetErr("KEY_HOLD vk invalid");
        g_shm->status = ST_ERR;
        return;
      }

      if (!InstallKeyStateHooks()) {
        SetErr("KEY_HOLD hook install fail");
        g_shm->status = ST_ERR;
        return;
      }

      if (action != 0) {
        // ON
        LONG was = g_force_vk[vk & 0xFFu];
        g_force_vk[vk & 0xFFu] = 1;
        // Shift group convenience: force LSHIFT aliases when generic shift
        if (vk == 0x10u) {
          g_force_vk[0xA0] = 1;
        }
        RecountForceAny();
        bool only_level = level_only || (was != 0);
        int wrote = SyncForcedVkState(vk, true, only_level, allow_soft);
        if (vk == 0x10u) {
          SyncForcedVkState(0xA0u, true, true, allow_soft);
        }
        void* inp = ResolveInputThis();
        if (!inp) inp = g_input_this;
        if (!inp) {
          // hooks alone may still satisfy GetModMask/IsKeyTable if input resolves later
          g_shm->ret = wrote > 0 ? wrote : 1;
          g_shm->status = ST_OK;
          char buf[180];
          FormatHoldDiag(buf, sizeof(buf), vk, wrote | 0x10, only_level);
          SetErr(buf);
          return;
        }
        g_shm->ret = wrote > 0 ? wrote : 1;
        g_shm->status = ST_OK;
        {
          char buf[180];
          FormatHoldDiag(buf, sizeof(buf), vk, wrote, only_level);
          SetErr(buf);
        }
      } else {
        // OFF
        LONG was = g_force_vk[vk & 0xFFu];
        g_force_vk[vk & 0xFFu] = 0;
        if (vk == 0x10u) {
          g_force_vk[0xA0] = 0;
          g_force_vk[0xA1] = 0;
        }
        RecountForceAny();
        int wrote = SyncForcedVkState(vk, false, /*level_only=*/(was == 0), allow_soft);
        if (vk == 0x10u) {
          SyncForcedVkState(0xA0u, false, true, allow_soft);
        }
        g_shm->ret = wrote;
        g_shm->status = ST_OK;
        {
          char buf[200];
          FormatHoldDiag(buf, sizeof(buf), vk, wrote, true);
          char out[220];
          _snprintf_s(out, _TRUNCATE, was ? "OFF %s" : "alreadyOFF %s", buf);
          SetErr(out);
        }
      }
      return;
    }

    if (cmd == CMD_KEY_FORCE) {
      // Background Shift reticle (no IAT):
      //   edge once: InjectKey KEYDOWN + one SoftSend LSHIFT down
      //   hold: UI timer only re-writes key table/mod mask (no extra edges)
      // id_lo=vk (Shift group), id_hi=0 clear / 1 force-down
      // mode bit0: 1 = level-only re-assert (Python keep-alive, no edge)
      const UINT vk = (UINT)(lo & 0xFFu);
      const int force_on = (int)(hi & 0xFFu) != 0;
      const int level_only = g_shm && ((int)g_shm->mode & 1);
      if (vk && vk != 0x10u && vk != 0xA0u && vk != 0xA1u) {
        SetErr("KEY_FORCE only Shift for now");
        g_shm->status = ST_ERR;
        return;
      }
      if (force_on) {
        LONG was = InterlockedExchange(&g_force_shift, 1);
        // Already holding: never re-fire edges (would toggle reticle off).
        bool only_level = level_only || (was != 0);
        int wrote = SyncForcedShiftState(true, only_level);
        void* inp = ResolveInputThis();
        if (!inp) inp = g_input_this;
        if (!inp) {
          InterlockedExchange(&g_force_shift, 0);
          SoftSendShiftGaks(true);
          SetErr("KEY_FORCE input this null (soft GAKS only)");
          g_shm->status = ST_ERR;
          return;
        }
        g_shm->ret = wrote > 0 ? wrote : 1;
        g_shm->status = ST_OK;
        {
          char buf[180];
          FormatForceDiag(buf, sizeof(buf), wrote, only_level);
          SetErr(buf);
        }
      } else {
        LONG was = InterlockedExchange(&g_force_shift, 0);
        int wrote = SyncForcedShiftState(false, /*level_only=*/(was == 0));
        g_shm->ret = wrote;
        g_shm->status = ST_OK;
        {
          char buf[180];
          FormatForceDiag(buf, sizeof(buf), wrote, /*only_level=*/true);
          // Prefix OFF so UI can tell stop path.
          char out[200];
          _snprintf_s(out, _TRUNCATE, was ? "OFF %s" : "alreadyOFF %s", buf);
          SetErr(out);
        }
      }
      return;
    }

    if (cmd == CMD_KEY_TRACE) {
      SetErr("UNSUPPORTED_UNSAFE_HOOK");
      g_shm->status = ST_ERR;
      return;
#if 0
      // id_hi=1 start, 0 stop+dump; id_lo=vk filter (0=Shift group)
      // mode reserved (max entries already fixed).
      const int start = (int)(hi & 0xFFu) != 0;
      const UINT vk_filt = (UINT)(lo & 0xFFu);
      if (start) {
        // Clear any force so real key state is visible in sample_real.
        InterlockedExchange(&g_force_shift, 0);
        if (!InstallKeyStateHooks()) {
          SetErr("KEY_TRACE hook install fail");
          g_shm->status = ST_ERR;
          return;
        }
        InterlockedExchange(&g_key_trace_filter_vk, (LONG)vk_filt);
        KeyTraceReset();
        g_key_trace_path[0] = 0;
        InterlockedExchange(&g_key_trace_on, 1);
        g_shm->ret = g_iat_patch_count > 0 ? g_iat_patch_count : 1;
        g_shm->status = ST_OK;
        {
          char buf[96];
          _snprintf_s(buf, _TRUNCATE,
                      "KEY_TRACE ON patches=%d hold real Shift then STOP",
                      g_iat_patch_count);
          SetErr(buf);
        }
      } else {
        InterlockedExchange(&g_key_trace_on, 0);
        char path[MAX_PATH] = {0};
        bool dumped = KeyTraceDumpFile(path, sizeof(path));
        if (dumped) {
          strncpy_s(g_key_trace_path, path, _TRUNCATE);
        }
        LONG hits = InterlockedCompareExchange(&g_key_trace_hits, 0, 0);
        g_shm->ret = g_key_trace_count;
        g_shm->status = ST_OK;
        {
          char buf[128];
          if (dumped && path[0]) {
            // Full path in note so helper can open the log.
            _snprintf_s(buf, _TRUNCATE, "KEY_TRACE %s u=%d h=%ld", path,
                        g_key_trace_count, hits);
            if (strlen(buf) >= 120) {
              const char* base = path;
              for (const char* p = path; *p; ++p) {
                if (*p == '\\' || *p == '/') base = p + 1;
              }
              _snprintf_s(buf, _TRUNCATE, "KEY_TRACE %s u=%d h=%ld", base,
                          g_key_trace_count, hits);
            }
          } else {
            _snprintf_s(buf, _TRUNCATE, "KEY_TRACE OFF unique=%d dump_fail",
                        g_key_trace_count);
          }
          SetErr(buf);
        }
      }
      return;
#endif
    }

    if (cmd == CMD_KEY_DIAG) {
      // Lightweight msg-queue probe plus controller-level free-aim bind probe.
      // id_hi=1 start, 0 stop+report, 2 immediate controller snapshot.
      const int action = (int)(hi & 0xFFu);
      if (action == 2) {
        char buf[180];
        FormatControllerDiag(buf, sizeof(buf));
        g_shm->ret = 1;
        g_shm->status = ST_OK;
        SetErr(buf);
      } else if (action != 0) {
        if (!InstallMsgDiagHook()) {
          g_shm->status = ST_ERR;
          // error set by InstallMsgDiagHook
          return;
        }
        g_shm->ret = 1;
        g_shm->status = ST_OK;
        SetErr("KEY_DIAG ON — press real Shift or KEY_FORCE then STOP to read");
      } else {
        LONG msgs = InterlockedCompareExchange(&g_diag_msgs, 0, 0);
        LONG shift = InterlockedCompareExchange(&g_diag_shift, 0, 0);
        LONG sys = InterlockedCompareExchange(&g_diag_syskey, 0, 0);
        RemoveMsgDiagHook();
        char ctrl[128];
        FormatControllerDiag(ctrl, sizeof(ctrl));
        g_shm->ret = (int)(shift & 0xFFFF);
        g_shm->status = ST_OK;
        char buf[200];
        _snprintf_s(buf, _TRUNCATE,
            "KEY_DIAG msgs=%ld shift_wm=%ld syskey=%ld %s",
            msgs, shift, sys, ctrl);
        SetErr(buf);
      }
      return;
    }

    if (cmd == CMD_AQ_SUBMIT) {
      // Pure AUI Btn_Ok path: thiscall 0x8CD2F0(dlg, 0). id_lo = dialog*.
      if (!lo) {
        SetErr("AQ_SUBMIT dlg=null");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t va = NoteToLive(kNoteAqSubmit);
      if (!va) {
        SetErr("AQ_SUBMIT va=0");
        g_shm->status = ST_ERR;
        return;
      }
      FnAqSubmit fn = (FnAqSubmit)(uintptr_t)va;
      fn((void*)(uintptr_t)lo, 0u);
      g_shm->ret = 1;
      g_shm->status = ST_OK;
      SetErr("AQ_SUBMIT ok");
      return;
    }

    if (cmd == CMD_INSTANCE_ENTER) {
      // Direct enter from 副本列表 Btn_Enter path (no UI click).
      // Disasm 0x930780 / 0xDD1810:
      //   host = GetHostSide()@0x4AE400; host_lo/hi @ +0x140/+0x144
      //   net  = GetNetRoot()@0x4AE440 (*global+0x2C)
      //   this = net + 0x1C8
      //   thiscall 0xCC6F80(this, host_lo, host_hi, inst, mode, flag)
      // Fallback: cdecl packet builder 0xCC81B0 (same 0x58 payload).
      // Shared: id_lo=inst_id, mode=difficulty (0=normal), tid=flag (1 typical).
      if (!lo) {
        SetErr("INSTANCE_ENTER inst=0");
        g_shm->status = ST_ERR;
        return;
      }
      void* host = nullptr;
      {
        uint32_t hva = NoteToLive(kNoteGetHostSide);
        if (!hva) {
          SetErr("INSTANCE_ENTER GetHostSide va=0");
          g_shm->status = ST_ERR;
          return;
        }
        FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)hva;
        __try {
          host = get_host();
        } __except (EXCEPTION_EXECUTE_HANDLER) {
          host = nullptr;
        }
      }
      if (!host) {
        SetErr("INSTANCE_ENTER host=null");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t host_lo = 0;
      uint32_t host_hi = 0;
      __try {
        host_lo = *(uint32_t*)((uint8_t*)host + kHostPlayerIdLoOff);
        host_hi = *(uint32_t*)((uint8_t*)host + kHostPlayerIdHiOff);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("INSTANCE_ENTER host id read fail");
        g_shm->status = ST_ERR;
        return;
      }
      if (!host_lo) {
        SetErr("INSTANCE_ENTER host_id=0");
        g_shm->status = ST_ERR;
        return;
      }
      void* net = ResolveNetRoot();
      if (!net) {
        SetErr("INSTANCE_ENTER net=null");
        g_shm->status = ST_ERR;
        return;
      }
      void* enter_this = (void*)((uint8_t*)net + kInstanceEnterThisOff);
      const uint32_t inst_id = lo;
      const uint32_t diff = (uint32_t)(g_shm->mode >= 0 ? g_shm->mode : 0);
      const uint32_t flag = (uint32_t)(g_shm->tid != 0 ? g_shm->tid : 1);

      uint32_t va = NoteToLive(kNoteInstanceEnter);
      if (va) {
        FnInstanceEnter fn = (FnInstanceEnter)(uintptr_t)va;
        __try {
          fn(enter_this, host_lo, host_hi, inst_id, diff, flag);
          g_shm->ret = 1;
          g_shm->status = ST_OK;
          SetErr("INSTANCE_ENTER ok");
          return;
        } __except (EXCEPTION_EXECUTE_HANDLER) {
          // fall through to direct packet builder
        }
      }

      uint32_t pva = NoteToLive(kNoteInstanceEnterPkt);
      if (!pva) {
        SetErr("INSTANCE_ENTER va=0");
        g_shm->status = ST_ERR;
        return;
      }
      FnInstanceEnterPkt pfn = (FnInstanceEnterPkt)(uintptr_t)pva;
      __try {
        pfn(host_lo, host_hi, inst_id, diff, flag);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("INSTANCE_ENTER exception");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->ret = 2;
      g_shm->status = ST_OK;
      SetErr("INSTANCE_ENTER pkt ok");
      return;
    }


    if (cmd == CMD_CHANGE_LINE) {
      // CDlgLineList list-click path: cdecl 0xCCBC70(line_id).
      // Packet: u16 opcode=0x7A, u8 line_id (size 3) via GetNetRoot send.
      // Shared: id_lo = line_id (0..255). 主线/0线 both use 0.
      const uint32_t line_id = lo & 0xFFu;
      uint32_t va = NoteToLive(kNoteChangeLine);
      if (!va) {
        SetErr("CHANGE_LINE va=0");
        g_shm->status = ST_ERR;
        return;
      }
      FnChangeLine fn = (FnChangeLine)(uintptr_t)va;
      __try {
        fn(line_id);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("CHANGE_LINE exception");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->ret = (int32_t)line_id;
      g_shm->status = ST_OK;
      SetErr("CHANGE_LINE ok");
      return;
    }


    if (cmd == CMD_TEAM_INVITE) {
      // Zeroed TeamInvite c2s 0x1261 via CD04D0 (NOT raw CEC4E0).
      // CEC4E0 leaves pkt+0x20 uncleared; serialize still sends it → server reject.
      // this = GetNetRoot/host-side @ 0x4AE440 (id @ +0x240).
      // Shared: id_lo/id_hi = invitee role id.
      if (!lo && !hi) {
        SetErr("TEAM_INVITE id=0");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t hva = NoteToLive(kNoteGetNetRoot);
      uint32_t sva = NoteToLive(kNotePktSend);
      if (!hva || !sva) {
        SetErr("TEAM_INVITE va=0");
        g_shm->status = ST_ERR;
        return;
      }
      void* host = nullptr;
      __try {
        FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)hva;
        host = get_host();
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        host = nullptr;
      }
      if (!host) {
        SetErr("TEAM_INVITE host=null");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t self_lo = 0, self_hi = 0;
      __try {
        self_lo = *(uint32_t*)((uint8_t*)host + kTeamInviteHostIdLoOff);
        self_hi = *(uint32_t*)((uint8_t*)host + kTeamInviteHostIdHiOff);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("TEAM_INVITE self id read fail");
        g_shm->status = ST_ERR;
        return;
      }
      if (!self_lo && !self_hi) {
        SetErr("TEAM_INVITE self id null");
        g_shm->status = ST_ERR;
        return;
      }
      // Full zero + fill fields; +0x20/+0x24 MUST stay 0.
      uint8_t pkt[0x28];
      memset(pkt, 0, sizeof(pkt));
      *(uint32_t*)(pkt + 0x00) = kTeamInviteVt;
      *(uint32_t*)(pkt + 0x04) = kTeamInviteType;
      *(uint32_t*)(pkt + 0x10) = self_lo;
      *(uint32_t*)(pkt + 0x14) = self_hi;
      *(uint32_t*)(pkt + 0x18) = lo;
      *(uint32_t*)(pkt + 0x1C) = hi;
      uint32_t call_ret = 0;
      __try {
        typedef uint8_t(__thiscall* FnPktSend)(void* self, void* pkt, uint32_t flag);
        FnPktSend fn = (FnPktSend)(uintptr_t)sva;
        call_ret = (uint32_t)fn(host, pkt, 0);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("TEAM_INVITE exception");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->ret = (int32_t)call_ret;
      g_shm->status = ST_OK;
      char buf[112];
      sprintf_s(buf, "TEAM_INVITE zeroed ret=%u tgt=%u:%u self=%u:%u f20=0",
                (unsigned)call_ret, (unsigned)lo, (unsigned)hi,
                (unsigned)self_lo, (unsigned)self_hi);
      SetErr(buf);
      return;
    }

    if (cmd == CMD_QUICK_TEAM_FOLLOW) {
      FnSetTarget set_target =
          (FnSetTarget)GetExport("?SetTarget@plg@@YA_N_J@Z");
      if (!set_target) {
        SetErr("QUICK_TEAM_FOLLOW SetTarget export missing");
        g_shm->status = ST_ERR;
        return;
      }
      void* command = DumpUiDlg("Win_QuickTeamLeader");
      if (!command) command = DumpUiDlg("Win_QuickTeamMember");
      if (!command) {
        SetErr("QUICK_TEAM_FOLLOW command object missing");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t set_ret = 0;
      uint32_t follow_ret = 0;
      __try {
        set_ret = (uint32_t)set_target(lo, hi);
        FnQuickTeamFollow follow =
            (FnQuickTeamFollow)(uintptr_t)NoteToLive(kNoteQuickTeamFollow);
        if (!follow) {
          SetErr("QUICK_TEAM_FOLLOW va=0");
          g_shm->status = ST_ERR;
          return;
        }
        follow_ret = follow(command, 0u);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("QUICK_TEAM_FOLLOW exception");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->ret = (int32_t)follow_ret;
      g_shm->status = ST_OK;
      char buf[128];
      sprintf_s(buf, "QUICK_TEAM_FOLLOW set=%u follow=%u target=%08X:%08X",
                (unsigned)set_ret, (unsigned)follow_ret,
                (unsigned)hi, (unsigned)lo);
      SetErr(buf);
      return;
    }

    if (cmd == CMD_TEAM_FOLLOW) {
      const bool enabled = lo != 0;
      const uint32_t note_va = enabled ? kNoteTeamFollowEnable
                                       : kNoteTeamFollowDisable;
      const uint32_t va = NoteToLive(note_va);
      if (!va) {
        SetErr("TEAM_FOLLOW va=0");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t call_ret = 0;
      __try {
        // These are the exact handlers registered for Btn_OK / Btn_Cancel.
        // Their sole stack argument is not read by the fixed client build.
        FnTeamFollowUi fn = (FnTeamFollowUi)(uintptr_t)va;
        call_ret = fn(nullptr);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("TEAM_FOLLOW exception");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->ret = (int32_t)call_ret;
      g_shm->status = ST_OK;
      char buf[96];
      sprintf_s(buf, "TEAM_FOLLOW ui_%s ret=%u", enabled ? "enable" : "disable",
                (unsigned)call_ret);
      SetErr(buf);
      return;
    }


    if (cmd == CMD_NPC_TALK_SELECT) {
      // UI-thread NPC talk list select (legal service menu path).
      // id_lo=opt_id; mode=opt_type (0=keep, 2/4=select); tid=dlg* (0=GetGameUIDlg)
      // Event 0x80000011 only calls AA3330(dlg, "back") after list selected-id is set.
      uint32_t opt_id = lo;
      int32_t opt_type = g_shm->mode;
      uint32_t dlg = (uint32_t)g_shm->tid;
      if (!dlg) {
        FnGetGameUIDlg get_dlg =
            (FnGetGameUIDlg)GetExport("?GetGameUIDlg@plg@@YAPAVAUIDialog@@PBD@Z");
        if (get_dlg) {
          __try {
            void* p = get_dlg("Win_NPCContent");
            if (p) dlg = (uint32_t)(uintptr_t)p;
            if (!dlg) {
              p = get_dlg("Win_NPC");
              if (p) dlg = (uint32_t)(uintptr_t)p;
            }
          } __except (EXCEPTION_EXECUTE_HANDLER) {
            dlg = 0;
          }
        }
      }
      if (!dlg) {
        SetErr("NPC_TALK_SELECT no dlg");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t va = NoteToLive(kNoteNpcSelect);
      uint32_t list_g = NoteToLive(kNoteListManGlobal);
      uint32_t str_back = NoteToLive(kNoteStrBack);
      if (!va || !list_g || !str_back) {
        SetErr("NPC_TALK_SELECT va=0");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t listman = 0;
      __try {
        listman = *(uint32_t*)(uintptr_t)list_g;
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        listman = 0;
      }
      if (!listman) {
        SetErr("NPC_TALK_SELECT listman=null");
        g_shm->status = ST_ERR;
        return;
      }
      __try {
        *(uint32_t*)(uintptr_t)(listman + kListSelectedIdOff) = opt_id;
        if (opt_type == 2 || opt_type == 4 || opt_type == 1 || opt_type == 3) {
          *(uint32_t*)(uintptr_t)(dlg + kDlgOptTypeOff) = (uint32_t)opt_type;
        }
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("NPC_TALK_SELECT write SEH");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t call_ret = 0;
      __try {
        __asm {
          push str_back
          mov ecx, dlg
          mov eax, va
          call eax
          mov call_ret, eax
        }
        g_shm->ret = (int32_t)call_ret;
        g_shm->status = ST_OK;
        char buf[96];
        sprintf_s(buf, "NPC_TALK_SELECT ok dlg=0x%X id=%u type=%d ret=%u",
                  (unsigned)dlg, (unsigned)opt_id, (int)opt_type,
                  (unsigned)call_ret);
        SetErr(buf);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        g_shm->status = ST_ERR;
        SetErr("NPC_TALK_SELECT SEH");
      }
      return;
    }


    if (cmd == CMD_NPC_HOST_SELECT) {
      // Host service row activate (Win_NPCTemplate / *0x19561F0).
      // Legal game path: cdecl AA5590(index, unused) on UI thread.
      // Live L1 (tasks unfinished): Win_NPC.opt_type=1 -> AA58AE host branch
      // (AE7C00 field0 -> magic 0x40ABCDEF packet OR normal service id path).
      // id_lo = row index (0..count-1). mode unused.
      uint32_t index = lo;
      uint32_t va = NoteToLive(kNoteNpcHostSelect);
      uint32_t host_g = NoteToLive(kNoteHostGlobal);
      if (!va || !host_g) {
        SetErr("NPC_HOST_SELECT va=0");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t host = 0;
      uint32_t count = 0;
      __try {
        host = *(uint32_t*)(uintptr_t)host_g;
        if (host) count = *(uint32_t*)(uintptr_t)(host + 0x2ACu);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        host = 0;
        count = 0;
      }
      if (!host) {
        SetErr("NPC_HOST_SELECT host=null");
        g_shm->status = ST_ERR;
        return;
      }
      if (count == 0 || count > 64 || index >= count) {
        char buf[96];
        sprintf_s(buf, "NPC_HOST_SELECT bad index=%u count=%u",
                  (unsigned)index, (unsigned)count);
        SetErr(buf);
        g_shm->status = ST_ERR;
        return;
      }
      // Optional: mirror selected index at host+0x2B8 (AE6DA0).
      __try {
        *(uint32_t*)(uintptr_t)(host + 0x2B8u) = index;
      } __except (EXCEPTION_EXECUTE_HANDLER) {
      }
      uint32_t call_ret = 0;
      __try {
        __asm {
          push 0
          push index
          mov eax, va
          call eax
          add esp, 8
          mov call_ret, eax
        }
        g_shm->ret = (int32_t)call_ret;
        g_shm->status = ST_OK;
        char buf[96];
        sprintf_s(buf, "NPC_HOST_SELECT ok idx=%u count=%u host=0x%X ret=%u",
                  (unsigned)index, (unsigned)count, (unsigned)host,
                  (unsigned)call_ret);
        SetErr(buf);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        g_shm->status = ST_ERR;
        SetErr("NPC_HOST_SELECT SEH");
      }
      return;
    }

    if (cmd == CMD_CANCEL_SESSION) {
      // Exact Cancel Action packet path used by native cancel sites:
      //   GetNetRoot()+0x1C8 -> 0xCC7010(clearBuff=1, idPerform=0) -> opcode 0x21
      //
      // Lua binder CancelSession@0x69EF50 gates on host+0x41C, but many native
      // callers (0x76F4B8 / 0x772304 / …) call CC7010 without that gate. Live
      // yaolu logs show host+0x41C stays 0 while the entry bar / post-fail state
      // is still stuck — early-return "already idle" then never sends 0x21.
      // Always queue the packet; report host gate only as diagnostic context.
      uint32_t hva = NoteToLive(kNoteGetHostSide);
      uint32_t cva = NoteToLive(kNoteCancelSession);
      if (!cva) {
        SetErr("CANCEL_SESSION va=0");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t state = 0;
      if (hva) {
        void* host = nullptr;
        __try {
          FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)hva;
          host = get_host();
        } __except (EXCEPTION_EXECUTE_HANDLER) {
          host = nullptr;
        }
        if (host) {
          __try {
            state = *(uint32_t*)((uint8_t*)host + kHostSessionStateOff);
          } __except (EXCEPTION_EXECUTE_HANDLER) {
            state = 0;
          }
        }
      }
      void* net = ResolveNetRoot();
      if (!net) {
        SetErr("CANCEL_SESSION net=null");
        g_shm->status = ST_ERR;
        return;
      }
      uint8_t queued = 0;
      __try {
        FnCancelSession cancel = (FnCancelSession)(uintptr_t)cva;
        queued = cancel((uint8_t*)net + kCancelSessionThisOff, 1u, 0u);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("CANCEL_SESSION SEH");
        g_shm->status = ST_ERR;
        return;
      }
      // CC7010: al=0 only when send queue rejects; success path returns send result.
      g_shm->ret = queued ? 1 : 0;
      if (!queued) {
        SetErr(state ? "CANCEL_SESSION queue rejected"
                     : "CANCEL_SESSION queue rejected (host gate=0)");
        g_shm->status = ST_ERR;
        return;
      }
      g_shm->status = ST_OK;
      if (state) {
        SetErr("CANCEL_SESSION queued opcode=0x21");
      } else {
        SetErr("CANCEL_SESSION forced queue opcode=0x21 (host gate=0)");
      }
      return;
    }

        if (cmd == CMD_ONSKILL_STOPPED) {
      // Lab-only: call CECHostSkillHdl::OnSkillStopped (0x75F750).
      // Identity-match: snapshot cast+0x10/+0x14/+0x18 into zeroed event so
      // 0x582350 can clear identity (not just +0x4A0).
      // mode (g_shm->mode), lab-only variants — never accept ptr/id from Python:
      //   0 = thin-wrapper scalars (event,0,0,0,1) bContinueAtk=1  [default]
      //   1 = bContinueAtk=0 (also runs 0x755280 present clear)
      //   2 = bContinueAtk=0 then call 0x754BA0 (clear +CC skill obj / +60..+7C)
      //   3 = bContinueAtk=1 then call 0x754BA0
      // Static refill path after late stop: OnPerformSkill@0x761330 ->
      // SetCurActiveSkill@0x758DF0 reloads +10/+14/+18 and zeros +20.
      // Local flush only. Requires XAJH_ENABLE_UNSAFE_SKILL_WRITE=1. Restart client.
      uint32_t sva = NoteToLive(kNoteOnSkillStopped);
      if (!sva) {
        SetErr("ONSKILL_STOPPED va=0");
        g_shm->status = ST_ERR;
        return;
      }
      const int mode = (int)g_shm->mode;
      const uint32_t cont =
          (mode == 1 || mode == 2) ? 0u : 1u;
      const int do_clear_obj = (mode == 2 || mode == 3) ? 1 : 0;
      // Resolve cast-this via host+0x1A88
      void* cast_this = nullptr;
      uint32_t hva = NoteToLive(kNoteGetHostSide);
      if (hva) {
        __try {
          FnGetHostSide get_host = (FnGetHostSide)(uintptr_t)hva;
          void* host = get_host();
          if (host) {
            __try {
              cast_this = *(void**)((uint8_t*)host + kSkillCastThisOff);
            } __except (EXCEPTION_EXECUTE_HANDLER) {
              cast_this = nullptr;
            }
          }
        } __except (EXCEPTION_EXECUTE_HANDLER) { }
      }
      if (!cast_this) {
        SetErr("ONSKILL_STOPPED cast_this=null");
        g_shm->status = ST_ERR;
        return;
      }
      // Snapshot active identity from cast-this; reject all-zero.
      uint32_t id0 = 0, id1 = 0, id2 = 0;
      __try {
        id0 = *(uint32_t*)((uint8_t*)cast_this + 0x10);
        id1 = *(uint32_t*)((uint8_t*)cast_this + 0x14);
        id2 = *(uint32_t*)((uint8_t*)cast_this + 0x18);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("ONSKILL_STOPPED identity SEH");
        g_shm->status = ST_ERR;
        return;
      }
      if (id0 == 0u && id1 == 0u && id2 == 0u) {
        SetErr("ONSKILL_STOPPED identity=0");
        g_shm->status = ST_ERR;
        return;
      }
      // Zero-init event buffer; first three dwords = identity for 0x582350.
      uint8_t event_buf[32] = {0};
      *(uint32_t*)(event_buf + 0) = id0;
      *(uint32_t*)(event_buf + 4) = id1;
      *(uint32_t*)(event_buf + 8) = id2;
      FnOnSkillStopped fn = (FnOnSkillStopped)(uintptr_t)sva;
      __try {
        fn(cast_this, event_buf, 0u, 0u, 0u, cont);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        SetErr("ONSKILL_STOPPED SEH");
        g_shm->status = ST_ERR;
        return;
      }
      // Optional: CECHostSkillHdl clear active skill object (+CC) and perform
      // cluster +60..+7C. Note VA 0x754BA0 (same build). Suppresses leftover
      // perform state that OnPerformSkill may re-arm after late stop.
      int clear_obj_ran = 0;
      if (do_clear_obj) {
        const uint32_t cva = NoteToLive(0x00754BA0u);
        if (cva) {
          typedef void(__thiscall* FnClearSkillObj)(void* self);
          FnClearSkillObj cfn = (FnClearSkillObj)(uintptr_t)cva;
          __try {
            cfn(cast_this);
            clear_obj_ran = 1;
          } __except (EXCEPTION_EXECUTE_HANDLER) {
            SetErr("ONSKILL_STOPPED clear_obj SEH");
            g_shm->status = ST_ERR;
            return;
          }
        }
      }
      uint32_t after10 = 0xFFFFFFFFu;
      uint32_t after7c = 0xFFFFFFFFu;
      __try {
        after10 = *(uint32_t*)((uint8_t*)cast_this + 0x10);
        after7c = *(uint32_t*)((uint8_t*)cast_this + 0x7C);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        after10 = 0xFFFFFFFFu;
        after7c = 0xFFFFFFFFu;
      }
      const int cleared = (after10 == 0u) ? 1 : 0;
      g_shm->ret = cleared;
      g_shm->status = ST_OK;
      char buf[160];
      sprintf_s(buf,
                "ONSKILL_STOPPED ok id=0x%X,0x%X,0x%X cleared=%d "
                "cont=%u clear_obj=%d/7c=0x%X mode=%d "
                "(identity-match; restart client after lab)",
                (unsigned)id0, (unsigned)id1, (unsigned)id2, cleared,
                (unsigned)cont, clear_obj_ran, (unsigned)after7c, mode);
      SetErr(buf);
      return;
    }

    if (cmd == CMD_TASK_ACCEPT) {
      // jieTask: push 0,-1,0,0,0,taskId; thiscall @ kNoteTaskAccept
      // Nearby NPC is enforced by Python (accept_task_routed). Native call only
      // needs task_id + task manager this; distance is server-side.
      if (!lo) {
        SetErr("TASK_ACCEPT id=0");
        g_shm->status = ST_ERR;
        return;
      }
      void* self = ResolveTaskMgrThis();
      if (!self) {
        SetErr("TASK_ACCEPT this=null");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t va = NoteToLive(kNoteTaskAccept);
      if (!va) {
        SetErr("TASK_ACCEPT va=0");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t task_id = lo;
      uint32_t call_ret = 0;
      __try {
        __asm {
          push 0
          push -1
          push 0
          push 0
          push 0
          push task_id
          mov ecx, self
          mov eax, va
          call eax
          mov call_ret, eax
        }
        g_shm->ret = (int32_t)call_ret;
        g_shm->status = ST_OK;
        char buf[64];
        sprintf_s(buf, "TASK_ACCEPT ok ret=%u id=%u",
                  (unsigned)call_ret, (unsigned)task_id);
        SetErr(buf);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        g_shm->status = ST_ERR;
        SetErr("TASK_ACCEPT SEH");
      }
      return;
    }

    if (cmd == CMD_TASK_COMPLETE) {
      // mode=task_id; id_lo/hi=npc id64; tid=npc object ptr (optional for +0x958)
      uint32_t task_id = (uint32_t)g_shm->mode;
      if (!task_id) {
        SetErr("TASK_COMPLETE task_id=0");
        g_shm->status = ST_ERR;
        return;
      }
      if (!lo && !g_shm->tid) {
        SetErr("TASK_COMPLETE need npc");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t va = NoteToLive(kNoteTaskComplete);
      if (!va) {
        SetErr("TASK_COMPLETE va=0");
        g_shm->status = ST_ERR;
        return;
      }
      uint32_t npc_lo = lo;
      uint32_t npc_hi = hi;
      uint32_t npc_ptr = (uint32_t)g_shm->tid;
      uint32_t task_npc_id = 0;
      const char* task_npc_source = "none";
      if (npc_ptr) {
        __try {
          uint32_t mid = *(uint32_t*)(uintptr_t)(npc_ptr + 0x958);
          if (mid) {
            task_npc_id = *(uint32_t*)(uintptr_t)mid;
            if (task_npc_id) task_npc_source = "obj+958**";
          }
          if (!task_npc_id) {
            task_npc_id = *(uint32_t*)(uintptr_t)(npc_ptr + 0x48C);
            if (task_npc_id) task_npc_source = "obj+48c";
          }
        } __except (EXCEPTION_EXECUTE_HANDLER) {
          task_npc_id = 0;
        }
      }
      if (!task_npc_id && npc_ptr) {
        // Different NPC classes do not all carry the old +0x958 component.
        // Prefer the exported virtual getter before rejecting the object.
        FARPROC raw = GetExport("?GetObjectTargetID@plg@@YA_JPAX@Z");
        if (raw) {
          __try {
            FnObjectTargetId get_target = (FnObjectTargetId)raw;
            unsigned __int64 target_id = get_target((void*)(uintptr_t)npc_ptr);
            task_npc_id = (uint32_t)(target_id & 0xFFFFFFFFu);
            if (task_npc_id) task_npc_source = "GetObjectTargetID";
          } __except (EXCEPTION_EXECUTE_HANDLER) {
            task_npc_id = 0;
          }
        }
      }
      if (!task_npc_id) {
        SetErr("TASK_COMPLETE taskNpcId=0 sources=obj+958**/obj+48c/GetObjectTargetID");
        g_shm->status = ST_ERR;
        return;
      }
      // Exact-build call site 0xAB9FDF -> 0xAB9B40:
      // cdecl(taskId, taskNpcId, id_lo, id_hi, -1, -1, 0), caller pops 0x1C.
      uint32_t call_ret = 0;
      __try {
        __asm {
          push 0
          push -1
          push -1
          push npc_hi
          push npc_lo
          push task_npc_id
          push task_id
          // The game wrapper passes taskNpcId both as the second argument
          // and as ECX (the callee reads object state through this pointer).
          mov ecx, task_npc_id
          mov eax, va
          call eax
          add esp, 0x1C
          mov call_ret, eax
        }
        g_shm->ret = (int32_t)call_ret;
        g_shm->status = ST_OK;
        char buf[96];
        sprintf_s(buf, "TASK_COMPLETE ret=%u taskNpcId=0x%X source=%s",
                  (unsigned)call_ret, (unsigned)task_npc_id, task_npc_source);
        SetErr(buf);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        g_shm->status = ST_ERR;
        SetErr("TASK_COMPLETE SEH");
      }
      return;
    }

    if (cmd == CMD_REHOOK) {
      // No WndProc install — re-arm UI dispatch timer only.
      // Green path: never EnumWindows; only shm hwnd / already-armed g_hwnd.
      HWND preferred = PreferredHwndFromShm();
      HWND target = preferred;
      if (!target || !IsWindow(target)) {
        if (g_hwnd && IsWindow(g_hwnd)) {
          target = g_hwnd;
        } else if (!InterlockedCompareExchange(&g_green_safe, 0, 0)) {
          // Ideal standard path only.
          target = FindGameHwnd();
        }
      }
      if (!target || !IsWindow(target)) {
        SetErr(InterlockedCompareExchange(&g_green_safe, 0, 0)
                   ? "rehook: no hwnd (green)"
                   : "rehook: no hwnd");
        g_shm->status = ST_ERR;
        return;
      }
      g_hwnd = target;
      if (g_shm) g_shm->hwnd = (uint32_t)(uintptr_t)target;
      if (ArmDispatchTimer(target)) {
        MarkReady(InterlockedCompareExchange(&g_green_safe, 0, 0)
                      ? "rehook ok green"
                      : "rehook ok");
        g_shm->ret = 1;
        g_shm->status = ST_OK;
        return;
      }
      g_shm->status = ST_ERR;
      if (g_shm->err[0] == 0) SetErr("rehook SetTimer fail");
      return;
    }

    if (cmd == CMD_UNLOAD) {
      // Hot-unloading is unsafe: attach/hook callbacks may currently execute
      // inside this DLL. Updating the bridge requires restarting the game.
      g_shm->ret = 0;
      g_shm->status = ST_ERR;
      SetErr("UNLOAD_DISABLED_RESTART_GAME");
      return;
    }

    SetErr("unknown cmd");
    g_shm->status = ST_ERR;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    SetErr("SEH exception in bridge cmd");
    g_shm->status = ST_ERR;
  }
}

// DllMain must stay loader-lock free.
// Dual-path protocol:
//   STANDARD (ideal): Global/Local shm + Python hwnd first; late EnumWindows
//                     fallback so full capability clients still self-heal.
//   GREEN_SAFE:       same shm/timer/exports; NEVER EnumWindows in-process
//                     (green packs crash on early window enum). Rely on helper
//                     hwnd only. Business RVAs/exports stay standard.
// NEVER SetWindowLongPtr on either path.
static DWORD WINAPI AttachThreadProc(LPVOID) {
  __try {
    if (!OpenShared()) {
      return 1;
    }
    // Capture unhandled exceptions with crash_point (module+offset + stack).
    InstallGameCrashCapture();
    // Snapshot attach policy once (helper wrote mode before LoadLibrary).
    if (g_shm && g_shm->mode == kAttachModeGreenSafe) {
      InterlockedExchange(&g_green_safe, 1);
    } else {
      InterlockedExchange(&g_green_safe, 0);
    }
    const bool green = InterlockedCompareExchange(&g_green_safe, 0, 0) != 0;
    if (g_shm) {
      // Keep any hwnd Python already wrote into pre-created Global mapping.
      if (!g_shm->hwnd) {
        SetErr(green ? "shm ready green (await hwnd)"
                     : "shm ready (await hwnd)");
      } else {
        SetErr(green ? "shm ready green (hwnd pre-set)"
                     : "shm ready (hwnd pre-set)");
      }
    }
    InterlockedExchange(&g_attach_done, 1);

    // Green waits longer for helper hwnd; standard keeps prior budget.
    // Background one-click inject often has no FG window for a few seconds.
    const int max_i = green ? 1200 : 900;
    for (int i = 0; i < max_i; ++i) {
      if (InterlockedCompareExchange(&g_ready, 0, 0)) {
        return 0;
      }
      HWND preferred = PreferredHwndFromShm();
      // Ideal path only: enum periodically if Python never wrote hwnd.
      // Green path forbids this — foreign green clients crash in EnumWindows.
      if (!preferred && !green &&
          (i == 20 || i == 40 || i == 80 || i == 120 || i == 200 ||
           (i > 200 && (i % 40) == 0))) {
        preferred = FindGameHwnd();
        if (preferred && g_shm && !g_shm->hwnd) {
          g_shm->hwnd = (uint32_t)(uintptr_t)preferred;
        }
        if (!preferred && g_shm) {
          SetErr("shm ready (enum failed; await hwnd)");
        }
      }
      if (preferred) {
        g_hwnd = preferred;
        if (ArmDispatchTimer(preferred)) {
          MarkReady(green ? "bridge ready green" : "bridge ready");
          return 0;
        }
        // SetTimer failed — keep retrying; note already set.
      }
      if (i == 100 && g_shm && !InterlockedCompareExchange(&g_ready, 0, 0)) {
        SetErr(green ? "shm ready green (timer slow; await hwnd)"
                     : "shm ready (timer slow; await hwnd)");
      }
      Sleep(i < 100 ? 50 : 100);
    }
    if (!InterlockedCompareExchange(&g_ready, 0, 0) && g_shm) {
      SetErr(green ? "shm ready green (timer timeout; await hwnd)"
                   : "shm ready (timer timeout; await hwnd)");
      // Keep trying in background: REHOOK/PING cannot run without a timer,
      // so a watchdog that arms SetTimer without the UI timer is required.
      HANDLE wd =
          CreateThread(nullptr, 0, ReadyWatchdogProc, nullptr, 0, nullptr);
      if (wd) CloseHandle(wd);
    }
    return 0;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    if (g_shm) SetErr("attach SEH");
    return 2;
  }
}

// Continues arming the UI dispatch timer after AttachThreadProc budget ends.
// Solves one-click / background inject chicken-egg: commands need the timer,
// but REHOOK itself also needs the timer.
static DWORD WINAPI ReadyWatchdogProc(LPVOID) {
  __try {
    const bool green = InterlockedCompareExchange(&g_green_safe, 0, 0) != 0;
    for (int i = 0; i < 1800; ++i) {  // ~90s more
      if (InterlockedCompareExchange(&g_ready, 0, 0)) {
        return 0;
      }
      HWND preferred = PreferredHwndFromShm();
      if (!preferred && g_hwnd && IsWindow(g_hwnd)) {
        preferred = g_hwnd;
      }
      if (!preferred && !green && (i % 10) == 0) {
        preferred = FindGameHwnd();
        if (preferred && g_shm && !g_shm->hwnd) {
          g_shm->hwnd = (uint32_t)(uintptr_t)preferred;
        }
      }
      if (preferred && ArmDispatchTimer(preferred)) {
        g_hwnd = preferred;
        MarkReady(green ? "bridge ready green wd" : "bridge ready wd");
        return 0;
      }
      if (g_shm && (i % 20) == 0 &&
          !InterlockedCompareExchange(&g_ready, 0, 0)) {
        SetErr(green ? "shm ready green (watchdog await hwnd)"
                     : "shm ready (watchdog await hwnd)");
      }
      Sleep(50);
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    if (g_shm) SetErr("ready watchdog SEH");
  }
  return 0;
}

static void MarkReady(const char* note) {
  InterlockedExchange(&g_ready, 1);
  if (g_shm) {
    if (g_hwnd) g_shm->hwnd = (uint32_t)(uintptr_t)g_hwnd;
    if (note && note[0]) {
      char buf[96];
      sprintf_s(buf, "%s hwnd=0x%lX", note,
                (unsigned long)(uintptr_t)g_hwnd);
      SetErr(buf);
    }
  }
}

// UI-thread timer: execute pending shm commands without WndProc hook.
static VOID CALLBACK DispatchTimerProc(HWND hwnd, UINT, UINT_PTR id, DWORD) {
  if (id != kDispatchTimerId) return;
  if (InterlockedCompareExchange(&g_command_running, 1, 0) != 0) return;
  uint32_t dispatch_seq = 0;
  __try {
    if (!g_shm || g_shm->magic != BRIDGE_MAGIC) __leave;
    ProcessPendingSkillRestop();
    ProcessPendingYoufengChain();
    ProcessPendingYoufengNextGate();
    ProcessPendingYoufengMash();
    ProcessPendingYoufengPhase();
    ProcessPendingKuangfengTail();
    ProcessYoufengUltimateCooldownClear();
    ProcessPendingYoufengUltimateTail();
    // Bind hwnd if Python wrote one later.
    HWND preferred = PreferredHwndFromShm();
    if (preferred) g_hwnd = preferred;
    else if (hwnd) g_hwnd = hwnd;
    if (g_shm->status == ST_PENDING) {
      dispatch_seq = g_shm->seq;
      RunCommand();
      if (g_shm_v2 && g_shm &&
          (g_shm->status == ST_OK || g_shm->status == ST_ERR)) {
        g_shm->ack_seq = dispatch_seq;
      }
    } else if (InterlockedCompareExchange(&g_force_shift, 0, 0) ||
               InterlockedCompareExchange(&g_force_any, 0, 0)) {
      // Re-seed Shift AND KEY_HOLD force map (UpdateKeys clears table).
      // Without g_force_any here, multi-key always-hold drops between Python refreshes.
      MaintainForcedShift();
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    if (g_shm) {
      SetErr("dispatch timer SEH");
      g_shm->status = ST_ERR;
      if (g_shm_v2) g_shm->ack_seq = dispatch_seq;
    }
  }
  InterlockedExchange(&g_command_running, 0);
}

static bool ArmDispatchTimer(HWND hwnd) {
  if (!hwnd || !IsWindow(hwnd)) return false;
  {
    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);
    if (pid != GetCurrentProcessId()) {
      if (g_shm) SetErr("timer: hwnd other pid");
      return false;
    }
  }
  // Already armed: keep existing timer (period timer is fine to re-SetTimer).
  if (!SetTimer(hwnd, kDispatchTimerId, kDispatchPeriodMs, DispatchTimerProc)) {
    if (g_shm) {
      char buf[80];
      sprintf_s(buf, "SetTimer fail gle=%lu",
                (unsigned long)GetLastError());
      SetErr(buf);
    }
    return false;
  }
  g_hwnd = hwnd;
  InterlockedExchange(&g_timer_armed, 1);
  return true;
}

static void KillDispatchTimer() {
  if (g_hwnd && IsWindow(g_hwnd)) {
    KillTimer(g_hwnd, kDispatchTimerId);
  }
  InterlockedExchange(&g_timer_armed, 0);
  InterlockedExchange(&g_ready, 0);
}

static HWND FindGameHwnd() {
  // Do NOT dereference absolute note VAs here — other client builds crash.
  // Prefer class/title match; last resort: first real top-level of this pid.
  struct EnumCtx {
    DWORD pid;
    HWND preferred;
    HWND any_visible;
  } ctx{GetCurrentProcessId(), nullptr, nullptr};
  EnumWindows(
      [](HWND h, LPARAM lp) -> BOOL {
        auto* c = (EnumCtx*)lp;
        DWORD pid = 0;
        GetWindowThreadProcessId(h, &pid);
        if (pid != c->pid) return TRUE;
        if (!IsWindowVisible(h)) return TRUE;
        LONG style = GetWindowLongW(h, GWL_STYLE);
        if (!(style & WS_VISIBLE)) return TRUE;

        wchar_t cls[128] = {};
        GetClassNameW(h, cls, 128);
        if (wcsstr(cls, L"XAJH") || wcsstr(cls, L"xajh") ||
            wcsstr(cls, L"Xajh")) {
          c->preferred = h;
          return FALSE;
        }
        wchar_t title[256] = {};
        GetWindowTextW(h, title, 256);
        // 笑傲江湖
        if (wcsstr(title, L"\x7B11\x50B2\x6C5F\x6E56") ||
            wcsstr(title, L"XAJH") || wcsstr(title, L"xajh")) {
          c->preferred = h;
          return FALSE;
        }
        if (!c->any_visible) {
          if ((style & WS_CAPTION) || (style & WS_THICKFRAME) ||
              (style & WS_POPUP)) {
            c->any_visible = h;
          }
        }
        return TRUE;
      },
      (LPARAM)&ctx);
  return ctx.preferred ? ctx.preferred : ctx.any_visible;
}

static bool OpenShared() {
  // Prefer helper-precreated Global\ mapping (cross-integrity admin helper
  // <-> medium game). Fall back to Local\ create.
  const DWORD pid = GetCurrentProcessId();
  wchar_t name_g[64];
  wchar_t name_l[64];
  swprintf_s(name_g, L"Global\\XajhBridge_%u", pid);
  swprintf_s(name_l, L"Local\\XajhBridge_%u", pid);

  bool created_fresh = false;
  g_map = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, name_g);
  if (!g_map) {
    g_map = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, name_l);
  }
  if (!g_map) {
    g_map = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0,
                               sizeof(BridgeShared), name_g);
    if (g_map && GetLastError() != ERROR_ALREADY_EXISTS) created_fresh = true;
  }
  if (!g_map) {
    g_map = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0,
                               sizeof(BridgeShared), name_l);
    if (g_map && GetLastError() != ERROR_ALREADY_EXISTS) created_fresh = true;
  }
  if (!g_map) return false;

  g_shm = (BridgeShared*)MapViewOfFile(g_map, FILE_MAP_ALL_ACCESS, 0, 0,
                                       sizeof(BridgeShared));
  g_shm_v2 = g_shm != nullptr;
  if (!g_shm) {
    g_shm = (BridgeShared*)MapViewOfFile(g_map, FILE_MAP_ALL_ACCESS, 0, 0, 184);
    g_shm_v2 = false;
  }
  if (!g_shm) return false;

  // Only wipe if we created a brand-new mapping; keep Python pre-written hwnd.
  if (created_fresh || g_shm->magic != BRIDGE_MAGIC) {
    uint32_t keep_hwnd = (g_shm->magic == BRIDGE_MAGIC) ? g_shm->hwnd : 0;
    ZeroMemory((void*)g_shm, g_shm_v2 ? sizeof(BridgeShared) : 184);
    g_shm->magic = BRIDGE_MAGIC;
    g_shm->status = ST_IDLE;
    if (keep_hwnd) g_shm->hwnd = keep_hwnd;
  } else {
    // Existing helper mapping: ensure magic/status sane, do not clear hwnd.
    g_shm->magic = BRIDGE_MAGIC;
    if (g_shm->status != ST_PENDING) g_shm->status = ST_IDLE;
  }

  g_game = GetModuleHandleW(L"xajh.exe");
  if (!g_game) g_game = GetModuleHandleW(nullptr);
  if (g_game) g_shm->module_base = (uint32_t)(uintptr_t)g_game;
  if (g_shm_v2) {
    g_shm->protocol_version = BRIDGE_PROTOCOL_VERSION;
    g_shm->struct_size = (uint32_t)sizeof(BridgeShared);
    g_shm->capabilities = kBridgeCapabilities;
  }
  return true;
}

static HWND PreferredHwndFromShm() {
  if (!g_shm || !g_shm->hwnd) return nullptr;
  HWND h = (HWND)(uintptr_t)g_shm->hwnd;
  if (!IsWindow(h)) return nullptr;
  DWORD pid = 0;
  GetWindowThreadProcessId(h, &pid);
  if (pid != GetCurrentProcessId()) return nullptr;
  return h;
}

static void CloseShared() {
  KillDispatchTimer();
  if (g_shm) {
    UnmapViewOfFile((LPCVOID)g_shm);
    g_shm = nullptr;
    g_shm_v2 = false;
  }
  if (g_map) {
    CloseHandle(g_map);
    g_map = nullptr;
  }
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
  if (reason == DLL_PROCESS_ATTACH) {
    g_self = hModule;
    DisableThreadLibraryCalls(hModule);
    // CRITICAL: do not call SetWindowLongPtr / EnumWindows / fopen under
    // loader lock. CreateRemoteThread inject path: schedule attach worker.
    HANDLE thr =
        CreateThread(nullptr, 0, AttachThreadProc, nullptr, 0, nullptr);
    if (thr) {
      CloseHandle(thr);
    } else {
      // Do not perform mapping/timer work under loader lock.
      return FALSE;
    }
  } else if (reason == DLL_PROCESS_DETACH) {
    // Avoid logging / heavy work during process teardown.
    KillDispatchTimer();
    CloseShared();
  }
  return TRUE;
}
