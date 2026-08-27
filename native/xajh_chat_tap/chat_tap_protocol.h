#pragma once

#include <stdint.h>

#define CHAT_TAP_MAGIC 0x50415443u /* 'CTAP' */
#define CHAT_TAP_VERSION 1u
#define CHAT_TAP_CAPACITY 50u
#define CHAT_TAP_TEXT_CHARS 256u

enum ChatTapStatus {
  CHAT_TAP_INIT = 0,
  CHAT_TAP_ACTIVE = 1,
  CHAT_TAP_ERROR = 2,
};

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
};
#pragma pack(pop)

static_assert(sizeof(ChatTapEvent) == 540, "ChatTapEvent layout mismatch");
static_assert(sizeof(ChatTapShared) == 27156, "ChatTapShared layout mismatch");
