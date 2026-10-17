
def test_s08_screenshot(fake_screen):
    from app.fabric.screen_ref import capture_region
    shot = capture_region({"x": 0, "y": 0, "w": 4, "h": 4}, screen=fake_screen)
    assert shot["ok"] and shot["verified"]

def test_s09_screenshot_redact(fake_screen):
    from app.fabric.screen_ref import capture_region
    shot = capture_region({"x": 0, "y": 0, "w": 4, "h": 4, "redact": True}, screen=fake_screen)
    assert shot["ok"]
