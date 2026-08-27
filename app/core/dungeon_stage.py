# -*- coding: utf-8 -*-
"""副本阶段 UI 状态机。

阶段事实来源是 Win_InstCfgTemplate.Txt_Template* 文本；空读被视为 AUI
重绘噪声，连续稳定采样后才发布阶段事件。
"""
from __future__ import annotations

import re
from dataclasses import dataclass


_COLOR_RE = re.compile(r"\^[0-9A-Fa-f]{1,8}")
_SPACE_RE = re.compile(r"\s+")
_NUMBER_RE = re.compile(r"\d+")
_TRANSITION_RE = re.compile(
    r"(?:(\d+)\s*秒\s*)?(?:后\s*)?(?:进入|前往|到达).{0,8}"
    r"(?:下一|下个).{0,6}(?:版面|阶段|区域)"
)
_COUNTDOWN_RE = re.compile(r"倒计时\s*(\d+)[:：](\d{2})(?:[:：](\d{2}))?")


def normalize_stage_text(text: str) -> str:
    """清理颜色码、空白和明显的非中文/ASCII 读坏数据。"""
    value = _SPACE_RE.sub(" ", _COLOR_RE.sub("", str(text or ""))).strip()
    if not value:
        return ""
    allowed = sum(
        ch in "\n\r\t"
        or "\x20" <= ch <= "\x7e"
        or "\u3000" <= ch <= "\u303f"
        or "\u4e00" <= ch <= "\u9fff"
        or "\uff00" <= ch <= "\uffef"
        for ch in value
    )
    return value if allowed == len(value) else ""


def parse_transition_seconds(texts: list[str] | tuple[str, ...]) -> int | None:
    """解析“5秒进入下一个版面”类过渡文案。"""
    for text in texts:
        match = _TRANSITION_RE.search(normalize_stage_text(text))
        if match:
            return int(match.group(1)) if match.group(1) else None
    return None


def stage_signature(texts: list[str] | tuple[str, ...]) -> str:
    """阶段身份签名：保留目标语义，屏蔽击杀/进度数字。"""
    clean = [normalize_stage_text(text) for text in texts]
    return " | ".join(_NUMBER_RE.sub("#", text) for text in clean if text)


def parse_instance_countdown(text: str) -> int | None:
    """解析副本总倒计时，分钟允许大于 59。"""
    match = _COUNTDOWN_RE.search(normalize_stage_text(text))
    if not match:
        return None
    first, second, third = match.groups()
    if third is not None:
        return int(first) * 3600 + int(second) * 60 + int(third)
    return int(first) * 60 + int(second)


@dataclass(frozen=True)
class DungeonStageSnapshot:
    scene_id: int = 0
    time_text: str = ""
    targets: tuple[str, ...] = ()
    transition_seconds: int | None = None
    stage_complete: bool = False

    @property
    def countdown_seconds(self) -> int | None:
        return parse_instance_countdown(self.time_text)

    @property
    def instance_expired(self) -> bool:
        return self.countdown_seconds == 0

    @classmethod
    def from_texts(
        cls,
        texts: list[str] | tuple[str, ...],
        *,
        scene_id: int = 0,
        time_text: str = "",
    ) -> "DungeonStageSnapshot":
        targets = tuple(
            text for text in (normalize_stage_text(v) for v in texts) if text
        )
        transition = parse_transition_seconds(targets)
        complete = transition is not None or any(
            marker in " | ".join(targets)
            for marker in ("已完成", "任务完成", "阶段完成")
        )
        return cls(
            scene_id=int(scene_id),
            time_text=normalize_stage_text(time_text),
            targets=targets,
            transition_seconds=transition,
            stage_complete=complete,
        )

    @property
    def signature(self) -> str:
        return stage_signature(self.targets)


class DungeonStageTracker:
    """连续稳定采样后发布阶段事件。"""

    def __init__(self, *, stable_samples: int = 2):
        self.stable_samples = max(1, int(stable_samples))
        self.current: DungeonStageSnapshot | None = None
        self._candidate: tuple[object, ...] | None = None
        self._candidate_count = 0

    def update(self, snapshot: DungeonStageSnapshot) -> str | None:
        previous = self.current
        # A transient all-empty read during AUI rebuild is not a lifecycle event.
        if (
            previous is not None
            and previous.targets
            and not snapshot.targets
            and not snapshot.instance_expired
        ):
            return None
        key = (
            snapshot.scene_id,
            snapshot.targets,
            snapshot.transition_seconds,
            snapshot.stage_complete,
        )
        if key == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = key
            self._candidate_count = 1
        if self._candidate_count < self.stable_samples:
            return None
        self.current = snapshot
        if previous is None:
            return "snapshot"
        if snapshot.instance_expired and not previous.instance_expired:
            return "instance_expired"
        if snapshot.transition_seconds != previous.transition_seconds:
            return "transition_countdown"
        if snapshot.stage_complete and not previous.stage_complete:
            return "stage_completed"
        if snapshot.signature != previous.signature:
            return "stage_started" if not previous.signature else "stage_changed"
        if snapshot.targets != previous.targets:
            return "stage_progress"
        return None


@dataclass(frozen=True)
class DungeonTaskSequenceProgress:
    """Observed task-transition ordinal, not the client's ``idBoard``."""

    sequence_no: int = 0
    absolute_known: bool = False
    awaiting_next: bool = False


class DungeonTaskSequenceCounter:
    """Count stable task changes when a static BoardCond match is unavailable."""

    def __init__(self) -> None:
        self.progress = DungeonTaskSequenceProgress()
        self._signature = ""

    def update(
        self,
        event: str | None,
        snapshot: DungeonStageSnapshot,
        *,
        entered_while_watching: bool = False,
    ) -> str | None:
        if not event:
            return None
        current = self.progress
        if event == "instance_expired":
            self.progress = DungeonTaskSequenceProgress(
                sequence_no=current.sequence_no,
                absolute_known=current.absolute_known,
                awaiting_next=False,
            )
            return "instance_expired"

        signature = snapshot.signature
        if signature and current.sequence_no == 0 and not snapshot.stage_complete:
            self._signature = signature
            self.progress = DungeonTaskSequenceProgress(
                sequence_no=1,
                absolute_known=bool(entered_while_watching),
                awaiting_next=False,
            )
            return "task_sequence_started"

        if event in ("transition_countdown", "stage_completed") or (
            snapshot.stage_complete and current.sequence_no > 0
        ):
            self.progress = DungeonTaskSequenceProgress(
                sequence_no=current.sequence_no,
                absolute_known=current.absolute_known,
                awaiting_next=True,
            )
            return "task_completed"

        if (
            current.sequence_no > 0
            and signature
            and signature != self._signature
            and not snapshot.stage_complete
        ):
            self._signature = signature
            self.progress = DungeonTaskSequenceProgress(
                sequence_no=current.sequence_no + 1,
                absolute_known=current.absolute_known,
                awaiting_next=False,
            )
            return "task_changed"

        return None
