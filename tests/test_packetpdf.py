"""The PDF view of an evidence packet (mirage/packetpdf.py). The page is plain HTML printed by a locally installed Edge or Chrome; the
printing tests are skipped if none is installed. Nothing touches the network."""
from __future__ import annotations

import copy
import json
import re

import pytest

from mirage import packet as pk
from mirage import packetpdf as pdf
from test_packet import build

needs_browser = pytest.mark.skipif(pdf.find_browser() is None, reason="no Edge or Chrome installed")


def test_the_label_comes_first_and_the_page_carries_the_hash_and_the_verify_command(tmp_path):
    p = build(tmp_path)
    page = pdf.render_html(p)
    assert page.index("STAGED DEMO") < page.index("<h1>") < page.index("Call summary")
    assert p["integrity"]["sha256"] in page and "python -m mirage.packet verify" in page and "the hash covers the JSON file, not this PDF" in page
    for heading in ("Call summary", "Intel", "Stage timeline", "Transcript excerpt", "Ending and disclosure", "Integrity"):
        assert f"<h2>{heading}</h2>" in page


def test_intel_confidence_labels_and_quotes_are_shown_as_recorded(tmp_path):
    page = pdf.render_html(build(tmp_path))
    assert "expected" in page and "keyword" in page and "possibly part of 00001234" in page
    assert "0000 | एक, दो, तीन, चार" in page
    assert "needs confirmation" in page and "not a probability" in page
    assert "No transcript" not in page


def test_devanagari_text_goes_into_the_page_unchanged(tmp_path):
    p = build(tmp_path)
    page = pdf.render_html(p)
    assert "Hello sir, customs office से बोल रहा हूँ।" in page and 'charset="utf-8"' in page


def test_nothing_in_the_packet_can_inject_markup(tmp_path):
    p = build(tmp_path)
    p["transcript_native"][0]["text"] = "<script>alert(1)</script> & <b>x</b>"
    p["intel"][0]["quote"] = "<img src=x onerror=alert(1)>"
    page = pdf.render_html(p)
    assert "<script>alert" not in page and "<img src=x" not in page and "&lt;script&gt;" in page


def test_the_page_is_self_contained_no_scripts_no_links_no_external_files(tmp_path):
    page = pdf.render_html(build(tmp_path))
    assert "default-src 'none'" in page
    assert not re.search(r"<script|<link|<img|<iframe|src=|href=|https?://|@import|url\(", page, re.I)


def test_no_claim_about_legal_admissibility_in_the_pdf_page(tmp_path):
    page = pdf.render_html(build(tmp_path)).lower()
    assert page.count("admissib") == 1 and "no claim is made about legal admissibility" in page
    for banned in ("court-ready", "legally valid", "legally binding", "is admissible", "certified"):
        assert banned not in page


def test_an_empty_intel_list_is_said_plainly(tmp_path):
    p = build(tmp_path, findings={})
    page = pdf.render_html(p)
    assert "No intel items were captured" in page and "No UPI id, account number or IFSC code was captured" in page


def test_the_excerpt_keeps_the_segments_with_intel_and_the_ends_and_stays_short(tmp_path):
    p = build(tmp_path)
    ex = pdf.excerpt_segments(p)
    texts = [x["text"] for x in ex]
    assert p["transcript_native"][0]["text"] in texts and p["transcript_native"][-1]["text"] in texts
    assert any("एक, दो, तीन, चार" in t for t in texts)
    assert [x["index"] for x in ex] == sorted(x["index"] for x in ex)
    big = copy.deepcopy(p)
    big["transcript_native"] = [{"t": float(i), "utterance_id": f"u{i}", "language": "hi-IN", "audio_s": 1.0, "text": f"line {i}"} for i in range(200)]
    assert len(pdf.excerpt_segments(big)) <= pdf.EXCERPT_MAX


def test_a_packet_that_does_not_verify_is_refused(tmp_path):
    p = build(tmp_path)
    p["intel"][1]["confidence"] = "keyword"                    # an upgrade after the fact
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="does not verify"):
        pdf.build_pdf(path, tmp_path / "bad.pdf", browser="not-used")


@needs_browser
def test_the_pdf_is_made_next_to_the_packet_in_one_or_two_pages_and_embeds_a_devanagari_font(tmp_path):
    p = build(tmp_path)
    path = tmp_path / "packets" / "c1.json"
    path.parent.mkdir()
    path.write_text(pk.format_packet(p), encoding="utf-8")
    out = pdf.build_pdf(path)
    assert out == path.with_suffix(".pdf") and out.read_bytes().startswith(b"%PDF")
    assert 1 <= pdf.pdf_page_count(out) <= 2
    fonts = set(re.findall(rb"/FontName\s*/[A-Z]{6}\+([A-Za-z0-9\-]+)", out.read_bytes()))
    assert fonts and not all(f.startswith((b"Arial", b"Helvetica", b"Times")) for f in fonts)
