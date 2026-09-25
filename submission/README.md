# DOCX PII redaction — Red Herring Prospectus

`redact_docx.py` reads a `.docx`, finds nine PII types across every text-bearing OOXML part, and writes a
redacted copy. It keeps the document's styles, tables, sections, headers and images in place. The original
file is never modified.

| File | Purpose |
|---|---|
| `redact_docx.py` | CLI and library: extraction, detectors, redaction, image sanitising |
| `app.py` | Streamlit web interface: upload a `.docx`, download the redacted copy (calls `redact_docx.run`) |
| `evaluate.py` | span/character metrics against the gold set; output QA (residual search, structure, media, Word rendering) |
| `test_redact.py` | self-check with synthetic values only (`python test_redact.py`): positive and negative cases for all nine types, output/report path collisions, `--force`, atomic report writes, values split across runs and paragraphs, headers, footers, tables, field codes, metadata, image OCR redaction, structure and unchanged source bytes |
| `gold/gold_labels.json` | reviewed gold annotations: segment id, offsets and type only, no source text |
| `output/Red_Herring_Prospectus_redacted.docx` | the redacted prospectus |
| `output/*.json` | redaction report, metrics, QA results (counts only) |
| `EVALUATION.md` | method, metrics, QA results and limitations |

## Usage

```bash
pip install -r requirements.txt          # Python 3.11; includes the spaCy en_core_web_lg model
python redact_docx.py INPUT.docx                          # -> output/INPUT_redacted.docx + .report.json
python redact_docx.py INPUT.docx -o out/red.docx --report out/red.json
```

Options: `--force` overwrites an existing output `.docx` **and/or** report (without it, the run is refused
if either already exists). `--no-ocr` skips image OCR, and the report then marks every image as NOT
INSPECTED. `--min-confidence` sets the detection threshold (default 0.5). `--model` selects the spaCy model.

Safety checks (all run before the document is read or anything is written):
- The input, output and report paths are resolved first. The script refuses to run when the output or
  the report resolves to the input file, or the report resolves to the output `.docx`. The comparison
  covers relative paths and `..`, symlinks, Windows case differences and, for existing files, file
  identity (hard links). `--force` never overrides these checks.
- It refuses an output or report path that exists but is not a regular file (e.g. a directory), and an
  output not ending in `.docx`.
- It exits with a clear message when the input is missing or isn't a WordprocessingML package, and when
  the spaCy model isn't installed.
- Both the `.docx` and the report are written to a temporary file in the target directory and renamed
  into place. On failure the temporary file is deleted and any existing file is left untouched, so a
  half-written document or report is never visible. The `.docx` is written before the report, so a
  failed report write can leave a new `.docx` next to the old report. Re-run with `--force`.
- Terminal output and the JSON report contain only counts, statuses and part names, never source values.

## Web interface

```bash
streamlit run app.py
```

Upload a `.docx`, click **Redact**, download the result. The app calls `redact_docx.run` unchanged, so
detection rules and defaults (OCR on, confidence 0.5, `en_core_web_lg`) are the same as the CLI. The upload
and output are written to a temporary directory that is deleted as soon as processing ends, including on
errors; the page shows only span counts, and errors show a generic message (no exception text, contents or
filename). The spaCy model is loaded once per server process.

**Render** (Web Service, Python 3.11 — set `PYTHON_VERSION=3.11.4`, root directory `submission`):

- Build command: `pip install -r requirements.txt`
- Start command: `streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true`

With `en_core_web_lg` and OCR loaded, the process peaked at about 1.3 GB RAM on a small test file, so a
512 MB instance will run out of memory. Choose a 2 GB instance.

## Reproducing the evaluation

The source prospectus is not part of this folder, because it is the unredacted input. The gold labels
and every metric belong to one exact file:

    SHA-256 8b5c93f7642d659e64b51be9f6172c86c2825417f376ca1800ed331515e6f929   (1,844,676 bytes)

Obtain `Red Herring Prospectus.docx` from whoever issued this task (the original assignment material)
and check its hash (`certutil -hashfile FILE SHA256` on Windows, `sha256sum FILE` elsewhere).
`evaluate.py` refuses to run against any other file. Then, from this folder:

```bash
python test_redact.py
python redact_docx.py "../Red Herring Prospectus.docx" --force
python evaluate.py metrics "../Red Herring Prospectus.docx" --gold gold/gold_labels.json --out output/metrics.json
python evaluate.py qa "../Red Herring Prospectus.docx" output/Red_Herring_Prospectus_redacted.docx --gold gold/gold_labels.json --out output/qa.json --render RENDER_DIR
```

`--render` needs Windows with Microsoft Word. It deletes the source rendering and keeps only the
redacted output's pages. Keep `RENDER_DIR` outside this folder all the same.

## What is inspected

- **Paragraph text.** Every `w:p` in `document.xml`, `header*.xml`, `footer*.xml`, `footnotes.xml`,
  `endnotes.xml`, `comments*.xml` and the glossary. This includes tables and text boxes: nested
  `txbxContent` paragraphs are separate segments, and the `mc:AlternateContent` choice and fallback copies
  are both covered. Text is joined across `w:r` runs (`w:t`, tabs, breaks), so a value split over
  formatting runs is still found.
- **Hidden text.** Field codes (`w:instrText`, `w:fldSimple/@w:instr`, e.g. `HYPERLINK "mailto:…"`) and
  tracked deletions (`w:delText`).
- **Alt text and titles.** On `wp:docPr`, `pic:cNvPr`, VML shapes and image data, and hyperlink tooltips.
- **DrawingML text.** Charts, diagrams and drawings (`a:t`).
- **Metadata.** Every leaf value in `docProps/core.xml`, `app.xml`, `custom.xml` and `customXml/item*.xml`.
- **Relationship targets** with `TargetMode="External"` (hyperlink URLs).
- **Embedded raster images** (`word/media`, `word/embeddings`): OCR with RapidOCR, then redaction in the
  pixels (see below).

Not processed for detection:
- Style, numbering, theme, font and settings parts. They hold no document content, but the QA step still
  searches the raw XML of every part.
- Vector or OLE media (`.emf`, `.wmf`, `.svg`, `.bin`). These are reported as NOT INSPECTED. The
  prospectus contains none.

The supplied file has 168 package parts (155 XML), 76 tables, 75 headers, 74 footers, 88 drawings (11 in the body, 77 in headers) and
8 raster images. It has no footnotes, endnotes, comments, charts, custom properties or external links, and
its core/app properties are empty.

## Detection approach

The detectors run over "streams": the consecutive paragraphs of one part joined with `\n`. A PDF-to-Word
conversion often breaks a name or address over several paragraphs, and the stream still sees it as one
value. Each hit is a typed span with a confidence score, a source label and a review flag. A value that
crosses paragraphs is split back into pieces: the first piece gets the replacement and the others are
emptied.

| Type | Method |
|---|---|
| EMAIL | RFC-style regex. Tolerates the converter's stray space before the TLD. |
| PHONE | International `+cc …` form, or a number after a Tel/Phone/Fax/Mobile label. 8–15 digits. Amounts and dates are excluded by digit-group and lookaround rules. |
| SSN | `ddd-dd-dddd` with the SSA area, group and serial rules (no 000/666/9xx). Without a nearby "SSN/Social Security" label it is flagged for review. |
| CREDIT_CARD | 13–19 digits passing both the Luhn check and an issuer-prefix/length check. |
| IP_ADDRESS | IPv4/IPv6 validated by `ipaddress`. Without an IP/server/host cue it is flagged for review. |
| DOB | A date is tagged only within 40 characters of an explicit cue (`DOB`, `Date of Birth`, `born on`, `birth date`). Other dates are never touched. |
| ADDRESS | Anchored on an Indian PIN code (after a place name), a US ZIP code, or a state + country tail with at least two street-level keywords. Extended backwards over lines that look like addresses, up to the last label (`Office:`, `located at`, …) or sentence boundary. A short building-name line (e.g. `The Samplehouse`) sitting between an organisation-name line and the street line is included; a department line (`… Division`, `… Department`) is not. An organisation name printed in front of the address is excluded. |
| PERSON | spaCy `en_core_web_lg` NER (ALL-CAPS text is title-cased first), plus label rules (`Contact Person:`, honorifics). Then document-wide propagation of confirmed names, name-token runs such as `DM <known surname>` or a table cell made of a known name, and family-branch labels (`<first name> Branch`, when the first name belongs to a confirmed person). NER hits are rejected when they are ordinary words of this document, acronyms, places (`at X`) or scheme names (`… Yojana`, `… Gram …`). |
| ORG | Legal-suffix grammar (Limited, Pvt Ltd, LLP, Inc, Corporation, Co., AB, N.A., Family Trust, HUF, Associates, Foundation, Laboratories, Electricals, trade-union `Sangathna`/`Sangathan`, …) on capitalised token runs. Firm names after a `Legal Counsel to our Company / the BRLMs … as to … Law` label. A distinctive capitalised unit or brand name printed directly after a legal name at a sentence or list end (`… Limited Acme Copper.`). Also document-wide propagation of each company's full name, core name, defined aliases (`(“Acme Research”)`), glossary abbreviations, leading acronyms, distinctive brand words and hyphenated short forms of two-word names (`A-Sam` for "Acme Samples Limited": first initial + first three letters of the second word). spaCy ORG hits without a suffix are low confidence (0.4): reported, not redacted by default. |

**Company-name policy.** Private legal entities are redacted: the issuer, its group companies and trusts,
promoters' companies, customers, suppliers, banks, the book-running lead managers (BRLMs), the registrar,
auditors, the rating agency, family trusts and HUFs.

Left unchanged:
- Government bodies, ministries, statutory authorities, regulators and courts.
- Public-sector bodies whose name is "… of India", such as State Bank of India, Export-Import Bank of
  India and Solar Energy Corporation of India.
- Market-infrastructure institutions with statutory regulatory roles: BSE, NSE, NSDL, CDSL, NPCI and
  clearing corporations. This is a judgement call. To redact them instead, delete the `MII` test in
  `is_public_body`.

Newspaper titles such as Financial Express are publications, not entity names, and are kept. A
company's web address is replaced when it contains that company's name, because otherwise it re-identifies
the redacted company. This happens in addition to the nine PII types and is reported separately as `URL`.

**Overlaps.** Candidates are ranked by type priority, then length (longer first), then confidence, then
position. Priority order: EMAIL = URL > SSN = CREDIT_CARD = IP > PHONE > DOB > ADDRESS > ORG = PERSON.
The best candidate is kept and every candidate overlapping it is dropped, so an email inside an address
stays an email and an address containing a landmark company stays one address.

**Replacements.** Each unique value, normalised per type, gets one synthetic token in order of first
appearance: `[PERSON_007]`, `[ORG_012]`, `[ADDRESS_003]`, `[PHONE_004]`, `redacted.email005@example.com`
(RFC 2606 domain), `192.0.2.x` (RFC 5737). So repeats of the same value, in body text, tables, headers or
field codes, read consistently. Phone numbers are normalised by digits, so `+91 22 1234 5678` and
`+91 22 12345678` share one token. Organisations are normalised by core name. Tokens are counters and
reveal nothing about the original. The value-to-token map exists only in memory for the duration of one
run and is never written anywhere.

**Images.** Each image is OCRed and then handled by the first rule that applies:
1. An identity document (ID-card cues or national-ID number patterns) is blacked out entirely. Photos,
   signatures and QR codes are not text, so OCR line boxes cannot cover them.
2. A logo or brand mark of a redacted organisation (OCR matches a redacted name, or the alt text says
   "logo") is blanked.
3. A QR code whose payload is not a public (government/exchange) URL is blanked.
4. Otherwise, only OCR lines containing PII are boxed out.

## Trade-offs, likely false positives and misses

- **Precision over breadth for organisations.** A name with no legal suffix, firm-type word or label is
  found only when the same entity appears elsewhere with a suffix, a defined alias or a glossary entry.
  Brand-only mentions of a company that is never introduced formally are missed. The rules that closed
  this document's misses (firm-type words, legal-counsel label, unit name after a legal name, `X-Yyy`
  short forms, branch labels, building-name line) were written against this document. They are narrow
  on purpose and untested on other prospectuses.
- **People.** First names used on their own are found only in `<first name> Branch` labels. Names
  printed only in ALL CAPS and never elsewhere depend on NER after title-casing.
- **Addresses.** Coverage is best when a postal code or state + country tail is present. A building-name
  line above an address is included only when an organisation-name line sits directly above it.
  Address spans can over-extend into a department name on the same line; this is treated as part of the
  postal address.
- **Structured types.** Phone numbers without `+` or a label, national IDs such as PAN or Aadhaar in
  body text, and non-Indian postcode formats other than US ZIP are not detected. The document's own
  identifiers are out of scope and left in place: corporate identity number (CIN), SEBI registration
  numbers and director identification numbers (DINs).
- **Layout.** A replacement token sits in the first run of the original value, so a token can be longer
  or shorter than the text it replaces and line breaks may shift slightly. Tabs and breaks inside a
  redacted span are kept.
- **Images.** Whole-image blanking over-redacts (e.g. printed government headers on an ID card) in
  exchange for certainty. OCR misses on stylised logos are covered only by the alt-text rule.
- **Evaluation.** The detectors were developed and evaluated on the same document, so the metrics
  overstate performance on unseen files. See `EVALUATION.md`.
