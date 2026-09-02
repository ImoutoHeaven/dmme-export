# dmme-export

Export content from a licensed DMMbookviewer session. The command opens the book
without keyboard or mouse input, attaches while the viewer is suspended, and
writes captured data on a worker thread.

Output depends on the book type:

- `.dmmb` is image content and is exported in forward logical-page order. Each
  page keeps its detected original image extension, such as `.jpg`, `.png`, or
  `.webp`.
- `.dmme` is fixed-layout EPUB content and is rebuilt as
  `<book-name>.epub`.
- `.dmmr` is reflowable EPUB content, normally used for novels and other
  text-heavy books, and is rebuilt as `<book-name>.epub`.

## Requirements

- Windows 10/11, 64-bit
- A licensed installation of `DMMbookviewer.exe`
- A viewer-readable `.dmmb`, `.dmme`, or `.dmmr` book
- [`uv`](https://docs.astral.sh/uv/)
- Docker Desktop for the optional self-check

## Install

Run from this directory:

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
For EPUB input, the generated file is inside the output directory:

```text
output\book.epub
```

The viewer executable is resolved in this order:

1. `--viewer PATH` or the `DMM_VIEWER` environment variable
2. The Windows `.dmme`/`.dmmb`/`.dmmr` file association when it points to
   `DMMbookviewer.exe`
3. Standard `DMM\DMMbookviewer` folders under `Program Files`,
   `Program Files (x86)`, or `LOCALAPPDATA`

Use `--viewer PATH` for an unregistered installation.

## Capture

For `.dmmb`, the exporter reads `PageCanvas.pageCount`, visits every logical
page from `0` through `pageCount - 1`, and rejects incomplete or out-of-order
coverage. Resources loaded before navigation attachment are ignored when the
viewer restored a nonzero page. Each page is copied byte-for-byte and keeps its
detected source format in the filename, for example `.jpg`, `.png`, or `.webp`.

For `.dmme`, the Reader reports the fixed-layout page count. The exporter
traverses all logical pages, discards the image painted from the restored
startup position, and packages the remaining original page images as fixed-
layout XHTML/EPUB resources. The output keeps the page order even when a dual
page callback labels both images with one page number.

For `.dmmr`, the exporter captures decrypted resources from the Reader's
`cjh://.../item/...` resource protocol. XHTML, CSS, images, fonts, and other
requested assets keep their relative paths. The generated EPUB contains a
standard `mimetype`, `META-INF/container.xml`, OPF package document, navigation
document, and a spine in resource order.

For all three paths, captured image bytes are copied byte-for-byte. EPUB
metadata, XHTML wrappers, and ZIP container records are generated as required
by EPUB; ZIP DEFLATE is lossless and does not re-encode the image. Pillow is used
only to inspect image format and dimensions, never to save or convert captured
images.

Read callbacks use the `IOBuffer` data pointer and treat the reported byte
count, not the method's boolean return value, as the EOF signal. Multiple reads
for one resource are merged before the resource is written.

## Viewer compatibility

The internal `load_job.ReadRawData` hook uses the pinned RVA when the viewer
SHA-256 is:

```text
edfac9ac051fdb6726dcc77168d661f546c062e64b3e05af405f2b2bf71cfd5f
```

When the SHA-256 differs, executable ranges are scanned for this signature:

```text
45 89 01 48 8B 89 18 01 00 00 4D 8B C1 E9
```

The `E9` relative displacement is excluded. The scan must find exactly one
valid match whose jump target remains executable code in
`DMMbookviewer.exe`. Otherwise the viewer version is unsupported until its
signature is updated.

## Options

```text
--viewer PATH          Use a specific DMMbookviewer.exe.
--settle-seconds N     Wait for final resource activity to settle (5).
--timeout-seconds N    Abort capture after N seconds (240).
--navigation-wait-ms N Extra delay after each .dmmb/.dmme page change (0).
--keep-resources       Keep OUT\_resources after a successful export.
--no-traverse          Disable .dmmb/.dmme page navigation; resource-only EPUB
                       capture remains available for .dmmr.
```

## Docker self-check

This checks asynchronous resource writing, byte-preserving image handling,
logical-page filtering, URL resource merging, and EPUB assembly without starting
Windows or the viewer. Run it from the repository directory in Git Bash:

```sh
MSYS_NO_PATHCONV=1 docker run --rm \
  --mount type=bind,src="$PWD",dst=/repo,readonly \
  ghcr.io/astral-sh/uv:python3.11-bookworm-slim \
  sh -lc 'uv venv /tmp/dmme-venv && uv pip install --python /tmp/dmme-venv/bin/python pillow && PYTHONDONTWRITEBYTECODE=1 /tmp/dmme-venv/bin/python -B /repo/test_check.py'
```
