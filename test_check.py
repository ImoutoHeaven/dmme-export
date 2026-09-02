"""Self-check for the resource capture pipeline (no Windows viewer needed)."""
from __future__ import annotations

import io
import os
import sys
import tempfile
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
            "page_001.png", "page_002.png", "page_003.png", "page_004.png"
        ]
        assert pages[0].read_bytes() == red, "PNG resources should stay byte-for-byte lossless"
        assert Image.open(pages[1]).size == (1000, 800), "JPEG resources must decode to PNG"
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

    print("all checks passed")


if __name__ == "__main__":
    main()
