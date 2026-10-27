"""Persistent export-folder tests use only pytest temporary files, never PowerPoint."""

import asyncio
import base64
import os
import stat
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from conty.adapters.base import AdapterError
from conty.adapters.macos import powerpoint as p
from conty.adapters.macos import powerpoint_workspace as w


@pytest.fixture
def directory(tmp_path):
    result = tmp_path / "PowerPoint Previews"
    result.mkdir(mode=0o700)
    return result


def test_workspace_reuses_folder_filename_and_lock_inode(directory):
    inode = directory.stat().st_ino
    lock_inode = None
    for index in range(3):
        with w.export_workspace(directory) as pdf:
            assert pdf == directory / "preview.pdf"
            assert not pdf.exists()
            pdf.write_bytes(f"%PDF-{index}".encode())
            lock = directory / ".export.lock"
            assert stat.S_IMODE(lock.stat().st_mode) == 0o600
            if lock_inode is None:
                lock_inode = lock.stat().st_ino
            assert lock.stat().st_ino == lock_inode
        assert directory.stat().st_ino == inode
        assert not pdf.exists()


def test_default_workspace_is_stable_and_created_only_when_exporting(tmp_path, monkeypatch):
    monkeypatch.setattr(w.Path, "home", lambda: tmp_path)
    expected = tmp_path / "Library" / "Application Support" / "Conty" / "PowerPoint Previews"
    adapter = p.NativePowerPoint()
    asyncio.run(adapter.close())
    assert not expected.exists()
    with w.export_workspace() as pdf:
        assert pdf == expected / "preview.pdf"
        assert stat.S_IMODE(expected.stat().st_mode) == 0o700
        assert stat.S_IMODE(expected.parent.stat().st_mode) == 0o700
    assert expected.is_dir()


def test_refresh_reuses_workspace_across_decks_and_new_adapter_instances(directory):
    state = {
        "slides": [{"id": "10", "number": 1, "title": "Slide 1", "hidden": False}],
        "view": "edit",
        "current_slide_id": None,
        "can_control": False,
    }
    handle = {"pid": 123, "lifetime": "fixed", "window_id": 10, "fullname": "/PRIVATE/A.pptx"}
    paths = []
    image = b"\x89PNG\r\n\x1a\nFAKE"

    def run(helper, request, _timeout, _effects):
        if request.get("operation") == "export":
            pdf = w.Path(request["pdf"])
            assert not pdf.exists()
            paths.append(pdf)
            pdf.write_bytes(b"%PDF-FAKE")
            pdf.chmod(0o644)
            return state
        if helper == "render":
            assert stat.S_IMODE(w.Path(request["pdf"]).stat().st_mode) == 0o600
            return [{"page": 1, "image": base64.b64encode(image).decode()}]
        return state

    inode = directory.stat().st_ino
    for filename in ("A.pptx", "B.pptx", "A.pptx"):
        adapter = p.NativePowerPoint(export_directory=directory)
        with patch.object(adapter, "_run", side_effect=run):
            result = adapter._refresh({**handle, "fullname": f"/PRIVATE/{filename}"})
        assert result["previews"] == [{"slide_id": "10", "page": 1, "image": image}]
        asyncio.run(adapter.close())
        assert directory.stat().st_ino == inode and not paths[-1].exists()
    assert paths == [directory / "preview.pdf"] * 3


def test_workspace_cleans_stale_pdf_but_not_unrelated_files(directory):
    pdf = directory / "preview.pdf"
    other = directory / "unrelated.txt"
    pdf.write_bytes(b"old sensitive artifact")
    other.write_bytes(b"leave alone")
    with w.export_workspace(directory) as target:
        assert target == pdf and not pdf.exists()
        target.write_bytes(b"new artifact")
    assert not pdf.exists() and other.read_bytes() == b"leave alone"


def test_workspace_cleanup_preserves_original_uncertain_error(directory):
    error = AdapterError("timed_out", "native outcome unknown", effects_possible=True)
    with pytest.raises(AdapterError) as caught:
        with w.export_workspace(directory) as pdf:
            pdf.write_bytes(b"%PDF-partial")
            raise error
    assert caught.value is error and caught.value.effects_possible
    assert not pdf.exists() and directory.exists()


def test_cleanup_failure_does_not_mask_primary_effects(directory, monkeypatch):
    error = AdapterError("timed_out", "native outcome unknown", effects_possible=True)
    with pytest.raises(AdapterError) as caught:
        with w.export_workspace(directory) as pdf:
            pdf.write_bytes(b"%PDF-partial")
            monkeypatch.setattr(w, "_remove_pdf", Mock(side_effect=OSError("private path")))
            raise error
    assert caught.value is error and caught.value.effects_possible


def test_successful_export_with_failed_cleanup_reports_redacted_error(directory, monkeypatch):
    with pytest.raises(AdapterError) as caught:
        with w.export_workspace(directory) as pdf:
            pdf.write_bytes(b"%PDF")
            monkeypatch.setattr(w, "_remove_pdf", Mock(side_effect=OSError("PRIVATE_PATH")))
    assert caught.value.code == "export_failed"
    assert "PRIVATE_PATH" not in str(caught.value)


def test_concurrent_export_is_rejected_before_native_work_and_lock_is_released(directory):
    adapter = p.NativePowerPoint(export_directory=directory)
    with w.export_workspace(directory):
        for _ in range(2):
            with patch.object(adapter, "_run") as native:
                with pytest.raises(AdapterError) as caught:
                    adapter._refresh({})
                assert caught.value.code == "export_busy"
                native.assert_not_called()
    with w.export_workspace(directory) as pdf:
        pdf.write_bytes(b"%PDF")
    assert not pdf.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_existing_unsafe_pdf_is_not_followed_or_deleted(directory, tmp_path, kind):
    outside = tmp_path / "outside"
    outside.write_bytes(b"PRIVATE")
    pdf = directory / "preview.pdf"
    if kind == "symlink":
        pdf.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, pdf)
    elif kind == "directory":
        pdf.mkdir()
    else:
        os.mkfifo(pdf)
    with pytest.raises(AdapterError, match="workspace"):
        with w.export_workspace(directory):
            pytest.fail("unsafe artifact must be rejected")
    assert outside.read_bytes() == b"PRIVATE" and pdf.lstat()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public_mode"])
def test_unsafe_lock_is_rejected_without_touching_other_files(directory, tmp_path, kind):
    outside = tmp_path / "outside"
    outside.write_bytes(b"PRIVATE")
    lock = directory / ".export.lock"
    if kind == "symlink":
        lock.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, lock)
    else:
        lock.write_bytes(b"")
        lock.chmod(0o644)
    with pytest.raises(AdapterError):
        with w.export_workspace(directory):
            pytest.fail("unsafe lock must be rejected")
    assert outside.read_bytes() == b"PRIVATE"


def test_symlink_workspace_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    link = tmp_path / "previews"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AdapterError):
        with w.export_workspace(link):
            pytest.fail("symlink must be rejected")
    assert list(outside.iterdir()) == []


def test_default_conty_root_symlink_is_rejected_before_creating_children(tmp_path, monkeypatch):
    monkeypatch.setattr(w.Path, "home", lambda: tmp_path)
    support = tmp_path / "Library" / "Application Support"
    support.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (support / "Conty").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AdapterError):
        with w.export_workspace():
            pytest.fail("symlink root must be rejected")
    assert list(outside.iterdir()) == []


def test_public_workspace_permissions_are_not_silently_changed(directory):
    directory.chmod(0o755)
    with pytest.raises(AdapterError):
        with w.export_workspace(directory):
            pytest.fail("public directory must be rejected")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


def test_relative_workspace_is_rejected_without_creation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AdapterError):
        with w.export_workspace("relative"):
            pytest.fail("relative path must be rejected")
    assert list(tmp_path.iterdir()) == []


def test_foreign_owned_files_are_rejected():
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid() + 1, st_nlink=1)
    assert not w._regular_owned(info)


def test_exported_pdf_is_size_checked_and_owner_only(directory):
    with w.export_workspace(directory) as pdf:
        pdf.write_bytes(b"%PDF")
        pdf.chmod(0o644)
        w.secure_pdf(pdf, 4)
        assert stat.S_IMODE(pdf.stat().st_mode) == 0o600
        with pytest.raises(AdapterError):
            w.secure_pdf(pdf, 3)


def test_exported_pdf_symlink_is_not_followed(directory, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"PRIVATE")
    outside.chmod(0o644)
    pdf = directory / "preview.pdf"
    pdf.symlink_to(outside)
    with pytest.raises(AdapterError):
        w.secure_pdf(pdf, 100)
    assert outside.read_bytes() == b"PRIVATE"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
