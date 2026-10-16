"""pdfingest: turn PDFs into Markdown, JSON and JSONL chunks, including the text inside embedded images.

Run:     python pdfingest.py                      every PDF in input/ -> output/
         python pdfingest.py a.pdf docs/ -o out   chosen files and folders
         python pdfingest.py --help               all options

Output (<name> = file name + first 8 characters of its SHA-256):
  <name>.md                    Markdown with front matter; text read from an image sits under that picture
  <name>.jsonl                 one chunk per line: text + headings, pages and source (ready for embedding)
  <name>.json                  full DoclingDocument (positions, pages, tables, image text)
  <name>_p<N>_stored_<i>.png   embedded images (unless --no-images)
  <name>_p<N>_stored_<i>.txt   text read from that image, if any
  manifest.json                sha256 -> result; files already processed with the same settings are skipped
  report.json                  settings, versions and one record per PDF
"""

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pypdfium2 as pdfium
from docling.datamodel.backend_options import ThreadedDoclingParseBackendOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode, TableStructureOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.transforms.chunker import HierarchicalChunker
from docling_core.transforms.chunker.hierarchical_chunker import ChunkingDocSerializer, ChunkingSerializerProvider
from docling_core.types.doc import (BoundingBox, ContentLayer, CoordOrigin, DescriptionMetaField, DocItem,
                                    PictureItem, PictureMeta, ProvenanceItem)


# Defaults. The command-line options change these (python pdfingest.py --help).
OUT_DIR = Path("output")
DO_OCR = False                          # True = Apple OCR (ocrmac, macOS only) reads text inside images
USE_GEMINI = False                      # True = images that fail the OCR checks go to Gemini (needs GEMINI_API_KEY)
GEMINI_MODEL = "gemini-3.5-flash-lite"  # a free Flash model; check the name in AI Studio
TABLE_MODE = TableFormerMode.ACCURATE
PAGE_RANGE = None                       # e.g. (1, 8); None = whole file
SAVE_IMAGES = True                      # write each embedded image as a PNG
KEEP_FURNITURE = False                  # True = page headers, footers and page numbers also go into the Markdown and chunks
FORCE = False                           # True = process every PDF again, even if nothing changed

IMAGE_ONLY_SHARE = 0.3                  # image covers more than 30% of the page...
IMAGE_ONLY_WORDS = 30                   # ...and the page's own text layer has fewer than 30 words -> image-only page
FLAG_MISSING = 0.05                     # flag a page if more than 5% of its text-layer words are missing...
MIN_MISSING = 3                         # ...and at least 3 words are missing (avoids noise on near-empty pages)

# Reading the text inside images: OCR first, Gemini only when the OCR result looks poor
OCR_SCALE = 3                           # OCR an image at (at least) 3 pixels per PDF point, like Docling's own OCR
CONFIDENCE_MIN = 0.95                   # OCR is trusted only if its average confidence is at least this...
REAL_WORD_MIN = 0.95                    # ...and at least this share of its words are real words
REAL_WORD_ZIPF = 3.0                    # a word counts as real if it is common English (wordfreq) or appears in the PDF's own text
MIN_IMAGE_SHARE = 0.02                  # images smaller than 2% of the page are not read (icons, bullets)
GEMINI_PROMPT = (
    "Transcribe this image for a document search system. Each line you output may be read on its own, "
    "so it must make sense without the lines around it.\n"
    "\n"
    "Rules:\n"
    "- Copy all text exactly. Keep headings as headings and bullet points as bullet points.\n"
    "- Every line or table row must be self-contained: include the context needed to understand it, "
    "such as the full date (day, month and year), or the row and column headings it belongs to.\n"
    "- If the image is a table, grid, timetable or schedule, read it cell by cell using its row and column "
    "headings. Output only cells that contain content; skip empty cells.\n"
    "- Labels that only mark position (week numbers, row numbers, page numbers) are context, not content: "
    "attach them to the entries they belong to instead of listing them alone.\n"
    "- If it is a chart or diagram, give its title, then the values or labels it shows.\n"
    "- If it is a photo, logo or decoration with no useful text, reply only: NO_TEXT\n"
    "- Never add anything that is not in the image. If something cannot be read, write [unreadable]."
)

ROOT = Path(__file__).parent
WORD = re.compile(r"\w+")
PLACEHOLDER = "<!-- picture -->"        # temporary marker, replaced by a label per picture


#Used to decide if the file has already been parsed or not
def sha_256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def words(text): #word count
    return Counter(WORD.findall(html.unescape(text or "").lower()))


def content_layers():
    """Which of Docling's layers go into the Markdown and chunks. Furniture = page headers, footers, page numbers."""
    return {ContentLayer.BODY, ContentLayer.FURNITURE} if KEEP_FURNITURE else {ContentLayer.BODY}


def pages_of(item):# every page an item sits on (one paragraph can cross a page break)
    """Every page an item sits on. One paragraph can cross a page break."""
    return sorted({p.page_no for p in item.prov})


#Docling
def build_converter():
    # Docling's own OCR stays off: it would put OCR text into the document before it can be checked.
    # pdfingest runs OCR itself on each stored image (ocr_image) and only adds text that passes the checks.
    options = PdfPipelineOptions(
        do_ocr=False,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(mode=TABLE_MODE),
        generate_parsed_pages=True,     # keep bitmap_resources (embedded images) after parsing
    )
    return DocumentConverter(format_options={
        InputFormat.PDF: PdfFormatOption(
            pipeline_options=options,
            # default docling-parse backend; also decode the embedded images' pixels
            backend_options=ThreadedDoclingParseBackendOptions(include_bitmap_images=True),
        )
    })


#Embedded images
def pdf_text_layer(pdf_path):
    """The PDF's own text layer, read with pypdfium2 (so OCR text is never included).
    Returns (every word as a set, number of words per page)."""
    vocabulary = set()
    page_words = Counter()
    pdf = pdfium.PdfDocument(str(pdf_path))
    for i in range(len(pdf)):
        found = WORD.findall(pdf[i].get_textpage().get_text_range())
        vocabulary.update(w.lower() for w in found)
        page_words[i + 1] = len(found)
    pdf.close()
    return vocabulary, page_words


def real_word_share(text, vocabulary):
    """Share of words (3+ letters) that are common English or appear in the PDF's own text."""
    try:
        from wordfreq import zipf_frequency
    except ImportError:
        raise RuntimeError("--ocr needs the wordfreq package: pip install wordfreq")
    candidates = [w.lower() for w in re.findall(r"[A-Za-z]+", text) if len(w) >= 3]
    if not candidates:
        return None
    real = sum(1 for w in candidates if w in vocabulary or zipf_frequency(w, "en") >= REAL_WORD_ZIPF)
    return round(real / len(candidates), 3)


def ocr_image(image, box):
    """Apple OCR (ocrmac) on one stored image. Small images are enlarged first, to at least
    OCR_SCALE pixels per PDF point (box = where the image sits on the page, in points).
    Returns (text in reading order, average confidence, number of lines)."""
    from ocrmac import ocrmac

    scale = min(4.0, OCR_SCALE * box.width / image.width)
    if scale > 1:
        image = image.resize((round(image.width * scale), round(image.height * scale)))
    lines = ocrmac.OCR(image, recognition_level="accurate").recognize()
    # Vision gives boxes as fractions of the image, measured from the bottom left: sort top to bottom, then left to right
    lines.sort(key=lambda line: (round(1 - line[2][1] - line[2][3], 3), line[2][0]))
    text = "\n".join(line[0] for line in lines)
    confidence = round(sum(line[1] for line in lines) / len(lines), 3) if lines else None
    return text, confidence, len(lines)


def check_ocr(text, confidence, vocabulary):
    """Is the OCR result good enough to keep? Returns (ok, real-word share, reason)."""
    if not DO_OCR:
        return False, None, "OCR off"
    if not text.strip():
        return False, None, "no OCR text"
    share = real_word_share(text, vocabulary)
    if confidence is None or confidence < CONFIDENCE_MIN:
        return False, share, f"low confidence ({confidence})"
    if share is None or share < REAL_WORD_MIN:
        return False, share, f"few real words ({share})"
    return True, share, "passed"


def place_text(doc, layout_pictures, text, source):
    """Put the text read from an image into the document, as the description of the first
    layout picture inside the image that has none yet. Docling's Markdown export and the
    chunker both include picture descriptions. Pictures in a layer left out of the Markdown
    (e.g. a header logo, without --furniture) are not used, so the text is not lost there.
    Returns the picture's ref, or None."""
    by_ref = {pic.self_ref: pic for pic in doc.pictures}
    for ref in layout_pictures:
        pic = by_ref[ref]
        if pic.content_layer not in content_layers():
            continue
        if pic.meta is not None and pic.meta.description is not None:
            continue
        if pic.meta is None:
            pic.meta = PictureMeta()
        pic.meta.description = DescriptionMetaField(text=text, created_by=created_by(source))
        return ref
    return None


def add_picture(doc, page_no, box, page_height, text, source):
    """The layout model found no picture for this image: add one to the document, at the image's
    page and position, carrying the text. It goes after the last top-level item on that page that
    starts above the image (or after the previous page's last item); if there is none, before the
    first item that follows it. So the JSON, the Markdown and the chunks all get the text in roughly
    the right place. Returns the new picture's ref."""
    layers = content_layers()
    before, following = None, None
    for item, _ in doc.iterate_items(included_content_layers=layers):
        if not isinstance(item, DocItem) or not item.prov or item.parent is None or item.parent.cref != "#/body":
            continue
        first = item.prov[0]
        if first.page_no < page_no or (first.page_no == page_no
                                       and first.bbox.to_top_left_origin(page_height).t <= box.t):
            before = item
        elif following is None:
            following = item                     # first item after the image, in reading order
        if first.page_no > page_no:
            break                                # reached the next pages
    prov = ProvenanceItem(page_no=page_no, charspan=(0, 0),
                          bbox=BoundingBox(l=box.l, t=box.t, r=box.r, b=box.b, coord_origin=CoordOrigin.TOPLEFT)
                          .to_bottom_left_origin(page_height))
    if before is not None:
        picture = doc.insert_picture(sibling=before, prov=prov, after=True)
    elif following is not None:
        picture = doc.insert_picture(sibling=following, prov=prov, after=False)
    else:
        picture = doc.add_picture(prov=prov)       # the document has no other items
    picture.meta = PictureMeta(description=DescriptionMetaField(text=text, created_by=created_by(source)))
    return picture.self_ref


def created_by(source):
    return f"gemini:{GEMINI_MODEL}" if source == "gemini" else "ocrmac"


def embedded_images(result, stem, vocabulary, page_words):
    """One record per image stored in the PDF, read from bitmap_resources
    (a fact from the file, not a guess by the layout model). With OCR on, each image is
    OCR'd and the result checked; images that fail go to Gemini if USE_GEMINI is on.
    Text that is read is put into the document (place_text, or add_picture if the layout
    model found no picture there)."""
    doc = result.document
    records = []

    for page in result.pages:
        if page.parsed_page is None:
            continue
        page_no = page.page_no
        size = doc.pages[page_no].size
        page_area = size.width * size.height

        for i, bmp in enumerate(page.parsed_page.bitmap_resources):
            box = bmp.rect.to_bounding_box().to_top_left_origin(size.height)
            if box.area() <= 0:
                continue
            share = box.area() / page_area

            # Which layout-model pictures sit (mostly) inside this image?
            layout_pictures = []
            for pic in doc.pictures:
                for prov in pic.prov:
                    if prov.page_no != page_no:
                        continue
                    pic_box = prov.bbox.to_top_left_origin(size.height)
                    if pic_box.area() > 0 and box.intersection_area_with(pic_box) / pic_box.area() > 0.5:
                        layout_pictures.append(pic.self_ref)

            image = bmp.image.pil_image if bmp.image is not None else None
            if image is not None and image.mode not in ("RGB", "L"):
                image = image.convert("RGB")      # e.g. CMYK or transparent images, which PNG/OCR can't take as they are
            saved_as = None
            if image is not None and SAVE_IMAGES:
                saved_as = f"{stem}_p{page_no}_stored_{i}.png"
                image.save(OUT_DIR / saved_as)

            # Read the text inside the image: OCR if it passes the checks, otherwise Gemini
            ocr_text, ocr_confidence, ocr_lines, ocr_error = "", None, 0, None
            if DO_OCR and image is not None and share >= MIN_IMAGE_SHARE:
                try:
                    ocr_text, ocr_confidence, ocr_lines = ocr_image(image, box)
                except Exception as e:           # one image must not fail the PDF; Gemini can still read it
                    ocr_error = f"{type(e).__name__}: {e}"
            ocr_ok, word_share, check_reason = check_ocr(ocr_text, ocr_confidence, vocabulary)
            if ocr_error:
                check_reason = f"OCR failed ({ocr_error})"
            text, source, gemini_error = None, None, None
            if share < MIN_IMAGE_SHARE:
                check_reason = "image too small, not read"
            elif ocr_ok:
                text, source = ocr_text, "ocr"
            elif USE_GEMINI and image is not None:
                text, gemini_error = read_image_with_gemini(image, page_no)
                source = "gemini" if text else None

            if text and text.strip() == "NO_TEXT":
                status = "no text in image"
            elif text:
                status = f"read ({source})"
            else:
                status = "not read"

            text_file, placed_in, picture_added = None, None, False
            if status.startswith("read"):
                text_file = f"{stem}_p{page_no}_stored_{i}.txt"
                (OUT_DIR / text_file).write_text(text, encoding="utf-8")
                placed_in = place_text(doc, layout_pictures, text, source)
                if placed_in is None:
                    placed_in = add_picture(doc, page_no, box, size.height, text, source)
                    picture_added = True

            image_only = share > IMAGE_ONLY_SHARE and page_words[page_no] < IMAGE_ONLY_WORDS
            records.append({
                "page": page_no,
                "index": i,
                "bbox_tl": [round(v, 1) for v in (box.l, box.t, box.r, box.b)],
                "share_of_page": round(share, 3),
                "pixels": f"{image.width}x{image.height}" if image is not None else None,
                "image_sha256": hashlib.sha256(image.tobytes()).hexdigest() if image is not None else None,
                "type": "image_only" if image_only else "mixed",
                "page_text_layer_words": page_words[page_no],
                "layout_pictures": layout_pictures,     # [] = layout model found no picture here
                "saved_as": saved_as,
                "ocr_lines": ocr_lines,
                "ocr_confidence": ocr_confidence,
                "real_word_share": word_share,
                "ocr_check": check_reason,
                "source": source,                       # "ocr", "gemini" or None
                "ocr_error": ocr_error,
                "gemini_error": gemini_error,
                "text_file": text_file,
                "placed_in": placed_in,                 # picture that now carries the text
                "picture_added": picture_added,         # True = no layout picture fitted, pdfingest added one
                "status": status,
            })
    return records


def picture_labels(doc, images):
    """Label every layout-model picture: inside an embedded image -> embedded, else not matched
    (no embedded image found under it; it may be drawn, or span several images)."""
    embedded = {ref: img["status"] for img in images for ref in img["layout_pictures"] + [img["placed_in"]] if ref}
    labels = {}
    for pic in doc.pictures:
        pages = ", ".join(f"p{p}" for p in pages_of(pic)) or "p?"
        if pic.self_ref in embedded:
            labels[pic.self_ref] = f"embedded image ({pages}): stored in the PDF, text {embedded[pic.self_ref]}"
        else:
            labels[pic.self_ref] = f"picture not matched ({pages}): no stored image found under it"
    return labels


def labelled_markdown(doc, labels):
    """Body Markdown with each <!-- image --> replaced by its label.
    Returns (markdown, True) or, if the pictures can't be matched up, the plain
    placeholders and False."""
    layers = content_layers()
    md = doc.export_to_markdown(image_placeholder=PLACEHOLDER, included_content_layers=layers)
    order = [item.self_ref for item, _ in doc.iterate_items(included_content_layers=layers)
             if isinstance(item, PictureItem)]
    parts = md.split(PLACEHOLDER)
    if len(parts) - 1 != len(order):
        return md.replace(PLACEHOLDER, "<!-- image -->"), False
    out = parts[0]
    for ref, part in zip(order, parts[1:]):
        out += f"<!-- {labels.get(ref, 'picture')} -->" + part
    return out, True


# Front matter
def pdf_date(value):
    """PDF date 'D:20260101120000+08'00'' -> '2026-01-01'. Anything else is kept as it is."""
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", value or "")
    return f"{m[1]}-{m[2]}-{m[3]}" if m else (value or None)


def front_matter(pdf_path, file_hash, pages):
    """YAML front matter: where the Markdown came from. Values are JSON strings, which YAML reads as-is."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    meta = pdf.get_metadata_dict()
    pdf.close()
    fields = {
        "source": pdf_path.name,
        "sha256": file_hash,
        "pages": pages,
        "title": meta.get("Title"),
        "author": meta.get("Author"),
        "created": pdf_date(meta.get("CreationDate")),
        "processed_at": datetime.now().isoformat(timespec="seconds"),
        "tool": "pdfingest",
    }
    lines = [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items() if v not in (None, "")]
    return "---\n" + "\n".join(lines) + "\n---\n\n"


# JSONL chunks
class LayerSerializers(ChunkingSerializerProvider):
    """Docling's chunking serializer, with the same content layers as the Markdown."""
    def get_serializer(self, doc):
        serializer = ChunkingDocSerializer(doc=doc)
        serializer.params = serializer.params.model_copy(update={"layers": content_layers()})
        return serializer


def chunk_records(doc, pdf_name, file_hash, images):
    """One record per chunk, for embedding. A chunk is a heading's section split into its
    paragraphs, lists, tables and pictures (Docling's HierarchicalChunker); 'text' starts
    with the headings it sits under. Image text is in the chunk of its picture."""
    image_source = {img["placed_in"]: img["source"] for img in images if img["placed_in"]}
    chunker = HierarchicalChunker(serializer_provider=LayerSerializers())
    records = []

    for chunk in chunker.chunk(doc):
        items = chunk.meta.doc_items
        sources = sorted({image_source[item.self_ref] for item in items if item.self_ref in image_source})
        records.append({
            "text": chunker.contextualize(chunk),
            "headings": chunk.meta.headings or [],
            "pages": sorted({p.page_no for item in items for p in item.prov}),
            "from_image": bool(sources),
            "image_text_by": sources[0] if len(sources) == 1 else (sources or None),
        })

    return [{"id": f"{file_hash[:8]}-{i}", "source": pdf_name, "sha256": file_hash, "chunk_index": i, **r}
            for i, r in enumerate(records)]


_gemini_client = None


def gemini_key():
    """GEMINI_API_KEY from .env (in the current folder or next to this file), or from the environment.
    Accepts spaces, quotes and 'export'."""
    for env_file in (Path.cwd() / ".env", ROOT / ".env"):
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip().removeprefix("export ").strip()
                if "=" in line:
                    name, value = line.split("=", 1)
                    if name.strip() in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
                        return value.strip().strip('"').strip("'")
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def read_image_with_gemini(image, page_no):
    """Send one image to Gemini. Returns (text, error); retries when Gemini is busy (503)."""
    global _gemini_client
    from google import genai
    from google.genai import types

    if _gemini_client is None:
        key = gemini_key()
        if not key:
            return None, "no GEMINI_API_KEY in .env or the environment"
        _gemini_client = genai.Client(api_key=key)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    contents = [types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/png"), GEMINI_PROMPT]

    for attempt in range(1, 6):                          # up to 5 tries
        try:
            response = _gemini_client.models.generate_content(model=GEMINI_MODEL, contents=contents)
            text = (response.text or "").strip()
            return (text, None) if text else (None, "Gemini returned no text")   # NO_TEXT is an answer; empty is not
        except Exception as e:
            if "503" in str(e) and attempt < 5:
                print(f"   p{page_no}: Gemini busy, retrying in {10 * attempt} s")
                time.sleep(10 * attempt)
            else:
                return None, f"{type(e).__name__}: {e}"



def docling_words(doc):
    """Words Docling stored: text, list markers and table cells.
    Returns (whole document, per page). An item that crosses a page break
    counts once in the total and on every page it touches."""
    total = Counter()
    by_page = {}

    def add(counter, item):
        total.update(counter)
        for p in pages_of(item):
            by_page.setdefault(p, Counter()).update(counter)

    for item in doc.texts:
        add(words(item.text) + words(getattr(item, "marker", "")), item)

    for table in doc.tables:
        cells = Counter()
        seen = set()
        for cell in table.data.table_cells:
            key = (cell.start_row_offset_idx, cell.start_col_offset_idx)
            if key not in seen:          # a merged cell is listed once per grid position
                seen.add(key)
                cells.update(words(cell.text))
        add(cells, table)

    return total, by_page


def text_check(doc, pdf_path):
    total, by_page = docling_words(doc)
    reference = Counter()
    flagged = []

    pdf = pdfium.PdfDocument(str(pdf_path))
    first, last = PAGE_RANGE if PAGE_RANGE else (1, len(pdf))
    first, last = max(1, first), min(last, len(pdf))   # same as Docling: ignore pages that don't exist
    for page_no in range(first, last + 1):
        textpage = pdf[page_no - 1].get_textpage()
        ref = words(textpage.get_text_range())
        reference.update(ref)
        n = sum(ref.values())
        missing = sum((ref - by_page.get(page_no, Counter())).values())
        if missing >= MIN_MISSING and missing / n > FLAG_MISSING:
            flagged.append({"page": page_no, "missing": missing, "of": n})
    pdf.close()

    n = sum(reference.values())
    found = sum((reference & total).values())
    return {
        "percent_read": round(100 * found / n, 2) if n else None,
        "reference": "pypdfium2 text layer",
        "reference_words": n,
        "missing_words": n - found,
        "extra_words": sum((total - reference).values()),
        "flagged_pages": flagged,
        "note": "text inside images is not in the text layer, so it is not counted here",
    }





def cache_settings():
    """Everything that changes the outputs. A PDF is only skipped if it was processed with exactly these."""
    return {
        "ocr": DO_OCR,
        "gemini": {"model": GEMINI_MODEL, "prompt_sha256": hashlib.sha256(GEMINI_PROMPT.encode()).hexdigest()[:12]}
                  if USE_GEMINI else None,
        "pages": list(PAGE_RANGE) if PAGE_RANGE else None,
        "table_mode": TABLE_MODE.value,
        "furniture": KEEP_FURNITURE,
        "save_images": SAVE_IMAGES,
        "checks": [CONFIDENCE_MIN, REAL_WORD_MIN, REAL_WORD_ZIPF, MIN_IMAGE_SHARE, OCR_SCALE],
        "versions": package_versions(),
    }


def output_files(record):
    """Every file a processed PDF wrote."""
    files = list(record.get("outputs", {}).values())
    for img in record.get("embedded_images", []):
        files += [name for name in (img.get("saved_as"), img.get("text_file")) if name]
    return files


def save_manifest(manifest):
    """Write manifest.json in one step (temporary file, then rename), so it is never half written."""
    path = OUT_DIR / "manifest.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temporary.replace(path)


def process_pdf(converter, pdf, manifest, settings):
    file_hash = sha_256(pdf)
    known = manifest.get(file_hash)
    if known and not FORCE and known.get("settings") == settings \
            and all((OUT_DIR / name).exists() for name in output_files(known)):
        # keep the saved results, so report.json still shows them for skipped files
        return {**known, "file": pdf.name, "status": "skipped", "parsed_status": known["status"],
                "reason": f"same file already processed with the same settings as {known['file']}"}
    if known:
        # Forget the old result before touching its outputs: if this run fails or is incomplete,
        # the next run must not skip the PDF because of the old success.
        del manifest[file_hash]
        save_manifest(manifest)
        for name in output_files(known):     # remove the old outputs, so none are left over from other settings
            (OUT_DIR / name).unlink(missing_ok=True)

    start = time.perf_counter()
    result = converter.convert(pdf, page_range=PAGE_RANGE) if PAGE_RANGE else converter.convert(pdf)
    seconds = round(time.perf_counter() - start, 1)
    doc = result.document
    stem = f"{pdf.stem}_{file_hash[:8]}"   # first 8 characters of the hash: each version of a file gets its own outputs

    vocabulary, page_words = pdf_text_layer(pdf)
    layout_pictures = len(doc.pictures)
    images = embedded_images(result, stem, vocabulary, page_words)   # also puts image text into doc
    doc.save_as_json(OUT_DIR / f"{stem}.json")

    labels = picture_labels(doc, images)
    md, labels_applied = labelled_markdown(doc, labels)
    md = front_matter(pdf, file_hash, len(doc.pages)) + md
    (OUT_DIR / f"{stem}.md").write_text(md, encoding="utf-8")

    chunks = chunk_records(doc, pdf.name, file_hash, images)
    with open(OUT_DIR / f"{stem}.jsonl", "w", encoding="utf-8") as f:
        for chunk in chunks:
            f.write(json.dumps(chunk, ensure_ascii=False) + "\n")

    # An image that should have been read but failed (e.g. Gemini busy, OCR error) makes the result incomplete:
    # it is not cached, so the next run tries again.
    failed_images = [f"p{img['page']}: {img['gemini_error'] or img['ocr_error']}" for img in images
                     if img["status"] == "not read" and (img["gemini_error"] or img["ocr_error"])]
    # Read, but the result was rejected or empty, with nothing left to try (e.g. OCR failed the checks and
    # --gemini is off). Running again with the same settings gives the same result, so this is not retried;
    # it is reported instead. Adding --gemini changes the settings, so the PDF is then processed again.
    unread_images = [f"p{img['page']}: {img['ocr_check']}" for img in images
                     if img["status"] == "not read" and not (img["gemini_error"] or img["ocr_error"])
                     and img["ocr_check"] not in ("OCR off", "image too small, not read")]
    status = result.status.value
    if status == ConversionStatus.SUCCESS.value and failed_images:
        status = "incomplete"

    return {
        "file": pdf.name,
        "sha256": file_hash,
        "status": status,
        "errors": [str(e) for e in result.errors] + [f"image not read: {e}" for e in failed_images],
        "parsed_at": datetime.now().isoformat(timespec="seconds"),
        "pages": len(doc.pages),
        "seconds": seconds,
        "text_check": text_check(doc, pdf),
        "pictures": {
            "stored_images": len(images),
            "layout_model_pictures": layout_pictures,
            "labelled_embedded": sum(1 for v in labels.values() if v.startswith("embedded")),
            "labelled_not_matched": sum(1 for v in labels.values() if v.startswith("picture not matched")),
            "markdown_labels_applied": labels_applied,
            "image_text_placed": sum(1 for img in images if img["placed_in"]),
            "pictures_added": sum(1 for img in images if img["picture_added"]),
        },
        "chunks": len(chunks),
        "unread_images": unread_images,
        "embedded_images": images,
        "outputs": {"json": f"{stem}.json", "md": f"{stem}.md", "jsonl": f"{stem}.jsonl"},
        "settings": settings,
    }


def print_summary(r):
    print(f"\n=== {r['file']} ===")
    if r["status"] in ("failed", "skipped"):
        print(f"{r['status'].upper()}: {r.get('error') or r.get('reason')}")
        return
    t, p = r["text_check"], r["pictures"]
    print(f"status {r['status']} | {r['pages']} pages | {r['seconds']} s | {r['chunks']} chunks")
    if r["status"] == "incomplete":
        print("   some images could not be read; this PDF will be processed again next run")
    if r.get("unread_images"):
        print(f"   not read: {'; '.join(r['unread_images'])}"
              + ("" if USE_GEMINI else " (add --gemini to read them)"))
    print(f"text read: {t['percent_read']}% of {t['reference_words']} words "
          f"(missing {t['missing_words']}, extra {t['extra_words']}) - image text not included")
    if t["flagged_pages"]:
        print(f"   flagged pages: {[f['page'] for f in t['flagged_pages']]}")
    print(f"stored images: {p['stored_images']} | layout-model pictures: {p['layout_model_pictures']} "
          f"(embedded {p['labelled_embedded']}, not matched {p['labelled_not_matched']})")
    for img in r["embedded_images"]:
        print(f"   p{img['page']}: {img['type']:10} {img['pixels'] or 'no pixels':10} "
              f"{img['share_of_page']:.0%} of page, layout pictures inside: {len(img['layout_pictures'])}")
        print(f"      OCR confidence {img['ocr_confidence']}, real words {img['real_word_share']} "
              f"-> {img['ocr_check']} -> {img['status']}"
              + (f" ({img['gemini_error']})" if img["gemini_error"] else ""))



def package_versions():
    found = {}
    for pkg in ("pdfingest", "docling", "docling-core", "docling-parse", "pypdfium2", "ocrmac", "wordfreq", "google-genai"):
        try:
            found[pkg] = version(pkg)
        except PackageNotFoundError:
            found[pkg] = None
    return found


# Command line
def page_range(text):
    """'5' -> (5, 5), '1-8' -> (1, 8)."""
    m = re.fullmatch(r"(\d+)(?:-(\d+))?", text.strip())
    if not m or int(m[1]) < 1 or (m[2] and int(m[2]) < int(m[1])):
        raise argparse.ArgumentTypeError(f"expected a page or a range like 1-8, got {text!r}")
    return int(m[1]), int(m[2] or m[1])


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="pdfingest",
        description="Turn PDFs into Markdown, JSON and JSONL chunks, including the text inside embedded images.")
    parser.add_argument("inputs", nargs="*", default=["input"], help="PDF files or folders (default: input)")
    parser.add_argument("-o", "--output", default="output", help="output folder (default: output)")
    parser.add_argument("--ocr", action="store_true", help="read text inside images with Apple OCR (macOS only)")
    parser.add_argument("--gemini", action="store_true",
                        help="send images whose OCR fails the checks (or every image, without --ocr) to Gemini; "
                             "needs GEMINI_API_KEY")
    parser.add_argument("--gemini-model", default=GEMINI_MODEL, help=f"default: {GEMINI_MODEL}")
    parser.add_argument("--pages", type=page_range, help="only these pages, e.g. 5 or 1-8")
    parser.add_argument("--no-images", action="store_true", help="do not save embedded images as PNG")
    parser.add_argument("--force", action="store_true",
                        help="process every PDF again, even if it was already processed with the same settings")
    parser.add_argument("--furniture", action="store_true",
                        help="keep page headers, footers and page numbers in the Markdown and chunks")
    parser.add_argument("--table-mode", choices=["accurate", "fast"], default="accurate",
                        help="Docling table model (default: accurate)")
    args = parser.parse_args(argv)
    if args.ocr and sys.platform != "darwin":
        parser.error("--ocr uses Apple's OCR (ocrmac) and works only on macOS")
    return args


def find_pdfs(inputs):
    """PDFs from the given files and folders (folders: not searched recursively). Returns (pdfs, problems)."""
    pdfs, problems = [], []
    for name in inputs:
        path = Path(name)
        if path.is_dir():
            pdfs += sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
        elif path.is_file():
            pdfs.append(path)
        else:
            problems.append(f"not found: {name}")
    return pdfs, problems


def main(argv=None):
    global OUT_DIR, DO_OCR, USE_GEMINI, GEMINI_MODEL, TABLE_MODE, PAGE_RANGE, SAVE_IMAGES, KEEP_FURNITURE, FORCE
    args = parse_args(argv)
    OUT_DIR = Path(args.output)
    DO_OCR, USE_GEMINI, GEMINI_MODEL = args.ocr, args.gemini, args.gemini_model
    TABLE_MODE = TableFormerMode.FAST if args.table_mode == "fast" else TableFormerMode.ACCURATE
    PAGE_RANGE, SAVE_IMAGES, KEEP_FURNITURE = args.pages, not args.no_images, args.furniture
    FORCE = args.force

    pdfs, problems = find_pdfs(args.inputs)
    for problem in problems:
        print(problem)
    if not pdfs:
        print("No PDFs found.")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest_file = OUT_DIR / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8")) if manifest_file.exists() else {}
    converter = build_converter()
    settings = cache_settings()

    records = []
    for pdf in pdfs:
        try:
            record = process_pdf(converter, pdf, manifest, settings)
        except Exception as e:           # one bad PDF must not stop the rest
            record = {"file": pdf.name, "status": "failed", "error": f"{type(e).__name__}: {e}"}
        if record["status"] == ConversionStatus.SUCCESS.value:
            manifest[record["sha256"]] = record   # only full successes are skipped next time (failed, incomplete: retried)
            save_manifest(manifest)               # saved as each PDF finishes, so a crash later loses nothing
        records.append(record)
        print_summary(record)

    report = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "settings": {
            "inputs": args.inputs,
            "backend": "docling-parse (default), include_bitmap_images=True",
            "do_ocr": DO_OCR,
            "ocr_engine": "ocrmac, run by pdfingest on each stored image (Docling's own OCR is off)" if DO_OCR else None,
            "force": FORCE,
            "use_gemini": USE_GEMINI,
            "gemini_model": GEMINI_MODEL if USE_GEMINI else None,
            "ocr_checks": f"confidence >= {CONFIDENCE_MIN} and real-word share >= {REAL_WORD_MIN} "
                          f"(wordfreq zipf >= {REAL_WORD_ZIPF} or word in the PDF's own text)",
            "min_image_share": MIN_IMAGE_SHARE,
            "table_mode": TABLE_MODE.value,
            "page_range": PAGE_RANGE,
            "generate_parsed_pages": True,
            "save_images": SAVE_IMAGES,
            "furniture": "kept" if KEEP_FURNITURE else "left out of Markdown and chunks (still in the JSON)",
            "image_only_rule": f"share > {IMAGE_ONLY_SHARE} and text-layer words < {IMAGE_ONLY_WORDS}",
            "text_check_reference": "pypdfium2 text layer",
            "chunker": "docling-core HierarchicalChunker",
        },
        "versions": package_versions(),
        "problems": problems,
        "files": records,
    }
    (OUT_DIR / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved to {OUT_DIR}")
    return 0 if all(r["status"] in ("success", "skipped") for r in records) and not problems else 1


if __name__ == "__main__":
    sys.exit(main())
