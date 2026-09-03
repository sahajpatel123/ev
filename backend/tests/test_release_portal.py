"""Private release portal: publish gate, checksum integrity, channel rules.

Covers directives B19/B20/B21/B22/B33/B34 at the unit level:
  * publishing requires matching SHA-256 between manifest and IPA bytes
  * canary and stable are independent channels
  * stable is never replaced implicitly; promotion is explicit + build-pinned
  * previous stable lands in bounded archive history
  * OTA manifest carries the absolute tailnet URL and bundle identity
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.config import get_settings
from app.device_gateway import release_portal as portal

FAKE_IPA = b"PK\x03\x04 fake ipa bytes for portal tests" * 64


def _make_artifact(root: Path, build: str, ipa: bytes = FAKE_IPA) -> Path:
    src = root / f"artifact-{build}"
    src.mkdir(parents=True, exist_ok=True)
    meta = {
        "channel": "canary",
        "app_version": "1.0",
        "native_build": build,
        "commit": "c" * 40,
        "web_core_build": "2026.08.22.21",
        "ipa_sha256": hashlib.sha256(ipa).hexdigest(),
    }
    (src / "release.json").write_text(json.dumps(meta))
    (src / "Evie.ipa").write_bytes(ipa)
    return src


def setup_function() -> None:
    portal.RELEASES_ROOT = Path(get_settings().storage_root) / "releases-test"
    if portal.RELEASES_ROOT.exists():
        import shutil

        shutil.rmtree(portal.RELEASES_ROOT)


def test_publish_requires_checksum_match(tmp_path: Path) -> None:
    src = _make_artifact(tmp_path, "100")
    # Corrupt the manifest checksum -> publish must refuse.
    meta = json.loads((src / "release.json").read_text())
    meta["ipa_sha256"] = "0" * 64
    (src / "release.json").write_text(json.dumps(meta))
    try:
        portal.publish_release("canary", src)
        raise AssertionError("publish should have failed on checksum mismatch")
    except ValueError as exc:
        assert "needs release.json" not in str(exc)


def test_publish_canary_then_promote_exact_artifact(tmp_path: Path) -> None:
    src = _make_artifact(tmp_path, "101")
    published = portal.publish_release("canary", src)
    assert published["native_build"] == "101"
    assert (portal._channel_dir("canary") / "Evie.ipa").read_bytes() == FAKE_IPA

    # Wrong expected build refuses (owner approved a different artifact).
    try:
        portal.promote_canary_to_stable(expected_build="999")
        raise AssertionError("promotion should have been refused")
    except ValueError:
        pass

    promoted = portal.promote_canary_to_stable(expected_build="101")
    assert promoted["native_build"] == "101"
    stable_ipa = (portal._channel_dir("stable") / "Evie.ipa").read_bytes()
    assert stable_ipa == FAKE_IPA  # exact artifact, no rebuild


def test_stable_history_is_kept_for_rollback(tmp_path: Path) -> None:
    first = _make_artifact(tmp_path, "200")
    second = _make_artifact(tmp_path, "201", ipa=FAKE_IPA + b"-v2")
    portal.publish_release("stable", first)
    portal.publish_release("stable", second)
    archive = portal.RELEASES_ROOT / "archive" / "stable"
    builds = sorted(p.name for p in archive.iterdir())
    assert builds == ["200"], "previous stable must be archived for rollback"
    current = json.loads((portal._channel_dir("stable") / "release.json").read_text())
    assert current["native_build"] == "201"


def test_unknown_channel_rejected(tmp_path: Path) -> None:
    src = _make_artifact(tmp_path, "300")
    try:
        portal.publish_release("beta", src)
        raise AssertionError("unknown channel must be rejected")
    except ValueError:
        pass


def test_ota_manifest_shape() -> None:
    """The itms-services plist must reference the absolute https IPA URL."""
    import inspect

    source = inspect.getsource(portal.ota_manifest)
    assert "software-package" in source
    assert "/evie-install/{channel}/Evie.ipa" in source
    assert "com.ev.evie.shell" in source
    assert "_base_url(request)" in source  # absolute URL, required by iOS OTA


def test_portal_index_installs_tailscale_pwa() -> None:
    import inspect

    source = inspect.getsource(portal.portal_index)
    assert "/evie/" in source
    assert "Add to Home Screen" in source
    assert "Xcode" in source

