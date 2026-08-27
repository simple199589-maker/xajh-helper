// Minimal LoadLibrary injector for x86 xajh_login_bridge.dll
// Usage: xajh_login_inject.exe <pid> <full_path_to_dll>
// @author by ak
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void FlushOut() {
  fflush(stdout);
  fflush(stderr);
}

int wmain(int argc, wchar_t** argv) {
  if (argc < 3) {
    wprintf(L"usage: xajh_login_inject.exe <pid> <dll_path>\n");
    FlushOut();
    return 2;
  }
  DWORD pid = (DWORD)wcstoul(argv[1], NULL, 10);
  const wchar_t* dll = argv[2];
  if (!pid || !dll || !dll[0]) {
    wprintf(L"bad args\n");
    FlushOut();
    return 2;
  }
  DWORD attr = GetFileAttributesW(dll);
  if (attr == INVALID_FILE_ATTRIBUTES || (attr & FILE_ATTRIBUTE_DIRECTORY)) {
    wprintf(L"dll not found: %s err=%lu\n", dll, GetLastError());
    FlushOut();
    return 1;
  }
  wprintf(L"login inject begin pid=%lu dll=%s\n", (unsigned long)pid, dll);
  FlushOut();

  size_t bytes = (wcslen(dll) + 1) * sizeof(wchar_t);
  HANDLE h = OpenProcess(PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
                             PROCESS_VM_OPERATION | PROCESS_VM_WRITE |
                             PROCESS_VM_READ,
                         FALSE, pid);
  if (!h) {
    wprintf(L"OpenProcess failed %lu\n", GetLastError());
    FlushOut();
    return 1;
  }
  void* remote = VirtualAllocEx(h, NULL, bytes, MEM_COMMIT | MEM_RESERVE,
                                PAGE_READWRITE);
  if (!remote) {
    wprintf(L"VirtualAllocEx failed %lu\n", GetLastError());
    FlushOut();
    CloseHandle(h);
    return 1;
  }
  if (!WriteProcessMemory(h, remote, dll, bytes, NULL)) {
    wprintf(L"WriteProcessMemory failed %lu\n", GetLastError());
    FlushOut();
    VirtualFreeEx(h, remote, 0, MEM_RELEASE);
    CloseHandle(h);
    return 1;
  }
  HMODULE k32 = GetModuleHandleW(L"kernel32.dll");
  FARPROC load = GetProcAddress(k32, "LoadLibraryW");
  if (!load) {
    wprintf(L"GetProcAddress LoadLibraryW failed\n");
    FlushOut();
    VirtualFreeEx(h, remote, 0, MEM_RELEASE);
    CloseHandle(h);
    return 1;
  }
  HANDLE thr = CreateRemoteThread(h, NULL, 0, (LPTHREAD_START_ROUTINE)load,
                                  remote, 0, NULL);
  if (!thr) {
    wprintf(L"CreateRemoteThread failed %lu\n", GetLastError());
    FlushOut();
    VirtualFreeEx(h, remote, 0, MEM_RELEASE);
    CloseHandle(h);
    return 1;
  }
  DWORD wait = WaitForSingleObject(thr, 8000);
  if (wait != WAIT_OBJECT_0) {
    wprintf(L"remote LoadLibrary incomplete wait=%lu\n", (unsigned long)wait);
    FlushOut();
    CloseHandle(thr);
    CloseHandle(h);
    return 1;
  }
  DWORD code = 0;
  GetExitCodeThread(thr, &code);
  CloseHandle(thr);
  VirtualFreeEx(h, remote, 0, MEM_RELEASE);
  DWORD exit_code = 0;
  if (GetExitCodeProcess(h, &exit_code) && exit_code != STILL_ACTIVE) {
    wprintf(L"target exited during inject code=%lu\n",
            (unsigned long)exit_code);
    FlushOut();
    CloseHandle(h);
    return 1;
  }
  CloseHandle(h);
  if (!code) {
    wprintf(L"LoadLibrary returned NULL (inject failed)\n");
    FlushOut();
    return 1;
  }
  wprintf(L"login injected ok module=0x%lX\n", (unsigned long)code);
  FlushOut();
  return 0;
}
