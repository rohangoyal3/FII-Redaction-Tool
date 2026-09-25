"""Self-check with synthetic values only:  python test_redact.py

Every fixture below is invented (example.org domains, test numbers, made-up names); no source text is used.
NER is not loaded here (rules only), so the check runs in seconds; the spaCy path is covered by evaluate.py.
"""
import io
import json
import os
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

import redact_docx as rd

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
DRAW = ('xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')


def _types(fn, text):
    return [(text[s.start:s.end], s.type) for s in fn(text)]


def unit_checks():
    """One positive and one negative case for each of the nine types."""
    # EMAIL
    assert _types(rd.detect_email, "mail a.b@example.com.")[0] == ("a.b@example.com", "EMAIL")
    assert not list(rd.detect_email("follow @example on social media; user at example dot com"))
    # PHONE: needs "+cc" or a Tel/Phone label; bare numbers and amounts are not phones
    assert _types(rd.detect_phone, "Tel: +91 22 1234 5678")[0][1] == "PHONE"
    assert _types(rd.detect_phone, "Phone: 022 2345 6789")[0] == ("022 2345 6789", "PHONE")
    assert not rd.detect_phone("revenue of 12345678 in Fiscal 2024 and 1,234,567.89 million")
    # SSN: SSA-valid ranges only
    assert _types(rd.detect_ssn, "SSN: 123-45-6789")[0] == ("123-45-6789", "SSN")
    assert not rd.detect_ssn("id 000-12-3456 and 666-12-3456 and 123-00-4567")
    # CREDIT_CARD: Luhn + issuer prefix/length
    assert rd.luhn_ok("4111111111111111") and not rd.luhn_ok("4111111111111112")
    assert _types(rd.detect_credit_card, "card 4111 1111 1111 1111 ok") == [("4111 1111 1111 1111", "CREDIT_CARD")]
    assert not list(rd.detect_credit_card("amount 1234 5678 9012 3456"))            # fails Luhn
    # IP_ADDRESS
    assert _types(rd.detect_ip, "server IP 192.168.10.25")[0] == ("192.168.10.25", "IP_ADDRESS")
    assert _types(rd.detect_ip, "host 2001:db8::1")[0][0] == "2001:db8::1"
    assert not list(rd.detect_ip("version 1.2.3.999 at 10:30:00"))
    # DOB: a date only with a birth cue
    assert _types(rd.detect_dob, "Date of Birth: 12/03/1971")[0] == ("12/03/1971", "DOB")
    assert not list(rd.detect_dob("dated March 12, 1971"))
    # PERSON: label / honorific rules; capitalised non-names are not people
    assert _types(rd.detect_person_rules, "Contact Person: Arjun Testname") == [("Arjun Testname", "PERSON")]
    assert _types(rd.detect_person_rules, "signed by Mr. Kiran Samplekar")[0][0] == "Kiran Samplekar"
    assert not list(rd.detect_person_rules("Contact Person: Board Committee; Annual Report 2024"))
    # ORG: private entities redacted, regulators / "... of India" public bodies kept
    orgs = [s for s, _ in _types(rd.detect_org_rules, "filed with Securities and Exchange Board of India Limited "
                                                        "and Acme Widgets Private Limited")]
    assert orgs == ["Acme Widgets Private Limited"], orgs
    assert _types(rd.detect_org_rules, "Legal Counsel to our Company as to Indian Law Samplelaw")[0][0] == "Samplelaw"
    assert _types(rd.detect_org_rules, "with the Acme Chakan Kamgar Sangathna, see")[0][0] == \
        "Acme Chakan Kamgar Sangathna"                                                  # trade union, full span
    assert not list(rd.detect_org_rules("the State Bank of India and Reserve Bank of India"))
    # ADDRESS: postal-code anchor; a state name in prose is not an address
    addr = _types(rd.detect_address, "Registered Office: 12, Test Road, Sampletown – 560 999, Karnataka, India")
    assert addr == [("12, Test Road, Sampletown – 560 999, Karnataka, India", "ADDRESS")], addr
    assert not list(rd.detect_address("Our operations are in Sampletown, Maharashtra, India."))
    # building-name line between an organisation line and the street line is part of the address
    t = "Acme Finance Limited\nThe Samplehouse\nUnit no. 16, B wing, Test Road, Mumbai Maharashtra India"
    assert t[list(rd.detect_address(t))[0].start:].startswith("The Samplehouse")
    t = "Acme Bank Limited\nCapital Markets Division\nUnit no. 16, B wing, Test Road, Mumbai Maharashtra India"
    assert t[list(rd.detect_address(t))[0].start:].startswith("Unit no.")              # a department is not
    # overlap rule: type priority beats length
    a, b = rd.Span(0, 10, "ORG", 0.9, "x"), rd.Span(5, 30, "ADDRESS", 0.9, "x")
    assert rd.resolve_overlaps([a, b]) == [b]


def _png_with_text(text):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (900, 120), "white")
    ImageDraw.Draw(img).text((20, 30), text, fill="black", font=ImageFont.load_default(size=40))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def _blank_png():
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (60, 40), (200, 220, 240)).save(out, "PNG")
    return out.getvalue()


def _make_docx(path):
    body = (f'<w:document {W} {DRAW}><w:body>'
            # e-mail split across two formatting runs
            '<w:p><w:r><w:t xml:space="preserve">Contact Person: Arjun Testname; e-mail: arjun.t</w:t></w:r>'
            '<w:r><w:rPr><w:b/></w:rPr><w:t>est@example.org</w:t></w:r></w:p>'
            # table cell address
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Registered Office: 12, Test Road, Sampletown – 560 999, '
            'Karnataka, India</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '<w:p><w:r><w:t>Acme Widgets Private Limited filed with the Reserve Bank of India.</w:t></w:r></w:p>'
            # organisation name split across two paragraphs (PDF-to-Word line break)
            '<w:p><w:r><w:t>Our supplier is Zentrix Gadgets</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>Private Limited.</w:t></w:r></w:p>'
            # types absent from the prospectus
            '<w:p><w:r><w:t>SSN: 123-45-6789; card 4111 1111 1111 1111; server IP 192.168.10.25; '
            'Date of Birth: 12/03/1971.</w:t></w:r></w:p>'
            # field codes: complex field and simple field
            '<w:p><w:r><w:instrText xml:space="preserve"> HYPERLINK "mailto:arjun.test@example.org" </w:instrText>'
            '</w:r></w:p>'
            '<w:p><w:fldSimple w:instr=" HYPERLINK &quot;mailto:kiran.s@example.net&quot; "><w:r><w:t>link</w:t>'
            '</w:r></w:fldSimple></w:p>'
            # a drawing with alt text pointing at an image
            '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Picture 1" descr="photo"/><a:graphic>'
            '<a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
            '<w:sectPr/></w:body></w:document>')
    files = {
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                               'content-types"><Default Extension="xml" ContentType="application/xml"/></Types>',
        "word/document.xml": body,
        "word/_rels/document.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                                        'relationships"><Relationship Id="rId1" Target="media/image1.png"/>'
                                        '</Relationships>',
        "word/header1.xml": f'<w:hdr {W}><w:p><w:r><w:t>Tel: +91 20 1111 2222</w:t></w:r></w:p></w:hdr>',
        "word/footer1.xml": f'<w:ftr {W}><w:p><w:r><w:t>Acme Widgets Private Limited</w:t></w:r></w:p></w:ftr>',
        "docProps/core.xml": '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
                             'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>'
                             'arjun.test@example.org</dc:creator><dc:description>Tel: +91 20 3333 4444'
                             '</dc:description></cp:coreProperties>',
        "word/media/image1.png": _png_with_text("Email: kiran.s@example.net"),
        "word/media/image2.png": _blank_png(),
        "word/media/image3.emf": b"\x01\x00\x00\x00synthetic-emf",
    }
    with zipfile.ZipFile(path, "w") as z:
        for n, c in files.items():
            z.writestr(n, c)


def _census(path):
    pkg = rd.Package(path)
    c = Counter()
    for root in pkg.trees.values():
        for tag in ("p", "tbl", "tr", "tc", "drawing", "sectPr", "instrText", "fldSimple"):
            c[tag] += sum(1 for _ in root.iter(f"{{{rd.W}}}{tag}"))
    return c, sorted(pkg.raw)


def end_to_end(d):
    src, out = Path(d, "in.docx"), Path(d, "out.docx")
    _make_docx(src)
    before = src.read_bytes()
    pkg = rd.Package(src)
    spans = rd.detect_all(pkg.segments, nlp=None)
    rd.apply_redactions(pkg.segments, spans)
    media = {m["part"]: m for m in rd.process_media(pkg, lambda line: list(rd.detect_email(line)), {}, ocr=True)}
    pkg.save(out)
    assert src.read_bytes() == before                                                  # original untouched

    red = rd.Package(out)
    text = "\n".join(s.text for s in red.segments)
    for leaked in ("Arjun Testname", "arjun.test@example.org", "kiran.s@example.net", "Test Road", "1111 2222",
                   "3333 4444", "Acme Widgets", "Zentrix", "123-45-6789", "4111 1111", "192.168.10.25", "12/03/1971"):
        assert leaked not in text, leaked
    with zipfile.ZipFile(out) as z:                                                    # raw XML too, not just segments
        raw = b"".join(z.read(n) for n in z.namelist() if n.endswith((".xml", ".rels"))).decode()
    for leaked in ("Testname", "arjun.test", "kiran.s@", "Zentrix", "123-45-6789", "192.168.10.25"):
        assert leaked not in raw, leaked
    assert "Reserve Bank of India" in text                                             # public body kept
    assert text.count("redacted.email001@example.com") == 3     # body (split runs) + field code + metadata
    segs = {s.sid: s.text for s in red.segments}
    split = [t for sid, t in segs.items() if sid.startswith("word/document.xml|p|")]
    assert "Private Limited." not in split and "." in split     # continuation paragraph emptied, not dropped
    assert "[ORG_001]" in segs["word/footer1.xml|p|0"]          # footer uses the body's token (consistency)
    assert any("[PHONE_" in t for sid, t in segs.items() if sid.startswith("word/header1"))
    assert "[SSN_001]" in text and "[CREDIT_CARD_001]" in text and "[DOB_001]" in text and "192.0.2." in text

    # images: OCR'd PII text is boxed out, a text-free image is untouched, vector media is reported not inspected
    assert media["word/media/image1.png"]["status"] == "REDACTED (text regions)", media
    assert media["word/media/image2.png"]["status"] == "inspected, no PII found"
    assert media["word/media/image3.emf"]["status"] == "NOT INSPECTED"
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(out) as b:
        assert b.testzip() is None
        assert a.read("word/media/image1.png") != b.read("word/media/image1.png")
        assert a.read("word/media/image2.png") == b.read("word/media/image2.png")
        assert a.read("word/media/image3.emf") == b.read("word/media/image3.emf")
        from rapidocr_onnxruntime import RapidOCR
        from PIL import Image
        after = RapidOCR()(rd._np(Image.open(io.BytesIO(b.read("word/media/image1.png"))).convert("RGB")))[0] or []
        assert not any("@" in line[1] for line in after), after                         # e-mail gone from pixels
    assert _census(src) == _census(out)                                                # structure and parts kept
    return src


def cli_paths(d, src):
    """Every output/report collision is refused before processing; overwrite needs --force; report is atomic."""
    before = src.read_bytes()
    calls = []
    real_run = rd.run
    rd.run = lambda *a, **k: calls.append(a) or real_run(*a, **k)
    rd._load_nlp = lambda model: None                                                  # rules only, fast

    def refused(argv, needle):
        n = len(calls)
        try:
            rd.main(argv + ["--no-ocr"])
        except SystemExit as e:
            assert needle in str(e), (argv, str(e))
        else:
            raise AssertionError(f"not refused: {argv}")
        assert len(calls) == n, "processing started before path validation"
        assert src.read_bytes() == before

    out, rep = Path(d, "o.docx"), Path(d, "o.json")
    alias = Path(d, "sub", "..", "in.docx")
    Path(d, "sub").mkdir()
    refused([str(src), "-o", str(src)], "never overwritten")                          # output == input
    refused([str(src), "-o", str(alias)], "never overwritten")                        # relative alias
    refused([str(src), "-o", str(out), "--report", str(src)], "report path resolves to the input")
    refused([str(src), "-o", str(out), "--report", str(alias)], "report path resolves to the input")
    refused([str(src), "-o", str(out), "--report", str(out)], "report path resolves to the output")
    refused([str(src), "-o", str(out), "--report", str(Path(d, "sub", "..", "o.docx"))], "resolves to the output")
    refused([str(src), "-o", str(Path(d, "x.txt"))], "must end in .docx")
    if os.name == "nt":
        refused([str(src), "-o", str(Path(d, "IN.DOCX"))], "never overwritten")       # case-insensitive alias
    hard = Path(d, "hard.docx")
    os.link(src, hard)                                                                 # same file, other name
    refused([str(src), "-o", str(hard), "--force"], "never overwritten")
    refused([str(src), "-o", str(out), "--report", str(hard), "--force"], "report path resolves to the input")
    skipped = []
    try:
        os.symlink(src, Path(d, "link.docx"))
    except OSError:
        skipped.append("symlink alias (OS refused to create a symlink)")
    else:
        refused([str(src), "-o", str(Path(d, "link.docx")), "--force"], "never overwritten")
    Path(d, "adir.docx").mkdir()
    refused([str(src), "-o", str(Path(d, "adir.docx")), "--force"], "not a regular file")
    assert not out.exists() and not rep.exists()                                       # nothing written by refusals

    rd.main([str(src), "-o", str(out), "--report", str(rep), "--no-ocr"])             # fresh run succeeds
    assert out.exists() and json.loads(rep.read_text())["text_segments"] > 0
    old_docx, old_rep = out.read_bytes(), rep.read_bytes()
    refused([str(src), "-o", str(out), "--report", str(rep)], "use --force")          # both exist
    refused([str(src), "-o", str(Path(d, "o2.docx")), "--report", str(rep)], "use --force")  # only report exists
    assert out.read_bytes() == old_docx and rep.read_bytes() == old_rep
    rd.main([str(src), "-o", str(out), "--report", str(rep), "--no-ocr", "--force"])  # explicit overwrite
    assert json.loads(rep.read_text())["media"][0]["status"] == "NOT INSPECTED"

    # atomic report: a failure while writing leaves the old report intact and no temp file behind
    old_rep = rep.read_bytes()
    rd.run = lambda *a, **k: {"unserialisable": object()}
    try:
        rd.main([str(src), "-o", str(out), "--report", str(rep), "--no-ocr", "--force"])
    except TypeError:
        pass
    else:
        raise AssertionError("report write did not fail")
    assert rep.read_bytes() == old_rep                                                 # old report intact
    assert not [p for p in Path(d).iterdir() if p.name.endswith(".tmp")], "temp file left behind"
    rd.run = real_run
    assert src.read_bytes() == before
    return skipped


if __name__ == "__main__":
    unit_checks()
    with tempfile.TemporaryDirectory() as d:
        skipped = cli_paths(d, end_to_end(d))
    print("all checks passed" + "".join(f"\nskipped: {s}" for s in skipped))
