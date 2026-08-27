// xajh_packet_cap.dll - inline hook RC4 Update.ret to capture decrypted S2C packets.
// Hook point: 0x00DAFB6B (xajh.exe preferred base 0x400000)
//   At this point the RC4 loop finished; [esp+0x18] is believed to be the Octets
//   buffer (begin=[oct+4], end=[oct+8]) OR the plaintext pointer.
// Captured bytes are pushed to a shared-memory ring that Python reads.
// @author by ak
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>

#pragma comment(lib, "user32.lib")
#pragma comment(lib, "kernel32.lib")

#define IMAGE_BASE 0x00400000u
#define HOOK_VA    0x00DAFB6Bu
// Octets struct: we guess begin at +4, end at +8
#define OCT_BEGIN_OFF 4
#define OCT_END_OFF   8

static uint8_t* g_target = nullptr;
static uint8_t g_orig[16] = {0};
static int g_orig_n = 0;

// ---- shared ring ----
#define RING_SIZE  (1 << 20)   // 1 MB
#define RING_MAGIC 0x58435043   // 'PCX'
typedef struct {
  uint32_t magic;
  volatile uint32_t head;
  volatile uint32_t tail;
  uint8_t  data[RING_SIZE];
} Ring;

static Ring* g_ring = nullptr;
static HANDLE g_map = nullptr;
static wchar_t g_shm_name[64] = {0};

// cdecl hook function: called with original stack intact? We patch over
// `mov eax,[esp+0x18]` (5 bytes: 8B 44 24 18). Our detour must preserve
// registers/stack then jump back. We use __declspec(naked) thunk.
// Simplest: patch the 5-byte mov into a jmp to our handler, handler reads
// [esp+0x18], does work, then re-executes the original mov and jumps back.
typedef int(__cdecl* FnOriginal)(void);
static uintptr_t g_back = 0;

static void ring_push(const uint8_t* p, uint32_t len) {
  if (!g_ring || !p || len == 0 || len > RING_SIZE / 2) return;
  uint32_t head = g_ring->head;
  uint32_t tail = g_ring->tail;
  if ((uint32_t)(head - tail) > RING_SIZE - len - 8) return; // full
  // frame: len(u32) + bytes
  uint8_t* dst = g_ring->data + (head & (RING_SIZE - 1));
  uint32_t rem = RING_SIZE - (head & (RING_SIZE - 1));
  // simple memcpy handling wrap
  auto cpy = [&](const void* src, uint32_t n) {
    const uint8_t* s = (const uint8_t*)src;
    uint32_t w = (uint32_t)(head & (RING_SIZE - 1));
    uint32_t first = RING_SIZE - w;
    if (n <= first) {
      memcpy(g_ring->data + w, s, n);
    } else {
      memcpy(g_ring->data + w, s, first);
      memcpy(g_ring->data, s + first, n - first);
    }
  };
  cpy(&len, 4);
  cpy(p, len);
  head += 4 + len;
  g_ring->head = head;
}

// This handler is entered with the 5-byte mov at HOOK_VA replaced by a JMP.
// Stack at entry: [esp+0x18] is the value the original `mov eax,[esp+0x18]`
// would load. We save all regs, read [esp+0x18], push, restore, jump to
// original+5.
extern "C" void cap_handle_capture(void* oct);

static void __declspec(naked) HookThunk(void) {
  __asm {
    pushad
    pushfd
    // At entry esp = E (ret addr). pushad -> E-32, pushfd -> E-36.
    // We need value at [E+0x18]. E = esp+36. So [esp+36+0x18] = [esp+0x54].
    mov eax, [esp+0x54]
    push eax
    call cap_handle_capture
    add esp, 4
    popfd
    popad
    // original instruction bytes we replaced: mov eax,[esp+0x18] (4B) + pop edi (1B)
    mov eax, [esp+0x18]
    pop edi
    jmp g_back
  }
}

// real handler (cdecl): oct* in first arg
void cap_handle_capture(void* oct) {
  if (!oct) return;
  __try {
    uint8_t* b = (uint8_t*)oct;
    // try begin/end layout: b+4 begin, b+8 end
    uint32_t begin = *(uint32_t*)(b + 4);
    uint32_t end = *(uint32_t*)(b + 8);
    if (end > begin && end - begin < (1 << 16)) {
      ring_push((uint8_t*)(uintptr_t)begin, end - begin);
    } else {
      // maybe oct itself points to plaintext with len at some offset
      // dump first 64 bytes as-is
      ring_push(b, 64);
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
}

static void install_hook(void) {
  // hook va -> live address (base is preferred 0x400000, assume rebased to 0x400000)
  g_target = (uint8_t*)(uintptr_t)HOOK_VA;
  // save original 5 bytes: 8B 44 24 18
  memcpy(g_orig, g_target, 5);
  g_orig_n = 5;
  // back = target+5
  g_back = (uintptr_t)(g_target + 5);
  // patch: E9 rel32
  DWORD old = 0;
  VirtualProtect(g_target, 5, PAGE_EXECUTE_READWRITE, &old);
  g_target[0] = 0xE9;
  *(int32_t*)(g_target + 1) = (int32_t)((uintptr_t)&HookThunk - (uintptr_t)(g_target + 5));
  for (int i = 5; i < 5; ++i) g_target[i] = 0x90;
  VirtualProtect(g_target, 5, old, &old);
  FlushInstructionCache(GetCurrentProcess(), g_target, 5);
}

static void remove_hook(void) {
  if (!g_target || !g_orig_n) return;
  DWORD old = 0;
  VirtualProtect(g_target, g_orig_n, PAGE_EXECUTE_READWRITE, &old);
  memcpy(g_target, g_orig, g_orig_n);
  VirtualProtect(g_target, g_orig_n, old, &old);
  FlushInstructionCache(GetCurrentProcess(), g_target, g_orig_n);
}

static DWORD WINAPI WorkerProc(LPVOID) {
  // open existing shared map created by helper (Global\XajhCap_<pid>);
  // fall back to creating our own so injection order does not matter.
  DWORD pid = GetCurrentProcessId();
  swprintf_s(g_shm_name, L"Global\\XajhCap_%u", pid);
  for (int i = 0; i < 200; ++i) {
    g_map = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, (LPCWSTR)g_shm_name);
    if (g_map) break;
    g_map = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0,
                               sizeof(Ring), (LPCWSTR)g_shm_name);
    if (g_map) break;
    Sleep(100);
  }
  if (!g_map) return 1;
  g_ring = (Ring*)MapViewOfFile(g_map, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(Ring));
  if (!g_ring) return 1;
  g_ring->magic = RING_MAGIC;
  g_ring->head = 0;
  g_ring->tail = 0;
  install_hook();
  // keep thread alive
  while (true) Sleep(1000000);
  return 0;
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
  if (reason == DLL_PROCESS_ATTACH) {
    DisableThreadLibraryCalls(hModule);
    HANDLE t = CreateThread(nullptr, 0, WorkerProc, nullptr, 0, nullptr);
    if (t) CloseHandle(t);
  } else if (reason == DLL_PROCESS_DETACH) {
    remove_hook();
  }
  return TRUE;
}
