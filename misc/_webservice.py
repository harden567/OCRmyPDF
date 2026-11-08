# SPDX-FileCopyrightText: 2025 James R. Barlow
# SPDX-FileCopyrightText: 2026 OCRmyPDF contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""This is a simple web service/HTTP wrapper for OCRmyPDF.

This may be more convenient than the command line tool for some Docker users.
Note that OCRmyPDF uses Ghostscript, which is licensed under AGPLv3+. While
OCRmyPDF is under GPLv3, this file is distributed under the Affero GPLv3+ license,
to emphasize that SaaS deployments should make sure they comply with
Ghostscript's license as well as OCRmyPDF's.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

import pikepdf
import pypdfium2
import streamlit as st

from ocrmypdf._defaults import DEFAULT_ROTATE_PAGES_THRESHOLD


def get_host_url_with_port(port: int) -> str:
    """Get the host URL for the web service. Hacky."""
    host_url = st.context.headers["host"]
    try:
        host, _streamlit_port = host_url.split(":", maxsplit=1)
    except ValueError:
        host = host_url
    return f"//{host}:{port}"  # Use the same protocol


def clear_review():
    """Discard only results and widgets belonging to the previous OCR request."""
    st.session_state['ocr_result'] = None
    st.session_state['reviewed_pages'] = set()
    st.session_state['review_notes'] = {}
    for key in list(st.session_state):
        if key.startswith(('review_page_', 'review_note_')) or key == 'confirm_export':
            del st.session_state[key]


def save_review(page_number):
    reviewed = set(st.session_state['reviewed_pages'])
    if st.session_state[f'review_page_{page_number}']:
        reviewed.add(page_number)
    else:
        reviewed.discard(page_number)
    st.session_state['reviewed_pages'] = reviewed


def save_note(page_number):
    st.session_state['review_notes'][page_number] = st.session_state[
        f'review_note_{page_number}'
    ]


def show_page(data, page_number, label):
    """Render a page on demand, keeping temporary native PDF objects bounded."""
    st.subheader(label)
    document = pypdfium2.PdfDocument(data)
    try:
        if page_number > len(document):
            st.info(f"This file has no page {page_number}.")
            return
        page = document[page_number - 1]
        try:
            bitmap = page.render(scale=1.25)
            try:
                st.image(bitmap.to_pil().copy(), width=700)
            finally:
                bitmap.close()
            textpage = page.get_textpage()
            try:
                text = textpage.get_text_range()
            finally:
                textpage.close()
            with st.expander(f"Extracted text — {label}"):
                if text.strip():
                    st.text(text)
                else:
                    st.info("No extractable text was found on this page.")
        finally:
            page.close()
    finally:
        document.close()


def show_review(result):
    st.header("Review result")
    columns = st.columns(4)
    original_size = len(result['input_bytes'])
    output_size = len(result['output_bytes'])
    columns[0].metric("Original size", f"{original_size:,} bytes")
    columns[1].metric("Result size", f"{output_size:,} bytes")
    columns[2].metric("Size change", f"{(output_size / original_size - 1):+.1%}")
    columns[3].metric("Pages", f"{result['input_pages']} → {result['output_pages']}")
    if result['input_pages'] != result['output_pages']:
        st.warning(
            "The original and result have different page counts. "
            "Review the extra pages."
        )
    st.caption(
        "File size and extractable text are review aids, "
        "not a measure of OCR accuracy. "
        "Check important text, stamps and page edges in the previews."
    )
    if result['log']:
        with st.expander("Processing log"):
            st.code(result['log'], language=None)

    total_pages = max(result['input_pages'], result['output_pages'])
    page_number = st.selectbox(
        "Review page", options=list(range(1, total_pages + 1)), key='review_page_number'
    )
    left, right = st.columns(2)
    with left:
        show_page(result['input_bytes'], page_number, "Original")
    with right:
        show_page(result['output_bytes'], page_number, "OCR result")

    reviewed_key = f'review_page_{page_number}'
    note_key = f'review_note_{page_number}'
    st.session_state.setdefault(
        reviewed_key, page_number in st.session_state['reviewed_pages']
    )
    st.session_state.setdefault(
        note_key, st.session_state['review_notes'].get(page_number, '')
    )
    st.checkbox(
        "Mark this page as reviewed",
        key=reviewed_key,
        on_change=save_review,
        args=(page_number,),
    )
    st.text_area(
        "Review notes for this page",
        key=note_key,
        on_change=save_note,
        args=(page_number,),
    )
    st.caption(
        f"Reviewed {len(st.session_state['reviewed_pages'])} of {total_pages} pages."
    )
    confirmed = st.checkbox(
        "I understand the review coverage and want to export this result",
        key='confirm_export',
    )
    left, right = st.columns(2)
    with left:
        st.download_button(
            "Download original PDF",
            data=result['input_bytes'],
            file_name=result['filename'],
            mime='application/pdf',
        )
    with right:
        st.download_button(
            "Download reviewed PDF",
            data=result['output_bytes'],
            file_name=f"{Path(result['filename']).stem}_ocr.pdf",
            mime='application/pdf',
            disabled=not confirmed,
        )


st.set_page_config(layout='wide')
st.title("OCRmyPDF Web Service")
st.session_state.setdefault('ocr_result', None)
st.session_state.setdefault('reviewed_pages', set())
st.session_state.setdefault('review_notes', {})
st.session_state.setdefault('running', False)

uploaded = st.file_uploader("Upload input PDF or image", type=["pdf"], key="file")

mode = st.selectbox("Mode", options=["normal", "skip-text", "force-ocr", "redo-ocr"])

pages = st.text_input(
    "Pages", value="", help="Comma-separated list of pages to process"
)

with st.expander("Input options"):
    invalidate_digital_signatures = st.checkbox(
        "Invalidate digital signatures", value=False, key='invalidate_signatures'
    )
    language = st.selectbox("Language", options=["eng", "deu", "fra", "spa"])

    image_dpi = st.slider(
        "Image DPI", value=300, key="image_dpi", min_value=1, max_value=5000, step=50
    )
with st.expander("Preprocessing"):
    skip_big = st.checkbox("Skip OCR on big pages", value=False, key="skip_big")
    skip_big_mpixels = st.number_input(
        "Skip pages over (MPixels)",
        min_value=0.0,
        max_value=5000.0,
        value=50.0,
        disabled=not skip_big,
    )
    oversample = st.slider("Oversample", min_value=0, max_value=5000, value=0, step=50)
    rotate_pages = st.checkbox("Rotate pages", value=False, key="rotate")
    deskew = st.checkbox("Deskew pages", value=False, key="deskew")
    clean = st.checkbox("Clean pages before OCR", value=False, key="clean")
    clean_final = st.checkbox("Clean final", value=False, key="clean_final")
    remove_vectors = st.checkbox("Remove vectors", value=False, key="remove_vectors")


with st.expander("Output options"):
    output_type = st.selectbox(
        "Output type",
        options=["pdfa", "pdf", "pdfa-1", "pdfa-2", "pdfa-3", "none"],
        key='output_type',
    )

    pdf_renderer = st.selectbox(
        "PDF renderer", options=["auto", "fpdf2", "hocr", "hocrdebug", "sandwich"]
    )

    optimize = st.selectbox("Optimize", options=["0", "1", "2", "3"])

    pdfa_compression = st.selectbox(
        "PDF/A compression",
        options=["auto", "jpeg", "lossless"],
        key='pdfa_compression',
    )

with st.expander("Metadata"):
    title = author = keywords = subject = None
    if uploaded:
        try:
            with (
                pikepdf.open(BytesIO(uploaded.getvalue())) as pdf,
                pdf.open_metadata() as meta,
            ):
                input_pages = len(pdf.pages)
                if not input_pages:
                    raise ValueError("The PDF has no pages.")
                st.code(str(meta), language="xml")
                title = st.text_input("Title", value=str(meta.get('dc:title', '')))
                author = st.text_input("Author", value=str(meta.get('dc:creator', '')))
                keywords = st.text_input(
                    "Keywords", value=str(meta.get('dc:subject', ''))
                )
                subject = st.text_input(
                    "Subject", value=str(meta.get('dc:description', ''))
                )
        except (pikepdf.PdfError, ValueError) as exc:
            clear_review()
            st.error(f"Unable to read the input PDF: {exc}")
            st.stop()


with st.expander("Optimization after OCR"):
    jpeg_quality = st.slider(
        "JPEG quality", min_value=0, max_value=100, value=75, key="jpeg_quality"
    )
    png_quality = st.slider(
        "PNG quality", min_value=0, max_value=100, value=75, key="png_quality"
    )
    jbig2_threshold = st.number_input(
        "JBIG2 threshold", value=0.85, key="jbig2_threshold"
    )

with st.expander("Advanced options"):
    jobs = st.slider(
        "Threads",
        min_value=1,
        max_value=os.cpu_count(),
        value=os.cpu_count(),
        key="threads",
    )
    max_image_mpixels = st.number_input(
        "Max image size",
        value=250.0,
        min_value=0.0,
        help="Maximum image size in megapixels",
    )
    rotate_pages_threshold = st.number_input(
        "Rotate pages threshold",
        value=DEFAULT_ROTATE_PAGES_THRESHOLD,
        min_value=0.0,
        max_value=1000.0,
        help="Threshold for automatic page rotation",
    )
    fast_web_view = st.number_input(
        "Fast web view",
        value=1.0,
        min_value=0.0,
        help="Linearize files above this size in MB",
    )
    continue_on_soft_render_error = st.checkbox(
        "Continue on soft render error", value=True
    )
    verbose_labels = {-1: "quiet", 0: "default", 1: "debug", 2: "debug_all"}
    verbose = st.selectbox(
        "Verbosity level",
        options=[-1, 0, 1, 2],
        index=1,
        format_func=verbose_labels.__getitem__,
    )

if uploaded:
    args = []
    if mode and mode != 'normal':
        args.append(f"--{mode}")
    if language:
        args.append(f"--language={language}")
    if not uploaded.name.lower().endswith(".pdf") and image_dpi:
        args.append(f"--image-dpi={image_dpi}")
    if skip_big:
        args.append(f"--skip-big={skip_big_mpixels}")
    if invalidate_digital_signatures:
        args.append("--invalidate-digital-signatures")
    if oversample:
        args.append(f"--oversample={oversample}")
    if rotate_pages:
        args.append("--rotate-pages")
    if deskew:
        args.append("--deskew")
    if clean:
        args.append("--clean")
    if clean_final:
        args.append("--clean-final")
    if remove_vectors:
        args.append("--remove-vectors")
    if output_type:
        args.append(f"--output-type={output_type}")
    if output_type.startswith('pdfa'):
        args.append(f"--pdfa-image-compression={pdfa_compression}")
    if pdf_renderer:
        args.append(f"--pdf-renderer={pdf_renderer}")
    if optimize:
        args.append(f"--optimize={optimize}")
    if title:
        args.append(f"--title={title}")
    if author:
        args.append(f"--author={author}")
    if keywords:
        args.append(f"--keywords={keywords}")
    if subject:
        args.append(f"--subject={subject}")
    if pages:
        args.append(f"--pages={pages}")
    if max_image_mpixels:
        args.append(f"--max-image-mpixels={max_image_mpixels}")
    if rotate_pages_threshold:
        args.append(f"--rotate-pages-threshold={rotate_pages_threshold}")
    if fast_web_view:
        args.append(f"--fast-web-view={fast_web_view}")
    if continue_on_soft_render_error:
        args.append("--continue-on-soft-render-error")
    if verbose < 0:
        args.append("--quiet")
    elif verbose:
        args.append(f"--verbose={verbose}")
    if optimize > '0' and jpeg_quality:
        args.append(f"--jpeg-quality={jpeg_quality}")
    if optimize > '0' and png_quality:
        args.append(f"--png-quality={png_quality}")
    if jbig2_threshold:
        args.append(f"--jbig2-threshold={jbig2_threshold}")
    if jobs:
        args.append(f"--jobs={jobs}")
    input_bytes = uploaded.getvalue()
    fingerprint = hashlib.sha256(
        input_bytes
        + json.dumps([uploaded.name, args], ensure_ascii=False).encode('utf-8')
    ).hexdigest()
    if st.session_state.get('ocr_request') != fingerprint:
        clear_review()
        st.session_state['ocr_request'] = fingerprint

    if st.button(
        "Run OCRmyPDF", disabled=st.session_state['running'], key='run_button'
    ):
        clear_review()
        st.session_state['running'] = True
        try:
            with st.spinner("Processing PDF..."), TemporaryDirectory() as directory:
                input_path = Path(directory) / 'input.pdf'
                output_path = Path(directory) / 'output.pdf'
                input_path.write_bytes(input_bytes)
                command = [
                    sys.executable,
                    '-u',
                    '-m',
                    'ocrmypdf',
                    *args,
                    str(input_path),
                    str(output_path),
                ]
                proc = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                )
                stdout, stderr = proc.communicate()
                log = (stderr + stdout).decode('utf-8', errors='replace')
                if proc.returncode != 0:
                    st.error(f"OCRmyPDF failed with exit code {proc.returncode}.")
                    st.code(log, language=None)
                elif not output_path.exists() or not output_path.stat().st_size:
                    st.error("No output PDF file was generated for review.")
                else:
                    output_bytes = output_path.read_bytes()
                    with pikepdf.open(BytesIO(output_bytes)) as candidate:
                        output_pages = len(candidate.pages)
                    if not output_pages:
                        raise ValueError("The output PDF has no pages.")
                    st.session_state['ocr_result'] = {
                        'input_bytes': input_bytes,
                        'output_bytes': output_bytes,
                        'input_pages': input_pages,
                        'output_pages': output_pages,
                        'filename': Path(uploaded.name).name,
                        'log': log,
                    }
        except (OSError, pikepdf.PdfError, ValueError) as exc:
            st.error(f"Unable to process the PDF: {exc}")
        finally:
            st.session_state['running'] = False

    if st.session_state['ocr_result'] is not None:
        show_review(st.session_state['ocr_result'])
else:
    clear_review()
    st.session_state.pop('ocr_request', None)
