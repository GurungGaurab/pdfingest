# pdfingest

Turn PDFs into clean Markdown, a full JSON document and JSONL chunks ready for embedding, including the text inside embedded images.

Built on [Docling](https://github.com/docling-project/docling). Docling extracts the PDF's text, layout and tables. pdfingest adds three things on top:

- it finds the images actually stored in the PDF
- it reads the text inside them, with OCR first and Gemini only when the OCR looks poor
- it puts that text back into the document, so it shows up in the Markdown and the chunks

## Install

Python 3.10 or newer.

```bash
git clone <this repo>
cd pdfingest
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `pdfingest` command. It works whenever the virtual environment is active. The first run downloads Docling's layout and table models.

## Usage

Every PDF in `input/`, results in `output/`:

```bash
pdfingest
```

Chosen files and folders, results in `out/`:

```bash
pdfingest report.pdf docs/ -o out
```

Also read the text inside images (macOS):

```bash
pdfingest report.pdf --ocr
```

Send images the OCR reads badly to Gemini:

```bash
pdfingest report.pdf --ocr --gemini
```

`python pdfingest.py` works the same way without installing.

| Option | What it does |
|---|---|
| `inputs` | PDF files or folders. Folders are not searched recursively. Default: `input` |
| `-o, --output` | Output folder. Default: `output` |
| `--ocr` | Read text inside images with Apple's OCR (ocrmac). macOS only |
| `--gemini` | Send images whose OCR fails the checks to Gemini. Without `--ocr`, every image is sent |
| `--gemini-model` | Default: `gemini-3.5-flash-lite` |
| `--pages` | Only these pages, e.g. `5` or `1-8` |
| `--no-images` | Do not save embedded images as PNG |
| `--furniture` | Keep page headers, footers and page numbers in the Markdown and chunks |
| `--force` | Process every PDF again, even if nothing changed |
| `--table-mode` | `accurate` (default) or `fast` |

A PDF is skipped when it was already processed with exactly the same settings: OCR, Gemini model and prompt, pages, table mode, `--furniture`, `--no-images`, the quality thresholds and the package versions. Change any of them, or delete any of its output files (including the PNG and `.txt` files), and it is processed again, with its old outputs replaced.

The exit code is 0 when every PDF succeeded or was skipped, and 1 otherwise. A PDF whose images could not all be read (e.g. Gemini was busy or answered with nothing, or OCR raised an error and there was no Gemini to fall back on) is marked `incomplete`. It is not cached, so the next run tries it again. Reprocessing a PDF first removes its old entry from the manifest, so a failed or incomplete rerun is never skipped later.

An image whose OCR result was rejected, with no `--gemini` to fall back on, is not an error: running again with the same settings would give the same result. It is listed under `unread_images` in `report.json` and in the terminal summary. Add `--gemini` and the PDF is processed again.

For Gemini, get a key in [Google AI Studio](https://aistudio.google.com/) and put it in a `.env` file in the folder you run from (or next to `pdfingest.py`):

```
GEMINI_API_KEY=your-key
```

`.env` is in `.gitignore`. Never commit it.

## Output

Each file is named after the PDF plus the first 8 characters of its SHA-256, so a changed file with the same name gets new outputs and the old ones stay.

| File | Contents |
|---|---|
| `<name>.md` | Markdown with YAML front matter. Each picture is marked with a comment, and text read from it follows the comment |
| `<name>.jsonl` | One chunk per line, ready for embedding |
| `<name>.json` | The full DoclingDocument: positions, pages, tables, and all image text as picture descriptions |
| `<name>_p<N>_stored_<i>.png` | Each embedded image |
| `<name>_p<N>_stored_<i>.txt` | Text read from that image, if any |
| `manifest.json` | SHA-256 -> result and settings. Files already processed with the same settings are skipped |
| `report.json` | Settings, package versions and one record per PDF: text check, images, OCR scores |

Front matter:

```yaml
---
source: "report.pdf"
sha256: "3a4c58cf…"
pages: 55
title: "Annual Report"
author: "Jane Doe"
created: "2026-05-12"
processed_at: "2026-10-16T10:41:39"
tool: "pdfingest"
---
```

A JSONL record:

```json
{"id": "3a4c58cf-15", "source": "report.pdf", "sha256": "3a4c58cf…", "chunk_index": 15,
 "text": "Results\nQuarterly sales by region\n…", "headings": ["Results"],
 "pages": [6], "from_image": true, "image_text_by": "ocr"}
```

**Headers and footers.** Docling tags page headers, footers and page numbers as *furniture*. By default they are left out of the Markdown and the chunks, because a header repeated on every page would land in every chunk. They stay in the `.json`. Use `--furniture` to keep them, e.g. when a footer holds something useful such as a version date. Text read from an image in a header or footer is not lost: without `--furniture` it gets a picture of its own in the body.

Chunks come from Docling's `HierarchicalChunker`: one chunk per paragraph, list, table or picture. `text` starts with the headings the chunk sits under, so it makes sense on its own. `from_image` marks chunks whose text was read from an image.

## How image text is read

1. **Find the images.** The images come from the PDF itself (Docling's `bitmap_resources`), not from the layout model's guesses. The layout model also marks logos, icons and drawn shapes as pictures, and may split one image into several. Each layout picture is labelled `embedded image` if it sits inside a stored image, or `picture not matched` if it doesn't.
2. **Skip tiny images.** Images under 2% of the page (icons, bullets) are not read.
3. **OCR (with `--ocr`).** pdfingest runs Apple's OCR on each stored image itself, enlarging small images first. Docling's own OCR is turned off, because it would put OCR text into the document before it could be checked. The OCR result is kept only if:
   - its average confidence is at least 0.95, and
   - at least 95% of its words are real words: common English (wordfreq) or words that appear in the PDF's own text.
4. **Gemini (with `--gemini`).** Images that fail the OCR checks, or where OCR raised an error, go to Gemini. The prompt asks for self-contained lines (full dates, row and column headings) and `NO_TEXT` for photos and logos.
5. **Put the text back.** The text becomes the description of the picture inside the image. Docling's Markdown export and chunker both include it. If the layout model found no picture there, pdfingest adds one at the image's page and position, so the text is in the JSON, the Markdown and the chunks either way.

Only text that passed the checks (or came from Gemini) ever enters the document. Rejected OCR text is kept only in `report.json`, as scores.

`report.json` keeps every image's OCR confidence, real-word share, check result and Gemini error, so you can see why each image was or wasn't read.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

`pytest -q` runs every test, including the real Docling conversions. For only the quick checks, run `pytest -q -m "not docling"`.

The tests make their own small PDFs with fpdf2. They cover:

- the outputs and the front matter
- `--no-images`
- skipping a file already processed
- a changed file getting new outputs
- a broken PDF not stopping the run
- a missing Gemini key being reported rather than crashing
- a failed image being retried on the next run
- a failed rerun (e.g. `--force` while Gemini is down) not being skipped afterwards
- an OCR error falling back to Gemini
- an empty Gemini answer being retried
- rejected OCR without Gemini being reported, and read once `--gemini` is added
- a deleted image file making the PDF be processed again
- changed settings (e.g. `--pages`) not being skipped, and `--force`
- rejected OCR text never reaching the outputs
- image text with no layout picture getting a picture in reading order
- Apple OCR reading a test image (macOS only)

## Limits

- **OCR is macOS only** (ocrmac). On other systems, use `--gemini` alone.
- **Gemini is optional and external.** The free tier is not available in every country, and Google may use free-tier data to improve its products. Don't send confidential PDFs on the free tier. Gemini's output is not checked automatically.
- **Image text can be duplicated** when the same words are also in the PDF's text layer, e.g. real text printed over a picture.
- **Scanned PDFs** (one image per page, no text layer) have not been tested. The page's text would come in as one block under its picture, without headings or paragraphs. pdfingest is built for PDFs with a text layer.
- **Furniture tagging is Docling's guess.** A real line of text tagged as a header or footer is left out without `--furniture`.
- **Text check.** `percent_read` in `report.json` only measures how many of the text layer's words Docling found. It says nothing about reading order, table structure or image text.
