#pragma once
#include <stdint.h>
#include <stddef.h>

#define CHAT_TAP_MAGIC 0x50415443u
#define CHAT_TAP_VERSION 3u
#define CHAT_TAP_CAPACITY 50u
#define CHAT_TAP_TEXT_CHARS 256u
#define CHAT_TAP_TEAM_CAPACITY 512u
#define CHAT_TAP_PRIVATE_CAPACITY 512u

enum { CHAT_TAP_INIT = 0, CHAT_TAP_ACTIVE = 1, CHAT_TAP_ERROR = 2 };

#pragma pack(push, 1)
struct ChatTapEvent {
  volatile uint32_t seq;
  uint32_t tick_ms;
  uint32_t thread_id;
  uint32_t caller_va;
  uint32_t channel;
  uint32_t flags;
  uint32_t text_len;
  wchar_t text[CHAT_TAP_TEXT_CHARS];
};

struct ChatTapShared {
  volatile uint32_t magic;
  volatile uint32_t version;
  volatile uint32_t struct_size;
  volatile uint32_t capacity;
  volatile uint32_t write_seq;
  volatile uint32_t status;
  volatile uint32_t target_va;
  char error[128];
  ChatTapEvent events[CHAT_TAP_CAPACITY];
  volatile uint32_t team_write_seq;
  ChatTapEvent team_events[CHAT_TAP_TEAM_CAPACITY];
  // v3: 私聊专用 ring（channel==9）。主 ring 容量 50，战斗 ch=12 高频刷屏，
  // 私聊控制消息（组队前预检查/离队回执）会被挤掉；专用 ring 与队伍 ring 同理。
  volatile uint32_t private_write_seq;
  ChatTapEvent private_events[CHAT_TAP_PRIVATE_CAPACITY];
};
#pragma pack(pop)

static_assert(sizeof(ChatTapEvent) == 540, "ChatTapEvent layout mismatch");
static_assert(offsetof(ChatTapShared, team_write_seq) == 27156,
              "team ring offset mismatch");
static_assert(offsetof(ChatTapShared, private_write_seq) == 303640,
              "private ring offset mismatch");
static_assert(sizeof(ChatTapShared) == 580124, "ChatTapShared v3 size mismatch");
