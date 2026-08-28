// Team-chat send-manager discovery tap for the fixed xajh.exe x86 build.
// Hooks the chat-encrypt wrapper 0x00D089B0 (thiscall: ecx = send manager,
// stack arg [esp+4] = Octets object). On the first hit it copies ecx (the
// send manager) to shared memory so the Python side can send chat via the
// wrapper without needing a stable global-chain parse.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <intrin.h>

#include "team_tap_protocol.h"

static HMODULE g_self = nullptr;
static HMODULE g_game = nullptr;
static HANDLE g_map = nullptr;
static TeamTapShared* g_shm = nullptr;
static uint8_t* g_target = nullptr;
static uint8_t* g_trampoline = nullptr;
static uint8_t g_original[9] = {};

static const uint32_t kPreferredImageBase = 0x00400000u;
static const uint32_t kKnownTimestamp = 0x56736608u;
static const uint32_t kKnownImageSize = 0x038A5000u;
// Exact send-manager object signature. All constants are note VAs and are
// converted with the live image base before validation.
static const uint32_t kSendMgrVtableRva = 0x00EC7C00u;   // 0x012C7C00 - 0x00400000
static const uint32_t kCryptoCfgARva   = 0x01961A88u;    // 0x01D61A88 - 0x00400000
static const uint32_t kCryptoCfgBRva   = 0x01961B9Cu;    // 0x01D61B9C - 0x00400000
static const uint32_t kSendMgrBufferCap = 0x00008000u;
static const uint32_t kSendMgrObjectSize = 0x000000E0u;
// Chat-encrypt wrapper: thiscall(this=sendmgr, octets*) -> bool, ret 4.
static const uint32_t kWrapVa = 0x00D089B0u;
static const int kStolenBytes = 9;
// Expected prologue (9 bytes, full instructions):
//   push ebx; push esi; mov esi,[esp+0xC]; push edi; mov edi,ecx
static const uint8_t kExpected[kStolenBytes] = {
    0x53, 0x56, 0x8B, 0x74, 0x24, 0x0C, 0x57, 0x8B, 0xF9};

typedef char(__thiscall* FnWrap)(void* self, void* octets);

static void SetError(const char* message) {
  if (!g_shm) return;
  g_shm->status = TEAM_TAP_ERROR;
  strncpy_s(g_shm->error, message ? message : "unknown", _TRUNCATE);
}

static bool OpenShared() {
  wchar_t name[64] = {};
  swprintf_s(name, L"Local\\XajhTeamTap_%lu",
             (unsigned long)GetCurrentProcessId());
  g_map = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0,
                             sizeof(TeamTapShared), name);
  if (!g_map) return false;
  bool fresh = GetLastError() != ERROR_ALREADY_EXISTS;
  g_shm = (TeamTapShared*)MapViewOfFile(g_map, FILE_MAP_ALL_ACCESS, 0, 0,
                                        sizeof(TeamTapShared));
  if (!g_shm) return false;
  if (fresh || g_shm->magic != TEAM_TAP_MAGIC ||
      g_shm->struct_size != sizeof(TeamTapShared)) {
    ZeroMemory(g_shm, sizeof(TeamTapShared));
  }
  g_shm->magic = TEAM_TAP_MAGIC;
  g_shm->version = TEAM_TAP_VERSION;
  g_shm->struct_size = sizeof(TeamTapShared);
  g_shm->status = TEAM_TAP_INIT;
  g_shm->target_va = 0;
  g_shm->send_mgr = 0;
  g_shm->send_mgr_seen = 0;
  g_shm->hit_write_seq = 0;
  g_shm->send_req.seq = 0;
  g_shm->send_req.status = TEAM_SEND_IDLE;
  g_shm->send_req.result = 0;
  g_shm->send_req.len = 0;
  g_shm->dump_len = 0;
  g_shm->send_ident0 = 0x2B;
  g_shm->send_ident1 = 0x40;
  g_shm->send_ident2 = 0x01;
  g_shm->send_ident_seen = 0;
  g_shm->exact_scan_status = EXACT_SCAN_IDLE;
  g_shm->exact_scan_count = 0;
  g_shm->exact_send_mgr = 0;
  g_shm->exact_send_mgr_seen = 0;
  g_shm->error[0] = 0;
  return true;
}

static bool ValidateGameBuild() {
  if (!g_game) return false;
  __try {
    auto* dos = (IMAGE_DOS_HEADER*)g_game;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return false;
    auto* nt = (IMAGE_NT_HEADERS*)((uint8_t*)g_game + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return false;
    if (nt->FileHeader.Machine != IMAGE_FILE_MACHINE_I386) return false;
    if (nt->FileHeader.TimeDateStamp != kKnownTimestamp) return false;
    if (nt->OptionalHeader.SizeOfImage != kKnownImageSize) return false;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  return true;
}

static void CaptureSendMgr(void* self, uint32_t caller) {
  if (!g_shm || g_shm->status != TEAM_TAP_ACTIVE || !self) return;
  uint32_t mgr = (uint32_t)(uintptr_t)self;
  if (!mgr) return;
  // Record every hit into the ring (self + caller) so the Python side can
  // tell which caller actually does chat sends vs other systems sharing the
  // wrapper 0x00D089B0.
  __try {
    uint32_t seq =
        (uint32_t)InterlockedIncrement((volatile LONG*)&g_shm->hit_write_seq);
    TeamTapHit* hit = &g_shm->hits[(seq - 1u) % TEAM_TAP_HITS];
    InterlockedExchange((volatile LONG*)&hit->seq, 0);
    hit->self = mgr;
    hit->caller = caller;
    MemoryBarrier();
    InterlockedExchange((volatile LONG*)&hit->seq, (LONG)seq);
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    // keep the ring unchanged on a shared-memory fault
  }
  if (g_shm->send_mgr_seen) return;  // already captured this session
  // Volatile stores; no locks needed (single writer per hit, idempotent).
  g_shm->send_mgr = mgr;
  InterlockedExchange((volatile LONG*)&g_shm->send_mgr_seen, 1);
}

static void DumpOctets(const void* octets, uint32_t caller) {
  if (!g_shm || !octets) return;
  // Only capture manual-chat packets (caller inside 0x00D0A380) so the dump
  // slot is not overwritten by position/heartbeat traffic.
  if (caller != 0x00D0A4C1u) return;
  __try {
    uint32_t begin = *(uint32_t*)((uint8_t*)octets + 4);
    uint32_t end = *(uint32_t*)((uint8_t*)octets + 8);
    uint32_t len = end > begin ? end - begin : 0;
    if (len > 256) len = 256;
    uint8_t* dst = g_shm->dump;
    for (uint32_t i = 0; i < len; ++i) {
      dst[i] = *(uint8_t*)(uintptr_t)(begin + i);
    }
    g_shm->dump_len = len;
    // Chat identity fingerprint (packet offsets 7..9, third byte == 0x01):
    //   十丶三=2b 40 01, 初一=2b a0 01, 唐家军1=30 30 01.
    // Capture the stable 3 bytes so the Python side can rebuild the header.
    if (len >= 10 && dst[9] == 0x01) {
      g_shm->send_ident0 = dst[7];
      g_shm->send_ident1 = dst[8];
      g_shm->send_ident2 = dst[9];
      g_shm->send_ident_seen = 1;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    g_shm->dump_len = 0;
  }
}

static char __fastcall HookWrap(void* self, void* /*edx*/, void* octets) {
  uint32_t caller = (uint32_t)(uintptr_t)_ReturnAddress();
  CaptureSendMgr(self, caller);
  DumpOctets(octets, caller);
  FnWrap original = (FnWrap)g_trampoline;
  return original(self, octets);
}

// Octets vtable (note VA) used by game chat wrappers.
static const uint32_t kOctetsVtableVa = 0x0122E228u;

static uint32_t NoteToLive(uint32_t note_va) {
  uint8_t* base = (uint8_t*)g_game;
  if (!base) return 0;
  return (uint32_t)(uintptr_t)(base + (note_va - kPreferredImageBase));
}

// Perform one in-process wrapper send with a constructed Octets object. Runs
// on the game-side sender thread. NOTE: manual chat sends RC4-encrypt via the
// game's own stream before this wrapper; direct calls here are best-effort
// only (the correct path is the game's enqueue+main-loop flow).
static int SendViaWrapper(uint32_t send_mgr, const uint8_t* data, uint32_t len) {
  if (!send_mgr || !data || !len) return -1;
  uint32_t wrapper = (uint32_t)(uintptr_t)g_trampoline;
  uint32_t vtable = NoteToLive(kOctetsVtableVa);
  if (!wrapper || !vtable) return -2;
  __try {
    uint8_t* obj = (uint8_t*)VirtualAlloc(
        nullptr, 0x1000, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!obj) return -3;
    uint8_t* data_addr = obj + 0x200;
    CopyMemory(data_addr, data, len);
    *(uint32_t*)(obj + 0x00) = vtable;
    *(uint32_t*)(obj + 0x04) = (uint32_t)(uintptr_t)data_addr;
    *(uint32_t*)(obj + 0x08) = (uint32_t)(uintptr_t)(data_addr + len);
    *(uint32_t*)(obj + 0x0C) = len;
    FnWrap wrap = (FnWrap)(uintptr_t)wrapper;
    int ret = (int)wrap((void*)(uintptr_t)send_mgr, (void*)obj);
    VirtualFree(obj, 0, MEM_RELEASE);
    return ret;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return -4;
  }
}

static bool ProtectReadable(DWORD protect) {
  protect &= ~(PAGE_GUARD | PAGE_NOCACHE);
  switch (protect) {
    case PAGE_NOACCESS:
      return false;
    case PAGE_READONLY:
    case PAGE_READWRITE:
    case PAGE_WRITECOPY:
    case PAGE_EXECUTE_READ:
    case PAGE_EXECUTE_READWRITE:
    case PAGE_EXECUTE_WRITECOPY:
      return true;
    default:
      return false;
  }
}

static uint32_t Rd32(const uint8_t* p, uint32_t offset) {
  uint32_t v = 0;
  CopyMemory(&v, p + offset, sizeof(v));
  return v;
}

// Find the outer chat/send-manager object by its unique vtable and invariant
// fields. This is not a raw heap-pointer guess: every candidate must satisfy
// the constructor's self references and the two live crypto/config pointers.
static int FindSendMgrExact(uint32_t* found_mgr) {
  if (!g_game || !found_mgr) return -1;
  *found_mgr = 0;

  uint8_t* base = (uint8_t*)g_game;
  uint32_t expected_vtable =
      (uint32_t)(uintptr_t)(base + kSendMgrVtableRva);
  uint32_t expected_cfg_a =
      (uint32_t)(uintptr_t)(base + kCryptoCfgARva);
  uint32_t expected_cfg_b =
      (uint32_t)(uintptr_t)(base + kCryptoCfgBRva);

  HANDLE self = GetCurrentProcess();
  SYSTEM_INFO si = {};
  GetSystemInfo(&si);
  uint8_t* addr = (uint8_t*)si.lpMinimumApplicationAddress;
  uint8_t* max_addr = (uint8_t*)si.lpMaximumApplicationAddress;
  int valid_count = 0;

  const SIZE_T kChunk = 0x00100000u;  // 1 MiB
  uint8_t* buf = (uint8_t*)VirtualAlloc(nullptr, kChunk + 0x1000,
                                        MEM_COMMIT | MEM_RESERVE,
                                        PAGE_READWRITE);
  if (!buf) return -2;

  while (addr < max_addr) {
    MEMORY_BASIC_INFORMATION mbi = {};
    if (!VirtualQuery(addr, &mbi, sizeof(mbi))) break;
    uint8_t* region = (uint8_t*)mbi.BaseAddress;
    SIZE_T size = (SIZE_T)mbi.RegionSize;
    if (!region || !size) break;
    if (mbi.State != MEM_COMMIT || !ProtectReadable(mbi.Protect)) {
      addr = region + size;
      continue;
    }

    SIZE_T stride = kChunk - kSendMgrObjectSize + 4;
    if (stride < 0x100u) stride = 0x100u;
    for (SIZE_T off = 0; off < size; off += stride) {
      SIZE_T want = kChunk;
      if (want > size - off) want = size - off;
      SIZE_T got = 0;
      if (!ReadProcessMemory(self, region + off, buf, want, &got) || got < kSendMgrObjectSize)
        continue;

      for (SIZE_T pos = 0; pos + kSendMgrObjectSize <= got; pos += 4) {
        if (*(const uint32_t*)(buf + pos) != expected_vtable) continue;
        uint8_t* candidate = region + off + pos;
        // Object allocations are 8-aligned; this removes code/immediate hits.
        if (((uintptr_t)candidate & 7u) != 0) continue;

        const uint8_t* o = buf + pos;
        if (Rd32(o, 0x7C) != (uint32_t)(uintptr_t)candidate) continue;
        if (Rd32(o, 0x9C) != (uint32_t)(uintptr_t)candidate) continue;
        if (Rd32(o, 0xCC) != expected_cfg_a) continue;
        if (Rd32(o, 0xD0) != expected_cfg_b) continue;
        if (Rd32(o, 0x24) != kSendMgrBufferCap) continue;

        ++valid_count;
        *found_mgr = (uint32_t)(uintptr_t)candidate;
      }
    }

    if (addr > region + size) break;  // overflow guard
    addr = region + size;
  }

  VirtualFree(buf, 0, MEM_RELEASE);
  return valid_count;
}

static DWORD WINAPI ExactScanThread(LPVOID) {
  for (;;) {
    if (!g_shm || g_shm->status != TEAM_TAP_ACTIVE) {
      Sleep(20);
      continue;
    }

    // Exact scan fills the main slot whenever hook has not captured it yet.
    // If Python invalidates a failed send (send_mgr_seen=0), this also makes
    // the next scan revalidate the exact object instead of reusing a stale one.
    if (g_shm->send_mgr_seen) {
      Sleep(500);
      continue;
    }

    InterlockedExchange((volatile LONG*)&g_shm->exact_send_mgr_seen, 0);
    InterlockedExchange((volatile LONG*)&g_shm->exact_send_mgr, 0);
    InterlockedExchange((volatile LONG*)&g_shm->exact_scan_status,
                        EXACT_SCAN_RUNNING);

    uint32_t mgr = 0;
    int count = FindSendMgrExact(&mgr);
    InterlockedExchange((volatile LONG*)&g_shm->exact_scan_count,
                        (LONG)count);
    if (count == 1 && mgr) {
      InterlockedExchange((volatile LONG*)&g_shm->exact_send_mgr,
                          (LONG)mgr);
      InterlockedExchange((volatile LONG*)&g_shm->exact_send_mgr_seen, 1);
      InterlockedExchange((volatile LONG*)&g_shm->exact_scan_status,
                          EXACT_SCAN_FOUND);
      // The hook remains active as a secondary validator/fallback.
      if (!g_shm->send_mgr_seen) {
        g_shm->send_mgr = mgr;
        InterlockedExchange((volatile LONG*)&g_shm->send_mgr_seen, 1);
      }
    } else {
      InterlockedExchange((volatile LONG*)&g_shm->exact_scan_status,
                          count > 1 ? EXACT_SCAN_MULTIPLE
                                    : EXACT_SCAN_NOT_FOUND);
    }

    // Object construction can happen later on some login paths, so retry.
    Sleep(500);
  }
  return 0;
}

// In-game sender thread: polls the mailbox and performs wrapper sends. Runs
// entirely inside the game process so the thiscall frame is native.
static DWORD WINAPI SenderThread(LPVOID) {
  for (;;) {
    if (!g_shm || g_shm->status != TEAM_TAP_ACTIVE) {
      Sleep(10);
      continue;
    }
    TeamSendReq* req = &g_shm->send_req;
    volatile uint32_t status =
        (uint32_t)InterlockedCompareExchange(
            (volatile LONG*)&req->status, TEAM_SEND_DONE, TEAM_SEND_PENDING);
    if (status != TEAM_SEND_PENDING) {
      Sleep(5);
      continue;
    }
    uint32_t len = req->len;
    if (!g_shm->send_mgr_seen || !len || len > TEAM_SEND_MAX_LEN) {
      req->result = -5;
      InterlockedExchange((volatile LONG*)&req->status, TEAM_SEND_DONE);
      continue;
    }
    int ret = SendViaWrapper(g_shm->send_mgr, req->data, len);
    req->result = (uint32_t)ret;
    InterlockedExchange((volatile LONG*)&req->status, TEAM_SEND_DONE);
  }
  return 0;
}

static bool InstallHook() {
  uint8_t* base = (uint8_t*)g_game;
  g_target = base + (kWrapVa - kPreferredImageBase);
  if (memcmp(g_target, kExpected, sizeof(kExpected)) != 0) {
    SetError("wrap prologue mismatch");
    return false;
  }

  CopyMemory(g_original, g_target, kStolenBytes);
  g_trampoline = (uint8_t*)VirtualAlloc(
      nullptr, kStolenBytes + 5, MEM_COMMIT | MEM_RESERVE,
      PAGE_EXECUTE_READWRITE);
  if (!g_trampoline) {
    SetError("trampoline alloc failed");
    return false;
  }
  CopyMemory(g_trampoline, g_original, kStolenBytes);
  g_trampoline[kStolenBytes] = 0xE9;
  *(int32_t*)(g_trampoline + kStolenBytes + 1) =
      (int32_t)((g_target + kStolenBytes) -
                (g_trampoline + kStolenBytes + 5));

  DWORD old = 0;
  if (!VirtualProtect(g_target, kStolenBytes, PAGE_EXECUTE_READWRITE, &old)) {
    SetError("target protect failed");
    return false;
  }
  g_target[0] = 0xE9;
  *(int32_t*)(g_target + 1) =
      (int32_t)((uint8_t*)&HookWrap - (g_target + 5));
  for (int i = 5; i < kStolenBytes; ++i) g_target[i] = 0x90;
  FlushInstructionCache(GetCurrentProcess(), g_target, kStolenBytes);
  DWORD ignored = 0;
  VirtualProtect(g_target, kStolenBytes, old, &ignored);
  g_shm->target_va = (uint32_t)(uintptr_t)g_target;

  g_shm->status = TEAM_TAP_ACTIVE;
  return true;
}

static DWORD WINAPI AttachThread(LPVOID) {
  if (!OpenShared()) return 1;
  g_game = GetModuleHandleW(L"xajh.exe");
  if (!g_game) g_game = GetModuleHandleW(nullptr);
  if (!ValidateGameBuild()) {
    SetError("unsupported xajh build");
    return 2;
  }
  int rc = InstallHook() ? 0 : 3;
  if (rc == 0) {
    HANDLE sender = CreateThread(nullptr, 0, SenderThread, nullptr, 0, nullptr);
    if (sender) CloseHandle(sender);
    HANDLE scanner = CreateThread(nullptr, 0, ExactScanThread, nullptr, 0, nullptr);
    if (scanner) CloseHandle(scanner);
  }
  return rc;
}

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
  if (reason == DLL_PROCESS_ATTACH) {
    g_self = module;
    DisableThreadLibraryCalls(module);
    HANDLE thread = CreateThread(nullptr, 0, AttachThread, nullptr, 0, nullptr);
    if (!thread) return FALSE;
    CloseHandle(thread);
  }
  return TRUE;
}
