# Evaluation report

All numbers below come from fresh `evaluate.py` runs (2026-09-26) on the supplied prospectus
(`output/metrics.json`, `output/qa.json`) and from the redactor's own report
(`output/Red_Herring_Prospectus_redacted.report.json`). Nothing here quotes source values.

## 1. Source inventory

| Item | Count |
|---|---|
| Package parts / XML parts parsed | 168 / 155 (none failed) |
| Text segments extracted | 4,674: 4,215 paragraphs (body, tables, text boxes, 75 headers), 263 field codes, 196 alt-text/title attributes |
| Characters across all segments | 334,951 (286,667 non-whitespace) |
| Characters of `w:t` text in the package | 324,898, all covered by paragraph segments (checked by summing every `w:t` node in every part) |
| Tables / rows / cells | 76 / 878 / 3,225 |
| Drawings / text boxes / sections | 88 (11 in the body, 77 in headers) / 156 / 85 |
| Raster images | 8 (no EMF/WMF/SVG/OLE) |
| Footnotes, endnotes, comments, charts, custom XML, external links | none present |
| Core/app document properties | present but empty |

Notes on the earlier unreviewed inventory:
- **Character count.** Its ~373,000-character figure could not be reproduced by this extractor. The
  counts above are what the package contains.
- **Emails.** The 52 email-shaped strings are the 52 emails in visible text. Each also appears in a
  hidden `HYPERLINK "mailto:…"` field code, which gives 104 reviewed email occurrences (27 distinct
  values).
- **Phones.** The 51 loosely matched phone candidates reduce to 36 reviewed phone occurrences (22 distinct).

The DOCX covers the prospectus only up to *Capital Structure*: its last text paragraph is roughly
page 84 of the original page numbering. Two photographs of identity cards are appended after that
point. The document contains no dates of birth, SSNs, card numbers or IP addresses in its text.

## 2. Gold-label method

1. **Guidelines.** Every mention of the nine types is annotated. The ORG policy is the one in the
   README: private entities are annotated, while government bodies, regulators, "… of India"
   public-sector bodies and market-infrastructure institutions are not. Newspaper titles and
   government scheme names are not annotated.
   - **Addresses:** every specific postal address, government offices included. The span runs from
     the first street-level element (department or building line included) to the postal code,
     state and country. A continuation line in the next paragraph is annotated as its own span.
   - **Names and entities split across paragraphs** are annotated as one span per paragraph piece.
   - **Boundaries:** a leading "The" and trailing footnote symbols are excluded.
2. **Pre-annotation and full manual reading.** The detectors pre-annotated the text. I then read every
   segment of the extracted document in full, including field codes and alt text. The 75 headers'
   text was reviewed as the list of its distinct strings (section titles, page fields, text-box
   names). All 8 images were reviewed visually. After the detectors were revised, every segment that carried a
   predicted span or had changed since the first pass (518 segments) was re-read in full.
3. **Corrections** were recorded as explicit edits: 2 false positives removed and 22 missed spans
   added. The misses were:
   - 8 first names used as family-branch labels
   - 7 short-form mentions of a BRLM
   - a law firm, a partnership firm, a supplier's business unit
   - a standards company and its abbreviation
   - a trade-union name containing the issuer's name
   - a building-name address line
4. **Stored form.** `gold/gold_labels.json` holds `[segment id, start, end, type]` plus the source
   file's SHA-256. There is no source text. `evaluate.py` refuses to run against a different file.

**Gold set: 748 spans.** 301 ORG, 221 PERSON, 104 EMAIL, 86 ADDRESS, 36 PHONE; 0 SSN, CREDIT_CARD,
IP_ADDRESS and DOB.

## 3. Span-level detection metrics (fresh run, 2026-09-26)

A true positive needs an exact match of segment, start, end **and** type. A correct span with the
wrong type counts as one FP and one FN. Precision = TP/(TP+FP), recall = TP/(TP+FN),
F1 = 2PR/(P+R). If a denominator is zero the metric is undefined and shown as **n/a**. F1 is n/a when
P or R is n/a, and 0 when P = R = 0. Overall figures are micro-averaged over all spans.

| Type | Gold | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| EMAIL | 104 | 104 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| PHONE | 36 | 36 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| SSN | 0 | 0 | 0 | 0 | n/a | n/a | n/a |
| CREDIT_CARD | 0 | 0 | 0 | 0 | n/a | n/a | n/a |
| IP_ADDRESS | 0 | 0 | 0 | 0 | n/a | n/a | n/a |
| PERSON | 221 | 221 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| ORG | 301 | 301 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| ADDRESS | 86 | 86 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| DOB | 0 | 0 | 0 | 0 | n/a | n/a | n/a |
| **Overall (micro)** | **748** | **748** | **0** | **0** | **1.0000** | **1.0000** | **1.0000** |

**Read these as training-set scores, not as expected performance.** Section 3a lists the rule that
closed each miss. Every one was written after seeing the misses in this document, and the gold set
describes the same document. SSN, CREDIT_CARD, IP_ADDRESS and DOB do not occur in it, so this
document gives no evidence about those detectors. Only the synthetic self-test (section 6) exercises
them.

### 3a. Review of the previous run's 22 false negatives and 2 false positives

The previous run (baseline, section 7) had 726 TP, 2 FP and 22 FN. Each item was reviewed against
its gold span, the surrounding text and the README policy.

| # | Baseline error | Policy decision | Change |
|---|---|---|---|
| 1–4 | PERSON FN: 4 first names in the family-branch definition row | In scope: here a first name identifies a member of the promoter family | `<first name> Branch` is tagged when the first name belongs to an already-confirmed person (`branch-label`). "Parents Branch" is not a name and is kept. |
| 5–8 | PERSON FN: the same 4 first names as table-heading cells | as above | same rule |
| 9–15 | ORG FN: 7 × a BRLM's hyphenated short form in the responsibility table | In scope (private BRLM) | The hyphenated short form of a confirmed two-word organisation name (first initial + first 3 letters of the second word) is added to the document gazetteer |
| 16 | ORG FN: law firm after "Legal Counsel to our Company as to Indian Law" | In scope (private firm) | Label rule for `Legal Counsel to our Company / the BRLMs / Selling Shareholders [as to … Law]` |
| 17 | ORG FN: partnership firm in a related-party table | In scope | `Electricals` added as a firm-type suffix word |
| 18 | ORG FN: a supplier's business-unit name printed directly after its legal name | In scope | A distinctive capitalised run printed directly after a legal-suffix name, at the end of a sentence or list item, is tagged. Its first word must not be an ordinary word of this document. Source `unit-name`, confidence 0.85, not propagated. |
| 19–20 | ORG FN: standards company and its glossary abbreviation | In scope (private company) | `Laboratories` added as a firm-type suffix word; the existing glossary rule then derives the abbreviation |
| 21 + FP 1 | ORG FN + boundary FP: a trade-union name containing the issuer's name, of which only the issuer part was tagged | In scope | `Sangathna` / `Sangathan` added as trade-union suffixes; the full union name now wins over the shorter issuer-name hit |
| 22 | ADDRESS FN: building-name line between a bank's name line and its street line | In scope | A short title-case line is included in the address when a legal-suffix organisation line sits directly above it. Department lines (`… Division`, `… Department`, `… Branch`) are excluded because the gold does not treat them as part of the address. A first version tagged two department lines; the evaluator caught this before the fix. |
| FP 2 | PERSON FP: two words of a government scheme title (a glossary expansion containing `Gram`) tagged by NER | Not PII: scheme name, kept by policy | `gram` added to the scheme-word filter for NER person hits |

The public-body policy was not broadened. Government bodies, `… of India` public-sector bodies and
market-infrastructure institutions are still kept. The self-test asserts that State Bank of India and
Reserve Bank of India are not tagged.

## 4. Character-level accuracy (fresh run)

Every non-whitespace character of every extracted segment gets a binary label: PII if it lies inside
any gold span. Separately, it is "predicted PII" if it lies inside any predicted span of the nine
types. The redactor's extra `URL` replacements are not counted as predicted PII.

| TP | FP | FN | TN | Total | Accuracy = (TP+TN)/Total |
|---:|---:|---:|---:|---:|---:|
| 16,983 | 0 | 0 | 269,684 | 286,667 | **1.00000** |

Consistency checks:
- The gold PII character count (TP+FN = 16,983) equals the baseline's 16,796 + 187, so both runs were
  scored against the same gold set.
- With 748/748 exact span matches and no extra spans, character-level FP = FN = 0 follows.

A redactor that flags nothing would score 269,684 / 286,667 = 0.9408 accuracy, because only 5.9% of
non-whitespace characters are PII. Span-level recall is the better measure of residual exposure.

## 5. Output QA (fresh run, separate from the detection metrics)

| Check | Result |
|---|---|
| Source identity | The input's SHA-256 is `8b5c93f7642d659e64b51be9f6172c86c2825417f376ca1800ed331515e6f929` (1,844,676 bytes). It equals `source_sha256` in `gold/gold_labels.json`, checked by hand and again by `evaluate.py`, which refuses a mismatch. The hash was identical before and after regeneration. |
| Regeneration | `output/Red_Herring_Prospectus_redacted.docx` and its report were regenerated from that source with `redact_docx.py --force`. They were not patched. |
| Residual search, extracted text | All 278 distinct gold values were searched in memory, whitespace-tolerant and case-insensitive, in every output segment. **Occurrences beyond the source's own non-PII contexts: 0 for every type.** Four values still match, and each was checked by hand: `Limited` / `Private Limited` in policy-kept public and MII names and in the prose "private limited company"; `Maharashtra, India` in two prose sentences; `the capital` in ordinary prose. None of these occurrences is an address or an entity mention. |
| Residual search, raw package XML | The same search over the tag-stripped XML and `.rels` of every part found 0 excess occurrences. A verbatim search of the un-stripped, entity-decoded XML finds only the same generic values (`Limited`, `Maharashtra, India`). Every PERSON, EMAIL and PHONE value was found 0 times. |
| Structure | Paragraph, table, row, cell, drawing, section, text-box and field-code counts are identical in source and output: 4,864 / 76 / 878 / 3,225 / 88 / 85 / 156 / 263. Both have the same 168 parts and 8 media files. |
| Package | `zipfile.testzip()` passes; all 155 XML parts parse, with no failures. |
| Media | All 8 images changed. RapidOCR finds 0 characters and 0 PII hits in every redacted image. |
| Metadata | Core/app properties are empty in the source and stay empty. There are no custom properties and no external relationships. |
| Rendering | Microsoft Word exported both files to PDF via COM (`--render`). Source: 128 pages; output: 125 pages, because shorter tokens reflow text. The script deleted the source PDF. |
| Visual review | Output pages reviewed at 100 dpi: the cover (QR code, issuer logo, registered-office table), the glossary (family-branch labels), the supplier list (unit name), the intermediaries pages (legal counsel, bankers, a kept department line, the building-name address) and the ID-card page. Tables, colours and layout are preserved. A replaced multi-paragraph value leaves empty continuation paragraphs. A text scan of the header region of all 125 pages found page-top text only on the cover and table pages; the residual search above covers the 75 header parts. Source pages were not re-rendered for side-by-side comparison in this run. |
| Original file | Unchanged: its SHA-256 was the value above before and after every run. The self-test checks the same for its synthetic inputs. |

### Image review (by item; separate from text metrics)

The table below comes from the manual content review done during gold labelling. In this run the 8
regenerated images were checked by OCR: no characters remain in any of them. The ID-card page and
the cover's QR code and logo were also inspected in the output render and are fully blanked. The
source images were not re-opened in this run.

| Image | Content | Reviewed PII items | Action | Items remaining |
|---|---|---|---|---|
| image1.jpeg | QR code on cover | links to a non-public URL (quasi-identifier) | blanked | 0 |
| image1.png, image2.png | issuer logo | ORG ×1 each | blanked (alt text "logo") | 0 |
| image2.jpeg, image3.jpeg | BRLM logos | ORG ×1 each | blanked (OCR match / alt text) | 0 |
| image3.png | registrar logo | ORG ×2 text lines | blanked (OCR match) | 0 |
| image4.png | photo of a PAN card | PERSON ×3, DOB ×1, PAN number ×1, ADDRESS ×1, PHONE ×2, EMAIL ×1, face photo, QR code | whole image blacked out | 0 |
| image5.png | photo of an Aadhaar card | PERSON ×4 (English and Hindi), DOB ×1, Aadhaar number ×2, ADDRESS ×2, PHONE ×2, EMAIL ×1, face photo, QR code | whole image blacked out | 0 |

Whole-image blanking also removes non-PII printed headers (over-redaction), so precision is not
meaningful for these images.

## 6. Self-check (`test_redact.py`)

The fixtures are synthetic only: example.org addresses, test card numbers and made-up names. NER is
not loaded in the self-test; the evaluation run exercises the spaCy path. It covers:
- **Detectors.** A positive and a negative case for each of the nine types, plus the new ORG and
  ADDRESS rules.
- **A generated DOCX** containing:
  - a value split across formatting runs, and an organisation split across two paragraphs;
  - a table, a header and a footer;
  - a complex and a simple field code, and core-property metadata;
  - SSN, card, IP and DOB values;
  - an OCR-readable PNG, a text-free PNG and an EMF.

  The test checks extracted text and raw XML for leaks, consistent tokens, OCR of the redacted
  pixels, identical structure counts and part lists, and unchanged source bytes.
- **CLI path safety.** Each of these must be refused before `run()` is called, with nothing written:
  - output or report resolving to the input: the same path, via `..`, via Windows case, or via a
    hard link;
  - report resolving to the output;
  - a directory as the output;
  - a non-`.docx` output.
- **Overwrite and atomic writes.** An existing output or report is refused without `--force` and
  overwritten with it. A failure injected during the report write must leave the old report
  byte-identical, with no temp file behind.

Result in the clean environment: `all checks passed`. **Skipped:** the symlink-alias case, because
Windows refused to create a symlink without the symlink privilege. `same_file` resolves symlinks with
`Path.resolve()`, but that path was not exercised on this machine.

## 7. Baseline (previous run, kept for comparison, not re-verified)

Before this run, `output/` held reports from an earlier run of the previous code. Copies were kept
outside the deliverable folder. Their values:
- span level: TP 726 / FP 2 / FN 22 (P 0.9973, R 0.9706, F1 0.9837);
- character level: TP 16,796 / FP 9 / FN 187 / TN 269,675;
- QA excess occurrences: ORG 12, PERSON 8, ADDRESS 1.

Before any code change, the unchanged detectors were rerun on the same source and gold. They
reproduced exactly the same 22 FN and 2 FP.

## 8. Reproduction log (2026-09-26)

Environment: Windows 11, Python 3.11.4, a fresh venv with `pip install -r requirements.txt`. The
install exited 0, `pip check` was clean, and every pin installed as specified, including
`en_core_web_lg-3.8.0`.

| Command (from `submission/`) | Result |
|---|---|
| `python test_redact.py` | all checks passed (symlink case skipped, see section 6) |
| `python redact_docx.py "../Red Herring Prospectus.docx" --force` | exit 0. Spans (first pieces only): ORG 295, PERSON 221, EMAIL 104, ADDRESS 51, PHONE 36, URL 47. 8/8 images redacted. |
| `python evaluate.py metrics "../Red Herring Prospectus.docx" --gold gold/gold_labels.json --out output/metrics.json` | exit 0; see section 3 |
| `python evaluate.py qa "../Red Herring Prospectus.docx" output/Red_Herring_Prospectus_redacted.docx --gold gold/gold_labels.json --out output/qa.json --render <scratch dir>` | exit 0; see section 5 |

SHA-256 of the regenerated artefacts:

| File | SHA-256 |
|---|---|
| redacted DOCX | `66e9e46180bfc1b92d5d4290d7aa3698901a567ce242cc7f50d89fbe466e926c` |
| report | `b9562a6f916f87074f4d764d6f40cadef94f63d53bdd4e8ac12d5a2783a54ea6` |
| `metrics.json` | `f688843beac96c5dbf11e4cf188e11c81043e426dea2d16a853dfa0e2fba39b8` |
| `qa.json` | `cfe320f3d5df422cacb7bf23cbe4c261db1dd36ec9e27f8a79b116bf8a4d3482` |

The whole pipeline was run twice. The DOCX, report and `metrics.json` were byte-identical both
times. `qa.json` had identical values, but its per-type keys came out in a different order, because
Python's set iteration order varies between runs. Its hash can therefore differ between runs even
when every value is the same.

## 9. Limitations and unresolved cases

- **No held-out test set; the fresh scores are fitted.** Detectors and gold were developed on the
  same document. The same person wrote the detectors and produced the gold set, starting from
  detector pre-annotations. The rules in section 3a were added specifically to close this document's
  misses. The perfect scores show that the known misses are fixed, not that the redactor generalises.
  Expect lower scores on a new prospectus, especially for ORG and PERSON mentions that have no
  suffix, label or repetition.
- **Borderline rules that may over-redact on other documents:**
  - The hyphenated short-form alias and the unit-name rule could tag an unrelated capitalised word.
  - The building-line rule depends on an organisation line directly above it.
  - The `Electricals` / `Laboratories` suffixes tag any capitalised run ending in those words.
- **Exact-match, per-segment scoring** counts each paragraph piece of a split value separately and
  penalises boundary differences fully.
- **Left visible by policy:**
  - Identifiers outside the nine types: CIN, company registration number, SEBI registration numbers,
    director identification numbers (DINs).
  - Public-body and market-infrastructure names and URLs.
  - Department names such as "Capital Market Division".
  - These can still help re-identify the issuer.
- **Not inspected:** vector or OLE media (none present here). Text inside images is covered only as
  the image rules describe.
- **Temporary files.** This run's source-side diagnostics were printed to the terminal only. The Word
  render deleted its source PDF, and only pages of the redacted output were rasterised, in a session
  scratch directory outside the deliverable folder. No source-to-replacement mapping was ever written
  to disk.
