# kckfxgen

**Kindle Comics KFX Gen**

**[中文版 / Chinese](README_zh.md)**

Convert **EPUB** files or **comic archives** (ZIP / CBZ / RAR / CBR) to Kindle fixed-layout comic **KFX** **without Kindle Previewer**. The tool builds a Kindle Create–style **`.kpf`** (ZIP with `book.kdf`), then repackages it into a single **`.kfx`** using the same **CONT** pipeline as [calibre-kfx-output](https://github.com/dstaley/calibre-kfx-output)’s [`kfx_container.py`](https://github.com/dstaley/calibre-kfx-output/blob/master/kfxlib/kfx_container.py) (`KpfContainer` → `KpfBook` fix-up → `KfxContainer.serialize`). The implementation is included in-tree as `kckfxgen/kfxlib/` (GPL v3, copyright John Howell et al.; see `kckfxgen/kfxlib/COPYING`).

Compared with MOBI-based comics, KFX comics usually **turn pages and load faster** with large, high-resolution artwork, and are **less likely to freeze** the device. Fixed layout keeps **pages tidy** (e.g. centered, fewer stray margins). When building the KDF, each page gets an **`orientation`** field (**portrait** / **landscape**) from the image dimensions **after applying EXIF orientation**, and book metadata declares support for **both** portrait and landscape so mixed vertical and horizontal pages behave correctly.

Each successful run writes one **`{sanitized_stem}_{random}.kfx`** into the chosen output directory. Intermediate **`.kpf`** is kept only in a temp folder and removed afterward.

## Requirements

- **Python 3.10+**
- **Required (pip):** `amazon-ion`, `lxml`, `pillow`
- **Python stdlib** `sqlite3` backed by **SQLite ≥ 3.8.2** (required for `WITHOUT ROWID` in `book.kdf`; current CPython builds on Windows/macOS/Linux satisfy this)
- **Optional**
  - **Double-page split** (`--split-spreads`): `numpy`, `pillow` (detect **blank centre gutter** before centre-splitting wide pages; parallel)
  - **RAR / CBR:** `rarfile` plus a working **UnRAR** backend (extraction fails otherwise)

```bash
pip install amazon-ion lxml pillow
# For --split-spreads:
pip install numpy pillow
# For .rar / .cbr:
pip install rarfile
```

## Supported inputs

| Kind | Extensions | Notes |
|------|------------|--------|
| EPUB | `.epub` | Images collected from spine / manifest (unchanged behaviour) |
| Comic archive | `.zip`, `.cbz` | Extracted with the stdlib; images ordered by **natural path sort** |
| Comic archive | `.rar`, `.cbr` | Requires `rarfile` + UnRAR |

`path` may be **any single file above**, or a **directory** (recursive scan; multiple files run with a thread pool).

## Command-line options

| Option | Description |
|--------|-------------|
| `path` | Input: one file (`.epub` / `.zip` / `.cbz` / `.rar` / `.cbr`) or a directory |
| `-o` / `--output` | **Single file only:** KFX output directory (default: same folder as the input) |
| `--output-dir` | **Batch:** write all `.kfx` files into one directory (name pattern above); cannot be combined with `-o` |
| `-j` / `--jobs` | Worker threads (default about `min(8, CPU count)`) |
| `-d` / `--debug` | DEBUG logging |
| `--split-spreads` | Wide images (width ≥ height×1.25): split at the **horizontal centre** only if a **blank binding gutter** is detected; otherwise keep the full image (**off** by default) |
| `--split-page-order` | With `--split-spreads`: `right-left` (default, right half then left) or `left-right` |
| `--rotate-landscape-90` | Before writing KDF: rotate **landscape** pages (width > height) **90° counter‑clockwise** so they display as portrait; portrait pages unchanged |
| `--page-progression` | KPF / KDF reading direction: `ltr` (default, left‑to‑right) or `rtl` (right‑to‑left, typical for manga). Sets `book.kcb` `book_reading_direction` and `book.kdf` `document_data.direction` |
| `--title` | Override **title** (see below for comic archives; for EPUB overrides OPF `dc:title`) |
| `--author` | Override **author** (comic archives: parsed from filename; EPUB overrides `dc:creator`) |
| `--publisher` | Override **publisher** (comic archives: parsed from filename; EPUB overrides `dc:publisher`) |

### Comic archive filenames → metadata (ZIP / CBZ / RAR / CBR)

Archives **without OPF** get `title` / `author` / `publisher` from the **stem** (filename without extension):

1. If the stem ends with **`[…]`**, the inside text is **publisher** and that suffix is removed; otherwise if it ends with **`(…)`**, that inner text is **publisher**.
2. On what remains, if there is **` — `**, **` – `**, or **` - `** (hyphen/dash **with spaces on both sides**), split on the **first** occurrence: left = **title**, right = **author**.
3. If there is no such separator, the whole remainder is **title**; author and publisher stay empty unless taken from brackets in step 1.

Example: `Attack on Titan - Hajime Isayama [Kodansha].cbz` → title=`Attack on Titan`, author=`Hajime Isayama`, publisher=`Kodansha`.

When **batch**-processing a directory, `--title` / `--author` / `--publisher` apply **the same values to every file** found in that run (handy for uniform test batches; use per-book runs or rely on filename parsing when each book differs).

## Usage examples

```bash
# Single EPUB → KFX next to the EPUB (default)
python main.py path/to/comic.epub

# Single CBZ / ZIP (title/author/publisher parsed from filename; see above)
python main.py path/to/comic.cbz

# Manual metadata (comic archive or EPUB)
python main.py path/to/comic.zip --title "Custom title" --author "Some Author" --publisher "Some Press"

# Single EPUB → KFX into a chosen directory
python main.py path/to/comic.epub -o output_dir

# Directory: recurse and convert all supported files (thread pool); default KFX next to each input
python main.py path/to/folder

# Batch: all KFX into one directory
python main.py path/to/folder --output-dir output_dir

# Thread count
python main.py path/to/folder -j 4

# Wide spreads: centre split + right-then-left (common for manga)
python main.py path/to/comic.epub --split-spreads

# After split, left page then right
python main.py path/to/comic.zip --split-spreads --split-page-order left-right

# Right-to-left page progression (manga-style; KCB + KDF)
python main.py path/to/manga.cbz --page-progression rtl

# Debug log
python main.py path/to/comic.epub -d
```

### Python API

`kckfxgen.pipeline.convert_to_kfx`, `epub_to_kpf`, `comic_archive_to_kpf`, and `convert_epub_to_kfx` accept the same behaviour via **`page_progression="ltr"`** (default) or **`page_progression="rtl"`**.

Run these from the **repository root**.

## Standalone spread detector (images only)

`kckfxgen/spread_split.py` can detect and split loose PNG/JPEG files without building KFX:

```bash
python -m kckfxgen.spread_split some.png -o output_dir
# Or, from repo root (so `kckfxgen` imports):
python src/comic_spread_split.py some.png --dry-run
```

Regression test for split logic: `python test/run_spread_split_tests.py` (expects sample images under `test/` named with `cut*` / `nocut*` prefixes).
