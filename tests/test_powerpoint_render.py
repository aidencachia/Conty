"""Safe macOS framework smoke tests, not PowerPoint acceptance tests.

Only a synthetic PDF in pytest's temporary directory is rendered. Apple Event
objects are constructed locally but NEVER sent. No running applications are
resolved, no user decks are read, and no permissions or input are requested.
"""

from __future__ import annotations

import base64
import sys
from unittest.mock import Mock

import pytest

from conty.adapters.macos.powerpoint import MAX_IMAGE
from conty.adapters.macos.powerpoint_scripts import native as n
from conty.adapters.macos.powerpoint_scripts import render as r

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS frameworks required")


def test_real_quartz_synthetic_pdf_render_and_imageio_png_decode(tmp_path):
    q = pytest.importorskip("Quartz")
    foundation = pytest.importorskip("Foundation")
    pdf = tmp_path / "synthetic.pdf"
    # An unpainted page with one centered black square exercises the renderer's
    # explicit white background as well as its actual PDF drawing transform.
    url = foundation.NSURL.fileURLWithPath_(str(pdf))
    context = q.CGPDFContextCreateWithURL(url, q.CGRectMake(0, 0, 72, 36), None)
    assert context is not None
    try:
        q.CGPDFContextBeginPage(context, None)
        q.CGContextSetRGBFillColor(context, 0, 0, 0, 1)
        q.CGContextFillRect(context, q.CGRectMake(30, 12, 12, 12))
        q.CGPDFContextEndPage(context)
    finally:
        q.CGPDFContextClose(context)
    assert 0 < pdf.stat().st_size < 16 * 1024
    assert pdf.read_bytes().startswith(b"%PDF-")

    pages = r.render(pdf, 1)
    assert len(pages) == 1 and pages[0]["page"] == 1
    png = base64.b64decode(pages[0]["image"], validate=True)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert 0 < len(png) <= MAX_IMAGE
    data = foundation.NSData.dataWithBytes_length_(png, len(png))
    source = q.CGImageSourceCreateWithData(data, None)
    assert source is not None and q.CGImageSourceGetCount(source) == 1
    assert str(q.CGImageSourceGetType(source)) == "public.png"
    image = q.CGImageSourceCreateImageAtIndex(source, 0, None)
    assert image is not None
    width, height = q.CGImageGetWidth(image), q.CGImageGetHeight(image)
    assert (width, height) == (1024, 512)

    # Decode into a known RGBA byte order rather than assuming ImageIO's PNG
    # color model, provider layout, alpha mode, or bytes-per-row.
    pixels = bytearray(width * height * 4)
    bitmap = q.CGBitmapContextCreate(
        pixels,
        width,
        height,
        8,
        width * 4,
        q.CGColorSpaceCreateDeviceRGB(),
        q.kCGBitmapByteOrder32Big | q.kCGImageAlphaPremultipliedLast,
    )
    assert bitmap is not None
    q.CGContextDrawImage(bitmap, q.CGRectMake(0, 0, width, height), image)
    assert pixels[:4] == b"\xff\xff\xff\xff"  # Opaque white, not transparent/black.
    center = ((height // 2) * width + width // 2) * 4
    assert pixels[center : center + 4] == b"\x00\x00\x00\xff"


def test_real_nsappleevent_descriptors_pdf_save_parameters_without_sending(tmp_path):
    apps = pytest.importorskip("AppKit")
    # Bypass __init__: it resolves PowerPoint and configures permission checks.
    # Descriptor construction/export parameter assembly alone has no AE effects.
    b = n.AppleEvents.__new__(n.AppleEvents)
    b.apps = apps
    b.D = apps.NSAppleEventDescriptor
    b.root = b.D.nullDescriptor()
    b.send = Mock()
    presentation = b.prop("pptP", b.by_id("pDWn", 42))
    unused = tmp_path / "not-exported.pdf"
    b.export(presentation, unused)
    assert not unused.exists()
    args, kwargs = b.send.call_args
    assert args[:3] == ("core", "save", presentation)
    assert kwargs == {"mutation": True}
    params = args[3]
    assert set(params) == {"kfil", "fltp"}
    assert params["fltp"].descriptorType() == n.fourcc("enum")
    assert params["fltp"].enumCodeValue() == n.SAVE_PDF == 0x00CC000E
    assert params["kfil"].descriptorType() == n.fourcc("furl")
    assert presentation.descriptorType() == n.fourcc("obj ")
    record = presentation.coerceToDescriptorType_(n.fourcc("reco"))
    assert record is not None
    want = record.descriptorForKeyword_(n.fourcc("want"))
    form = record.descriptorForKeyword_(n.fourcc("form"))
    selected = record.descriptorForKeyword_(n.fourcc("seld"))
    assert want.descriptorType() == n.fourcc("type")
    assert want.typeCodeValue() == n.fourcc("prop")
    assert form.descriptorType() == n.fourcc("enum")
    assert form.enumCodeValue() == n.fourcc("prop")
    assert selected.descriptorType() == n.fourcc("type")
    assert selected.typeCodeValue() == n.fourcc("pptP")
    owner = record.descriptorForKeyword_(n.fourcc("from"))
    window = owner.coerceToDescriptorType_(n.fourcc("reco"))
    assert window.descriptorForKeyword_(n.fourcc("want")).typeCodeValue() == n.fourcc("pDWn")
    assert window.descriptorForKeyword_(n.fourcc("form")).enumCodeValue() == n.fourcc("ID  ")
    identity = window.descriptorForKeyword_(n.fourcc("seld"))
    assert identity.descriptorType() == n.fourcc("long") and identity.int32Value() == 42
    target = b.D.descriptorWithProcessIdentifier_(123)
    assert target.descriptorType() == n.fourcc("kpid")
    assert len(bytes(target.data())) == 4


def test_real_recursive_all_specifiers_use_absolute_ordinal_not_enum():
    apps = pytest.importorskip("AppKit")
    b = n.AppleEvents.__new__(n.AppleEvents)
    b.D = apps.NSAppleEventDescriptor
    b.root = b.D.nullDescriptor()

    def inspect(specifier):
        assert specifier.descriptorType() == n.fourcc("obj ")
        record = specifier.coerceToDescriptorType_(n.fourcc("reco"))
        assert record is not None
        form = record.descriptorForKeyword_(n.fourcc("form"))
        assert form.descriptorType() == n.fourcc("enum")
        assert form.enumCodeValue() == n.fourcc("indx")
        selector = record.descriptorForKeyword_(n.fourcc("seld"))
        assert selector.descriptorType() == n.fourcc("abso")
        assert bytes(selector.data()) == bytes(b.enum(n.fourcc("all ")).data())
        owner = record.descriptorForKeyword_(n.fourcc("from"))
        if owner.descriptorType() == n.fourcc("obj "):
            inspect(owner)
        else:
            assert owner.descriptorType() == n.fourcc("null")

    for specifier in (
        b.all("pDWn"),
        b.all("pSSw"),
        b.all("pSLD"),
        b.all("pSLD", b.all("pptP")),
        b.all("cD05", b.all("pSLD", b.all("pptP"))),
    ):
        inspect(specifier)
