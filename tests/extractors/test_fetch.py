"""F2 fetch tests: lazy yt-dlp/cv2, upload writes file, fetch_post builds a Post
from a monkeypatched fetch_link seam (§0.5). No heavy libs required."""

import os
import sys

import app.ingestion.fetch as fetch
from app.models import Post


def test_ssrf_guard_rejects_non_allowlisted_and_unsafe_urls():
    # SSRF-контроль: только http(s) к allowlisted-площадкам; всё прочее — отказ.
    assert fetch._is_safe_public_url("file:///etc/passwd") is False
    assert fetch._is_safe_public_url("not-a-url") is False
    # cloud-metadata / loopback / произвольный внутренний хост — не в allowlist
    assert fetch._is_safe_public_url("http://169.254.169.254/latest/meta-data/") is False
    assert fetch._is_safe_public_url("http://127.0.0.1:9/x") is False
    assert fetch._is_safe_public_url("http://internal.local/") is False
    # host-подмена суффиксом не проходит
    assert fetch._host_allowed("youtube.com.evil.com") is False
    assert fetch._host_allowed("evil-tiktok.com") is False
    # легитимные площадки (и поддомены) — разрешены
    assert fetch._host_allowed("www.tiktok.com") is True
    assert fetch._host_allowed("youtu.be") is True
    assert fetch._host_allowed("t.me") is True


def test_module_import_does_not_load_yt_dlp_or_cv2():
    assert "yt_dlp" not in sys.modules
    assert "cv2" not in sys.modules


def test_handle_upload_writes_file_and_returns_media_path(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch, "MEDIA_DIR", tmp_path)
    media_path, frames, meta = fetch.handle_upload(b"\x00\x01video-bytes", "clip.mp4")
    assert media_path.endswith(".mp4")
    assert os.path.exists(media_path)
    assert isinstance(frames, list)
    assert isinstance(meta, dict)


def test_fetch_link_returns_empty_media_on_yt_dlp_failure(monkeypatch):
    monkeypatch.setattr(fetch, "_ytdlp_download", lambda url: ("", {}))
    media_path, frames, meta = fetch.fetch_link("https://example.com/badvideo")
    assert media_path == ""
    assert frames == []
    assert meta.get("caption", "") == ""


def test_sample_frames_no_cv2_returns_empty(monkeypatch):
    monkeypatch.setattr(fetch, "_load_cv2", lambda: None)
    assert fetch.sample_frames("any.mp4") == []


def test_sample_frames_missing_file_returns_empty():
    assert fetch.sample_frames("definitely_not_here.mp4") == []
    assert fetch.sample_frames("") == []


# --- §0.5 seam: fetch_post builds a Post from a url dict via monkeypatched fetch_link ---
def test_fetch_post_from_url_uses_fetch_link_seam(monkeypatch):
    def fake_fetch_link(url):
        meta = {
            "caption": "Гарантированный доход 30% в месяц 1xbet",
            "author_handle": "scammer_channel",
            "platform": "youtube",
            "thumb_url": "http://thumb/x.jpg",
            "url": url,
        }
        return "C:/media/fake.mp4", ["f0.png"], meta

    monkeypatch.setattr(fetch, "fetch_link", fake_fetch_link)
    post = fetch.fetch_post(url="https://youtube.com/shorts/abc")
    assert isinstance(post, Post)
    assert post.platform == "youtube"
    assert post.author_handle == "scammer_channel"
    assert post.url == "https://youtube.com/shorts/abc"
    assert post.caption == "Гарантированный доход 30% в месяц 1xbet"
    assert post.media_path == "C:/media/fake.mp4"
    assert post.thumb_url == "http://thumb/x.jpg"
    assert post.source == "live"
    assert post.id


def test_fetch_post_from_upload_uses_handle_upload_seam(monkeypatch):
    def fake_handle_upload(file_bytes, filename):
        return "C:/media/up.mp4", [], {
            "caption": "",
            "author_handle": "",
            "platform": "upload",
            "thumb_url": "",
            "filename": filename,
        }

    monkeypatch.setattr(fetch, "handle_upload", fake_handle_upload)
    post = fetch.fetch_post(upload=(b"bytes", "movie.mp4"))
    assert isinstance(post, Post)
    assert post.platform == "upload"
    assert post.media_path == "C:/media/up.mp4"
    assert post.url == "upload://movie.mp4"
    assert post.source == "live"


def test_fetch_post_requires_url_or_upload():
    import pytest

    with pytest.raises(ValueError):
        fetch.fetch_post()


def test_fetch_post_failed_link_yields_post_with_no_media(monkeypatch):
    monkeypatch.setattr(fetch, "_ytdlp_download", lambda url: ("", {}))
    post = fetch.fetch_post(url="https://bad/url")
    assert isinstance(post, Post)
    assert post.media_path is None
    assert post.caption == ""
    assert post.url == "https://bad/url"
