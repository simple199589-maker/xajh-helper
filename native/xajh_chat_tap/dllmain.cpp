// Universal chat tap v3 — DllMain NEVER returns FALSE.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <intrin.h>

// Inline struct definitions (no external header dependency).
#define CT_MAGIC 0x50415443u
#define CT_VERSION 3u
#define CT_CAPACITY 50u
#define CT_TEXT_CHARS 256u
#define CT_TEAM_CAPACITY 512u
#define CT_PRIVATE_CAPACITY 512u

#pragma pack(push, 1)
struct CTEvent {
  volatile uint32_t seq;
  uint32_t tick_ms;
  uint32_t thread_id;
  uint32_t caller_va;
  uint32_t channel;
  uint32_t flags;
  uint32_t text_len;
  wchar_t text[CT_TEXT_CHARS];
};
struct CTShared {
  volatile uint32_t magic;
  volatile uint32_t version;
  volatile uint32_t struct_size;
  volatile uint32_t capacity;
  volatile uint32_t write_seq;
  volatile uint32_t status; // 0=init 1=active 2=error
  volatile uint32_t target_va;
  char error[128];
  CTEvent events[CT_CAPACITY];
  volatile uint32_t team_write_seq;
  CTEvent team_events[CT_TEAM_CAPACITY];
  volatile uint32_t private_write_seq;
  CTEvent private_events[CT_PRIVATE_CAPACITY];
};
#pragma pack(pop)

static HMODULE g_self = nullptr;
static HMODULE g_game = nullptr;
static HANDLE g_map = nullptr;
static CTShared* g_ring = nullptr;
static uint8_t* g_target = nullptr;
static uint8_t* g_trampoline = nullptr;
static uint8_t g_original[8] = {};
static volatile LONG g_lock = 0;

static const uint32_t kBase = 0x00400000u;
static const uint32_t kKnownVa = 0x00885300u;
static const int kStolen = 8;

typedef void(__thiscall* FnAdd)(void*, const wchar_t*, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t, uint32_t);

static void SetErr(const char* msg) {
  if (!g_ring) return;
  g_ring->status = 2;
  strncpy_s(g_ring->error, msg ? msg : "?", _TRUNCATE);
}

static bool OpenRing() {
  wchar_t name[64] = {};
  swprintf_s(name, L"Local\\XajhChatTap_%lu", (unsigned long)GetCurrentProcessId());
  g_map = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(CTShared), name);
  if (!g_map) return false;
  bool fresh = GetLastError() != ERROR_ALREADY_EXISTS;
  g_ring = (CTShared*)MapViewOfFile(g_map, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(CTShared));
  if (!g_ring) return false;
  if (fresh || g_ring->magic != CT_MAGIC || g_ring->struct_size != sizeof(CTShared))
    ZeroMemory(g_ring, sizeof(CTShared));
  g_ring->magic = CT_MAGIC;
  g_ring->version = CT_VERSION;
  g_ring->struct_size = sizeof(CTShared);
  g_ring->capacity = CT_CAPACITY;
  g_ring->status = 0;
  g_ring->target_va = 0;
  g_ring->error[0] = 0;
  return true;
}

static void PushEv(CTEvent* ring, volatile uint32_t* seq, uint32_t cap,
                   const wchar_t* text, uint32_t len, uint32_t ch, uint32_t caller) {
  uint32_t s = (uint32_t)InterlockedIncrement((volatile LONG*)seq);
  CTEvent* e = &ring[(s - 1u) % cap];
  InterlockedExchange((volatile LONG*)&e->seq, 0);
  e->tick_ms = GetTickCount();
  e->thread_id = GetCurrentThreadId();
  e->caller_va = caller;
  e->channel = ch;
  e->flags = 0;
  e->text_len = len;
  CopyMemory(e->text, text, (len + 1u) * sizeof(wchar_t));
  MemoryBarrier();
  InterlockedExchange((volatile LONG*)&e->seq, (LONG)s);
}

static void Capture(const wchar_t* text, uint32_t ch, uint32_t caller) {
  if (!g_ring || g_ring->status != 1 || !text) return;
  wchar_t local[CT_TEXT_CHARS] = {};
  uint32_t len = 0;
  __try {
    while (len + 1 < CT_TEXT_CHARS && text[len]) { local[len] = text[len]; ++len; }
    local[len] = 0;
  } __except (EXCEPTION_EXECUTE_HANDLER) { return; }
  if (!len) return;
  for (int i = 0; i < 128; ++i) {
    if (InterlockedCompareExchange(&g_lock, 1, 0) == 0) {
      __try {
        PushEv(g_ring->events, &g_ring->write_seq, CT_CAPACITY, local, len, ch, caller);
        if (ch == 3) PushEv(g_ring->team_events, &g_ring->team_write_seq, CT_TEAM_CAPACITY, local, len, ch, caller);
        if (ch == 9) PushEv(g_ring->private_events, &g_ring->private_write_seq, CT_PRIVATE_CAPACITY, local, len, ch, caller);
      } __except (EXCEPTION_EXECUTE_HANDLER) {}
      InterlockedExchange(&g_lock, 0);
      return;
    }
    YieldProcessor();
  }
}

static void __fastcall HookFn(void* self, void*, const wchar_t* text, uint32_t ch,
    uint32_t a3, uint32_t a4, uint32_t a5, uint32_t a6, uint32_t a7, uint32_t a8, uint32_t a9, uint32_t a10) {
  Capture(text, ch, (uint32_t)(uintptr_t)_ReturnAddress());
  ((FnAdd)g_trampoline)(self, text, ch, a3, a4, a5, a6, a7, a8, a9, a10);
}

static const uint8_t kPro[8] = {0x6A, 0xFF, 0x64, 0xA1, 0x00, 0x00, 0x00, 0x00};

static bool ProOk(uint8_t* p) {
  __try { return memcmp(p, kPro, 8) == 0; }
  __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static bool TryHook(uint8_t* addr) {
  if (!ProOk(addr)) return false;
  CopyMemory(g_original, addr, kStolen);
  g_trampoline = (uint8_t*)VirtualAlloc(nullptr, kStolen + 5, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
  if (!g_trampoline) { SetErr("tram alloc"); return false; }
  CopyMemory(g_trampoline, g_original, kStolen);
  g_trampoline[kStolen] = 0xE9;
  *(int32_t*)(g_trampoline + kStolen + 1) = (int32_t)((addr + kStolen) - (g_trampoline + kStolen + 5));
  DWORD old = 0;
  if (!VirtualProtect(addr, kStolen, PAGE_EXECUTE_READWRITE, &old)) { SetErr("protect"); return false; }
  addr[0] = 0xE9;
  *(int32_t*)(addr + 1) = (int32_t)((uint8_t*)&HookFn - (addr + 5));
  for (int i = 5; i < kStolen; ++i) addr[i] = 0x90;
  FlushInstructionCache(GetCurrentProcess(), addr, kStolen);
  DWORD ig = 0; VirtualProtect(addr, kStolen, old, &ig);
  g_target = addr;
  g_ring->target_va = (uint32_t)(uintptr_t)addr;
  g_ring->status = 1;
  return true;
}

static bool Install() {
  if (!g_game || !g_ring) return false;
  char buf[32] = {};
  if (GetEnvironmentVariableA("XAJH_CHAT_HOOK_VA", buf, sizeof(buf)) && buf[0]) {
    uint32_t va = (uint32_t)strtoul(buf, nullptr, 0);
    if (va) return TryHook((uint8_t*)g_game + (va - kBase));
  }
  if (TryHook((uint8_t*)g_game + (kKnownVa - kBase))) return true;
  __try {
    auto* dos = (IMAGE_DOS_HEADER*)g_game;
    auto* nt = (IMAGE_NT_HEADERS*)((uint8_t*)g_game + dos->e_lfanew);
    auto* sec = IMAGE_FIRST_SECTION(nt);
    for (int i = 0; i < nt->FileHeader.NumberOfSections; ++i, ++sec) {
      if (!(sec->Characteristics & IMAGE_SCN_MEM_EXECUTE)) continue;
      uint8_t* st = (uint8_t*)g_game + sec->VirtualAddress;
      for (uint32_t o = 0; o + 8 <= sec->Misc.VirtualSize; ++o) {
        if (ProOk(st + o)) {
          uint32_t va = kBase + sec->VirtualAddress + o;
          if (va == kKnownVa) continue;
          if (TryHook(st + o)) return true;
          if (g_trampoline) { VirtualFree(g_trampoline, 0, MEM_RELEASE); g_trampoline = nullptr; }
        }
      }
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {}
  SetErr("no match");
  return false;
}

static DWORD WINAPI Thread(LPVOID) {
  g_game = GetModuleHandleW(L"xajh.exe");
  if (!g_game) g_game = GetModuleHandleW(nullptr);
  if (!g_game) { SetErr("no module"); return 2; }
  return Install() ? 0 : 3;
}

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
  if (reason == DLL_PROCESS_ATTACH) {
    g_self = module;
    DisableThreadLibraryCalls(module);
    if (!g_ring && !OpenRing()) {
      // Can't create shared memory — still return TRUE so LoadLibrary succeeds.
      // The diagnostic will just show "not found".
    }
    HANDLE t = CreateThread(nullptr, 0, Thread, nullptr, 0, nullptr);
    if (t) CloseHandle(t);
    // NEVER return FALSE.
  }
  return TRUE;
}
