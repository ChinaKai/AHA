from __future__ import annotations

import io
import zipfile
from pathlib import Path

from aha_cli.domain.models import default_knowledge_config
from aha_cli.store.io import write_json
from aha_cli.store.knowledge import find_entry, init_knowledge_base, read_entry, write_entry
from aha_cli.store.knowledge_assets import (
    EntryImageRejected,
    _entry_asset_dir,
    _entry_asset_path,
    add_docx_media_as_images,
    add_entry_attachment,
    attachment_kind,
    attachment_mime_for,
    extract_docx_media,
    is_local_only_attachment,
    local_assets_root,
    read_entry_attachment,
)
from aha_cli.store.paths import config_path


def _setup(tmp_path: Path) -> tuple[Path, dict]:
    home = tmp_path / ".aha"
    kb = default_knowledge_config()
    kb["enabled"] = True
    cfg = {"knowledge": kb}
    write_json(config_path(home), cfg)
    init_knowledge_base(home, cfg)
    write_entry(home, config=cfg, scope="project", kind="solutions", project_key_value="git-abc",
                title="Attachment test", body="body", slug="attachment-test", meta={"type": "solution"})
    return home, cfg


def _entry(home: Path, cfg: dict):
    return find_entry(home, cfg, "attachment-test")


def test_attachment_mime_and_kind():
    assert attachment_mime_for("x.pdf") == "application/pdf"
    assert attachment_mime_for("x.txt") == "text/plain"
    assert attachment_mime_for("x.docx") == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert attachment_mime_for("x.mp4") == "video/mp4"
    assert attachment_kind("application/pdf") == "pdf"
    assert attachment_kind("video/mp4") == "video"
    assert attachment_kind("text/plain") == "text"


def test_local_only_policy():
    assert is_local_only_attachment("video/mp4", 100) is True
    assert is_local_only_attachment("audio/mpeg", 100) is True
    assert is_local_only_attachment("text/plain", 10 * 1024 * 1024) is True  # > sync limit
    assert is_local_only_attachment("text/plain", 100) is False
    assert is_local_only_attachment("application/pdf", 1000) is False


def test_small_text_attachment_syncs_into_kb(tmp_path: Path):
    home, cfg = _setup(tmp_path)
    _, att = add_entry_attachment(home, cfg, "attachment-test", data=b"hello", filename="notes.txt")
    assert att["mime"] == "text/plain"
    assert att["local_only"] is False
    assert att["kind"] == "text"
    assert (_entry_asset_dir(_entry(home, cfg)) / "notes.txt").is_file()
    # Metadata recorded with checksum.
    entry = _entry(home, cfg)
    assert entry["meta"]["assets"][-1]["sha256"]


def test_pdf_attachment_sniffs_magic(tmp_path: Path):
    home, cfg = _setup(tmp_path)
    pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
    _, att = add_entry_attachment(home, cfg, "attachment-test", data=pdf, filename="doc.bin")
    assert att["mime"] == "application/pdf"
    assert att["local_only"] is False


def test_media_attachment_is_local_only_outside_repo(tmp_path: Path):
    home, cfg = _setup(tmp_path)
    _, att = add_entry_attachment(home, cfg, "attachment-test", data=b"FAKE-MP4", filename="video.mp4")
    assert att["local_only"] is True
    assert att["kind"] == "video"
    assert (local_assets_root(home) / att["sha256"] / "video.mp4").is_file()
    # Not inside the KB repo.
    assert not (_entry_asset_dir(_entry(home, cfg)) / "video.mp4").exists()


def test_oversized_file_is_local_only(tmp_path: Path):
    home, cfg = _setup(tmp_path)
    big = b"x" * (6 * 1024 * 1024)  # > 5MB sync limit
    _, att = add_entry_attachment(home, cfg, "attachment-test", data=big, filename="big.pdf")
    assert att["local_only"] is True


def test_unsupported_attachment_rejected(tmp_path: Path):
    home, cfg = _setup(tmp_path)
    try:
        add_entry_attachment(home, cfg, "attachment-test", data=b"GIF89a", filename="x.exe")
        raise AssertionError("expected rejection")
    except EntryImageRejected as exc:
        assert "unsupported" in str(exc)


def test_read_attachment_synced_and_local(tmp_path: Path):
    home, cfg = _setup(tmp_path)
    add_entry_attachment(home, cfg, "attachment-test", data=b"text-data", filename="notes.txt")
    add_entry_attachment(home, cfg, "attachment-test", data=b"mp4-data", filename="clip.mp4")

    data, rec = read_entry_attachment(home, cfg, "attachment-test", "notes.txt")
    assert data == b"text-data"
    assert rec["mime"] == "text/plain" and rec["disposition"] == "inline"

    data, rec = read_entry_attachment(home, cfg, "attachment-test", "clip.mp4")
    assert data == b"mp4-data"
    assert rec["mime"] == "video/mp4" and rec["disposition"] == "inline"


def test_read_attachment_blocks_path_traversal(tmp_path: Path):
    home, cfg = _setup(tmp_path)
    # _entry_asset_path refuses traversal segments.
    entry = _entry(home, cfg)
    assert _entry_asset_path(entry, "assets/attachment-test/../../../etc/passwd") is None
    assert _entry_asset_path(entry, "../secrets.txt") is None
    # read_entry_attachment on a traversal path returns None (record lookup fails).
    assert read_entry_attachment(home, cfg, "attachment-test", "../../etc/passwd") is None


def test_extract_docx_media(tmp_path: Path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"PNGDATA")
        zf.writestr("word/media/image2.jpg", b"\xff\xd8\xff" + b"JPGDATA")
    media = extract_docx_media(buf.getvalue())
    assert len(media) == 2
    assert {m["name"] for m in media} == {"image1.png", "image2.jpg"}
    assert all(m["mime"] in {"image/png", "image/jpeg"} for m in media)
    # Non-zip is graceful.
    assert extract_docx_media(b"not a zip") == []
    assert extract_docx_media(b"") == []


def test_add_docx_media_as_images(tmp_path: Path):
    home, cfg = _setup(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"PNGDATA")
    result = add_docx_media_as_images(home, cfg, "attachment-test", data=buf.getvalue())
    assert result["added"] == 1
    entry = _entry(home, cfg)
    image_assets = [a for a in entry["meta"]["assets"] if a["kind"] == "image"]
    assert len(image_assets) == 1
    assert image_assets[0]["mime"] == "image/png"


def test_attachment_metadata_persists_across_rewrite(tmp_path: Path):
    home, cfg = _setup(tmp_path)
    add_entry_attachment(home, cfg, "attachment-test", data=b"hello", filename="notes.txt")
    add_entry_attachment(home, cfg, "attachment-test", data=b"mp4data", filename="clip.mp4")
    entry = _entry(home, cfg)
    assert len(entry["meta"]["assets"]) == 2
    # Rewriting the entry body preserves assets.
    write_entry(home, config=cfg, scope="project", kind="solutions", project_key_value="git-abc",
                title="Attachment test", body="updated", slug="attachment-test",
                meta=entry["meta"])
    entry2 = read_entry(find_entry(home, cfg, "attachment-test")["path"])
    assert len(entry2["meta"]["assets"]) == 2


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _get(home: Path, path: str, query: dict | None = None):
    from aha_cli.web.knowledge_routes import knowledge_route_response
    from tests.helpers import json_response_body

    return json_response_body(knowledge_route_response(home, "GET", path, query or {}, b"", {}))


def test_attachment_route_upload_and_serve(tmp_path: Path):
    import base64

    from aha_cli.web.knowledge_routes import knowledge_route_response
    from tests.helpers import json_response_body

    home, cfg = _setup(tmp_path)
    entry = _entry(home, cfg)
    # Text attachment (synced).
    data_url = "data:text/plain;base64," + base64.b64encode(b"route text").decode()
    up = json_response_body(knowledge_route_response(
        home, "POST", "/api/kb/attachment", {},
        ('{"id":"attachment-test","filename":"notes.txt","data_url":"' + data_url + '"}').encode(), {},
    ))
    assert up["ok"] is True
    att = up["attachment"]
    assert att["mime"] == "text/plain" and att["local_only"] is False

    # Serve inline with Content-Disposition.
    raw = knowledge_route_response(
        home, "GET", "/api/kb/attachment",
        {"id": [entry["meta"]["id"]], "path": [att["path"]]}, b"", {},
    )
    assert raw.startswith(b"HTTP/1.1 200 OK")
    assert b"text/plain" in raw
    assert b"inline" in raw
    assert raw.endswith(b"route text")


def test_attachment_route_media_local_only(tmp_path: Path):
    import base64

    from aha_cli.web.knowledge_routes import knowledge_route_response
    from tests.helpers import json_response_body

    home, cfg = _setup(tmp_path)
    data_url = "data:video/mp4;base64," + base64.b64encode(b"fake-mp4").decode()
    up = json_response_body(knowledge_route_response(
        home, "POST", "/api/kb/attachment", {},
        ('{"id":"attachment-test","filename":"clip.mp4","data_url":"' + data_url + '"}').encode(), {},
    ))
    assert up["attachment"]["local_only"] is True


def test_attachment_route_rejects_unsupported(tmp_path: Path):
    import base64

    from aha_cli.web.knowledge_routes import knowledge_route_response
    from tests.helpers import json_response_body

    home, cfg = _setup(tmp_path)
    data_url = "data:application/octet-stream;base64," + base64.b64encode(b"MZ...").decode()
    resp = knowledge_route_response(
        home, "POST", "/api/kb/attachment", {},
        ('{"id":"attachment-test","filename":"tool.exe","data_url":"' + data_url + '"}').encode(), {},
    )
    body = json_response_body(resp)
    assert resp.startswith(b"HTTP/1.1 400")
    assert "unsupported" in body["error"]


def test_attachment_route_docx_extraction(tmp_path: Path):
    import base64
    import io
    import zipfile

    from aha_cli.web.knowledge_routes import knowledge_route_response
    from tests.helpers import json_response_body

    home, cfg = _setup(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"PNGDATA")
    docx_b64 = base64.b64encode(buf.getvalue()).decode()
    resp = knowledge_route_response(
        home, "POST", "/api/kb/attachment", {},
        ('{"id":"attachment-test","filename":"report.docx","data_url":"data:application/zip;base64,' + docx_b64 + '"}').encode(), {},
    )
    body = json_response_body(resp)
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert body["attachment"]["mime"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert body.get("extracted_images", {}).get("added") == 1
    entry = _entry(home, cfg)
    assert any(a["kind"] == "image" for a in entry["meta"]["assets"])
