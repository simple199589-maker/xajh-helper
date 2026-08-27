// Passive AddChatMessage tap for the fixed xajh.exe x86 build.
// Copies each incoming message to a 50-entry shared-memory ring, then calls
// the original function unchanged.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <intrin.h>

#include "chat_tap_protocol.h"

static HMODULE g_self = nullptr;
static HMODULE g_game = nullptr;
static HANDLE g_map = nullptr;
static ChatTapShared* g_ring = nullptr;
static uint8_t* g_target = nullptr;
static uint8_t* g_trampoline = nullptr;
static uint8_t g_original[8] = {};
// Serializes only the bounded ring write. The original game call is always
// outside this lock; a stalled tap can therefore drop an event, never stall
// or re-enter the game's chat path.
static volatile LONG g_capture_lock = 0;

static const uint32_t kPreferredImageBase = 0x00400000u;
static const uint32_t kKnownTimestamp = 0x56736608u;
static const uint32_t kKnownImageSize = 0x038A5000u;
static const uint32_t kAddChatMessageVa = 0x00885300u;
static const int kStolenBytes = 8;

typedef void(__thiscall* FnAddChatMessage)(
    void* self, const wchar_t* text, uint32_t channel, uint32_t arg3,
    uint32_t arg4, uint32_t arg5, uint32_t arg6, uint32_t arg7,
    uint32_t arg8, uint32_t arg9, uint32_t arg10);

static void SetError(const char* message) {
  if (!g_ring) return;
  g_ring->status = CHAT_TAP_ERROR;
  strncpy_s(g_ring->error, message ? message : "unknown", _TRUNCATE);
}

static bool OpenRing() {
  wchar_t name[64] = {};
  swprintf_s(name, L"Local\\XajhChatTap_%lu",
             (unsigned long)GetCurrentProcessId());
  g_map = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0,
                             sizeof(ChatTapShared), name);
  if (!g_map) return false;
  bool fresh = GetLastError() != ERROR_ALREADY_EXISTS;
  g_ring = (ChatTapShared*)MapViewOfFile(
      g_map, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(ChatTapShared));
  if (!g_ring) return false;
  if (fresh || g_ring->magic != CHAT_TAP_MAGIC ||
      g_ring->struct_size != sizeof(ChatTapShared)) {
    ZeroMemory(g_ring, sizeof(ChatTapShared));
  }
  g_ring->magic = CHAT_TAP_MAGIC;
  g_ring->version = CHAT_TAP_VERSION;
  g_ring->struct_size = sizeof(ChatTapShared);
  g_ring->capacity = CHAT_TAP_CAPACITY;
  g_ring->status = CHAT_TAP_INIT;
  g_ring->target_va = 0;
  g_ring->error[0] = 0;
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

static void CaptureMessage(const wchar_t* text, uint32_t channel,
                           uint32_t caller_va) {
  if (!g_ring || g_ring->status != CHAT_TAP_ACTIVE || !text) return;
  wchar_t local[CHAT_TAP_TEXT_CHARS] = {};
  uint32_t length = 0;
  __try {
    while (length + 1 < CHAT_TAP_TEXT_CHARS && text[length]) {
      local[length] = text[length];
      ++length;
    }
    local[length] = 0;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return;
  }
  if (!length) return;

  bool locked = false;
  for (int spin = 0; spin < 128; ++spin) {
    if (InterlockedCompareExchange(&g_capture_lock, 1, 0) == 0) {
      locked = true;
      break;
    }
    YieldProcessor();
  }
  if (!locked) {
    return;
  }

  __try {
    uint32_t seq =
        (uint32_t)InterlockedIncrement((volatile LONG*)&g_ring->write_seq);
    ChatTapEvent* event = &g_ring->events[(seq - 1u) % CHAT_TAP_CAPACITY];
    InterlockedExchange((volatile LONG*)&event->seq, 0);
    event->tick_ms = GetTickCount();
    event->thread_id = GetCurrentThreadId();
    event->caller_va = caller_va;
    event->channel = channel;
    event->flags = 0;
    event->text_len = length;
    CopyMemory(event->text, local, (length + 1u) * sizeof(wchar_t));
    MemoryBarrier();
    InterlockedExchange((volatile LONG*)&event->seq, (LONG)seq);
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    // Keep the ring unchanged on an unexpected shared-memory fault.
  }
  InterlockedExchange(&g_capture_lock, 0);
}

static void __fastcall HookAddChatMessage(
    void* self, void* /*edx*/, const wchar_t* text, uint32_t channel,
    uint32_t arg3, uint32_t arg4, uint32_t arg5, uint32_t arg6,
    uint32_t arg7, uint32_t arg8, uint32_t arg9, uint32_t arg10) {
  CaptureMessage(text, channel, (uint32_t)(uintptr_t)_ReturnAddress());
  FnAddChatMessage original = (FnAddChatMessage)g_trampoline;
  original(self, text, channel, arg3, arg4, arg5, arg6, arg7, arg8, arg9,
           arg10);
}

static bool InstallHook() {
  uint8_t* base = (uint8_t*)g_game;
  g_target = base + (kAddChatMessageVa - kPreferredImageBase);
  static const uint8_t expected[kStolenBytes] = {
      0x6A, 0xFF, 0x64, 0xA1, 0x00, 0x00, 0x00, 0x00};
  if (memcmp(g_target, expected, sizeof(expected)) != 0) {
    SetError("AddChatMessage prologue mismatch");
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
      (int32_t)((uint8_t*)&HookAddChatMessage - (g_target + 5));
  for (int i = 5; i < kStolenBytes; ++i) g_target[i] = 0x90;
  FlushInstructionCache(GetCurrentProcess(), g_target, kStolenBytes);
  DWORD ignored = 0;
  VirtualProtect(g_target, kStolenBytes, old, &ignored);
  g_ring->target_va = (uint32_t)(uintptr_t)g_target;
  g_ring->status = CHAT_TAP_ACTIVE;
  return true;
}

static DWORD WINAPI AttachThread(LPVOID) {
  if (!OpenRing()) return 1;
  g_game = GetModuleHandleW(L"xajh.exe");
  if (!g_game) g_game = GetModuleHandleW(nullptr);
  if (!ValidateGameBuild()) {
    SetError("unsupported xajh build");
    return 2;
  }
  return InstallHook() ? 0 : 3;
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
