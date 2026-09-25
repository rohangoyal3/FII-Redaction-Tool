# PII Redaction Tool

A Python tool for redacting personally identifiable information (PII) from Word `.docx` documents. It includes a command-line redactor, a Streamlit upload page, an evaluation script, and a self-check.

**Live demo:** [Open the app](https://YOUR-RENDER-SERVICE.onrender.com)

## Features

- Upload a `.docx` in the web app and download its redacted version.
- Run redaction from the command line.
- Generate a JSON report for a redaction run.
- Evaluate results against the project’s gold labels.
- Review evaluation metrics and QA results in `output/`.

The categories covered and the redaction policy are described in the project code and `EVALUATION.md`.

## Run locally

Use the Python version recorded in `EVALUATION.md`.

Create and activate a virtual environment on Windows:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Start the web app:

```powershell
streamlit run app.py
```

## Command-line use

See the available options:

```powershell
python redact_docx.py --help
```

Example, if the input document is provided as the first argument:

```powershell
python redact_docx.py input.docx --output output/redacted.docx --report output/report.json
```

Use the exact argument format shown by `--help`. Keep the original document separate from the output and report paths.

## Evaluation and self-check

Run the self-check:

```powershell
python test_redact.py
```

See the evaluator’s available options:

```powershell
python evaluate.py --help
```

Evaluation methodology, metrics, QA checks, and known limitations are documented in `EVALUATION.md`. To reproduce the evaluation, use the matching source document identified there by its SHA-256 hash.

## Project files

```text
.
├── app.py
├── redact_docx.py
├── evaluate.py
├── test_redact.py
├── requirements.txt
├── README.md
├── EVALUATION.md
├── gold/
│   └── gold_labels.json
└── output/
    ├── Red_Herring_Prospectus_redacted.docx
    ├── Red_Herring_Prospectus_redacted.report.json
    ├── metrics.json
    └── qa.json
```

The original prospectus and the local `.venv/` environment are not included.

## Deploy on Render

The app is deployed as a Render web service connected to this GitHub repository. The service uses:

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `streamlit run app.py --server.address 0.0.0.0 --server.port $PORT --server.headless true`

Pushes to the connected GitHub branch trigger deployments. See [Render’s web service documentation](https://render.com/docs/web-services).

## Privacy

Use synthetic or otherwise non-sensitive documents in the public demo. Do not upload the original, unredacted prospectus to this repository or the demo.

Gold labels and reports can contain text derived from the source document. Review them before sharing publicly, and only keep them in a public repository if their contents are approved for public release.
