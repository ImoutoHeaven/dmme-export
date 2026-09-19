# dmme-export

Capture content from a licensed DMMbookviewer session. The exporter attaches
while the viewer is suspended, installs hooks, and writes an output for the
book type.

## Outputs

- `.dmmb` produces `page_001.<format>`, `page_002.<format>`, and so on in
  forward logical-page order. The suffix is detected from the captured image;
  JPEG remains `.jpg`, PNG remains `.png`, and other supported formats keep
  their format. `--dmmb-output epub` packs those pages as a fixed-layout
  `<book-name>.epub`.
- `.dmme` and `.dmmr` produce `<book-name>.epub` from the viewer's in-memory
  OCF ZIP (`zip_book`): original `mimetype`, `META-INF/container.xml`,
  `item/standard.opf`, publication resources, and the original ZIP central
  directory. The exporter copies the decrypted ZIP through its EOCD byte-for-byte
  and removes only the viewer/container trailer after the EOCD.

For `.dmmb --dmmb-output epub`, EPUB `dc:title` is `my_library.title` for
`product_Id` (the filename stem) in
`%APPDATA%\DMM\DMMbookviewer2\dmmbookshelf.sqlite3`, or the filename stem.
A `.dmme` or `.dmmr` EPUB keeps the dumped package metadata (`dc:title`, spine,
manifest).

Captured `.dmmb` image payloads are copied byte-for-byte. Pillow inspects image
format and dimensions. Generated fixed-layout EPUB image entries may use ZIP
DEFLATE as lossless container compression.

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

For `.dmmb --dmmb-output images` (default), pages are files in the output
directory. For `.dmme`, `.dmmr`, and `.dmmb --dmmb-output epub`, the EPUB is:

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

The exporter attaches before the viewer resumes.

On the pinned viewer build, a pre-navigation hook sets the returned in-memory
saved position to spine item `0` with an empty CFI. Default `.dmmb` capture
collects the opening spread from flush, then `pageJump`s from `1` through
`pageCount - 1`. That capture path installs the position-reset hook first.

For `.dmmb`, the viewer decrypts the current spread into page objects. Capture
hooks `FUN_140021b80` (fill) and `FUN_140020ed0` (flush) at RVAs `0x21B80` and
`0x20ED0`. A flush buffers the plaintext JPEG or PNG at `this+0xa0` without a
page number. Capture assigns pages when `PageCanvas.currentPage` equals the
pending `pageJump`, using that settled index for the spread (`current`,
`current+1` on a two-page spread). The next `pageJump` runs after that emit.
Navigation coverage and the number of page-sized image resources must match the
viewer's logical page count. Identical image bytes on different logical pages
are retained.

For `.dmme` and `.dmmr`, capture finishes when the `zip_book` dump arrives. The
pinned build dumps the decrypted OCF at RVA `0x1349E0` with `zseek(0)` and
chunked `zread` until EOF, subject to the `256 MiB` capture limit. The field
at `this+0x60+0xa8` is the ZIP central-directory offset, not the complete
stream length. The exporter locates
the valid EOCD, writes the exact ZIP prefix through that record, and excludes
the short viewer/container trailer. The publication keeps its original spine
and manifest from `item/standard.opf`.

## EPUB compatibility

A `.dmme` or `.dmmr` EPUB is the exact decrypted OCF ZIP prefix through its
original EOCD. It preserves the original central-directory metadata and keeps
the package `full-path` from `META-INF/container.xml` (typically
`item/standard.opf`).

A `.dmmb --dmmb-output epub` file is a generated container:

- `mimetype` as the first, uncompressed ZIP entry
- `META-INF/container.xml`
- `OEBPS/content.opf`
- EPUB 3 `nav.xhtml`
- EPUB 2-compatible `toc.ncx` and `spine toc="ncx"`
- ZIP directory entries for `META-INF`, `OEBPS`, and resource parents
- cover metadata when a cover resource or first fixed-layout page is present

## Viewer compatibility

Pinned `DMMbookviewer.exe` SHA-256:

```text
edfac9ac051fdb6726dcc77168d661f546c062e64b3e05af405f2b2bf71cfd5f
```

On that build the exporter uses these RVAs:

| hook | RVA |
|------|-----|
| `load_job.ReadRawData` | `0x8B340` |
| saved-position loader | `0x40070` |
| `zip_book` OCF dump (`.dmme`/`.dmmr`) | `0x1349E0` |
| `.dmmb` page fill | `0x21B80` |
| `.dmmb` page flush | `0x20ED0` |

For a different SHA-256, executable ranges of `DMMbookviewer.exe` are scanned
for `load_job.ReadRawData` and the saved-position loader. `.dmme`/`.dmmr` OCF
dump uses the pinned `zip_book` RVA, so it runs on the pinned SHA-256.

`load_job.ReadRawData` signature (the relative displacement after `E9` is
omitted). Exactly one match must resolve to executable code in
`DMMbookviewer.exe`:

```text
45 89 01 48 8B 89 18 01 00 00 4D 8B C1 E9
```

Saved-position loader function-start signature. Exactly one match is required
before default `.dmmb`/`.dmme` traversal continues. The hook writes the
returned in-memory `item_index` and CFI:

```text
48 8B C4 48 89 48 08 56 57 41 56 48 83 EC 60
48 C7 40 C0 FE FF FF FF 48 89 58 10 48 89 68 18
48 8B DA 48 8B F9 33 ED 89 68 B8 89 29 48 C7 41 08
FF FF FF FF 48 C7 41 28 0F 00 00 00 48 89 69 20 40
88 69 10 89 69 30
```

## Options

```text
--viewer PATH          Use a specific DMMbookviewer.exe.
--settle-seconds N     Wait for final resource activity to settle (5).
--timeout-seconds N    Abort capture after N seconds (240).
--navigation-wait-ms N Extra delay after each page change (0). Unused for `.dmmb`.
--keep-resources       Keep OUT\_resources after a successful export.
--no-traverse          Skip page navigation (diagnostic for .dmmb;
                       .dmme/.dmmr still dump zip_book).
--dmmb-output images|epub
                       For .dmmb: write page images (default) or a fixed-layout EPUB.
```

## Docker self-check

Run this from the repository directory in Git Bash. It creates the Python
environment inside an ephemeral container, runs the byte-preservation, EPUB,
and exact OCF ZIP checks, and compiles the Python sources without writing to the
repository.

```sh
MSYS_NO_PATHCONV=1 docker run --rm \
  --mount type=bind,src="$PWD",dst=/repo,readonly \
  ghcr.io/astral-sh/uv:python3.11-bookworm-slim \
  sh -lc 'uv venv /tmp/dmme-venv && \
    uv pip install --python /tmp/dmme-venv/bin/python pillow && \
    PYTHONDONTWRITEBYTECODE=1 /tmp/dmme-venv/bin/python -B /repo/test_check.py && \
    /tmp/dmme-venv/bin/python -B -c "from pathlib import Path; [compile(p.read_text(), str(p), \"exec\") for p in (Path(\"/repo/export_dmme.py\"), Path(\"/repo/test_check.py\"))]; print(\"compile=ok\")"'
```
