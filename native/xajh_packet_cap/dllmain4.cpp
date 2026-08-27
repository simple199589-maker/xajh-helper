// xajh_packet_cap3.dll - capture RC4 key (SetKey) + Octets (Update entry).
// Two minimal entry hooks, no data copy in-game (record pointers/key bytes).
// Key: at SetKey entry, [esp+8] = key Octets*, key data at [k+4], len=[k+8]-[k+4].
// We copy the key bytes (small) into the ring so Python can decrypt offline.
// @author by ak
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>

#define IMAGE_BASE 0x00400000u
#define HOOK_SETKEY 0x00DAFA60u
#define HOOK_UPDATE 0x00DAFAF0u

static uint8_t* g_sk = nullptr;
static uint8_t* g_up = nullptr;
static uint8_t g_sk_orig[8] = {0}, g_up_orig[8] = {0};
static uintptr_t g_sk_back = 0, g_up_back = 0;

#define RING_SIZE (1 << 20)
#define RING_MAGIC 0x58435034
typedef struct {
  uint32_t magic;
  volatile uint32_t head;
  volatile uint32_t tail;
  uint8_t  data[RING_SIZE];
} Ring;
static Ring* g_ring = nullptr;
static HANDLE g_map = nullptr;
static wchar_t g_shm_name[64] = {0};

// frame types
enum { FT_OCT = 1, FT_KEY = 2, FT_PAIR = 3 };
static void ring_frame(uint8_t kind, const void* src, uint32_t n) {
  if (!g_ring || n == 0 || n > RING_SIZE / 2) return;
  uint32_t head = g_ring->head;
  uint32_t tail = g_ring->tail;
  if ((uint32_t)(head - tail) > RING_SIZE - n - 8) return;
  // header: kind(u8) + len(u24) = 4 bytes
  uint32_t hdr = ((uint32_t)kind) | (n << 8);
  uint32_t w = head & (RING_SIZE - 1);
  uint32_t first = RING_SIZE - w;
  const uint8_t* s = (const uint8_t*)src;
  // write hdr
  if (first >= 4) { memcpy(g_ring->data + w, &hdr, 4); }
  else { memcpy(g_ring->data + w, &hdr, first); memcpy(g_ring->data, (char*)&hdr + first, 4 - first); }
  head += 4;
  w = head & (RING_SIZE - 1);
  first = RING_SIZE - w;
  // write payload
  if (first >= n) { memcpy(g_ring->data + w, s, n); }
  else { memcpy(g_ring->data + w, s, first); memcpy(g_ring->data, s + first, n - first); }
  head += n;
  g_ring->head = head;
}

extern "C" void cap_record_key(void* k);
extern "C" void cap_record_oct(void* oct);
extern "C" void cap_record_pair(void* self, void* oct);

// SetKey entry: push ecx; mov eax,[esp+8]; (5 bytes: 51 8B 44 24 08)
static void __declspec(naked) HookSetKey(void) {
  __asm {
    pushad
    pushfd
    // entry esp=E, [E+8]=key Octets*. pushad(32)+pushfd(4). [E+8]=[esp+36+8]=[esp+0x2C]
    mov eax, [esp+0x2C]
    push eax
    call cap_record_key
    add esp, 4
    popfd
    popad
    push ecx
    mov eax, [esp+8]
    jmp g_sk_back
  }
}

// Update entry: push ecx; mov eax,[esp+8]; (5 bytes)
static void __declspec(naked) HookUpdate(void) {
  __asm {
    pushad
    pushfd
    // record pair: push self(ecx), push oct([esp+0x2C])
    mov eax, [esp+0x2C]
    push eax
    push ecx
    call cap_record_pair
    add esp, 8
    popfd
    popad
    push ecx
    mov eax, [esp+8]
    jmp g_up_back
  }
}

void cap_record_key(void* k) {
  if (!k) return;
  __try {
    uint32_t kb = *(uint32_t*)((uint8_t*)k + 4);
    uint32_t ke = *(uint32_t*)((uint8_t*)k + 8);
    uint32_t len = ke - kb;
    if (len > 0 && len <= 256 && 0x10000 < kb < 0x7FFE0000) {
      ring_frame(FT_KEY, (const void*)(uintptr_t)kb, len);
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {}
}

void cap_record_pair(void* self, void* oct) {
  if (!oct || !self) return;
  uintptr_t s = (uintptr_t)self, o = (uintptr_t)oct;
  if (s < 0x10000 || s > 0x7FFE0000 || o < 0x10000 || o > 0x7FFE0000) return;
  uint8_t buf[8];
  memcpy(buf, &s, 4);
  memcpy(buf + 4, &o, 4);
  ring_frame(FT_PAIR, buf, 8);
}

void cap_record_oct(void* oct) {
  if (!oct) return;
  uintptr_t v = (uintptr_t)oct;
  if (v < 0x10000 || v > 0x7FFE0000) return;
  ring_frame(FT_OCT, &v, 4);
}

static void patch_hook(uint8_t* target, void* thunk, uint8_t* orig, uintptr_t* back) {
  memcpy(orig, target, 5);
  *back = (uintptr_t)(target + 5);
  DWORD old = 0;
  VirtualProtect(target, 5, PAGE_EXECUTE_READWRITE, &old);
  target[0] = 0xE9;
  *(int32_t*)(target + 1) = (int32_t)((uintptr_t)thunk - (uintptr_t)(target + 5));
  VirtualProtect(target, 5, old, &old);
  FlushInstructionCache(GetCurrentProcess(), target, 5);
}

static void install_hooks(void) {
  g_sk = (uint8_t*)(uintptr_t)HOOK_SETKEY;
  g_up = (uint8_t*)(uintptr_t)HOOK_UPDATE;
  patch_hook(g_sk, (void*)&HookSetKey, g_sk_orig, &g_sk_back);
  patch_hook(g_up, (void*)&HookUpdate, g_up_orig, &g_up_back);
}

static void remove_hooks(void) {
  auto restore = [](uint8_t* t, uint8_t* o) {
    if (!t) return;
    DWORD old = 0;
    VirtualProtect(t, 5, PAGE_EXECUTE_READWRITE, &old);
    memcpy(t, o, 5);
    VirtualProtect(t, 5, old, &old);
    FlushInstructionCache(GetCurrentProcess(), t, 5);
  };
  restore(g_sk, g_sk_orig);
  restore(g_up, g_up_orig);
}

static DWORD WINAPI WorkerProc(LPVOID) {
  DWORD pid = GetCurrentProcessId();
  swprintf_s(g_shm_name, L"Global\\XajhCap4_%u", pid);
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
  install_hooks();
  while (true) Sleep(1000000);
  return 0;
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
  if (reason == DLL_PROCESS_ATTACH) {
    DisableThreadLibraryCalls(hModule);
    HANDLE t = CreateThread(nullptr, 0, WorkerProc, nullptr, 0, nullptr);
    if (t) CloseHandle(t);
  } else if (reason == DLL_PROCESS_DETACH) {
    remove_hooks();
  }
  return TRUE;
}
