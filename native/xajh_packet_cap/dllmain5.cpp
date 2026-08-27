#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <string.h>
#include <stddef.h>
#include <wchar.h>

#define HOOK_SETKEY 0x00DAFA60u
#define HOOK_UPDATE 0x00DAFAF0u
#define RING_SIZE (1 << 20)
#define RING_MAGIC 0x58435035
#define FT_KEY 2
#define FT_SNAPSHOT 5
#define SNAP_MAX 1024
#pragma pack(push, 1)
typedef struct {
  uint32_t self;
  uint32_t octets;
  uint32_t begin;
  uint16_t length;
  uint8_t rc4_i;
  uint8_t rc4_j;
  uint8_t phase;
  uint8_t reserved;
  uint8_t sbox[256];
  uint8_t data[SNAP_MAX];
} Snapshot;
#pragma pack(pop)
typedef struct { uint32_t magic; volatile uint32_t head; volatile uint32_t tail; uint8_t data[RING_SIZE]; } Ring;
static Ring* g_ring = nullptr; static HANDLE g_map = nullptr; static wchar_t g_name[64] = {0};
static uint8_t* g_sk = nullptr; static uint8_t* g_up = nullptr; static uint8_t g_sk_orig[8] = {0}, g_up_orig[8] = {0};
static uintptr_t g_sk_back = 0, g_up_back = 0, g_up_tramp = 0;

static void ring_frame(uint8_t kind, const void* src, uint32_t n) {
  if (!g_ring || !src || n == 0 || n > RING_SIZE / 2) return;
  uint32_t head = g_ring->head, tail = g_ring->tail;
  if ((uint32_t)(head - tail) > RING_SIZE - n - 8) return;
  uint32_t hdr = ((uint32_t)kind) | (n << 8), w = head & (RING_SIZE - 1), first = RING_SIZE - w;
  if (first >= 4) memcpy(g_ring->data + w, &hdr, 4); else { memcpy(g_ring->data + w, &hdr, first); memcpy(g_ring->data, ((uint8_t*)&hdr) + first, 4-first); }
  head += 4; w = head & (RING_SIZE - 1); first = RING_SIZE - w;
  if (first >= n) memcpy(g_ring->data + w, src, n); else { memcpy(g_ring->data + w, src, first); memcpy(g_ring->data, ((const uint8_t*)src)+first, n-first); }
  g_ring->head = head + n;
}

static bool valid_ptr(uint32_t p) { return p > 0x10000u && p < 0x7FFE0000u; }
static void capture_snapshot(void* self_ptr, void* oct_ptr, uint8_t phase) {
  __try {
    uint32_t self = (uint32_t)(uintptr_t)self_ptr, oct = (uint32_t)(uintptr_t)oct_ptr;
    if (!valid_ptr(self) || !valid_ptr(oct)) return;
    uint32_t begin = *(uint32_t*)(oct + 4), end = *(uint32_t*)(oct + 8);
    if (!valid_ptr(begin) || end < begin) return;
    uint32_t n = end - begin; if (n == 0 || n > SNAP_MAX) return;
    Snapshot snap = {};
    snap.self = self; snap.octets = oct; snap.begin = begin; snap.length = (uint16_t)n; snap.phase = phase;
    snap.rc4_i = *(uint8_t*)(self + 0x108); snap.rc4_j = *(uint8_t*)(self + 0x109);
    memcpy(snap.data, (void*)(uintptr_t)begin, n);
    ring_frame(FT_SNAPSHOT, &snap, (uint32_t)(sizeof(Snapshot) - SNAP_MAX + n));
  } __except (EXCEPTION_EXECUTE_HANDLER) {}
}

extern "C" void cap_key(void* k) {
  __try { if (!k) return; uint32_t b=*(uint32_t*)((uint8_t*)k+4), e=*(uint32_t*)((uint8_t*)k+8); if(valid_ptr(b)&&e>=b&&e-b<=256) ring_frame(FT_KEY,(void*)(uintptr_t)b,e-b); } __except(EXCEPTION_EXECUTE_HANDLER) {}
}
extern "C" void cap_pre(void* self, void* oct) { capture_snapshot(self, oct, 0); }
extern "C" void cap_post(void* self, void* oct) { capture_snapshot(self, oct, 1); }

static void __declspec(naked) HookSetKey(void) {
  __asm {
    pushad
    pushfd
    mov eax, [esp+0x2C]
    push eax
    call cap_key
    add esp, 4
    popfd
    popad
    push ecx
    mov eax, [esp+8]
    jmp g_sk_back
  }
}
static void __declspec(naked) HookUpdate(void) {
  __asm {
    pushad
    pushfd
    mov eax, [esp+0x2C]
    push eax
    push ecx
    call cap_pre
    add esp, 8
    popfd
    popad
    push ecx
    mov eax, [esp+8]
    jmp g_up_back
  }
}static void patch(uint8_t* target, void* thunk, uint8_t* orig, uintptr_t* back) {
  memcpy(orig,target,5); *back=(uintptr_t)(target+5); DWORD old=0; VirtualProtect(target,5,PAGE_EXECUTE_READWRITE,&old); target[0]=0xE9; *(int32_t*)(target+1)=(int32_t)((uintptr_t)thunk-(uintptr_t)(target+5)); VirtualProtect(target,5,old,&old); FlushInstructionCache(GetCurrentProcess(),target,5);
}
static void make_tramp() {
  g_up_tramp=(uintptr_t)VirtualAlloc(nullptr,32,MEM_COMMIT|MEM_RESERVE,PAGE_EXECUTE_READWRITE);
  if(!g_up_tramp)return; memcpy((void*)g_up_tramp,g_up_orig,5); uint8_t* p=(uint8_t*)g_up_tramp+5; p[0]=0xE9; *(int32_t*)(p+1)=(int32_t)((uintptr_t)g_up+5-((uintptr_t)p+5)); FlushInstructionCache(GetCurrentProcess(),(void*)g_up_tramp,10);
}
static void install() { g_sk=(uint8_t*)HOOK_SETKEY; g_up=(uint8_t*)HOOK_UPDATE; patch(g_sk,(void*)&HookSetKey,g_sk_orig,&g_sk_back); memcpy(g_up_orig,g_up,5); make_tramp(); patch(g_up,(void*)&HookUpdate,g_up_orig,&g_up_back); }
static void remove_hooks() { if(g_sk){DWORD o;VirtualProtect(g_sk,5,PAGE_EXECUTE_READWRITE,&o);memcpy(g_sk,g_sk_orig,5);VirtualProtect(g_sk,5,o,&o);} if(g_up){DWORD o;VirtualProtect(g_up,5,PAGE_EXECUTE_READWRITE,&o);memcpy(g_up,g_up_orig,5);VirtualProtect(g_up,5,o,&o);} if(g_up_tramp) VirtualFree((void*)g_up_tramp,0,MEM_RELEASE); }
static DWORD WINAPI Worker(LPVOID) { DWORD pid=GetCurrentProcessId(); swprintf_s(g_name, 64, L"Global\\XajhCap5_%u", pid); for(int i=0;i<200;i++){g_map=CreateFileMappingW(INVALID_HANDLE_VALUE,nullptr,PAGE_READWRITE,0,sizeof(Ring),g_name); if(g_map)break;Sleep(100);} if(!g_map)return 1; g_ring=(Ring*)MapViewOfFile(g_map,FILE_MAP_ALL_ACCESS,0,0,sizeof(Ring)); if(!g_ring)return 1; g_ring->magic=RING_MAGIC;g_ring->head=0;g_ring->tail=0;install();while(true)Sleep(1000000);return 0; }
BOOL APIENTRY DllMain(HMODULE h,DWORD reason,LPVOID){if(reason==DLL_PROCESS_ATTACH){DisableThreadLibraryCalls(h);HANDLE t=CreateThread(nullptr,0,Worker,nullptr,0,nullptr);if(t)CloseHandle(t);}else if(reason==DLL_PROCESS_DETACH)remove_hooks();return TRUE;}
