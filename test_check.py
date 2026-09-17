"""Self-check for the resource capture pipeline (no Windows viewer needed)."""
from __future__ import annotations

import io
import os
import sqlite3
import struct
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import export_dmme as ex


def encoded(color: tuple[int, int, int], format_name: str,
            size: tuple[int, int] = (1000, 800)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format=format_name)
    return stream.getvalue()


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "resources"
        output = Path(temporary) / "pages"
        red = encoded((255, 0, 0), "PNG")
        blue = encoded((0, 0, 255), "JPEG")

        writer = ex.ResourceWriter(root)
        # A small UI asset is captured too, but must not become a page.
        thumbnail = encoded((0, 255, 0), "PNG", (100, 100))
        writer.submit(
            {"source": "qt.QFileDevice.readData", "owner": "thumbnail", "sequence": 1},
            thumbnail,
        )
        writer.submit_boundary(
            "resource-eof",
            {"source": "qt.QFileDevice.readData", "owner": "thumbnail", "sequence": 2},
        )
        # Chunks are queued immediately and written by the background worker.
        writer.submit(
            {"source": "qt.QPixmap.loadFromData", "owner": "red", "sequence": 7,
             "page": 0},
            red[:19],
        )
        writer.submit(
            {"source": "qt.QPixmap.loadFromData", "owner": "red", "sequence": 8,
             "page": 0},
            red[19:],
        )
        writer.submit_boundary(
            "resource-eof",
            {"source": "qt.QPixmap.loadFromData", "owner": "red", "sequence": 9},
        )
        writer.submit(
            {"source": "qt.QPixmap.loadFromData", "owner": "blue", "sequence": 10,
             "page": 1},
            blue,
        )
        writer.submit_boundary(
            "resource-eof",
            {"source": "qt.QPixmap.loadFromData", "owner": "blue", "sequence": 11},
        )
        repeated = encoded((128, 128, 128), "PNG")
        writer.submit(
            {"source": "qt.QPixmap.loadFromData", "owner": "repeat-one", "sequence": 13,
             "page": 2},
            repeated,
        )
        writer.submit_boundary(
            "resource-eof",
            {"source": "qt.QPixmap.loadFromData", "owner": "repeat-one", "sequence": 14},
        )
        writer.submit(
            {"source": "qt.QPixmap.loadFromData", "owner": "repeat-two", "sequence": 15,
             "page": 3},
            repeated,
        )
        writer.submit_boundary(
            "resource-eof",
            {"source": "qt.QPixmap.loadFromData", "owner": "repeat-two", "sequence": 16},
        )
        # A second delivery for page 0 is a retry; the final exporter removes it.
        writer.submit(
            {"source": "qt.QPixmap.loadFromData", "owner": "duplicate", "sequence": 17,
             "page": 0},
            red,
        )
        writer.close()

        resources = writer.resources()
        assert writer.chunks == 7
        assert writer.bytes_written == (
            len(thumbnail) + len(red) + len(blue) + 2 * len(repeated) + len(red)
        )
        assert [ex.resource_kind(item.path) for item in resources] == [
            "png", "png", "jpeg", "png", "png", "png"
        ]

        pages = ex.export_images(resources, output)
        assert [path.name for path in pages] == [
            "page_001.png", "page_002.jpg", "page_003.png", "page_004.png"
        ]
        assert pages[0].read_bytes() == red, "PNG resources must stay byte-for-byte unchanged"
        assert pages[1].read_bytes() == blue, "JPEG resources must stay byte-for-byte unchanged"
        assert ex.resource_kind(pages[1]) == "jpeg"
        assert ex._image_size(pages[1]) == (1000, 800), "image inspection must not rewrite bytes"
        assert pages[2].read_bytes() == pages[3].read_bytes(), (
            "identical images on different logical pages must both be retained"
        )

        def captured(path: Path, payload: bytes, sequence: int,
                     page: int) -> ex.CapturedResource:
            path.write_bytes(payload)
            return ex.CapturedResource(
                path=path,
                sequence=sequence,
                sha256=ex._sha256(path),
                size=len(payload),
                source=ex.PAGE_RESOURCE_SOURCE,
                page=page,
            )

        page_one = encoded((0, 0, 255), "PNG")
        page_two = encoded((0, 255, 0), "PNG")
        page_three = encoded((255, 255, 0), "PNG")
        polluted = [
            # These are pages 2 and 3 restored before navigation was attached.
            captured(root / "startup-two.png", page_two, 0, -1),
            captured(root / "startup-three.png", page_three, 1, -1),
            # The controlled traversal later sees pages 0 through 3.
            captured(root / "page-zero.png", red, 2, 0),
            captured(root / "page-one.png", page_one, 3, 1),
            captured(root / "page-two.png", page_two, 4, 2),
            captured(root / "page-three.png", page_three, 5, 3),
        ]
        filtered = ex.export_images(
            polluted, Path(temporary) / "startup-filtered", initial_page=2
        )
        assert [path.read_bytes() for path in filtered] == [
            red, page_one, page_two, page_three
        ], "stale startup pages must not win dedupe"
        retained = ex.export_images(
            [
                captured(root / "startup-zero.png", red, 0, -1),
                captured(root / "startup-one.png", page_one, 1, -1),
                captured(root / "page-zero-reload.png", red, 2, 0),
                captured(root / "page-one-reload.png", page_one, 3, 1),
                captured(root / "zero-page-two.png", page_two, 4, 2),
                captured(root / "zero-page-three.png", page_three, 5, 3),
            ],
            Path(temporary) / "initial-page-zero",
            initial_page=0,
        )
        assert [path.read_bytes() for path in retained] == [
            red, page_one, page_two, page_three
        ], "startup page 0 and its reload are one logical page"

        ex.validate_navigation(4, [0, 0, 1, 2, 3], len(filtered), initial_page=2)
        try:
            ex.validate_navigation(2, [1, 0], 2, initial_page=1)
        except RuntimeError:
            pass
        else:
            raise AssertionError("out-of-order navigation should fail")

        stream_root = Path(temporary) / "stream-resources"
        stream_writer = ex.ResourceWriter(stream_root)
        stream_message = {
            "source": "net.URLRequest.Read",
            "owner": "url-owner",
            "sequence": 20,
            "url": "cjh://book/item/xhtml/p-stream.xhtml",
        }
        stream_writer.submit(stream_message, b"abc")
        stream_writer.submit({**stream_message, "sequence": 21}, b"def")
        stream_writer.submit_boundary("resource-eof", stream_message)
        stream_writer.close()
        stream_resources = stream_writer.resources()
        assert len(stream_resources) == 1
        assert stream_resources[0].path.read_bytes() == b"abcdef"
        assert stream_resources[0].url.endswith("/p-stream.xhtml")

        html = (
            b'<?xml version="1.0"?><html '
            b'xmlns="http://www.w3.org/1999/xhtml"><head><title>Test</title>'
            b'</head><body><img src="../image/cover.jpg"/></body></html>'
        )
        cover = encoded((255, 0, 0), "JPEG", (1200, 800))
        html_path = Path(temporary) / "epub-html.bin"
        cover_path = Path(temporary) / "epub-cover.bin"
        html_path.write_bytes(html)
        cover_path.write_bytes(cover)
        epub_resources = [
            ex.CapturedResource(
                path=html_path, sequence=30, sha256=ex._sha256(html_path),
                size=len(html), source="load_job.ReadRawData", page=0,
                url="cjh://book/item/xhtml/p-cover.xhtml",
            ),
            ex.CapturedResource(
                path=cover_path, sequence=31, sha256=ex._sha256(cover_path),
                size=len(cover), source="load_job.ReadRawData", page=1,
                url="cjh://book/item/image/cover.jpg",
            ),
        ]
        selected = ex._epub_resources(
            [
                ex.CapturedResource(
                    path=cover_path, sequence=29, sha256=ex._sha256(cover_path),
                    size=len(cover), source="load_job.ReadRawData", page=-1,
                    url="cjh://book/item/xhtml/stale.xhtml",
                ),
                *epub_resources,
            ],
            drop_startup=True,
        )
        assert list(selected) == ["xhtml/p-cover.xhtml", "image/cover.jpg"]
        reordered = ex._epub_resources(
            [
                ex.CapturedResource(
                    path=cover_path, sequence=29, sha256=ex._sha256(cover_path),
                    size=len(cover), source="load_job.ReadRawData", page=-1,
                    url="cjh://book/item/xhtml/stale.xhtml",
                ),
                *epub_resources,
            ],
            reorder_startup=True,
        )
        assert list(reordered) == [
            "image/cover.jpg", "xhtml/p-cover.xhtml", "xhtml/stale.xhtml"
        ]
        unnamed = ex.export_epub(
            epub_resources, Path(temporary) / "epub" / "unnamed.epub",
            Path("k077bscsh09791.dmmr"),
        )
        with zipfile.ZipFile(unnamed) as archive:
            assert "<dc:title>k077bscsh09791</dc:title>" in archive.read(
                "OEBPS/content.opf"
            ).decode()

        epub_path = ex.export_epub(
            epub_resources, Path(temporary) / "epub" / "book.epub",
            Path("k077bscsh09791.dmmr"),
            title="記憶の鍵盤（新潮文庫nex）",
        )
        with zipfile.ZipFile(epub_path) as archive:
            assert archive.namelist()[0] == "mimetype"
            assert archive.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
            assert archive.read("mimetype") == b"application/epub+zip"
            assert "dcterms:modified" in archive.read("OEBPS/content.opf").decode()
            assert 'toc="ncx"' in archive.read("OEBPS/content.opf").decode()
            assert archive.read("OEBPS/toc.ncx").startswith(b"<?xml")
            assert "META-INF/" in archive.namelist()
            assert "OEBPS/" in archive.namelist()
            assert "OEBPS/xhtml/" in archive.namelist()
            assert archive.testzip() is None
            assert "OEBPS/xhtml/p-cover.xhtml" in archive.namelist()
            assert "OEBPS/image/cover.jpg" in archive.namelist()
            assert archive.read("OEBPS/image/cover.jpg") == cover
            opf = archive.read("OEBPS/content.opf").decode()
            assert "<dc:title>記憶の鍵盤（新潮文庫nex）</dc:title>" in opf
            assert "k077bscsh09791" not in archive.read("OEBPS/nav.xhtml").decode()

        assert "this.buffer.add(16).readPointer()" in ex.JS
        assert "page: navigationPage" in ex.JS
        assert "position.add(8).writeS64(0)" in ex.JS
        assert "last-position.reset" in ex.JS
        assert "return installURLRead('load_job.ReadRawData', target);" in ex.JS
        assert "installZipBookDump" in ex.JS
        assert "ocf-chunk" in ex.JS
        assert "MAX_OCF" in ex.JS
        assert "dmmb.page.flush" in ex.JS
        assert "spreadComplete" in ex.JS
        assert "Date.now() - navigationLastResourceAt >= 200" in ex.JS
        assert "qt.QQuickText.setText" not in ex.JS
        assert "book-title" not in ex.JS

        shelf = Path(temporary) / "dmmbookshelf.sqlite3"
        connection = sqlite3.connect(shelf)
        try:
            connection.execute(
                "CREATE TABLE my_library (product_Id TEXT, title TEXT)"
            )
            connection.execute(
                "INSERT INTO my_library VALUES (?, ?)",
                ("k077bscsh09791", "記憶の鍵盤（新潮文庫nex）"),
            )
            connection.execute(
                "INSERT INTO my_library VALUES (?, ?)",
                ("s011akamj03006", "月刊メガストア2026年9月号"),
            )
            connection.commit()
        finally:
            connection.close()
        assert ex.bookshelf_title("k077bscsh09791", shelf) == "記憶の鍵盤（新潮文庫nex）"
        assert ex.bookshelf_title("s011akamj03006", shelf) == "月刊メガストア2026年9月号"
        assert ex.bookshelf_title("missing", shelf) is None
        assert ex.bookshelf_title("k077bscsh09791", Path(temporary) / "no.sqlite3") is None
        assert ex.publication_title(Path("k077bscsh09791.dmmr"), shelf) == (
            "記憶の鍵盤（新潮文庫nex）"
        )
        assert ex.publication_title(Path("missing.dmmb"), shelf) == "missing"
        ex.validate_navigation_coverage(307, [0, *range(307)], 306)

        parsed = ex.parse_args([str(Path("book.dmmb"))])
        assert parsed.dmmb_output == "images"
        parsed_epub = ex.parse_args(
            [str(Path("book.dmmb")), "--dmmb-output", "epub"]
        )
        assert parsed_epub.dmmb_output == "epub"

        payload = b"application/epub+zip"
        name = b"mimetype"
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        local_header = (
            b"PK\x03\x04\x14\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            + struct.pack("<I", crc)
            + struct.pack("<II", len(payload), len(payload))
            + struct.pack("<HH", len(name), 0)
            + name
            + payload
        )
        rebuilt = ex.rebuild_ocf_epub(local_header)
        with zipfile.ZipFile(io.BytesIO(rebuilt)) as archive:
            assert archive.namelist() == ["mimetype"]
            assert archive.read("mimetype") == payload
            assert archive.testzip() is None

        fixed_output = ex.export_fixed_epub(
            [
                captured(root / "fixed-startup.png", page_three, 20, -1),
                captured(root / "fixed-page-zero.png", red, 21, 0),
                captured(root / "fixed-page-one.jpg", blue, 22, 1),
                captured(root / "fixed-page-two.png", page_two, 23, 3),
            ],
            Path(temporary) / "fixed-epub" / "book.epub",
            Path("b355iakta25451.dmme"),
            page_count=3,
            initial_page=2,
            title="サンプル作品",
        )
        with zipfile.ZipFile(fixed_output) as archive:
            assert archive.namelist()[0] == "mimetype"
            assert archive.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
            assert archive.testzip() is None
            assert "dcterms:modified" in archive.read("OEBPS/content.opf").decode()
            assert 'name="cover"' in archive.read("OEBPS/content.opf").decode()
            assert 'toc="ncx"' in archive.read("OEBPS/content.opf").decode()
            assert "OEBPS/toc.ncx" in archive.namelist()
            assert len([
                name for name in archive.namelist()
                if name.startswith("OEBPS/xhtml/page-")
            ]) == 3
            assert "rendition:layout" in archive.read("OEBPS/content.opf").decode()
            assert archive.read("OEBPS/image/page-0001.png") == red
            assert archive.read("OEBPS/image/page-0002.jpg") == blue
            assert "<dc:title>サンプル作品</dc:title>" in archive.read("OEBPS/content.opf").decode()
            assert "b355iakta25451" not in archive.read("OEBPS/nav.xhtml").decode()

        dmmb_epub = ex.export_fixed_epub(
            [
                captured(root / "dmmb-page-zero.png", red, 30, 0),
                captured(root / "dmmb-page-one.jpg", blue, 31, 1),
            ],
            Path(temporary) / "dmmb-epub" / "s011akamj03006.epub",
            Path("s011akamj03006.dmmb"),
            page_count=2,
            initial_page=0,
            title="月刊メガストア2026年9月号",
        )
        with zipfile.ZipFile(dmmb_epub) as archive:
            assert archive.namelist()[0] == "mimetype"
            assert archive.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
            assert archive.testzip() is None
            assert archive.read("OEBPS/image/page-0001.png") == red
            assert archive.read("OEBPS/image/page-0002.jpg") == blue
            assert "rendition:layout" in archive.read("OEBPS/content.opf").decode()
            assert "<dc:title>月刊メガストア2026年9月号</dc:title>" in archive.read("OEBPS/content.opf").decode()
            assert "s011akamj03006" not in archive.read("OEBPS/nav.xhtml").decode()

    print("all checks passed")


if __name__ == "__main__":
    main()
