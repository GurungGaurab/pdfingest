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
from docling_core.types.doc import ContentLayer, DocItemLabel, DoclingDocument

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pdfingest as P  # noqa: E402

docling = pytest.mark.docling


class FooterPDF(FPDF):
    def footer(self):
        self.set_y(-15)
        self.set_font("helvetica", size=8)
        self.cell(0, 10, f"pdfingest test footer - page {self.page_no()}", align="C")


def make_pdf(path, title="Test document", extra="", footer=False):
    """A heading, two paragraphs and an embedded image; with footer=True, two pages with a footer on each."""
    image = Image.new("RGB", (900, 360), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([20, 20, 880, 340], outline="navy", width=8)
    draw.rectangle([60, 80, 300, 280], fill="orange")
    draw.text((340, 160), "TEXT INSIDE THE IMAGE", fill="black", font_size=40)

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
    otherwise place_text returns None and the text goes to the 'not placed' section."""
    monkeypatch.setattr(P, "KEEP_FURNITURE", keep)
    doc, picture = small_doc()
    placed = P.place_text(doc, [picture.self_ref], "Logo text", "ocr")
    assert placed == (picture.self_ref if keep else None)
    assert (picture.meta is not None and picture.meta.description is not None) is keep


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
    image = report["files"][0]["embedded_images"][0]

    assert code == 0
    assert image["status"] == "not read" and "GEMINI_API_KEY" in image["gemini_error"]


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
