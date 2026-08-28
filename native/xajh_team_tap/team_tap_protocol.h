#pragma once



#include <stdint.h>



#define TEAM_TAP_MAGIC 0x54544D50u /* 'TMTP' */

#define TEAM_TAP_VERSION 5u

#define TEAM_TAP_HITS 64u

#define TEAM_SEND_MAX_LEN 512u



enum TeamTapStatus {

  TEAM_TAP_INIT = 0,

  TEAM_TAP_ACTIVE = 1,

  TEAM_TAP_ERROR = 2,

};



// Send request mailbox: the Python side writes a chat c2s payload here and the

// in-game sender thread performs the thiscall wrapper send in-process.

enum ExactScanStatus {

  EXACT_SCAN_IDLE = 0,

  EXACT_SCAN_RUNNING = 1,

  EXACT_SCAN_FOUND = 2,

  EXACT_SCAN_MULTIPLE = 3,

  EXACT_SCAN_NOT_FOUND = 4,

};



enum TeamSendReqStatus {

  TEAM_SEND_IDLE = 0,

  TEAM_SEND_PENDING = 1,

  TEAM_SEND_DONE = 2,

  TEAM_SEND_ERR = 3,

};



#pragma pack(push, 1)

struct TeamTapHit {

  volatile uint32_t seq;

  uint32_t self;      // ecx (send manager candidate)

  uint32_t caller;    // return address

};



struct TeamSendReq {

  volatile uint32_t seq;      // request id (incremented by Python side)

  volatile uint32_t status;   // TeamSendReqStatus

  volatile uint32_t result;   // wrapper return value / error code

  uint32_t len;               // payload length

  uint8_t data[TEAM_SEND_MAX_LEN];

};



struct TeamTapShared {

  volatile uint32_t magic;

  volatile uint32_t version;

  volatile uint32_t struct_size;

  volatile uint32_t status;

  volatile uint32_t target_va;

  // Send manager (ecx) captured from the chat-encrypt wrapper 0x00D089B0.

  // Set once on the first captured hit; stable for the game session.

  volatile uint32_t send_mgr;

  volatile uint32_t send_mgr_seen;  // 1 once captured

  char error[128];

  // Recent hits ring (last TEAM_TAP_HITS calls), to inspect which caller

  // actually performs chat sends vs other systems sharing the wrapper.

  volatile uint32_t hit_write_seq;

  TeamTapHit hits[TEAM_TAP_HITS];

  // In-game sender mailbox (single slot; sender thread serializes).

  TeamSendReq send_req;

  // Last wrapper Octets snapshot (manual-send analysis): begin/end/len + bytes.

  volatile uint32_t dump_len;

  uint8_t dump[256];

  // Chat identity fingerprint bytes (packet offsets 7..9, 'xx xx 01').

  // 十丶三=2b 40 01, 初一=2b a0 01, 唐家军1=30 30 01. Captured on manual chat.

  volatile uint32_t send_ident0;

  volatile uint32_t send_ident1;

  volatile uint32_t send_ident2;

  volatile uint32_t send_ident_seen;

  // Exact object discovery (vtable + invariant fields), no chat hit needed.

  volatile uint32_t exact_scan_status;

  volatile uint32_t exact_scan_count;

  volatile uint32_t exact_send_mgr;

  volatile uint32_t exact_send_mgr_seen;

};

#pragma pack(pop)



static_assert(sizeof(TeamTapHit) == 12, "TeamTapHit layout mismatch");

static_assert(sizeof(TeamSendReq) == 16 + TEAM_SEND_MAX_LEN,

              "TeamSendReq layout mismatch");

static_assert(sizeof(TeamTapShared) == 156 + 4 + sizeof(TeamTapHit) * TEAM_TAP_HITS +

                  sizeof(TeamSendReq) + 4 + 256 + 16 + 16,

              "TeamTapShared layout mismatch");

