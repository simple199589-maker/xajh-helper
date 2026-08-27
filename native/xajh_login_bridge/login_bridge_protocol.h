// xajh_login_bridge protocol — standalone minimal login-stage bridge.
// Only PING / UI_CLICK / UI_KEY / UI_DIALOG_COMMAND. Completely separate from the production
// xajh_bridge (own magic, own shared-memory namespace). Used exclusively
// during the account/login stage so login automation never touches the
// production feature bridge or its hooks.
// @author by ak
#pragma once

#include <stdint.h>

#define LOGIN_BRIDGE_MAGIC 0x4C584A32u          /* 'LXJ2' */
#define LOGIN_BRIDGE_PROTOCOL_VERSION 4u
#define LOGIN_BRIDGE_BUILD_ID 2026081301u

enum LoginBridgeCmdId {
  LCMD_IDLE = 0,
  LCMD_PING = 1,
  LCMD_UI_CLICK = 2,
  LCMD_UI_KEY = 3,
  // Executes the dialog's own UI-command virtual function on the game UI
  // thread. id_lo=AUIDialog*, id_hi=LoginBridgeDialogCommand.
  LCMD_UI_DIALOG_COMMAND = 4,
  // Process-local key force via IAT GetAsyncKeyState/GetKeyState hook.
  // id_lo=vk, id_hi=0 off / 1 on / 2 status / 3 clear-all.
  LCMD_KEY_HOLD = 5,
  // Same-process login UI input processor (preferred VA 0x00E90CC0).
  // id_lo=this*, id_hi=msg, mode=wParam, tid=lParam; extra fixed to 1.
  LCMD_UI_INPUT = 6,
  // Disable the process IME (ImmSetOpenStatus FALSE) on the game UI thread so
  // keybd_event ASCII chars are not swallowed by a Chinese IME. id_lo:
  // 0=off 1=on (restore). Returns 1 on success.
  LCMD_IME_OFF = 7,
  // Background char inject: install the GetKeyboardState inline hook, then
  // resolve the game input object (input_this) and call 0x4BF9C0(input_this,
  // WM_KEYDOWN/KEYUP, wParam=vk, lParam=1, 0, 0) so the game's key->char
  // translation (which reads GetKeyboardState) sees the key down. Lowercase
  // is forced (VK_SHIFT up / VK_CAPITAL off); id_lo = char (u16). No SendInput,
  // no focus steal.
  LCMD_INJECT_CHAR = 8,
  // Foreground Unicode char input: SendInput(KEYEVENTF_UNICODE) on the game UI
  // thread for one char. Bypasses the Chinese IME (no candidate window) and
  // delivers WM_CHAR directly to the focused game edit control. Requires the
  // game window to be foreground (SendInput target). id_lo = char (u16).
  LCMD_UNICODE_INPUT = 9,
};


enum LoginBridgeDialogCommand {
  LDC_NONE = 0,
  LDC_CONFIRM = 1,
};

enum LoginBridgeStatus {
  LST_IDLE = 0,
  LST_PENDING = 1,
  LST_OK = 2,
  LST_ERR = 3,
};

#pragma pack(push, 1)
struct LoginBridgeShared {
  volatile uint32_t magic;
  volatile uint32_t seq;
  volatile uint32_t cmd;
  volatile int32_t status;
  volatile int32_t ret;
  volatile uint32_t module_base;
  volatile uint32_t hwnd;
  volatile int32_t mode;      /* UI_CLICK: button hold ms; UI_KEY: hold_ms + 0x1000 no-focus */
  volatile uint32_t id_lo;    /* UI_CLICK: cx; UI_KEY: vk */
  volatile uint32_t id_hi;    /* UI_CLICK: cy; UI_KEY: action 0=down 1=up 2=press */
  volatile int32_t tid;       /* UI_CLICK: 0=left 1=right; UI_KEY: unused */
  char err[128];
  volatile uint32_t protocol_version;
  volatile uint32_t struct_size;
  volatile uint32_t ack_seq;
};
#pragma pack(pop)

static_assert(sizeof(LoginBridgeShared) == 184,
              "LoginBridgeShared protocol size mismatch");
