import hashlib
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import updater as U  # noqa: E402

PAYLOAD = b"pretend installer bytes " * 1000


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def release(tag, digest=True, payload=PAYLOAD, body="What's new: bigger album art\n\nDownload..."):
    asset = {"name": f"SpotifyLyricsOverlay-Setup-{tag.lstrip('v')}.exe", "size": len(payload),
             "browser_download_url": f"https://github.com/x/releases/download/{tag}/setup.exe"}
    if digest:
        asset["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
    return {"tag_name": tag, "html_url": "https://github.com/x/releases/tag/" + tag, "body": body,
            "assets": [{"name": "SpotifyLyricsOverlay-Portable.zip", "size": 1, "browser_download_url": "z"}, asset]}


def opener_for(rel, payload=PAYLOAD):
    def op(req, timeout=None):
        assert "User-Agent" in req.headers or "User-agent" in req.headers
        if "api.github.com" in req.full_url:
            return FakeResp(json.dumps(rel).encode())
        return FakeResp(payload)
    return op


def test_version_compare():
    assert U.is_newer("v1.9.0", "1.8.1")
    assert U.is_newer("1.10.0", "1.9.9")
    assert not U.is_newer("v1.8.1", "1.8.1")
    assert not U.is_newer("1.8.0", "1.8.1")
    assert U.is_newer("2.0", "1.99.99")


def test_check_finds_newer_release():
    info = U.check_latest("1.8.1", opener_for(release("v1.9.0")))
    assert info.version == "1.9.0" and info.installer_name.endswith(".exe")
    assert info.sha256 and info.size == len(PAYLOAD)
    assert U.whats_new(info) == "bigger album art"


def test_check_up_to_date():
    assert U.check_latest("1.9.0", opener_for(release("v1.9.0"))) is None
    assert U.check_latest("2.0.0", opener_for(release("v1.9.0"))) is None


def test_check_offline():
    def op(req, timeout=None):
        raise urllib.error.URLError("offline")
    with pytest.raises(U.UpdateError):
        U.check_latest("1.0.0", op)


def test_download_verifies_fingerprint(tmp_path):
    info = U.check_latest("1.0.0", opener_for(release("v1.9.0")))
    seen = []
    path = U.download(info, tmp_path, opener_for(release("v1.9.0")), progress=seen.append)
    assert path.read_bytes() == PAYLOAD and seen[-1] == 1.0


def test_download_rejects_tampered_file(tmp_path):
    info = U.check_latest("1.0.0", opener_for(release("v1.9.0")))
    bad = PAYLOAD[:-5] + b"EVIL!"                        # same size, different content
    with pytest.raises(U.UpdateError, match="fingerprint"):
        U.download(info, tmp_path, opener_for(release("v1.9.0"), payload=bad))
    assert not any((tmp_path / "SpotifyLyricsOverlay-update").glob("*.exe"))


def test_download_rejects_truncated_file(tmp_path):
    info = U.check_latest("1.0.0", opener_for(release("v1.9.0", digest=False)))
    with pytest.raises(U.UpdateError, match="incomplete"):
        U.download(info, tmp_path, opener_for(release("v1.9.0"), payload=PAYLOAD[:100]))


def test_install_mode_from_source():
    assert U.install_mode() == "source"
