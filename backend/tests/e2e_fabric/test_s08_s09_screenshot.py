"""S8-S9: screenshot-as-reference — pane inference accuracy, tmux layout, resolution.

Accuracy threshold (documented): on the synthetic 2x2 fixture (4 text bands at
y = 0/100/200/300 with the 40px clustering gap), infer_panes must recover
exactly 4 panes with every word in its correct band — 100% band accuracy.
"""
import os
import shutil
import time

import pytest

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")


def _fixture_ocr():
    # 2x2 terminal panes: two columns x two rows of text bands
    words = []
    for band, y in enumerate((0, 100, 200, 300)):
        for col, x in enumerate((0, 400)):
            words.append({"text": f"pane{band // 2}-{col} cmd --resume sess{band}{col}",
                          "x": x, "y": y + 5 * col, "band": band})
    return words


def test_s08_pane_inference_accuracy():
    from app.fabric.screen_ref import infer_panes
    ocr = _fixture_ocr()
    panes = infer_panes(ocr)
    assert len(panes) == 4, panes
    for pane, band in zip(panes, (0, 1, 2, 3)):
        got = sorted(w["band"] for w in pane["words"])
        assert got == [band, band], got


def test_s08_plan_confirm_execute_pipeline(fake_screen):
    from app.fabric.screen_ref import plan, confirm, execute
    p = plan("restore 2x2 layout")
    assert p["ok"]
    refused = execute(p, screen=fake_screen)
    assert not refused["ok"] and refused["evidence"].get("needs_confirm"), refused
    done = execute(confirm(p), screen=fake_screen)
    assert done["ok"] and done["verified"], done


@needs_tmux
def test_s08_tmux_layout_readback(tmp_path):
    from app.fabric.terminal import TmuxLayout
    lay = TmuxLayout(root=tmp_path)
    panes = [
        {"cwd": str(tmp_path), "command": "echo PANE-ALPHA", "title": "alpha"},
        {"cwd": str(tmp_path), "command": "echo PANE-BETA", "title": "beta"},
    ]
    try:
        parked = lay.open(panes, confirm=False)
        assert parked["ok"] and parked["evidence"]["needs_confirm"], parked
        assert len(parked["evidence"]["plan"]) == 2
        opened = lay.open(panes, confirm=True)
        assert opened["ok"], opened
        time.sleep(1.5)
        back = lay.read_panes()
        assert back["ok"] and back["verified"], back
        texts = list(back["evidence"]["panes"].values())
        assert any("PANE-ALPHA" in t for t in texts), texts
        assert any("PANE-BETA" in t for t in texts), texts
    finally:
        lay.close()


def test_s09_last_screenshot_resolution(tmp_path):
    from app.fabric.screen_ref import newest_screenshot, resolve_last_screenshot
    shot_dir = tmp_path / "shots"
    shot_dir.mkdir()
    old = shot_dir / "Screenshot 2026-10-17 at 9.00.00 AM.png"
    new = shot_dir / "Screenshot 2026-10-17 at 9.05.00 AM.png"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    (shot_dir / "notes.txt").write_text("ignore me")
    found = newest_screenshot(shot_dir)
    assert found and found["path"] == str(new), found
    assert found["untrusted"] is True
    # ambiguous: conversation image wins and the source says so
    conv = [{"images": ["conv-img-1"]}]
    amb = resolve_last_screenshot(conv, store=[str(old), str(new)])
    assert amb["source"] == "conversation" and amb["image"] == "conv-img-1", amb


def test_s09_embedded_instruction_not_executed(tmp_path):
    from app.fabric.policy import decide
    from app.fabric.shell_sessions import ShellSessions
    # screenshot text is untrusted_content: even AUTO commands park for confirm
    assert decide("read", "echo hi", "untrusted_content") == "CONFIRM"
    s = ShellSessions(root=tmp_path)
    parked = s.run("echo pwned", provenance="untrusted_content")
    assert not parked["ok"] and parked["evidence"].get("needs_confirm"), parked
