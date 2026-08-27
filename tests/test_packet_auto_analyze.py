# -*- coding: utf-8 -*-
"""Tests for packet auto-analyze pipeline. @author by ak"""
from __future__ import annotations

from pathlib import Path

from packet.auto_analyze import (
    analyze_log_text,
    analyze_paths,
    pair_send10_plain,
    parse_events,
    ui_rows,
)
from packet.crypto_knowledge import match_shell, knowledge_dict, core_points_markdown
from packet.x32dbg_recipes import write_recipes, recipe_shot_script


SAMPLE = """
init...
PLAIN 22080721000100000000AABB
PLAIN DEADBEEF001122334455
SEND10 AABBCCDDEEFF00112233
PLAIN 22080721000100000000CCDD
SEND10 11223344556677889900
PLAIN 221B1A0100ABCDEF
"""


def test_parse_and_pair_open_chest_shell():
    events = parse_events(SAMPLE.splitlines())
    kinds = [e.kind for e in events]
    assert kinds.count("PLAIN") == 4
    assert kinds.count("SEND10") == 2
    pairs = pair_send10_plain(events, lookback=20)
    assert len(pairs) == 2
    assert pairs[0].plain_hex10.startswith("22080721")
    assert pairs[0].shell == "open_chest_c2s10"
    assert pairs[1].same_plain_as_frozen


def test_analyze_log_text_findings():
    r = analyze_log_text(SAMPLE, source="mem")
    assert r.ok
    assert r.counts["SEND10"] == 2
    assert any("22080721000100000000" in f or "主明文壳" in f for f in r.findings)
    rows = ui_rows(r)
    assert any("C:" in x and "P:" in x for x in rows)


def test_match_shell_and_knowledge():
    sh = match_shell("22080721000100000000")
    assert sh is not None
    assert sh.name == "open_chest_c2s10"
    kd = knowledge_dict()
    assert "update" in kd["arcfour"]
    md = core_points_markdown()
    assert "00DAFAF0" in md.upper()


def test_recipes_write(tmp_path: Path):
    tools = tmp_path / "tools"
    issues = tmp_path / "issues"
    paths = write_recipes(out_tools=tools, out_issues=issues)
    assert Path(paths["shot_script"]).is_file()
    assert "DAFAF0" in recipe_shot_script().upper()


def test_real_runtime_log_if_present():
    logp = Path(".issues/packets/x32dbg_runtime.log")
    if not logp.is_file():
        return
    r = analyze_paths([logp], write=False)
    assert r.ok
    assert r.counts.get("PLAIN", 0) > 0
    if r.counts.get("SEND10", 0):
        assert any(x.plain_hex10 for x in r.pairs)
