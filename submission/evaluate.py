#!/usr/bin/env python3
"""Evaluation and output QA for redact_docx.py.  Prints counts and metrics only, never source values.

  python evaluate.py metrics SOURCE.docx --gold gold/gold_labels.json [--out output/metrics.json]
  python evaluate.py qa SOURCE.docx REDACTED.docx --gold gold/gold_labels.json [--out output/qa.json] [--render DIR]

metrics : span-level exact-match P/R/F1 per type + character-level accuracy against the gold set.
qa      : searches every part of the redacted file for the gold values (in memory only), re-checks media,
          compares document structure, and optionally renders both files with Microsoft Word.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import redact_docx as rd


def _load_gold(path, src):
    gold = json.loads(Path(path).read_text())
    sha = hashlib.sha256(Path(src).read_bytes()).hexdigest()
    if gold["source_sha256"] != sha:
        sys.exit("error: gold labels were made for a different source file (sha256 mismatch)")
    return {tuple(a) for a in gold["annotations"]}


def _ratio(a, b):
    return a / b if b else None          # zero denominator -> undefined (reported as n/a)


def _prf(tp, fp, fn):
    p, r = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    f1 = None if p is None or r is None else (0.0 if p + r == 0 else 2 * p * r / (p + r))
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f1}


def metrics(src, gold_path, model="en_core_web_lg"):
    gold = _load_gold(gold_path, src)
    pkg = rd.Package(src)
    pred = {(sid, s.start, s.end, s.type) for sid, v in rd.detect_all(pkg.segments, rd._load_nlp(model)).items()
            for s in v if s.type in rd.TYPES}
    out = {"per_type": {}, "note": "exact match of (segment, start, end, type); n/a = zero denominator"}
    for t in rd.TYPES:
        g, p = {x for x in gold if x[3] == t}, {x for x in pred if x[3] == t}
        out["per_type"][t] = _prf(len(g & p), len(p - g), len(g - p))
        out["per_type"][t]["gold"] = len(g)
        # diagnostics: FPs that overlap a same-type gold span are boundary errors, not spurious detections
        out["per_type"][t]["fp_boundary"] = sum(any(q[0] == x[0] and q[1] < x[2] and x[1] < q[2] for q in g) for x in p - g)
    out["overall_micro"] = _prf(len(gold & pred), len(pred - gold), len(gold - pred))
    out["overall_micro"]["gold"] = len(gold)

    # character level: binary PII mask over non-whitespace characters of every extracted segment
    gm, pm = defaultdict(set), defaultdict(set)
    for sid, a, b, _ in gold:
        gm[sid].update(range(a, b))
    for sid, a, b, _ in pred:
        pm[sid].update(range(a, b))
    c = Counter()
    for seg in pkg.segments:
        for i, ch in enumerate(seg.text):
            if not ch.isspace():
                c[(i in gm[seg.sid], i in pm[seg.sid])] += 1
    tp, fp, fn, tn = c[(True, True)], c[(False, True)], c[(True, False)], c[(False, False)]
    out["character_level"] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "non_whitespace_chars": tp + fp + fn + tn,
                              "accuracy": _ratio(tp + tn, tp + fp + fn + tn), **{k: v for k, v in _prf(tp, fp, fn).items()
                                                                                 if k in ("precision", "recall", "f1")}}
    return out


def _count(value, text):
    pat = r"(?<![\w@.])" + r"\s+".join(map(re.escape, value.split())) + r"(?![\w@])"
    return len(re.findall(pat, text, re.I))


def qa(src, red, gold_path, render_dir=None):
    gold = _load_gold(gold_path, src)
    s_pkg, r_pkg = rd.Package(src), rd.Package(red)
    seg = {s.sid: s for s in s_pkg.segments}
    values = defaultdict(set)                               # in-memory only; never written or printed
    for sid, a, b, t in gold:
        values[t].add(re.sub(r"\s+", " ", seg[sid].text[a:b]).strip())
    # occurrences outside gold spans in the source are legitimately retained (e.g. "Maharashtra, India" in prose)
    masked = []
    for s in s_pkg.segments:
        t = list(s.text)
        for sid, a, b, _ in gold:
            if sid == s.sid:
                t[a:b] = " " * (b - a)
        masked.append("".join(t))
    src_rest = "\n".join(masked)
    out_text = "\n".join(s.text for s in r_pkg.segments)
    out_raw = "\n".join(html.unescape(re.sub(r"<[^>]+>", " ", b.decode("utf8", "replace")))
                        for n, b in r_pkg.raw.items() if n.endswith((".xml", ".rels")))
    leaks, residual = Counter(), Counter()
    for t, vals in values.items():
        for v in vals:
            expected = _count(v, src_rest)
            n_seg, n_raw = _count(v, out_text), _count(v, out_raw)
            residual[t] += n_seg > 0
            leaks[t] += max(0, max(n_seg, n_raw) - expected)
    res = {"distinct_gold_values": {t: len(v) for t, v in values.items()},
           "values_still_present_in_output_text": dict(residual),
           "occurrences_beyond_non_pii_contexts": dict(leaks),
           "note": "a value still present only in non-PII contexts of the source (e.g. a state name) is not a leak"}

    # structure: the same OOXML skeleton must survive
    def census(pkg):
        c = Counter()
        for name, root in pkg.trees.items():
            for tag in ("p", "tbl", "tr", "tc", "drawing", "sectPr", "txbxContent", "instrText", "hyperlink"):
                c[tag] += sum(1 for _ in root.iter(f"{{{rd.W}}}{tag}"))
        c["parts"] = len(pkg.raw)
        c["media"] = len(pkg.media())
        return dict(c)
    res["structure_source"], res["structure_output"] = census(s_pkg), census(r_pkg)
    res["structure_identical"] = res["structure_source"] == res["structure_output"]
    with zipfile.ZipFile(red) as z:
        res["zip_test_ok"] = z.testzip() is None
    res["xml_parts_parsed"] = len(r_pkg.trees)
    res["xml_parse_failures"] = r_pkg.skipped

    # media: which images changed, and does OCR of the output still find PII?
    res["media"] = []
    try:
        from rapidocr_onnxruntime import RapidOCR
        from PIL import Image
        engine = RapidOCR()
    except Exception:
        engine = None
    names = {v.lower() for t in ("PERSON", "ORG") for v in values.get(t, ())}
    for name in r_pkg.media():
        m = {"part": name, "changed": s_pkg.raw[name] != r_pkg.raw[name]}
        if engine is not None and Path(name).suffix.lower() in rd.IMAGE_EXT:
            lines = engine(rd._np(Image.open(io.BytesIO(r_pkg.raw[name])).convert("RGB")))[0] or []
            text = "\n".join(l[1] for l in lines)
            m["ocr_chars_after"] = len(text)
            m["pii_hits_after"] = sum(1 for l in lines if rd.NATIONAL_ID_RE.search(l[1]) or
                                      any(rd._compact(n) in rd._compact(l[1]) for n in names if len(rd._compact(n)) >= 3))
        res["media"].append(m)
    if render_dir:
        res["render"] = {"source": _render(src, render_dir, "source", keep_images=False),
                         "output": _render(red, render_dir, "output", keep_images=True)}
    return res


def _render(docx, out_dir, tag, keep_images):
    """Export with Microsoft Word (COM) to PDF; count pages; rasterise the output pages for review."""
    import pythoncom
    import win32com.client
    import fitz
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pdf = (out / f"{tag}.pdf").resolve()
    pythoncom.CoInitialize()
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible, word.DisplayAlerts = False, 0
    try:
        doc = word.Documents.Open(str(Path(docx).resolve()), ReadOnly=True, AddToRecentFiles=False)
        doc.ExportAsFixedFormat(str(pdf), 17)            # 17 = wdExportFormatPDF
        doc.Close(False)
    finally:
        word.Quit()
    with fitz.open(pdf) as d:
        info = {"pages": d.page_count}
        if keep_images:
            for i, page in enumerate(d):
                page.get_pixmap(dpi=60).save(out / f"{tag}-{i + 1:03d}.png")
    if not keep_images:
        pdf.unlink()                                      # the source rendering contains PII: do not keep it
    return info


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("metrics")
    m.add_argument("source")
    m.add_argument("--gold", required=True)
    m.add_argument("--out")
    q = sub.add_parser("qa")
    q.add_argument("source")
    q.add_argument("redacted")
    q.add_argument("--gold", required=True)
    q.add_argument("--out")
    q.add_argument("--render", metavar="DIR", help="render with Word into DIR (Windows + Word only)")
    a = ap.parse_args()
    res = metrics(a.source, a.gold) if a.cmd == "metrics" else qa(a.source, a.redacted, a.gold, a.render)
    txt = json.dumps(res, indent=2)
    if a.out:
        Path(a.out).write_text(txt)
    print(txt)


if __name__ == "__main__":
    main()
