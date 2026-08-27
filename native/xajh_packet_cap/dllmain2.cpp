// xajh_packet_cap2.dll - lightweight hook at RC4 Update ENTRY (0xDAFAF0).
// Records the Octets* argument ([esp+4]) into shared memory. Python polls the
// ring, then reads the decrypted plaintext from the game at [oct+4]/[oct+8]
// (begin/end) AFTER the update completes.
//
// Hook: at 0x00DAFAF0, first bytes are "51 8B 44 24 08" (push ecx; mov eax,[esp+8]).
// We patch the first 5 bytes "51 8B 44 24 08" -> JMP hook. Hook must replicate
// those 5 bytes then jump to +5.
//
// This hook is minimal (no data copy) -> very low crash risk.
// @author by ak
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>

#define IMAGE_BASE 0x00400000u
#define HOOK_VA    0x00DAFAF0u

static uint8_t* g_target = nullptr;
static uint8_t g_orig[8] = {0};
static int g_orig_n = 0;
static uintptr_t g_back = 0;

// shared ring: just record Octets* values (u32) with a small counter
#define RING_SIZE  (1 << 20)
#define RING_MAGIC 0x58435032
typedef struct {
  uint32_t magic;
  volatile uint32_t head;
  volatile uint32_t tail;
  uint8_t  data[RING_SIZE];
} Ring;
static Ring* g_ring = nullptr;
static HANDLE g_map = nullptr;
static wchar_t g_shm_name[64] = {0};

// push one Octets* (u32) into ring
static void ring_push_u32(uint32_t v) {
  if (!g_ring) return;
  uint32_t head = g_ring->head;
  uint32_t tail = g_ring->tail;
  if ((uint32_t)(head - tail) > RING_SIZE - 16) return; // full
  uint32_t w = head & (RING_SIZE - 1);
  if (w + 4 <= RING_SIZE) {
    memcpy(g_ring->data + w, &v, 4);
  } else {
    memcpy(g_ring->data + w, &v, RING_SIZE - w);
    memcpy(g_ring->data, (char*)&v + (RING_SIZE - w), 4 - (RING_SIZE - w));
  }
  g_ring->head = head + 4;
}

extern "C" void cap_record(void* oct);

static void __declspec(naked) HookThunk(void) {
  __asm {
    pushad
    pushfd
    // entry esp = E (ret addr). We need [E+4] = first arg = Octets*.
    // pushad -> E-32, pushfd -> E-36. [E+4] = [esp+36+4] = [esp+0x28].
    mov eax, [esp+0x28]
    push eax
    call cap_record
    add esp, 4
    popfd
    popad
    // replicate original first 5 bytes: 51 8B 44 24 08
    push ecx
    mov eax, [esp+8]
    jmp g_back
  }
}

void cap_record(void* oct) {
  if (!oct) return;
  uintptr_t v = (uintptr_t)oct;
  if (v < 0x10000 || v > 0x7FFE0000) return;
  ring_push_u32((uint32_t)v);
}

static void install_hook(void) {
  g_target = (uint8_t*)(uintptr_t)HOOK_VA;
  // save original 5 bytes
  memcpy(g_orig, g_target, 5);
  g_orig_n = 5;
  g_back = (uintptr_t)(g_target + 5);
  DWORD old = 0;
  VirtualProtect(g_target, 5, PAGE_EXECUTE_READWRITE, &old);
  g_target[0] = 0xE9;
  *(int32_t*)(g_target + 1) = (int32_t)((uintptr_t)&HookThunk - (uintptr_t)(g_target + 5));
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
  DWORD pid = GetCurrentProcessId();
  swprintf_s(g_shm_name, L"Global\\XajhCap2_%u", pid);
  for (int i = 0; i < 200; ++i) {
    g_map = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, (LPCWSTR)g_shm_name);
    if (g_map) break;
    g_map = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, sizeof(Ring), (LPCWSTR)g_shm_name);
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
