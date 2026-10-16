"""Tests for pdfingest. The PDFs are made here with fpdf2, so no real documents are needed.

The tests marked "docling" run the real Docling conversion (the first run downloads its models).
Run:  pytest -q              everything
      pytest -q -m "not docling"   only the quick checks
"""
import json
import shutil
import sys
from pathlib import Path

import pytest
from fpdf import FPDF
from PIL import Image, ImageDraw
from docling_core.types.doc import BoundingBox, ContentLayer, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pdfingest as P  # noqa: E402

docling = pytest.mark.docling


class FooterPDF(FPDF):
    def footer(self):
        self.set_y(-15)
        self.set_font("helvetica", size=8)
        self.cell(0, 10, f"pdfingest test footer - page {self.page_no()}", align="C")


def sample_image():
    image = Image.new("RGB", (900, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([20, 20, 880, 340], outline="navy", width=8)
    draw.rectangle([60, 80, 300, 280], fill="orange")
    draw.text((340, 160), "TEXT INSIDE THE IMAGE", fill="black", font_size=40)
    return image


def make_pdf(path, title="Test document", extra="", footer=False):
    """A heading, two paragraphs and an embedded image; with footer=True, two pages with a footer on each."""
    image = sample_image()

    pdf = FooterPDF() if footer else FPDF()
    pdf.set_title(title)
    pdf.set_author("pdfingest tests")
    pdf.add_page()
    pdf.set_font("helvetica", size=20)
    pdf.cell(text="Introduction", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", size=11)
    pdf.multi_cell(0, 6, "This short document checks that pdfingest writes Markdown, JSON and JSONL. "
                         "The picture below is stored in the PDF as an embedded image. " + extra,
                 new_x="LMARGIN", new_y="NEXT")
    pdf.image(image, x=20, w=170)
    pdf.multi_cell(0, 6, "This paragraph comes after the picture.", new_x="LMARGIN", new_y="NEXT")
    if footer:
        pdf.add_page()
        pdf.multi_cell(0, 6, "A second page, so the footer repeats.", new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))
    return path


def run(*args):
    """Run pdfingest and return (exit code, report.json)."""
    out = Path(args[args.index("-o") + 1])
    code = P.main(list(args))
    return code, json.loads((out / "report.json").read_text(encoding="utf-8"))


@pytest.fixture
def no_gemini_key(tmp_path, monkeypatch):
    """Hide any real key: no .env in the current folder or next to pdfingest.py, nothing in the environment."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(P, "ROOT", tmp_path)
    monkeypatch.setattr(P, "_gemini_client", None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)


# ---------------------------------------------------------------- quick checks (no Docling run)

def test_page_range():
    assert P.page_range("5") == (5, 5)
    assert P.page_range("1-8") == (1, 8)
    for bad in ("0", "8-1", "a", "1-"):
        with pytest.raises(Exception):
            P.page_range(bad)


def test_pdf_date():
    assert P.pdf_date("D:20260512093000+08'00'") == "2026-05-12"
    assert P.pdf_date("12 May 2026") == "12 May 2026"
    assert P.pdf_date("") is None


def test_front_matter_quotes_values(tmp_path):
    pdf = make_pdf(tmp_path / "doc.pdf", title='Notes: "draft" #1')
    fm = P.front_matter(pdf, "ab" * 32, 1)
    assert fm.startswith("---\n") and fm.endswith("---\n\n")
    assert 'title: "Notes: \\"draft\\" #1"' in fm
    assert 'source: "doc.pdf"' in fm and "pages: 1" in fm


def test_find_pdfs(tmp_path):
    make_pdf(tmp_path / "a.pdf")
    make_pdf(tmp_path / "B.PDF")
    (tmp_path / "notes.txt").write_text("not a pdf")
    pdfs, problems = P.find_pdfs([str(tmp_path), str(tmp_path / "missing.pdf")])
    assert [p.name for p in pdfs] == ["B.PDF", "a.pdf"]
    assert problems == [f"not found: {tmp_path / 'missing.pdf'}"]


def test_missing_gemini_key(no_gemini_key):
    text, error = P.read_image_with_gemini(Image.new("RGB", (10, 10)), 1)
    assert text is None and "GEMINI_API_KEY" in error


def small_doc():
    """A document with a body paragraph, a page footer (furniture) and a picture in the header (furniture)."""
    doc = DoclingDocument(name="small")
    doc.add_text(label=DocItemLabel.TEXT, text="A body paragraph.")
    doc.add_text(label=DocItemLabel.PAGE_FOOTER, text="Footer line", content_layer=ContentLayer.FURNITURE)
    picture = doc.add_picture(content_layer=ContentLayer.FURNITURE)
    return doc, picture


@pytest.mark.parametrize("keep", [False, True])
def test_furniture_in_markdown_and_chunks(monkeypatch, keep):
    monkeypatch.setattr(P, "KEEP_FURNITURE", keep)
    doc, _ = small_doc()
    md, _ = P.labelled_markdown(doc, {})
    chunks = " ".join(r["text"] for r in P.chunk_records(doc, "small.pdf", "ab" * 32, []))
    assert "A body paragraph." in md and "A body paragraph." in chunks
    assert ("Footer line" in md) is keep
    assert ("Footer line" in chunks) is keep


@pytest.mark.parametrize("keep", [False, True])
def test_image_text_not_hidden_in_furniture(monkeypatch, keep):
    """Text read from an image is only attached to a picture that will be exported;
    otherwise place_text returns None and pdfingest adds a body picture for the text instead."""
    monkeypatch.setattr(P, "KEEP_FURNITURE", keep)
    doc, picture = small_doc()
    placed = P.place_text(doc, [picture.self_ref], "Logo text", "ocr")
    assert placed == (picture.self_ref if keep else None)
    assert (picture.meta is not None and picture.meta.description is not None) is keep


def test_added_picture_goes_in_reading_order():
    """Image text with no layout picture becomes a new picture, placed by page and height."""
    def prov(page, top):
        box = BoundingBox(l=50, t=top, r=500, b=top + 20, coord_origin=CoordOrigin.TOPLEFT)
        return ProvenanceItem(page_no=page, bbox=box.to_bottom_left_origin(800), charspan=(0, 1))

    doc = DoclingDocument(name="order")
    for page, top, text in [(1, 100, "Para A"), (1, 500, "Para B"), (2, 100, "Para C")]:
        doc.add_text(label=DocItemLabel.TEXT, text=text, prov=prov(page, top))
    box = BoundingBox(l=50, t=300, r=500, b=450, coord_origin=CoordOrigin.TOPLEFT)
    ref = P.add_picture(doc, 1, box, 800, "Text from the image", "ocr")

    md = doc.export_to_markdown()
    assert md.index("Para A") < md.index("Text from the image") < md.index("Para B")
    picture = doc.pictures[0]
    assert picture.self_ref == ref and picture.prov[0].page_no == 1
    assert picture.meta.description.created_by == "ocrmac"
    again = DoclingDocument.model_validate_json(doc.model_dump_json())   # survives the JSON round trip
    assert again.pictures[0].meta.description.text == "Text from the image"

    top = BoundingBox(l=50, t=20, r=500, b=80, coord_origin=CoordOrigin.TOPLEFT)   # above everything on page 1
    P.add_picture(doc, 1, top, 800, "Image at the top", "ocr")
    md = doc.export_to_markdown()
    assert md.index("Image at the top") < md.index("Para A")


def test_settings_change_the_cache_key(monkeypatch):
    first = P.cache_settings()
    monkeypatch.setattr(P, "PAGE_RANGE", (1, 1))
    assert P.cache_settings() != first
    monkeypatch.setattr(P, "PAGE_RANGE", None)
    monkeypatch.setattr(P, "DO_OCR", not P.DO_OCR)
    assert P.cache_settings() != first


# ---------------------------------------------------------------- full runs (real Docling)

@docling
def test_outputs(tmp_path):
    (tmp_path / "in").mkdir()
    make_pdf(tmp_path / "in" / "doc.pdf")
    code, report = run(str(tmp_path / "in"), "-o", str(tmp_path / "out"))
    record = report["files"][0]
    out = tmp_path / "out"

    assert code == 0 and record["status"] == "success"
    assert set(record["outputs"]) == {"json", "md", "jsonl"}
    assert all((out / name).exists() for name in record["outputs"].values())
    assert record["outputs"]["md"].startswith("doc_" + record["sha256"][:8])

    md = (out / record["outputs"]["md"]).read_text(encoding="utf-8")
    assert md.startswith('---\nsource: "doc.pdf"\n')
    assert 'title: "Test document"' in md
    assert "Introduction" in md and "after the picture" in md

    rows = [json.loads(line) for line in (out / record["outputs"]["jsonl"]).read_text(encoding="utf-8").splitlines()]
    assert rows and record["chunks"] == len(rows)
    assert [r["chunk_index"] for r in rows] == list(range(len(rows)))
    assert all(r["source"] == "doc.pdf" and r["text"].strip() and r["pages"] == [1] for r in rows)
    assert any("after the picture" in r["text"] for r in rows)

    images = record["embedded_images"]
    assert len(images) == 1 and images[0]["page"] == 1
    assert (out / images[0]["saved_as"]).exists()
    assert images[0]["status"] == "not read" and images[0]["ocr_check"] == "OCR off"


@docling
def test_no_images_option(tmp_path):
    (tmp_path / "in").mkdir()
    make_pdf(tmp_path / "in" / "doc.pdf")
    code, report = run(str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--no-images")
    assert code == 0
    assert report["files"][0]["embedded_images"][0]["saved_as"] is None
    assert not list((tmp_path / "out").glob("*.png"))


@docling
def test_same_file_skipped_changed_file_gets_new_outputs(tmp_path):
    folder, out = tmp_path / "in", tmp_path / "out"
    folder.mkdir()
    pdf = make_pdf(folder / "doc.pdf")
    _, first = run(str(folder), "-o", str(out))
    old = first["files"][0]

    _, again = run(str(folder), "-o", str(out))
    assert again["files"][0]["status"] == "skipped"

    make_pdf(pdf, extra="This sentence makes it a new version.")
    _, changed = run(str(folder), "-o", str(out))
    new = changed["files"][0]
    assert new["status"] == "success" and new["sha256"] != old["sha256"]
    assert new["outputs"]["json"] != old["outputs"]["json"]
    assert (out / old["outputs"]["json"]).exists()          # the old version's outputs are kept

    shutil.copy(folder / "doc.pdf", folder / "copy.pdf")     # same content under another name
    _, copied = run(str(folder / "copy.pdf"), "-o", str(out))
    assert copied["files"][0]["status"] == "skipped"


@docling
def test_broken_pdf_does_not_stop_the_run(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "broken.pdf").write_text("this is not a PDF")
    make_pdf(folder / "good.pdf")
    code, report = run(str(folder), "-o", str(tmp_path / "out"))
    by_file = {r["file"]: r for r in report["files"]}

    assert code == 1
    assert by_file["broken.pdf"]["status"] == "failed" and by_file["broken.pdf"]["error"]
    assert by_file["good.pdf"]["status"] == "success"


@docling
def test_gemini_without_key_is_reported_not_crashed(tmp_path, no_gemini_key):
    (tmp_path / "in").mkdir()
    make_pdf(tmp_path / "in" / "doc.pdf")
    code, report = run(str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--gemini")
    record = report["files"][0]
    image = record["embedded_images"][0]

    assert code == 1 and record["status"] == "incomplete"
    assert image["status"] == "not read" and "GEMINI_API_KEY" in image["gemini_error"]


@docling
def test_failed_image_is_retried_next_run(tmp_path, monkeypatch):
    folder, out = tmp_path / "in", str(tmp_path / "out")
    folder.mkdir()
    make_pdf(folder / "doc.pdf")

    monkeypatch.setattr(P, "read_image_with_gemini", lambda image, page_no: (None, "ServerError: 503 UNAVAILABLE"))
    code, report = run(str(folder), "-o", out, "--gemini")
    assert code == 1 and report["files"][0]["status"] == "incomplete"

    monkeypatch.setattr(P, "read_image_with_gemini", lambda image, page_no: ("Words from Gemini", None))
    code, report = run(str(folder), "-o", out, "--gemini")
    record = report["files"][0]
    assert code == 0 and record["status"] == "success"
    assert "Words from Gemini" in (tmp_path / "out" / record["outputs"]["md"]).read_text(encoding="utf-8")

    _, report = run(str(folder), "-o", out, "--gemini")
    assert report["files"][0]["status"] == "skipped"


@docling
def test_changed_settings_are_not_skipped(tmp_path):
    folder, out = tmp_path / "in", str(tmp_path / "out")
    folder.mkdir()
    make_pdf(folder / "doc.pdf", footer=True)                 # two pages

    _, report = run(str(folder), "-o", out, "--pages", "1")
    assert report["files"][0]["pages"] == 1
    _, report = run(str(folder), "-o", out)
    assert report["files"][0]["status"] == "success" and report["files"][0]["pages"] == 2
    _, report = run(str(folder), "-o", out)
    assert report["files"][0]["status"] == "skipped"
    _, report = run(str(folder), "-o", out, "--force")
    assert report["files"][0]["status"] == "success"


@docling
def test_rejected_ocr_never_reaches_the_outputs(tmp_path, monkeypatch):
    """OCR text that fails the checks must not appear anywhere; Gemini's reading replaces it.
    OCR and Gemini are simulated, so this runs on any system."""
    monkeypatch.setattr(P.sys, "platform", "darwin")
    monkeypatch.setattr(P, "ocr_image", lambda image, box: ("Xqzt Vrrl Pkkm", 0.3, 1))
    monkeypatch.setattr(P, "read_image_with_gemini", lambda image, page_no: ("Words from Gemini", None))
    (tmp_path / "in").mkdir()
    make_pdf(tmp_path / "in" / "doc.pdf")
    code, report = run(str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--ocr", "--gemini")
    record = report["files"][0]

    assert code == 0 and record["embedded_images"][0]["source"] == "gemini"
    for name in record["outputs"].values():
        text = (tmp_path / "out" / name).read_text(encoding="utf-8")
        assert "Xqzt" not in text
        assert "Words from Gemini" in text


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple OCR only runs on macOS")
def test_real_ocr_reads_the_image():
    image = sample_image()
    box = BoundingBox(l=0, t=0, r=480, b=192, coord_origin=CoordOrigin.TOPLEFT)   # 170 mm wide on the page
    text, confidence, lines = P.ocr_image(image, box)
    assert "INSIDE THE IMAGE" in text.upper()
    assert lines >= 1 and 0 < confidence <= 1


@docling
def test_furniture_option(tmp_path):
    (tmp_path / "in").mkdir()
    make_pdf(tmp_path / "in" / "doc.pdf", footer=True)
    for option in ([], ["--furniture"]):
        out = tmp_path / ("kept" if option else "default")
        code, report = run(str(tmp_path / "in"), "-o", str(out), *option)
        record = report["files"][0]
        assert code == 0
        doc = DoclingDocument.load_from_json(out / record["outputs"]["json"])
        furniture = [t.text for t in doc.texts if t.content_layer == ContentLayer.FURNITURE and t.text.strip()]
        if not furniture:
            pytest.skip("Docling did not tag the test footer as furniture")
        md = (out / record["outputs"]["md"]).read_text(encoding="utf-8")
        chunks = (out / record["outputs"]["jsonl"]).read_text(encoding="utf-8")
        for text in furniture:
            assert (text in md) is bool(option)
            assert (json.dumps(text)[1:-1] in chunks) is bool(option)


@docling
def test_failed_reprocessing_is_not_skipped(tmp_path, monkeypatch):
    """success, then --force while Gemini is down (incomplete), then a normal run must process it again."""
    folder, out = tmp_path / "in", tmp_path / "out"
    folder.mkdir()
    make_pdf(folder / "doc.pdf")

    monkeypatch.setattr(P, "read_image_with_gemini", lambda image, page_no: ("Words from Gemini", None))
    _, report = run(str(folder), "-o", str(out), "--gemini")
    file_hash = report["files"][0]["sha256"]
    assert report["files"][0]["status"] == "success"

    monkeypatch.setattr(P, "read_image_with_gemini", lambda image, page_no: (None, "ServerError: 503 UNAVAILABLE"))
    _, report = run(str(folder), "-o", str(out), "--gemini", "--force")
    assert report["files"][0]["status"] == "incomplete"
    assert file_hash not in json.loads((out / "manifest.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(P, "read_image_with_gemini", lambda image, page_no: ("Words from Gemini", None))
    _, report = run(str(folder), "-o", str(out), "--gemini")
    record = report["files"][0]
    assert record["status"] == "success"
    assert "Words from Gemini" in (out / record["outputs"]["md"]).read_text(encoding="utf-8")


@docling
def test_ocr_error_falls_back_to_gemini(tmp_path, monkeypatch):
    def broken_ocr(image, box):
        raise RuntimeError("Vision request failed")

    monkeypatch.setattr(P.sys, "platform", "darwin")
    monkeypatch.setattr(P, "ocr_image", broken_ocr)
    monkeypatch.setattr(P, "read_image_with_gemini", lambda image, page_no: ("Words from Gemini", None))
    (tmp_path / "in").mkdir()
    make_pdf(tmp_path / "in" / "doc.pdf")

    code, report = run(str(tmp_path / "in"), "-o", str(tmp_path / "a"), "--ocr", "--gemini")
    image = report["files"][0]["embedded_images"][0]
    assert code == 0 and report["files"][0]["status"] == "success"
    assert image["source"] == "gemini" and "Vision request failed" in image["ocr_error"]

    code, report = run(str(tmp_path / "in"), "-o", str(tmp_path / "b"), "--ocr")      # no Gemini to fall back on
    assert code == 1 and report["files"][0]["status"] == "incomplete"


@docling
def test_empty_gemini_answer_is_retried(tmp_path, monkeypatch):
    """Gemini answering with nothing (not even NO_TEXT) is a failure, so the PDF is retried.
    The Gemini client is replaced, so no request leaves the machine."""
    from google import genai

    class EmptyAnswer:
        class models:
            @staticmethod
            def generate_content(model, contents):
                return type("Response", (), {"text": "  "})()

    monkeypatch.setattr(P, "_gemini_client", None)
    monkeypatch.setattr(P, "gemini_key", lambda: "test-key")
    monkeypatch.setattr(genai, "Client", lambda api_key: EmptyAnswer())
    (tmp_path / "in").mkdir()
    make_pdf(tmp_path / "in" / "doc.pdf")
    code, report = run(str(tmp_path / "in"), "-o", str(tmp_path / "out"), "--gemini")
    record = report["files"][0]
    assert code == 1 and record["status"] == "incomplete"
    assert record["embedded_images"][0]["gemini_error"] == "Gemini returned no text"


@docling
def test_rejected_ocr_without_gemini_is_reported(tmp_path, monkeypatch):
    """Rejected OCR with nothing else to try is a final result: reported, not retried forever.
    Adding --gemini changes the settings, so the PDF is processed again."""
    monkeypatch.setattr(P.sys, "platform", "darwin")
    monkeypatch.setattr(P, "ocr_image", lambda image, box: ("Xqzt Vrrl Pkkm", 0.3, 1))
    monkeypatch.setattr(P, "read_image_with_gemini", lambda image, page_no: ("Words from Gemini", None))
    folder, out = tmp_path / "in", str(tmp_path / "out")
    folder.mkdir()
    make_pdf(folder / "doc.pdf")

    _, report = run(str(folder), "-o", out, "--ocr")
    record = report["files"][0]
    assert record["status"] == "success" and record["unread_images"] == ["p1: low confidence (0.3)"]
    _, report = run(str(folder), "-o", out, "--ocr", "--gemini")
    assert report["files"][0]["status"] == "success"
    assert report["files"][0]["embedded_images"][0]["source"] == "gemini"


@docling
def test_missing_image_file_is_not_skipped(tmp_path):
    folder, out = tmp_path / "in", tmp_path / "out"
    folder.mkdir()
    make_pdf(folder / "doc.pdf")
    _, report = run(str(folder), "-o", str(out))
    png = out / report["files"][0]["embedded_images"][0]["saved_as"]
    png.unlink()

    _, report = run(str(folder), "-o", str(out))
    assert report["files"][0]["status"] == "success" and png.exists()
