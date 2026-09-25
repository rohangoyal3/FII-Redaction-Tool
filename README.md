# PII Redaction Tool

A Python command-line tool for redacting personally identifiable information (PII) from Word `.docx` documents. It produces a redacted document and a JSON report. The project also includes gold annotations, an evaluator, and a self-check.

## Contents

- `redact_docx.py` — redacts a Word document.
- `evaluate.py` — evaluates redaction results against the gold annotations.
- `test_redact.py` — runs the project’s self-check.
- `gold/gold_labels.json` — annotated reference labels used for evaluation.
- `output/` — generated redacted document, report, metrics, and QA results.
- `EVALUATION.md` — evaluation method, results, checks, and limitations.
- `requirements.txt` — Python package dependencies.

## Requirements

Use the Python version recorded in `EVALUATION.md`.

Install the dependencies in a virtual environment:

```bash
python -m venv .venv
```

On Windows, activate it with:

```powershell
.venv\Scripts\Activate.ps1
```

Then install the dependencies:

```bash
python -m pip install -r requirements.txt
```

The `.venv` folder is local to your computer and should not be uploaded to GitHub.

## Use

Check the available command-line options:

```bash
python redact_docx.py --help
```

Example, if the input document is provided as the first argument:

```bash
python redact_docx.py input.docx --output output/redacted.docx --report output/report.json
```

Use the exact argument format shown by `--help`. Choose output and report paths that do not overwrite the source document.

## Run the self-check

```bash
python test_redact.py
```

For evaluator usage and the exact evaluation command, see:

```bash
python evaluate.py --help
```

The evaluation method and results are documented in `EVALUATION.md`. Results depend on using the same source document as the one associated with the gold labels.

## Source document and privacy

The original prospectus is not included in this repository. To reproduce the evaluation, obtain the matching source document privately and verify its SHA-256 hash against the value documented in `EVALUATION.md`.

Before sharing this repository publicly, review `gold/gold_labels.json`, the JSON report, and all generated files. They may contain text derived from the source document. Do not upload the original, unredacted prospectus.

## Limitations

Redaction quality depends on the detection rules, document structure, and project policy described in `EVALUATION.md`. Text embedded in images may need separate review. Check the evaluation and QA reports before relying on an output document.
