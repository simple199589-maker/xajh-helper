# -*- coding: utf-8 -*-
"""
九层妖楼 full-auto enter loop (Fuzhou -> open 暗道 -> captcha -> enter).

Flow:
  0. Fuzhou + team/leader gate (memory)
  1. path / open 「九层妖楼暗道」 via super_loot (random among nearby gates)
  2. wait captcha via IsDlgShow; export crop (CaptureScreen/BitBlt)
  3. identify API -> select 2 cells (Img_Question 2x4 memory) + AQ_SUBMIT
  4. success if leave Fuzhou into 妖楼; else entry CD / fail sleep
  5. wait return Fuzhou, wander far, continue

Captcha interaction prefers human-like UI-thread bridge (minimized-safe):
  CMD_UI_CLICK cells with hold/jitter/gaps + optional slide trail, then Btn_Ok.
  Risk profile (2026-07-25 攻防): green-zone daily enter cap + mouse path entropy;
  dungeon-inside duration is intentionally not the main lever.

@author by ak
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.core.automove import read_scene_position
from app.core.captcha_client import (
    DEFAULT_CAPTCHA_API_KEY,
    DEFAULT_CAPTCHA_BASE_URL,
    CaptchaIdentifyResult,
    identify_image,
    send_feedback,
)
from app.core.game_attach import GameAttachSession
from app.core.map_names import format_scene_display, resolve_scene_id
from app.core.captcha_dialog import (
    TRAIN_DIALOG_H,
    TRAIN_DIALOG_W,
    DialogBox,
    clamp_dialog_confirm_point,
    crop_dialog_png,
    dialog_cell_point,
    dialog_confirm_point,
    find_captcha_dialog,
)
from app.core.dlg_image_export import export_dlg_image
from app.core.aui_click import (
    click_captcha_btn_ok,
    click_captcha_cells,
    click_client_bg,
    close_captcha_dialog,
    get_captcha_cell_click_point,
    get_captcha_cell_center,
    hide_aui_dialog,
    submit_activity_question,
)
from app.core.game_sys_msg import (
    CAPTCHA_FAIL_TEXT,
    CAPTCHA_OK_MSG_ID,
    CAPTCHA_OK_TEXT,
    ENTRY_OPEN_BLOCK_TEXT,
    SHENFA_BLOCK_TEXT,
    CaptchaAnswerFeedback,
    is_entry_open_block_feedback,
    is_explicit_captcha_ok_feedback,
    is_fresh_entry_open_block_feedback,
    is_shenfa_block_feedback,
    probe_entry_open_block_text,
    snapshot_entry_open_block_hits,
    snapshot_feedback_heap_baseline,
    wait_captcha_answer_feedback,
)
from app.core.human_input import human_delay_s, human_hold_ms, human_inter_click_gap_s, jitter_around
from app.core.plg_ui import (
    CAPTCHA_DIALOG_NAME_CANDIDATES,
    DlgShowResult,
    check_interact_gate,
    get_captcha_dlg_rect,
    get_game_ui_dlg,
    is_captcha_dialog_open,
    is_dlg_show,
    query_dlg_show,
    wait_dlg_show,
)
from app.core.runner import RunnerLifecycle, interruptible_sleep
from app.core.loot import (
    SuperLootConfig,
    SuperLootStepResult,
    SuperLootTarget,
    move_to_target,
    open_attach_session,
    read_host_cast_state,
    super_loot_step,
)

LogFn = Callable[[str], None]
StatusFn = Callable[[str], None]


def resolve_deferred_stage1_feedback(
    *,
    answer_likely_ok: bool,
    ans_fb: CaptchaAnswerFeedback | None,
) -> bool | None:
    """Resolve a deferred stage-1 fail after the final enter wait.

    ``True``/``False`` are final correctness verdicts; ``None`` means the
    evidence is inconclusive and must not create a negative sample.
    """
    if answer_likely_ok:
        return True
    if ans_fb is None:
        return None
    if ans_fb.kind == "fail":
        return False
    blob = f"{ans_fb.text or ''} {ans_fb.error or ''}"
    if "答案错误" in blob or "重新来过" in blob:
        return False
    return None

# Live-verified: Fuzhou city scene_id=68 (map x59).
# 九层妖楼 Inst SID / scene = 1529 (map a57).
FUZHOU_SCENE_IDS = frozenset({68})
YAOLU_SCENE_IDS = frozenset({1529})
FUZHOU_NAME_KEYS = ("福州",)
YAOLU_NAME_KEYS = ("妖楼",)
ENTRY_ITEM_NAME = "九层妖楼暗道"
# Matter-interact 0% bar UI (live 2026-07-20): Progress2.xml / Win_Prgs2.
# MagicProgress1..6 are sibling slots; scan all when clearing.
INTERACT_PROGRESS_DLG_NAMES: tuple[str, ...] = (
    "Win_Prgs2",
    "MagicProgress1",
    "MagicProgress2",
    "MagicProgress3",
    "MagicProgress4",
    "Win_MagicProgress5",
    "MagicProgress6",
)
ENTRY_PROGRESS_TEXT_KEYS: tuple[str, ...] = (
    "九层妖楼暗道",
    "妖楼暗道",
    "暗道",
)
# AUIDialog::IsShow reads byte at this+0x94; Show(bool) is vtable+0x1C.
AUI_DLG_ISSHOW_OFF = 0x94
AUI_DLG_SHOW_VTABLE_OFF = 0x1C
# Live Txt_Name wide-string pointer offs (Win_Prgs2 verified +0xB8).
AUI_PROGRESS_TXT_STR_OFFS: tuple[int, ...] = (0xB8, 0xBC, 0xC0, 0xB0, 0xA8, 0xC4)
# Preferred-image-base note VAs (rebase via module_base - 0x400000).
# 0x8C6A70: free fn -> game UI mgr this*
# 0x87C890(this=mgr): if Win_Prgs* shown -> CancelAction 0xCC7010(1,0) then Show(0,0,1)
NOTE_VA_GET_UI_MGR = 0x8C6A70
NOTE_VA_CANCEL_PROGRESS_UI = 0x87C890
NOTE_VA_GLOBAL_ROOT = 0x15282D8  # used by pure-read mgr fallback
# UI mgr progress dialog slots (PE 0x87C890): +0x3C4 Win_Prgs2 primary.
UI_MGR_PROGRESS_SLOT_OFFS: tuple[int, ...] = (0x3C4, 0x3C8, 0x3CC, 0x3D0, 0x3D8)
DEFAULT_IMAGE_BASE = 0x400000
# Host perform state that DRIVES Win_Prgs2 (PE progress tick 0x8862E0).
# host = *(*(*0x15282D8+0x24)+0x8C); perform_mgr = *(host+0x270);
# curr = *(perform_mgr+8); type=*(curr+4); subtype=*(curr+0x20).
HOST_OFF_PERFORM_MGR = 0x270
PERFORM_MGR_OFF_CURR = 0x8
PERFORM_MGR_OFF_SIDE = 0xC
PERFORM_OFF_TYPE = 0x4
PERFORM_OFF_SUBTYPE = 0x20
PERFORM_SIDE_OFF_FLAG0 = 0x40
PERFORM_SIDE_OFF_FLAG1 = 0x41
# HostStopSession stop path: thiscall perform_mgr.vt+0x10(0x65)
PERFORM_STOP_VT_OFF = 0x10
PERFORM_STOP_ARG = 0x65
# perform_mgr.vt+0x1C(type, arg) installs a new curr perform (PE 0x79E480).
# Live ctor path uses push 0; push 2; call vt+0x1C  => (type=2, arg=0).
PERFORM_MGR_VT_INSTALL_OFF = 0x1C
PERFORM_MGR_VT_FACTORY_OFF = 0x20
# Matter perform object (type 0x66..0x6b, live 103=0x67):
#   vt+0x0C(arg=1) -> 0x79E650 -> vt+0x6C(0,1,-1,0xC8) unlock/end
PERFORM_OBJ_VT_END_OFF = 0x0C
PERFORM_OBJ_VT_UNLOCK_OFF = 0x6C
HOST_OFF_SESSION_GATE = 0x41C  # CancelSession gate / interact session
HOST_OFF_MOVE_CTRL = 0x480
PERFORM_MGR_OFF_ENTITY = 0x4D4
ENTITY_VT_MOVE_UNLOCK_OFF = 0x2CC
NOTE_VA_GET_HOST = 0x4AE400
# HostStopSession branch for matter perform type 0x67: thiscall 0x772B60(perform)
NOTE_VA_STOP_PERFORM_MATTER = 0x772B60
NOTE_VA_CANCEL_ACTION = 0xCC7010
NOTE_VA_GET_CANCEL_CTX = 0x4AE440  # global+0x2C; CancelAction this = eax+0x1C8
# Base locomotion perform. Live healthy chars almost always hold type=2 here.
# Bare *(mgr+8)=0 removes it and BREAKS HostMove/click-to-move (verified 2026-07-20).
PERFORM_TYPE_BASE_LOCOMOTION = 2
# Matter interact family that drives Win_Prgs2 / entry wait.
PERFORM_TYPES_MATTER = frozenset(range(0x66, 0x6C))  # 102..107
# Sustain hide against ~20ms flash-back after bare Show(false).
CLEAR_BAR_SUSTAIN_S = 1.6
CLEAR_BAR_HIDE_INTERVAL_S = 0.08
CLEAR_BAR_VERIFY_DELAYS_S: tuple[float, ...] = (0.25, 0.55)
ENTRY_ITEM_TID = 81184
# Fuzhou stand at 妖楼暗道 (user live 2026-07-19); cross-map HostMove target.
DEFAULT_ENTRY_ANCHOR = (35.70, 55.30, -79.80)
# HostMove mode for cross-map path to 暗道 (Fuzhou scene_id).
ENTRY_PATH_SCENE_ID = 68

@dataclass
class YaoluConfig:
    """九层妖楼 auto parameters. @author by ak"""

    entry_name: str = ENTRY_ITEM_NAME
    pick_range: float = 6.0
    scan_radius: float = 80.0
    # Fuzhou stand often has ~4 九层妖楼暗道; randomize among nearby matches
    # so we do not always MatterInteract the same local-nearest entity.
    entry_select_random: bool = True
    # XZ radius (m) for random entry pool; covers the 4-gate cluster.
    entry_select_radius: float = 35.0
    cast_wait_s: float = 6.5
    # The entry must prove a cast transition in memory before waiting captcha.
    entry_cast_start_timeout_s: float = 3.0
    # Wait dialog after open_entry interaction (cast bar + server dialog).
    open_dialog_wait_s: float = 20.0
    captcha_wait_s: float = 24.0
    # urllib connect/read timeout for one identify POST.
    captcha_api_timeout_s: float = 45.0
    captcha_poll_s: float = 1.0
    # One bounded post-confirm budget: feedback + hard block + scene transfer.
    enter_timeout_s: float = 18.0
    # After hist 答案错误 / enter timeout: always enter grace, watch scene only
    # (false message possible; do NOT depend on cast bar/progress).
    # Still not in 妖楼 after grace -> real fail + recover.
    enter_fail_grace_s: float = 14.0
    # Poll interval while waiting back to Fuzhou (was 2s; too chatty).
    wait_return_poll_s: float = 15.0
    loop_idle_s: float = 1.5
    wander_min_dist: float = 18.0
    wander_max_dist: float = 35.0
    # A random move is part of the round transition. Do not start a new
    # full flow until live coordinates confirm that the move has completed.
    wander_arrive_radius: float = 4.0
    wander_arrive_timeout_s: float = 45.0
    wander_poll_s: float = 0.4
    wander_stop_delta_m: float = 0.25
    wander_stop_hits: int = 2
    # If live coordinates stop changing before reaching the point, abandon the
    # current random point instead of spending the whole arrival timeout.
    wander_stuck_timeout_s: float = 8.0
    captcha_base_url: str = DEFAULT_CAPTCHA_BASE_URL
    captcha_api_key: str = DEFAULT_CAPTCHA_API_KEY
    # Issued by /api/license/verify; never persisted with Yaolu preferences.
    login_token: str = ""
    # Submit borderline samples so the final answer verdict can train the
    # identify service; predictions below 25% are too noisy to be useful.
    min_confidence: float = 0.25
    use_bridge: bool = True
    # After success, wait back to Fuzhou before next open.
    wait_return_fuzhou_s: float = 600.0
    # Prefer PostMessage clicks (no cursor); fallback real mouse.
    prefer_post_click: bool = True
    # Minimum dialog detector score to treat as opened (stricter).
    dialog_min_score: float = 1.90
    # After MatterInteract, short settle before first memory poll (cast starts).
    # Open verification is plg IsDlgShow, not this timer.
    post_open_settle_s: float = 0.8
    # Prefer plg GetGameUIDlg + IsDlgShow over screenshot timers.
    use_memory_dialog: bool = True
    # Prefer game CaptureScreen / BitBlt over PrintWindow (hangs).
    use_game_capture: bool = True
    # 九层妖楼暗道: must be in party AND be team leader (memory CECTeam).
    require_team_leader: bool = True
    # Legacy inverted flag (reject if in team). Keep False for 妖楼 entry.
    require_solo: bool = False
    # Dialog name candidates for CDlgActivityQuestion (filled at runtime if empty).
    captcha_dlg_names: tuple[str, ...] = CAPTCHA_DIALOG_NAME_CANDIDATES
    # Short random sleep after non-answer enter miss (timeout / unknown).
    fail_sleep_min_s: float = 3.0
    fail_sleep_max_s: float = 10.0
    # Entry object CD after failed open / wrong captcha (live ~10-15s).
    entry_cd_s: float = 12.0
    entry_cd_jitter_s: float = 3.0
    # Hourly blackout (anti 整点刷): only open before :55 and after :05 (local).
    # i.e. blackout [:55:00, next :05:00).
    entry_blackout_enabled: bool = True
    entry_blackout_start_min: int = 55
    entry_blackout_end_min: int = 5
    # Legacy alias kept for old settings/tests; ignored when end_min is set.
    entry_blackout_end_sec: int = 0
    # Stop full-auto on 神罚 team block (do not retry open).
    stop_on_shenfa: bool = True
    # ---- Risk pacing: entry/captcha focused (not dungeon-inside duration) ----
    # User-facing pacing constraint. 80 reproduces the current recommended
    # defaults exactly; lower values reduce waits, higher values add margin.
    risk_constraint_pct: int = 80
    # Profile id for audit / ban postmortem (see YAOLU_RISK_BASELINE_PRE_20260722).
    # 2026-07-25 攻防：实锤日频约 30~40 成功 → 神罚 7 天；绿区约 ≤10。
    profile_id: str = "entry_focus_v2"
    profile_note: str = "攻防20260725：绿区日限+轨迹点选；本内时长不硬顶"
    # Daily successful enters cap (local calendar day). 0 = unlimited.
    # Default 20: user-facing soft cap (server green≤10 / yellow 10~25; 30+ 高危).
    daily_success_limit: int = 20
    # After leave dungeon / back to Fuzhou: inter-round rest (not in-dungeon F4).
    success_rest_min_s: float = 25.0
    success_rest_max_s: float = 90.0
    # Extra rest after wrong captcha (stacked with entry object CD).
    answer_fail_rest_min_s: float = 60.0
    answer_fail_rest_max_s: float = 180.0
    # Consecutive wrong answers in one run: long pause then continue (or stop).
    consecutive_answer_fail_limit: int = 3
    consecutive_answer_fail_pause_s: float = 300.0
    consecutive_answer_fail_stop: bool = False
    # After 神罚: refuse re-start for the rest of the local calendar day.
    shenfa_lock_day: bool = True
    # Persist daily ok / 神罚 lock under logs/ (survives process restart same day).
    risk_state_enabled: bool = True
    # Append-only parameter/event audit under logs/yaolu_risk_audit.jsonl.
    risk_audit_enabled: bool = True
    # Reject captcha submit when confidence below min_confidence.
    captcha_reject_low_confidence: bool = True
    # Delay between captcha cell clicks / before confirm (seconds).
    # Wider than pre-20260725 metronome (~0.28-0.63) to lower click-density score.
    captcha_click_gap_s: float = 0.45
    # Extra random gap range after base captcha_click_gap_s (human think/move).
    captcha_click_gap_jitter_s: float = 0.90
    # Mouse-down hold on each captcha cell / confirm (ms).
    captcha_hold_min_ms: int = 60
    captcha_hold_max_ms: int = 220
    # Pause after identify returns, before first cell (read/think).
    captcha_pre_first_click_min_s: float = 0.35
    captcha_pre_first_click_max_s: float = 1.20
    # Pause after last cell before confirm (min/max seconds).
    captcha_pre_confirm_min_s: float = 0.45
    captcha_pre_confirm_max_s: float = 1.40
    # Prefer Btn_Ok UI click (selection state) over pure AQ_SUBMIT thiscall.
    captcha_prefer_btn_click: bool = True
    # Jitter click points inside cells (avoid perfect centers).
    captcha_humanize_clicks: bool = True
    # Bezier slide trail between successive captcha clicks (background mousemove).
    captcha_slide_enabled: bool = True
    # Simulate human noise during slow identify (~12s): randomly select one animal then deselect.
    # Must finish before real result arrives. If real arrives early, skip.
    captcha_fake_noise_enabled: bool = True
    captcha_fake_noise_prob: float = 0.65
    captcha_fake_noise_min_s: float = 1.2
    captcha_fake_noise_max_s: float = 4.5
    captcha_fake_noise_approach: bool = True
    captcha_slide_min_steps: int = 8
    captcha_slide_max_steps: int = 22
    captcha_slide_duration_min_s: float = 0.25
    captcha_slide_duration_max_s: float = 0.85
    # First-cell approach: synthesize a nearby path_from so first click is not teleport.
    captcha_approach_slide: bool = True
    # Approach radius for first cell and any missing path_from (force path, no teleport).
    captcha_approach_radius_min: int = 35
    captcha_approach_radius_max: int = 85
    # Real OS mouse for captcha (off by default — inject/bridge only + timing).
    # True: SetCursorPos + mouse_event (needs game visible; can break enter).
    allow_cursor_click: bool = False
    # Prefer real mouse over bridge/post for captcha cell/confirm clicks.
    captcha_prefer_real_mouse: bool = False
    # Optional foreground-only calibration. Live physical cursor verification
    # shows Win_Question3D's derived Btn_Ok rect already matches the hit area.
    captcha_real_mouse_confirm_y_offset_px: int = 0
    # Close mini-game (Btn_Cancel/Esc) before cancel-session when dialog stuck.
    captcha_close_on_fail: bool = True
    captcha_close_attempts: int = 4
    captcha_close_settle_s: float = 0.45
    # Save crops under captures/captcha only when login「开启调试」is on.
    # When False: PNG stays in memory for identify, then released (no disk write).
    debug_save_crops: bool = False
    # UI「调试」: skip identify/feedback API; pick 2 random cells to exercise fail path.
    debug_random_answer: bool = False
    # Debug crop TTL (seconds); 0 = delete immediately after captcha attempt.
    debug_crop_ttl_s: float = 120.0
    # Max identify/export attempts per dialog open (2 = one re-crop retry on fail/timeout).
    captcha_identify_max_attempts: int = 2
    # Retry an already-answered entry without another captcha (free-entry heartbeat).
    # Delay: base * growth^attempt, then ±jitter (humanized). Stop after total wait ~30min.
    entry_reopen_base_s: float = 30.0
    entry_reopen_growth: float = 1.5  # softer than historical 2.0 / interim 1.6
    entry_reopen_jitter: float = 0.18  # ±18% random on each scheduled delay
    entry_reopen_max_total_s: float = 1800.0  # half-hour cumulative planned wait cap
    # Captcha identify API consecutive failures before longer cool-down.
    captcha_api_fail_max_consecutive: int = 5
    captcha_api_fail_cooldown_s: float = 45.0
    # Bridge CancelSession (opcode 0x21); force even when host+0x41C is 0.
    cancel_session_wait_s: float = 4.0
    cancel_session_poll_s: float = 0.12
    cancel_session_idle_hits: int = 2
    cancel_session_force: bool = True
    # After wrong answer: 0x21 pulses + Esc (matter interact bar) + HostMove.
    cancel_session_pulses: int = 3
    cancel_nudge_after: bool = True
    # Matter interact 0% bar is not skill cast; Esc cancels client interact UI.
    cancel_use_esc: bool = False  # PostMessage keys ineffective; re-enable when verified
    cancel_esc_presses: int = 3
    cancel_esc_hold_ms: int = 50
    cancel_nudge_dist: float = 6.5
    # Path to entry uses Fuzhou scene_id (cross-map ok). 0 = live scene only.
    entry_path_scene_id: int = ENTRY_PATH_SCENE_ID
    entry_path_arrive_radius: float = 8.0
    entry_path_timeout_s: float = 180.0
    entry_path_nudge_s: float = 6.0
    # Path to known entry coords before super_loot scan (far / no entity).
    prepath_to_entry: bool = True
    prepath_min_dist: float = 12.0
    # After same-map prepath arrival (already in 福州): short settle only.
    # 不稳定状态 does NOT apply when already in Fuzhou — do not burn 20s here.
    post_arrive_open_delay_s: float = 1.2
    # Only after real scene transfer (出本/跨图进入福州) wait before open.
    post_map_settle_s: float = 20.0
    # Soft toast that means client is still loading after map change.
    unstable_state_text: str = "不稳定状态无法进行此操作"
    # After MatterInteract, re-open same entity if captcha dialog never appears.
    open_dialog_retries: int = 2

    def entry_cd_sleep_s(self) -> float:
        """
        Randomized wait for entry object CD (~12s + jitter).

        @author by ak
        """
        base = max(0.0, float(self.entry_cd_s))
        jit = max(0.0, float(self.entry_cd_jitter_s))
        if jit <= 0:
            return base
        return base + random.uniform(0.0, jit)

    def success_rest_sleep_s(self) -> float:
        """Random rest after a successful dungeon leave."""
        lo = max(0.0, float(getattr(self, "success_rest_min_s", 0.0) or 0.0))
        hi = max(lo, float(getattr(self, "success_rest_max_s", lo) or lo))
        return random.uniform(lo, hi) if hi > 0 else 0.0

    def answer_fail_rest_sleep_s(self) -> float:
        """Extra rest after wrong captcha answer (plus entry CD)."""
        lo = max(0.0, float(getattr(self, "answer_fail_rest_min_s", 0.0) or 0.0))
        hi = max(lo, float(getattr(self, "answer_fail_rest_max_s", lo) or lo))
        return random.uniform(lo, hi) if hi > 0 else 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def entry_blackout_remaining_s(
    cfg: YaoluConfig | None = None,
    *,
    now=None,
) -> float:
    """
    Seconds until entry is usable again during hourly risk window.

    Default (local): blackout from :55:00 until next hour :05:00
    (only open before :55 and after :05). Returns 0 outside blackout.

    now: optional datetime for tests.
    @author by ak
    """
    import datetime as _dt

    c = cfg or YaoluConfig()
    if not bool(c.entry_blackout_enabled):
        return 0.0
    now = now or _dt.datetime.now()
    start_min = max(0, min(59, int(getattr(c, "entry_blackout_start_min", 55) or 55)))
    raw_end = getattr(c, "entry_blackout_end_min", 5)
    try:
        end_min = int(5 if raw_end is None else raw_end)
    except Exception:
        end_min = 5
    # Backward compat: old configs used end_sec only (next hour + seconds).
    end_sec = max(0, min(59, int(getattr(c, "entry_blackout_end_sec", 0) or 0)))
    if end_min <= 0 and end_sec > 0:
        end_min = 0
    end_min = max(0, min(59, end_min))

    # In blackout if minute >= start_min OR minute < end_min
    if now.minute >= start_min:
        end = (now + _dt.timedelta(hours=1)).replace(
            minute=0, second=0, microsecond=0
        ) + _dt.timedelta(minutes=end_min, seconds=end_sec if end_min == 0 else 0)
        return max(0.0, (end - now).total_seconds())
    if now.minute < end_min:
        end = now.replace(minute=0, second=0, microsecond=0) + _dt.timedelta(
            minutes=end_min
        )
        return max(0.0, (end - now).total_seconds())
    if end_min == 0 and end_sec > 0 and now.minute == 0 and now.second < end_sec:
        end = now.replace(minute=0, second=0, microsecond=0) + _dt.timedelta(
            seconds=end_sec
        )
        return max(0.0, (end - now).total_seconds())
    return 0.0


def is_entry_blackout(cfg: YaoluConfig | None = None) -> bool:
    """
    True when 九层妖楼暗道 is expected to be missing (hourly window).

    @author by ak
    """
    return entry_blackout_remaining_s(cfg) > 0.05


def _local_day_key() -> str:
    import datetime as _dt

    return _dt.datetime.now().strftime("%Y-%m-%d")


def _yaolu_log_dir() -> Path:
    """Resolve logs dir: XAJH_LOG_DIR if set, else cwd/logs."""
    raw = str(os.environ.get("XAJH_LOG_DIR") or "").strip()
    base = Path(raw).expanduser() if raw else (Path.cwd() / "logs")
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        base = Path.cwd()
    return base


def yaolu_risk_state_path() -> Path:
    """
    Persist daily enter-ok / 神罚 lock under logs/.

    Prefer process cwd/logs (same place frozen helper writes day logs).
    """
    return _yaolu_log_dir() / "yaolu_risk_state.json"


def load_yaolu_risk_state() -> dict:
    path = yaolu_risk_state_path()
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_yaolu_risk_state(state: dict) -> None:
    path = yaolu_risk_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass



def yaolu_risk_pid_bucket(state: dict, pid: int) -> dict:
    """Return mutable bucket for pid under today's key; prunes other days."""
    day = _local_day_key()
    # Keep only today to avoid unbounded growth.
    out = {day: dict(state.get(day) or {})} if isinstance(state.get(day), dict) else {day: {}}
    day_map = out[day]
    key = str(int(pid))
    bucket = day_map.get(key)
    if not isinstance(bucket, dict):
        bucket = {"ok": 0, "shenfa_lock": False}
        day_map[key] = bucket
    bucket.setdefault("ok", 0)
    bucket.setdefault("shenfa_lock", False)
    return bucket


# Frozen pre-2026-07-22 risk-related defaults for ban postmortem diffs.
# Keep permanently — do not "update" this to match new defaults.
YAOLU_RISK_BASELINE_PRE_20260722: dict[str, Any] = {
    "min_confidence": 0.15,
    "daily_success_limit": 0,
    "success_rest_min_s": 0.0,
    "success_rest_max_s": 0.0,
    "answer_fail_rest_min_s": 0.0,
    "answer_fail_rest_max_s": 0.0,
    "consecutive_answer_fail_limit": 0,
    "consecutive_answer_fail_pause_s": 0.0,
    "consecutive_answer_fail_stop": False,
    "shenfa_lock_day": False,
    "risk_state_enabled": False,
    "risk_audit_enabled": False,
    "captcha_reject_low_confidence": False,
    "entry_reopen_base_s": 30.0,
    "entry_reopen_growth": 2.0,
    "entry_reopen_jitter": 0.18,
    "entry_reopen_max_total_s": 1800.0,
    "entry_cd_s": 12.0,
    "entry_cd_jitter_s": 3.0,
    "captcha_click_gap_s": 0.18,
    "captcha_click_gap_jitter_s": 0.12,
    "captcha_hold_min_ms": 48,
    "captcha_hold_max_ms": 115,
    "captcha_pre_confirm_min_s": 0.12,
    "captcha_pre_confirm_max_s": 0.28,
    "captcha_prefer_btn_click": True,
    "captcha_humanize_clicks": True,
    "captcha_slide_enabled": False,
    "captcha_slide_min_steps": 0,
    "captcha_slide_max_steps": 0,
    "captcha_slide_duration_min_s": 0.0,
    "captcha_slide_duration_max_s": 0.0,
    "stop_on_shenfa": True,
    "profile_id": "baseline_pre_20260722",
    "profile_note": "改版前：无日锁/无答错加长冷却/心跳growth=2.0/min_conf=0.15",
}

YAOLU_RISK_CFG_KEYS: tuple[str, ...] = tuple(YAOLU_RISK_BASELINE_PRE_20260722.keys()) + (
    "risk_constraint_pct",
    "enter_timeout_s",
    "enter_fail_grace_s",
    "fail_sleep_min_s",
    "fail_sleep_max_s",
    "entry_blackout_start_min",
    "entry_blackout_end_min",
    "entry_blackout_end_sec",
    "post_map_settle_s",
    "cancel_session_wait_s",
)
YAOLU_RISK_AUDIT_SCHEMA = 1


def yaolu_risk_audit_path() -> Path:
    """Append-only jsonl next to day logs / risk state."""
    return _yaolu_log_dir() / "yaolu_risk_audit.jsonl"


def snapshot_yaolu_risk_cfg(cfg: Any) -> dict[str, Any]:
    """Risk/entry/captcha knobs only; never include captcha_api_key."""
    out: dict[str, Any] = {}
    for key in YAOLU_RISK_CFG_KEYS:
        if hasattr(cfg, key):
            val = getattr(cfg, key)
        else:
            val = YAOLU_RISK_BASELINE_PRE_20260722.get(key)
        # Normalize numbers for stable JSON / diff.
        if isinstance(val, float):
            out[key] = float(val)
        elif isinstance(val, bool):
            out[key] = bool(val)
        elif isinstance(val, int):
            out[key] = int(val)
        else:
            out[key] = val
    return out


def diff_yaolu_risk_vs_baseline(cfg_risk: dict[str, Any]) -> dict[str, Any]:
    """Return {key: {"old": baseline, "new": current}} for changed keys."""
    diff: dict[str, Any] = {}
    for key, old in YAOLU_RISK_BASELINE_PRE_20260722.items():
        new = cfg_risk.get(key, old)
        if new != old:
            diff[key] = {"old": old, "new": new}
    return diff


def append_yaolu_risk_audit(record: dict[str, Any]) -> None:
    """Append one JSON object line; best-effort, never raises to caller."""
    try:
        path = yaolu_risk_audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass






@dataclass
class DialogCapture:
    """
    One detected captcha dialog crop.

    @author by ak
    """

    ok: bool
    box: DialogBox | None = None
    png: bytes | None = None
    client_w: int = 0
    client_h: int = 0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "box": self.box.to_dict() if self.box else None,
            "client_w": self.client_w,
            "client_h": self.client_h,
            "png_len": len(self.png or b""),
            "error": self.error,
        }


@dataclass
class YaoluStepEvent:
    """UI / log event from runner. @author by ak"""

    phase: str
    message: str
    ok: bool = True
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_yaolu_risk_constraint(value, default: int = 80) -> int:
    """Clamp a user-facing risk pacing percentage to 1..100."""
    try:
        pct = int(float(str(value).strip().rstrip("%")))
    except (TypeError, ValueError):
        pct = int(default)
    return max(1, min(100, pct))


def _risk_constraint_value(
    pct: int,
    fast: float,
    current: float,
    cautious: float,
) -> float:
    """Piecewise interpolation with the current profile anchored at 80%."""
    p = normalize_yaolu_risk_constraint(pct)
    if p <= 80:
        ratio = (p - 1) / 79.0
        return float(fast) + (float(current) - float(fast)) * ratio
    ratio = (p - 80) / 20.0
    return float(current) + (float(cautious) - float(current)) * ratio


def apply_yaolu_risk_constraint(
    cfg: YaoluConfig,
    percent: int | str,
) -> YaoluConfig:
    """Apply the selected pacing profile without weakening hard safety gates.

    80% is byte-for-byte equivalent to today's recommended wait values. 1%
    approaches the original fast cadence while retaining daily limits, low
    confidence rejection, 神罚 lock, exact feedback handling and all input
    correctness checks.
    """
    pct = normalize_yaolu_risk_constraint(percent)
    cfg.risk_constraint_pct = pct

    def setf(name: str, fast: float, current: float, cautious: float) -> None:
        setattr(
            cfg,
            name,
            _risk_constraint_value(pct, fast, current, cautious),
        )

    # Wait for feedback / transfer; exact tap verdicts still short-circuit.
    setf("enter_timeout_s", 12.0, 18.0, 24.0)
    setf("enter_fail_grace_s", 3.0, 14.0, 18.0)

    # General failure/recovery cadence.
    setf("fail_sleep_min_s", 0.5, 3.0, 5.0)
    setf("fail_sleep_max_s", 2.0, 10.0, 15.0)
    setf("entry_cd_s", 8.0, 12.0, 15.0)
    setf("entry_cd_jitter_s", 1.0, 3.0, 4.0)
    setf("post_arrive_open_delay_s", 0.3, 1.2, 2.0)
    # Scene-transfer stabilization is a game-side fixed gate. Keep 20s as the
    # hard floor even for fast pacing; higher constraints may add margin.
    setf("post_map_settle_s", 20.0, 20.0, 30.0)
    setf("cancel_session_wait_s", 2.0, 4.0, 5.0)
    setf("loop_idle_s", 0.3, 1.5, 2.0)

    # Answer-correct reopen heartbeat and service recovery.
    setf("entry_reopen_base_s", 5.0, 30.0, 45.0)
    setf("entry_reopen_growth", 1.15, 1.5, 1.65)
    setf("captcha_api_fail_cooldown_s", 8.0, 45.0, 60.0)

    # Successful dungeon cycles keep the original 80% cooldown as their floor.
    # Higher constraints may raise the lower bound, but one rest caps at 90s.
    setf("success_rest_min_s", 25.0, 25.0, 40.0)
    setf("success_rest_max_s", 90.0, 90.0, 90.0)

    # Wrong-answer pacing remains adjustable. Hard correctness and stop rules remain.
    setf("answer_fail_rest_min_s", 0.0, 60.0, 90.0)
    setf("answer_fail_rest_max_s", 0.0, 180.0, 300.0)
    setf("consecutive_answer_fail_pause_s", 15.0, 300.0, 420.0)

    # Hourly blackout shrinks toward the original :58 -> :00:15 window and
    # expands to :54 -> :06 at 100%. The gate itself is never removed.
    if pct == 1:
        cfg.entry_blackout_start_min = 58
        cfg.entry_blackout_end_min = 0
        cfg.entry_blackout_end_sec = 15
    else:
        before = _risk_constraint_value(pct, 2.0, 5.0, 6.0)
        after = _risk_constraint_value(pct, 0.25, 5.0, 6.0)
        cfg.entry_blackout_start_min = max(0, min(59, 60 - int(round(before))))
        cfg.entry_blackout_end_min = max(0, min(59, int(round(after))))
        cfg.entry_blackout_end_sec = 0

    base_profile = (
        "entry_focus_v2_fg"
        if bool(cfg.captcha_prefer_real_mouse)
        else "entry_focus_v2"
    )
    if pct == 80:
        cfg.profile_id = base_profile
    else:
        cfg.profile_id = f"{base_profile}_r{pct}"
    cfg.profile_note = (
        f"用户风控约束 {pct}%：80%=当前推荐；越低等待越短；"
        "日限/神罚锁/低置信拒答等硬约束保持不变"
    )
    return cfg


def yaolu_start_credential_error(cfg: YaoluConfig) -> tuple[str, str] | None:
    """Return the blocking API-key error for a normal captcha run."""
    if bool(getattr(cfg, "debug_random_answer", False)):
        return None
    key = str(getattr(cfg, "captcha_api_key", "") or "").strip()
    if not key:
        return (
            "missing_captcha_api_key",
            "未配置答题专用 Key，请先在「快捷设置」中填写并保存",
        )
    from app.core.captcha_client import api_key_header_error

    key_error = api_key_header_error(key)
    if key_error:
        return "invalid_captcha_api_key", key_error
    return None


def _dist_xz(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[2] - b[2]) ** 2)


def describe_captcha_error(error: str | None, cfg: YaoluConfig) -> str:
    """Convert captcha API failures into a status line suitable for players."""
    raw = str(error or "未知错误").strip()
    low = raw.lower()
    if "timed out" in low or "timeout" in low:
        return f"识别服务请求超时（等待 {float(cfg.captcha_api_timeout_s):.0f} 秒未返回）"
    if "connection refused" in low or "urlopen error" in low:
        return "识别服务连接失败（请检查识别服务与网络）"
    if "missing api key" in low:
        return "未配置识别密钥"
    if "empty response" in low:
        return "识别服务返回空数据"
    if "no positions" in low or "no click points" in low:
        return "识别服务未给出可点击的验证码位置"
    if "500" in raw or "http 500" in low or "http=500" in low:
        return "识别服务异常（HTTP 500），请稍后重试"
    if "识别失败" in raw and len(raw) <= 20:
        return "识别服务返回失败，请稍后重试"
    if "http error" in low:
        return f"识别失败：{raw}"
    return f"识别失败：{raw}"


def is_fuzhou_scene(scene_id: int | None, name: str | None = None) -> bool:
    """True if current map is Fuzhou city. @author by ak"""
    if scene_id is not None and int(scene_id) in FUZHOU_SCENE_IDS:
        return True
    n = name or ""
    if scene_id is not None:
        _mid, cn = resolve_scene_id(scene_id)
        n = cn or n
    return any(k in (n or "") for k in FUZHOU_NAME_KEYS)


def is_yaolu_scene(scene_id: int | None, name: str | None = None) -> bool:
    """True if current map is 九层妖楼. @author by ak"""
    if scene_id is not None and int(scene_id) in YAOLU_SCENE_IDS:
        return True
    n = name or ""
    if scene_id is not None:
        mid, cn = resolve_scene_id(scene_id)
        n = (cn or "") + " " + (mid or "") + " " + n
    return any(k in n for k in YAOLU_NAME_KEYS) or ("a57" in n.lower())


def read_scene_state(
    session: GameAttachSession,
    *,
    fresh: bool = False,
    log: LogFn | None = None,
) -> tuple[int | None, tuple[float, float, float] | None, str]:
    """
    Return (scene_id, pos, display_name).

    fresh=False: 非马上需要，优先 state/hub 短缓存。
    fresh=True: 跨图/开门后校验等必须时效，直读并回写缓存。

    @author by ak
    """
    log = log or (lambda _m: None)
    if not fresh:
        try:
            from app.core.state_dispatch import StateKind, get_state

            sc = get_state(session, StateKind.SCENE, fresh=False, log=lambda _m: None)
            if isinstance(sc, dict) and (
                sc.get("scene_id") is not None or sc.get("pos") is not None
            ):
                sid = sc.get("scene_id")
                try:
                    sid_i = int(sid) if sid is not None else None
                except Exception:
                    sid_i = None
                pos = sc.get("pos")
                pos_t = None
                if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                    pos_t = (float(pos[0]), float(pos[1]), float(pos[2]))
                label = str(sc.get("scene_label") or "").strip()
                if not label and sid_i is not None:
                    label = format_scene_display(sid_i) if sid_i is not None else "-"
                return sid_i, pos_t, label or "-"
        except Exception:
            pass
    sp = read_scene_position(session, log=log)
    if not sp.ok:
        return None, None, "-"
    sid = int(sp.scene_id) if sp.scene_id is not None else None
    pos = sp.scene_pos
    label = format_scene_display(sid) if sid is not None else "-"
    if fresh:
        try:
            from app.core.state_dispatch import StateKind, invalidate_states
            from app.core.live_scene_hub import publish_live_scene

            invalidate_states(session, StateKind.SCENE, StateKind.POS)
            pos_t = None
            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                pos_t = (float(pos[0]), float(pos[1]), float(pos[2]))
            publish_live_scene(
                int(getattr(session, "pid", 0) or 0),
                scene_id=sid,
                scene_label=label if label != "-" else None,
                pos=pos_t,
                source="yaolu_scene",
            )
        except Exception:
            pass
    return sid, pos, label


def _sleep_interruptible(
    seconds: float,
    stop_event: threading.Event | None,
    *,
    step: float = 0.2,
) -> bool:
    """
    Sleep up to seconds; return False if stopped.

    @author by ak
    """
    return interruptible_sleep(seconds, stop_event, interval=step)



def _sleep_with_status_countdown(
    seconds: float,
    stop_event: threading.Event | None,
    *,
    on_tick: Callable[[str], None] | None = None,
    prefix: str = "等待中",
    reason: str = "",
    phase_log: LogFn | None = None,
) -> bool:
    """
    Interruptible sleep with per-second status text for long waits.

    Returns False if stop_event fired.

    @author by ak
    """
    total = max(0.0, float(seconds))
    if total <= 0.05:
        return True
    end = time.monotonic() + total
    last_shown = -1
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        remain = end - time.monotonic()
        if remain <= 0:
            if on_tick is not None:
                done = f"{prefix}完成"
                if reason:
                    done = f"{prefix}完成（{reason}）"
                on_tick(done)
            return True
        sec = int(math.ceil(remain))
        if sec != last_shown:
            last_shown = sec
            msg = f"{prefix}，剩余 {sec}s"
            if reason:
                msg = f"{prefix}，剩余 {sec}s（{reason}）"
            if on_tick is not None:
                on_tick(msg)
            if phase_log is not None and (sec == int(math.ceil(total)) or sec % 5 == 0):
                phase_log(f"yaolu wait tick: {msg}")
        step = 0.25 if remain > 1.0 else max(0.05, remain)
        if not _sleep_interruptible(step, stop_event):
            return False



def _loot_cfg(cfg: YaoluConfig) -> SuperLootConfig:
    use_rand = bool(getattr(cfg, "entry_select_random", True))
    pool_r = float(getattr(cfg, "entry_select_radius", 35.0) or 35.0)
    return SuperLootConfig(
        item_name=cfg.entry_name,
        tid=ENTRY_ITEM_TID,
        match_tid_fallback=True,
        pick_range=float(cfg.pick_range),
        scan_radius=float(cfg.scan_radius),
        # Entry proof is IsDlgShow (captcha), not skill cast-this. Live logs show
        # MatterInteract ok while host+0x41C/cast ids stay 0 — require-cast then
        # false no_cast loops forever. Zero grace keeps interact ok for dialog wait.
        cast_start_grace_s=0.0,
        cast_wait_s=0.0,
        confirm_cast_start_only=False,
        use_bridge=bool(cfg.use_bridge),
        interact_mode="open",
        skip_opened_s=8.0,
        # Multi-gate stand: random among nearby 暗道 (not always same nearest).
        select_mode="random" if use_rand else "nearest",
        random_pool_radius=max(0.0, pool_r) if use_rand else 0.0,
        # Keep nearest fallbacks for non-random bands when random disabled.
        prefer_nearest=True,
    )


def _dist_to_entry_anchor(
    host_pos: tuple[float, float, float] | None,
    anchor: tuple[float, float, float] | None = None,
) -> float | None:
    if host_pos is None:
        return None
    base = anchor or DEFAULT_ENTRY_ANCHOR
    return _dist_xz(host_pos, base)


def ensure_path_to_entry(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    hwnd: int = 0,
    host_pos: tuple[float, float, float] | None = None,
    anchor: tuple[float, float, float] | None = None,
    force: bool = False,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
) -> dict:
    """
    Cross-map HostMove to entry stand point when far or force=True.

    super_loot only paths after scanning the entity; far hosts never get a hop.
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    quiet = lambda _m: None  # noqa: E731
    dest = anchor or DEFAULT_ENTRY_ANCHOR
    sp = read_scene_position(session, log=quiet)
    live_sid = sp.scene_id if sp.ok else None
    if host_pos is None and sp.ok and sp.scene_pos:
        host_pos = sp.scene_pos
    dist = _dist_to_entry_anchor(host_pos, dest)
    need = bool(force)
    if not need and dist is None:
        need = True
    elif not need and dist is not None and dist >= float(cfg.prepath_min_dist):
        need = True
    elif not need and live_sid is not None and not is_fuzhou_scene(live_sid, None):
        need = True
    out: dict = {
        "ok": True,
        "skipped": not need,
        "dist": dist,
        "live_scene_id": live_sid,
        "target_xyz": list(dest),
    }
    if not bool(cfg.prepath_to_entry) and not force:
        out["skipped"] = True
        return out
    if not need:
        log(f"yaolu prepath skip dist={dist} scene={live_sid}")
        return out
    status(
        f"全图寻路到妖楼入口 dist="
        f"{f'{dist:.1f}m' if dist is not None else '?'} scene={live_sid}"
    )
    moved = path_to_entry_anchor(
        session,
        cfg,
        hwnd=hwnd,
        anchor=dest,
        host_pos=host_pos,
        stop_event=stop_event,
        log=log,
    )
    out.update(moved)
    if not bool(moved.get("ok")):
        out["ok"] = False
        return out
    arrived, pos, last_dist = wait_for_entry_arrival(
        session,
        moved,
        cfg,
        stop_event=stop_event,
        log=log,
        on_progress=status,
    )
    out["arrived"] = arrived
    out["final_dist"] = last_dist
    out["host_pos"] = list(pos) if pos else None
    out["ok"] = bool(arrived)
    return out


def open_entry(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    hwnd: int = 0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
    host_pos: tuple[float, float, float] | None = None,
    prepath: bool = True,
) -> SuperLootStepResult:
    """
    Path to 九层妖楼暗道 and open (MatterInteract).

    Far hosts: HostMove to DEFAULT_ENTRY_ANCHOR (scene 68) first, then open
    only after live coordinates prove arrival (do not open mid-path).
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    if prepath and bool(cfg.prepath_to_entry):
        pre = ensure_path_to_entry(
            session,
            cfg,
            hwnd=hwnd,
            host_pos=host_pos,
            stop_event=stop_event,
            log=log,
            status=status,
        )
        if stop_event is not None and stop_event.is_set():
            return SuperLootStepResult(
                ok=False, action="stop", message="已停止", error="stopped"
            )
        if not pre.get("skipped"):
            # Must prove arrival before MatterInteract; HostMove ok alone is not enough.
            if not (pre.get("ok") and pre.get("arrived")):
                dist = pre.get("final_dist", pre.get("dist"))
                dist_txt = (
                    f"{float(dist):.1f}m"
                    if isinstance(dist, (int, float))
                    else "?"
                )
                err = (
                    str(pre.get("error") or "").strip()
                    or f"寻路未到达入口 last_d={dist_txt}"
                )
                status(f"未真正到达妖楼入口，暂不打开暗道（{err}）")
                log(
                    f"yaolu prepath not arrived err={pre.get('error')!r} "
                    f"arrived={pre.get('arrived')} dist={dist_txt}; skip open"
                )
                return SuperLootStepResult(
                    ok=False,
                    action="path",
                    message=f"寻路未到达入口，暂不打开暗道（{err}）",
                    error=err,
                )
            status("已真正到达妖楼入口，打开暗道")
            log(
                f"yaolu prepath arrived d={pre.get('final_dist')} "
                f"scene={pre.get('live_scene_id')}; open entry now"
            )
            if pre.get("host_pos"):
                try:
                    hp = pre["host_pos"]
                    host_pos = (float(hp[0]), float(hp[1]), float(hp[2]))
                except (TypeError, ValueError, IndexError):
                    pass
            # Only delay when we actually pathed here (not already standing near).
            delay = max(0.0, float(cfg.post_arrive_open_delay_s))
            if delay > 0.05:
                log(f"yaolu post-arrive open delay {delay:.2f}s")
                if not _sleep_with_status_countdown(
                    delay,
                    stop_event,
                    on_tick=status,
                    prefix="到达后短等",
                    reason="同图到达 settle",
                    phase_log=log,
                ):
                    return SuperLootStepResult(
                        ok=False, action="stop", message="已停止", error="stopped"
                    )
    res = super_loot_step(
        session,
        _loot_cfg(cfg),
        hwnd=hwnd,
        stop_event=stop_event,
        log=log,
    )
    # No entity in scan radius: force path to known coords once, then re-open.
    if (
        not res.ok
        and res.action == "none"
        and prepath
        and bool(cfg.prepath_to_entry)
    ):
        log("yaolu open: no entry in scan; force path to anchor then retry")
        status("附近无入口实体，强制寻路到妖楼坐标后重试")
        pre2 = ensure_path_to_entry(
            session,
            cfg,
            hwnd=hwnd,
            host_pos=host_pos,
            force=True,
            stop_event=stop_event,
            log=log,
            status=status,
        )
        if stop_event is not None and stop_event.is_set():
            return SuperLootStepResult(
                ok=False, action="stop", message="已停止", error="stopped"
            )
        if not (pre2.get("ok") and pre2.get("arrived")):
            dist = pre2.get("final_dist", pre2.get("dist"))
            dist_txt = (
                f"{float(dist):.1f}m"
                if isinstance(dist, (int, float))
                else "?"
            )
            err = (
                str(pre2.get("error") or "").strip()
                or f"强制寻路未到达入口 last_d={dist_txt}"
            )
            status(f"强制寻路后仍未到达，暂不打开暗道（{err}）")
            log(
                f"yaolu force prepath not arrived err={pre2.get('error')!r} "
                f"arrived={pre2.get('arrived')} dist={dist_txt}; skip open"
            )
            return SuperLootStepResult(
                ok=False,
                action="path",
                message=f"强制寻路未到达入口，暂不打开暗道（{err}）",
                error=err,
            )
        status("已真正到达妖楼入口，打开暗道")
        log("yaolu force prepath arrived; open entry now")
        delay = max(0.0, float(cfg.post_arrive_open_delay_s))
        if delay > 0.05:
            log(f"yaolu force post-arrive open delay {delay:.2f}s")
            if not _sleep_with_status_countdown(
                delay,
                stop_event,
                on_tick=status,
                prefix="到达后短等",
                reason="同图到达 settle",
                phase_log=log,
            ):
                return SuperLootStepResult(
                    ok=False, action="stop", message="已停止", error="stopped"
                )
        res = super_loot_step(
            session,
            _loot_cfg(cfg),
            hwnd=hwnd,
            stop_event=stop_event,
            log=log,
        )
    return res


def capture_captcha_dialog(
    hwnd: int,
    cfg: YaoluConfig,
    *,
    session: GameAttachSession | None = None,
    log: LogFn | None = None,
    save_tag: str | None = None,
) -> DialogCapture:
    """
    Export captcha dialog image (memory rect + game capture / BitBlt).

    Does NOT use PrintWindow (known hang). Open state must already be true
    in memory, or session is required to query it.
    @author by ak
    """
    log = log or (lambda _m: None)

    # Preferred: memory rect + game CaptureScreen / BitBlt
    if session is not None and bool(cfg.use_game_capture):
        exp = export_dlg_image(
            session,
            hwnd=hwnd,
            names=cfg.captcha_dlg_names,
            scale_to_train=True,
            prefer_bridge=bool(cfg.use_bridge),
            debug_save_fail=bool(cfg.debug_save_crops),
            log=log,
        )
        if exp.ok and exp.png and exp.rect is not None:
            box = DialogBox(
                left=int(exp.rect.x),
                top=int(exp.rect.y),
                right=int(exp.rect.right),
                bottom=int(exp.rect.bottom),
                score=9.0,
                method=f"mem_rect+{exp.method}",
            )
            if cfg.debug_save_crops:
                _debug_save_bytes(exp.png, tag=save_tag or "dialog", log=log)
            log(
                f"captcha export ok {box.width}x{box.height} via={exp.method} "
                f"png={len(exp.png)}"
            )
            return DialogCapture(
                ok=True,
                box=box,
                png=exp.png,
                client_w=exp.client_w,
                client_h=exp.client_h,
            )
        # If memory says open but export failed, surface that error.
        rect = get_captcha_dlg_rect(
            session, names=cfg.captcha_dlg_names, log=log
        )
        if rect.shown:
            return DialogCapture(
                ok=False,
                box=DialogBox(
                    left=rect.x,
                    top=rect.y,
                    right=rect.right,
                    bottom=rect.bottom,
                    score=0.0,
                    method="mem_rect_only",
                )
                if rect.ok
                else None,
                error=exp.error or "export failed while dialog shown",
            )
        return DialogCapture(ok=False, error=exp.error or "dialog not shown")

    # Last resort without session: BitBlt full client, then crop dialog only.
    if not hwnd:
        return DialogCapture(ok=False, error="no hwnd")
    from app.core.dlg_image_export import (
        _crop_activity_dialog_from_full,
        capture_client_bitblt_only,
    )

    cap = capture_client_bitblt_only(hwnd, log=log)
    if not cap.ok or cap.image is None:
        return DialogCapture(ok=False, error=cap.error or "bitblt failed")
    # Never send full client to API — crop 活动限时答题 panel only.
    crop, used, method = _crop_activity_dialog_from_full(
        cap.image, mem_rect=None, scale_to_train=True, log=log
    )
    if crop is None or used is None:
        if cfg.debug_save_crops and save_tag:
            _debug_save_full(cap.image, tag=f"miss_{save_tag}", log=log)
        box = find_captcha_dialog(cap.image, log=log)
        return DialogCapture(
            ok=False,
            box=box,
            client_w=cap.width,
            client_h=cap.height,
            error=method or "dialog not visible",
        )
    box = DialogBox(
        left=int(used.x),
        top=int(used.y),
        right=int(used.x + used.w),
        bottom=int(used.y + used.h),
        score=9.0,
        method=method,
    )
    from io import BytesIO

    bio = BytesIO()
    crop.save(bio, format="PNG")
    png = bio.getvalue()
    if cfg.debug_save_crops:
        _debug_save_bytes(png, tag=save_tag or "dialog", log=log)
    log(
        f"captcha export ok {box.width}x{box.height} via=bitblt+{method} "
        f"png={len(png)} (dialog-only, not full scene)"
    )
    return DialogCapture(
        ok=True,
        box=box,
        png=png,
        client_w=cap.width,
        client_h=cap.height,
    )


def _debug_dir():
    """
    Captcha crop dir fixed beside the running source/executable.

    @author by ak
    """
    try:
        from common.paths import captcha_debug_dir

        return captcha_debug_dir()
    except Exception:
        from pathlib import Path
        from common.paths import app_root

        # Keep diagnostics deterministic. Save callers catch and log an I/O
        # failure instead of silently moving evidence to LOCALAPPDATA/cwd.
        return Path(app_root()) / "captures" / "captcha"


def cleanup_captcha_debug(
    *,
    ttl_s: float = 120.0,
    force_all: bool = False,
    log: LogFn | None = None,
) -> int:
    """
    Delete captcha debug images older than ttl_s (or all if force_all).

    ttl_s <= 0 with force_all=False still removes files older than 0s (all).
    @author by ak
    """
    log = log or (lambda _m: None)
    try:
        d = _debug_dir()
    except Exception:
        return 0
    if not d.is_dir():
        return 0
    now = time.time()
    ttl = max(0.0, float(ttl_s))
    removed = 0
    for p in d.glob("*.png"):
        try:
            if force_all or (now - p.stat().st_mtime) >= ttl:
                p.unlink(missing_ok=True)
                removed += 1
        except Exception:
            continue
    if removed:
        log(f"captcha debug cleanup dir={d} removed={removed} ttl_s={ttl}")
    return removed


def _debug_save_full(img, *, tag: str, log: LogFn) -> None:
    """
    Save full-frame debug image under writable captcha dir.

    @author by ak
    """
    try:
        d = _debug_dir()
        path = d / f"{int(time.time())}_{tag}_full.png"
        img.save(path)
        log(f"captcha debug full -> {path} bytes={path.stat().st_size}")
        cleanup_captcha_debug(ttl_s=120.0, log=log)
    except Exception as e:
        log(f"captcha debug full save err: {e}")


def _debug_save_bytes(png: bytes, *, tag: str, log: LogFn) -> None:
    """
    Save crop PNG under writable captcha dir (log absolute path).

    @author by ak
    """
    try:
        d = _debug_dir()
        path = d / f"{int(time.time())}_{tag}_crop.png"
        path.write_bytes(png)
        log(f"captcha debug crop -> {path} bytes={len(png)}")
        cleanup_captcha_debug(ttl_s=120.0, log=log)
    except Exception as e:
        log(f"captcha debug crop save err: {e}")


def wait_for_captcha_dialog_mem(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    timeout_s: float | None = None,
    settle_s: float | None = None,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
    entry_block_baseline: dict[str, int] | None = None,
) -> DlgShowResult:
    """
    Poll plg IsDlgShow until ActivityQuestion (or candidates) is open.

    Memory-backed “入口/验证码已打开” — not timer-based.
    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    settle = float(settle_s if settle_s is not None else cfg.post_open_settle_s)
    if settle > 0:
        status(f"短等读条启动 {settle:.1f}s 后查内存弹窗")
        if not _sleep_interruptible(settle, stop_event):
            return DlgShowResult(ok=False, shown=False, error="stopped")
    limit = float(timeout_s if timeout_s is not None else cfg.open_dialog_wait_s)
    names = tuple(cfg.captcha_dlg_names) or CAPTCHA_DIALOG_NAME_CANDIDATES
    last_block_probe = 0.0

    def _abort_on_entry_block() -> str | None:
        nonlocal last_block_probe
        if entry_block_baseline is None:
            return None
        now = time.monotonic()
        if now - last_block_probe < 0.8:
            return None
        last_block_probe = now
        hit = probe_entry_open_block_text(
            session,
            baseline=entry_block_baseline,
            log=log,
            heavy=False,
        )
        return (hit.text or ENTRY_OPEN_BLOCK_TEXT) if hit is not None else None

    return wait_dlg_show(
        session,
        names,
        timeout_s=limit,
        poll_s=max(0.2, float(cfg.captcha_poll_s) * 0.5),
        stop_event=stop_event,
        log=log,
        status=status,
        abort_probe=_abort_on_entry_block,
    )


def wait_for_captcha_dialog(
    hwnd: int,
    cfg: YaoluConfig,
    *,
    session: GameAttachSession | None = None,
    timeout_s: float | None = None,
    settle_s: float | None = None,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
    entry_block_baseline: dict[str, int] | None = None,
) -> DialogCapture:
    """
    Wait until captcha is open (memory first), then crop dialog pixels.

    Open/exist signal = plg GetGameUIDlg + IsDlgShow when session is available.
    Screenshot crop is only for identify API payload, not open proof.
    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    mem: DlgShowResult | None = None

    if session is not None and bool(cfg.use_memory_dialog):
        mem = wait_for_captcha_dialog_mem(
            session,
            cfg,
            timeout_s=timeout_s,
            settle_s=settle_s,
            stop_event=stop_event,
            log=log,
            status=status,
            entry_block_baseline=entry_block_baseline,
        )
        if stop_event is not None and stop_event.is_set():
            return DialogCapture(ok=False, error="stopped")
        if not mem.ok or not mem.shown:
            return DialogCapture(
                ok=False,
                error=mem.error or f"dialog not open (mem name={mem.name!r})",
            )
        status(f"内存确认弹窗已开 {mem.name} ptr=0x{mem.dlg_ptr:X}，导出弹窗图")
        # Dialog is open in memory: export image via game capture / BitBlt.
        # Cap export tries to avoid CaptureScreen spam when crop keeps failing.
        export_tries = max(1, min(2, int(getattr(cfg, "captcha_identify_max_attempts", 1))))
        for i in range(export_tries):
            if stop_event is not None and stop_event.is_set():
                return DialogCapture(ok=False, error="stopped")
            cap = capture_captcha_dialog(
                hwnd,
                cfg,
                session=session,
                log=log,
                save_tag=f"memok{i+1}",
            )
            if cap.ok and cap.box is not None and cap.png:
                status(
                    f"验证码导出 {cap.box.width}x{cap.box.height} "
                    f"score={cap.box.score:.2f} via={mem.name}"
                )
                return cap
            # still open? if closed, fail
            chk = is_captcha_dialog_open(
                session, names=cfg.captcha_dlg_names, log=log
            )
            if not chk.shown:
                return DialogCapture(ok=False, error="dialog closed before export")
            if not _sleep_interruptible(0.45, stop_event):
                return DialogCapture(ok=False, error="stopped")
        return DialogCapture(
            ok=False,
            error="dialog open in mem but export failed",
        )

    # Fallback: visual-only poll (no session / memory disabled).
    settle = float(settle_s if settle_s is not None else cfg.post_open_settle_s)
    if settle > 0:
        status(f"等待读条/弹窗 settle {settle:.1f}s (视觉回退)")
        if not _sleep_interruptible(settle, stop_event):
            return DialogCapture(ok=False, error="stopped")

    limit = float(timeout_s if timeout_s is not None else cfg.open_dialog_wait_s)
    deadline = time.time() + max(0.5, limit)
    last = DialogCapture(ok=False, error="timeout")
    attempt = 0
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return DialogCapture(ok=False, error="stopped")
        attempt += 1
        status(f"等待验证码弹窗(视觉)… #{attempt}")
        last = capture_captcha_dialog(
            hwnd,
            cfg,
            session=None,
            log=log,
            save_tag=f"wait{attempt}",
        )
        if last.ok and last.box is not None:
            status(
                f"验证码已弹出 {last.box.width}x{last.box.height} "
                f"score={last.box.score:.2f}"
            )
            return last
        if not _sleep_interruptible(float(cfg.captcha_poll_s), stop_event):
            return DialogCapture(ok=False, error="stopped")
    return last


def fake_captcha_noise(
    hwnd: int,
    cfg: YaoluConfig,
    dialog: DialogCapture,
    *,
    session: GameAttachSession | None = None,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> bool:
    """
    Simulate human noise: randomly select one animal then deselect after random delay.
    Must finish before real identify result arrives.
    If real result is ready early, skip.
    @author by ak
    """
    log = log or (lambda _m: None)
    if not dialog.ok or not dialog.box:
        return False
    if stop_event is not None and stop_event.is_set():
        return False

    try:
        # Approximate 2x4 grid centers from dialog box (660x460 typical).
        # Grid usually occupies upper ~70% of dialog.
        box = dialog.box
        grid_w = int(box.width * 0.78)
        grid_h = int(box.height * 0.55)
        grid_left = box.left + int(box.width * 0.11)
        grid_top = box.top + int(box.height * 0.12)

        cell_w = grid_w // 4
        cell_h = grid_h // 2

        # 8 cells 0-based row-major
        cell_centers = []
        for row in range(2):
            for col in range(4):
                cx = grid_left + col * cell_w + cell_w // 2
                cy = grid_top + row * cell_h + cell_h // 2
                cell_centers.append((cx, cy))

        # Random one animal
        cell_idx = random.randint(0, 7)
        cx, cy = cell_centers[cell_idx]
        # Add human jitter
        cx, cy = jitter_around(cx, cy, radius_x=12, radius_y=8)

        log(f"yaolu fake noise: select animal {cell_idx+1}/8 at ({cx},{cy})")

        # Click to select
        ok_click = click_client_bg(
            session,
            hwnd,
            cx,
            cy,
            prefer_bridge=bool(cfg.use_bridge),
            prefer_post=True,
            allow_cursor=False,
            humanize=True,
            log=log,
        )
        if not ok_click:
            log("fake noise first click failed")
            return False

        # Random think time before deselect (must be < identify time ~12s)
        delay = human_delay_s(
            float(getattr(cfg, "captcha_fake_noise_min_s", 1.2)),
            float(getattr(cfg, "captcha_fake_noise_max_s", 4.5)),
        )
        log(f"yaolu fake noise pause {delay:.2f}s before deselect")
        if not _sleep_interruptible(delay, stop_event):
            return False

        # Deselect by clicking same cell again
        ok_deselect = click_client_bg(
            session,
            hwnd,
            cx,
            cy,
            prefer_bridge=bool(cfg.use_bridge),
            prefer_post=True,
            allow_cursor=False,
            humanize=True,
            log=log,
        )
        log(f"yaolu fake noise deselect ok={ok_deselect}")
        return True
    except Exception as e:
        log(f"yaolu fake noise err: {e}")
        return False


def solve_captcha_dialog(
    hwnd: int,
    cfg: YaoluConfig,
    dialog: DialogCapture,
    *,
    session: GameAttachSession | None = None,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> CaptchaIdentifyResult:
    """
    Identify cropped dialog image, select two cells + confirm.

    Human-like path (minimized-safe):
      1) memory Img_Question cell points with inset jitter
      2) bridge/UI-thread click with hold + random gaps
      3) Btn_Ok click first (latches selection); optional AQ_SUBMIT only if configured
    Fallback: dialog-relative geometry + background Post/SendMessage.
    @author by ak
    """
    log = log or (lambda _m: None)
    if not dialog.ok or dialog.box is None:
        return CaptchaIdentifyResult(ok=False, error=dialog.error or "no dialog")
    # Debug mode needs dialog open + session cells; PNG/API not required.
    if not bool(getattr(cfg, "debug_random_answer", False)) and not dialog.png:
        return CaptchaIdentifyResult(ok=False, error=dialog.error or "no dialog png")

    if bool(getattr(cfg, "debug_random_answer", False)):
        positions = random.sample(range(1, 9), 2)
        log(
            f"yaolu captcha DEBUG random positions={positions} "
            "(skip identify API + feedback)"
        )
        ident = CaptchaIdentifyResult(
            ok=True,
            positions=list(positions),
            animal="debug-random",
            confidence=0.0,
            identify_id=None,
            raw={"debug_random_answer": True, "positions": list(positions)},
        )
    else:
        ident = identify_image(
            dialog.png,
            api_key=cfg.captcha_api_key,
            login_token=cfg.login_token,
            base_url=cfg.captcha_base_url,
            timeout_s=float(cfg.captcha_api_timeout_s),
            log=log,
        )
    if not ident.ok:
        try:
            dialog.png = None
        except Exception:
            pass
        if bool(cfg.debug_save_crops):
            cleanup_captcha_debug(ttl_s=float(cfg.debug_crop_ttl_s), log=log)
        return ident
    if (
        ident.confidence is not None
        and float(ident.confidence) < float(cfg.min_confidence)
    ):
        log(f"yaolu captcha low conf={ident.confidence}")
        if bool(getattr(cfg, "captcha_reject_low_confidence", True)):
            try:
                dialog.png = None
            except Exception:
                pass
            if bool(cfg.debug_save_crops):
                cleanup_captcha_debug(ttl_s=float(cfg.debug_crop_ttl_s), log=log)
            return CaptchaIdentifyResult(
                ok=False,
                error=(
                    f"置信度过低 conf={float(ident.confidence):.3f} "
                    f"< min={float(cfg.min_confidence):.2f}（已拒绝提交）"
                ),
                confidence=ident.confidence,
                animal=ident.animal,
                positions=list(ident.positions or []),
                identify_id=ident.identify_id,
                http_status=ident.http_status,
                raw=ident.raw,
            )

    box = dialog.box
    positions = [int(p) for p in (ident.positions or []) if 1 <= int(p) <= 8][:2]
    gap_base = max(0.12, float(cfg.captcha_click_gap_s))
    gap_jit = max(0.0, float(cfg.captcha_click_gap_jitter_s))
    prefer_bridge = bool(cfg.use_bridge)
    prefer_post = bool(cfg.prefer_post_click)
    allow_cursor = bool(cfg.allow_cursor_click)
    prefer_real = bool(
        getattr(cfg, "captcha_prefer_real_mouse", allow_cursor)
    )
    real_confirm_y_offset = (
        max(
            -48,
            min(
                48,
                int(getattr(cfg, "captcha_real_mouse_confirm_y_offset_px", 0) or 0),
            ),
        )
        if prefer_real
        else 0
    )
    humanize = bool(cfg.captcha_humanize_clicks)
    dlg_names = tuple(cfg.captcha_dlg_names) or CAPTCHA_DIALOG_NAME_CANDIDATES
    # Prefer real Btn_Ok click so selection state is applied like a player.
    pure_submit = prefer_bridge and (not bool(cfg.captcha_prefer_btn_click))

    # --- resolve cell client points ---
    # Prefer memory Img_Question 2x4 (scans ActivityQuestion / Question3D…).
    # API click_centers are train crop space (701x430) — scale to live box.
    clicks: list[tuple[int, int]] = []
    point_src = "none"
    if session is not None and positions:
        for p in positions:
            if humanize:
                pt = get_captcha_cell_click_point(
                    session, int(p), names=dlg_names, log=log
                )
            else:
                pt = get_captcha_cell_center(
                    session, int(p), names=dlg_names, log=log
                )
            if pt is not None:
                clicks.append(pt)
        if len(clicks) >= 2:
            point_src = "mem_img_question"
        else:
            log(
                f"yaolu captcha mem grid partial {len(clicks)}/2 "
                f"pos={positions}"
            )
            clicks = []

    if len(clicks) < 2 and positions:
        clicks = []
        for p in positions:
            pt = dialog_cell_point(box, int(p))
            if pt is not None:
                if humanize:
                    pt = jitter_around(pt[0], pt[1], radius_x=7, radius_y=6)
                clicks.append(pt)
        if len(clicks) >= 2:
            point_src = "dialog_grid"

    if len(clicks) < 2 and ident.click_centers:
        clicks = []
        bw = max(1, int(box.width))
        bh = max(1, int(box.height))
        sx = bw / float(TRAIN_DIALOG_W)
        sy = bh / float(TRAIN_DIALOG_H)
        for c in ident.click_centers[:2]:
            try:
                rx = float(c[0])
                ry = float(c[1])
            except Exception:
                continue
            if 0.0 <= rx <= float(TRAIN_DIALOG_W + 40) and 0.0 <= ry <= float(
                TRAIN_DIALOG_H + 40
            ):
                cx = box.left + int(round(rx * sx))
                cy = box.top + int(round(ry * sy))
            else:
                cx = int(round(rx))
                cy = int(round(ry))
            if humanize:
                cx, cy = jitter_around(cx, cy, radius_x=6, radius_y=5)
            clicks.append((cx, cy))
        if len(clicks) >= 2:
            point_src = "api_centers_scaled"

    if len(clicks) < 2:
        try:
            dialog.png = None
        except Exception:
            pass
        if bool(cfg.debug_save_crops):
            cleanup_captcha_debug(ttl_s=float(cfg.debug_crop_ttl_s), log=log)
        return CaptchaIdentifyResult(
            ok=False,
            error="no click points",
            identify_id=ident.identify_id,
            positions=ident.positions,
            confidence=ident.confidence,
            raw=ident.raw,
        )

    log(
        f"yaolu captcha animal={ident.animal!r} pos={positions or ident.positions} "
        f"conf={ident.confidence} clicks={clicks} src={point_src} "
        f"bridge={prefer_bridge} human={humanize} cursor={allow_cursor} real={prefer_real}"
    )

    # Fake human noise during slow identify (simulate thinking/select-deselect).
    # If real result arrives early, fake is skipped by timing.
    if (
        bool(getattr(cfg, "captcha_fake_noise_enabled", True))
        and random.random() < float(getattr(cfg, "captcha_fake_noise_prob", 0.65))
    ):
        try:
            fake_captcha_noise(
                hwnd,
                cfg,
                dialog,
                session=session,
                stop_event=stop_event,
                log=log,
            )
        except Exception as e:
            log(f"yaolu fake noise err: {e}")

    # Normal automation uses bounded dialog/chat probes only. Process-wide text
    # scans are diagnostic-only because pattern_scan_all has no hard deadline.
    ident.answer_baseline = None

    # Foreground/real-mouse: steal focus before any OS cursor move/click.
    if prefer_real and hwnd:
        try:
            from app.core.win_capture import ensure_foreground

            ok_fg = ensure_foreground(
                int(hwnd),
                retries=4,
                settle_s=0.08,
                force=True,
                log=log,
            )
            log(
                f"yaolu captcha ensure_foreground hwnd=0x{int(hwnd):X} ok={ok_fg}"
            )
        except Exception as e:
            log(f"yaolu captcha ensure_foreground err: {e}")

    hold_lo = int(getattr(cfg, "captcha_hold_min_ms", 60) or 60)
    hold_hi = int(getattr(cfg, "captcha_hold_max_ms", 220) or 220)
    slide_on = bool(humanize and getattr(cfg, "captcha_slide_enabled", False))
    approach_on = bool(
        slide_on and humanize and getattr(cfg, "captcha_approach_slide", True)
    )
    approach_r_lo = max(8, int(getattr(cfg, "captcha_approach_radius_min", 28) or 28))
    approach_r_hi = max(
        approach_r_lo, int(getattr(cfg, "captcha_approach_radius_max", 72) or 72)
    )
    slide_min_steps = int(getattr(cfg, "captcha_slide_min_steps", 5) or 5)
    slide_max_steps = int(getattr(cfg, "captcha_slide_max_steps", 14) or 14)
    slide_dur_lo = float(getattr(cfg, "captcha_slide_duration_min_s", 0.12) or 0.12)
    slide_dur_hi = float(getattr(cfg, "captcha_slide_duration_max_s", 0.55) or 0.55)

    # After identify: short "read the board" pause before first press.
    if humanize and clicks:
        pre_first = human_delay_s(
            float(getattr(cfg, "captcha_pre_first_click_min_s", 0.35) or 0.0),
            float(getattr(cfg, "captcha_pre_first_click_max_s", 1.20) or 0.0),
        )
        if pre_first > 0:
            log(f"yaolu captcha pre-first-click pause {pre_first:.2f}s")
            if not _sleep_interruptible(pre_first, stop_event):
                try:
                    dialog.png = None
                except Exception:
                    pass
                if bool(cfg.debug_save_crops):
                    cleanup_captcha_debug(
                        ttl_s=float(cfg.debug_crop_ttl_s), log=log
                    )
                return CaptchaIdentifyResult(ok=False, error="stopped")

    last_pt: tuple[int, int] | None = None
    for i, (cx, cy) in enumerate(clicks):
        if stop_event is not None and stop_event.is_set():
            try:
                dialog.png = None
            except Exception:
                pass
            if bool(cfg.debug_save_crops):
                cleanup_captcha_debug(ttl_s=float(cfg.debug_crop_ttl_s), log=log)
            return CaptchaIdentifyResult(ok=False, error="stopped")
        hold_ms = (
            human_hold_ms(hold_lo, hold_hi)
            if humanize
            else max(40, (hold_lo + hold_hi) // 2)
        )
        path_from = last_pt if slide_on else None
        # Force approach path for EVERY click when slide is on and no previous point
        # (eliminates teleport jumps even on first cell or after deselect).
        if path_from is None and approach_on:
            ang = random.uniform(0.0, 6.283185307179586)
            rad = random.uniform(float(approach_r_lo), float(approach_r_hi))
            path_from = (
                int(round(cx + math.cos(ang) * rad)),
                int(round(cy + math.sin(ang) * rad)),
            )
            log(
                f"yaolu captcha approach from {path_from} -> ({cx},{cy}) "
                f"r≈{rad:.0f} (cell {i+1})"
            )
        ok_click = click_client_bg(
            session,
            hwnd,
            int(cx),
            int(cy),
            prefer_bridge=prefer_bridge and session is not None,
            prefer_post=prefer_post,
            allow_cursor=allow_cursor,
            humanize=humanize,
            hold_ms=hold_ms,
            hold_min_ms=hold_lo,
            hold_max_ms=hold_hi,
            path_from=path_from,
            slide=bool(slide_on and path_from is not None),
            slide_min_steps=slide_min_steps,
            slide_max_steps=slide_max_steps,
            slide_duration_min_s=slide_dur_lo,
            slide_duration_max_s=slide_dur_hi,
            prefer_real_mouse=prefer_real,
            stop_event=stop_event,
            log=log,
        )
        log(
            f"yaolu captcha cell click ({cx},{cy}) ok={ok_click} "
            f"hold_ms={hold_ms} slide_from={path_from if slide_on else None}"
        )
        if ok_click:
            last_pt = (int(cx), int(cy))
        gap = (
            human_inter_click_gap_s(gap_base, gap_jit)
            if humanize
            else float(gap_base)
        )
        if i + 1 < len(clicks):
            if not _sleep_interruptible(gap, stop_event):
                try:
                    dialog.png = None
                except Exception:
                    pass
                if bool(cfg.debug_save_crops):
                    cleanup_captcha_debug(
                        ttl_s=float(cfg.debug_crop_ttl_s), log=log
                    )
                return CaptchaIdentifyResult(ok=False, error="stopped")

    # Pre-confirm think pause (human re-check selection).
    pre = human_delay_s(
        float(cfg.captcha_pre_confirm_min_s),
        float(cfg.captcha_pre_confirm_max_s),
    )
    if humanize and pre > 0:
        log(f"yaolu captcha pre-confirm pause {pre:.2f}s")
        if not _sleep_interruptible(pre, stop_event):
            try:
                dialog.png = None
            except Exception:
                pass
            if bool(cfg.debug_save_crops):
                cleanup_captcha_debug(ttl_s=float(cfg.debug_crop_ttl_s), log=log)
            return CaptchaIdentifyResult(ok=False, error="stopped")
    elif not humanize:
        if not _sleep_interruptible(gap_base, stop_event):
            try:
                dialog.png = None
            except Exception:
                pass
            if bool(cfg.debug_save_crops):
                cleanup_captcha_debug(ttl_s=float(cfg.debug_crop_ttl_s), log=log)
            return CaptchaIdentifyResult(ok=False, error="stopped")

    # Heap short-marker baseline BEFORE confirm so post-submit growth is "new".
    # Without this, a fast 答案错误 can land before first wait tick and be absorbed.
    if session is not None:
        try:
            snapshot_feedback_heap_baseline(session, log=log)
        except Exception as e:
            log(f"yaolu feedback pre-confirm baseline err: {e}")
            # Heap optional; hist baseline is the production signal.
            try:
                from app.core.game_sys_msg import snapshot_feedback_hist_baseline

                snapshot_feedback_hist_baseline(session, budget_s=1.0, log=log)
            except Exception as e2:
                log(f"yaolu feedback hist baseline fallback err: {e2}")

    # --- confirm: Btn_Ok UI click first (selection-safe), AQ_SUBMIT optional ---
    ok_conf = False
    conf_path_from = last_pt if slide_on else None
    # Force approach into confirm if no trail (no teleport on Btn_Ok either).
    if slide_on and approach_on and conf_path_from is None:
        try:
            conf_x0, conf_y0 = dialog_confirm_point(box)
            ang = random.uniform(0.0, 6.283185307179586)
            rad = random.uniform(float(approach_r_lo), float(approach_r_hi))
            conf_path_from = (
                int(round(conf_x0 + math.cos(ang) * rad)),
                int(round(conf_y0 + math.sin(ang) * rad)),
            )
            log(
                f"yaolu captcha confirm approach from {conf_path_from} "
                f"-> approx ({conf_x0},{conf_y0}) r≈{rad:.0f}"
            )
        except Exception as e:
            log(f"yaolu captcha confirm approach err: {e}")
            conf_path_from = None
    if session is not None:
        ok_conf = click_captcha_btn_ok(
            session,
            hwnd,
            prefer_bridge=prefer_bridge,
            prefer_post=prefer_post,
            allow_cursor=allow_cursor,
            pure_submit=pure_submit,
            humanize=humanize,
            names=dlg_names,
            path_from=conf_path_from,
            slide=bool(slide_on and conf_path_from is not None),
            slide_min_steps=slide_min_steps,
            slide_max_steps=slide_max_steps,
            slide_duration_min_s=slide_dur_lo,
            slide_duration_max_s=slide_dur_hi,
            hold_min_ms=hold_lo,
            hold_max_ms=hold_hi,
            prefer_real_mouse=prefer_real,
            real_mouse_y_offset_px=real_confirm_y_offset,
            stop_event=stop_event,
            log=log,
        )
        if ok_conf:
            log(
                "yaolu captcha confirm via "
                + ("AQ_SUBMIT" if pure_submit else "Btn_Ok click")
            )
    if not ok_conf:
        conf_x, conf_y = dialog_confirm_point(box)
        if prefer_real:
            conf_y = max(box.top + 1, min(box.bottom - 2, conf_y + real_confirm_y_offset))
        if humanize:
            conf_x, conf_y = jitter_around(conf_x, conf_y, radius_x=10, radius_y=4)
        conf_x, conf_y = clamp_dialog_confirm_point(box, conf_x, conf_y)
        conf_hold = (
            human_hold_ms(hold_lo, hold_hi)
            if humanize
            else max(40, (hold_lo + hold_hi) // 2)
        )
        log(
            f"yaolu captcha confirm client=({conf_x},{conf_y}) "
            f"(UI click fallback real_y_offset={real_confirm_y_offset}) hold_ms={conf_hold} "
            f"slide_from={last_pt if slide_on else None}"
        )
        ok_conf = click_client_bg(
            session,
            hwnd,
            conf_x,
            conf_y,
            prefer_bridge=prefer_bridge and session is not None,
            prefer_post=prefer_post,
            allow_cursor=allow_cursor,
            humanize=humanize,
            hold_ms=conf_hold,
            hold_min_ms=hold_lo,
            hold_max_ms=hold_hi,
            path_from=conf_path_from if slide_on else None,
            slide=bool(slide_on and conf_path_from is not None),
            slide_min_steps=slide_min_steps,
            slide_max_steps=slide_max_steps,
            slide_duration_min_s=slide_dur_lo,
            slide_duration_max_s=slide_dur_hi,
            prefer_real_mouse=prefer_real,
            stop_event=stop_event,
            log=log,
        )

    # A bridge UI_CLICK ret=1 only proves that the mouse message was delivered.
    # It does not prove the thin 确定 control accepted the click. Verify that the
    # dialog actually closed, then retry at the calibrated visual center and
    # finally use the UI-thread AQ_SUBMIT handler with the selected cells latched.
    if ok_conf and session is not None:
        if not _sleep_interruptible(0.35, stop_event):
            ident.ok = False
            ident.error = "stopped"
            ok_conf = False

        def _confirm_dialog_still_open(tag: str) -> bool | None:
            try:
                hit = is_captcha_dialog_open(
                    session,
                    names=dlg_names,
                    log=lambda _m: None,
                )
                shown = bool(hit.shown)
                log(
                    f"yaolu captcha confirm verify {tag}: "
                    f"shown={shown} name={hit.name!r} dlg=0x{int(hit.dlg_ptr or 0):X}"
                )
                return shown
            except Exception as e:
                log(f"yaolu captcha confirm verify {tag} err: {e}")
                return None

        still_open = _confirm_dialog_still_open("after_click") if ok_conf else None
        if still_open is True:
            conf_x, conf_y = dialog_confirm_point(box)
            if prefer_real:
                conf_y = max(
                    box.top + 1,
                    min(box.bottom - 2, conf_y + real_confirm_y_offset),
                )
            if humanize:
                conf_x, conf_y = jitter_around(
                    conf_x,
                    conf_y,
                    radius_x=4,
                    radius_y=1,
                )
            conf_x, conf_y = clamp_dialog_confirm_point(box, conf_x, conf_y)
            retry_hold = (
                human_hold_ms(hold_lo, hold_hi)
                if humanize
                else max(40, (hold_lo + hold_hi) // 2)
            )
            log(
                f"yaolu captcha confirm retry calibrated=({conf_x},{conf_y}) "
                f"hold_ms={retry_hold}"
            )
            retry_ok = click_client_bg(
                session,
                hwnd,
                conf_x,
                conf_y,
                prefer_bridge=prefer_bridge,
                prefer_post=prefer_post,
                allow_cursor=allow_cursor,
                humanize=humanize,
                hold_ms=retry_hold,
                hold_min_ms=hold_lo,
                hold_max_ms=hold_hi,
                prefer_real_mouse=prefer_real,
                stop_event=stop_event,
                log=log,
            )
            log(f"yaolu captcha confirm retry click ok={retry_ok}")
            if _sleep_interruptible(0.35, stop_event):
                still_open = _confirm_dialog_still_open("after_calibrated_retry")
            else:
                ident.ok = False
                ident.error = "stopped"
                still_open = True

        if still_open is True and prefer_bridge:
            submit_ok = submit_activity_question(
                session,
                names=dlg_names,
                hwnd=hwnd,
                log=log,
            )
            log(f"yaolu captcha confirm retry AQ_SUBMIT ok={submit_ok}")
            if _sleep_interruptible(0.35, stop_event):
                still_open = _confirm_dialog_still_open("after_aq_submit")
            else:
                ident.ok = False
                ident.error = "stopped"
                still_open = True

        if still_open is False:
            ok_conf = True
        elif still_open is True:
            ok_conf = False
            ident.ok = False
            ident.error = "确认按钮未生效（验证码弹窗仍显示，未提交）"
            log("yaolu captcha confirm failed: dialog still shown after retries")
    log(f"yaolu captcha confirm ok={ok_conf} src={point_src}")
    # Drop PNG from memory and prune debug crops (use-once / TTL).
    try:
        dialog.png = None
    except Exception:
        pass
    if bool(cfg.debug_save_crops):
        cleanup_captcha_debug(ttl_s=float(cfg.debug_crop_ttl_s), log=log)
    return ident


def wait_and_solve_captcha(
    hwnd: int,
    cfg: YaoluConfig,
    *,
    session: GameAttachSession | None = None,
    dialog: DialogCapture | None = None,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
) -> CaptchaIdentifyResult:
    """
    Ensure dialog is open (memory), then identify + click once.

    On identify/export fail: never click/submit; limited re-crop attempts only.
    Caller must cool down (walk-away or 15-60s wait) before re-open.

    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)

    # 1) listen for real dialog (memory when session provided)
    cur = dialog
    if cur is None or not cur.ok:
        cur = wait_for_captcha_dialog(
            hwnd,
            cfg,
            session=session,
            timeout_s=cfg.captcha_wait_s,
            stop_event=stop_event,
            log=log,
            status=status,
        )
    if not cur.ok:
        return CaptchaIdentifyResult(ok=False, error=cur.error or "dialog not open")

    # 2) identify with capped retries (re-crop only; never spam CaptureScreen forever)
    max_attempts = max(1, int(cfg.captcha_identify_max_attempts))
    attempt = 0
    last_err = "identify fail"
    last_result: CaptchaIdentifyResult | None = None
    extra_timeout_retry_used = False
    while attempt < max_attempts:
        if stop_event is not None and stop_event.is_set():
            return CaptchaIdentifyResult(ok=False, error="stopped")
        # Bail if memory says dialog closed mid-solve
        if session is not None and bool(cfg.use_memory_dialog):
            mem = is_captcha_dialog_open(
                session, names=cfg.captcha_dlg_names, log=log
            )
            if not mem.shown:
                return CaptchaIdentifyResult(
                    ok=False, error=f"dialog closed ({mem.name})"
                )
        attempt += 1
        status(f"识别中… #{attempt}/{max_attempts}")
        # re-crop each attempt in case dialog just fully rendered
        if attempt > 1:
            cur = capture_captcha_dialog(
                hwnd, cfg, session=session, log=log, save_tag=f"retry{attempt}"
            )
            if not cur.ok:
                last_err = cur.error or "dialog lost"
                log(f"yaolu captcha re-export fail #{attempt}: {last_err}")
                if not _sleep_interruptible(float(cfg.captcha_poll_s), stop_event):
                    return CaptchaIdentifyResult(ok=False, error="stopped")
                continue
        res = solve_captcha_dialog(
            hwnd,
            cfg,
            cur,
            session=session,
            stop_event=stop_event,
            log=log,
        )
        if res.ok:
            return res
        last_result = res
        last_err = res.error or "identify fail"
        http_status = int(getattr(res, "http_status", 0) or 0)
        log(
            f"yaolu captcha attempt#{attempt}/{max_attempts} fail: {last_err} "
            f"http={http_status or '-'} "
            f"(no submit; skip enter path)"
        )
        if http_status == 401:
            log("yaolu captcha HTTP 401 is permanent; stop identify retries")
            return res
        # Timeout / transient API fail: allow one extra re-crop+identify even if
        # captcha_identify_max_attempts was left at 1.
        low = str(last_err).lower()
        transient = (
            "timeout" in low
            or "timed out" in low
            or "识别失败" in str(last_err)
            or "empty response" in low
            or "urlopen error" in low
            or "connection" in low
            or http_status >= 500
        )
        if (
            transient
            and not extra_timeout_retry_used
            and attempt >= max_attempts
        ):
            extra_timeout_retry_used = True
            max_attempts = attempt + 1
            log(
                f"yaolu captcha transient fail; grant one extra identify "
                f"(now max={max_attempts})"
            )
        # Identify/API fail: do not click cells; leave dialog for next open after CD.
        if not _sleep_interruptible(float(cfg.captcha_poll_s), stop_event):
            return CaptchaIdentifyResult(ok=False, error="stopped")
    return last_result or CaptchaIdentifyResult(ok=False, error=last_err)


def wait_enter_result(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    start_scene: int | None,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
    entry_block_baseline: dict[str, int] | None = None,
    answer_baseline: dict[str, int] | None = None,
    on_stage1=None,
) -> tuple[bool, int | None, str, CaptchaAnswerFeedback | None]:
    """
    After captcha submit: two-stage feedback + scene enter.

    Stage 1 (must resolve first):
      答案错误 -> fail (re-answer)
      答案正确 -> stage1 passed only

    Stage 2 (only after stage1 ok):
      神罚/捕羽 -> hard block (stop permanently)
      other refuse toast -> soft (temporary cannot enter)
      no second toast -> can enter / transferring

    Final success only when scene leaves Fuzhou into 妖楼.
    answer_baseline is optional diagnostic only; the timed path uses UI probes
    (heavy=False). Do not snapshot process-wide text on the hot path.
    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    quiet = lambda _m: None  # noqa: E731
    last_sid: int | None = None
    last_label = ""
    ans_base = answer_baseline
    deadline = time.monotonic() + max(1.0, float(cfg.enter_timeout_s))
    last_countdown_s: int | None = None

    def _authoritative_tap(feedback, kind: str) -> bool:
        """True when this verdict came from the post-submit DLL event stream."""
        if feedback is None or feedback.kind != kind:
            return False
        return f"chat_tap_{kind}" in str(feedback.method or "")

    def _countdown() -> None:
        """
        Publish the shared post-confirm countdown once per changed second.

        @author by ak
        """
        nonlocal last_countdown_s
        remain = max(0, int(math.ceil(deadline - time.monotonic())))
        if remain == last_countdown_s:
            return
        last_countdown_s = remain
        status(f"等待系统消息/进图，剩余 {remain}s")

    def _scene_ok() -> bool:
        nonlocal last_sid, last_label
        sid, _pos, label = read_scene_state(session, log=quiet)
        last_sid, last_label = sid, label or ""
        if sid is None:
            return False
        if is_yaolu_scene(sid, label):
            return True
        if start_scene is not None and int(sid) != int(start_scene):
            if not is_fuzhou_scene(sid, label):
                return True
        return False

    # One shared monotonic budget covers feedback, stage2, and scene transfer.
    _countdown()
    fb = wait_captcha_answer_feedback(
        session,
        timeout_s=float(cfg.enter_timeout_s),
        poll_s=0.22,
        stop_event=stop_event,
        log=log,
        status=status,
        countdown=_countdown,
        deadline=deadline,
        scene_ok_fn=_scene_ok,
        entry_block_baseline=entry_block_baseline,
        answer_baseline=ans_base,
        stage2_hold_s=4.5,
            on_stage1=on_stage1,
    )
    if fb.kind == "stopped":
        return False, last_sid, "stopped", fb
    if fb.kind == "block":
        # Hard 神罚/捕羽 (or unclassified block without proven ok).
        status(fb.text or SHENFA_BLOCK_TEXT)
        log(
            f"captcha answer BLOCK via={fb.method} text={fb.text!r} "
            f"scene={last_sid} {last_label}"
        )
        return False, last_sid, fb.text or SHENFA_BLOCK_TEXT, fb
    if fb.kind == "fail":
        status(fb.text or CAPTCHA_FAIL_TEXT)
        log(
            f"captcha answer FAIL via={fb.method} text={fb.text!r} "
            f"scene={last_sid} {last_label}"
        )
        if _authoritative_tap(fb, "fail"):
            log("captcha authoritative tap FAIL; skip legacy scene grace")
            return False, last_sid, fb.text or CAPTCHA_FAIL_TEXT, fb
        # Soft-confirm: hist may lag / false-fail while entry cast already runs.
        entered, esid, elabel = wait_enter_while_transfer(
            session,
            cfg,
            start_scene=start_scene,
            stop_event=stop_event,
            log=log,
            status=status,
            reason="stage1_fail_grace",
        )
        if entered:
            ok_fb = CaptchaAnswerFeedback(
                kind="ok",
                text=CAPTCHA_OK_TEXT,
                method=f"grace_after_fail:{fb.method or 'stage1'}",
                error=fb.text,
            )
            return True, esid, elabel or CAPTCHA_OK_TEXT, ok_fb
        return False, last_sid, fb.text or CAPTCHA_FAIL_TEXT, fb
    if fb.kind == "ok":
        # Stage1+stage2 clear (or soft refuse in fb.error): only scene = entered.
        if _scene_ok():
            status(f"已进入 {last_label}" if last_label else "答案正确已进图")
            return True, last_sid, last_label or CAPTCHA_OK_TEXT, fb
        log(
            f"captcha stage ok, wait scene transfer: {fb.method} "
            f"soft={fb.error!r}"
        )
        if fb.error:
            soft = str(fb.error)
            if "不稳定" in soft:
                status(
                    f"答案正确，跨图后不稳定状态：{soft}；状态栏等待进图/稳定"
                )
            else:
                status(
                    f"答案正确但暂不可进: {soft}；继续等待进图/后续提示"
                )
        else:
            status("答案正确且无硬拦截，等待进入妖楼…")

    # Continue only within the same post-confirm deadline.
    while time.monotonic() < deadline:
        _countdown()
        if stop_event is not None and stop_event.is_set():
            return False, last_sid, "stopped", fb
        if _scene_ok():
            status(f"已进入 {last_label}")
            ok_fb = CaptchaAnswerFeedback(
                kind="ok",
                text=CAPTCHA_OK_TEXT,
                method="scene_late",
            )
            return True, last_sid, last_label, ok_fb
        # Re-probe late stage2 (神罚 often lags 答案正确 by > hold window).
        from app.core.game_sys_msg import probe_captcha_answer_text

        hit = probe_captcha_answer_text(
            session,
            log=log,
            entry_block_baseline=entry_block_baseline,
            answer_baseline=ans_base,
            heavy=False,
            # Stage1 may already be ok — trust chat-only 神罚 without dual line.
            trust_chat_hard=bool(
                fb is not None
                and (
                    fb.kind == "ok"
                    or is_explicit_captcha_ok_feedback(fb)
                )
            ),
        )
        if hit is not None and hit.kind == "block":
            status(hit.text or SHENFA_BLOCK_TEXT)
            if is_shenfa_block_feedback(hit):
                # Permanent party debuff: surface block, keep proven ok text.
                return (
                    False,
                    last_sid,
                    hit.text or SHENFA_BLOCK_TEXT,
                    CaptchaAnswerFeedback(
                        kind="block",
                        text=hit.text or SHENFA_BLOCK_TEXT,
                        method=f"stage2_hard_late:{hit.method or 'shenfa'}",
                        error=(
                            fb.text
                            if is_explicit_captcha_ok_feedback(fb)
                            else hit.error
                        ),
                    ),
                )
            if is_explicit_captcha_ok_feedback(fb) or (
                fb is not None and fb.kind == "ok"
            ):
                # Soft entry-condition toast after correct answer (retry later).
                soft_txt = hit.text or ENTRY_OPEN_BLOCK_TEXT
                if "不稳定" in str(soft_txt):
                    status(f"答案正确但不稳定状态：{soft_txt}")
                proven_ok = CaptchaAnswerFeedback(
                    kind="ok",
                    text=fb.text or CAPTCHA_OK_TEXT,
                    msg_id=fb.msg_id or CAPTCHA_OK_MSG_ID,
                    method=f"{fb.method}+stage2_soft_late:{hit.method}",
                    error=hit.text,
                )
                return False, last_sid, soft_txt, proven_ok
            return False, last_sid, hit.text or ENTRY_OPEN_BLOCK_TEXT, hit
        if hit is not None and hit.kind == "fail":
            # Do not demote a proven-ok answer to fail from stale catalog text.
            if is_explicit_captcha_ok_feedback(fb) or (
                fb is not None and fb.kind == "ok"
            ):
                log(
                    f"captcha ignore fail after proven ok via={hit.method} "
                    f"text={hit.text!r}"
                )
            else:
                status(hit.text or CAPTCHA_FAIL_TEXT)
                if _authoritative_tap(hit, "fail"):
                    log("captcha authoritative late tap FAIL; skip legacy scene grace")
                    return False, last_sid, hit.text or CAPTCHA_FAIL_TEXT, hit
                entered, esid, elabel = wait_enter_while_transfer(
                    session,
                    cfg,
                    start_scene=start_scene,
                    stop_event=stop_event,
                    log=log,
                    status=status,
                    reason="late_fail_grace",
                )
                if entered:
                    ok_fb = CaptchaAnswerFeedback(
                        kind="ok",
                        text=CAPTCHA_OK_TEXT,
                        method=f"grace_after_fail:{hit.method or 'late'}",
                        error=hit.text,
                    )
                    return True, esid, elabel or CAPTCHA_OK_TEXT, ok_fb
                return False, last_sid, hit.text or CAPTCHA_FAIL_TEXT, hit
        if fb is not None and fb.kind == "ok":
            soft = str(fb.error or "")
            if soft and "不稳定" in soft:
                status(
                    f"答案正确·等待稳定/进图… {last_label} scene={last_sid} "
                    f"({soft})"
                )
            elif soft:
                status(
                    f"答案正确，等待进图… {last_label} scene={last_sid} soft={soft}"
                )
            else:
                status(f"答案正确，等待进图… {last_label} scene={last_sid}")
        else:
            status(f"等待系统消息/进图… {last_label} scene={last_sid}")
        remain = max(0.0, deadline - time.monotonic())
        if remain <= 0:
            break
        if not _sleep_interruptible(min(0.55, remain), stop_event):
            return False, last_sid, "stopped", fb
    _countdown()
    if _authoritative_tap(fb, "ok"):
        log("captcha authoritative tap OK but scene unchanged at deadline; skip timeout grace")
        return False, last_sid, last_label or "timeout", fb
    # Timeout: if cast/bar still running, give transfer one last chance.
    entered, esid, elabel = wait_enter_while_transfer(
        session,
        cfg,
        start_scene=start_scene,
        stop_event=stop_event,
        log=log,
        status=status,
        reason="enter_timeout_grace",
    )
    if entered:
        ok_fb = CaptchaAnswerFeedback(
            kind="ok",
            text=CAPTCHA_OK_TEXT,
            method="grace_after_timeout",
            error=last_label or "timeout",
        )
        return True, esid, elabel or CAPTCHA_OK_TEXT, ok_fb
    return False, last_sid, last_label or "timeout", fb


def _cast_state_text(state: dict) -> str:
    """Compact read-only cast snapshot for recovery diagnostics."""
    return (
        f"readable={bool(state.get('readable'))} "
        f"active={bool(state.get('active'))} "
        f"session=0x{int(state.get('session_state') or 0):X} "
        f"cast=0x{int(state.get('cast') or 0):X} "
        f"id=0x{int(state.get('skill_id') or 0):X}/"
        f"0x{int(state.get('skill_id_b') or 0):X} "
        f"flags=0x{int(state.get('flags') or 0):X} "
        f"elapsed={int(state.get('elapsed') or 0)}"
    )


def _cast_session_busy_state(st: dict | None) -> bool:
    """True when skill cast-this or host+0x41C session gate is busy."""
    if not st:
        return False
    return bool(st.get("active") or st.get("session_active"))



def force_wrong_captcha_submit(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    hwnd: int = 0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    LAST-RESORT only: random 2 cells + confirm to dismiss stuck captcha.

    Prefer close_captcha_dialog. YaoluRunner does NOT call this on the normal
    path — wrong submits pollute captcha-service feedback audit. Kept for
    manual/debug use.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "was_open": False,
        "submitted": False,
        "positions": [],
        "clicks": 0,
        "confirm": False,
        "error": None,
        "method": "random_wrong+confirm",
    }
    if session is None:
        out["error"] = "no session"
        return out
    try:
        hit = is_captcha_dialog_open(
            session, names=cfg.captcha_dlg_names, log=lambda _m: None
        )
    except Exception as e:
        out["error"] = f"dlg check err: {e}"
        return out
    out["was_open"] = bool(hit.shown)
    if not hit.shown:
        out["ok"] = True
        out["error"] = "already_closed"
        out["method"] = "already_closed"
        return out
    if stop_event is not None and stop_event.is_set():
        out["error"] = "stopped"
        return out

    # Two distinct cells 1..8; intentionally not from identify API.
    positions = random.sample(range(1, 9), 2)
    out["positions"] = list(positions)
    log(f"yaolu force-wrong captcha submit positions={positions}")
    clicks = click_captcha_cells(
        session,
        int(hwnd or 0),
        positions,
        prefer_bridge=bool(cfg.use_bridge),
        prefer_post=bool(cfg.prefer_post_click),
        allow_cursor=bool(cfg.allow_cursor_click),
        humanize=bool(cfg.captcha_humanize_clicks),
        names=cfg.captcha_dlg_names,
        log=log,
    )
    out["clicks"] = sum(1 for _x, _y, ok in clicks if ok)
    if out["clicks"] < 1:
        out["error"] = "cell click failed"
        return out
    gap = max(0.05, float(cfg.captcha_click_gap_s) + random.uniform(0.0, float(cfg.captcha_click_gap_jitter_s)))
    if not _sleep_interruptible(gap, stop_event):
        out["error"] = "stopped"
        return out
    conf = click_captcha_btn_ok(
        session,
        int(hwnd or 0),
        prefer_bridge=bool(cfg.use_bridge),
        prefer_post=bool(cfg.prefer_post_click),
        allow_cursor=bool(cfg.allow_cursor_click),
        humanize=bool(cfg.captcha_humanize_clicks),
        pure_submit=False,
        names=cfg.captcha_dlg_names,
        log=log,
    )
    out["confirm"] = bool(conf)
    out["submitted"] = bool(conf) or out["clicks"] >= 2
    # Brief settle so client can reject answer / drop dialog before bar clear.
    _sleep_interruptible(0.45, stop_event)
    try:
        hit2 = is_captcha_dialog_open(
            session, names=cfg.captcha_dlg_names, log=lambda _m: None
        )
        out["still_open"] = bool(hit2.shown)
        out["ok"] = not bool(hit2.shown) or bool(conf)
    except Exception:
        out["still_open"] = None
        out["ok"] = bool(conf)
    log(
        f"yaolu force-wrong result ok={out['ok']} clicks={out['clicks']} "
        f"confirm={out['confirm']} still_open={out.get('still_open')}"
    )
    if not out["ok"] and not out.get("error"):
        out["error"] = "force wrong submit did not dismiss dialog"
    return out



def _read_u32(session: GameAttachSession, addr: int) -> int:
    """Read remote u32. @author by ak"""
    addr = int(addr) & 0xFFFFFFFF
    if not addr:
        return 0
    pm = getattr(session, "pm", None)
    if pm is None:
        return 0
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(pm.process_handle, addr, 4)
        import struct

        return int(struct.unpack_from("<I", raw, 0)[0]) & 0xFFFFFFFF
    except Exception:
        return 0


def _read_u8(session: GameAttachSession, addr: int) -> int:
    """Read remote u8. @author by ak"""
    addr = int(addr) & 0xFFFFFFFF
    if not addr:
        return 0
    pm = getattr(session, "pm", None)
    if pm is None:
        return 0
    try:
        import pymem.memory

        raw = pymem.memory.read_bytes(pm.process_handle, addr, 1)
        return int(raw[0]) & 0xFF
    except Exception:
        return 0


def _write_u32(session: GameAttachSession, addr: int, value: int) -> bool:
    """Write remote u32 through the scene-gated runtime. @author by ak"""
    addr = int(addr) & 0xFFFFFFFF
    pid = int(getattr(session, "pid", 0) or 0)
    if not addr or not pid:
        return False
    try:
        import struct
        from app.core.remote_runtime import remote_write_bytes

        raw = struct.pack("<I", int(value) & 0xFFFFFFFF)
        return remote_write_bytes(
            pid,
            addr,
            raw,
        ) == len(raw)
    except Exception:
        return False


def _write_u8(session: GameAttachSession, addr: int, value: int) -> bool:
    """Write remote u8 through the scene-gated runtime. @author by ak"""
    addr = int(addr) & 0xFFFFFFFF
    pid = int(getattr(session, "pid", 0) or 0)
    if not addr or not pid:
        return False
    try:
        from app.core.remote_runtime import remote_write_bytes

        raw = bytes([int(value) & 0xFF])
        return remote_write_bytes(pid, addr, raw) == 1
    except Exception:
        return False


def get_host_ptr(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
    prefer_call: bool = True,
) -> dict:
    """
    Resolve host player object (preferred note VA 0x4AE400).

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {"ok": False, "host": 0, "method": "", "error": None}
    pid = int(getattr(session, "pid", 0) or 0)
    if prefer_call and pid:
        try:
            from app.core.remote_runtime import remote_call_cdecl_x86

            va = _yaolu_live_va(session, NOTE_VA_GET_HOST)
            host = int(remote_call_cdecl_x86(pid, va, (), timeout_ms=2500)) & 0xFFFFFFFF
            if host:
                out["ok"] = True
                out["host"] = host
                out["method"] = f"call:0x{NOTE_VA_GET_HOST:X}"
                return out
        except Exception as e:
            out["error"] = f"call host: {e}"
            log(f"get_host_ptr call err: {e}")
    try:
        g = _read_u32(session, _yaolu_live_va(session, NOTE_VA_GLOBAL_ROOT))
        a24 = _read_u32(session, g + 0x24) if g else 0
        host = _read_u32(session, a24 + 0x8C) if a24 else 0
        if host:
            out["ok"] = True
            out["host"] = int(host) & 0xFFFFFFFF
            out["method"] = "mem:global+0x24+0x8C"
            out["error"] = None
        else:
            if not out.get("error"):
                out["error"] = f"host=0 g=0x{g:X} a24=0x{a24:X}"
    except Exception as e:
        out["error"] = f"mem host: {e}"
        log(f"get_host_ptr mem err: {e}")
    return out


def _perform_type_is_matter(ptype: int | None) -> bool:
    """True if perform type is matter-interact family 0x66..0x6b. @author by ak"""
    try:
        return int(ptype) in PERFORM_TYPES_MATTER
    except Exception:
        return False



def wait_enter_while_transfer(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    start_scene: int | None,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
    grace_s: float | None = None,
    reason: str = "fail_grace",
) -> tuple[bool, int | None, str]:
    """
    After 答案错误 / enter timeout: full grace watching **scene change only**.

    Rationale: chat hist may miss 答案正确 or false-hit 答案错误 while the client
    is already transferring. Do **not** depend on progress bar/perform. If still not in 妖楼 when grace ends -> caller fails.

    Returns (entered, scene_id, label). Does not cancel perform/bar.
    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    grace = float(
        grace_s if grace_s is not None else getattr(cfg, "enter_fail_grace_s", 14.0)
    )
    grace = max(0.0, grace)
    quiet = lambda _m: None  # noqa: E731
    last_sid: int | None = None
    last_label = ""
    last_report = -1
    start_sid = start_scene

    def _scene_entered() -> bool:
        nonlocal last_sid, last_label
        sid, _pos, label = read_scene_state(session, log=quiet)
        last_sid, last_label = sid, label or ""
        if sid is None:
            return False
        if is_yaolu_scene(sid, label):
            return True
        # Left Fuzhou into another non-Fuzhou map counts as transfer success.
        base = start_sid
        if base is None:
            return False
        try:
            if int(sid) != int(base) and not is_fuzhou_scene(sid, label):
                return True
        except (TypeError, ValueError):
            return False
        return False

    # Snapshot start scene if caller did not pass one.
    if start_sid is None:
        try:
            start_sid, _, _ = read_scene_state(session, log=quiet)
        except Exception:
            start_sid = None

    if _scene_entered():
        status(f"已进入 {last_label or '妖楼'}（{reason} 中途纠偏）")
        log(f"yaolu grace already-in scene={last_sid} {last_label} via={reason}")
        return True, last_sid, last_label

    if grace <= 0.05:
        return False, last_sid, last_label

    status(
        f"答案/进图待确认宽限 {grace:.0f}s：只看是否换图进妖楼（{reason}）"
    )
    log(
        f"yaolu fail-grace start {grace:.1f}s via={reason} "
        f"start_scene={start_sid} (scene-only, no bar gate)"
    )
    deadline = time.monotonic() + grace

    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False, last_sid, last_label
        if _scene_entered():
            status(f"已进入 {last_label or '妖楼'}（宽限中纠偏）")
            log(
                f"yaolu grace entered scene={last_sid} {last_label} "
                f"via={reason} remain={deadline - time.monotonic():.1f}s"
            )
            return True, last_sid, last_label
        remain = max(0, int(math.ceil(deadline - time.monotonic())))
        if remain != last_report:
            last_report = remain
            status(
                f"进图宽限中… 剩余 {remain}s scene={last_sid} {last_label}（只看换图）"
            )
        if not _sleep_interruptible(0.5, stop_event):
            return False, last_sid, last_label

    _scene_entered()
    log(
        f"yaolu fail-grace end no-enter via={reason} "
        f"scene={last_sid} {last_label} start={start_sid}"
    )
    return False, last_sid, last_label


def read_host_perform_state(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
) -> dict:
    """
    Snapshot host perform slot that keeps Win_Prgs2 shown.

    PE: progress tick 0x8862E0 re-Shows bar while any 0x72A380.. check is true,
    all of which read perform_mgr=host+0x270 and curr=*(mgr+8).

    Notes (live 2026-07-20):
      - type=2 is the normal base locomotion perform on healthy chars.
      - side flags non-zero are normal; do NOT treat as move-lock.
      - bare nulling mgr+8 removes type=2 and breaks HostMove/click-to-move.
      - locked_hint only for matter types / session gates / missing base slot.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "host": 0,
        "perform_mgr": 0,
        "curr_perform": 0,
        "perform_type": None,
        "perform_subtype": None,
        "side": 0,
        "flag0": None,
        "flag1": None,
        "session_gate": None,
        "entity": 0,
        "move_ctrl": 0,
        "active": False,
        "is_matter": False,
        "is_base": False,
        "base_missing": False,
        "locked_hint": False,
        "error": None,
    }
    try:
        gh = get_host_ptr(session, log=log, prefer_call=True)
        host = int(gh.get("host") or 0) & 0xFFFFFFFF
        out["host"] = host
        if not host:
            out["error"] = gh.get("error") or "host=0"
            return out
        out["session_gate"] = _read_u32(session, host + HOST_OFF_SESSION_GATE)
        out["session_gate1"] = _read_u32(session, host + HOST_OFF_SESSION_GATE + 4)
        out["session_gate2"] = _read_u32(session, host + HOST_OFF_SESSION_GATE + 8)
        out["move_ctrl"] = _read_u32(session, host + HOST_OFF_MOVE_CTRL)
        mgr = _read_u32(session, host + HOST_OFF_PERFORM_MGR)
        out["perform_mgr"] = int(mgr) & 0xFFFFFFFF
        if not mgr:
            out["ok"] = True
            out["active"] = False
            out["base_missing"] = True
            gate_on = bool(
                int(out.get("session_gate") or 0)
                or int(out.get("session_gate1") or 0)
                or int(out.get("session_gate2") or 0)
            )
            out["locked_hint"] = True  # no mgr => cannot move safely
            out["error"] = None
            return out
        curr = _read_u32(session, mgr + PERFORM_MGR_OFF_CURR)
        side = _read_u32(session, mgr + PERFORM_MGR_OFF_SIDE)
        ent = _read_u32(session, mgr + PERFORM_MGR_OFF_ENTITY)
        out["curr_perform"] = int(curr) & 0xFFFFFFFF
        out["side"] = int(side) & 0xFFFFFFFF
        out["entity"] = int(ent) & 0xFFFFFFFF
        if curr:
            ptype = _read_u32(session, curr + PERFORM_OFF_TYPE)
            out["perform_type"] = ptype
            out["perform_subtype"] = _read_u32(session, curr + PERFORM_OFF_SUBTYPE)
            out["perform_flag_2c"] = _read_u8(session, curr + 0x2C)
            out["perform_flag_2d"] = _read_u8(session, curr + 0x2D)
            out["perform_host"] = _read_u32(session, curr + 0x1C)
            out["active"] = True
            out["is_matter"] = _perform_type_is_matter(ptype)
            out["is_base"] = int(ptype) == int(PERFORM_TYPE_BASE_LOCOMOTION)
        else:
            out["base_missing"] = True
        if side:
            out["flag0"] = _read_u8(session, side + PERFORM_SIDE_OFF_FLAG0)
            out["flag1"] = _read_u8(session, side + PERFORM_SIDE_OFF_FLAG1)
        gate_on = bool(
            int(out.get("session_gate") or 0)
            or int(out.get("session_gate1") or 0)
            or int(out.get("session_gate2") or 0)
        )
        # Move-lock hint: matter interact / session gate / missing locomotion slot.
        # type=2 + random side flags are healthy idle and must NOT count as locked.
        out["locked_hint"] = bool(out.get("is_matter") or gate_on or out.get("base_missing"))
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)
        log(f"read_host_perform_state err: {e}")
    return out


def ensure_base_locomotion_perform(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
) -> dict:
    """
    Ensure perform_mgr curr is base locomotion type=2 via mgr.vt+0x1C(2,0).

    PE: 0x79E480 factory-install; live host init uses (type=2, arg=0).
    Use after accidental slot clear or when curr is empty.
    Does NOT guarantee full move recovery if deeper controllers were corrupted.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "before": None,
        "after": None,
        "method": "",
        "ret": None,
        "error": None,
    }
    before = read_host_perform_state(session, log=log)
    out["before"] = before
    if not before.get("ok"):
        out["error"] = before.get("error") or "perform state unreadable"
        return out
    if before.get("is_base") and before.get("active"):
        out["ok"] = True
        out["after"] = before
        out["method"] = "already_base"
        out["note"] = "type=2 already installed"
        return out
    # If a non-matter perform is present (e.g. skill type 3), leave it.
    if before.get("active") and not before.get("is_matter") and not before.get("base_missing"):
        out["ok"] = True
        out["after"] = before
        out["method"] = "non_matter_present"
        out["note"] = f"curr type={before.get('perform_type')} kept"
        return out
    mgr = int(before.get("perform_mgr") or 0) & 0xFFFFFFFF
    pid = int(getattr(session, "pid", 0) or 0)
    if not mgr or not pid:
        out["error"] = "mgr/pid missing"
        return out
    try:
        from app.core.remote_runtime import remote_call_thiscall_x86

        mvt = _read_u32(session, mgr)
        fn = _read_u32(session, (mvt + PERFORM_MGR_VT_INSTALL_OFF) & 0xFFFFFFFF) if mvt else 0
        if not fn:
            out["error"] = "mgr.vt+0x1C null"
            return out
        ret = remote_call_thiscall_x86(
            pid,
            int(fn) & 0xFFFFFFFF,
            mgr,
            [int(PERFORM_TYPE_BASE_LOCOMOTION), 0],
            caller_cleanup=False,
            timeout_ms=4000,
        )
        out["ret"] = ret
        out["method"] = f"mgr.vt+0x1C(2,0) fn=0x{fn:X}"
        time.sleep(0.12)
        after = read_host_perform_state(session, log=lambda _m: None)
        out["after"] = after
        out["ok"] = bool(after.get("active") and not after.get("base_missing"))
        if out["ok"]:
            out["note"] = (
                f"installed type={after.get('perform_type')} "
                f"curr=0x{int(after.get('curr_perform') or 0):X}"
            )
        else:
            out["error"] = (
                f"install failed type={after.get('perform_type')} "
                f"curr=0x{int(after.get('curr_perform') or 0):X} ret={ret}"
            )
        log(
            f"ensure_base_locomotion ok={out['ok']} method={out['method']} "
            f"ret={ret} after_type={after.get('perform_type')} err={out.get('error')!r}"
        )
    except Exception as e:
        out["error"] = str(e)
        log(f"ensure_base_locomotion err: {e}")
    return out


def probe_host_move(
    session: GameAttachSession,
    *,
    dist: float = 2.5,
    mode: int = 0,
    wait_s: float = 1.0,
    log: LogFn | None = None,
) -> dict:
    """
    Issue a short HostMove and report whether live coords actually change.

    Distinguishes API ok from real locomotion. @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "moved": False,
        "delta": 0.0,
        "ret": None,
        "before_pos": None,
        "after_pos": None,
        "perform_before": None,
        "perform_after": None,
        "error": None,
    }
    try:
        from app.core.automove import PathTarget, host_move_to, read_scene_position

        out["perform_before"] = read_host_perform_state(session, log=lambda _m: None)
        pos = read_scene_position(session, log=lambda _m: None)
        if not getattr(pos, "ok", False) or not getattr(pos, "scene_pos", None):
            out["error"] = f"pos unreadable: {getattr(pos, 'error', None)}"
            return out
        x, y, z = pos.scene_pos
        out["before_pos"] = (float(x), float(y), float(z))
        out["scene_id"] = getattr(pos, "scene_id", None)
        tgt = PathTarget(x=float(x) + float(dist), y=float(y), z=float(z), mode=int(mode))
        mv = host_move_to(session, tgt, log=log)
        out["ret"] = getattr(mv, "ret", None)
        out["move_ok"] = bool(getattr(mv, "ok", False))
        out["move_note"] = getattr(mv, "note", None)
        time.sleep(max(0.2, float(wait_s)))
        pos2 = read_scene_position(session, log=lambda _m: None)
        if getattr(pos2, "scene_pos", None):
            x2, y2, z2 = pos2.scene_pos
            out["after_pos"] = (float(x2), float(y2), float(z2))
            dx = float(x2) - float(x)
            dz = float(z2) - float(z)
            out["delta"] = float((dx * dx + dz * dz) ** 0.5)
            out["moved"] = bool(out["delta"] >= 0.35)
        out["perform_after"] = read_host_perform_state(session, log=lambda _m: None)
        out["ok"] = True
        out["error"] = None
        log(
            f"probe_host_move moved={out['moved']} delta={out['delta']:.3f} "
            f"ret={out['ret']} type={out['perform_before'].get('perform_type')}->"
            f"{(out.get('perform_after') or {}).get('perform_type')}"
        )
    except Exception as e:
        out["error"] = str(e)
        log(f"probe_host_move err: {e}")
    return out


def stop_host_perform(
    session: GameAttachSession,
    *,
    force_clear_slot: bool = False,
    hwnd: int = 0,
    use_bridge_cancel: bool = True,
    restore_base: bool = True,
    log: LogFn | None = None,
) -> dict:
    """
    Stop host matter-perform and unlock character control.

    Live (2026-07-20):
      - type=103 (0x67) matter interact keeps Win_Prgs2 via progress tick
      - type=2 is normal base locomotion; healthy chars always have some curr
      - bare *(mgr+8)=0 HIDES the bar but DESTROYS locomotion -> HostMove
        returns ok yet coords never change; click-to-move also dies
      - proper end for type 0x67:
          0x772B60(perform) -> CancelAction + vt+0x6C(0,1,-1,0xC8)
        plus clear host+0x41C..424, SetTarget(0), bridge 0x21
      - force_clear_slot only allowed for matter types, and MUST restore type=2

    @author by ak
    """
    log = log or (lambda _m: None)
    before = read_host_perform_state(session, log=log)
    out: dict = {
        "ok": False,
        "before": before,
        "after": before,
        "method": "",
        "steps": [],
        "vt_fn": 0,
        "unlock_fn": 0,
        "end_fn": 0,
        "slot_cleared": False,
        "gate_cleared": False,
        "entity_unlock": False,
        "cancel21": None,
        "error": None,
        "needed": bool(
            before.get("is_matter")
            or before.get("base_missing")
            or int(before.get("session_gate") or 0)
            or int(before.get("session_gate1") or 0)
            or int(before.get("session_gate2") or 0)
        ),
        "restored_base": False,
    }
    if not before.get("ok"):
        out["error"] = before.get("error") or "perform state unreadable"
        return out

    mgr = int(before.get("perform_mgr") or 0) & 0xFFFFFFFF
    side = int(before.get("side") or 0) & 0xFFFFFFFF
    curr = int(before.get("curr_perform") or 0) & 0xFFFFFFFF
    host = int(before.get("host") or 0) & 0xFFFFFFFF
    entity = int(before.get("entity") or 0) & 0xFFFFFFFF
    ptype = before.get("perform_type")
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        out["error"] = "pid missing"
        return out

    from app.core.remote_runtime import remote_call_thiscall_x86

    def _step(name: str, **kw) -> None:
        row = {"name": name, **kw}
        out["steps"].append(row)
        log(f"stop_host_perform step {name} {kw}")

    # Healthy base locomotion + no session gate: do nothing (avoid thrashing type=2).
    if not out.get("needed"):
        out["ok"] = True
        out["unlocked"] = True
        out["method"] = "noop_healthy"
        out["note"] = (
            f"no matter/gate lock type={before.get('perform_type')} "
            f"base_missing={before.get('base_missing')}"
        )
        out["after"] = before
        log(
            f"stop_host_perform RESULT ok=True unlocked=True method=noop_healthy "
            f"type={before.get('perform_type')} note={out['note']!r}"
        )
        return out

    # --- A) HostStopSession native matter stop (type 0x67 -> 0x772B60) ---
    if curr and ptype is not None and int(ptype) == 0x67:
        try:
            fn67 = _yaolu_live_va(session, NOTE_VA_STOP_PERFORM_MATTER)
            remote_call_thiscall_x86(
                pid,
                int(fn67) & 0xFFFFFFFF,
                curr,
                (),
                caller_cleanup=False,
                timeout_ms=4000,
            )
            out["method"] = "0x772B60(type67)"
            _step("native_772B60", fn=hex(fn67), type=ptype, curr=hex(curr))
        except Exception as e:
            _step("native_772B60_err", error=str(e))
            log(f"stop_host_perform 0x772B60 err: {e}")

    # --- A1) native CancelAction 0xCC7010(1,0) via global+0x1C8 (same as 772B60 tail) ---
    if curr and _perform_type_is_matter(ptype):
        try:
            from app.core.remote_runtime import remote_call_cdecl_x86, remote_call_thiscall_x86 as _tc

            gfn = _yaolu_live_va(session, NOTE_VA_GET_CANCEL_CTX)
            gctx = int(remote_call_cdecl_x86(pid, int(gfn) & 0xFFFFFFFF, (), timeout_ms=2000)) & 0xFFFFFFFF
            cthis = (gctx + 0x1C8) & 0xFFFFFFFF if gctx else 0
            cfn = _yaolu_live_va(session, NOTE_VA_CANCEL_ACTION)
            if cthis and cfn:
                cret = remote_call_thiscall_x86(
                    pid,
                    int(cfn) & 0xFFFFFFFF,
                    cthis,
                    [1, 0],
                    caller_cleanup=False,
                    timeout_ms=3000,
                )
                out["method"] = (out.get("method") or "") + "+CC7010"
                _step("cancel_action_cc7010", fn=hex(cfn), this=hex(cthis), ret=cret)
                if curr:
                    _write_u8(session, curr + 0x2C, 1)
            else:
                _step("cancel_action_skip", gctx=hex(gctx), cthis=hex(cthis))
        except Exception as e:
            _step("cancel_action_err", error=str(e))
            log(f"stop_host_perform CC7010 err: {e}")

    # --- A2) generic matter end: vt+0x0C(1) / vt+0x6C ---
    # Re-read curr in case 772B60 already dropped it.
    if mgr:
        curr = _read_u32(session, mgr + PERFORM_MGR_OFF_CURR)
        if curr:
            ptype = _read_u32(session, curr + PERFORM_OFF_TYPE)
    if curr and _perform_type_is_matter(ptype):
        try:
            pvt = _read_u32(session, curr)
            end_fn = _read_u32(session, (pvt + PERFORM_OBJ_VT_END_OFF) & 0xFFFFFFFF) if pvt else 0
            unlock_fn = (
                _read_u32(session, (pvt + PERFORM_OBJ_VT_UNLOCK_OFF) & 0xFFFFFFFF) if pvt else 0
            )
            out["end_fn"] = int(end_fn) & 0xFFFFFFFF
            out["unlock_fn"] = int(unlock_fn) & 0xFFFFFFFF
            if unlock_fn:
                # HostStopSession 0x772B60 tail: vt+0x6C(0,1,-1,0xC8)
                remote_call_thiscall_x86(
                    pid,
                    int(unlock_fn) & 0xFFFFFFFF,
                    curr,
                    [0, 1, 0xFFFFFFFF, 0xC8],
                    caller_cleanup=False,
                    timeout_ms=3000,
                )
                out["method"] = (out.get("method") or "") + "+vt+0x6C"
                _step("perform_unlock_6c", fn=hex(unlock_fn), type=ptype)
            elif end_fn:
                remote_call_thiscall_x86(
                    pid,
                    int(end_fn) & 0xFFFFFFFF,
                    curr,
                    [1],
                    caller_cleanup=False,
                    timeout_ms=3000,
                )
                out["method"] = (out.get("method") or "") + "+vt+0x0C(1)"
                _step("perform_end", fn=hex(end_fn), arg=1, type=ptype)
            # Mark cancel-sent flag like 0x772B60 does after 0xCC7010.
            _write_u8(session, curr + 0x2C, 1)
        except Exception as e:
            _step("perform_end_err", error=str(e))
            log(f"stop_host_perform matter-end err: {e}")

    # --- B) HostStopSession-style mgr stop ---
    if mgr:
        try:
            if side:
                _write_u8(session, side + PERFORM_SIDE_OFF_FLAG0, 0)
                _write_u8(session, side + PERFORM_SIDE_OFF_FLAG1, 0)
                _step("side_flags", flag0=0, flag1=0, side=hex(side))
            mvt = _read_u32(session, mgr)
            fn = _read_u32(session, (mvt + PERFORM_STOP_VT_OFF) & 0xFFFFFFFF) if mvt else 0
            out["vt_fn"] = int(fn) & 0xFFFFFFFF
            if fn:
                remote_call_thiscall_x86(
                    pid,
                    int(fn) & 0xFFFFFFFF,
                    mgr,
                    [int(PERFORM_STOP_ARG)],
                    caller_cleanup=False,
                    timeout_ms=3000,
                )
                out["method"] = (out.get("method") or "") + "+mgr.vt+0x10(0x65)"
                _step("mgr_stop65", fn=hex(fn))
        except Exception as e:
            _step("mgr_stop_err", error=str(e))
            log(f"stop_host_perform mgr-stop err: {e}")

    # --- C) clear full host session triple (+0x41C/+420/+424) ---
    if host:
        try:
            g0 = _read_u32(session, host + HOST_OFF_SESSION_GATE)
            g1 = _read_u32(session, host + HOST_OFF_SESSION_GATE + 4)
            g2 = _read_u32(session, host + HOST_OFF_SESSION_GATE + 8)
            if g0 or g1 or g2:
                ok0 = _write_u32(session, host + HOST_OFF_SESSION_GATE, 0)
                ok1 = _write_u32(session, host + HOST_OFF_SESSION_GATE + 4, 0)
                ok2 = _write_u32(session, host + HOST_OFF_SESSION_GATE + 8, 0)
                out["gate_cleared"] = bool(ok0 and ok1 and ok2)
                out["method"] = (out.get("method") or "") + "+clear_gate12"
                _step(
                    "clear_gate12",
                    before=[hex(g0), hex(g1), hex(g2)],
                    ok=out["gate_cleared"],
                )
        except Exception as e:
            _step("gate_err", error=str(e))

    # --- D) clear current target (MatterInteract SetTarget residue) ---
    try:
        from app.core.plg_interact import set_target

        st = set_target(session, 0, log=lambda _m: None)
        out["clear_target"] = {
            "ok": bool(getattr(st, "ok", False)),
            "ret": getattr(st, "ret", None),
            "error": getattr(st, "error", None),
        }
        out["method"] = (out.get("method") or "") + "+SetTarget(0)"
        _step("clear_target", ok=bool(getattr(st, "ok", False)), ret=getattr(st, "ret", None))
    except Exception as e:
        _step("clear_target_err", error=str(e))
        log(f"stop_host_perform SetTarget(0) err: {e}")

    # --- E) bridge 0x21 cancel packet ---
    if use_bridge_cancel:
        try:
            from app.core.xajh_bridge import ensure_bridge

            br = ensure_bridge(
                pid,
                log=log,
                inject_if_needed=False,
                hwnd=int(hwnd or 0) or None,
                force_reinject=False,
            )
            if br is not None:
                try:
                    r = br.cancel_session(hwnd=int(hwnd or 0) or None, timeout_ms=2500)
                    out["cancel21"] = {
                        "ok": bool(r.ok),
                        "ret": r.ret,
                        "note": r.note,
                        "error": r.error,
                    }
                    out["method"] = (out.get("method") or "") + "+0x21"
                    _step("cancel21", ok=bool(r.ok), ret=r.ret, note=r.note)
                finally:
                    try:
                        br.close()
                    except Exception:
                        pass
        except Exception as e:
            _step("cancel21_err", error=str(e))
            log(f"stop_host_perform 0x21 err: {e}")

    after = read_host_perform_state(session, log=lambda _m: None)
    out["after"] = after

    # --- F) last resort: ONLY for still-matter curr. Never bare-null type=2. ---
    if (
        force_clear_slot
        and mgr
        and after.get("is_matter")
        and after.get("active")
    ):
        try:
            matter_curr = int(after.get("curr_perform") or 0) & 0xFFFFFFFF
            okw = _write_u32(session, mgr + PERFORM_MGR_OFF_CURR, 0)
            out["slot_cleared"] = bool(okw)
            if okw:
                out["method"] = (out.get("method") or "") + "+clear_matter_slot"
                _step(
                    "clear_matter_slot",
                    addr=hex(mgr + PERFORM_MGR_OFF_CURR),
                    old_curr=hex(matter_curr),
                    old_type=after.get("perform_type"),
                )
            after2 = read_host_perform_state(session, log=lambda _m: None)
            out["after"] = after2
        except Exception as e:
            out["error"] = f"clear slot: {e}"
            log(f"stop_host_perform clear-slot err: {e}")
    elif force_clear_slot and after.get("active") and not after.get("is_matter"):
        _step(
            "clear_slot_refused",
            type=after.get("perform_type"),
            reason="refusing to null non-matter/base perform (breaks locomotion)",
        )
        log(
            f"stop_host_perform refuse clear_slot type={after.get('perform_type')} "
            "(bare null breaks HostMove)"
        )

    # --- G) restore base locomotion if slot empty after stop ---
    final = out.get("after") or {}
    if restore_base and (final.get("base_missing") or not final.get("active")):
        try:
            rb = ensure_base_locomotion_perform(session, log=log)
            out["restore_base"] = {
                "ok": rb.get("ok"),
                "method": rb.get("method"),
                "ret": rb.get("ret"),
                "error": rb.get("error"),
                "note": rb.get("note"),
            }
            if rb.get("ok"):
                out["restored_base"] = True
                out["method"] = (out.get("method") or "") + "+restore_base"
                _step("restore_base", **{k: rb.get(k) for k in ("method", "ret", "note")})
            else:
                _step("restore_base_fail", error=rb.get("error"), method=rb.get("method"))
            final = read_host_perform_state(session, log=lambda _m: None)
            out["after"] = final
        except Exception as e:
            _step("restore_base_err", error=str(e))
            log(f"stop_host_perform restore_base err: {e}")

    # Re-clear gates if still set (do not touch healthy side flags).
    if host and (
        int(final.get("session_gate") or 0)
        or int(final.get("session_gate1") or 0)
        or int(final.get("session_gate2") or 0)
    ):
        try:
            _write_u32(session, host + HOST_OFF_SESSION_GATE, 0)
            _write_u32(session, host + HOST_OFF_SESSION_GATE + 4, 0)
            _write_u32(session, host + HOST_OFF_SESSION_GATE + 8, 0)
            out["gate_cleared"] = True
            final = read_host_perform_state(session, log=lambda _m: None)
            out["after"] = final
            out["method"] = (out.get("method") or "") + "+clear_gate12"
            _step("clear_gate12_final", gate=final.get("session_gate"))
        except Exception as e:
            _step("gate_final_err", error=str(e))

    matter_gone = not bool(final.get("is_matter"))
    base_ok = bool(final.get("active")) and not bool(final.get("base_missing"))
    gate_clear = not bool(
        int(final.get("session_gate") or 0)
        or int(final.get("session_gate1") or 0)
        or int(final.get("session_gate2") or 0)
    )
    # ok: matter/driver gone (bar will not re-show). unlocked needs base slot too.
    out["ok"] = bool(matter_gone and gate_clear and out.get("error") is None)
    out["unlocked"] = bool(out["ok"] and base_ok and not final.get("locked_hint"))
    if out["ok"] and out["unlocked"]:
        out["error"] = None
        out["note"] = (
            f"matter cleared; base type={final.get('perform_type')} "
            f"curr=0x{int(final.get('curr_perform') or 0):X}"
        )
    elif out["ok"] and not out["unlocked"]:
        out["error"] = None
        out["note"] = (
            f"matter/gate clear but locomotion not healthy "
            f"active={final.get('active')} type={final.get('perform_type')} "
            f"base_missing={final.get('base_missing')} locked={final.get('locked_hint')}"
        )
    elif not out.get("error"):
        out["error"] = (
            f"still locked matter={final.get('is_matter')} type={final.get('perform_type')} "
            f"sub={final.get('perform_subtype')} curr=0x{int(final.get('curr_perform') or 0):X} "
            f"gate={final.get('session_gate')}"
        )
    if not out.get("method"):
        out["method"] = "noop" if not out.get("needed") else "partial"
    log(
        f"stop_host_perform RESULT ok={out['ok']} unlocked={out.get('unlocked')} "
        f"method={out.get('method')} before_type={before.get('perform_type')}/"
        f"{before.get('perform_subtype')} after_type={final.get('perform_type')} "
        f"after_active={final.get('active')} after_lock={final.get('locked_hint')} "
        f"base_missing={final.get('base_missing')} gate={final.get('session_gate')} "
        f"slot_cleared={out.get('slot_cleared')} restored_base={out.get('restored_base')} "
        f"err={out.get('error')!r} note={out.get('note')!r}"
    )
    return out


def _read_remote_wstring(session: GameAttachSession, addr: int, max_chars: int = 48) -> str:
    """Read remote UTF-16LE C string. @author by ak"""
    addr = int(addr) & 0xFFFFFFFF
    if not addr:
        return ""
    pm = getattr(session, "pm", None)
    if pm is None:
        return ""
    try:
        import pymem.memory

        n = max(2, min(96, int(max_chars) * 2 + 2))
        raw = pymem.memory.read_bytes(pm.process_handle, addr, n)
        return raw.decode("utf-16le", "ignore").split("\x00", 1)[0].strip()
    except Exception:
        return ""


def _progress_text_from_ctrl(session: GameAttachSession, ctrl_ptr: int) -> str:
    """
    Best-effort read of AUI text control caption (Txt_Name).

    Live Win_Prgs2 Txt_Name keeps wide title pointer around +0xB8.
    @author by ak
    """
    ctrl = int(ctrl_ptr) & 0xFFFFFFFF
    if not ctrl:
        return ""
    for off in AUI_PROGRESS_TXT_STR_OFFS:
        p = _read_u32(session, ctrl + int(off))
        s = _read_remote_wstring(session, p, 40)
        if s and any("\u4e00" <= ch <= "\u9fff" for ch in s):
            return s
    return ""


def read_interact_progress_text(
    session: GameAttachSession,
    dlg_ptr: int,
    *,
    log: LogFn | None = None,
) -> str:
    """
    Read progress bar title text from dialog (Txt_Name preferred).

    @author by ak
    """
    log = log or (lambda _m: None)
    dlg = int(dlg_ptr) & 0xFFFFFFFF
    if not dlg:
        return ""
    try:
        from app.core.aui_click import get_aui_dlg_item_ptr

        for name in ("Txt_Name", "Txt_Title", "Lab_Name", "Lab_Title", "Txt_Info"):
            ctrl = get_aui_dlg_item_ptr(session, dlg, name, log=lambda _m: None)
            if not ctrl:
                continue
            s = _progress_text_from_ctrl(session, ctrl)
            if s:
                return s
    except Exception as e:
        log(f"read_interact_progress_text err: {e}")
    return ""


def _progress_text_matches_entry(text: str, *, any_progress: bool = False) -> bool:
    """True if progress title is 暗道 entry bar (or any non-empty when forced). @author by ak"""
    t = (text or "").strip()
    if not t:
        return bool(any_progress)
    if any(k in t for k in ENTRY_PROGRESS_TEXT_KEYS):
        return True
    if ENTRY_ITEM_NAME and ENTRY_ITEM_NAME in t:
        return True
    return bool(any_progress)


def query_interact_progress_bars(
    session: GameAttachSession,
    *,
    names: tuple[str, ...] | list[str] | None = None,
    entry_only: bool = True,
    log: LogFn | None = None,
) -> list[dict]:
    """
    List currently shown matter/skill progress dialogs.

    Live 0%「九层妖楼暗道」bar = Win_Prgs2 (Progress2.xml), rect ~bottom center.
    @author by ak
    """
    log = log or (lambda _m: None)
    out: list[dict] = []
    use_names = tuple(names or INTERACT_PROGRESS_DLG_NAMES)
    for name in use_names:
        try:
            r = query_dlg_show(session, name, log=lambda _m: None)
        except Exception as e:
            log(f"query progress {name}: {e}")
            continue
        if not r.ok or not r.dlg_ptr:
            continue
        shown = bool(r.shown)
        text = ""
        rect = None
        if r.dlg_ptr:
            try:
                text = read_interact_progress_text(session, int(r.dlg_ptr), log=log)
            except Exception:
                text = ""
            if shown:
                try:
                    from app.core.dlg_image_export import read_dlg_rect

                    rr = read_dlg_rect(session, int(r.dlg_ptr), name=name, log=lambda _m: None)
                    if getattr(rr, "ok", False):
                        rect = {
                            "x": int(rr.x),
                            "y": int(rr.y),
                            "w": int(rr.w),
                            "h": int(rr.h),
                        }
                except Exception:
                    rect = None
        item = {
            "name": name,
            "shown": shown,
            "dlg_ptr": int(r.dlg_ptr) & 0xFFFFFFFF,
            "text": text,
            "rect": rect,
            "entry_match": _progress_text_matches_entry(text, any_progress=False),
        }
        if shown and (not entry_only or item["entry_match"] or name == "Win_Prgs2"):
            # Win_Prgs2 shown without readable text still counts as candidate
            # when entry_only: require entry text OR empty text on Win_Prgs2.
            if entry_only:
                if item["entry_match"] or (name == "Win_Prgs2" and not text):
                    out.append(item)
            else:
                out.append(item)
    return out


def _yaolu_live_va(session: GameAttachSession, note_va: int) -> int:
    """Map preferred-base note VA to live module VA. @author by ak"""
    base = int(getattr(session, "module_base", 0) or 0) or DEFAULT_IMAGE_BASE
    return int(base) + (int(note_va) - int(DEFAULT_IMAGE_BASE))


def get_game_ui_mgr(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
    prefer_call: bool = True,
) -> dict:
    """
    Resolve game UI manager this-ptr (preferred note VA 0x8C6A70).

    Memory layout (from PE getter):
      g = *[0x15282D8]; a = *[g+0x24]; b = *[a+8]; idx = *[b+0x330]
      mgr = *[b + idx*4 + 0x320]  (0 if idx==-1)

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {"ok": False, "mgr": 0, "method": "", "error": None, "slots": {}}
    pid = int(getattr(session, "pid", 0) or 0)
    if prefer_call and pid:
        try:
            from app.core.remote_runtime import remote_call_cdecl_x86

            va = _yaolu_live_va(session, NOTE_VA_GET_UI_MGR)
            mgr = int(remote_call_cdecl_x86(pid, va, (), timeout_ms=2500)) & 0xFFFFFFFF
            if mgr:
                out["ok"] = True
                out["mgr"] = mgr
                out["method"] = f"call:0x{NOTE_VA_GET_UI_MGR:X}"
        except Exception as e:
            out["error"] = f"call mgr: {e}"
            log(f"get_game_ui_mgr call err: {e}")
    if not out["mgr"]:
        try:
            g = _read_u32(session, _yaolu_live_va(session, NOTE_VA_GLOBAL_ROOT))
            a24 = _read_u32(session, g + 0x24) if g else 0
            a8 = _read_u32(session, a24 + 8) if a24 else 0
            idx = _read_u32(session, a8 + 0x330) if a8 else 0xFFFFFFFF
            mgr = 0
            if a8 and idx != 0xFFFFFFFF:
                mgr = _read_u32(session, a8 + int(idx) * 4 + 0x320)
            if mgr:
                out["ok"] = True
                out["mgr"] = int(mgr) & 0xFFFFFFFF
                out["method"] = "mem:global+0x24+8"
                out["error"] = None
            else:
                if not out.get("error"):
                    out["error"] = f"mgr=0 g=0x{g:X} a24=0x{a24:X} a8=0x{a8:X} idx=0x{idx:X}"
        except Exception as e:
            out["error"] = f"mem mgr: {e}"
            log(f"get_game_ui_mgr mem err: {e}")
    mgr = int(out.get("mgr") or 0) & 0xFFFFFFFF
    if mgr:
        slots = {}
        for off in UI_MGR_PROGRESS_SLOT_OFFS:
            slots[hex(off)] = _read_u32(session, mgr + int(off))
        out["slots"] = slots
    return out


def native_cancel_progress_ui(
    session: GameAttachSession,
    *,
    mgr: int = 0,
    log: LogFn | None = None,
) -> dict:
    """
    Native cancel+hide for progress dialogs: thiscall 0x87C890(this=UI_mgr).

    PE: if mgr+0x3C4 (Win_Prgs2) shown -> 0xCC7010(clear=1,id=0) then
    Show(0,0,1). Also walks sibling slots. Returns al=1 only when cancel
    packet path accepted AND Show issued.

    @author by ak
    """
    log = log or (lambda _m: None)
    out: dict = {
        "ok": False,
        "ret": 0,
        "mgr": int(mgr or 0) & 0xFFFFFFFF,
        "fn": 0,
        "error": None,
        "method": "thiscall:0x87C890",
    }
    pid = int(getattr(session, "pid", 0) or 0)
    if not pid:
        out["error"] = "no pid"
        return out
    if not out["mgr"]:
        gi = get_game_ui_mgr(session, log=log, prefer_call=True)
        out["mgr"] = int(gi.get("mgr") or 0) & 0xFFFFFFFF
        if not out["mgr"]:
            out["error"] = gi.get("error") or "ui mgr=0"
            return out
    try:
        from app.core.remote_runtime import remote_call_thiscall_x86

        fn = _yaolu_live_va(session, NOTE_VA_CANCEL_PROGRESS_UI)
        out["fn"] = int(fn) & 0xFFFFFFFF
        ret = int(
            remote_call_thiscall_x86(
                pid,
                fn,
                int(out["mgr"]) & 0xFFFFFFFF,
                (),
                caller_cleanup=False,
                timeout_ms=3000,
            )
        ) & 0xFF
        out["ret"] = ret
        out["ok"] = bool(ret)
        if not ret:
            out["error"] = "0x87C890 returned 0 (CancelAction failed or no shown slot)"
        log(
            f"native_cancel_progress_ui mgr=0x{out['mgr']:X} "
            f"fn=0x{fn:X} ret={ret}"
        )
    except Exception as e:
        out["error"] = str(e)
        log(f"native_cancel_progress_ui err: {e}")
    return out


def _entry_bar_targets(
    session: GameAttachSession,
    *,
    log: LogFn | None = None,
) -> list[dict]:
    """Collect hide targets for 暗道 0% bar. @author by ak"""
    log = log or (lambda _m: None)
    before_bars = query_interact_progress_bars(session, entry_only=True, log=log)
    if before_bars:
        return list(before_bars)
    all_bars = query_interact_progress_bars(session, entry_only=False, log=log)
    out: list[dict] = []
    for b in all_bars:
        if not b.get("shown") or b.get("name") != "Win_Prgs2":
            continue
        txt = str(b.get("text") or "")
        if (not txt) or _progress_text_matches_entry(txt, any_progress=False):
            out.append(b)
    return out


def _entry_progress_still_shown(
    session: GameAttachSession,
    targets: list[dict] | None = None,
    *,
    log: LogFn | None = None,
) -> list[dict]:
    """
    Re-check entry progress dialogs that are still shown.

    @author by ak
    """
    log = log or (lambda _m: None)
    still: list[dict] = []
    seen: set[int] = set()
    check = list(targets or [])
    # Always re-query live entry bars (ptr may be stable; show flag changes).
    try:
        live = query_interact_progress_bars(session, entry_only=True, log=lambda _m: None)
    except Exception:
        live = []
    for b in live:
        ptr = int(b.get("dlg_ptr") or 0) & 0xFFFFFFFF
        if ptr:
            seen.add(ptr)
        if b.get("shown"):
            still.append(
                {
                    "name": b.get("name"),
                    "dlg_ptr": ptr,
                    "text": b.get("text"),
                    "via": "query",
                }
            )
    for t in check:
        ptr = int(t.get("dlg_ptr") or 0) & 0xFFFFFFFF
        name = str(t.get("name") or "")
        if ptr and ptr in seen:
            continue
        shown = False
        try:
            if ptr:
                shown = bool(is_dlg_show(session, ptr, log=lambda _m: None))
            elif name:
                q = query_dlg_show(session, name, log=lambda _m: None)
                shown = bool(q.shown)
                ptr = int(getattr(q, "dlg_ptr", 0) or ptr) & 0xFFFFFFFF
        except Exception:
            shown = True
        if not shown:
            continue
        txt = ""
        try:
            txt = read_interact_progress_text(session, ptr, log=lambda _m: None) if ptr else ""
        except Exception:
            txt = ""
        if _progress_text_matches_entry(txt, any_progress=False) or (
            not txt and name == "Win_Prgs2"
        ):
            still.append({"name": name, "dlg_ptr": ptr, "text": txt, "via": "target"})
    return still


def clear_entry_interact_bar(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    hwnd: int = 0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
) -> dict:
    """
    Cancel stuck 暗道 matter-interact progress bar (the 0% bar).

    Live evidence 2026-07-20:
      - NOT skill cast: host session often idle while Win_Prgs2 paints 0%
      - bare AUIDialog::Show(false) can hide for ~1ms then flash-back ~20ms
      - driver is progress/session state; native path 0x87C890 only hides after
        CancelAction 0xCC7010(1,0) succeeds, then Show(0,0,1)

    Strategy:
      1) detect shown Win_Prgs2 / 暗道 title
      2) stop host perform slot (true re-show driver; PE 0x8862E0)
      3) force CMD_CANCEL_SESSION 0x21 (even when cast idle)
      4) call native 0x87C890(UI_mgr) cancel+hide
      5) sustain hide 1.6s (Show 0,0,0 / 0,0,1 + re-native) against flash-back
      6) delayed rechecks; ok=True only if bar stays gone after delay

    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    before_cast = read_host_cast_state(session, log=log)
    before_bars = query_interact_progress_bars(session, entry_only=True, log=log)
    all_bars = query_interact_progress_bars(session, entry_only=False, log=log)
    hide_targets = list(before_bars)
    if not hide_targets:
        for b in all_bars:
            if not b.get("shown") or b.get("name") != "Win_Prgs2":
                continue
            txt = str(b.get("text") or "")
            if (not txt) or _progress_text_matches_entry(txt, any_progress=False):
                hide_targets.append(b)

    out: dict = {
        "ok": False,
        "before": before_cast,
        "after": before_cast,
        "before_bars": before_bars,
        "after_bars": [],
        "all_bars_before": all_bars,
        "hidden": [],
        "pulses": 0,
        "esc": 0,
        "nudge": False,
        "nudge_count": 0,
        "queued": False,
        "error": None,
        "method": "0x21+0x87C890+sustain_Show",
        "cast_busy_before": _cast_session_busy_state(before_cast),
        "cast_busy_after": False,
        "cast_idle_before": not _cast_session_busy_state(before_cast),
        "note": "",
        "bar_before": bool(before_bars) or bool(hide_targets),
        "bar_after": False,
        "flash_back": 0,
        "sustain_hides": 0,
        "native_calls": 0,
        "native_ok": 0,
        "ui_mgr": 0,
        "verify_delays": [],
        "still_shown": [],
    }
    status(
        "清理进图读条(0%): "
        + (
            ", ".join(
                f"{b.get('name')}[{b.get('text') or '?'}]" for b in (hide_targets or before_bars)
            )
            if (hide_targets or before_bars)
            else "未检测到暗道进度条UI"
        )
    )
    log(
        f"yaolu clear-bar before cast={_cast_state_text(before_cast)} "
        f"bars={[{'n': b.get('name'), 't': b.get('text'), 'r': b.get('rect')} for b in hide_targets]}"
    )

    # Even with no Win_Prgs2 UI, matter perform type 0x67 can still lock move.
    if not hide_targets and not out.get("bar_before"):
        try:
            pf0 = read_host_perform_state(session, log=lambda _m: None)
        except Exception:
            pf0 = {}
        if pf0.get("is_matter") or pf0.get("base_missing"):
            status("清理进图读条(0%): 无条UI但 perform 仍异常，先停/恢复基座")
            try:
                ps = stop_host_perform(
                    session,
                    force_clear_slot=True,
                    restore_base=True,
                    hwnd=int(hwnd or 0),
                    use_bridge_cancel=bool(getattr(cfg, "use_bridge", True)),
                    log=log,
                )
                out["perform_stop"] = ps
                out["perform_before"] = pf0
                out["method"] = f"perform_only:{ps.get('method') or ''}"
                pf1 = read_host_perform_state(session, log=lambda _m: None)
                out["ok"] = (not pf1.get("is_matter")) and (not pf1.get("base_missing"))
                out["note"] = (
                    f"no bar UI; perform type {pf0.get('perform_type')}->"
                    f"{pf1.get('perform_type')} unlocked={ps.get('unlocked')}"
                )
                out["error"] = None if out["ok"] else (
                    ps.get("error") or "matter/base still unhealthy without bar UI"
                )
                status(
                    "清理进图读条(0%): "
                    + ("成功 仅停perform" if out["ok"] else f"失败 {out['error']}")
                )
                log(
                    f"yaolu clear-bar RESULT ok={out['ok']} note={out['note']!r} "
                    f"err={out.get('error')!r}"
                )
                return out
            except Exception as e:
                out["ok"] = False
                out["error"] = f"perform-only clear: {e}"
                log(f"yaolu clear-bar perform-only err: {e}")
                return out
        out["ok"] = True
        out["note"] = "no entry progress bar UI shown"
        out["method"] = "none_needed"
        status("清理进图读条(0%): 成功 无需清理")
        log("yaolu clear-bar RESULT ok=True note='no entry progress bar UI shown'")
        return out

    def _stopped(msg: str) -> bool:
        if stop_event is not None and stop_event.is_set():
            out["error"] = msg
            return True
        return False

    # --- 0) stop host perform (true driver of Win_Prgs2 re-show) ---
    out["perform_before"] = None
    out["perform_stop"] = None
    try:
        status("清理进图读条(0%): 停止 host perform（防读条刷回）")
        pb = read_host_perform_state(session, log=log)
        out["perform_before"] = pb
        log(
            f"yaolu clear-bar perform before active={pb.get('active')} "
            f"host=0x{int(pb.get('host') or 0):X} mgr=0x{int(pb.get('perform_mgr') or 0):X} "
            f"curr=0x{int(pb.get('curr_perform') or 0):X} "
            f"type={pb.get('perform_type')} sub={pb.get('perform_subtype')} "
            f"flags={pb.get('flag0')}/{pb.get('flag1')}"
        )
        ps = stop_host_perform(
            session,
            force_clear_slot=True,
            restore_base=True,
            hwnd=int(hwnd or 0),
            use_bridge_cancel=True,
            log=log,
        )
        out["perform_stop"] = ps
        status(
            "清理进图读条(0%): perform "
            + ("已停" if ps.get("ok") else "未停净")
            + f" method={ps.get('method')} active_after="
            + str((ps.get("after") or {}).get("active"))
        )
    except Exception as e:
        out["perform_stop"] = {"ok": False, "error": str(e)}
        log(f"yaolu clear-bar perform-stop err: {e}")

    # --- 1) force 0x21 to clear driving session even when cast idle ---
    if bool(getattr(cfg, "use_bridge", True)):
        try:
            from app.core.xajh_bridge import ensure_bridge

            bridge = ensure_bridge(
                int(session.pid),
                log=log,
                inject_if_needed=False,
                hwnd=int(hwnd or 0) or None,
                force_reinject=False,
            )
            if bridge is not None:
                try:
                    n = max(1, min(4, int(getattr(cfg, "cancel_session_pulses", 3) or 3)))
                    for i in range(n):
                        if _stopped("stopped during clear-bar 0x21"):
                            break
                        status(f"清理进图读条(0%): 取消会话 0x21 {i + 1}/{n}")
                        result = bridge.cancel_session(
                            hwnd=int(hwnd or 0) or None,
                            timeout_ms=2500,
                        )
                        out["pulses"] = int(out["pulses"] or 0) + 1
                        note = str(result.note or result.error or "")
                        if result.ok:
                            q = bool(int(result.ret or 0)) or ("queued" in note.lower())
                            out["queued"] = out["queued"] or q
                        log(
                            f"yaolu clear-bar 0x21 #{i + 1}/{n} "
                            f"ok={result.ok} ret={result.ret} note={note!r}"
                        )
                        if i + 1 < n and not _sleep_interruptible(0.10, stop_event):
                            out["error"] = "stopped during clear-bar 0x21"
                            break
                finally:
                    try:
                        bridge.close()
                    except Exception:
                        pass
        except Exception as e:
            log(f"yaolu clear-bar 0x21 err: {e}")

    if out.get("error"):
        status(f"清理进图读条(0%): 失败 {out['error']}")
        return out

    # --- 2) resolve UI mgr + native cancel path ---
    gi = get_game_ui_mgr(session, log=log, prefer_call=True)
    out["ui_mgr"] = int(gi.get("mgr") or 0) & 0xFFFFFFFF
    log(
        f"yaolu clear-bar ui_mgr=0x{out['ui_mgr']:X} via={gi.get('method')} "
        f"slots={gi.get('slots')} err={gi.get('error')!r}"
    )
    if out["ui_mgr"]:
        status("清理进图读条(0%): 原生 0x87C890 取消+隐藏")
        nr = native_cancel_progress_ui(session, mgr=out["ui_mgr"], log=log)
        out["native_calls"] = int(out["native_calls"] or 0) + 1
        if nr.get("ok"):
            out["native_ok"] = int(out["native_ok"] or 0) + 1
        out["hidden"].append(
            {
                "name": "native_0x87C890",
                "dlg_ptr": out["ui_mgr"],
                "ok": bool(nr.get("ok")),
                "ret": nr.get("ret"),
                "error": nr.get("error"),
                "method": nr.get("method"),
            }
        )

    # --- 3) sustain hide against flash-back ---
    sustain_s = float(CLEAR_BAR_SUSTAIN_S)
    interval = float(CLEAR_BAR_HIDE_INTERVAL_S)
    deadline = time.monotonic() + max(0.4, sustain_s)
    last_shown = True
    flash_back = 0
    sustain_hides = 0
    sample: list[tuple[int, bool]] = []  # (ms, shown)
    t0 = time.monotonic()
    next_native_at = 0.0

    status(f"清理进图读条(0%): 持续压制 {sustain_s:.1f}s 防闪回")
    while time.monotonic() < deadline:
        if _stopped("stopped during clear-bar sustain"):
            break
        still = _entry_progress_still_shown(session, hide_targets, log=lambda _m: None)
        shown_now = bool(still)
        now = time.monotonic()
        ms = int((now - t0) * 1000)
        if len(sample) < 40:
            sample.append((ms, shown_now))
        if (not last_shown) and shown_now:
            flash_back += 1
            log(f"yaolu clear-bar FLASH_BACK#{flash_back} at {ms}ms still={still}")
            status(f"清理进图读条(0%): 检测到闪回 #{flash_back}，再停 perform")
            try:
                stop_host_perform(session, force_clear_slot=True, restore_base=True, log=lambda _m: None)
            except Exception:
                pass
        last_shown = shown_now

        if shown_now:
            # Re-drive cancel state (throttled CRT) then hide each target.
            if out["ui_mgr"] and now >= next_native_at:
                nr = native_cancel_progress_ui(
                    session, mgr=out["ui_mgr"], log=lambda _m: None
                )
                out["native_calls"] = int(out["native_calls"] or 0) + 1
                if nr.get("ok"):
                    out["native_ok"] = int(out["native_ok"] or 0) + 1
                next_native_at = now + 0.25
            for b in still or hide_targets:
                ptr = int(b.get("dlg_ptr") or 0) & 0xFFFFFFFF
                name = str(b.get("name") or "?")
                if not ptr:
                    continue
                # Native uses Show(0,0,1) after cancel; bare hide often needs (0,0,0).
                hr = {"ok": False, "shown_after": True, "args": None, "method": None}
                for args in ((0, 0, 0), (0, 0, 1)):
                    hr = hide_aui_dialog(
                        session,
                        ptr,
                        force=True,
                        show_args=args,
                        try_variants=False,
                        log=lambda _m: None,
                    )
                    sustain_hides += 1
                    if hr.get("ok"):
                        break
                out["hidden"].append(
                    {
                        "name": name,
                        "dlg_ptr": ptr,
                        "text": b.get("text"),
                        "ok": bool(hr.get("ok")),
                        "shown_after": hr.get("shown_after"),
                        "args": hr.get("args"),
                        "method": hr.get("method"),
                        "wave": "sustain",
                    }
                )
        if not _sleep_interruptible(interval, stop_event):
            out["error"] = "stopped during clear-bar sustain"
            break

    out["flash_back"] = int(flash_back)
    out["sustain_hides"] = int(sustain_hides)
    out["sample"] = sample

    # --- 4) optional short nudge only if still shown ---
    still_mid = _entry_progress_still_shown(session, hide_targets, log=lambda _m: None)
    if (
        still_mid
        and bool(getattr(cfg, "cancel_nudge_after", False))
        and out.get("error") is None
        and not _stopped("stopped before clear-bar nudge")
    ):
        try:
            sp = read_scene_position(session, log=lambda _m: None)
            if sp.ok and sp.scene_pos:
                hx, hy, hz = sp.scene_pos
                sid = int(getattr(sp, "scene_id", 0) or ENTRY_PATH_SCENE_ID or 68)
                ang = 0.0
                dist = max(3.0, float(getattr(cfg, "cancel_nudge_dist", 4.5) or 4.5) * 0.7)
                status("清理进图读条(0%): 副清理 短挪位")
                nudge = SuperLootTarget(
                    name="clear-bar-nudge",
                    ptr=0,
                    x=float(hx) + math.cos(ang) * dist,
                    y=float(hy),
                    z=float(hz) + math.sin(ang) * dist,
                )
                moved = move_to_target(
                    session,
                    nudge,
                    hwnd=int(hwnd or 0),
                    move_mode=int(sid),
                    use_bridge=True,
                    host_pos=(hx, hy, hz),
                    scene_id=int(sid),
                    stop_event=stop_event,
                    log=log,
                )
                ok_m = bool(moved.get("ok"))
                out["nudge"] = ok_m
                out["nudge_count"] = 1 if ok_m else 0
                log(f"yaolu clear-bar side-nudge ok={ok_m} dist={dist:.1f}")
                # After nudge: one more 0x21 + native + hide wave.
                if bool(getattr(cfg, "use_bridge", True)):
                    try:
                        from app.core.xajh_bridge import ensure_bridge

                        br2 = ensure_bridge(
                            int(session.pid),
                            log=log,
                            inject_if_needed=False,
                            hwnd=int(hwnd or 0) or None,
                            force_reinject=False,
                        )
                        if br2 is not None:
                            try:
                                r2 = br2.cancel_session(
                                    hwnd=int(hwnd or 0) or None, timeout_ms=2500
                                )
                                out["pulses"] = int(out["pulses"] or 0) + 1
                                log(
                                    f"yaolu clear-bar post-nudge 0x21 ok={r2.ok} "
                                    f"ret={r2.ret} note={r2.note!r}"
                                )
                            finally:
                                try:
                                    br2.close()
                                except Exception:
                                    pass
                    except Exception as e:
                        log(f"yaolu clear-bar post-nudge 0x21 err: {e}")
                if out["ui_mgr"]:
                    native_cancel_progress_ui(session, mgr=out["ui_mgr"], log=log)
                    out["native_calls"] = int(out["native_calls"] or 0) + 1
                for b in still_mid:
                    ptr = int(b.get("dlg_ptr") or 0)
                    if ptr:
                        hide_aui_dialog(
                            session,
                            ptr,
                            force=True,
                            show_args=(0, 0, 0),
                            try_variants=True,
                            log=log,
                        )
        except Exception as e:
            log(f"yaolu clear-bar side-nudge err: {e}")

    # --- 5) delayed verifies (no hide) — flash-back must not return ---
    verify_rows: list[dict] = []
    final_still: list[dict] = []
    for delay in CLEAR_BAR_VERIFY_DELAYS_S:
        if out.get("error"):
            break
        if not _sleep_interruptible(float(delay), stop_event):
            out["error"] = "stopped during clear-bar verify"
            break
        still = _entry_progress_still_shown(session, hide_targets, log=lambda _m: None)
        row = {
            "delay_s": float(delay),
            "shown": bool(still),
            "still": still,
        }
        verify_rows.append(row)
        log(
            f"yaolu clear-bar verify +{delay:.2f}s shown={bool(still)} "
            f"still={still}"
        )
        if still:
            # rescue wave on failed verify
            status(f"清理进图读条(0%): 延迟复检仍在，再清一轮 (+{delay:.2f}s)")
            if out["ui_mgr"]:
                native_cancel_progress_ui(session, mgr=out["ui_mgr"], log=log)
                out["native_calls"] = int(out["native_calls"] or 0) + 1
            for b in still:
                ptr = int(b.get("dlg_ptr") or 0)
                if not ptr:
                    continue
                hide_aui_dialog(
                    session,
                    ptr,
                    force=True,
                    show_args=(0, 0, 0),
                    try_variants=True,
                    log=log,
                )
            out["flash_back"] = int(out.get("flash_back") or 0) + 1
            final_still = still
        else:
            final_still = []

    out["verify_delays"] = verify_rows
    try:
        after_cast = read_host_cast_state(session, log=lambda _m: None)
        out["after"] = after_cast
        out["cast_busy_after"] = _cast_session_busy_state(after_cast)
    except Exception:
        pass
    after_bars = query_interact_progress_bars(session, entry_only=True, log=log)
    out["after_bars"] = after_bars
    out["still_shown"] = final_still or _entry_progress_still_shown(
        session, hide_targets, log=lambda _m: None
    )
    out["bar_after"] = bool(out["still_shown"]) or bool(after_bars)

    if out.get("error"):
        out["ok"] = False
        out["note"] = str(out["error"])
    elif not out["bar_after"]:
        out["ok"] = True
        ps = out.get("perform_stop") or {}
        out["note"] = (
            f"cleared; flash_back={out.get('flash_back')} "
            f"sustain_hides={out.get('sustain_hides')} "
            f"native_ok={out.get('native_ok')}/{out.get('native_calls')} "
            f"0x21x{out.get('pulses')} "
            f"perform={ps.get('method') if isinstance(ps, dict) else None} "
            f"verified_gone"
        )
    else:
        out["ok"] = False
        out["error"] = (
            "entry progress bar still shown after cancel+sustain: "
            + ", ".join(
                f"{s.get('name')}[{s.get('text') or ''}]" for s in (out["still_shown"] or after_bars)
            )
        )
        ps = out.get("perform_stop") or {}
        pa = (ps.get("after") or {}) if isinstance(ps, dict) else {}
        out["note"] = (
            f"FAILED flash_back={out.get('flash_back')} "
            f"sustain_hides={out.get('sustain_hides')} "
            f"native_ok={out.get('native_ok')}/{out.get('native_calls')} "
            f"perform_active={pa.get('active')} "
            f"perform_type={pa.get('perform_type')}/{pa.get('perform_subtype')} "
            f"perform_method={ps.get('method') if isinstance(ps, dict) else None} "
            f"sample={sample[:12]}"
        )

    status(
        f"清理进图读条(0%): {'成功' if out['ok'] else '失败'} "
        f"flash_back={out.get('flash_back')} sustain={out.get('sustain_hides')} "
        f"native={out.get('native_ok')}/{out.get('native_calls')} "
        f"bar_after={out['bar_after']} {out.get('note') or ''}"
    )
    log(
        f"yaolu clear-bar RESULT ok={out['ok']} flash_back={out.get('flash_back')} "
        f"sustain_hides={out.get('sustain_hides')} "
        f"native_ok={out.get('native_ok')}/{out.get('native_calls')} "
        f"ui_mgr=0x{int(out.get('ui_mgr') or 0):X} "
        f"bar_before={out['bar_before']} bar_after={out['bar_after']} "
        f"still={out.get('still_shown')} pulses={out['pulses']} "
        f"nudge_n={out.get('nudge_count')} note={out.get('note')!r} "
        f"err={out.get('error')!r} {_cast_state_text(out.get('after') or {})}"
    )
    return out


def recover_entry_interact_state(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    hwnd: int = 0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
    close_captcha: bool = True,
    clear_bar: bool = True,
    stop_perform: bool = True,
    use_bridge_cancel: bool = True,
) -> dict:
    """
    Official post-fail recovery — same order proven in 妖楼 lab (2026-07-20/21).

    1) close captcha via AUI Show(0,0,1) (no random wrong submit / audit pollution)
    2) stop_host_perform: matter 0x67 end + CancelAction + SetTarget(0) + restore type=2
       (NEVER bare-null type=2 locomotion slot)
    3) clear_entry_interact_bar: native hide Win_Prgs2 + sustain against flash-back
    4) light bridge 0x21 residual (cast/session soft)

    Measured ok:
      captcha closed AND not matter perform AND base locomotion present
      AND (no entry bar UI or clear_bar ok)

    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    out: dict = {
        "ok": False,
        "captcha_closed": False,
        "captcha_was_open": False,
        "captcha_close": None,
        "perform_stop": None,
        "clear_bar": None,
        "cancel21": None,
        "perform_after": None,
        "bars_after": [],
        "error": None,
        "method": "",
        "note": "",
    }
    methods: list[str] = []

    def _stopped(msg: str) -> bool:
        if stop_event is not None and stop_event.is_set():
            out["error"] = msg
            return True
        return False

    # --- 1) captcha UI ---
    if close_captcha:
        try:
            hit0 = is_captcha_dialog_open(
                session, names=cfg.captcha_dlg_names, log=lambda _m: None
            )
            out["captcha_was_open"] = bool(hit0.shown)
        except Exception:
            out["captcha_was_open"] = False
        if out["captcha_was_open"] or close_captcha:
            status("恢复：关闭验证码 UI（Show 函数路径）")
            try:
                closed = close_captcha_dialog(
                    session,
                    int(hwnd or 0),
                    prefer_bridge=bool(cfg.use_bridge),
                    prefer_post=bool(cfg.prefer_post_click),
                    allow_cursor=bool(cfg.allow_cursor_click),
                    humanize=bool(cfg.captcha_humanize_clicks),
                    names=cfg.captcha_dlg_names,
                    attempts=max(2, int(cfg.captcha_close_attempts)),
                    settle_s=max(0.2, float(cfg.captcha_close_settle_s)),
                    use_esc=bool(cfg.cancel_use_esc),
                    prefer_show_hide=True,
                    log=log,
                )
                out["captcha_close"] = closed
                still = is_captcha_dialog_open(
                    session, names=cfg.captcha_dlg_names, log=lambda _m: None
                )
                out["captcha_closed"] = not bool(still.shown)
                methods.append(f"close:{closed.get('method') or '?'}")
                log(
                    f"yaolu recover-state captcha closed={out['captcha_closed']} "
                    f"via={closed.get('method')} was_open={out['captcha_was_open']}"
                )
            except Exception as e:
                out["captcha_close"] = {"ok": False, "error": str(e)}
                log(f"yaolu recover-state captcha err: {e}")
                # recheck
                try:
                    still = is_captcha_dialog_open(
                        session, names=cfg.captcha_dlg_names, log=lambda _m: None
                    )
                    out["captcha_closed"] = not bool(still.shown)
                except Exception:
                    out["captcha_closed"] = not bool(out.get("captcha_was_open"))
        else:
            out["captcha_closed"] = True
    else:
        try:
            still = is_captcha_dialog_open(
                session, names=cfg.captcha_dlg_names, log=lambda _m: None
            )
            out["captcha_closed"] = not bool(still.shown)
        except Exception:
            out["captcha_closed"] = True

    if _stopped("stopped during captcha close"):
        return out

    # --- 2) stop matter perform / restore type=2 ---
    if stop_perform:
        status("恢复：停 host perform + 恢复移动基座")
        try:
            ps = stop_host_perform(
                session,
                force_clear_slot=True,
                restore_base=True,
                hwnd=int(hwnd or 0),
                use_bridge_cancel=bool(use_bridge_cancel and cfg.use_bridge),
                log=log,
            )
            out["perform_stop"] = ps
            methods.append(f"perform:{ps.get('method') or '?'}")
            log(
                f"yaolu recover-state perform ok={ps.get('ok')} unlocked={ps.get('unlocked')} "
                f"method={ps.get('method')} note={ps.get('note')!r}"
            )
        except Exception as e:
            out["perform_stop"] = {"ok": False, "error": str(e)}
            log(f"yaolu recover-state perform err: {e}")

    if _stopped("stopped during perform stop"):
        return out

    # --- 3) clear 0% entry bar UI ---
    if clear_bar:
        status("恢复：清理进图读条(0%)")
        try:
            bar = clear_entry_interact_bar(
                session,
                cfg,
                hwnd=int(hwnd or 0),
                stop_event=stop_event,
                log=log,
                status=status,
            )
            out["clear_bar"] = bar
            methods.append(f"bar:{bar.get('method') or ('ok' if bar.get('ok') else 'fail')}")
            log(
                f"yaolu recover-state bar ok={bar.get('ok')} bar_after={bar.get('bar_after')} "
                f"note={bar.get('note')!r} err={bar.get('error')!r}"
            )
        except Exception as e:
            out["clear_bar"] = {"ok": False, "error": str(e)}
            log(f"yaolu recover-state bar err: {e}")

    if _stopped("stopped during clear bar"):
        return out

    # --- 4) light residual 0x21 (session soft) ---
    if use_bridge_cancel and bool(cfg.use_bridge):
        try:
            from app.core.xajh_bridge import ensure_bridge

            br = ensure_bridge(
                int(session.pid),
                log=log,
                inject_if_needed=False,
                hwnd=int(hwnd or 0) or None,
                force_reinject=False,
            )
            if br is not None:
                try:
                    pulses = max(1, min(2, int(cfg.cancel_session_pulses or 1)))
                    queued = False
                    for i in range(pulses):
                        if _stopped("stopped during 0x21"):
                            return out
                        r = br.cancel_session(
                            hwnd=int(hwnd or 0) or None, timeout_ms=2500
                        )
                        note = str(r.note or r.error or "")
                        if r.ok and (
                            bool(int(r.ret or 0)) or "queued" in note.lower()
                        ):
                            queued = True
                        log(
                            f"yaolu recover-state 0x21#{i + 1}/{pulses} "
                            f"ok={r.ok} ret={r.ret} note={note!r}"
                        )
                        if i + 1 < pulses and not _sleep_interruptible(0.12, stop_event):
                            out["error"] = "stopped during 0x21"
                            return out
                    out["cancel21"] = {"ok": True, "queued": queued, "pulses": pulses}
                    methods.append("0x21")
                finally:
                    try:
                        br.close()
                    except Exception:
                        pass
        except Exception as e:
            out["cancel21"] = {"ok": False, "error": str(e)}
            log(f"yaolu recover-state 0x21 err: {e}")

    # --- 5) final ensure base if missing ---
    try:
        st = read_host_perform_state(session, log=lambda _m: None)
        if st.get("base_missing") or not st.get("active"):
            status("恢复：补装 type=2 移动基座")
            rb = ensure_base_locomotion_perform(session, log=log)
            out["restore_base"] = rb
            methods.append("restore_base" if rb.get("ok") else "restore_base_fail")
            st = read_host_perform_state(session, log=lambda _m: None)
        out["perform_after"] = st
    except Exception as e:
        log(f"yaolu recover-state final perform err: {e}")
        st = {}
        out["perform_after"] = st

    try:
        bars = query_interact_progress_bars(
            session, entry_only=True, log=lambda _m: None
        )
        out["bars_after"] = bars
    except Exception:
        bars = []
        out["bars_after"] = []

    captcha_ok = bool(out.get("captcha_closed"))
    matter_gone = not bool(st.get("is_matter"))
    base_ok = bool(st.get("active")) and not bool(st.get("base_missing"))
    bar_ok = (not bars) or bool((out.get("clear_bar") or {}).get("ok"))
    # If clear_bar skipped because none shown, bars empty => ok
    if not clear_bar:
        bar_ok = not bars

    out["method"] = "+".join(methods) if methods else "noop"
    out["ok"] = bool(captcha_ok and matter_gone and base_ok and bar_ok)
    if out["ok"]:
        out["error"] = None
        out["note"] = (
            f"recovered type={st.get('perform_type')} "
            f"bars={len(bars)} captcha_closed={captcha_ok}"
        )
        status(
            f"恢复完成：type={st.get('perform_type')} "
            f"bars={len(bars)} captcha_ok={captcha_ok}"
        )
    else:
        bits = []
        if not captcha_ok:
            bits.append("captcha_open")
        if not matter_gone:
            bits.append(f"matter_type={st.get('perform_type')}")
        if not base_ok:
            bits.append("base_missing")
        if not bar_ok:
            bits.append(f"bars={len(bars)}")
        out["error"] = out.get("error") or ("recover incomplete: " + ",".join(bits))
        out["note"] = out["error"]
        status(f"恢复未完全：{out['error']}")
    log(
        f"yaolu recover-state RESULT ok={out['ok']} method={out['method']} "
        f"type={st.get('perform_type')} base_missing={st.get('base_missing')} "
        f"matter={st.get('is_matter')} bars={len(bars)} "
        f"captcha_closed={captcha_ok} err={out.get('error')!r}"
    )
    return out


def cancel_active_entry_session(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    hwnd: int = 0,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    force: bool | None = None,
    close_captcha: bool | None = None,
) -> dict:
    """
    Clear stuck captcha + 暗道 interact / perform after wrong answer / failed open.

    Official path (lab-aligned 2026-07-21):
      recover_entry_interact_state =
        Show-close captcha → stop_host_perform(+restore type=2) →
        clear Win_Prgs2 → light 0x21

    Legacy Esc/nudge kept only as optional residual inside recover / clear_bar.
    force is accepted for API compat; recovery always measures real UI/perform.

    @author by ak
    """
    log = log or (lambda _m: None)
    _ = force  # force historically meant "run even if cast idle"; always recover now
    do_close = bool(
        cfg.captcha_close_on_fail if close_captcha is None else close_captcha
    )
    before_cast = read_host_cast_state(session, log=log)
    before_pf = read_host_perform_state(session, log=lambda _m: None)
    log(
        f"yaolu cancel-session before cast={_cast_state_text(before_cast)} "
        f"perform type={before_pf.get('perform_type')} "
        f"matter={before_pf.get('is_matter')} base_missing={before_pf.get('base_missing')}"
    )
    rec = recover_entry_interact_state(
        session,
        cfg,
        hwnd=int(hwnd or 0),
        stop_event=stop_event,
        log=log,
        status=lambda m: log(f"yaolu cancel-session {m}"),
        close_captcha=do_close,
        clear_bar=True,
        stop_perform=True,
        use_bridge_cancel=bool(cfg.use_bridge),
    )
    after_cast = read_host_cast_state(session, log=lambda _m: None)
    after_pf = rec.get("perform_after") or read_host_perform_state(
        session, log=lambda _m: None
    )
    out: dict = {
        "ok": bool(rec.get("ok")),
        "needed": True,
        "queued": bool((rec.get("cancel21") or {}).get("queued")),
        "already_idle": False,
        "before": before_cast,
        "after": after_cast,
        "error": rec.get("error"),
        "method": rec.get("method") or "recover_entry_interact_state",
        "pulses": int((rec.get("cancel21") or {}).get("pulses") or 0),
        "esc": 0,
        "nudge": bool((rec.get("clear_bar") or {}).get("nudge") or False),
        "nudge_count": int((rec.get("clear_bar") or {}).get("nudge_count") or 0),
        "captcha_closed": bool(rec.get("captcha_closed")),
        "captcha_was_open": bool(rec.get("captcha_was_open")),
        "captcha_close": rec.get("captcha_close"),
        "captcha_still_open": not bool(rec.get("captcha_closed")),
        "cast_busy": _cast_session_busy_state(after_cast),
        "perform_stop": rec.get("perform_stop"),
        "clear_bar": rec.get("clear_bar"),
        "perform_after": after_pf,
        "bars_after": rec.get("bars_after") or [],
        "note": rec.get("note") or "",
        "recover": rec,
    }
    # Soft: if measured recovery ok, drop transport errors.
    if out["ok"]:
        out["error"] = None
    log(
        f"yaolu cancel-session RESULT ok={out['ok']} method={out['method']} "
        f"captcha_closed={out['captcha_closed']} "
        f"type={after_pf.get('perform_type')} matter={after_pf.get('is_matter')} "
        f"base_missing={after_pf.get('base_missing')} "
        f"bars={len(out.get('bars_after') or [])} "
        f"queued={out['queued']} err={out.get('error')!r} note={out.get('note')!r}"
    )
    return out


def path_to_entry_anchor(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    hwnd: int = 0,
    anchor: tuple[float, float, float] | None = None,
    host_pos: tuple[float, float, float] | None = None,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    HostMove to 妖楼暗道 stand point (Fuzhou), cross-map allowed.

    mode = entry_path_scene_id (default 68) so path works from other maps.
    """
    log = log or (lambda _m: None)
    quiet = lambda _m: None  # noqa: E731
    live_sid: int | None = None
    sp = read_scene_position(session, log=quiet)
    if sp.ok:
        live_sid = sp.scene_id
        if host_pos is None and sp.scene_pos:
            host_pos = sp.scene_pos
    base = anchor or DEFAULT_ENTRY_ANCHOR
    tx, ty, tz = float(base[0]), float(base[1]), float(base[2])
    # Always use Fuzhou scene_id for cross-map; never collapse to live non-68.
    path_sid = int(cfg.entry_path_scene_id or ENTRY_PATH_SCENE_ID)
    if path_sid <= 0:
        path_sid = ENTRY_PATH_SCENE_ID
    log(
        f"yaolu path-to-entry ({tx:.2f},{ty:.2f},{tz:.2f}) "
        f"mode={path_sid} live_scene={live_sid}"
    )
    target = SuperLootTarget(
        name="九层妖楼入口",
        ptr=0,
        x=tx,
        y=ty,
        z=tz,
    )
    # Explicit scene mode: no live-scene resolve, no mode=0 fallback inside hop.
    moved = move_to_target(
        session,
        target,
        hwnd=int(hwnd),
        move_mode=int(path_sid),
        use_bridge=bool(cfg.use_bridge),
        host_pos=host_pos,
        scene_id=int(path_sid),
        stop_event=stop_event,
        log=log,
    )
    out: dict = dict(moved)
    out["target_xyz"] = [tx, ty, tz]
    out["path_scene_id"] = path_sid
    out["live_scene_id"] = live_sid
    out["purpose"] = "cross_map_path_to_entry"
    out["anchor"] = [tx, ty, tz]
    out["hwnd"] = int(hwnd or 0)
    log(
        f"yaolu path-to-entry ok={out.get('ok')} via={out.get('via')} "
        f"mode={out.get('mode')} ret={out.get('ret')} err={out.get('error')}"
    )
    return out


def wait_for_entry_arrival(
    session: GameAttachSession,
    move: dict,
    cfg: YaoluConfig,
    *,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    on_progress: StatusFn | None = None,
) -> tuple[bool, tuple[float, float, float] | None, float | None]:
    """
    Wait until host is near entry anchor on Fuzhou (or target scene).

    Arrival requires a readable Fuzhou/target scene_id and distance within
    radius. Unknown scene_id alone never counts as arrived.
    """
    log = log or (lambda _m: None)
    on_progress = on_progress or (lambda _m: None)
    raw_target = move.get("target_xyz") or move.get("anchor") or []
    if len(raw_target) < 3:
        return False, None, None
    target = (float(raw_target[0]), float(raw_target[1]), float(raw_target[2]))
    want_sid = int(
        move.get("path_scene_id")
        or cfg.entry_path_scene_id
        or ENTRY_PATH_SCENE_ID
    )
    radius = max(0.5, float(cfg.entry_path_arrive_radius))
    deadline = time.monotonic() + max(5.0, float(cfg.entry_path_timeout_s))
    last_pos: tuple[float, float, float] | None = None
    last_dist: float | None = None
    last_sid: int | None = None
    last_move_ts = time.monotonic()
    last_report_s: int | None = None
    nudge_s = max(2.0, float(cfg.entry_path_nudge_s))
    nudged = 0
    max_nudges = 4
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False, last_pos, last_dist
        sp = read_scene_position(session, log=lambda _m: None)
        if sp.ok and sp.scene_pos:
            current = sp.scene_pos
            last_sid = int(sp.scene_id) if sp.scene_id is not None else last_sid
            if last_pos is not None and _dist_xz(last_pos, current) > float(
                cfg.wander_stop_delta_m
            ):
                last_move_ts = time.monotonic()
            last_pos = current
            last_dist = _dist_xz(last_pos, target)
            # Require known scene on Fuzhou/target; never treat scene=? as arrived.
            # Already in dungeon: stop path-wait (caller claims enter success).
            if last_sid is not None and is_yaolu_scene(last_sid, None):
                log(
                    f"yaolu entry wait: already in yaolu scene={last_sid} "
                    f"d={last_dist}"
                )
                on_progress(
                    f"已在妖楼 scene={last_sid}，停止寻路等待"
                )
                return True, last_pos, last_dist
            on_fuzhou = last_sid is not None and (
                int(last_sid) == int(want_sid) or is_fuzhou_scene(last_sid, None)
            )
            if on_fuzhou and last_dist is not None and last_dist <= radius:
                log(
                    f"yaolu entry arrived d={last_dist:.1f} scene={last_sid} "
                    f"target={target}"
                )
                return True, last_pos, last_dist
            now = time.monotonic()
            remain_s = max(0, int(math.ceil(deadline - now)))
            if remain_s != last_report_s:
                last_report_s = remain_s
                sid_txt = f"scene={last_sid}" if last_sid is not None else "scene=?"
                dist_txt = (
                    f"{last_dist:.1f}m" if last_dist is not None else "未知"
                )
                if last_sid is not None and not on_fuzhou:
                    on_progress(
                        f"寻路到妖楼入口：跨图中 {sid_txt} 距目标 {dist_txt}，"
                        f"剩余 {remain_s}s"
                    )
                else:
                    on_progress(
                        f"寻路到妖楼入口：{sid_txt} 距目标 {dist_txt}，剩余 {remain_s}s"
                    )
            # Re-issue HostMove if stuck far from target.
            if (
                last_dist is not None
                and last_dist > radius
                and now - last_move_ts >= nudge_s
                and nudged < max_nudges
            ):
                nudged += 1
                log(
                    f"yaolu entry path nudge#{nudged} d={last_dist:.1f} "
                    f"scene={last_sid}"
                )
                path_to_entry_anchor(
                    session,
                    cfg,
                    hwnd=int(move.get("hwnd") or 0),
                    anchor=target,
                    host_pos=last_pos,
                    stop_event=stop_event,
                    log=log,
                )
                last_move_ts = now
        else:
            now = time.monotonic()
            remain_s = max(0, int(math.ceil(deadline - now)))
            if remain_s != last_report_s:
                last_report_s = remain_s
                on_progress(f"寻路到妖楼入口：暂时无法读取坐标，剩余 {remain_s}s")
        if not _sleep_interruptible(float(cfg.wander_poll_s), stop_event):
            return False, last_pos, last_dist
    log(
        f"yaolu entry path timeout last_d="
        f"{last_dist if last_dist is not None else 'n/a'} scene={last_sid}"
    )
    return False, last_pos, last_dist


def wander_far_from_entry(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    hwnd: int = 0,
    anchor: tuple[float, float, float] | None = None,
    host_pos: tuple[float, float, float] | None = None,
    log: LogFn | None = None,
) -> dict:
    """
    Random XZ hop near entry (same-map). Prefer path_to_entry_anchor for recover.
    """
    log = log or (lambda _m: None)
    quiet = lambda _m: None  # noqa: E731
    scene_id: int | None = None
    sp = read_scene_position(session, log=quiet)
    if sp.ok:
        scene_id = sp.scene_id
        if host_pos is None and sp.scene_pos:
            host_pos = sp.scene_pos
    base = anchor or host_pos or DEFAULT_ENTRY_ANCHOR
    ang = random.uniform(0.0, math.pi * 2.0)
    dist = random.uniform(float(cfg.wander_min_dist), float(cfg.wander_max_dist))
    tx = float(base[0]) + math.cos(ang) * dist
    ty = float(base[1])
    tz = float(base[2]) + math.sin(ang) * dist
    log(f"yaolu wander to ({tx:.1f},{ty:.1f},{tz:.1f}) dist={dist:.1f}")
    out: dict = {
        "ok": False,
        "target_xyz": [tx, ty, tz],
        "dist": dist,
        "via": "",
    }
    path_sid = int(cfg.entry_path_scene_id or scene_id or ENTRY_PATH_SCENE_ID)
    target = SuperLootTarget(
        name="九层妖楼附近游走",
        ptr=0,
        x=tx,
        y=ty,
        z=tz,
    )
    moved = move_to_target(
        session,
        target,
        hwnd=int(hwnd),
        move_mode=int(path_sid),
        use_bridge=bool(cfg.use_bridge),
        host_pos=host_pos,
        scene_id=int(path_sid),
        log=log,
    )
    out.update(moved)
    out["target_xyz"] = [tx, ty, tz]
    out["dist"] = dist
    out["scene_id"] = path_sid
    out["path_scene_id"] = path_sid
    out["purpose"] = "wander_near_entry"
    log(
        f"yaolu wander path ok={out.get('ok')} via={out.get('via')} "
        f"mode={out.get('mode')} ret={out.get('ret')} err={out.get('error')}"
    )
    return out


def wait_for_wander_arrival(
    session: GameAttachSession,
    move: dict,
    cfg: YaoluConfig,
    *,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    on_progress: StatusFn | None = None,
) -> tuple[bool, tuple[float, float, float] | None, float | None]:
    """Wait for the random HostMove target to be reached in live coordinates."""
    log = log or (lambda _m: None)
    on_progress = on_progress or (lambda _m: None)
    raw_target = move.get("target_xyz") or []
    if len(raw_target) < 3:
        return False, None, None
    target = (float(raw_target[0]), float(raw_target[1]), float(raw_target[2]))
    radius = max(0.5, float(cfg.wander_arrive_radius))
    deadline = time.monotonic() + max(1.0, float(cfg.wander_arrive_timeout_s))
    last_pos: tuple[float, float, float] | None = None
    last_dist: float | None = None
    stopped_hits = 0
    need_stopped = max(1, int(cfg.wander_stop_hits))
    last_move_ts = time.monotonic()
    last_report_s: int | None = None
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False, last_pos, last_dist
        sp = read_scene_position(session, log=lambda _m: None)
        if sp.ok and sp.scene_pos:
            current = sp.scene_pos
            moved = last_pos is not None and _dist_xz(last_pos, current) > float(
                cfg.wander_stop_delta_m
            )
            if moved:
                last_move_ts = time.monotonic()
                stopped_hits = 0
            elif last_pos is not None:
                stopped_hits += 1
            else:
                stopped_hits = 0
            last_pos = current
            last_dist = _dist_xz(last_pos, target)
            if last_dist <= radius and stopped_hits >= need_stopped:
                log(
                    f"yaolu wander arrived+stopped d={last_dist:.1f} <= "
                    f"radius={radius:.1f} target={target}"
                )
                return True, last_pos, last_dist
            now = time.monotonic()
            remain_s = max(0, int(math.ceil(deadline - now)))
            if remain_s != last_report_s:
                last_report_s = remain_s
                on_progress(
                    f"随机寻路确认中：距目标 {last_dist:.1f}m，"
                    f"剩余 {remain_s}s（未到将换点）"
                )
            stuck_after = max(1.0, float(cfg.wander_stuck_timeout_s))
            if last_dist > radius and now - last_move_ts >= stuck_after:
                message = (
                    f"随机寻路卡住：{stuck_after:.0f}s 未检测到坐标移动，"
                    f"距目标 {last_dist:.1f}m，立即换点"
                )
                log(f"yaolu {message}")
                on_progress(message)
                return False, last_pos, last_dist
        else:
            now = time.monotonic()
            remain_s = max(0, int(math.ceil(deadline - now)))
            if remain_s != last_report_s:
                last_report_s = remain_s
                on_progress(f"随机寻路确认中：暂时无法读取坐标，剩余 {remain_s}s")
        if not _sleep_interruptible(float(cfg.wander_poll_s), stop_event):
            return False, last_pos, last_dist
    log(
        f"yaolu wander timeout last_d="
        f"{last_dist if last_dist is not None else 'n/a'} target={target}"
    )
    return False, last_pos, last_dist


def wait_return_fuzhou(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
) -> bool:
    """
    Block until back in Fuzhou or timeout/stop.

    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    deadline = time.time() + float(cfg.wait_return_fuzhou_s)
    quiet = lambda _m: None  # noqa: E731
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return False
        sid, _pos, label = read_scene_state(session, log=quiet)
        if is_fuzhou_scene(sid, label):
            status(f"已回福州 {label}")
            return True
        status(f"等待回福州… {label} scene={sid}")
        poll = max(5.0, float(getattr(cfg, "wait_return_poll_s", 15.0) or 15.0))
        if not _sleep_interruptible(poll, stop_event):
            return False
    return False


def wait_entry_reopen_result(
    session: GameAttachSession,
    cfg: YaoluConfig,
    *,
    start_scene: int | None,
    entry_block_baseline: dict[str, int],
    stop_event: threading.Event | None = None,
    log: LogFn | None = None,
    status: StatusFn | None = None,
) -> tuple[str, int | None, str, CaptchaAnswerFeedback | None]:
    """
    After an answered retry click, wait for enter, captcha, or block.

    Uses the same monotonic enter_timeout budget as captcha submit wait.
    Entry-block probes stay UI-only so the loop cannot stall on process scans.

    @author by ak
    """
    log = log or (lambda _m: None)
    status = status or (lambda _m: None)
    budget = max(2.0, float(cfg.enter_timeout_s))
    deadline = time.monotonic() + budget
    last_sid: int | None = start_scene
    last_label = ""
    last_countdown_s: int | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if stop_event is not None and stop_event.is_set():
            return "stopped", last_sid, last_label, None
        remain_s = max(0, int(math.ceil(remaining)))
        if remain_s != last_countdown_s:
            last_countdown_s = remain_s
            status(
                f"免答题重开入口：等待进图/条件结果，剩余 {remain_s}s"
            )
        sid, _pos, label = read_scene_state(session, log=lambda _m: None)
        last_sid, last_label = sid, label or ""
        if sid is not None and (
            is_yaolu_scene(sid, label)
            or (
                start_scene is not None
                and int(sid) != int(start_scene)
                and not is_fuzhou_scene(sid, label)
            )
        ):
            return "entered", sid, label, None
        cap = is_captcha_dialog_open(
            session, names=cfg.captcha_dlg_names, log=lambda _m: None
        )
        if cap.shown:
            return "captcha", sid, label, None
        blocked = probe_entry_open_block_text(
            session,
            baseline=entry_block_baseline,
            log=log,
            heavy=False,
        )
        if blocked is not None:
            return "blocked", sid, label, blocked
        if not _sleep_interruptible(min(0.6, max(0.05, remaining)), stop_event):
            return "stopped", last_sid, last_label, None
    return "timeout", last_sid, last_label, None


class YaoluRunner:
    """
    Background full-auto loop for 九层妖楼 enter.

    @author by ak
    """

    def __init__(
        self,
        *,
        pid: int,
        hwnd: int = 0,
        cfg: YaoluConfig | None = None,
        on_event: Callable[[YaoluStepEvent], None] | None = None,
        log: LogFn | None = None,
    ):
        self.pid = int(pid)
        self.hwnd = int(hwnd or 0)
        self.cfg = cfg or YaoluConfig()
        # Track whether a real UI sink is wired so _emit does not also log
        # the same line (UI already prints 「九层妖楼 [phase] …」).
        self._has_event_sink = on_event is not None
        self.on_event = on_event or (lambda _e: None)
        self.log = log or (lambda _m: None)
        self._lifecycle = RunnerLifecycle(f"xajh-yaolu-{self.pid}")
        self._stop = self._lifecycle.stop_event
        self._thread: threading.Thread | None = None
        self._session: GameAttachSession | None = None
        self.running = False
        self._rounds = 0
        self._ok_count = 0
        self._fail_count = 0
        # Monotonic timestamp: next allowed open attempt (entry CD).
        self._next_open_ts: float = 0.0
        self._entry_reopen_pending = False
        self._entry_reopen_attempt = 0
        self._entry_reopen_total_s = 0.0
        self._entry_reopen_next_ts = 0.0
        self._entry_reopen_reason = ""
        self._run_id: str = ""
        self._run_answer_fail: int = 0
        # Consecutive captcha identify/API failures (HTTP 500 / timeout…).
        self._captcha_api_fail_streak = 0
        self._answer_fail_streak = 0
        self._next_open_reason: str = "入口CD"
        # After identify fail: force close + re-open entry (do not re-use stale dlg).
        self._force_reopen_entry = False
        # Specific stop reason (神罚/识别失败/…) so finally does not overwrite UI.
        self._last_stop_reason: str = ""
        # After cross-map / leave dungeon: need ~20s settle before open.
        self._need_map_settle: bool = False

    def start(self) -> None:
        """Start background loop. @author by ak"""
        if self.running:
            return
        # Risk gates (神罚日锁 / 日成功上限) block starting a new session.
        blocked, why = self._risk_start_blocked()
        if blocked:
            self._last_stop_reason = why
            self._emit("stopped", why, ok=False, reason="risk_gate")
            self._audit(
                "risk_gate",
                ok=False,
                message=why,
                daily_ok=self._daily_ok_persisted_safe(),
            )
            self.running = False
            return
        # Per-run enter success counter (UI mirrors for 成功×N / 妖楼×N).
        self._ok_count = 0
        self._fail_count = 0
        self._answer_fail_streak = 0
        self._run_answer_fail = 0
        self._next_open_ts = 0.0
        self._next_open_reason = "入口CD"
        self._force_reopen_entry = False
        self._last_stop_reason = ""
        self._need_map_settle = False
        self._captcha_api_fail_streak = 0
        self._reset_entry_reopen()
        self._run_id = uuid.uuid4().hex[:12]
        self._write_run_start_audit()
        thread = self._lifecycle.start(self._loop)
        if thread is not None:
            self._thread = thread
            self.running = True

    def stop(self) -> bool:
        """Request stop. @author by ak"""
        stopped = self._lifecycle.stop(wait=True)
        self.running = False
        return stopped

    def is_running(self) -> bool:
        return bool(self.running and self._lifecycle.is_running())

    def _emit(self, phase: str, message: str, ok: bool = True, **detail) -> None:
        """
        Push UI event. When a UI sink is wired, YaoluPage already logs
        「九层妖楼 [phase] …」 — skip the duplicate yaolu line.
        """
        if self._rounds:
            detail.setdefault("round", self._rounds)
        # Keep the first specific stop reason; ignore summary "已停止 ok=N fail=M".
        if phase in ("stopped", "stop"):
            msg = str(message or "").strip()
            if msg and not msg.startswith("已停止 ok="):
                self._last_stop_reason = msg
        ev = YaoluStepEvent(phase=phase, message=message, ok=ok, detail=detail)
        try:
            self.on_event(ev)
        except Exception:
            pass
        if not self._has_event_sink:
            self.log(f"yaolu [{phase}] {message}")

    def _status(self, msg: str) -> None:
        self._emit("status", msg, ok=True)

    def _mark_map_settle_needed(self, why: str = "") -> None:
        """
        Arm settle ONLY after real scene transfer (跨图/出本进福州).

        Already standing in 福州城 does not need this — no 不稳定状态 there.

        @author by ak
        """
        self._need_map_settle = True
        if why:
            self.log(f"yaolu map-settle armed: {why}")

    def _consume_map_settle_if_needed(
        self,
        *,
        phase: str = "path_open",
    ) -> bool:
        """
        Sleep post_map_settle_s once when armed. Return False if stopped.

        Status bar shows a live countdown so the 20s wait is not a blank hang.

        @author by ak
        """
        if not bool(getattr(self, "_need_map_settle", False)):
            return True
        delay = max(0.0, float(getattr(self.cfg, "post_map_settle_s", 20.0) or 0.0))
        self._need_map_settle = False
        if delay <= 0.05:
            return True
        text = str(
            getattr(self.cfg, "unstable_state_text", "") or "不稳定状态无法进行此操作"
        )

        def _tick(msg: str) -> None:
            self._emit(
                phase,
                msg,
                ok=True,
                reason="map_settle",
                settle_s=delay,
            )

        self._emit(
            phase,
            f"跨图/切图后等待稳定，共 {delay:.0f}s（仅新进地图；避免：{text}）",
            ok=True,
            reason="map_settle",
            settle_s=delay,
        )
        return _sleep_with_status_countdown(
            delay,
            self._stop,
            on_tick=_tick,
            prefix="跨图/切图后等待稳定",
            reason=f"仅新进地图，避免：{text}",
            phase_log=self.log,
        )

    def _entry_block_reason(self, error: str | None) -> str | None:
        text = str(error or "")
        prefix = "entry_block:"
        if not text.startswith(prefix):
            return None
        return text[len(prefix) :].strip() or ENTRY_OPEN_BLOCK_TEXT

    def _stop_for_entry_block(self, error: str | None) -> bool:
        """Legacy explicit-stop helper kept for direct callers and diagnostics."""
        reason = self._entry_block_reason(error)
        if reason is None:
            return False
        self._fail_count += 1
        self._emit(
            "stopped",
            f"副本开启条件不满足，已停止自动: {reason}",
            ok=False,
            reason="entry_open_block",
        )
        self._stop.set()
        self.running = False
        return True

    def _arm_entry_cd(self, reason: str) -> float:
        """
        Arm entry open CD after a failed open / wrong-answer path.

        Returns sleep seconds applied. Path recovery may clear earlier stamps;
        always re-arm after recover when the entry itself is on cooldown.

        @author by ak
        """
        sleep_s = float(self.cfg.entry_cd_sleep_s())
        return self._arm_open_rest(sleep_s, reason, label="入口CD")

    def _arm_open_rest(
        self,
        sleep_s: float,
        reason: str,
        *,
        label: str = "冷却",
        extend_only: bool = True,
    ) -> float:
        """
        Schedule next open time. extend_only keeps a longer existing rest.
        """
        sleep_s = max(0.0, float(sleep_s))
        target = time.monotonic() + sleep_s
        if extend_only and target < float(self._next_open_ts or 0.0):
            # Keep longer rest already armed.
            remain = max(0.0, float(self._next_open_ts) - time.monotonic())
            self._emit(
                "rest",
                f"{label}保持更长等待 剩余 {remain:.0f}s（忽略更短 {sleep_s:.0f}s / {reason}）",
                ok=False,
                sleep_s=remain,
                reason=reason,
                label=label,
            )
            return remain
        self._next_open_ts = target
        self._next_open_reason = str(label or "冷却")
        self._emit(
            "rest" if label != "入口CD" else "entry_cd",
            f"{self._next_open_reason} {sleep_s:.1f}s（{reason}）",
            ok=False,
            sleep_s=sleep_s,
            reason=reason,
            label=self._next_open_reason,
        )
        return sleep_s

    def _wait_entry_cd(self) -> bool:
        """
        Block until entry CD / rest elapsed. False if stopped.

        @author by ak
        """
        label = str(getattr(self, "_next_open_reason", "") or "入口CD")
        while not self._stop.is_set():
            remain = float(self._next_open_ts) - time.monotonic()
            if remain <= 0.05:
                return True
            # Friendly countdown for long rests (show mm:ss when >= 60s).
            if remain >= 60.0:
                mm = int(remain // 60)
                ss = int(remain % 60)
                self._status(f"{label}中… {mm}分{ss:02d}秒")
            else:
                self._status(f"{label}中… {remain:.1f}s")
            tick = 1.0 if remain >= 60.0 else 0.5
            if not _sleep_interruptible(min(remain, tick), self._stop):
                return False
        return False

    def _arm_and_wait_entry_cd(self, reason: str) -> bool:
        """
        Arm entry CD then wait it out (status shows countdown).

        @author by ak
        """
        self._arm_entry_cd(reason)
        return self._wait_entry_cd()

    def _fail_sleep_random(self, reason: str) -> bool:
        """
        Short random sleep for non-answer enter miss (timeout/unknown).

        @author by ak
        """
        lo = max(0.0, float(self.cfg.fail_sleep_min_s))
        hi = max(lo, float(self.cfg.fail_sleep_max_s))
        sleep_s = random.uniform(lo, hi) if hi > 0 else 0.0
        self._next_open_ts = time.monotonic() + sleep_s
        self._next_open_reason = "失败冷却"
        self._emit(
            "fail_sleep",
            f"失败冷却 {sleep_s:.1f}s（{reason}）",
            ok=False,
            sleep_s=sleep_s,
            reason=reason,
        )
        self._status(f"失败冷却中… {sleep_s:.1f}s（{reason}）")
        if sleep_s <= 0:
            return not self._stop.is_set()
        return self._wait_entry_cd()

    # ---- risk state (daily cap / 神罚 day lock / pacing) ----

    def _risk_enabled(self) -> bool:
        return bool(getattr(self.cfg, "risk_state_enabled", True))

    def _audit_enabled(self) -> bool:
        return bool(getattr(self.cfg, "risk_audit_enabled", True))

    def _daily_ok_persisted_safe(self) -> int:
        try:
            return int(self._daily_ok_persisted())
        except Exception:
            return 0

    def _audit_counters(self) -> dict[str, Any]:
        return {
            "ok_count": int(getattr(self, "_ok_count", 0) or 0),
            "fail_count": int(getattr(self, "_fail_count", 0) or 0),
            "answer_fail_streak": int(getattr(self, "_answer_fail_streak", 0) or 0),
            "run_answer_fail": int(getattr(self, "_run_answer_fail", 0) or 0),
            "daily_ok": self._daily_ok_persisted_safe(),
            "entry_reopen_attempt": int(getattr(self, "_entry_reopen_attempt", 0) or 0),
            "entry_reopen_total_s": float(getattr(self, "_entry_reopen_total_s", 0.0) or 0.0),
        }

    def _audit(self, event: str, *, ok: bool = True, message: str = "", **extra: Any) -> None:
        if not self._audit_enabled():
            return
        cfg_risk = snapshot_yaolu_risk_cfg(self.cfg)
        rec: dict[str, Any] = {
            "schema_version": YAOLU_RISK_AUDIT_SCHEMA,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "ts_unix": time.time(),
            "event": str(event or ""),
            "run_id": str(getattr(self, "_run_id", "") or ""),
            "pid": int(self.pid),
            "profile_id": str(getattr(self.cfg, "profile_id", "") or ""),
            "ok": bool(ok),
            "message": str(message or "")[:500],
            "counters": self._audit_counters(),
        }
        if extra:
            # Keep payload compact; drop secrets if any slip in.
            extra = {k: v for k, v in extra.items() if "api_key" not in str(k).lower()}
            rec["detail"] = extra
        append_yaolu_risk_audit(rec)

    def _write_run_start_audit(self) -> None:
        cfg_risk = snapshot_yaolu_risk_cfg(self.cfg)
        diff = diff_yaolu_risk_vs_baseline(cfg_risk)
        profile = str(getattr(self.cfg, "profile_id", "") or "")
        note = str(getattr(self.cfg, "profile_note", "") or "")
        daily = self._daily_ok_persisted_safe()
        summary = (
            f"profile={profile} daily_ok={daily} "
            f"diff_keys={len(diff)} run_id={self._run_id}"
        )
        self.log(f"yaolu risk audit start {summary}")
        self._status(f"风控画像 {profile} · 今日成功 {daily}")
        if self._audit_enabled():
            append_yaolu_risk_audit(
                {
                    "schema_version": YAOLU_RISK_AUDIT_SCHEMA,
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                    "ts_unix": time.time(),
                    "event": "run_start",
                    "run_id": str(self._run_id or ""),
                    "pid": int(self.pid),
                    "profile_id": profile,
                    "profile_note": note,
                    "ok": True,
                    "message": summary,
                    "cfg_risk": cfg_risk,
                    "diff_vs_baseline": diff,
                    "counters": self._audit_counters(),
                }
            )
        # Align risk_state with last run for postmortem joins.
        try:
            if self._risk_enabled():
                b = self._risk_bucket()
                b["last_profile_id"] = profile
                b["last_run_id"] = str(self._run_id or "")
                self._risk_save()
        except Exception:
            pass

    def _risk_bucket(self) -> dict:
        state = load_yaolu_risk_state() if self._risk_enabled() else {}
        bucket = yaolu_risk_pid_bucket(state, self.pid)
        # Re-bind state root for save
        self._risk_state_cache = state
        day = _local_day_key()
        if day not in state or not isinstance(state.get(day), dict):
            state[day] = {}
        state[day][str(int(self.pid))] = bucket
        # drop other days
        for k in list(state.keys()):
            if k != day:
                state.pop(k, None)
        return bucket

    def _risk_save(self) -> None:
        if not self._risk_enabled():
            return
        state = getattr(self, "_risk_state_cache", None)
        if isinstance(state, dict):
            save_yaolu_risk_state(state)

    def _daily_ok_persisted(self) -> int:
        try:
            return max(0, int(self._risk_bucket().get("ok") or 0))
        except Exception:
            return 0

    def _bump_daily_ok(self) -> int:
        b = self._risk_bucket()
        b["ok"] = int(b.get("ok") or 0) + 1
        self._risk_save()
        return int(b["ok"])

    def _set_shenfa_day_lock(self) -> None:
        if not bool(getattr(self.cfg, "shenfa_lock_day", True)):
            return
        b = self._risk_bucket()
        b["shenfa_lock"] = True
        self._risk_save()

    def _is_shenfa_day_locked(self) -> bool:
        if not bool(getattr(self.cfg, "shenfa_lock_day", True)):
            return False
        try:
            return bool(self._risk_bucket().get("shenfa_lock"))
        except Exception:
            return False

    def _risk_start_blocked(self) -> tuple[bool, str]:
        if self._is_shenfa_day_locked():
            return True, (
                f"今日已触发神罚锁定（账号/进程 pid={self.pid}），"
                "为降低二次处罚已禁止启动妖楼全自动"
            )
        limit = int(getattr(self.cfg, "daily_success_limit", 0) or 0)
        if limit > 0:
            ok_day = self._daily_ok_persisted()
            if ok_day >= limit:
                return True, (
                    f"今日成功进本已达上限 {ok_day}/{limit}，"
                    "停止启动妖楼全自动（风控降频）"
                )
        return False, ""

    def _stop_for_risk(self, message: str, *, reason: str) -> None:
        self._last_stop_reason = message
        self._emit(
            "stopped",
            message,
            ok=False,
            reason=reason,
            profile_id=str(getattr(self.cfg, "profile_id", "") or ""),
            run_id=str(getattr(self, "_run_id", "") or ""),
            daily_ok=self._daily_ok_persisted_safe(),
        )
        self._audit(str(reason or "risk_stop"), ok=False, message=message)
        self._stop.set()
        self.running = False

    def _maybe_stop_daily_limit(self) -> bool:
        """True if stopped due to daily success limit."""
        limit = int(getattr(self.cfg, "daily_success_limit", 0) or 0)
        if limit <= 0:
            return False
        ok_day = self._daily_ok_persisted()
        # session ok already counted into persist before call
        if ok_day >= limit:
            self._stop_for_risk(
                f"今日成功进本已达上限 {ok_day}/{limit}，停止自动（风控降频）",
                reason="daily_success_limit",
            )
            return True
        return False

    def _on_shenfa_stop(self, msg: str) -> None:
        self._set_shenfa_day_lock()
        self._last_stop_reason = msg
        c = self._audit_counters()
        profile = str(getattr(self.cfg, "profile_id", "") or "")
        summary = (
            f"profile={profile} daily_ok={c.get('daily_ok')} "
            f"answer_fail={c.get('run_answer_fail')} run_id={self._run_id}"
        )
        self.log(f"yaolu shenfa stop {summary}")
        self._emit(
            "stopped",
            f"{msg}（{summary}）",
            ok=False,
            reason="shenfa",
            profile_id=profile,
            daily_ok=c.get("daily_ok"),
            run_answer_fail=c.get("run_answer_fail"),
            run_id=self._run_id,
        )
        self._audit("shenfa", ok=False, message=msg, **c)
        self._stop.set()
        self.running = False

    def _reset_entry_reopen(self) -> None:
        self._entry_reopen_pending = False
        self._entry_reopen_attempt = 0
        self._entry_reopen_total_s = 0.0
        self._entry_reopen_next_ts = 0.0
        self._entry_reopen_reason = ""

    def _entry_reopen_delay_s(self, attempt_index: int) -> float:
        """
        Heartbeat delay for the next free-entry reopen.

        base * growth^attempt, then random ±jitter (not a fixed ladder).
        attempt_index is 0-based for the upcoming schedule.
        """
        cfg = self.cfg
        base = max(1.0, float(getattr(cfg, "entry_reopen_base_s", 30.0) or 30.0))
        growth = float(getattr(cfg, "entry_reopen_growth", 1.6) or 1.6)
        if growth < 1.0:
            growth = 1.0
        jitter = max(0.0, min(0.5, float(getattr(cfg, "entry_reopen_jitter", 0.18) or 0.0)))
        raw = base * (growth ** max(0, int(attempt_index)))
        if jitter > 0:
            factor = random.uniform(1.0 - jitter, 1.0 + jitter)
            raw *= factor
        return max(1.0, float(raw))

    def _schedule_entry_reopen(self, reason: str) -> bool:
        """
        Schedule free-entry reopen heartbeat (answer already OK / soft block).

        Grows with entry_reopen_growth and random jitter each step.
        Stops full-auto when cumulative planned wait reaches entry_reopen_max_total_s
        (default 1800s / half hour).
        """
        cfg = self.cfg
        max_total = max(1.0, float(getattr(cfg, "entry_reopen_max_total_s", 1800.0) or 1800.0))
        if self._entry_reopen_attempt > 0 and self._entry_reopen_total_s >= max_total:
            msg = (
                f"入口心跳累计计划等待 {self._entry_reopen_total_s:.0f}s "
                f"已超过 {max_total:.0f}s（约半小时），仍未进入妖楼，停止自动；"
                f"最后原因: {reason}"
            )
            self._status(msg)
            self._emit(
                "stopped",
                msg,
                ok=False,
                reason="entry_reopen_timeout",
                attempts=self._entry_reopen_attempt,
                total_wait_s=self._entry_reopen_total_s,
                max_total_s=max_total,
            )
            self._audit(
                "entry_reopen_timeout",
                ok=False,
                message=msg,
                attempts=self._entry_reopen_attempt,
                total_wait_s=self._entry_reopen_total_s,
                max_total_s=max_total,
            )
            self._last_stop_reason = msg
            self._stop.set()
            self.running = False
            return False

        delay = self._entry_reopen_delay_s(self._entry_reopen_attempt)
        # Do not schedule a wait that would wildly overshoot the half-hour budget
        # on the last step; clamp only the final remainder, keep earlier steps free.
        remain_budget = max_total - float(self._entry_reopen_total_s)
        if self._entry_reopen_attempt > 0 and remain_budget > 0:
            delay = min(delay, max(1.0, remain_budget))

        self._entry_reopen_attempt += 1
        self._entry_reopen_total_s += delay
        self._entry_reopen_pending = True
        self._entry_reopen_next_ts = time.monotonic() + delay
        self._entry_reopen_reason = str(reason or ENTRY_OPEN_BLOCK_TEXT)
        status_line = (
            f"入口受阻，心跳第 {self._entry_reopen_attempt} 轮 "
            f"{delay:.0f}s 后重试（累计计划 {self._entry_reopen_total_s:.0f}/"
            f"{max_total:.0f}s）；原因: {self._entry_reopen_reason}"
        )
        self._status(status_line)
        self._emit(
            "entry_retry",
            status_line,
            ok=False,
            reason="entry_reopen_scheduled",
            attempt=self._entry_reopen_attempt,
            delay_s=delay,
            total_wait_s=self._entry_reopen_total_s,
            max_total_s=max_total,
        )
        self._audit(
            "entry_reopen_scheduled",
            ok=False,
            message=status_line,
            attempt=self._entry_reopen_attempt,
            delay_s=delay,
            total_wait_s=self._entry_reopen_total_s,
            max_total_s=max_total,
            reopen_reason=str(reason or ""),
        )
        return True

    def _handle_entry_block_error(self, error: str | None) -> bool:
        reason = self._entry_block_reason(error)
        if reason is None:
            return False
        self._fail_count += 1
        self._schedule_entry_reopen(reason)
        return True

    def _wait_entry_reopen_heartbeat(self) -> bool:
        if not self._entry_reopen_pending:
            return True
        max_total = max(1.0, float(getattr(self.cfg, "entry_reopen_max_total_s", 1800.0) or 1800.0))
        remain = self._entry_reopen_next_ts - time.monotonic()
        if remain > 0:
            end_ts = time.monotonic() + remain
            while True:
                left = end_ts - time.monotonic()
                if left <= 0.05:
                    break
                self._status(
                    f"入口心跳等待中：第 {self._entry_reopen_attempt} 轮 "
                    f"{left:.0f}s 后重试（累计计划 {self._entry_reopen_total_s:.0f}/"
                    f"{max_total:.0f}s）"
                )
                self._emit(
                    "entry_retry_wait",
                    (
                        f"入口心跳等待中：第 {self._entry_reopen_attempt} 轮 "
                        f"{left:.0f}s 后唤醒"
                    ),
                    ok=False,
                    attempt=self._entry_reopen_attempt,
                    remain_s=left,
                    total_wait_s=self._entry_reopen_total_s,
                    max_total_s=max_total,
                )
                if not _sleep_interruptible(min(left, 1.0), self._stop):
                    return False
        self._entry_reopen_next_ts = 0.0
        self._status(
            f"入口心跳第 {self._entry_reopen_attempt} 轮已唤醒，重新点击入口"
        )
        self._emit(
            "entry_retry_wake",
            f"入口心跳第 {self._entry_reopen_attempt} 轮已唤醒，重新点击入口",
            ok=True,
            attempt=self._entry_reopen_attempt,
            total_wait_s=self._entry_reopen_total_s,
        )
        return True

    @staticmethod
    def _entry_anchor(
        result: SuperLootStepResult | None,
        fallback: tuple[float, float, float] | None,
    ) -> tuple[float, float, float] | None:
        """Prefer fixed Fuzhou stand for cross-map path; entity xyz only if near."""
        # Always prefer known 暗道 stand so recover/prepath works off-map.
        if fallback is not None:
            d_fb = _dist_xz(fallback, DEFAULT_ENTRY_ANCHOR)
            # If fallback is already near the fixed stand, keep it; else use default.
            if d_fb <= 15.0:
                return fallback
        target = result.target if result is not None else None
        if target and target.get("x") is not None and target.get("z") is not None:
            ent = (
                float(target["x"]),
                float(target.get("y") or 0.0),
                float(target["z"]),
            )
            # Entity scan coords are only useful when already near the entry.
            if fallback is not None and _dist_xz(fallback, ent) <= 20.0:
                return ent
            return DEFAULT_ENTRY_ANCHOR
        return fallback if fallback is not None else DEFAULT_ENTRY_ANCHOR

    def _path_to_entry_until_arrived(
        self,
        session: GameAttachSession,
        *,
        phase: str,
        reason: str,
        host_pos: tuple[float, float, float] | None = None,
        anchor: tuple[float, float, float] | None = None,
    ) -> bool:
        """Cross-map HostMove to 妖楼入口 anchor until live arrival."""
        attempt = 0
        dest = anchor or DEFAULT_ENTRY_ANCHOR
        while not self._stop.is_set():
            attempt += 1
            # Already transferred into 妖楼 (false-fail / free-enter cast done).
            try:
                sid_now, _p_now, lab_now = read_scene_state(
                    session, log=lambda _m: None
                )
            except Exception:
                sid_now, lab_now = None, ""
            if is_yaolu_scene(sid_now, lab_now):
                self._emit(
                    phase,
                    f"寻路中途纠偏：已在妖楼 scene={sid_now} {lab_now}",
                    ok=True,
                    scene_id=sid_now,
                )
                if phase == "recover":
                    self._finish_enter_success(
                        session,
                        scene_id=sid_now,
                        label=lab_now or "",
                        open_res=None,
                    )
                return True
            self._emit(
                phase,
                f"全图寻路到妖楼入口（第 {attempt} 次）：{reason}",
                ok=phase != "recover",
            )
            try:
                moved = path_to_entry_anchor(
                    session,
                    self.cfg,
                    hwnd=self.hwnd,
                    anchor=dest,
                    host_pos=host_pos,
                    stop_event=self._stop,
                    log=self.log,
                )
            except Exception as e:
                moved = {"ok": False, "error": str(e)}
                self.log(f"yaolu {phase} path-to-entry err: {e}")
            moved["hwnd"] = int(self.hwnd or 0)
            xyz = moved.get("target_xyz") or list(dest)
            point = (
                f"({float(xyz[0]):.1f},{float(xyz[1]):.1f},{float(xyz[2]):.1f})"
                if len(xyz) >= 3
                else "入口坐标"
            )
            mode = moved.get("mode") or moved.get("path_scene_id")
            if bool(moved.get("ok")):
                self._emit(
                    phase,
                    f"已发往妖楼入口 {point} mode={mode}，等待跨图/到达",
                    ok=phase != "recover",
                )
                arrived, host_pos, last_dist = wait_for_entry_arrival(
                    session,
                    moved,
                    self.cfg,
                    stop_event=self._stop,
                    log=self.log,
                    on_progress=lambda text: self._emit(
                        phase, text, ok=phase != "recover"
                    ),
                )
                if arrived:
                    # Brief settle so client stops walking before next open.
                    if not _sleep_interruptible(0.6, self._stop):
                        return False
                    self._next_open_ts = 0.0
                    self._emit(
                        phase,
                        f"已到达妖楼入口（距目标 {float(last_dist or 0):.1f}m），"
                        "开始下一轮完整流程",
                        ok=True,
                        move_ok=True,
                    )
                    return True
                if self._stop.is_set():
                    return False
                last = f"{float(last_dist):.1f}m" if last_dist is not None else "未知"
                self._emit(
                    phase,
                    f"尚未到达入口（距目标 {last}），重新发 HostMove",
                    ok=False,
                    move_ok=False,
                )
            else:
                self._emit(
                    phase,
                    f"寻路命令失败：{moved.get('error') or '未知错误'}；重试",
                    ok=False,
                    move_ok=False,
                )
            if not _sleep_interruptible(1.0, self._stop):
                return False
        return False

    def _wander_until_arrived(
        self,
        session: GameAttachSession,
        *,
        phase: str,
        reason: str,
        host_pos: tuple[float, float, float] | None = None,
        anchor: tuple[float, float, float] | None = None,
    ) -> bool:
        """Post-success nearby hop; recover uses _path_to_entry_until_arrived."""
        if phase == "recover":
            return self._path_to_entry_until_arrived(
                session,
                phase=phase,
                reason=reason,
                host_pos=host_pos,
                anchor=anchor,
            )
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            self._emit(
                phase,
                f"随机寻路到入口附近（第 {attempt} 次）：{reason}",
                ok=True,
            )
            try:
                moved = wander_far_from_entry(
                    session,
                    self.cfg,
                    hwnd=self.hwnd,
                    anchor=anchor,
                    host_pos=host_pos,
                    log=self.log,
                )
            except Exception as e:
                moved = {"ok": False, "error": str(e)}
                self.log(f"yaolu {phase} wander err: {e}")
            xyz = moved.get("target_xyz") or []
            point = (
                f"({float(xyz[0]):.1f},{float(xyz[1]):.1f},{float(xyz[2]):.1f})"
                if len(xyz) >= 3
                else "附近坐标"
            )
            if bool(moved.get("ok")):
                self._emit(
                    phase,
                    f"随机寻路已发往 {point}，等待角色实际到达后再进入下一轮",
                    ok=True,
                )
                arrived, host_pos, last_dist = wait_for_wander_arrival(
                    session,
                    moved,
                    self.cfg,
                    stop_event=self._stop,
                    log=self.log,
                    on_progress=lambda text: self._emit(phase, text, ok=True),
                )
                if arrived:
                    self._next_open_ts = 0.0
                    self._emit(
                        phase,
                        f"随机寻路已完成（距目标 {float(last_dist or 0):.1f}m），"
                        "已清除本轮状态，开始下一轮完整流程",
                        ok=True,
                        move_ok=True,
                    )
                    return True
                if self._stop.is_set():
                    return False
                last = f"{float(last_dist):.1f}m" if last_dist is not None else "未知"
                self._emit(
                    phase,
                    f"随机寻路未完成（距目标 {last}），更换附近坐标后继续寻路",
                    ok=False,
                    move_ok=False,
                )
            else:
                self._emit(
                    phase,
                    f"随机寻路命令失败：{moved.get('error') or '未知错误'}；更换坐标重试",
                    ok=False,
                    move_ok=False,
                )
            if not _sleep_interruptible(1.0, self._stop):
                return False
        return False

    def _try_claim_already_in_yaolu(
        self,
        session: GameAttachSession,
        *,
        open_res: SuperLootStepResult | None = None,
        reason: str = "mid_flow",
    ) -> bool:
        """
        If already in 妖楼, treat as enter success and wait-return.

        Corrects false fail/open-miss while transfer already completed.
        @author by ak
        """
        try:
            sid, _pos, label = read_scene_state(session, log=lambda _m: None)
        except Exception:
            return False
        if not is_yaolu_scene(sid, label):
            return False
        self.log(
            f"yaolu claim already-in-yaolu scene={sid} {label} via={reason}"
        )
        self._emit(
            "gate",
            f"中途纠偏：已在妖楼 scene={sid} {label}（{reason}）",
            ok=True,
            scene_id=sid,
            reason=f"claim_{reason}",
        )
        self._finish_enter_success(
            session,
            scene_id=sid,
            label=label or "",
            open_res=open_res,
        )
        return True

    def _recover_after_failure(
        self,
        session: GameAttachSession,
        reason: str,
        *,
        host_pos: tuple[float, float, float] | None = None,
        anchor: tuple[float, float, float] | None = None,
        dismiss_stuck_captcha: bool = False,
        clear_bar: bool = True,
    ) -> bool:
        """
        Recover after identify fail / 答案错误 / open fail.

        Lab-aligned official path (2026-07-21):
          A) captcha UI — close_captcha_dialog Show(0,0,1) only
             (never random wrong submit; protects captcha feedback audit)
          B) matter perform type 0x67 + Win_Prgs2 0% bar —
             stop_host_perform(+restore type=2) then clear_entry_interact_bar
          C) path back to DEFAULT_ENTRY_ANCHOR for a fresh MatterInteract

        @author by ak
        """
        cfg = self.cfg
        want_bar = bool(clear_bar)
        # dismiss_stuck_captcha kept for API compat; never random-submit wrongs.
        _ = bool(dismiss_stuck_captcha)

        # Already in 妖楼 (false-fail while transfer finished) — claim, do not cancel.
        if self._try_claim_already_in_yaolu(
            session, open_res=None, reason=f"recover:{reason}"
        ):
            return True

        self._emit(
            "recover",
            f"失败恢复：{reason}（关UI + 停perform/基座 + 清读条）",
            ok=True,
            reason="recover_start",
        )
        rec = recover_entry_interact_state(
            session,
            cfg,
            hwnd=self.hwnd,
            stop_event=self._stop,
            log=self.log,
            status=self._status,
            close_captcha=True,
            clear_bar=want_bar,
            stop_perform=True,
            use_bridge_cancel=bool(cfg.use_bridge),
        )
        pf = rec.get("perform_after") or {}
        bars = rec.get("bars_after") or []
        captcha_ok = bool(rec.get("captcha_closed"))
        self._emit(
            "recover",
            (
                f"恢复结果 ok={rec.get('ok')} method={rec.get('method')} "
                f"captcha_ok={captcha_ok} type={pf.get('perform_type')} "
                f"matter={pf.get('is_matter')} base_missing={pf.get('base_missing')} "
                f"bars={len(bars)} note={rec.get('note') or rec.get('error') or ''}"
            ),
            ok=bool(rec.get("ok")),
            reason="recover_result",
            recover_ok=bool(rec.get("ok")),
            captcha_closed=captcha_ok,
            perform_type=pf.get("perform_type"),
            base_missing=bool(pf.get("base_missing")),
            bars=len(bars),
        )

        # Soft continue when locomotion base is healthy even if bar residual soft-fails.
        hard_fail = (not bool(rec.get("ok"))) and (
            (not captcha_ok)
            or bool(pf.get("is_matter"))
            or bool(pf.get("base_missing"))
        )
        if hard_fail:
            # One more pass before hard stop.
            self._emit(
                "recover",
                "恢复未完全，再试一轮关UI/停perform/清读条",
                ok=False,
                reason="recover_retry",
            )
            rec2 = recover_entry_interact_state(
                session,
                cfg,
                hwnd=self.hwnd,
                stop_event=self._stop,
                log=self.log,
                status=self._status,
                close_captcha=True,
                clear_bar=want_bar,
                stop_perform=True,
                use_bridge_cancel=bool(cfg.use_bridge),
            )
            pf = rec2.get("perform_after") or pf
            captcha_ok = bool(rec2.get("captcha_closed"))
            hard_fail = (not bool(rec2.get("ok"))) and (
                (not captcha_ok)
                or bool(pf.get("is_matter"))
                or bool(pf.get("base_missing"))
            )
            self._emit(
                "recover",
                (
                    f"二轮恢复 ok={rec2.get('ok')} type={pf.get('perform_type')} "
                    f"captcha_ok={captcha_ok} base_missing={pf.get('base_missing')} "
                    f"err={rec2.get('error')!r}"
                ),
                ok=bool(rec2.get("ok")),
                reason="recover_retry_result",
            )
            if hard_fail and bool(pf.get("base_missing")):
                # Missing type=2 means character may be permanently stuck until relog.
                err = str(rec2.get("error") or rec.get("error") or "recover failed")
                self._emit(
                    "stopped",
                    f"入口恢复失败（移动基座丢失），已停止自动：{err}",
                    ok=False,
                    reason="recover_base_missing",
                )
                self._stop.set()
                self.running = False
                return False
            # captcha still open or matter sticky: do not hard-stop; path away and reopen.
            if hard_fail:
                self._emit(
                    "recover",
                    (
                        "恢复未完全但继续寻路重开入口 "
                        f"(captcha_ok={captcha_ok} matter={pf.get('is_matter')} "
                        f"base_missing={pf.get('base_missing')})"
                    ),
                    ok=False,
                    reason="recover_soft_continue",
                )

        if not _sleep_interruptible(0.2, self._stop):
            return False

        # Next round must re-interact 暗道 (never reuse stale Win_Question3D).
        self._force_reopen_entry = True
        # Fixed stand only — do not use entity/host xyz (false d=0 arrival).
        dest = DEFAULT_ENTRY_ANCHOR
        return self._path_to_entry_until_arrived(
            session,
            phase="recover",
            reason=reason,
            host_pos=host_pos,
            anchor=dest,
        )

    def _finish_enter_success(
        self,
        session: GameAttachSession,
        *,
        scene_id: int | None,
        label: str,
        open_res: SuperLootStepResult | None,
    ) -> None:
        self._reset_entry_reopen()
        self._captcha_api_fail_streak = 0
        self._answer_fail_streak = 0
        self._ok_count += 1
        ok_day = self._bump_daily_ok()
        limit = int(getattr(self.cfg, "daily_success_limit", 0) or 0)
        self._emit(
            "enter_ok",
            (
                f"进入成功 scene={scene_id} {label} "
                f"本轮ok={self._ok_count} 今日ok={ok_day}"
                + (f"/{limit}" if limit > 0 else "")
                + f" profile={getattr(self.cfg, 'profile_id', '')}"
            ),
            ok=True,
            scene_id=scene_id,
            ok_count=int(self._ok_count),
            daily_ok=int(ok_day),
            daily_limit=int(limit),
            profile_id=str(getattr(self.cfg, "profile_id", "") or ""),
            run_id=str(getattr(self, "_run_id", "") or ""),
        )
        self._audit(
            "enter_ok",
            ok=True,
            message=f"scene={scene_id} {label}",
            scene_id=scene_id,
            daily_ok=int(ok_day),
            daily_limit=int(limit),
        )
        self._emit("wait_return", "等待退出妖楼回到福州")
        if not wait_return_fuzhou(
            session,
            self.cfg,
            stop_event=self._stop,
            log=self.log,
            status=self._status,
        ):
            return
        self._mark_map_settle_needed("leave dungeon -> fuzhou")
        # Back in Fuzhou: path to fixed entry stand (no random wander).
        _sid2, pos2, _ = read_scene_state(session, log=self.log)
        anchor = self._entry_anchor(open_res, pos2) or DEFAULT_ENTRY_ANCHOR
        self._path_to_entry_until_arrived(
            session,
            phase="wait_return",
            reason="已回福州，寻路到妖楼入口",
            host_pos=pos2,
            anchor=anchor,
        )
        # Daily limit after a completed cycle (entered + returned).
        if self._maybe_stop_daily_limit():
            return
        # Long random rest before next open (main anti-ban lever).
        rest = float(self.cfg.success_rest_sleep_s())
        if rest > 0:
            self._arm_open_rest(rest, "通关后降频休息", label="通关休息")
            self._status(
                f"通关休息 {rest/60.0:.1f} 分钟后继续（风控降频）"
            )

    def _one_round(self, session: GameAttachSession) -> None:
        cfg = self.cfg
        blocked, why = self._risk_start_blocked()
        if blocked:
            self._stop_for_risk(why, reason="risk_gate")
            return
        # Wait success-rest / answer-fail rest before counting a new open round.
        if not self._wait_entry_cd():
            return
        # 0. Fuzhou gate
        sid, pos, label = read_scene_state(session, log=self.log)
        if is_yaolu_scene(sid, label):
            self._emit("wait_return", f"当前在妖楼 {label}，等待回福州", ok=True)
            if not wait_return_fuzhou(
                session,
                cfg,
                stop_event=self._stop,
                log=self.log,
                status=self._status,
            ):
                self._emit("stop", "等待回福州中断", ok=False)
                return
            self._mark_map_settle_needed("start-in-yaolu wait_return")
            sid, pos, label = read_scene_state(session, fresh=True, log=self.log)

        if not is_fuzhou_scene(sid, label):
            # Cross-map HostMove(mode=68) to fixed 暗道 stand — do not sit and wait.
            self._emit(
                "gate",
                f"不在福州城 scene={sid} {label}，跨图寻路到妖楼入口 "
                f"{DEFAULT_ENTRY_ANCHOR[0]:.1f},{DEFAULT_ENTRY_ANCHOR[1]:.1f},"
                f"{DEFAULT_ENTRY_ANCHOR[2]:.1f}",
                ok=True,
                scene_id=sid,
            )
            if not self._path_to_entry_until_arrived(
                session,
                phase="path_open",
                reason=f"跨图回福州妖楼入口 scene={sid}",
                host_pos=pos,
                anchor=DEFAULT_ENTRY_ANCHOR,
            ):
                return
            self._mark_map_settle_needed(f"cross-map scene was {sid}")
            sid, pos, label = read_scene_state(session, fresh=True, log=self.log)
            if not is_fuzhou_scene(sid, label):
                self._emit(
                    "gate",
                    f"跨图后仍不在福州 scene={sid} {label}，下一轮重试",
                    ok=False,
                    scene_id=sid,
                )
                _sleep_interruptible(2.0, self._stop)
                return

        self._emit("gate", f"福州城就绪 scene={sid} {label}", scene_id=sid)

        # Hourly entry vanish: :58:00 ~ next :00:15 — do not path/open.
        bo = entry_blackout_remaining_s(cfg)
        if bo > 0.05:
            self._emit(
                "entry_cd",
                f"整点避险窗口，等待 {bo:.0f}s（:{int(getattr(cfg, "entry_blackout_start_min", 55)):02d}–次时:{int(getattr(cfg, "entry_blackout_end_min", 5)):02d}）",
                ok=False,
                blackout_s=bo,
            )
            _sleep_interruptible(min(bo, 90.0), self._stop)
            return

        # A blackout/preflight wait is not an entry attempt.  Count the round
        # only after the existing hourly risk gate permits actual interaction.
        self._rounds += 1
        daily = self._daily_ok_persisted()
        limit = int(getattr(cfg, "daily_success_limit", 0) or 0)
        self._emit(
            "round",
            (
                f"第 {self._rounds} 轮 开始"
                + (f"（今日成功 {daily}/{limit}）" if limit > 0 else f"（今日成功 {daily}）")
            ),
            daily_ok=daily,
            daily_limit=limit,
        )

        if self._entry_reopen_pending and not self._wait_entry_reopen_heartbeat():
            return

        open_res: SuperLootStepResult | None = None

        # Memory gate: team+leader / already-open captcha (not blind open).
        gate = check_interact_gate(
            session,
            require_team_leader=bool(cfg.require_team_leader),
            require_solo=bool(cfg.require_solo) if cfg.require_solo else None,
            names=cfg.captcha_dlg_names,
            log=self.log,
        )
        # Always surface team/leader memory state in 妖楼 log for the user.
        team_txt = "已组队" if gate.in_team else "未组队"
        if gate.in_team:
            leader_txt = "是队长" if gate.is_leader else "不是队长"
            team_detail = (
                f"{team_txt} · {leader_txt} · "
                f"team=0x{gate.team_ptr:X} "
                f"自己id=0x{gate.host_id_lo:X} 队长id=0x{gate.leader_id_lo:X}"
            )
        else:
            team_detail = team_txt
        self._emit(
            "gate",
            f"组队状态: {team_detail}",
            ok=gate.ok or not cfg.require_team_leader,
            reason="team_status",
            in_team=gate.in_team,
            is_leader=gate.is_leader,
            team_ptr=gate.team_ptr,
            host_id_lo=gate.host_id_lo,
            leader_id_lo=gate.leader_id_lo,
        )
        if not gate.ok and self._entry_reopen_pending:
            self._fail_count += 1
            self._emit(
                "entry_retry",
                f"心跳唤醒后队伍门控仍未满足: {gate.reason}",
                ok=False,
                reason=gate.reason,
            )
            self._schedule_entry_reopen(f"team_gate:{gate.reason}")
            return
        if not gate.ok:
            if gate.reason == "not_in_team":
                self._emit(
                    "gate",
                    "门控拦截: 未组队（入口必须组队且自己是队长）",
                    ok=False,
                    reason=gate.reason,
                )
                if not _sleep_interruptible(3.0, self._stop):
                    return
                return
            if gate.reason == "not_leader":
                self._emit(
                    "gate",
                    (
                        "门控拦截: 已组队但自己不是队长，无法打开入口 "
                        f"(自己id=0x{gate.host_id_lo:X} 队长id=0x{gate.leader_id_lo:X})"
                    ),
                    ok=False,
                    reason=gate.reason,
                    team_ptr=gate.team_ptr,
                    is_leader=gate.is_leader,
                )
                if not _sleep_interruptible(3.0, self._stop):
                    return
                return
            if gate.reason == "in_team":
                self._emit(
                    "gate",
                    f"门控拦截: 当前配置要求单人，但已组队(team=0x{gate.team_ptr:X})",
                    ok=False,
                    reason=gate.reason,
                    team_ptr=gate.team_ptr,
                )
                if not _sleep_interruptible(3.0, self._stop):
                    return
                return
            self._emit(
                "gate",
                f"交互门控失败: {gate.reason} {gate.error or ''}",
                ok=False,
                reason=gate.reason,
            )
            return
        if cfg.require_team_leader:
            self._emit(
                "gate",
                "门控通过: 已组队且是队长，允许打开入口",
                ok=True,
                reason="team_leader_ok",
                team_ptr=gate.team_ptr,
                is_leader=True,
            )
        if self._entry_reopen_pending and gate.captcha_open:
            self._emit(
                "entry_retry",
                "服务器重新要求验证码，退出免答题重开模式并恢复正常答题流程",
                ok=True,
                reason="captcha_reopened",
            )
            self._reset_entry_reopen()
        elif self._entry_reopen_pending:
            entry_block_baseline = snapshot_entry_open_block_hits(session)
            self._emit(
                "entry_retry",
                f"心跳第 {self._entry_reopen_attempt} 轮重新点击 {cfg.entry_name}",
                ok=True,
                attempt=self._entry_reopen_attempt,
                total_wait_s=self._entry_reopen_total_s,
            )
            open_res = open_entry(
                session,
                cfg,
                hwnd=self.hwnd,
                stop_event=self._stop,
                log=self.log,
                status=self._status,
                host_pos=pos,
                prepath=True,
            )
            if self._stop.is_set():
                return
            if not open_res.ok:
                direct_sid, _direct_pos, direct_label = read_scene_state(
                    session, log=lambda _m: None
                )
                if direct_sid is not None and (
                    is_yaolu_scene(direct_sid, direct_label)
                    or (
                        sid is not None
                        and int(direct_sid) != int(sid)
                        and not is_fuzhou_scene(direct_sid, direct_label)
                    )
                ):
                    self._finish_enter_success(
                        session,
                        scene_id=direct_sid,
                        label=direct_label,
                        open_res=open_res,
                    )
                    return
                self._fail_count += 1
                self._schedule_entry_reopen(
                    open_res.error or open_res.action or "入口交互失败"
                )
                return
            kind, retry_sid, retry_label, retry_fb = wait_entry_reopen_result(
                session,
                cfg,
                start_scene=sid,
                entry_block_baseline=entry_block_baseline,
                stop_event=self._stop,
                log=self.log,
                status=self._status,
            )
            if kind == "entered":
                self._finish_enter_success(
                    session,
                    scene_id=retry_sid,
                    label=retry_label,
                    open_res=open_res,
                )
                return
            if kind == "captcha":
                self._emit(
                    "entry_retry",
                    "免答题资格已失效，检测到验证码；下一轮恢复正常答题",
                    ok=False,
                    reason="captcha_reopened",
                )
                self._reset_entry_reopen()
                return
            if kind == "stopped":
                return
            self._fail_count += 1
            reason = (
                retry_fb.text
                if retry_fb is not None and retry_fb.text
                else f"reopen_{kind} scene={retry_sid} {retry_label}"
            )
            self._schedule_entry_reopen(reason)
            return
        # After identify fail / cancel: never re-use leftover Win_Question3D.
        # That path only re-exports the same crop → API fail loop, no MatterInteract.
        force_reopen = bool(self._force_reopen_entry)
        if force_reopen:
            if gate.captcha_open:
                self._emit(
                    "path_open",
                    f"强制重开入口：关闭残留验证码 {gate.captcha_name}",
                    ok=True,
                    reason="force_reopen_stale_captcha",
                )
                closed_ok = False
                for _close_i in range(3):
                    closed = cancel_active_entry_session(
                        session,
                        cfg,
                        hwnd=self.hwnd,
                        stop_event=self._stop,
                        log=self.log,
                        force=True,
                        close_captcha=True,
                    )
                    if not _sleep_interruptible(0.40, self._stop):
                        return
                    gate = check_interact_gate(
                        session,
                        require_team_leader=bool(cfg.require_team_leader),
                        names=cfg.captcha_dlg_names,
                        log=self.log,
                    )
                    if not gate.captcha_open or bool(closed.get("captcha_closed")):
                        # Re-check memory; closed flag may lag one frame.
                        still = is_captcha_dialog_open(
                            session,
                            names=cfg.captcha_dlg_names,
                            log=lambda _m: None,
                        )
                        if not still.shown:
                            closed_ok = True
                            gate = check_interact_gate(
                                session,
                                require_team_leader=bool(cfg.require_team_leader),
                                names=cfg.captcha_dlg_names,
                                log=self.log,
                            )
                            break
                if gate.captcha_open and not closed_ok:
                    self._emit(
                        "path_open",
                        f"残留验证码未能关闭 {gate.captcha_name}，本轮跳过打开入口",
                        ok=False,
                        reason="stale_captcha_stuck",
                    )
                    self._force_reopen_entry = True
                    self._fail_count += 1
                    # Walk off and retry next round — never open while dialog up.
                    self._path_to_entry_until_arrived(
                        session,
                        phase="recover",
                        reason=f"残留验证码未关闭 {gate.captcha_name}",
                        host_pos=pos,
                        anchor=DEFAULT_ENTRY_ANCHOR,
                    )
                    return
            else:
                self._emit(
                    "path_open",
                    "强制重开入口：重新交互暗道",
                    ok=True,
                    reason="force_reopen_entry",
                )
            self._force_reopen_entry = False

        if gate.captcha_open and not force_reopen:
            # Stale dialog from a previous failed identify must not be reused.
            # Close + recover so next MatterInteract gets a fresh captcha.
            self._emit(
                "path_open",
                f"检测到残留验证码 {gate.captcha_name}，先关闭再重新打开入口",
                ok=False,
                reason="stale_captcha_reuse_blocked",
            )
            self._fail_count += 1
            self._recover_after_failure(
                session,
                f"残留验证码需关闭：{gate.captcha_name}",
                host_pos=pos,
                anchor=DEFAULT_ENTRY_ANCHOR,
            )
            return
        else:
            # Entry object CD from previous wrong answer / open fail.
            if not self._wait_entry_cd():
                return
            entry_block_baseline = snapshot_entry_open_block_hits(session)
            self.log(f"yaolu entry block baseline={entry_block_baseline}")

            # 1-2. path to entry, open immediately; short in-place re-open if no dlg
            self._emit("path_open", f"寻路/打开 {cfg.entry_name}")
            if not self._consume_map_settle_if_needed(phase="path_open"):
                return
            open_res = None
            dlg = DialogCapture(ok=False, error="not_started")
            open_tries = max(1, int(cfg.open_dialog_retries) + 1)
            for open_i in range(open_tries):
                if self._stop.is_set():
                    return
                # First try: full prepath. Retries: already at stand, open only.
                open_res = open_entry(
                    session,
                    cfg,
                    hwnd=self.hwnd,
                    stop_event=self._stop,
                    log=self.log,
                    status=self._status,
                    host_pos=pos,
                    prepath=(open_i == 0),
                )
                if self._stop.is_set():
                    return
                if not open_res.ok:
                    if open_i + 1 < open_tries and open_res.action not in ("stop",):
                        self._emit(
                            "path_open",
                            f"打开失败，就地重试 {open_i + 1}/{open_tries - 1}："
                            f"{open_res.message or open_res.error or open_res.action}",
                            ok=False,
                            action=open_res.action,
                        )
                        cancel_active_entry_session(
                            session,
                            cfg,
                            hwnd=self.hwnd,
                            stop_event=self._stop,
                            log=self.log,
                            force=True,
                        )
                        if not _sleep_interruptible(0.6, self._stop):
                            return
                        continue
                    try:
                        from app.core import diag_log

                        diag_log.error(
                            "open_entry failed "
                            f"pid={self.pid} action={open_res.action!r} "
                            f"error={open_res.error!r} target={open_res.target!r} "
                            f"interact={open_res.interact!r}",
                            tag="YAOLU",
                        )
                    except Exception:
                        pass
                    self._emit(
                        "path_open",
                        open_res.message or "打开入口失败",
                        ok=False,
                        action=open_res.action,
                    )
                    if self._try_claim_already_in_yaolu(
                        session, open_res=open_res, reason="open_fail"
                    ):
                        return
                    self._recover_after_failure(
                        session,
                        f"入口交互失败：{open_res.error or open_res.action or '未知错误'}",
                        host_pos=pos,
                        anchor=self._entry_anchor(open_res, pos),
                    )
                    # Recover path-back clears _next_open_ts; re-arm real object CD.
                    self._arm_and_wait_entry_cd(open_res.action or "open_fail")
                    return

                # Successful MatterInteract starts entry CD even if dialog later misses.
                open_cd = self._arm_entry_cd("opened_interact")
                self._emit(
                    "path_open",
                    (open_res.message or "入口已交互")
                    + f" · 已交互，正在内存确认验证码弹窗（入口CD {open_cd:.1f}s）",
                    ok=True,
                )
                # Shorter wait on retries — avoid “等下一轮”感。
                wait_s = float(cfg.open_dialog_wait_s)
                if open_i > 0:
                    wait_s = min(wait_s, 10.0)
                dlg = wait_for_captcha_dialog(
                    self.hwnd,
                    cfg,
                    session=session,
                    timeout_s=wait_s,
                    settle_s=cfg.post_open_settle_s if open_i == 0 else 0.4,
                    stop_event=self._stop,
                    log=self.log,
                    status=self._status,
                    entry_block_baseline=entry_block_baseline,
                )
                if self._stop.is_set():
                    return
                if dlg.ok:
                    break
                if self._handle_entry_block_error(dlg.error):
                    return
                why = dlg.error or "dialog not open"
                if open_i + 1 < open_tries:
                    self._emit(
                        "path_open",
                        f"未确认验证码弹窗，就地再开入口 {open_i + 1}/{open_tries - 1}：{why}",
                        ok=False,
                        reason="no_captcha_dialog_retry",
                    )
                    cancel_active_entry_session(
                        session,
                        cfg,
                        hwnd=self.hwnd,
                        stop_event=self._stop,
                        log=self.log,
                        force=True,
                    )
                    if not _sleep_interruptible(0.5, self._stop):
                        return
                    continue
                self._fail_count += 1
                gate2 = check_interact_gate(
                    session,
                    require_team_leader=False,
                    names=cfg.captcha_dlg_names,
                    log=self.log,
                )
                if not gate2.in_team:
                    why = f"{why}; not_in_team"
                elif not gate2.is_leader:
                    why = f"{why}; not_leader"
                self._emit(
                    "path_open",
                    f"内存未确认验证码弹窗（交互未成功/CD/服务器拒绝）: {why}；"
                    "清理读条后寻路重试",
                    ok=False,
                    reason="no_captcha_dialog",
                    in_team=gate2.in_team,
                    is_leader=gate2.is_leader,
                )
                self._recover_after_failure(
                    session,
                    f"入口未弹验证码：{why}",
                    host_pos=pos,
                    anchor=self._entry_anchor(open_res, pos),
                )
                self._arm_and_wait_entry_cd("no_captcha_dialog")
                return
            if not dlg.ok:
                return

        if not dlg.ok:
            # Defensive: open path must have set dlg; recover instead of hang.
            self._fail_count += 1
            self._emit("path_open", "验证码弹窗状态异常，清理后重试", ok=False)
            self._recover_after_failure(
                session,
                "验证码弹窗状态异常",
                host_pos=pos,
                anchor=DEFAULT_ENTRY_ANCHOR,
            )
            return

        self._emit(
            "path_open",
            f"内存确认弹窗已开，导出 {dlg.box.width}x{dlg.box.height}"
            if dlg.box
            else "内存确认弹窗已开",
            ok=True,
        )
        # Baseline entry-condition text immediately before captcha submission.
        # A toast from an earlier failed click must not become this answer's
        # result and trigger the no-captcha heartbeat path.
        answer_entry_block_baseline = snapshot_entry_open_block_hits(session)

        # 3-4. captcha identify + click (dialog already confirmed in memory)
        self._emit(
            "captcha",
            f"识别验证码并点选确认（超时 {cfg.captcha_api_timeout_s:.0f}s）",
        )
        if bool(cfg.debug_save_crops):
            try:
                dbg = _debug_dir()
                self.log(f"yaolu captcha crop dir={dbg} (debug on)")
            except Exception as e:
                self.log(f"yaolu captcha crop dir err: {e}")
        else:
            self.log("yaolu captcha crops: memory only (debug off)")
        ident = wait_and_solve_captcha(
            self.hwnd,
            cfg,
            session=session,
            dialog=dlg,
            stop_event=self._stop,
            log=self.log,
            status=self._status,
        )
        if self._stop.is_set():
            return
        if not ident.ok:
            self._fail_count += 1
            err_raw = str(ident.error or "")
            key_rejected = bool(
                int(getattr(ident, "http_status", 0) or 0) == 401
                or "答题专用 Key 不存在或已经失效" in err_raw
            )
            if key_rejected:
                from app.core.captcha_client import api_key_fingerprint

                key_hint = api_key_fingerprint(cfg.captcha_api_key)
                message = f"答题专用 Key 不存在或已经失效（{key_hint}）"
                self._last_stop_reason = message
                self._emit(
                    "stopped",
                    message,
                    ok=False,
                    reason="captcha_api_key_rejected",
                    http_status=int(getattr(ident, "http_status", 0) or 401),
                    key_hint=key_hint,
                )
                self._audit(
                    "captcha_api_key_rejected",
                    ok=False,
                    message=message,
                    http_status=int(getattr(ident, "http_status", 0) or 401),
                    key_hint=key_hint,
                )
                self._stop.set()
                self.running = False
                return
            low_conf = ("置信度过低" in err_raw) or ("已拒绝提交" in err_raw)
            if low_conf:
                self._audit(
                    "captcha_reject_low_conf",
                    ok=False,
                    message=err_raw,
                    confidence=getattr(ident, "confidence", None),
                    animal=getattr(ident, "animal", None),
                    min_confidence=float(getattr(cfg, "min_confidence", 0.25) or 0.25),
                )
            submit_failed = "确认按钮未生效" in err_raw
            if submit_failed:
                # Local UI delivery failure is not an identify-service failure.
                # Do not poison the API failure streak or report this sample as
                # an incorrect answer because no submission was acknowledged.
                self._audit(
                    "captcha_submit_not_acknowledged",
                    ok=False,
                    message=err_raw,
                    identify_id=getattr(ident, "identify_id", None),
                    positions=getattr(ident, "positions", None),
                )
                self._emit(
                    "captcha",
                    f"确认未生效，未提交：{err_raw}",
                    ok=False,
                    reason="captcha_submit_not_acknowledged",
                )
                self._status(f"确认未生效，正在恢复：{err_raw}")
                self._recover_after_failure(
                    session,
                    err_raw,
                    host_pos=pos,
                    anchor=DEFAULT_ENTRY_ANCHOR,
                    dismiss_stuck_captcha=False,
                    clear_bar=True,
                )
                self._arm_and_wait_entry_cd("captcha_submit_fail")
                return
            self._captcha_api_fail_streak = int(self._captcha_api_fail_streak) + 1
            err_text = describe_captcha_error(ident.error, cfg)
            streak = int(self._captcha_api_fail_streak)
            max_api = max(
                1,
                int(getattr(cfg, "captcha_api_fail_max_consecutive", 5) or 5),
            )
            cool = max(
                0.0,
                float(getattr(cfg, "captcha_api_fail_cooldown_s", 45.0) or 0.0),
            )
            self._emit(
                "captcha",
                f"识别失败（连续 {streak}/{max_api}）：{err_text}",
                ok=False,
                streak=streak,
                max_consecutive=max_api,
            )
            self._status(f"识别失败（连续 {streak}/{max_api}）：{err_text}")
            # Identify fail: close UI via functions only (no random wrong submit /
            # captcha feedback pollution), then clear 0% interact bar.
            self._recover_after_failure(
                session,
                err_text,
                host_pos=pos,
                anchor=DEFAULT_ENTRY_ANCHOR,
                dismiss_stuck_captcha=False,
                clear_bar=True,
            )
            if streak >= max_api and cool > 0:
                self._status(
                    f"识别服务连续失败 {streak} 次，冷却 {cool:.0f}s 后再试"
                )
                self._emit(
                    "captcha",
                    f"识别服务连续失败 {streak} 次，冷却 {cool:.0f}s 后再试",
                    ok=False,
                    reason="captcha_api_cooldown",
                    streak=streak,
                    cooldown_s=cool,
                )
                self._next_open_ts = time.monotonic() + cool
                if not self._wait_entry_cd():
                    return
                # After a full cool-down, allow a fresh streak window.
                self._captcha_api_fail_streak = 0
            else:
                self._arm_and_wait_entry_cd("captcha_fail")
            return

        self._captcha_api_fail_streak = 0
        self._emit(
            "captcha",
            f"已提交 动物={ident.animal} pos={ident.positions} conf={ident.confidence}",
            ok=True,
            positions=ident.positions,
            identify_id=ident.identify_id,
        )

        # Normal path keeps answer_baseline None (UI-only feedback after submit).
        answer_text_baseline = getattr(ident, "answer_baseline", None)

        # 5. enter result + chat system message (答案错误/正确/神罚)
        self._emit("enter_wait", f"等待系统消息/进图（{cfg.enter_timeout_s:.0f}s）")
        feedback_sent = {"done": False}
        pending_stage1_fail: dict[str, CaptchaAnswerFeedback | None] = {
            "fb": None
        }

        def _dispatch_feedback(
            correct: bool, *, asynchronous: bool, reason: str
        ) -> None:
            """Send one final verdict for this identify request."""
            if feedback_sent["done"]:
                return
            iid = str(getattr(ident, "identify_id", "") or "").strip()
            if not iid:
                return
            feedback_sent["done"] = True
            self.log(
                f"yaolu feedback dispatch correct={bool(correct)} reason={reason}"
            )

            def _worker() -> None:
                try:
                    send_feedback(
                        iid,
                        bool(correct),
                        api_key=cfg.captcha_api_key,
                        login_token=cfg.login_token,
                        base_url=cfg.captcha_base_url,
                        log=self.log,
                        timeout_s=8.0,
                    )
                except Exception as e:
                    self.log(f"yaolu feedback async feedback err: {e}")

            if asynchronous:
                threading.Thread(
                    target=_worker, name="yaolu-feedback", daemon=True
                ).start()
            else:
                _worker()

        def _on_stage1_feedback(stage1_fb: CaptchaAnswerFeedback) -> None:
            """
            First-order answer result: async-report proven success, but defer a
            failure until the scene grace window has produced a final verdict.

            Second-order 神罚 is handled later for hard-stop and must not
            flip a proven-correct identify to wrong.

            @author by ak
            """
            if feedback_sent["done"]:
                return
            if bool(getattr(cfg, "debug_random_answer", False)):
                self.log(
                    "yaolu stage1 feedback skipped: debug_random_answer"
                )
                feedback_sent["done"] = True
                return
            iid = str(getattr(ident, "identify_id", "") or "").strip()
            if not iid:
                return
            kind = str(getattr(stage1_fb, "kind", "") or "")
            text = str(getattr(stage1_fb, "text", "") or "")
            err = str(getattr(stage1_fb, "error", "") or "")
            blob = f"{text} {err}"
            # Hard 神罚 after/with ok still means identify was correct.
            if kind == "ok" or any(k in blob for k in ("答案正确", CAPTCHA_OK_TEXT)):
                correct = True
            elif kind == "fail" or "答案错误" in blob or "重新来过" in blob:
                correct = False
            elif kind == "block" and any(k in blob for k in ("神罚", "捕羽")):
                # No explicit answer line: still do not mark identify wrong.
                correct = True
            else:
                return
            # A fail can be a stale/early first-order line: wait_enter_result
            # may still prove entry via the scene grace window.  Keep it in a
            # tiny pending buffer until the final scene verdict is available.
            if not correct:
                pending_stage1_fail["fb"] = stage1_fb
                self.log(
                    f"yaolu stage1 feedback deferred kind={kind} text={text!r}"
                )
                return
            self.log(
                f"yaolu stage1 async feedback kind={kind} correct={correct} "
                f"text={text!r}"
            )
            _dispatch_feedback(correct, asynchronous=True, reason="stage1")

        ok_enter, new_sid, new_label, ans_fb = wait_enter_result(
            session,
            cfg,
            start_scene=sid,
            stop_event=self._stop,
            log=self.log,
            status=self._status,
            entry_block_baseline=answer_entry_block_baseline,
            answer_baseline=answer_text_baseline,
            on_stage1=_on_stage1_feedback,
        )
        if self._stop.is_set():
            return

        if ans_fb is not None and ans_fb.kind in ("ok", "fail", "block"):
            self.log(
                f"yaolu system msg kind={ans_fb.kind} via={ans_fb.method} "
                f"text={ans_fb.text!r}"
            )

        entry_block_fb = is_entry_open_block_feedback(ans_fb)
        fresh_entry_block_fb = is_fresh_entry_open_block_feedback(ans_fb)
        shenfa_like = is_shenfa_block_feedback(ans_fb) or (
            ans_fb is not None
            and any(
                k in f"{ans_fb.text or ''} {ans_fb.error or ''}"
                for k in ("神罚", "捕羽")
            )
        )
        # 神罚/捕羽 after correct answer must not mark identify as wrong.
        answer_likely_ok = bool(
            ok_enter
            or (ans_fb is not None and ans_fb.kind == "ok")
            or is_explicit_captcha_ok_feedback(ans_fb)
            or fresh_entry_block_fb
            or shenfa_like
        )

        # Resolve a deferred stage-1 failure only after the scene/second-order
        # result.  This prevents a stale "答案错误" line from becoming a
        # negative sample when the transfer actually reached scene 1529.
        pending_fail = pending_stage1_fail.get("fb")
        if pending_fail is not None and not feedback_sent.get("done"):
            final_correct = resolve_deferred_stage1_feedback(
                answer_likely_ok=answer_likely_ok,
                ans_fb=ans_fb,
            )
            if final_correct is not None:
                _dispatch_feedback(
                    final_correct,
                    asynchronous=False,
                    reason=(
                        "stage1_fail_final_ok"
                        if final_correct
                        else "stage1_fail_final_fail"
                    ),
                )
            else:
                self.log(
                    "yaolu stage1 feedback unresolved after enter wait; "
                    "skip negative report"
                )
        if bool(getattr(cfg, "debug_random_answer", False)):
            self.log(
                "yaolu feedback skipped: debug_random_answer "
                f"(local random pos={getattr(ident, 'positions', None)})"
            )
        elif feedback_sent.get("done"):
            self.log(
                "yaolu feedback already async-sent on stage1; "
                f"skip trailing report (likely_ok={answer_likely_ok})"
            )
        elif ident.identify_id and (not entry_block_fb or fresh_entry_block_fb or shenfa_like):
            try:
                _dispatch_feedback(
                    answer_likely_ok,
                    asynchronous=False,
                    reason="trailing",
                )
            except Exception as e:
                self.log(f"yaolu feedback err: {e}")
        elif ident.identify_id and entry_block_fb:
            self.log(
                "yaolu feedback skipped: entry condition blocked transfer; "
                "captcha correctness is not disproved"
            )

        if not ok_enter:
            # Mid-transfer may finish after wait_enter returns — claim before fail.
            if self._try_claim_already_in_yaolu(
                session, open_res=open_res, reason="post_enter_wait"
            ):
                return
            self._fail_count += 1
            # Classify permanent stop (神罚/捕羽) vs soft refuse vs wrong answer.
            hard_blob = ""
            if ans_fb is not None:
                # text = current toast; error may hold prior 答案正确 after merge.
                hard_blob = f"{ans_fb.text or ''} {ans_fb.error or ''}"
            shenfa_fb = is_shenfa_block_feedback(ans_fb) or any(
                k in hard_blob for k in ("神罚", "捕羽")
            )
            answer_ok_seen = bool(
                is_explicit_captcha_ok_feedback(ans_fb)
                or (ans_fb is not None and ans_fb.kind == "ok")
                or (
                    ans_fb is not None
                    and any(
                        k in f"{ans_fb.text or ''} {ans_fb.error or ''}"
                        for k in ("答案正确", CAPTCHA_OK_TEXT)
                    )
                )
            )

            if shenfa_fb:
                # Permanent: 神罚 / 捕羽 — stop full-auto, never reopen.
                msg = (
                    (ans_fb.text if ans_fb and ans_fb.text else None)
                    or SHENFA_BLOCK_TEXT
                )
                if "神罚" not in msg and "捕羽" not in msg:
                    # Prefer the hard marker fragment from the blob.
                    for frag in (
                        "队伍成员处于神罚状态，不能进入副本",
                        "队伍成员处于捕羽状态，不能进入副本",
                    ):
                        if frag in hard_blob:
                            msg = frag
                            break
                self._emit(
                    "enter_fail",
                    (
                        f"答案正确但队伍被神罚/捕羽拦截：{msg}"
                        if answer_ok_seen
                        else f"队伍拦截（神罚/捕羽）：{msg}"
                    ),
                    ok=False,
                    reason="shenfa_after_ok" if answer_ok_seen else "shenfa",
                )
                self._on_shenfa_stop(f"神罚/捕羽拦截，已停止自动: {msg}")
                return

            if answer_ok_seen:
                # 答案正确 confirmed: any later non-hard toast / timeout is soft.
                # Soft refuse => heartbeat reopen (no captcha again).
                from app.core.game_sys_msg import probe_captcha_answer_text

                late = probe_captcha_answer_text(
                    session,
                    log=self.log,
                    entry_block_baseline=answer_entry_block_baseline,
                    answer_baseline=answer_text_baseline,
                    heavy=False,
                    trust_chat_hard=True,
                )
                if late is not None and is_shenfa_block_feedback(late):
                    bmsg = late.text or SHENFA_BLOCK_TEXT
                    self._emit(
                        "enter_fail",
                        f"答案正确但队伍被神罚/捕羽拦截：{bmsg}",
                        ok=False,
                        reason="shenfa_after_ok",
                    )
                    self._on_shenfa_stop(f"神罚/捕羽拦截，已停止自动: {bmsg}")
                    return
                soft_msg = (
                    (ans_fb.error if ans_fb and ans_fb.error else None)
                    or (ans_fb.text if ans_fb and ans_fb.text else None)
                    or (late.text if late is not None and late.text else None)
                    or CAPTCHA_OK_TEXT
                )
                self._emit(
                    "enter_fail",
                    f"答案正确但尚未进图（软拦截/传送中）：{soft_msg}；"
                    "清理残留UI后进入免答题心跳重开",
                    ok=False,
                    reason="answer_ok_transfer_blocked",
                )
                # Residual Win_Question3D after "答案正确" blocks reopen; clear it.
                try:
                    cancel_active_entry_session(
                        session,
                        cfg,
                        hwnd=self.hwnd,
                        stop_event=self._stop,
                        log=self.log,
                        force=True,
                        close_captcha=True,
                    )
                except Exception as e:
                    self.log(f"yaolu answer_ok pre-reopen clear err: {e}")
                self._schedule_entry_reopen(soft_msg)
                return

            if entry_block_fb:
                msg = (ans_fb.text if ans_fb else None) or ENTRY_OPEN_BLOCK_TEXT
                if fresh_entry_block_fb:
                    self._emit(
                        "enter_fail",
                        f"本轮答题后新出现入口条件提示，判定答案正确但未进图：{msg}；"
                        "清理残留UI后进入免答题心跳重开",
                        ok=False,
                        reason="answer_ok_entry_blocked",
                    )
                    try:
                        cancel_active_entry_session(
                            session,
                            cfg,
                            hwnd=self.hwnd,
                            stop_event=self._stop,
                            log=self.log,
                            force=True,
                            close_captcha=True,
                        )
                    except Exception as e:
                        self.log(f"yaolu entry_block pre-reopen clear err: {e}")
                    self._schedule_entry_reopen(msg)
                    return
                self._emit(
                    "enter_fail",
                    f"未取得答案正确证据，仅检测到入口条件提示：{msg}；"
                    "按本轮失败处理并取消卡住状态",
                    ok=False,
                    reason="entry_open_block",
                )
                self._reset_entry_reopen()
                self._recover_after_failure(
                    session,
                    f"答案结果未证实正确：{msg}",
                    host_pos=pos,
                    anchor=self._entry_anchor(open_res, pos),
                )
                self._arm_and_wait_entry_cd("entry_open_block")
                return

            if ans_fb is not None and ans_fb.kind == "block":
                # Non-shenfa block without proven ok: soft reopen only when
                # stop_on_shenfa is off; default keeps historical hard-stop.
                msg = ans_fb.text or ENTRY_OPEN_BLOCK_TEXT
                self._emit("enter_fail", msg, ok=False, reason="block")
                if bool(cfg.stop_on_shenfa):
                    self._emit("stopped", f"进图拦截，已停止自动: {msg}", ok=False)
                    self._stop.set()
                    self.running = False
                else:
                    self._schedule_entry_reopen(msg)
                return

            is_answer_fail = ans_fb is not None and ans_fb.kind == "fail"
            if is_answer_fail:
                self._answer_fail_streak = int(self._answer_fail_streak) + 1
                self._run_answer_fail = int(self._run_answer_fail) + 1
                self._emit(
                    "enter_fail",
                    (
                        f"{ans_fb.text or CAPTCHA_FAIL_TEXT}；"
                        f"连续答错 {self._answer_fail_streak} 次；"
                        "关UI+停perform/恢复基座+清0%读条后延长冷却"
                    ),
                    ok=False,
                    reason="answer_fail",
                    streak=int(self._answer_fail_streak),
                )
                self._audit(
                    "answer_fail",
                    ok=False,
                    message=str((ans_fb.text if ans_fb is not None else "") or CAPTCHA_FAIL_TEXT),
                    streak=int(self._answer_fail_streak),
                )
            else:
                self._emit(
                    "enter_fail",
                    (
                        f"未进入妖楼 scene={new_sid} {new_label}；"
                        "关UI+停perform/恢复基座+清0%读条后短暂冷却"
                    ),
                    ok=False,
                    reason="enter_timeout_or_unknown",
                )
            # 答案错误后：读条(0%) + matter perform 必须清；验证码用函数关，不随机错答。
            self._recover_after_failure(
                session,
                ans_fb.text if ans_fb is not None and ans_fb.text else "未进入妖楼",
                host_pos=pos,
                anchor=self._entry_anchor(open_res, pos),
                dismiss_stuck_captcha=False,
                clear_bar=True,
            )
            # Recover path-back clears _next_open_ts — re-arm real entry CD here.
            if is_answer_fail:
                # Object CD + extra fail rest (anti rapid re-open).
                self._arm_entry_cd("answer_fail")
                extra = float(self.cfg.answer_fail_rest_sleep_s())
                if extra > 0:
                    self._arm_open_rest(extra, "答错加长冷却", label="答错冷却")
                fail_lim = int(
                    getattr(cfg, "consecutive_answer_fail_limit", 0) or 0
                )
                if (
                    fail_lim > 0
                    and int(self._answer_fail_streak) >= fail_lim
                ):
                    if bool(getattr(cfg, "consecutive_answer_fail_stop", False)):
                        self._stop_for_risk(
                            (
                                f"连续答错 {self._answer_fail_streak} 次"
                                f"（上限 {fail_lim}），停止自动"
                            ),
                            reason="consecutive_answer_fail",
                        )
                        return
                    pause = max(
                        0.0,
                        float(
                            getattr(cfg, "consecutive_answer_fail_pause_s", 300.0)
                            or 0.0
                        ),
                    )
                    if pause > 0:
                        self._arm_open_rest(
                            pause,
                            f"连续答错{self._answer_fail_streak}次熔断",
                            label="答错熔断",
                        )
                        self._answer_fail_streak = 0
                if not self._wait_entry_cd():
                    return
            else:
                self._fail_sleep_random("enter_timeout_or_unknown")
            return

        self._finish_enter_success(
            session,
            scene_id=new_sid,
            label=new_label,
            open_res=open_res,
        )

    def _loop(self) -> None:
        try:
            credential_error = yaolu_start_credential_error(self.cfg)
            if credential_error is not None:
                reason, message = credential_error
                self._emit(
                    "gate",
                    message,
                    ok=False,
                    reason=reason,
                )
                self.running = False
                return
            if bool(getattr(self.cfg, "debug_random_answer", False)):
                self._emit(
                    "gate",
                    "调试模式：跳过识别/反馈接口，随机点选 2 格",
                    ok=True,
                    reason="debug_random_answer",
                )
            self._session = open_attach_session(self.pid, log=self.log)
            self._emit("attach", f"attach pid={self.pid} hwnd=0x{self.hwnd:X}")
            # 非马上需要：开跑前预热场景/坐标/背包，供寻路/开门复用
            try:
                from app.core.state_dispatch import warmup_session

                warmup_session(
                    self._session,
                    log=lambda m: self.log(f"yaolu warmup: {m}") if m else None,
                )
            except Exception as e:
                self.log(f"yaolu warmup skip: {e}")
            try:
                from app.core.client_build import fingerprint_client, match_client_profile

                client_path = str(self._session.exe_path or "").strip()
                fp = fingerprint_client(client_path)
                matched = match_client_profile(fp)
                self._emit(
                    "attach",
                    f"client sha256={fp.sha256} size={fp.size} "
                    f"image=0x{fp.image_size:X} profile="
                    f"{matched.build_id if matched else 'UNKNOWN'}",
                    ok=matched is not None,
                    reason="client_build_known" if matched else "unsupported_client_build",
                    sha256=fp.sha256,
                    size=fp.size,
                    image_size=fp.image_size,
                    build_id=matched.build_id if matched else "",
                )
                if matched is None:
                    self.running = False
                    return
            except Exception as e:
                self._emit(
                    "gate",
                    f"客户端版本校验失败: {e}",
                    ok=False,
                    reason="client_fingerprint_failed",
                )
                self.running = False
                return
            # Background/minimized path needs live bridge; never force reinject here.
            if bool(self.cfg.use_bridge):
                try:
                    from app.core.xajh_bridge import CMD_PING, ensure_bridge

                    br = ensure_bridge(
                        self.pid,
                        log=self.log,
                        inject_if_needed=True,
                        hwnd=self.hwnd or None,
                        force_reinject=False,
                    )
                    if br is None:
                        self._emit(
                            "gate",
                            "桥接版本不匹配或未就绪：请完全退出游戏，重开后按 Delete 注入",
                            ok=False,
                            reason="bridge_unavailable_or_stale",
                        )
                        try:
                            from app.core import diag_log

                            diag_log.error(
                                f"yaolu bridge unavailable/stale pid={self.pid}",
                                tag="YAOLU",
                            )
                        except Exception:
                            pass
                        self.running = False
                        return
                    else:
                        try:
                            p = br.call(
                                CMD_PING, hwnd=self.hwnd or None, timeout_ms=3000
                            )
                            self._emit(
                                "attach",
                                f"bridge ping ok={p.ok} note={p.note!r}",
                                ok=bool(p.ok),
                                bridge_build=p.ret,
                                protocol_version=p.protocol_version,
                                capabilities=p.capabilities,
                            )
                            if not p.ok:
                                self._emit(
                                    "gate",
                                    f"桥接 PING 失败: {p.error or p.note}",
                                    ok=False,
                                    reason="bridge_ping_failed",
                                )
                                self.running = False
                                return
                        finally:
                            try:
                                br.close()
                            except Exception:
                                pass
                except Exception as e:
                    self._emit("gate", f"bridge 检查失败: {e}", ok=False)
            while not self._stop.is_set():
                try:
                    from app.core.safe_dispatch import session_blocked

                    blocked, brsn = session_blocked(self.pid)
                except Exception:
                    blocked, brsn = False, ""
                if blocked:
                    self._emit(
                        "remote_blocked",
                        f"远程不可用已停止 pid={self.pid} ({brsn})",
                        ok=False,
                        reason=brsn,
                    )
                    break
                try:
                    self._one_round(self._session)
                except Exception as e:
                    self._emit("error", str(e), ok=False)
                    try:
                        from app.core.safe_dispatch import get_dispatch

                        get_dispatch().note_exception(int(self.pid), e)
                    except Exception:
                        pass
                    # hard_dead after note: stop instead of tight retry storm
                    try:
                        from app.core.safe_dispatch import session_blocked

                        b2, r2 = session_blocked(self.pid)
                        if b2:
                            self._emit(
                                "remote_blocked",
                                f"远程失败后停手 ({r2}): {e}",
                                ok=False,
                                reason=r2,
                            )
                            break
                    except Exception:
                        pass
                    if not _sleep_interruptible(2.0, self._stop):
                        break
                if not _sleep_interruptible(float(self.cfg.loop_idle_s), self._stop):
                    break
        finally:
            self.running = False
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:
                    pass
                self._session = None
            reason = str(getattr(self, "_last_stop_reason", "") or "").strip()
            if reason:
                final_msg = (
                    f"{reason}（ok={self._ok_count} fail={self._fail_count}）"
                )
            else:
                profile = str(getattr(self.cfg, "profile_id", "") or "")
                run_id = str(getattr(self, "_run_id", "") or "")
                final_msg = (
                    f"已停止 ok={self._ok_count} fail={self._fail_count} "
                    f"profile={profile} run_id={run_id}"
                )
            self._emit("stopped", final_msg)
            try:
                self._audit("stopped", ok=False, message=str(final_msg or ""))
            except Exception:
                pass
