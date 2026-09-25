# Evaluation report

All numbers below come from `evaluate.py` runs on the supplied prospectus
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

## 3. Span-level detection metrics

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
| PERSON | 221 | 213 | 1 | 8 | 0.9953 | 0.9638 | 0.9793 |
| ORG | 301 | 288 | 1 | 13 | 0.9965 | 0.9568 | 0.9763 |
| ADDRESS | 86 | 85 | 0 | 1 | 1.0000 | 0.9884 | 0.9942 |
| DOB | 0 | 0 | 0 | 0 | n/a | n/a | n/a |
| **Overall (micro)** | **748** | **726** | **2** | **22** | **0.9973** | **0.9706** | **0.9837** |

Error breakdown:
- **PERSON FP:** a name inside a government scheme title.
- **PERSON FN:** 8 standalone first names in family-branch labels.
- **ORG FP:** a boundary error. Only the issuer-name part of a trade-union name was tagged, which
  also counts as one ORG FN.
- **ORG FN (the other 12):** 7 × BRLM short form, law firm, partnership firm, supplier business unit,
  standards company and its abbreviation.
- **ADDRESS FN:** a building-name line above an address block.

SSN, CREDIT_CARD, IP_ADDRESS and DOB do not occur in this document. Their metrics are undefined, so
this document gives no evidence about those detectors. They are exercised only by the synthetic
self-test (`test_redact.py`), which covers validation and the context rules.

## 4. Character-level accuracy

Every non-whitespace character of every extracted segment gets a binary label: PII if it lies inside
any gold span, and separately "predicted PII" if it lies inside any predicted span of the nine types.
The redactor's extra `URL` replacements are not counted as predicted PII.

| TP | FP | FN | TN | Total | Accuracy = (TP+TN)/Total |
|---:|---:|---:|---:|---:|---:|
| 16,796 | 9 | 187 | 269,675 | 286,667 | **0.99932** |

Supplementary character-level scores: precision 0.99946, recall 0.98899, F1 0.99420.

**Why accuracy looks so high:** only 5.9% of non-whitespace characters are PII. A redactor that
flags nothing would score (TN + FP)/Total = 269,684 / 286,667 = 0.9408 accuracy. Accuracy is
therefore dominated by the non-PII majority. The span-level recall (0.9706) and the character-level
recall (0.98899) are the better measures of residual exposure.

## 5. Output QA (separate from the detection metrics)

| Check | Result |
|---|---|
| Residual search | All 278 distinct gold values were searched, in memory, in every output segment and in the tag-stripped raw XML of every part. **EMAIL 0, PHONE 0** occurrences remain. Occurrences beyond those in non-PII contexts of the source: **ORG 12, PERSON 8, ADDRESS 1**. A cross-check confirmed these are exactly the 21 missed gold spans, each still present in its segment. The 22nd miss (trade union) is partially replaced. |
| Structure | Paragraph, table, row, cell, drawing, section, text-box and field-code counts are identical between source and output. The zip integrity test passes; all 155 XML parts parse. |
| Media | All 8 images changed. OCR of the redacted images finds 0 characters in each. |
| Metadata | Core/app properties are empty in the source and remain so. There are no custom properties or external relationships. |
| Rendering | Both files were exported to PDF with the installed Microsoft Word via COM. Source: 128 pages; output: 125 pages, because shorter tokens reflow text. |
| Visual review | Contact sheets of 71 output pages; 7 pages at readable resolution. 5 of them were compared side by side with the source: pages 1, 2, 3, 5 and the auditors table. The director address table and the ID-card page were reviewed in the output only. Tables, colours, headers and page furniture are preserved. Narrow stacked-letter columns appear identically in the source (a PDF-to-Word conversion artefact) and are not caused by redaction. Multi-line addresses collapse to one token followed by blank paragraphs. |
| Original file | Unchanged (the CLI never writes to the input; the self-test asserts this). |

### Image review (manual, by item; separate from text metrics)

| Image | Content | Reviewed PII items | Action | Items remaining |
|---|---|---|---|---|
| image1.jpeg | QR code on cover | links to a non-public URL (quasi-identifier) | blanked | 0 |
| image1.png, image2.png | issuer logo | ORG ×1 each | blanked (alt text "logo"; OCR could not read the stylised mark) | 0 |
| image2.jpeg, image3.jpeg | BRLM logos | ORG ×1 each | blanked (OCR match / alt text) | 0 |
| image3.png | registrar logo | ORG ×2 text lines | blanked (OCR match) | 0 |
| image4.png | photo of a PAN card | PERSON ×3 (holder, father, signature), DOB ×1, PAN number ×1, ADDRESS ×1, PHONE ×2, EMAIL ×1, face photo, QR code | whole image blacked out | 0 |
| image5.png | photo of an Aadhaar card | PERSON ×4 (holder, father; English and Hindi), DOB ×1, Aadhaar number ×2, ADDRESS ×2 (English and Hindi), PHONE ×2, EMAIL ×1, face photo, QR code | whole image blacked out | 0 |

For image4.png and image5.png, OCR alone would not have been enough: it lost word spacing, could not
read Devanagari, and cannot cover photos or signatures. That is why identity documents are blanked
entirely. Whole-image blanking also removes non-PII printed headers (over-redaction), so
precision is not meaningful for these images.

## 6. Limitations and unresolved cases

- **No held-out test set.** Detectors and gold were developed on the same document, and the gold set
  was produced by the same person who wrote the detectors, starting from detector pre-annotations.
  Both effects favour the detectors. Expect lower scores on a new prospectus, especially for ORG and
  PERSON mentions without suffixes, labels or repetition.
- **Exact-match, per-segment scoring** counts each paragraph piece of a split value separately and
  penalises boundary differences fully.
- **Unresolved leaks (left visible in the output):** the 21 missed spans above, and the remaining
  words of the trade-union name.
- **Left visible by policy:**
  - Identifiers outside the nine types: CIN, company registration number, SEBI registration numbers,
    director identification numbers (DINs).
  - Public-body names and URLs.
  - These can still help re-identify the issuer.
- **Not inspected:** vector or OLE media (none present here). Text rendered inside images is covered
  only as described in the image rules.
- **Temporary files.** The manual review needed the extracted source text, the source images and a
  source rendering in a session scratch directory. They were overwritten and deleted after the review
  (best effort: overwriting cannot guarantee erasure on SSDs or other copy-on-write storage). No
  source-to-replacement mapping was ever written to disk.
