# SPDX-FileCopyrightText: 2025 James R. Barlow
# SPDX-FileCopyrightText: 2026 OCRmyPDF contributors
# SPDX-License-Identifier: MIT

"""Compare two PDFs."""

from __future__ import annotations

from io import BytesIO
from itertools import zip_longest
from pathlib import Path
from tempfile import TemporaryDirectory

import pikepdf
import pymupdf
import streamlit as st
from lxml import etree
from streamlit_pdf_viewer import pdf_viewer


def do_metadata(pdf):
    with pikepdf.open(pdf) as pdf:
        with pdf.open_metadata() as meta:
            xml_txt = str(meta)
            parser = etree.XMLParser(remove_blank_text=True)
            tree = etree.fromstring(xml_txt, parser=parser)
            st.code(
                etree.tostring(tree, pretty_print=True).decode("utf-8"),
                language="xml",
            )
        st.write(pdf.docinfo)
        st.write("Number of pages:", len(pdf.pages))


def main():
    st.set_page_config(layout="wide")

    st.title("PDF Compare")
    st.write("Compare two PDFs.")

    col1, col2 = st.columns(2)
    with col1:
        uploaded_pdf1 = st.file_uploader("Upload a PDF", type=["pdf"], key='pdf1')
    with col2:
        uploaded_pdf2 = st.file_uploader("Upload a PDF", type=["pdf"], key='pdf2')
    if uploaded_pdf1 is None or uploaded_pdf2 is None:
        return

    pdf_bytes1 = uploaded_pdf1.getvalue()
    pdf_bytes2 = uploaded_pdf2.getvalue()

    with st.expander("PDF Metadata"):
        col1, col2 = st.columns(2)
        with col1:
            do_metadata(BytesIO(pdf_bytes1))
        with col2:
            do_metadata(BytesIO(pdf_bytes2))

    with TemporaryDirectory() as d:
        Path(d, "1.pdf").write_bytes(pdf_bytes1)
        Path(d, "2.pdf").write_bytes(pdf_bytes2)

        with (
            pymupdf.open(stream=pdf_bytes1, filetype='pdf') as doc1,
            pymupdf.open(stream=pdf_bytes2, filetype='pdf') as doc2,
        ):
            if len(doc1) != len(doc2):
                st.warning(
                    f"Page counts differ: the left PDF has {len(doc1)} pages, "
                    f"and the right PDF has {len(doc2)} pages."
                )
            with st.expander("Text"):
                for i, pages in enumerate(zip_longest(doc1, doc2)):
                    st.write(f"Page {i + 1}")
                    columns = st.columns(2)
                    for label, page, column in zip(
                        ('Left', 'Right'), pages, columns, strict=True
                    ):
                        with column, st.container(border=True):
                            if page is None:
                                st.info(f"{label} PDF has no page {i + 1}.")
                            else:
                                st.write(page.get_text())

        with st.expander("PDF Viewer"):
            col1, col2 = st.columns(2)
            with col1:
                pdf_viewer(Path(d, "1.pdf"), key='pdf_viewer1', render_text=True)
            with col2:
                pdf_viewer(Path(d, "2.pdf"), key='pdf_viewer2', render_text=True)


if __name__ == "__main__":
    main()
