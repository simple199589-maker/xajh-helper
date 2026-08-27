// xajh_login_bridge.dll — minimal standalone login-stage bridge for xajh.exe
// (x86). Purpose: drive the game's own account/password UI strictly in the
// background (no foreground switch) using only:
//   LCMD_PING      — health check
//   LCMD_UI_CLICK  — background mouse sequence on the game hwnd
//   LCMD_UI_KEY    — background key inject (AttachThreadInput + SendInput +
//                    PostMessage fallback)
// It deliberately does NOT load any production bridge logic, does not patch
// game code, and carries none of the production capabilities. It is injected
// only while the login stage is active and is never used after the character
// enters the world.
// Shared memory: Global\XajhLoginBridge_<pid> / Local\XajhLoginBridge_<pid>
// @author by ak
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "login_bridge_protocol.h"

#pragma comment(lib, "user32.lib")
#pragma comment(lib, "kernel32.lib")
#pragma comment(lib, "imm32.lib")

#define LOGIN_BRIDGE_WM (WM_APP + 0x53)

static LoginBridgeShared* g_shm = nullptr;
static HANDLE g_map = nullptr;
static HWND g_hwnd = nullptr;
static HMODULE g_self = nullptr;
static HMODULE g_game = nullptr;
static volatile LONG g_ready = 0;
static volatile LONG g_command_running = 0;
static const UINT_PTR kDispatchTimerId = 0x4C424A32u; /* 'LBJ2' */
static const UINT kDispatchPeriodMs = 25;

// ---- KEY_HOLD: process-local key force via IAT GetAsyncKeyState/GetKeyState
// hook. Mirrors the production bridge's solution-2 (no SoftSend, no focus).
// Unload-safe: restored in DllMain PROCESS_DETACH.
static const uint32_t kIatRvaGetAsyncKeyState = 0x00E28BE0u;
static const uint32_t kIatRvaGetKeyState = 0x00E28B50u;
typedef SHORT(WINAPI* FnGetAsyncKeyStateT)(int vKey);
typedef SHORT(WINAPI* FnGetKeyStateT)(int nVirtKey);
static FnGetAsyncKeyStateT g_real_GetAsyncKeyState = nullptr;
static FnGetKeyStateT g_real_GetKeyState = nullptr;
static volatile uint8_t g_force_vk[256] = {0};
static volatile LONG g_key_hooks_installed = 0;
static uint32_t g_iat_async_orig = 0;
static uint32_t g_iat_key_orig = 0;
static bool g_iat_async_patched = false;
static bool g_iat_key_patched = false;

static bool IsForcedVk(int vk) {
  if (vk < 0 || vk > 0xFF) return false;
  return g_force_vk[vk & 0xFF] != 0;
}

static SHORT WINAPI LbHook_GetAsyncKeyState(int vKey) {
  SHORT r = g_real_GetAsyncKeyState ? g_real_GetAsyncKeyState(vKey) : (SHORT)0;
  if (IsForcedVk(vKey)) return (SHORT)0x8001;
  return r;
}

static SHORT WINAPI LbHook_GetKeyState(int nVirtKey) {
  SHORT r = g_real_GetKeyState ? g_real_GetKeyState(nVirtKey) : (SHORT)0;
  if (IsForcedVk(nVirtKey)) return (SHORT)0x8001;
  return r;
}

static bool PatchIatSlot32(uint32_t* slot, uint32_t new_fn, uint32_t* out_old) {
  if (!slot || !new_fn) return false;
  DWORD old_prot = 0;
  if (!VirtualProtect(slot, sizeof(uint32_t), PAGE_EXECUTE_READWRITE, &old_prot)) {
    if (!VirtualProtect(slot, sizeof(uint32_t), PAGE_READWRITE, &old_prot)) return false;
  }
  if (out_old) *out_old = *slot;
  *slot = new_fn;
  FlushInstructionCache(GetCurrentProcess(), slot, sizeof(uint32_t));
  VirtualProtect(slot, sizeof(uint32_t), old_prot, &old_prot);
  return true;
}

static bool InstallKeyStateHooks() {
  if (InterlockedCompareExchange(&g_key_hooks_installed, 0, 0)) return true;
  HMODULE u32 = GetModuleHandleW(L"user32.dll");
  if (!u32) u32 = LoadLibraryW(L"user32.dll");
  if (!u32) return false;
  if (!g_real_GetAsyncKeyState)
    g_real_GetAsyncKeyState = (FnGetAsyncKeyStateT)GetProcAddress(u32, "GetAsyncKeyState");
  if (!g_real_GetKeyState)
    g_real_GetKeyState = (FnGetKeyStateT)GetProcAddress(u32, "GetKeyState");
  if (!g_real_GetAsyncKeyState || !g_real_GetKeyState) return false;

  uint32_t base = g_shm ? g_shm->module_base : 0;
  if (!base) base = (uint32_t)(uintptr_t)g_game;
  if (!base) base = 0x400000u;
  uint32_t hook_async = (uint32_t)(uintptr_t)&LbHook_GetAsyncKeyState;
  uint32_t hook_key = (uint32_t)(uintptr_t)&LbHook_GetKeyState;

  __try {
    uint32_t* iat_async = (uint32_t*)(uintptr_t)(base + kIatRvaGetAsyncKeyState);
    uint32_t* iat_key = (uint32_t*)(uintptr_t)(base + kIatRvaGetKeyState);
    uint32_t cur_a = *iat_async;
    uint32_t cur_k = *iat_key;
    if (cur_a == (uint32_t)(uintptr_t)g_real_GetAsyncKeyState ||
        cur_a == hook_async) {
      if (cur_a == (uint32_t)(uintptr_t)g_real_GetAsyncKeyState) {
        g_iat_async_orig = cur_a;
        if (!PatchIatSlot32(iat_async, hook_async, &g_iat_async_orig)) return false;
      }
      g_iat_async_patched = true;
    }
    if (cur_k == (uint32_t)(uintptr_t)g_real_GetKeyState || cur_k == hook_key) {
      if (cur_k == (uint32_t)(uintptr_t)g_real_GetKeyState) {
        g_iat_key_orig = cur_k;
        if (!PatchIatSlot32(iat_key, hook_key, &g_iat_key_orig)) return false;
      }
      g_iat_key_patched = true;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
  InterlockedExchange(&g_key_hooks_installed, 1);
  return true;
}

static void RemoveKeyStateHooks() {
  if (!InterlockedCompareExchange(&g_key_hooks_installed, 0, 0)) return;
  uint32_t base = g_shm ? g_shm->module_base : 0;
  if (!base) base = (uint32_t)(uintptr_t)g_game;
  if (!base) base = 0x400000u;
  __try {
    if (g_iat_async_patched && g_iat_async_orig) {
      PatchIatSlot32((uint32_t*)(uintptr_t)(base + kIatRvaGetAsyncKeyState),
                     g_iat_async_orig, nullptr);
      g_iat_async_patched = false;
    }
    if (g_iat_key_patched && g_iat_key_orig) {
      PatchIatSlot32((uint32_t*)(uintptr_t)(base + kIatRvaGetKeyState),
                     g_iat_key_orig, nullptr);
      g_iat_key_patched = false;
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  for (int i = 0; i < 256; ++i) g_force_vk[i] = 0;
}

// ---- GetKeyboardState inline hook (background char input) ----
// The game translates an injected WM_KEYDOWN to a character using the global
// keyboard state (GetKeyboardState). Hook it so the forced VK is reported
// down only while the key is being typed. Trampoline preserves the original
// prologue (mov edi,edi; push ebp; mov ebp,esp) so the real body still works.
static BOOL(WINAPI* g_real_GKS)(BYTE*) = nullptr;
static uint8_t g_gks_tramp[16];
static volatile LONG g_gks_hooked = 0;
static volatile uint8_t g_forced_vk = 0;

static BOOL WINAPI LbHook_GKS(BYTE* lpKeyState) {
  BOOL r = FALSE;
  if (g_real_GKS) {
    r = ((BOOL(WINAPI*)(BYTE*))(void*)g_gks_tramp)(lpKeyState);
  }
  if (lpKeyState && g_forced_vk) {
    lpKeyState[g_forced_vk & 0xFFu] |= 0x80;
    lpKeyState[0x10u & 0xFFu] &= ~0x80;   /* VK_SHIFT up -> lowercase */
    lpKeyState[0x14u & 0xFFu] &= ~0x01;   /* VK_CAPITAL off */
  }
  return r;
}

static bool InstallGksHook() {
  if (InterlockedCompareExchange(&g_gks_hooked, 0, 0)) return true;
  HMODULE u32 = GetModuleHandleW(L"user32.dll");
  if (!u32) u32 = LoadLibraryW(L"user32.dll");
  if (!u32) return false;
  g_real_GKS = (BOOL(WINAPI*)(BYTE*))GetProcAddress(u32, "GetKeyboardState");
  if (!g_real_GKS) return false;
  __try {
    memcpy(g_gks_tramp, (void*)g_real_GKS, 5);
    uintptr_t real_body = (uintptr_t)g_real_GKS + 5;
    int32_t rel = (int32_t)(real_body - (uintptr_t)(g_gks_tramp + 5));
    g_gks_tramp[5] = 0xE9;
    *(int32_t*)(g_gks_tramp + 6) = rel;
    uintptr_t hook = (uintptr_t)&LbHook_GKS;
    int32_t rel2 = (int32_t)(hook - ((uintptr_t)g_real_GKS + 5));
    DWORD old = 0;
    if (!VirtualProtect((void*)g_real_GKS, 5, PAGE_EXECUTE_READWRITE, &old)) return false;
    uint8_t* p = (uint8_t*)g_real_GKS;
    p[0] = 0xE9;
    *(int32_t*)(p + 1) = rel2;
    VirtualProtect((void*)g_real_GKS, 5, old, &old);
    FlushInstructionCache(GetCurrentProcess(), (void*)g_real_GKS, 5);
    InterlockedExchange(&g_gks_hooked, 1);
    return true;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    return false;
  }
}

static void RemoveGksHook() {
  if (!InterlockedCompareExchange(&g_gks_hooked, 0, 0)) return;
  __try {
    if (g_real_GKS) {
      DWORD old = 0;
      if (VirtualProtect((void*)g_real_GKS, 5, PAGE_EXECUTE_READWRITE, &old)) {
        memcpy((void*)g_real_GKS, g_gks_tramp, 5);
        VirtualProtect((void*)g_real_GKS, 5, old, &old);
        FlushInstructionCache(GetCurrentProcess(), (void*)g_real_GKS, 5);
      }
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
  }
  g_forced_vk = 0;
  g_real_GKS = nullptr;
}

static void SetErr(const char* msg) {
  if (!g_shm) return;
  strncpy_s(g_shm->err, msg ? msg : "error", _TRUNCATE);
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

static HWND FindGameHwnd() {
  struct EnumCtx {
    DWORD pid;
    HWND preferred;
  } ctx{GetCurrentProcessId(), nullptr};
  EnumWindows(
      [](HWND h, LPARAM lp) -> BOOL {
        auto* c = (EnumCtx*)lp;
        DWORD pid = 0;
        GetWindowThreadProcessId(h, &pid);
        if (pid != c->pid) return TRUE;
        if (!IsWindowVisible(h)) return TRUE;
        wchar_t cls[128] = {};
        GetClassNameW(h, cls, 128);
        if (wcsstr(cls, L"XAJH") || wcsstr(cls, L"xajh") ||
            wcsstr(cls, L"Xajh")) {
          c->preferred = h;
          return FALSE;
        }
        return TRUE;
      },
      (LPARAM)&ctx);
  return ctx.preferred;
}

// Execute one pending command. Runs on the game's UI thread (SetTimer).
static void RunCommand() {
  if (!g_shm) return;
  const uint32_t cmd = g_shm->cmd;
  const uint32_t lo = g_shm->id_lo;
  const uint32_t hi = g_shm->id_hi;

  if (cmd == LCMD_PING) {
    g_shm->ret = (int32_t)LOGIN_BRIDGE_BUILD_ID;
    g_shm->status = LST_OK;
    SetErr("pong");
    return;
  }

  if (cmd == LCMD_UI_CLICK) {
    HWND h = g_hwnd;
    if ((!h || !IsWindow(h)) && g_shm && g_shm->hwnd) {
      h = (HWND)(uintptr_t)g_shm->hwnd;
    }
    if (!h || !IsWindow(h)) {
      SetErr("UI_CLICK no hwnd");
      g_shm->status = LST_ERR;
      return;
    }
    const int cx = (int)(lo & 0xFFFFu);
    const int cy = (int)(hi & 0xFFFFu);
    int hold_ms = 55;
    if (g_shm) {
      const int m = (int)g_shm->mode;
      if (m > 0 && m < 800) hold_ms = m;
    }
    const int btn = g_shm ? (int)g_shm->tid : 0; /* 0=L 1=R */
    const LPARAM lp = (LPARAM)((cy << 16) | (cx & 0xFFFF));
    const bool right = (btn == 1);
    const WPARAM mk = right ? 0x0002u : 0x0001u;
    const UINT msg_down = right ? 0x0204u : 0x0201u;
    const UINT msg_up = right ? 0x0205u : 0x0202u;
    PostMessageW(h, 0x0200 /* WM_MOUSEMOVE */, 0, lp);
    Sleep(8 + (DWORD)(hold_ms / 12));
    PostMessageW(h, msg_down, mk, lp);
    Sleep((DWORD)hold_ms);
    PostMessageW(h, msg_up, 0, lp);
    Sleep(6 + (DWORD)(hold_ms / 16));
    PostMessageW(h, 0x0200, 0, lp);
    g_shm->ret = 1;
    g_shm->status = LST_OK;
    SetErr(right ? "UI_CLICK R ok" : "UI_CLICK L ok");
    return;
  }

  if (cmd == LCMD_UI_KEY) {
    HWND h = g_hwnd;
    if ((!h || !IsWindow(h)) && g_shm && g_shm->hwnd) {
      h = (HWND)(uintptr_t)g_shm->hwnd;
    }
    const UINT vk_in = (UINT)(lo & 0xFFu);
    if (!vk_in) {
      SetErr("UI_KEY vk=0");
      g_shm->status = LST_ERR;
      return;
    }
    UINT vk = vk_in;
    if (vk == 0x10u) vk = 0xA0u;   /* LSHIFT */
    if (vk == 0x11u) vk = 0xA2u;   /* LCONTROL */
    if (vk == 0x12u) vk = 0xA4u;   /* LMENU */
    const int action = (int)(hi & 0xFFu); /* 0 down, 1 up, 2 press */
    int hold_ms = 40;
    bool no_focus = false;
    if (g_shm) {
      const int m = (int)g_shm->mode;
      hold_ms = m & 0x0FFF;
      if (hold_ms <= 0 || hold_ms >= 2000) hold_ms = 40;
      no_focus = (m & 0x1000) != 0;
    }
    const UINT sc = MapVirtualKeyW(vk, MAPVK_VK_TO_VSC);
    const LPARAM lp_down = (LPARAM)(1u | ((sc & 0xFFu) << 16));
    const LPARAM lp_up = (LPARAM)(1u | ((sc & 0xFFu) << 16) | (1u << 30) |
                                  (1u << 31));
    const bool is_ext =
        (vk == 0xA3u || vk == 0xA5u || vk == 0x2Du || vk == 0x2Eu ||
         vk == 0x21u || vk == 0x22u || vk == 0x23u || vk == 0x24u ||
         (vk >= 0x25u && vk <= 0x28u));
    const LPARAM lp_down_e = lp_down | (1u << 24);
    const LPARAM lp_up_e = lp_up | (1u << 24);

    // Soft-foreground so SendInput updates async key state without stealing
    // the user's foreground when no_focus is requested (e.g. Enter confirm).
    DWORD fg_tid = 0;
    DWORD our_tid = GetCurrentThreadId();
    bool attached = false;
    if (!no_focus && h && IsWindow(h)) {
      HWND fg = GetForegroundWindow();
      if (fg != h) {
        fg_tid = GetWindowThreadProcessId(fg ? fg : h, nullptr);
        if (fg_tid && fg_tid != our_tid) {
          if (AttachThreadInput(our_tid, fg_tid, TRUE)) attached = true;
        }
        BringWindowToTop(h);
        SetForegroundWindow(h);
        SetFocus(h);
      }
    }

    auto inject_down = [&]() {
      INPUT in[2] = {};
      memset(in, 0, sizeof(in));
      in[0].type = in[1].type = INPUT_KEYBOARD;
      in[0].ki.wVk = (WORD)vk;
      in[0].ki.wScan = (WORD)sc;
      in[0].ki.dwFlags = is_ext ? KEYEVENTF_EXTENDEDKEY : 0;
      in[1].ki.wVk = (WORD)vk;
      in[1].ki.wScan = (WORD)sc;
      in[1].ki.dwFlags = is_ext ? KEYEVENTF_EXTENDEDKEY : 0;
      SendInput(2, in, sizeof(INPUT));
      PostMessageW(h, 0x0100 /* WM_KEYDOWN */, vk, is_ext ? lp_down_e : lp_down);
    };
    auto inject_up = [&]() {
      INPUT in[2] = {};
      memset(in, 0, sizeof(in));
      in[0].type = in[1].type = INPUT_KEYBOARD;
      in[0].ki.wVk = (WORD)vk;
      in[0].ki.wScan = (WORD)sc;
      in[0].ki.dwFlags =
          (is_ext ? KEYEVENTF_EXTENDEDKEY : 0) | KEYEVENTF_KEYUP;
      in[1].ki.wVk = (WORD)vk;
      in[1].ki.wScan = (WORD)sc;
      in[1].ki.dwFlags =
          (is_ext ? KEYEVENTF_EXTENDEDKEY : 0) | KEYEVENTF_KEYUP;
      SendInput(2, in, sizeof(INPUT));
      PostMessageW(h, 0x0101 /* WM_KEYUP */, vk, is_ext ? lp_up_e : lp_up);
    };

    if (action == 2) { /* press */
      inject_down();
      Sleep((DWORD)hold_ms);
      inject_up();
    } else if (action == 0) { /* down */
      inject_down();
    } else { /* up */
      inject_up();
    }

    if (attached) AttachThreadInput(our_tid, fg_tid, FALSE);
    g_shm->ret = 1;
    g_shm->status = LST_OK;
    SetErr("UI_KEY ok");
    return;
  }

  if (cmd == LCMD_UI_DIALOG_COMMAND) {
    // Win_LoginServerList does not expose a visible AUI child named
    // "confirm". Its native command switch (vtable+0x28 -> 0xE8C690) only
    // resolves IDCANCEL/IDYES/IDNO when this->state(+0x78) == 1; otherwise the
    // command string is forwarded to a login-UI manager that checks
    // Win_Login/Win_LoginWait etc. (absent on the server-select page) and
    // silently no-ops. So the real confirm is: set state=1 then dispatch
    // "IDYES" on the same UI thread.
    const uintptr_t dialog = (uintptr_t)lo;
    const uint32_t command = hi;
    if (!dialog || command != LDC_CONFIRM) {
      SetErr("UI_DIALOG_COMMAND bad args");
      g_shm->status = LST_ERR;
      return;
    }
    __try {
      const uintptr_t vtable = *(const uintptr_t*)dialog;
      const uintptr_t fn_addr = vtable ? *(const uintptr_t*)(vtable + 0x28) : 0;
      if (!vtable || !fn_addr) {
        SetErr("UI_DIALOG_COMMAND bad vtable");
        g_shm->status = LST_ERR;
        return;
      }
      // Unlock the IDCANCEL/IDYES/IDNO command switch, then confirm.
      *(volatile uint32_t*)(dialog + 0x78) = 1;
      typedef int(__thiscall* DialogCommandFn)(void*, const char*);
      const int ret = ((DialogCommandFn)fn_addr)((void*)dialog, "IDYES");
      g_shm->ret = ret;
      g_shm->status = LST_OK;
      SetErr("UI_DIALOG_COMMAND confirm ok");
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      SetErr("UI_DIALOG_COMMAND SEH");
      g_shm->status = LST_ERR;
    }
    return;
  }

  if (cmd == LCMD_KEY_HOLD) {
    // Process-local key force via IAT hook. id_lo=vk, id_hi:
    // 0=off 1=on 2=status 3=clear-all. mode bit0=press (down then up).
    const UINT vk = (UINT)(lo & 0xFFu);
    const int action = (int)(hi & 0xFFu);
    const int mode = g_shm ? (int)g_shm->mode : 0;

    if (action == 3) {
      for (int i = 0; i < 256; ++i) g_force_vk[i] = 0;
      g_shm->ret = 0;
      g_shm->status = LST_OK;
      SetErr("KEY_HOLD CLEAR_ALL ok");
      return;
    }

    if (!InstallKeyStateHooks()) {
      SetErr("KEY_HOLD hook install fail");
      g_shm->status = LST_ERR;
      return;
    }

    if (action == 2) {
      int n = 0;
      for (int i = 1; i < 256; ++i)
        if (g_force_vk[i]) n++;
      g_shm->ret = n;
      g_shm->status = LST_OK;
      SetErr("KEY_HOLD status ok");
      return;
    }

    if (!vk || vk > 0xFEu) {
      SetErr("KEY_HOLD vk invalid");
      g_shm->status = LST_ERR;
      return;
    }

    if (action != 0) {
      g_force_vk[vk & 0xFFu] = 1;
      if (vk == 0x10u) g_force_vk[0xA0u] = 1;  // generic shift -> LSHIFT
    } else {
      g_force_vk[vk & 0xFFu] = 0;
      if (vk == 0x10u) g_force_vk[0xA0u] = 0;
    }

    if (action != 0 && (mode & 1)) {
      // press: hold briefly then release
      Sleep((DWORD)((g_shm && g_shm->mode) ? 60 : 60));
      g_force_vk[vk & 0xFFu] = 0;
      if (vk == 0x10u) g_force_vk[0xA0u] = 0;
    }
    g_shm->ret = 1;
    g_shm->status = LST_OK;
    SetErr("KEY_HOLD ok");
    return;
  }


  if (cmd == LCMD_UI_INPUT) {
    // Login UI unified input processor (preferred base VA 0x00E90CC0).
    // Native Enter on server-select hits this with:
    //   ecx = this (Win_LoginServerList dialog)
    //   msg = 0x100 WM_KEYDOWN / 0x101 WM_KEYUP / 0x102 WM_CHAR
    //   wParam = VK/char, lParam = 1, extra = 1
    // SendInput / PostMessage / GAKS hooks do NOT reach this path for
    // server-select confirm. Credentials typing can still use KEY_HOLD once
    // an edit control has focus.
    const uintptr_t self = (uintptr_t)lo;
    const uint32_t msg = hi;
    const uint32_t wparam = (uint32_t)(g_shm ? g_shm->mode : 0);
    const uint32_t lparam = (uint32_t)(g_shm ? g_shm->tid : 1);
    if (!self || !msg) {
      SetErr("UI_INPUT bad args");
      g_shm->status = LST_ERR;
      return;
    }
    HMODULE game = g_game ? g_game : GetModuleHandleW(L"xajh.exe");
    if (!game) game = GetModuleHandleW(nullptr);
    if (!game) {
      SetErr("UI_INPUT no module");
      g_shm->status = LST_ERR;
      return;
    }
    const uintptr_t fn = (uintptr_t)game + 0x00A90CC0u; /* 0xE90CC0 - 0x400000 */
    __try {
      typedef int(__thiscall* LoginInputFn)(void*, uint32_t, uint32_t, uint32_t, uint32_t);
      const int ret =
          ((LoginInputFn)fn)((void*)self, msg, wparam, lparam ? lparam : 1u, 1u);
      g_shm->ret = ret;
      g_shm->status = LST_OK;
      SetErr("UI_INPUT ok");
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      SetErr("UI_INPUT SEH");
      g_shm->status = LST_ERR;
    }
    return;
  }

  if (cmd == LCMD_IME_OFF) {
    // Disable (or restore) the process IME from the game UI thread so
    // keybd_event ASCII characters are not swallowed by a Chinese IME.
    const int on = (int)(lo & 0xFFu);
    HWND h = g_hwnd;
    if ((!h || !IsWindow(h)) && g_shm && g_shm->hwnd) {
      h = (HWND)(uintptr_t)g_shm->hwnd;
    }
    __try {
      if (h && IsWindow(h)) {
        HIMC imc = ImmGetContext(h);
        if (imc) {
          ImmSetOpenStatus(imc, on ? TRUE : FALSE);
          ImmReleaseContext(h, imc);
          g_shm->ret = 1;
          g_shm->status = LST_OK;
          SetErr(on ? "IME_ON ok" : "IME_OFF ok");
          return;
        }
      }
      SetErr("IME_OFF no imc");
      g_shm->status = LST_ERR;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      SetErr("IME_OFF SEH");
      g_shm->status = LST_ERR;
    }
    return;
  }

  if (cmd == LCMD_INJECT_CHAR) {
    // Background char input. Production bridge showed 0x4BF9C0 is NOT an
    // injector (it dereferences arg3 as a pointer), and that the reliable
    // background path is: IAT GetAsyncKeyState/GetKeyState hooks + force
    // the VK in the game's own key table [input_this+0x2C+vk] + call game
    // UpdateKeys (0x4BEFF0). We add the GetKeyboardState hook on top so the
    // element engine's key->char translation sees the key down too.
    const uint32_t ch = (uint32_t)(lo & 0xFFFFu);
    if (!ch) {
      SetErr("INJECT_CHAR bad char");
      g_shm->status = LST_ERR;
      return;
    }
    if (!InstallGksHook() || !InstallKeyStateHooks()) {
      SetErr("INJECT_CHAR hook fail");
      g_shm->status = LST_ERR;
      return;
    }
    BYTE vk = (BYTE)(ch & 0xFFu);
    if (ch >= 0x61u && ch <= 0x7Au) vk = (BYTE)(ch - 0x20u); /* a-z -> VK */

    HMODULE game = g_game ? g_game : GetModuleHandleW(L"xajh.exe");
    if (!game) game = GetModuleHandleW(nullptr);
    uint32_t base = game ? (uint32_t)(uintptr_t)game : 0;
    if (!base) base = 0x400000u;

    void* inp = nullptr;
    __try {
      uint32_t* glob = (uint32_t*)(uintptr_t)(base + 0x11282D8u);
      uint32_t root = glob ? *glob : 0;
      uint32_t mid = root ? *(uint32_t*)(uintptr_t)(root + 0x24u) : 0;
      inp = mid ? (void*)(uintptr_t)(*(uint32_t*)(uintptr_t)(mid + 0x78u)) : nullptr;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      inp = nullptr;
    }

    // Force the VK in all state sources the game may poll.
    g_force_vk[vk & 0xFFu] = 1;
    if (vk == 0x10u) g_force_vk[0xA0u] = 1; /* SHIFT -> LSHIFT */
    g_forced_vk = vk;

    if (inp) {
      __try {
        uint8_t* t = (uint8_t*)inp;
        t[0x2Cu + (vk & 0xFFu)] = 1; /* input_this key table */
        if (vk == 0x10u || vk == 0xA0u || vk == 0xA1u) {
          t[0x2Cu + 0x10] = 1;
          t[0x2Cu + 0xA0] = 1;
          t[0x2Cu + 0xA1] = 1;
          *(uint32_t*)(t + 0x134u) |= 1u; /* mod mask shift */
        }
        // Call game UpdateKeys(input_this) to rebuild its internal state.
        uint32_t fn_update = base + 0x00BEFF0u; /* 0x4BEFF0 */
        __asm {
          mov ecx, inp
          call fn_update
        }
      } __except (EXCEPTION_EXECUTE_HANDLER) {
      }
    }

    Sleep(70);
    g_forced_vk = 0;
    g_force_vk[vk & 0xFFu] = 0;
    if (vk == 0x10u) g_force_vk[0xA0u] = 0;
    if (inp) {
      __try {
        uint8_t* t = (uint8_t*)inp;
        t[0x2Cu + (vk & 0xFFu)] = 0;
        if (vk == 0x10u || vk == 0xA0u || vk == 0xA1u) {
          t[0x2Cu + 0x10] = 0;
          t[0x2Cu + 0xA0] = 0;
          t[0x2Cu + 0xA1] = 0;
          *(uint32_t*)(t + 0x134u) &= ~1u;
        }
      } __except (EXCEPTION_EXECUTE_HANDLER) {
      }
    }
    g_shm->ret = 1;
    g_shm->status = LST_OK;
    SetErr("INJECT_CHAR ok");
    return;
  }

  if (cmd == LCMD_UNICODE_INPUT) {
    // Foreground Unicode char input via SendInput(KEYEVENTF_UNICODE) on the
    // game UI thread. Bypasses the Chinese IME (no candidate window) and
    // delivers WM_CHAR directly to the focused edit control. The game window
    // must be the foreground window (SendInput target).
    const uint32_t ch = (uint32_t)(lo & 0xFFFFu);
    if (!ch || ch > 0xFFFFu) {
      SetErr("UNICODE_INPUT bad char");
      g_shm->status = LST_ERR;
      return;
    }
    __try {
      INPUT in[2];
      ZeroMemory(in, sizeof(in));
      in[0].type = in[1].type = INPUT_KEYBOARD;
      in[0].ki.wVk = in[1].ki.wVk = 0;
      in[0].ki.wScan = in[1].ki.wScan = (WORD)ch;
      in[0].ki.dwFlags = KEYEVENTF_UNICODE;
      in[1].ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP;
      UINT n = SendInput(2, in, sizeof(INPUT));
      g_shm->ret = (int32_t)n;
      g_shm->status = n > 0 ? LST_OK : LST_ERR;
      SetErr(n > 0 ? "UNICODE_INPUT ok" : "UNICODE_INPUT SendInput=0");
    } __except (EXCEPTION_EXECUTE_HANDLER) {
      SetErr("UNICODE_INPUT SEH");
      g_shm->status = LST_ERR;
    }
    return;
  }

  SetErr("unknown cmd");
  g_shm->status = LST_ERR;
}

static VOID CALLBACK DispatchTimerProc(HWND hwnd, UINT, UINT_PTR id, DWORD) {
  if (id != kDispatchTimerId) return;
  if (InterlockedCompareExchange(&g_command_running, 1, 0) != 0) return;
  uint32_t dispatch_seq = 0;
  __try {
    if (!g_shm || g_shm->magic != LOGIN_BRIDGE_MAGIC) __leave;
    HWND preferred = PreferredHwndFromShm();
    if (preferred) g_hwnd = preferred;
    else if (hwnd) g_hwnd = hwnd;
    if (g_shm->status == LST_PENDING) {
      dispatch_seq = g_shm->seq;
      RunCommand();
      if (g_shm->status == LST_OK || g_shm->status == LST_ERR) {
        g_shm->ack_seq = dispatch_seq;
      }
    }
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    if (g_shm) {
      SetErr("dispatch timer SEH");
      g_shm->status = LST_ERR;
      g_shm->ack_seq = dispatch_seq;
    }
  }
  InterlockedExchange(&g_command_running, 0);
}

static bool ArmDispatchTimer(HWND hwnd) {
  if (!hwnd || !IsWindow(hwnd)) return false;
  DWORD pid = 0;
  GetWindowThreadProcessId(hwnd, &pid);
  if (pid != GetCurrentProcessId()) return false;
  if (!SetTimer(hwnd, kDispatchTimerId, kDispatchPeriodMs, DispatchTimerProc)) {
    return false;
  }
  g_hwnd = hwnd;
  return true;
}

static void KillDispatchTimer() {
  if (g_hwnd && IsWindow(g_hwnd)) {
    KillTimer(g_hwnd, kDispatchTimerId);
  }
  g_hwnd = nullptr;
  InterlockedExchange(&g_ready, 0);
}

static bool OpenShared() {
  const DWORD pid = GetCurrentProcessId();
  wchar_t name_g[64], name_l[64];
  swprintf_s(name_g, L"Global\\XajhLoginBridgeV2_%u", pid);
  swprintf_s(name_l, L"Local\\XajhLoginBridgeV2_%u", pid);
  bool created_fresh = false;
  g_map = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, name_g);
  if (!g_map) g_map = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, name_l);
  if (!g_map) {
    g_map = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0,
                               sizeof(LoginBridgeShared), name_g);
    if (g_map && GetLastError() != ERROR_ALREADY_EXISTS) created_fresh = true;
  }
  if (!g_map) {
    g_map = CreateFileMappingW(INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE, 0,
                               sizeof(LoginBridgeShared), name_l);
    if (g_map && GetLastError() != ERROR_ALREADY_EXISTS) created_fresh = true;
  }
  if (!g_map) return false;
  g_shm = (LoginBridgeShared*)MapViewOfFile(g_map, FILE_MAP_ALL_ACCESS, 0, 0,
                                            sizeof(LoginBridgeShared));
  if (!g_shm) return false;
  if (created_fresh || g_shm->magic != LOGIN_BRIDGE_MAGIC) {
    uint32_t keep_hwnd = (g_shm->magic == LOGIN_BRIDGE_MAGIC) ? g_shm->hwnd : 0;
    ZeroMemory((void*)g_shm, sizeof(LoginBridgeShared));
    g_shm->magic = LOGIN_BRIDGE_MAGIC;
    g_shm->status = LST_IDLE;
    if (keep_hwnd) g_shm->hwnd = keep_hwnd;
  } else {
    g_shm->magic = LOGIN_BRIDGE_MAGIC;
    if (g_shm->status != LST_PENDING) g_shm->status = LST_IDLE;
  }
  g_game = GetModuleHandleW(L"xajh.exe");
  if (!g_game) g_game = GetModuleHandleW(nullptr);
  if (g_game) g_shm->module_base = (uint32_t)(uintptr_t)g_game;
  g_shm->protocol_version = LOGIN_BRIDGE_PROTOCOL_VERSION;
  g_shm->struct_size = (uint32_t)sizeof(LoginBridgeShared);
  return true;
}

static void CloseShared() {
  KillDispatchTimer();
  if (g_shm) {
    UnmapViewOfFile((LPCVOID)g_shm);
    g_shm = nullptr;
  }
  if (g_map) {
    CloseHandle(g_map);
    g_map = nullptr;
  }
}

static DWORD WINAPI AttachThreadProc(LPVOID) {
  __try {
    if (!OpenShared()) return 1;
    if (g_shm) SetErr("shm ready (await hwnd)");
    // Wait for a game hwnd: prefer helper-written shm hwnd, else enum.
    const int max_i = 600;
    for (int i = 0; i < max_i; ++i) {
      if (InterlockedCompareExchange(&g_ready, 0, 0)) return 0;
      HWND preferred = PreferredHwndFromShm();
      if (!preferred && (i == 10 || i == 30 || (i > 30 && (i % 30) == 0))) {
        preferred = FindGameHwnd();
        if (preferred && g_shm && !g_shm->hwnd) {
          g_shm->hwnd = (uint32_t)(uintptr_t)preferred;
        }
      }
      if (preferred && ArmDispatchTimer(preferred)) {
        InterlockedExchange(&g_ready, 1);
        if (g_shm) {
          g_shm->hwnd = (uint32_t)(uintptr_t)preferred;
          SetErr("login bridge ready");
        }
        return 0;
      }
      Sleep(50);
    }
    if (g_shm) SetErr("login bridge timer timeout");
    return 2;
  } __except (EXCEPTION_EXECUTE_HANDLER) {
    if (g_shm) SetErr("attach SEH");
    return 3;
  }
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
  if (reason == DLL_PROCESS_ATTACH) {
    g_self = hModule;
    DisableThreadLibraryCalls(hModule);
    HANDLE thr = CreateThread(nullptr, 0, AttachThreadProc, nullptr, 0, nullptr);
    if (thr) CloseHandle(thr);
    else return FALSE;
  } else if (reason == DLL_PROCESS_DETACH) {
    RemoveGksHook();
    RemoveKeyStateHooks();
    KillDispatchTimer();
    CloseShared();
  }
  return TRUE;
}
