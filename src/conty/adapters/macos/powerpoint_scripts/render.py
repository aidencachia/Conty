"""Bounded Quartz/ImageIO PDF renderer; production runs in a supervised child.

Tests may render harmless synthetic PDFs directly, without application access.

No PDFKit, rasterizer executable, or third-party PDF dependency is used. PNGs
leave this worker via a bounded pipe; no preview images are written to disk.
"""

from __future__ import annotations

import base64
import json
import math
import sys
from pathlib import Path

from conty.adapters.macos.powerpoint import MAX_IMAGE, MAX_PDF, MAX_PREVIEWS, MAX_SLIDES, _error


def render(path, count, *, quartz=None, foundation=None):
    if quartz is None:
        import Quartz as quartz
    if foundation is None:
        import Foundation as foundation

    if type(count) is not int or not 1 <= count <= MAX_SLIDES:
        raise _error("limit_exceeded")
    if not path.is_file() or path.is_symlink() or not 0 < path.stat().st_size <= MAX_PDF:
        raise _error("export_failed")
    q = quartz
    url = foundation.NSURL.fileURLWithPath_(str(path))
    document = q.CGPDFDocumentCreateWithURL(url)
    if document is None or q.CGPDFDocumentIsEncrypted(document):
        raise _error("export_failed")
    if q.CGPDFDocumentGetNumberOfPages(document) != count:
        raise _error("export_failed")
    # A per-page share guarantees the aggregate bound, even for a 500-page deck.
    budget = min(MAX_IMAGE, MAX_PREVIEWS // count)
    result = []
    for index in range(1, count + 1):
        page = q.CGPDFDocumentGetPage(document, index)
        if page is None:
            raise _error("export_failed")
        rect = q.CGPDFPageGetBoxRect(page, q.kCGPDFMediaBox)
        width, height = float(rect.size.width), float(rect.size.height)
        if not all(math.isfinite(v) and 0 < v <= 100000 for v in (width, height)):
            raise _error("export_failed")
        rotation = q.CGPDFPageGetRotationAngle(page) % 360
        if rotation not in (0, 90, 180, 270):
            raise _error("export_failed")
        if rotation in (90, 270):
            width, height = height, width
        edge = 1024
        while True:
            scale = edge / max(width, height)
            w, h = max(1, round(width * scale)), max(1, round(height * scale))
            context = q.CGBitmapContextCreate(
                None,
                w,
                h,
                8,
                w * 4,
                q.CGColorSpaceCreateDeviceRGB(),
                q.kCGImageAlphaNoneSkipLast,
            )
            if context is None:
                raise _error("export_failed")
            destination_rect = q.CGRectMake(0, 0, w, h)
            q.CGContextSetRGBFillColor(context, 1, 1, 1, 1)
            q.CGContextFillRect(context, destination_rect)
            transform = q.CGPDFPageGetDrawingTransform(
                page, q.kCGPDFMediaBox, destination_rect, 0, True
            )
            q.CGContextConcatCTM(context, transform)
            q.CGContextDrawPDFPage(context, page)
            image = q.CGBitmapContextCreateImage(context)
            if image is None:
                raise _error("export_failed")
            data = foundation.NSMutableData.data()
            destination = q.CGImageDestinationCreateWithData(data, "public.png", 1, None)
            if destination is None:
                raise _error("export_failed")
            q.CGImageDestinationAddImage(destination, image, None)
            if not q.CGImageDestinationFinalize(destination):
                raise _error("export_failed")
            png = bytes(data)
            if len(png) <= budget:
                break
            if edge <= 64:
                raise _error("limit_exceeded")
            edge = max(64, edge * 3 // 4)
        result.append({"page": index, "image": base64.b64encode(png).decode("ascii")})
    return result


def main():
    try:
        request = json.loads(sys.argv[1])
        result = {"ok": True, "result": render(Path(request["pdf"]), request["count"])}
    except Exception as exc:
        from conty.adapters.base import AdapterError

        result = {
            "ok": False,
            "code": exc.code if isinstance(exc, AdapterError) else "export_failed",
            "effects_possible": False,
            "poison": False,
        }
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
