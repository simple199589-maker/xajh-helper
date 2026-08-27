// Minimal test: just create shared memory, return TRUE.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>

#define MY_MAGIC 0x54455354u  // 'TEST'

static HANDLE g_map = nullptr;

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
  if (reason == DLL_PROCESS_ATTACH) {
    DisableThreadLibraryCalls(module);
    wchar_t name[64] = {};
    swprintf_s(name, L"Local\\XajhChatTap_%lu", (unsigned long)GetCurrentProcessId());
    g_map = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0, 303640, name);
    if (!g_map) return FALSE;
  }
  return TRUE;
}
