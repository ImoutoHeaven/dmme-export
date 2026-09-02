# dmme-export

Export pages from a licensed DMMbookviewer session as PNG files. The command
opens the book directly, attaches before the viewer resumes, captures decrypted
image bytes at the Qt image boundary, and writes resources on a worker thread.
It does not use keyboard or mouse input.

## Requirements

- Windows 10/11, 64-bit
- A licensed installation of `DMMbookviewer.exe`
- A viewer-readable `.dmme` or `.dmmb` book
- [`uv`](https://docs.astral.sh/uv/)
- Docker Desktop only for the optional self-check

## Install

Run from this directory:

```bat
uv venv .venv
uv pip install --python .venv\Scripts\python.exe frida-tools pillow
```

## Export

```bat
.venv\Scripts\python.exe export_dmme.py ^
  C:\path\book.dmmb ^
  C:\path\output
```

If the output directory is omitted, files go to `dump\<book-name>`. Pages are
named `page_001.png`, `page_002.png`, and so on in forward logical-page order.
For `.dmmb`, `pageCount` is the number of logical page images, not the number
of visible page halves.

The installed executable is resolved in this order:

1. `--viewer PATH` or `DMM_VIEWER`
2. The Windows `.dmme`/`.dmmb` file association, when it points to
   `DMMbookviewer.exe`
3. Standard `DMM\DMMbookviewer` folders under `Program Files`,
   `Program Files (x86)`, or `LOCALAPPDATA`

Use `--viewer PATH` for an unregistered non-standard installation.

## Correctness

The exporter reads `PageCanvas.pageCount`, visits pages `0..pageCount-1`, and
fails if navigation or captured page coverage is incomplete. If the viewer
restores a previous position, unassigned startup resources are discarded when
the initial page is not zero. Temporary resources remain under
`OUT\_resources` after a failure for diagnosis; incomplete page files are
removed.

Retries are deduplicated by `(logical_page, sha256)`. The same image on two
different logical pages is retained.

## Viewer compatibility

The internal hook uses the pinned RVA when the viewer SHA-256 is:

```text
edfac9ac051fdb6726dcc77168d661f546c062e64b3e05af405f2b2bf71cfd5f
```

When the SHA-256 differs, the exporter scans executable ranges for this
signature:

```text
45 89 01 48 8B 89 18 01 00 00 4D 8B C1 E9
```

The `E9` relative displacement is excluded. The scan must find exactly one
valid match, and its jump target must remain executable code in
`DMMbookviewer.exe`. If it fails, the viewer version is unsupported until its
hook signature is updated.

## Options

```text
--viewer PATH          Use a specific DMMbookviewer.exe.
--settle-seconds N     Wait for final resource activity to settle (5).
--timeout-seconds N    Abort navigation after N seconds (240).
--navigation-wait-ms N Extra delay after each page change (0).
--keep-resources       Keep OUT\_resources after a successful export.
--no-traverse          Capture only; not a complete book export.
```

## Docker self-check

This checks image handling, filtering, asynchronous writing, and logical-page
deduplication without starting Windows or the viewer. Run it from the repo
directory in Git Bash:

```sh
MSYS_NO_PATHCONV=1 docker run --rm \
  --mount type=bind,src="$PWD",dst=/repo,readonly \
  ghcr.io/astral-sh/uv:python3.11-bookworm-slim \
  sh -lc 'uv venv /tmp/dmme-venv && uv pip install --python /tmp/dmme-venv/bin/python pillow && PYTHONDONTWRITEBYTECODE=1 /tmp/dmme-venv/bin/python -B /repo/test_check.py'
```
