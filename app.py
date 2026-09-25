"""Streamlit front end for redact_docx.run():  streamlit run app.py

The upload and the redacted output live in a temporary directory that is deleted when processing ends,
successfully or not. Nothing about the document (contents or filename) is logged or shown back.
"""
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

import redact_docx

# load the spaCy model once per server process instead of on every upload
redact_docx._load_nlp = st.cache_resource(show_spinner=False)(redact_docx._load_nlp)

st.set_page_config(page_title="DOCX redaction")
st.title("DOCX PII redaction")
upload = st.file_uploader("Upload a .docx", type=["docx"])

if upload and st.button("Redact"):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp, "input.docx"), Path(tmp, "redacted.docx")
            src.write_bytes(upload.getvalue())
            if not zipfile.is_zipfile(src) or "word/document.xml" not in zipfile.ZipFile(src).namelist():
                raise ValueError("not a .docx")
            with st.spinner("Redacting…"):
                summary = redact_docx.run(src, out)
            data = out.read_bytes()
    except (Exception, SystemExit):  # _load_nlp calls sys.exit(); never echo exception text (may quote content)
        st.error("Redaction failed. Check that the file is a valid .docx and try again.")
    else:
        st.success("Done. Redacted spans by type:")
        st.json(summary["spans_by_type"])
        st.download_button("Download redacted .docx", data, file_name="redacted.docx", on_click="ignore",
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
