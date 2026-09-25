#!/usr/bin/env python3
"""Redact PII from a DOCX file while preserving its OOXML structure.

Usage:
    python redact_docx.py INPUT.docx [-o OUTPUT.docx] [--force] [--no-ocr]
                          [--min-confidence 0.5] [--report REPORT.json]

Detects nine PII types (EMAIL, PHONE, SSN, CREDIT_CARD, IP_ADDRESS, PERSON,
ORG, ADDRESS, DOB) in every text-bearing part of the package and replaces each
unique value with a consistent synthetic token.  The source file is never
modified and no source value is ever written to stdout, logs or reports.
"""
from __future__ import annotations

import argparse
import copy
import io
import ipaddress
import json
import os
import re
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

# --------------------------------------------------------------------------
# OOXML namespaces
# --------------------------------------------------------------------------
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
V = "urn:schemas-microsoft-com:vml"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

WORDML_PART = re.compile(
    r"^word/(document|header\d*|footer\d*|footnotes|endnotes|comments\w*|glossary/document)\.xml$")
DRAWINGML_PART = re.compile(r"^word/(charts|diagrams|drawings)/.*\.xml$|^word/theme/.*\.xml$")
PROPS_PART = re.compile(r"^docProps/(core|app|custom)\.xml$|^customXml/item\d*\.xml$")
RELS_PART = re.compile(r"\.rels$")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff"}
UNSUPPORTED_MEDIA_EXT = {".emf", ".wmf", ".svg", ".bin", ".ole"}

TYPES = ["EMAIL", "PHONE", "SSN", "CREDIT_CARD", "IP_ADDRESS", "PERSON", "ORG", "ADDRESS", "DOB"]


# --------------------------------------------------------------------------
# Text model
# --------------------------------------------------------------------------
@dataclass
class Segment:
    """One unit of detectable text.  `pieces` maps text offsets to XML nodes."""
    sid: str                     # stable id: part|kind|index
    text: str
    pieces: list                 # [(start, end, setter-or-None)]
    part: str


@dataclass
class Span:
    start: int
    end: int
    type: str
    confidence: float
    source: str
    review: bool = False
    key: str = ""                # normalisation key for consistent replacement
    cont: bool = False           # continuation of a value split across paragraphs


def _set_text(el):
    def setter(new):
        el.text = new
        if new != new.strip() or "  " in new:
            el.set(XML_SPACE, "preserve")
    return setter


def _set_attr(el, name):
    return lambda new: el.set(name, new)


def _wordml_segments(part, root):
    """Paragraph segments (w:t joined across runs), field codes, deleted text, alt text."""
    segs = []
    wt, wp = f"{{{W}}}t", f"{{{W}}}p"
    for i, p in enumerate(root.iter(wp)):
        text, pieces = [], []
        pos = 0
        for el in p.iter(wt, f"{{{W}}}tab", f"{{{W}}}br", f"{{{W}}}cr", f"{{{W}}}noBreakHyphen"):
            # only elements whose nearest paragraph is this one (text boxes nest paragraphs)
            anc = el.getparent()
            while anc is not None and anc.tag != wp:
                anc = anc.getparent()
            if anc is not p:
                continue
            if el.tag == wt:
                s = el.text or ""
                pieces.append((pos, pos + len(s), _set_text(el)))
            else:
                s = "\t" if el.tag.endswith("tab") else "-" if el.tag.endswith("Hyphen") else "\n"
                pieces.append((pos, pos + len(s), None))
            text.append(s)
            pos += len(s)
        if pos:
            segs.append(Segment(f"{part}|p|{i}", "".join(text), pieces, part))
    for kind, tag in (("instr", f"{{{W}}}instrText"), ("del", f"{{{W}}}delText")):
        for i, el in enumerate(root.iter(tag)):
            if el.text and el.text.strip():
                segs.append(Segment(f"{part}|{kind}|{i}", el.text, [(0, len(el.text), _set_text(el))], part))
    for i, el in enumerate(root.iter(f"{{{W}}}fldSimple")):
        v = el.get(f"{{{W}}}instr") or ""
        if v.strip():
            segs.append(Segment(f"{part}|fld|{i}", v, [(0, len(v), _set_attr(el, f"{{{W}}}instr"))], part))
    segs += _attr_segments(part, root)
    return segs


def _attr_segments(part, root):
    """Alt text / titles / tooltips on drawings, pictures, VML shapes and hyperlinks."""
    segs = []
    targets = [(f"{{{WP}}}docPr", ("descr", "title", "name")),
               (f"{{{PIC}}}cNvPr", ("descr", "title", "name")),
               (f"{{{V}}}shape", ("alt", "title")),
               (f"{{{V}}}imagedata", ("title",)),
               (f"{{{W}}}hyperlink", (f"{{{W}}}tooltip",))]
    n = 0
    for tag, attrs in targets:
        for el in root.iter(tag):
            for a in attrs:
                v = el.get(a)
                if v and v.strip():
                    segs.append(Segment(f"{part}|attr|{n}", v, [(0, len(v), _set_attr(el, a))], part))
                    n += 1
    return segs


def _drawingml_segments(part, root):
    segs = []
    for i, p in enumerate(root.iter(f"{{{A}}}p")):
        text, pieces, pos = [], [], 0
        for el in p.iter(f"{{{A}}}t"):
            s = el.text or ""
            pieces.append((pos, pos + len(s), _set_text(el)))
            text.append(s)
            pos += len(s)
        if pos:
            segs.append(Segment(f"{part}|a|{i}", "".join(text), pieces, part))
    return segs + _attr_segments(part, root)


def _leaf_segments(part, root):
    segs = []
    for i, el in enumerate(root.iter()):
        if isinstance(el.tag, str) and len(el) == 0 and el.text and el.text.strip():
            segs.append(Segment(f"{part}|leaf|{i}", el.text, [(0, len(el.text), _set_text(el))], part))
    return segs


def _rels_segments(part, root):
    segs = []
    for i, el in enumerate(root.iter(f"{{{REL}}}Relationship")):
        if el.get("TargetMode") == "External":
            v = el.get("Target", "")
            segs.append(Segment(f"{part}|rel|{i}", v, [(0, len(v), _set_attr(el, "Target"))], part))
    return segs


class Package:
    """A DOCX opened in memory: parsed XML trees plus raw bytes for everything else."""

    def __init__(self, path):
        self.path = Path(path)
        with zipfile.ZipFile(self.path) as z:
            self.infos = z.infolist()
            self.raw = {i.filename: z.read(i.filename) for i in self.infos}
        self.trees, self.segments, self.skipped = {}, [], []
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
        for name, data in self.raw.items():
            kind = self._kind(name)
            if kind is None:
                continue
            try:
                root = etree.fromstring(data, parser)
            except etree.XMLSyntaxError:
                self.skipped.append((name, "unparseable XML"))
                continue
            self.trees[name] = root
            self.segments += {"wordml": _wordml_segments, "drawingml": _drawingml_segments,
                              "props": _leaf_segments, "rels": _rels_segments}[kind](name, root)

    @staticmethod
    def _kind(name):
        if WORDML_PART.match(name):
            return "wordml"
        if DRAWINGML_PART.match(name):
            return "drawingml"
        if PROPS_PART.match(name):
            return "props"
        if RELS_PART.search(name):
            return "rels"
        return None

    def media(self):
        return [n for n in self.raw if n.startswith("word/media/") or n.startswith("word/embeddings/")]

    def save(self, out_path):
        for name, root in self.trees.items():
            self.raw[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

        def write(tmp):
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for info in self.infos:
                    z.writestr(info, self.raw[info.filename], compress_type=zipfile.ZIP_DEFLATED)
        atomic_write(out_path, write)


def atomic_write(path, write):
    """write(tmp_path) into a temp file beside `path`, then rename over it.  On any failure the temp
    file is removed and `path` is left as it was, so a partial file is never visible."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    try:
        write(tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def same_file(a, b):
    """Do two paths name the same file?  Compares resolved paths (relative paths, symlinks, `..`,
    Windows case) and, when both exist, file identity (hard links, other aliases)."""
    a, b = Path(a), Path(b)
    if os.path.normcase(a.resolve()) == os.path.normcase(b.resolve()):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False



# --------------------------------------------------------------------------
# Detectors.  Each yields Span(start, end, type, confidence, source, review, key).
# Rule detectors run over "streams" (consecutive paragraphs of one part joined
# by "\n") so that values split across paragraphs by PDF->Word conversion are
# still found; whitespace inside patterns is [ \t] unless crossing is intended.
# --------------------------------------------------------------------------
PRIORITY = {"EMAIL": 9, "URL": 9, "CREDIT_CARD": 8, "SSN": 8, "IP_ADDRESS": 8, "PHONE": 7,
            "DOB": 6, "ADDRESS": 5, "ORG": 4, "PERSON": 4}
TLDS = r"(?:com|in|net|org|co|gov|edu|biz|info|io|uk|us)"

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
    rf"(?:\. {TLDS}\b|\.[A-Za-z]{{2,}})")


def detect_email(text):
    for m in EMAIL_RE.finditer(text):
        yield Span(m.start(), m.end(), "EMAIL", 0.99, "regex", key=re.sub(r"\s", "", m.group()).lower())


# URLs are not one of the nine PII types, but a web address containing a redacted company's
# name re-identifies it; such URLs are replaced too (filtered in detect_all, reported separately).
URL_RE = re.compile(rf"(?<![\w@/.])(?:https?://|www\.)[\w\-]+(?:\.[\w\-]+)*(?:\. {TLDS}\b)?(?:/[^\s,;)”\"]*)?", re.I)


def detect_url(text):
    for m in URL_RE.finditer(text):
        s = m.group().rstrip(".")
        yield Span(m.start(), m.start() + len(s), "URL", 0.9, "regex+entity-name", key=_compact(s))


PUBLIC_DOMAIN = re.compile(r"gov|nicin|sebi|rbiorg|bseindia|nseindia|nsdl|cdsl|npci")  # on _compact()ed URLs


def _compact(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


PHONE_LABEL = r"(?i:tel(?:ephone)?|ph(?:one)?|fax|mob(?:ile)?|contact\s+no\.?|toll[ \t-]*free(?:[ \t]+no\.?)?|helpline)"
PHONE_INTL = re.compile(r"(?<![\w+])\+[ \t]?\d{1,3}[ \t\-–]*(?:\(\d{1,4}\)[ \t\-–]*)?\d[\d \t\-–]{5,15}\d(?![\d,.]\d)")
PHONE_LABELLED = re.compile(
    PHONE_LABEL + r"[ \t]*(?:no\.?|number)?[ \t]*[:.\-–]?[ \t]*((?:\(?0?\d{2,5}\)?[ \t\-–]*)?\d[\d \t\-–]{5,14}\d)(?![\d,.]\d)")


def _phone_key(s):
    d = re.sub(r"\D", "", s)
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    return d.lstrip("0")


def detect_phone(text):
    spans = []
    for m in PHONE_INTL.finditer(text):
        s = m.group().rstrip(" \t-–")
        if 8 <= len(re.sub(r"\D", "", s)) <= 15:
            spans.append(Span(m.start(), m.start() + len(s), "PHONE", 0.95, "regex+intl", key=_phone_key(s)))
    for m in PHONE_LABELLED.finditer(text):
        s = m.group(1).rstrip(" \t-–")
        if 8 <= len(re.sub(r"\D", "", s)) <= 13:
            spans.append(Span(m.start(1), m.start(1) + len(s), "PHONE", 0.85, "regex+label", key=_phone_key(s)))
    return spans


SSN_RE = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])")
SSN_CTX_RE = re.compile(
    r"(?i:\bssn|social[ \t]+security(?:[ \t]+(?:no\.?|number))?)[ \t]*[:#.]?[ \t]*((?!000|666|9\d\d)\d{3}[ -]?(?!00)\d{2}[ -]?(?!0000)\d{4})(?!\d)")


def detect_ssn(text):
    spans = {}
    for m in SSN_RE.finditer(text):  # bare ddd-dd-dddd in valid ranges but without context -> review flag
        spans[m.start()] = Span(m.start(), m.end(), "SSN", 0.8, "regex", review=True, key=re.sub(r"\D", "", m.group()))
    for m in SSN_CTX_RE.finditer(text):
        spans[m.start(1)] = Span(m.start(1), m.end(1), "SSN", 0.97, "regex+context", key=re.sub(r"\D", "", m.group(1)))
    return list(spans.values())


CC_RE = re.compile(r"(?<![\d.,-])\d(?:[ -]?\d){12,18}(?![\d.,]\d|-\d|\d)")


def luhn_ok(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _cc_brand(d):
    return (d[0] == "4" and len(d) in (13, 16, 19)) or (51 <= int(d[:2]) <= 55 and len(d) == 16) \
        or (2221 <= int(d[:4]) <= 2720 and len(d) == 16) or (d[:2] in ("34", "37") and len(d) == 15) \
        or ((d[:4] == "6011" or d[:2] == "65") and len(d) in (16, 19)) or (d[:2] == "35" and len(d) == 16) \
        or (d[:2] in ("36", "38") and len(d) == 14)


def detect_credit_card(text):
    for m in CC_RE.finditer(text):
        d = re.sub(r"\D", "", m.group())
        if luhn_ok(d) and _cc_brand(d):
            yield Span(m.start(), m.end(), "CREDIT_CARD", 0.95, "regex+luhn+iin", key=d)


IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.]|\.\d)")
IPV6_RE = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")


def detect_ip(text):
    for rx in (IPV4_RE, IPV6_RE):
        for m in rx.finditer(text):
            if rx is IPV6_RE and m.group().count(":") < 3 and "::" not in m.group():
                continue  # times such as 10:30:00
            try:
                ip = ipaddress.ip_address(m.group())
            except ValueError:
                continue
            ctx = re.search(r"(?i)\bip\b|address|server|host", text[max(0, m.start() - 40):m.start()])
            yield Span(m.start(), m.end(), "IP_ADDRESS", 0.95 if ctx else 0.7, "regex+ipaddress",
                       review=not ctx, key=str(ip))


MONTHS = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
          r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?")
DATE = (rf"(?:\d{{1,2}}(?:st|nd|rd|th)?[ \-/]+{MONTHS}[ \-/,]+\d{{4}}|{MONTHS}[ ]+\d{{1,2}}(?:st|nd|rd|th)?,?[ ]+\d{{4}}"
        rf"|\d{{1,2}}[./-]\d{{1,2}}[./-]\d{{2,4}}|\d{{4}}-\d{{2}}-\d{{2}})")
# A DOB needs an explicit birth cue within ~40 characters; ordinary dates are never touched.
DOB_RE = re.compile(rf"\b(?:d\.?[ ]?o\.?[ ]?b\.?|date[ \t]+of[ \t]+birth|birth[ \t]*date|born(?:[ \t]+on)?)\b"
                    rf"[^\n\d]{{0,40}}?({DATE})", re.I)


def detect_dob(text):
    for m in DOB_RE.finditer(text):
        yield Span(m.start(1), m.end(1), "DOB", 0.95, "regex+context", key=re.sub(r"\W", "", m.group(1)).lower())


# --- Addresses: postal-code (or state+country) anchor, extended back over address lines ---
STATES = ("Andhra Pradesh|Arunachal Pradesh|Assam|Bihar|Chhattisgarh|Goa|Gujarat|Haryana|Himachal Pradesh|"
          "Jharkhand|Karnataka|Kerala|Madhya Pradesh|Maharashtra|Manipur|Meghalaya|Mizoram|Nagaland|Odisha|"
          "Punjab|Rajasthan|Sikkim|Tamil Nadu|Telangana|Tripura|Uttar Pradesh|Uttarakhand|West Bengal|"
          "New Delhi|NCT of Delhi|Delhi|Chandigarh|Puducherry|Jammu and Kashmir|Ladakh|India|United States|USA|"
          "U\\.S\\.A\\.|United Kingdom|UK|Singapore|Germany|Japan|Mexico|China")
PIN_RE = re.compile(r"(?<![\w,.₹/-])[1-9]\d{2}[ ]?\d{3}(?!\d|[.,]\d|%|[ ]?(?:million|crore|lakh|equity|shares))", re.I)
US_ZIP_RE = re.compile(r"\b[A-Z]{2}[ ]+\d{5}(?:-\d{4})?\b")
STATE_TAIL_RE = re.compile(rf"(?<=[\w)])[ \t]*,?[ \t]*\(?(?:{STATES})\)?[ \t]*,?[ \t]*India\b")
ADDR_TAIL_RE = re.compile(rf"(?:[ \t]*[,\-–]?\s*\(?(?:{STATES})\)?(?![\w]))+")
ADDR_LABEL_RE = re.compile(
    r"(?i)(?:office|address|premises|situated|located|residing|resident|facility|plant|warehouse|branch)"
    r"[ \t]*(?:is[ \t]+)?(?:at|:)[ \t]*|:[ \t]*|\bat[ \t]+(?=\d)")
ADDR_KEYWORDS = re.compile(
    r"(?i)\b(?:floor|building|bldg|tower|wing|block|plot|gat|survey|sector|road|marg|street|lane|nagar|colony|"
    r"society|apartments?|flat|house|bhavan|bhawan|chambers?|complex|centre|center|park|estate|plaza|village|"
    r"taluka|district|dist|near|opp|opposite|behind|campus|level|unit|midc|peth|chowk|bunglow|bungalow|"
    r"residency|station|highway|cantonment)\b")
UNIT_NO_RE = re.compile(r"\b[A-Z]-\d+\b|\b\d+/\d+\b|(?i:\bno\.?)[ \t]*\d|^[ \t]*\d+[\w/-]*[ \t]*,")
BUILDING_LINE_RE = re.compile(r"(?!.*\b(?:Division|Department|Branch|Group|Desk)\b)[ \t]*(?:The[ \t]+)?[A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){0,2}[ \t]*$")
ABBREV_END = re.compile(r"(?i)(?:\b(?:no|nos|st|rd|opp|nr|dist|tal|bldg|sr|dr|mr|ms|mrs|smt|shri|co|pvt|ltd|flat)|\b[A-Z])$")


def _address_like(line):
    return len(line) < 200 and bool(ADDR_KEYWORDS.search(line) or UNIT_NO_RE.search(line))


def _address_start(text, anchor):
    """Walk back from the anchor over address lines, then to the last label / sentence boundary."""
    line_start = text.rfind("\n", 0, anchor) + 1
    lines = 0
    while line_start > 0 and lines < 4:
        prev_start = text.rfind("\n", 0, line_start - 1) + 1
        prev = text[prev_start:line_start - 1]
        if not (prev.rstrip().endswith(",") or _address_like(prev)):
            # a short building-name line between an organisation line and the street line ("The Capital")
            above = text[text.rfind("\n", 0, max(prev_start - 1, 0)) + 1:max(prev_start - 1, 0)]
            if prev_start and BUILDING_LINE_RE.match(prev) and re.search(ORG_SUFFIX + r"[ \t]*$", above):
                line_start = prev_start
            break
        line_start, lines = prev_start, lines + 1
    region = text[line_start:anchor]
    cut = region.rfind(";") + 1
    for m in re.finditer(r"\. ", region):
        if not ABBREV_END.search(region[:m.start()]):
            cut = max(cut, m.end())
    lab = None
    for lab in ADDR_LABEL_RE.finditer(region, cut):
        pass
    start = line_start + (lab.end() if lab else cut)
    while start < anchor and text[start] in " ,()\t\n\u201c\"":
        start += 1
    return start


def detect_address(text):
    anchors = [(m, 0.9) for m in PIN_RE.finditer(text)] + [(m, 0.9) for m in US_ZIP_RE.finditer(text)]
    anchors += [(m, 0.7) for m in STATE_TAIL_RE.finditer(text)]
    for m, conf in anchors:
        if m.re is PIN_RE and not re.search(r"[A-Za-z]{3,}\)?[ \t,]*[-–]?[ \t]*$", text[max(0, m.start() - 12):m.start()]):
            continue  # a PIN follows a place name ("Pune – 411 045"), unlike codes such as "M-140388"
        end = m.end()
        if m.re is not STATE_TAIL_RE:
            t = ADDR_TAIL_RE.match(text, end)
            if t and t.group().count("\n") <= 1:
                end = t.end()
        start = _address_start(text, m.start())
        parts = text[start:m.start()]
        if not re.search(r"[A-Za-z]", parts) or (parts.count(",") < 1 and len(parts) > 60 and "\n" not in parts):
            continue
        if m.re is STATE_TAIL_RE:  # no postal code: require real street-level detail
            if len(set(k.lower() for k in ADDR_KEYWORDS.findall(parts))) < 2 or not re.search(r"\d", parts):
                continue
        elif parts.count(",") < 2:
            conf = 0.7
        yield Span(start, end, "ADDRESS", conf, "postcode+context" if conf > 0.7 else "address-context",
                   review=conf < 0.9, key=re.sub(r"\W", "", text[start:end]).lower())


# --- Organisations: legal-suffix grammar + NER, filtered by the public-body policy ---
SUFFIX_WORDS = r"(?:limited|ltd|llp|trust|inc|incorporated|corporation|corp|llc|plc|gmbh|huf|associates|foundation)"
ORG_SUFFIX = (r"(?i:private\s+limited|pvt\.?\s*ltd\.?|limited|ltd\.?|l\.l\.p\.|llp|inc\.?|incorporated|corporation|"
              r"corp\.?|llc|plc|pte\.?\s*ltd\.?|family\s+trust|huf|associates|foundation|co\.|"
              r"laboratories|electricals|sangathna|sangathan)(?!\w)")  # firm-type / trade-union words
ORG_SHORT_SUFFIX = r"(?:AB|AG|NV|BV|Oyj?|SpA|S\.p\.A\.|S\.A\.|B\.V\.|N\.V\.|N\.A\.|GmbH|KG)(?!\w)"  # case-sensitive
ORG_TOKEN = rf"(?:(?!(?i:{SUFFIX_WORDS})\b)[A-Z0-9][\w’'.&\-]*|\((?:India|[A-Z][\w ]{{0,20}})\)|&)"
ORG_RE = re.compile(rf"(?:{ORG_TOKEN}[ \t]+(?:(?:and|of|for|de|the)[ \t]+)?)*{ORG_TOKEN}"
                    rf"(?:,?\s+{ORG_SUFFIX}|[ \t]+{ORG_SHORT_SUFFIX})")
ORG_LEAD_STOP = re.compile(
    r"^(?:(?i:the|of|and|by|to|from|with|for|in|our|its|their|further|also|as|at|on|between|including|namely|viz\.?|"
    r"said|such|each|all|any|m/s\.?|a|an|is|was|that|which|through|under|upon|into|whereas|pursuant|subsequently|"
    r"accordingly|hence|thus|additionally|moreover|however|we|us|name|named|formerly|entities|entity|promoter|"
    r"corporate|subsidiary|company|companies|group|member|members|customer|customers|supplier|suppliers|lender|"
    r"lenders|client|clients|auditors?|registrar)[ \t]+)+")
# Government bodies, statutory authorities, regulators, courts and "... of India" public-sector bodies.
PUBLIC_BODY = re.compile(
    r"(?i)government|ministry|department|reserve bank|securities and exchange board|\bsebi\b|registrar of companies|"
    r"tribunal|court|authority|commission|municipal|directorate|council|police|customs|development corporation|"
    r"\brbi\b|\bnclt\b|\bgoi\b|central board|state of|republic of|\broc\b|parliament|\bof\s+india\b|"
    r"institute of chartered")
# Stock exchanges, depositories and NPCI are market-infrastructure institutions with statutory regulatory
# roles; policy: treated like regulators and left unchanged.  See README "Company-name policy".
MII = re.compile(r"(?i)\bbse\b|\bnse\b|\bnsdl\b|\bcdsl\b|\bnpci\b|stock\s+exchange|metal\s+exchange|depositor(?:y|ies)|"
                 r"clearing\s+corporation|payments\s+corporation")
GENERIC_CORE = {"india", "indian", "the", "group", "private", "public", "company", "entities", "global", "international"}
SUFFIX_NORM = re.compile(r"(?i)\s*,?\s*\b(?:(?:private|pvt\.?)\s*(?:limited|ltd\.?)|limited|ltd\.?|l\.l\.p\.|llp|inc\.?|"
                         r"incorporated|corporation|corp\.?|llc|plc|gmbh|n\.a\.|ab|ag|nv|bv|spa|kg)$")


def org_core(name):
    s = re.sub(r"(?i)\(formerly[^)]*\)", "", name).strip()
    s = SUFFIX_NORM.sub("", s)
    return re.sub(r"[^\w&]+", " ", s).strip().lower()


def is_public_body(name):
    """Government bodies, regulators and market-infrastructure institutions are never redacted."""
    return bool(PUBLIC_BODY.search(name) or MII.search(name))


COUNSEL_RE = re.compile(r"(?i:legal[ \t]+counsel[ \t]+to[ \t]+(?:our[ \t]+company|the[ \t]+(?:brlms|book[ \t]+running"
                        r"[ \t]+lead[ \t]+managers|selling[ \t]+shareholders?))(?:[ \t]+as[ \t]+to[ \t]+[a-z ]+?[ \t]+law)?)"
                        r"[ \t:\-–]*\n?[ \t]*([A-Z][\w&.’'-]*(?:[ \t]+[A-Z&][\w&.’'-]*){0,5})")
UNIT_AFTER_RE = re.compile(r"[ \t]+([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){0,2})(?=[.;]|\n|$)")


def detect_org_rules(text, common=frozenset()):
    for m in COUNSEL_RE.finditer(text):              # "Legal Counsel to our Company as to Indian Law <Firm>"
        yield Span(m.start(1), m.end(1), "ORG", 0.9, "label", key=org_core(m.group(1)))
    for m in ORG_RE.finditer(text):
        s, start = m.group(), m.start()
        lead = ORG_LEAD_STOP.match(s)
        if lead:
            s, start = s[lead.end():], start + lead.end()
        # "Escrow Collection Bank ABCD Bank Limited": drop heading words before an acronym
        toks = s.split()
        for i, tok in enumerate(toks[1:], 1):
            if tok.isupper() and tok.isalpha() and len(tok) >= 2:
                if all(t.lower() in common for t in toks[:i]):
                    cut = s.index(tok, len(" ".join(toks[:i])))
                    s, start = s[cut:], start + cut
                break
        core = org_core(s)
        if not re.search(r"[a-z]{2}", core) or set(core.split()) <= GENERIC_CORE or len(s.split()) < 2 \
                or is_public_body(s) or re.match(r"\s+of\s+India\b", text[start + len(s):]):
            continue
        yield Span(start, start + len(s), "ORG", 0.9, "legal-suffix", key=core)
        # a business-unit / brand name printed right after the legal name ("X Limited Acme Copper.")
        u = UNIT_AFTER_RE.match(text, start + len(s))
        if u and u.group(1).split()[0].lower() not in common:
            yield Span(u.start(1), u.end(1), "ORG", 0.85, "unit-name", key=org_core(u.group(1)))


# --- People ---
PERSON_STOP = re.compile(
    r"(?i)\b(?:offer|equity|shares?|board|company|limited|committee|director|officer|secretary|promoter|registrar|"
    r"regulations?|act|rules|bank|price|investors?|section|schedule|risk|financial|india|indian|annexure|chapter|"
    r"stock|exchange|fund|trust|plant|unit|facility|road|marg|nagar|statement|note|table|total|fiscal|crore|"
    r"million|ltd|llp|management|capital|group|compliance|kmp|chairman|chairperson|sebi|icdr|brlms?|qib|rii|nii|"
    r"asba|upi|capex|ebitda|gst|branch|taluka|village|district|tower|floor|centre|center|park|industrial|"
    r"wires?|winding|copper|aluminium|motors?|electricals?|private|public|report|policy|scheme|plan|family|"
    r"depository|participant|house|showroom|chambers|apartment)\b")
NAME_SPLIT = re.compile(r"[ \t]*(?:/|,|;|\band\b|&)[ \t]*")
CONTACT_CUT = re.compile(r"[ \t]+(?:Website|E-?mail|Email|Tel|Telephone|Phone|Fax|Mobile|SEBI|CIN|DIN)\b.*$", re.S)
HONORIFIC = r"(?:Mr|Ms|Mrs|Dr|Shri|Smt|Prof)\.?"
NAME_WORD = r"[A-Z][a-z.]*(?![A-Za-z])"
PERSON_LABEL_RE = re.compile(
    r"(?i:contact[ \t]+person|signed[ \t]+by|attention|attn\.?)[ \t]*[:\-–][ \t]*"
    rf"((?:[A-Z][a-z]+(?:[ \t]+{NAME_WORD}){{1,4}})(?:[ \t]*/[ \t]*[A-Z][a-z]+(?:[ \t]+{NAME_WORD}){{1,4}})*)")
HONORIFIC_RE = re.compile(rf"\b{HONORIFIC}[ \t]+([A-Z][a-z]+(?:[ \t]+{NAME_WORD}){{0,3}})")
EDGE_WORD = re.compile(r"(?i)(?:by|of|and|to|from|the|for|with|in|at|on|as|being|namely|dated)\s+")
EDGE_WORD_END = re.compile(r"(?i)\s+(?:by|of|and|to|from|the|for|with|in|at|on|as|being|aggregating|dated)$")
SCHEME_AFTER = re.compile(r"(?i)^.{0,40}\b(?:yojana|scheme|mission|abhiyan|mahabhiyan|gram)\b")
PLACE_BEFORE = re.compile(r"(?i)(?:\bat|\bin|located at|situated at)[ \t]*$")


def person_key(s):
    s = re.sub(rf"(?i)^{HONORIFIC}\s+", "", s.strip())
    return re.sub(r"[^a-z ]", "", re.sub(r"\s+", " ", s.lower())).strip()


def _name_parts(text, start, end, conf, source, review):
    """Split 'A B / C D' lists into one PERSON span per name; trim contact labels and symbols."""
    chunk = CONTACT_CUT.sub("", text[start:end])
    pos = start
    for piece in NAME_SPLIT.split(chunk):
        i = text.find(piece, pos)
        if i < 0:
            continue
        pos = i + len(piece)
        m = re.search(r"[A-Za-z].*[A-Za-z.]", piece)
        while m and EDGE_WORD.match(m.group()):
            m = re.compile(r"[A-Za-z].*[A-Za-z.]").search(piece, m.start() + EDGE_WORD.match(m.group()).end())
        while m and EDGE_WORD_END.search(m.group()):
            m = re.compile(r"[A-Za-z].*[A-Za-z.]").search(piece[:m.start() + EDGE_WORD_END.search(m.group()).start()], m.start())
        if not m:
            continue
        p = m.group().rstrip(".") if m.group().count(".") == 1 and m.group().endswith(".") else m.group()
        if len(p.split()) >= 2 and not PERSON_STOP.search(p) and not any(c.isdigit() for c in p):
            j = i + m.start()
            yield Span(j, j + len(p), "PERSON", conf, source, review, key=person_key(p))


def detect_person_rules(text):
    for m in PERSON_LABEL_RE.finditer(text):
        yield from _name_parts(text, m.start(1), m.end(1), 0.9, "label", False)
    for m in HONORIFIC_RE.finditer(text):
        yield from _name_parts(text, m.start(1), m.end(1), 0.85, "honorific", False)


def detect_ner(texts, nlp, common=frozenset(), acronyms=frozenset()):
    """spaCy PERSON/ORG per segment.  ALL-CAPS text is title-cased first (same length, so offsets hold).
    PERSON hits are dropped when every word is an ordinary lower-case word of this document, when a word is
    an acronym inside mixed-case text, when the hit follows 'at/in' (a place), or precedes a scheme name."""
    prepared, caps_flags = [], []
    for t in texts:
        letters = [c for c in t if c.isalpha()]
        caps = bool(letters) and sum(c.isupper() for c in letters) / len(letters) > 0.7
        caps_flags.append(caps)
        prepared.append(t.title() if caps and len(t.title()) == len(t) else t)
    out = []
    for text, caps, doc in zip(texts, caps_flags, nlp.pipe(prepared, batch_size=64)):
        spans = []
        for e in doc.ents:
            words = e.text.split()
            if e.label_ == "PERSON":
                if not all(w[:1].isupper() for w in words if w[:1].isalpha()) \
                        or sum(w.lower().strip(".,") in common for w in words) >= max(1, len(words) - 1) \
                        or any(w.upper() in acronyms for w in words if len(w) >= 3) \
                        or (not caps and any(w.isupper() and len(w) >= 3 and w.isalpha() for w in words)) \
                        or PLACE_BEFORE.search(text[:e.start_char]) or SCHEME_AFTER.match(text[e.end_char:]):
                    continue
                spans += _name_parts(text, e.start_char, e.end_char, 0.75, "spacy", True)
            elif e.label_ == "ORG" and not is_public_body(e.text) and org_core(e.text):
                # NER-only organisations (no legal suffix) are low confidence: reported, not redacted by default
                spans.append(Span(e.start_char, e.end_char, "ORG", 0.4, "spacy", review=True, key=org_core(e.text)))
        out.append(spans)
    return out


NAME_TOKEN = r"(?:[A-Z][a-z]+|[A-Z]{2,}|(?:[A-Z]\.){1,3}|[A-Z]\.?)"
CAP_SEQ_RE = re.compile(rf"(?<![\w’'])(?:{NAME_TOKEN})(?:[ \t]+(?:{NAME_TOKEN})){{1,3}}(?![\w’'])")
INITIALS = re.compile(r"^(?:[A-Z]{1,3}|(?:[A-Z]\.){1,3}|[A-Z]\.)$")


def detect_person_tokens(text, name_tokens, common=frozenset()):
    """Capitalised word runs built from name words of already-confirmed people, e.g. 'Asha Vikram Rao',
    'DM Rao' (initials + known surname) or a table cell 'Neel Arun Rao'."""
    for m in CAP_SEQ_RE.finditer(text):
        toks = m.group().split()
        kind = ["known" if t.lower() in name_tokens else "init" if INITIALS.match(t) else "other" for t in toks]
        # a run that fills a whole table cell / line keeps its leading words ("Neel Arun Rao")
        whole_cell = not text[text.rfind("\n", 0, m.start()) + 1:m.start()].strip() \
            and re.match(r"[*^&#\s]*(?:\n|$)", text[m.end():]) and kind[-1] == "known"
        while toks and kind[-1] != "known":
            toks, kind = toks[:-1], kind[:-1]
        while toks and kind[0] == "other" and not whole_cell:
            toks, kind = toks[1:], kind[1:]
        if len(toks) < 2 or len(toks) > 4:
            continue
        i = text.index(toks[0], m.start())
        j = m.start() + m.group().rindex(toks[-1]) + len(toks[-1])
        known, others = kind.count("known"), [t for t, k in zip(toks, kind) if k == "other"]
        ok = known >= 2 or (known >= 1 and not others and "init" in kind) or \
            (known >= 1 and whole_cell and len(others) <= 2 and not any(o.lower() in common for o in others))
        if ok and not PERSON_STOP.search(text[i:j]):
            yield Span(i, j, "PERSON", 0.7, "name-tokens", key=person_key(text[i:j]))


GENERIC_ALIAS = re.compile(r"(?i)^(?:the\s+)?(?:company|issuer|registrar|brlms?|book running lead managers?|"
                           r"auditors?|statutory auditors?|promoters?|bank|lenders?|client|offer|we|us|our|subsidiary|"
                           r"holding company|joint venture|jv|stock exchanges?|report|group (?:entities|companies)|"
                           r"promoter group|entities|[\W\d]+)$")
ALIAS_RE = re.compile(r"[ \t]*\((?:the[ \t]+|formerly[ \t]+)?[“\"]([^”\"\n]{2,60})[”\"]", re.I)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def resolve_overlaps(spans):
    """Overlap rule: higher type priority wins; then the longer span; then higher confidence;
    then the earlier start.  Losers are dropped entirely (no partial spans)."""
    ranked = sorted(spans, key=lambda s: (-PRIORITY[s.type], -(s.end - s.start), -s.confidence, s.start))
    taken = []
    for s in ranked:
        if all(s.end <= t.start or s.start >= t.end for t in taken):
            taken.append(s)
    return sorted(taken, key=lambda s: s.start)


def _streams(segments):
    """Group consecutive paragraph segments of the same part; other segments stand alone."""
    streams, last_part = [], None
    for seg in segments:
        kind = seg.sid.split("|")[1]
        if kind in ("p", "a") and seg.part == last_part:
            streams[-1].append(seg)
        else:
            streams.append([seg])
        last_part = seg.part if kind in ("p", "a") else None
    return streams


def _common_words(texts):
    """Words that occur in lower case somewhere in the document: evidence that a capitalised
    occurrence is an ordinary word (heading, defined term) rather than a name."""
    return frozenset(w for t in texts for w in re.findall(r"(?<![\w@./-])[a-z][a-z'’]+(?![\w@/-]|\.\w)", t))


def _is_capitalised(s):
    return all(w[:1].isupper() or not w[:1].isalpha() for w in s.split())


def detect_all(segments, nlp=None, min_confidence=0.5):
    """Return {sid: [Span]} (non-overlapping, sorted).  Spans carry `cont=True` when they are
    the continuation of a value that started in a previous paragraph."""
    streams = _streams(segments)
    texts = ["\n".join(s.text for s in st) for st in streams]
    common = _common_words(texts)
    found = [[] for _ in streams]
    for i, t in enumerate(texts):
        for fn in (detect_email, detect_url, detect_phone, detect_ssn, detect_credit_card, detect_ip,
                   detect_dob, detect_address, detect_person_rules):
            found[i] += list(fn(t))
        found[i] += list(detect_org_rules(t, common))
    if nlp is not None:  # NER per paragraph, mapped to stream offsets
        flat = [(i, off, seg) for i, st in enumerate(streams) for off, seg in _offsets(st)]
        # acronyms: words printed in capitals but never in title case anywhere (PAT, CAGR; not KUSHAL)
        upper = {w for t in texts for w in re.findall(r"\b[A-Z]{3,}\b", t)}
        titled = {w.upper() for t in texts for w in re.findall(r"\b[A-Z][a-z]{2,}\b", t)}
        acr = frozenset(upper - titled)
        for (i, off, seg), spans in zip(flat, detect_ner([seg.text for _, _, seg in flat], nlp, common, acr)):
            for s in spans:
                s.start += off
                s.end += off
                found[i].append(s)

    # document-level propagation: confident names, their core names and defined aliases everywhere
    gaz, acronyms, name_tokens = {}, {}, set()

    def add_alias(alias, key):
        alias = alias.strip()
        if not GENERIC_ALIAS.match(alias) and not is_public_body(alias) \
                and not all(w.lower() in common for w in re.findall(r"[A-Za-z]+", alias)):
            gaz.setdefault(re.sub(r"\s+", " ", alias.lower()), ("ORG", key, 0.85))

    for t, spans in zip(texts, found):
        for s in spans:
            surface = t[s.start:s.end]
            if s.type == "ORG" and s.confidence >= 0.9:
                gaz.setdefault(re.sub(r"\s+", " ", surface.lower()), ("ORG", s.key, 0.9))
                core = org_core(surface)
                if len(core) >= 4 and (" " in core or len(core) >= 6) and not is_public_body(core):
                    gaz.setdefault(core, ("ORG", s.key, 0.85))
                first = surface.split()[0]
                if first.isupper() and first.isalpha() and len(first) >= 3 and not is_public_body(first):
                    acronyms.setdefault(first, s.key)          # "XYZ International Limited" -> "XYZ"
                elif nlp is not None and first.istitle() and first.isalpha() and len(first) >= 5 \
                        and first.lower() not in common and not nlp.vocab.has_vector(first.lower()):
                    acronyms.setdefault(first, s.key)          # distinctive brand word, e.g. "Zentrova"
                a = ALIAS_RE.match(t, s.end)
                if a:
                    add_alias(a.group(1), s.key)
            elif s.type == "PERSON" and s.confidence >= 0.75 and len(s.key.split()) >= 2:
                gaz.setdefault(s.key, ("PERSON", s.key, 0.8))
                name_tokens.update(w for w in s.key.split() if len(w) >= 3)
        # glossary tables: a short term cell followed by a cell that is exactly an organisation name
        lines = t.split("\n")
        offs = [0]
        for ln in lines:
            offs.append(offs[-1] + len(ln) + 1)
        orgs = {(s.start, s.end): s for s in spans if s.type == "ORG" and s.confidence >= 0.9}
        for k in range(len(lines) - 1):
            term, nxt = lines[k].strip(), lines[k + 1].strip()
            if not term or len(term.split()) > 3 or not nxt:
                continue
            a = offs[k + 1] + lines[k + 1].index(nxt)
            o = orgs.get((a, a + len(nxt)))
            initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", nxt)).upper()
            if o and (nxt.lower().startswith(term.lower()) or term.upper() == initials):
                add_alias(term, o.key)
    # hyphenated short form of a two-word organisation name: "I-Sec" for "Icici Securities Limited"
    for k, (typ, key, conf) in list(gaz.items()):
        w = org_core(k).split()
        if typ == "ORG" and conf >= 0.9 and len(w) >= 2 and len(w[1]) >= 3:
            gaz.setdefault(f"{w[0][0]}-{w[1][:3]}", ("ORG", key, 0.85))
    name_tokens -= common
    for i, t in enumerate(texts):
        found[i] += list(detect_person_tokens(t, name_tokens, common))
        # family-branch labels name a person by first name alone: "<first name> Branch"
        found[i] += [Span(m.start(), m.end(), "PERSON", 0.8, "branch-label", key=m.group().lower())
                     for m in re.finditer(r"\b[A-Z][a-z]+(?=[ \t]+Branch\b)", t) if m.group().lower() in name_tokens]
        for s in found[i]:
            if s.type == "PERSON" and s.source == "name-tokens":
                gaz.setdefault(s.key, ("PERSON", s.key, 0.8))
    if gaz:
        names = sorted(gaz, key=len, reverse=True)
        pat = re.compile(r"(?<![\w@./])(?:" + "|".join(re.escape(n).replace(r"\ ", r"\s+") for n in names) + r")(?![\w@])", re.I)
        for i, t in enumerate(texts):
            for m in _overlapping(pat, t):
                norm = re.sub(r"\s+", " ", m.group().lower())
                typ, key, conf = gaz.get(norm) or gaz.get(person_key(norm)) or (None, None, 0)
                if typ and _is_capitalised(m.group()):
                    found[i].append(Span(m.start(), m.end(), typ, conf, "gazetteer", key=key))
    if acronyms:
        pat = re.compile(r"(?<![\w@./])(?:" + "|".join(map(re.escape, acronyms)) + r")(?![\w@])")  # case-sensitive
        for i, t in enumerate(texts):
            for m in pat.finditer(t):
                found[i].append(Span(m.start(), m.end(), "ORG", 0.8, "alias", key=acronyms[m.group()]))

    # URLs are kept unless they contain the name of a redacted organisation
    org_names = {w for k, (typ, _, _) in gaz.items() if typ == "ORG" for w in org_core(k).split()
                 if len(w) >= 4 and w not in common and w not in GENERIC_CORE} | {a.lower() for a in acronyms}
    for spans in found:
        spans[:] = [s for s in spans if s.type != "URL" or
                    (any(n in s.key for n in org_names) and not PUBLIC_DOMAIN.search(s.key))]

    result = {}
    for st, t, spans in zip(streams, texts, found):
        spans = [_trim(t, s) for s in spans if s.confidence >= min_confidence and s.end > s.start]
        # an address never starts with the organisation name printed in front of it
        orgs = [s for s in spans if s.type == "ORG" and s.source in ("legal-suffix", "gazetteer")]
        for a in (s for s in spans if s.type == "ADDRESS"):
            for o in sorted(orgs, key=lambda o: o.start):
                if o.start <= a.start < o.end or (a.start <= o.start < a.end and not re.search(r"\d", t[a.start:o.end])):
                    a.start = o.end
                    while a.start < a.end and t[a.start] in " ,()\t\n":
                        a.start += 1
        kept = resolve_overlaps([s for s in spans if s.end > s.start])
        for off, seg in _offsets(st):
            segspans = []
            for s in kept:
                a, b = max(s.start, off), min(s.end, off + len(seg.text))
                if a < b and seg.text[a - off:b - off].strip():
                    c = copy.copy(s)
                    c.start, c.end, c.cont = a - off, b - off, a > s.start
                    segspans.append(_trim(seg.text, c))
            result[seg.sid] = segspans
    return result


def _overlapping(pat, text):
    pos = 0
    while True:
        m = pat.search(text, pos)
        if not m:
            return
        yield m
        pos = m.start() + 1


def _offsets(stream):
    off = 0
    for seg in stream:
        yield off, seg
        off += len(seg.text) + 1


def _trim(text, s):
    while s.start < s.end and text[s.start] in " \t\n,;:":
        s.start += 1
    while s.end > s.start and text[s.end - 1] in " \t\n,;:":
        s.end -= 1
    return s


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------
def _replacement(typ, n):
    if typ == "EMAIL":
        return f"redacted.email{n:03d}@example.com"      # RFC 2606 reserved domain
    if typ == "IP_ADDRESS":
        return f"192.0.2.{n % 254 + 1}"                    # RFC 5737 documentation range
    return f"[{typ}_{n:03d}]"


def apply_redactions(segments, spans_by_sid):
    """Patch XML text nodes in place.  The value->token registry lives only in memory."""
    registry, counters = {}, Counter()
    for seg in segments:
        for s in spans_by_sid.get(seg.sid, []):
            if (s.type, s.key) not in registry:
                counters[s.type] += 1
                registry[(s.type, s.key)] = _replacement(s.type, counters[s.type])
    for seg in segments:
        for s in sorted(spans_by_sid.get(seg.sid, []), key=lambda s: s.start, reverse=True):
            _patch(seg, s.start, s.end, "" if getattr(s, "cont", False) else registry[(s.type, s.key)])
    return counters


def _patch(seg, start, end, repl):
    if not hasattr(seg, "_cur"):
        seg._cur = [seg.text[a:b] for a, b, _ in seg.pieces]
    hit = [i for i, (a, b, setter) in enumerate(seg.pieces) if a < end and b > start and setter]
    for n, i in enumerate(hit):
        a, b, setter = seg.pieces[i]
        cur, ls = seg._cur[i], max(start - a, 0)
        head = cur[:ls] + (repl if n == 0 else "")
        seg._cur[i] = head + cur[end - a:] if end <= b else head
        setter(seg._cur[i])
    # separators (tabs / breaks) inside a redacted span are left in place so layout is preserved


# --------------------------------------------------------------------------
# Embedded media: OCR, detect, and redact PII inside the image itself
# --------------------------------------------------------------------------
ID_DOC_RE = re.compile(r"(?i)date\s*of\s*birth|\bdob\b|permanent\s*account\s*number|unique\s*identification|aadha+r|"
                       r"passport|driving\s*licen[cs]e|income\s*tax\s*department|election\s*commission|"
                       r"govt\.?\s*of\s*india|government\s*of\s*india|father'?s?\s*name")
NATIONAL_ID_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b|\b\d{4}\s?\d{4}\s?\d{4}\b")
PUBLIC_URL = re.compile(r"(?i)\.gov\b|\.nic\.in|sebi|rbi\.org|bseindia|nseindia")


def _media_alt_text(pkg):
    """{media part: [alt text, ...]} from drawing descr/title attributes and their image relationships."""
    out = defaultdict(list)
    R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    for part, root in pkg.trees.items():
        rels_name = re.sub(r"^(.*/)?([^/]+)$", r"\1_rels/\2.rels", part)
        if rels_name not in pkg.trees:
            continue
        rid = {r.get("Id"): r.get("Target") for r in pkg.trees[rels_name].iter(f"{{{REL}}}Relationship")}
        for doc_pr in root.iter(f"{{{WP}}}docPr"):
            alt = " ".join(filter(None, (doc_pr.get("descr"), doc_pr.get("title"))))
            for blip in doc_pr.getparent().iter(f"{{{A}}}blip"):
                target = rid.get(blip.get(f"{{{R}}}embed"), "")
                out["word/" + target.lstrip("/").replace("word/", "")].append(alt)
    return out


def process_media(pkg, text_detect, doc_names, ocr=True):
    """OCR each raster image and redact in the pixels.  Rules, in order:
    identity documents -> whole image blacked out (faces, signatures and QR codes are not text);
    logos / brand marks naming a redacted organisation -> whole image blanked;
    QR codes with a non-public payload -> blanked; other PII text lines -> black boxes."""
    report = []
    alt = _media_alt_text(pkg)
    engine = None
    if ocr:
        try:
            from rapidocr_onnxruntime import RapidOCR
            engine = RapidOCR()
        except Exception:
            engine = None
    from PIL import Image, ImageDraw
    names = [n for n in doc_names if len(_compact(n)) >= 3]
    for name in pkg.media():
        ext = Path(name).suffix.lower()
        entry = {"part": name, "bytes": len(pkg.raw[name])}
        if ext not in IMAGE_EXT:
            entry.update(status="NOT INSPECTED", reason=f"unsupported media type {ext}")
            report.append(entry)
            continue
        try:
            img = Image.open(io.BytesIO(pkg.raw[name]))
            img.load()
        except Exception as exc:
            entry.update(status="NOT INSPECTED", reason=f"cannot decode image ({type(exc).__name__})")
            report.append(entry)
            continue
        if engine is None:
            entry.update(status="NOT INSPECTED", reason="OCR disabled or unavailable")
            report.append(entry)
            continue
        rgb = img.convert("RGB")
        lines = engine(_np(rgb))[0] or []
        text = "\n".join(l[1] for l in lines)
        flat = _compact(text)
        hits = []
        for box, line, _score in lines:
            types = {s.type for s in text_detect(line)}
            if NATIONAL_ID_RE.search(line):
                types.add("NATIONAL_ID")
            types |= {doc_names[n] for n in names if _compact(n) in _compact(line)}
            if types:
                hits.append((box, types))
        qr = _qr_payload(rgb)
        id_signals = len(ID_DOC_RE.findall(text)) + len(NATIONAL_ID_RE.findall(text))
        org_mark = any(doc_names[n] == "ORG" and _compact(n) in flat for n in names)
        logo = any("logo" in a.lower() for a in alt.get(name, []))
        entry.update(size=list(img.size), ocr_lines=len(lines), ocr_chars=len(text),
                     pii_lines_by_type=dict(Counter(t for _, ts in hits for t in ts)), qr_code=bool(qr))
        draw = ImageDraw.Draw(rgb)
        if id_signals >= 2:
            draw.rectangle([0, 0, rgb.width, rgb.height], fill="black")
            entry.update(status="REDACTED (whole image)", reason="identity document: text PII plus photo, "
                         "signature and QR code, which OCR line boxes cannot cover")
        elif (logo or org_mark) and len(text) < 60:
            draw.rectangle([0, 0, rgb.width, rgb.height], fill="#d9d9d9")
            entry.update(status="REDACTED (whole image)", reason="logo / brand mark of a redacted organisation ("
                         + ("alt text says logo" if logo else "OCR matched a redacted organisation name") + ")")
        elif qr and not PUBLIC_URL.search(qr):
            draw.rectangle([0, 0, rgb.width, rgb.height], fill="#d9d9d9")
            entry.update(status="REDACTED (whole image)", reason="QR code with a non-public payload")
        elif hits:
            for box, _ in hits:
                xs, ys = [p[0] for p in box], [p[1] for p in box]
                draw.rectangle([min(xs) - 3, min(ys) - 3, max(xs) + 3, max(ys) + 3], fill="black")
            entry.update(status="REDACTED (text regions)", reason=f"{len(hits)} OCR line(s) with PII blacked out")
        else:
            entry.update(status="inspected, no PII found", reason="OCR text contained no detectable PII")
            report.append(entry)
            continue
        out = io.BytesIO()
        fmt = "JPEG" if ext in (".jpg", ".jpeg") else "PNG" if ext == ".png" else (img.format or "PNG")
        (rgb if fmt == "JPEG" or img.mode in ("RGB", "L") else rgb.convert(img.mode)).save(out, fmt)
        pkg.raw[name] = out.getvalue()
        report.append(entry)
    return report


def _np(img):
    import numpy as np
    return np.asarray(img)


def _qr_payload(img):
    """QR payload string ('' if none).  Used only for the decision; never logged or returned."""
    try:
        import cv2
        data, _, _ = cv2.QRCodeDetector().detectAndDecode(_np(img))
        return data or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _load_nlp(model):
    try:
        import spacy
        return spacy.load(model, disable=["lemmatizer"])
    except OSError:
        sys.exit(f"error: spaCy model '{model}' is not installed. Run: python -m spacy download {model}")
    except ImportError:
        sys.exit("error: spaCy is not installed. Run: pip install -r requirements.txt")


def run(input_path, output_path, model="en_core_web_lg", min_confidence=0.5, ocr=True):
    pkg = Package(input_path)
    nlp = _load_nlp(model)
    spans = detect_all(pkg.segments, nlp, min_confidence)

    # OCR text is checked with the same detectors plus the names this document's text redacted
    doc_names = {}
    for seg in pkg.segments:
        for s in spans.get(seg.sid, []):
            if s.type in ("ORG", "PERSON") and not s.cont:
                surface = re.sub(r"\s+", " ", seg.text[s.start:s.end])
                doc_names.setdefault(surface.lower(), s.type)
                if s.type == "ORG":                      # logos print short forms: core name, leading acronym
                    doc_names.setdefault(org_core(surface), "ORG")
                    first = surface.split()[0]
                    if first.isupper() and first.isalpha() and len(first) >= 3:
                        doc_names.setdefault(first.lower(), "ORG")

    def image_detect(line):
        out = []
        for fn in (detect_email, detect_phone, detect_ssn, detect_credit_card, detect_ip, detect_dob,
                   detect_address, detect_org_rules, detect_person_rules):
            out += list(fn(line))
        return out

    counters = apply_redactions(pkg.segments, spans)
    media = process_media(pkg, image_detect, doc_names, ocr)
    pkg.save(output_path)
    flat = [s for v in spans.values() for s in v if not getattr(s, "cont", False)]
    return {
        "input_parts": len(pkg.raw), "text_segments": len(pkg.segments),
        "segments_by_kind": dict(Counter(s.sid.split("|")[1] for s in pkg.segments)),
        "spans_by_type": dict(Counter(s.type for s in flat)),
        "unique_values_by_type": dict(counters),
        "review_flagged_spans": sum(s.review for s in flat),
        "spans_by_source": dict(Counter(s.source for s in flat)),
        "media": media, "unparsed_parts": pkg.skipped,
        "min_confidence": min_confidence, "ner_model": model,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="source .docx (never modified)")
    ap.add_argument("-o", "--output", help="redacted .docx path (default: output/<name>_redacted.docx)")
    ap.add_argument("--report", help="JSON summary path (counts only, no source values)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing output .docx and/or report")
    ap.add_argument("--no-ocr", action="store_true", help="skip OCR of embedded images (reported as not inspected)")
    ap.add_argument("--min-confidence", type=float, default=0.5, help="drop detections below this (default 0.5)")
    ap.add_argument("--model", default="en_core_web_lg", help="spaCy model for NER (default en_core_web_lg)")
    a = ap.parse_args(argv)

    src = Path(a.input)
    if not src.is_file():
        sys.exit(f"error: input not found: {src}")
    if not zipfile.is_zipfile(src) or "word/document.xml" not in zipfile.ZipFile(src).namelist():
        sys.exit(f"error: not a .docx (WordprocessingML) package: {src}")
    out = Path(a.output) if a.output else Path("output") / f"{src.stem.replace(' ', '_')}_redacted.docx"
    rep = Path(a.report) if a.report else out.with_suffix(".report.json")
    # every path check runs before any processing or writing
    if out.suffix.lower() != ".docx":
        sys.exit("error: output must end in .docx")
    for p, what in ((out, "output"), (rep, "report")):
        if same_file(p, src):
            sys.exit(f"error: {what} path resolves to the input file; the original is never overwritten")
        if p.exists() and not p.is_file():
            sys.exit(f"error: {what} path exists and is not a regular file: {p}")
    if same_file(rep, out):
        sys.exit("error: report path resolves to the output .docx path")
    existing = [str(p) for p in (out, rep) if p.exists()]
    if existing and not a.force:
        sys.exit(f"error: {', '.join(existing)} exists (use --force to overwrite)")
    out.parent.mkdir(parents=True, exist_ok=True)
    rep.parent.mkdir(parents=True, exist_ok=True)

    summary = run(src, out, a.model, a.min_confidence, not a.no_ocr)
    atomic_write(rep, lambda tmp: Path(tmp).write_text(json.dumps(summary, indent=2)))
    print(f"wrote {out}")
    print(f"wrote {rep}")
    print("redacted spans by type:", json.dumps(summary["spans_by_type"]))
    for m in summary["media"]:
        print(f"media {m['part']}: {m['status']}" + (f" ({m.get('reason')})" if m.get("reason") else ""))


if __name__ == "__main__":
    main()
