# dmme-export

Capture content from a licensed DMMbookviewer session without keyboard or mouse
input. The exporter attaches while the viewer is suspended, hooks decrypted
resource reads, writes resource data asynchronously, and produces an output
for the book type.

## Outputs

- `.dmmb` produces `page_001.<format>`, `page_002.<format>`, and so on in
  forward logical-page order. The suffix is detected from the captured image;
  JPEG remains `.jpg`, PNG remains `.png`, and other supported formats keep
  their format.
- `.dmme` produces a fixed-layout `<book-name>.epub` built from the captured
  page images.
- `.dmmr` produces a reflowable `<book-name>.epub` built from captured XHTML,
  CSS, image, font, and other book resources.

Captured image and document payloads are copied byte-for-byte. Pillow is used
only to inspect image format and dimensions. It never saves or converts captured
images. EPUB image entries may use ZIP DEFLATE, which is lossless container
compression and does not re-encode the image.

## Requirements

- Windows 10 or 11, 64-bit
- A licensed, viewer-readable `.dmmb`, `.dmme`, or `.dmmr` book
- An installed `DMMbookviewer.exe`
- `uv`
- Docker Desktop for the self-check

## Install

Run from this repository directory:

```bat
uv venv .venv
uv pip install --python .venv\Scripts\python.exe frida-tools pillow
```

## Export

```bat
.venv\Scripts\python.exe export_dmme.py ^
  C:\path\book.dmmr ^
  C:\path\output
```

The output directory is optional. The default is `dump\<book-name>`.

For `.dmmb`, pages are files in the output directory. For `.dmme` and `.dmmr`,
the generated EPUB is:

```text
output\<book-name>.epub
```

The viewer is resolved in this order:

1. `--viewer PATH` or the `DMM_VIEWER` environment variable
2. The Windows `.dmme`/`.dmmb`/`.dmmr` file association when it points to
   `DMMbookviewer.exe`
3. `DMM\DMMbookviewer\DMMbookviewer.exe` under `Program Files`, `Program
   Files (x86)`, or `LOCALAPPDATA`

Use `--viewer PATH` for an unregistered installation.

## Capture

All three formats may reopen at the viewer's saved position. On the pinned
viewer build, a pre-navigation hook changes the returned saved position to
spine item `0` with an empty CFI in memory; it does not modify the Reader's
SQLite database. The default capture then attaches before resume, requests
logical page `0`, and traverses forward through `pageCount - 1`. If the
position-reset hook cannot be installed, default capture fails rather than
silently using a restored position.

For `.dmmb`, navigation coverage and the number of page-sized image resources
must match the viewer's logical page count. Identical image bytes on different
logical pages are retained.

For `.dmme`, the viewer's fixed-layout page count is checked against the page
images. The exporter removes images observed before a restored nonzero starting
position and creates one fixed-layout XHTML wrapper per page. A dual-page image
callback is not used as the page-order source.

For `.dmmr`, `pageCount` is the Reader's rendered pagination count, not the
number of XHTML spine documents. The exporter validates the full logical-page
traversal before packaging resources from the `cjh://.../item/...` protocol.
Resource request order can differ from the source publication spine when the
Reader preloads or restores a page, so the generated spine is an ordering of
captured resources, not a recovery of the source OPF spine.

The generated EPUB is a new container. It creates its own metadata, OPF,
spine, navigation, and XHTML wrappers; it does not restore the publication's
original OPF metadata or spine. Captured XHTML, CSS, images, fonts, and other
payloads remain in their captured relative paths.

## EPUB compatibility

Generated EPUB files contain:

- `mimetype` as the first, uncompressed ZIP entry
- `META-INF/container.xml`
- `OEBPS/content.opf`
- EPUB 3 `nav.xhtml`
- EPUB 2-compatible `toc.ncx` and `spine toc="ncx"`
- ZIP directory entries for `META-INF`, `OEBPS`, and resource parents
- cover metadata when a cover resource or first fixed-layout page is present

## Viewer compatibility

The internal `load_job.ReadRawData` hook uses a pinned RVA when the viewer
SHA-256 is:

```text
edfac9ac051fdb6726dcc77168d661f546c062e64b3e05af405f2b2bf71cfd5f
```

For a different SHA-256, executable ranges are scanned for this signature:

```text
45 89 01 48 8B 89 18 01 00 00 4D 8B C1 E9
```

The relative displacement after `E9` is not part of the match. Exactly one
match must resolve to executable code in `DMMbookviewer.exe`.

The saved-position loader uses RVA `0x40070` on the pinned build. For a
non-pinned build, executable ranges are scanned for this function-start
signature:

```text
48 8B C4 48 89 48 08 56 57 41 56 48 83 EC 60
48 C7 40 C0 FE FF FF FF 48 89 58 10 48 89 68 18
48 8B DA 48 8B F9 33 ED 89 68 B8 89 29 48 C7 41 08
FF FF FF FF 48 C7 41 28 0F 00 00 00 48 89 69 20 40
88 69 10 89 69 30
```

The position signature must resolve exactly once before default traversal can
continue. The hook changes only the returned in-memory `item_index` and CFI.

## Options

```text
--viewer PATH          Use a specific DMMbookviewer.exe.
--settle-seconds N     Wait for final resource activity to settle (5).
--timeout-seconds N    Abort capture after N seconds (240).
--navigation-wait-ms N Extra delay after each page change (0).
--keep-resources       Keep OUT\_resources after a successful export.
--no-traverse          Disable page navigation for diagnostic resource capture;
                       the resulting export may be incomplete.
```

## Docker self-check

Run this from the repository directory in Git Bash. It creates the Python
environment inside an ephemeral container, runs the byte-preservation and EPUB
checks, and compiles the Python sources without writing to the repository.

```sh
MSYS_NO_PATHCONV=1 docker run --rm \
  --mount type=bind,src="$PWD",dst=/repo,readonly \
  ghcr.io/astral-sh/uv:python3.11-bookworm-slim \
  sh -lc 'uv venv /tmp/dmme-venv && \
    uv pip install --python /tmp/dmme-venv/bin/python pillow && \
    PYTHONDONTWRITEBYTECODE=1 /tmp/dmme-venv/bin/python -B /repo/test_check.py && \
    /tmp/dmme-venv/bin/python -B -c "from pathlib import Path; [compile(p.read_text(), str(p), \"exec\") for p in (Path(\"/repo/export_dmme.py\"), Path(\"/repo/test_check.py\"))]; print(\"compile=ok\")"'
```
