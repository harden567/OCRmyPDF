# SPDX-FileCopyrightText: 2025 James R. Barlow
# SPDX-FileCopyrightText: 2026 OCRmyPDF contributors
# SPDX-License-Identifier: MIT

"""Run OCRmyPDF on the same PDF with different options."""

from __future__ import annotations

import os
import shlex
from contextlib import ExitStack
from io import BytesIO
from itertools import zip_longest
from pathlib import Path
from subprocess import CalledProcessError, TimeoutExpired, check_output, run
from tempfile import TemporaryDirectory

import pikepdf
import pymupdf
import streamlit as st
from lxml import etree
from streamlit_pdf_viewer import pdf_viewer


def do_column(label, suffix, d):
    cli = st.text_area(
        f"Command line arguments for {label}",
        key=f"args{suffix}",
        value="ocrmypdf {in_} {out}",
    )
    env_text = st.text_area(f"Environment variables for {label}", key=f"env{suffix}")
    env = os.environ.copy()
    for line in env_text.splitlines():
        if line:
            try:
                k, v = line.split("=", 1)
            except ValueError:
                st.error(f"Invalid environment variable: {line}")
                break
            env[k] = v
    # Tokenize the template first: substituting an unquoted Windows path before
    # shlex.split() would lose backslashes or split directories containing spaces.
    try:
        args = [
            argument.replace('{in_}', str(Path(d) / 'input.pdf')).replace(
                '{out}', str(Path(d) / f'output{suffix}.pdf')
            )
            for argument in shlex.split(cli)
        ]
    except ValueError as exc:
        st.error(f"Version {label} has an invalid command: {exc}")
        args = []
    with st.expander("Environment variables", expanded=bool(env_text.strip())):
        st.code('\n'.join(f"{k}={v}" for k, v in env.items()))
    st.code(shlex.join(args))
    return env, args


def run_version(label, args, env, output_path, timeout):
    """Run one comparison independently and return its valid PDF, if any."""
    if not args:
        st.error(f"Version {label} has no command to execute.")
        return None
    try:
        result = run(
            args,
            env=env,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            check=False,
            timeout=timeout or None,
        )
    except TimeoutExpired as exc:
        st.error(f"Version {label} timed out after {exc.timeout} seconds.")
        if exc.stderr:
            st.text(
                exc.stderr.decode('utf-8', errors='replace')
                if isinstance(exc.stderr, bytes)
                else exc.stderr
            )
        return None
    except OSError as exc:
        st.error(f"Version {label} could not run: {exc}")
        return None
    if result.returncode:
        st.error(f"Version {label} failed with exit code {result.returncode}.")
        if result.stderr:
            st.text(result.stderr)
        if result.stdout:
            st.text(result.stdout)
        return None
    if not output_path.is_file():
        st.error(f"Version {label} completed but produced no output PDF.")
        return None
    try:
        document = pymupdf.open(stream=output_path.read_bytes(), filetype='pdf')
        if not document.is_pdf or not len(document):
            document.close()
            st.error(f"Version {label} produced a PDF with no pages.")
            return None
    except (OSError, RuntimeError, ValueError) as exc:
        st.error(f"Version {label} produced an invalid output PDF: {exc}")
        return None
    st.success(f"Version {label} completed: {len(document)} pages.")
    return document


def show_ghostscript_version(label, env):
    """Keep the optional version diagnostic from blocking PDF comparison."""
    try:
        version = check_output(
            ['gs', '--version'], env=env, text=True, timeout=5
        ).strip()
    except (OSError, CalledProcessError, TimeoutExpired):
        st.info(f"Ghostscript version {label} is unavailable (optional diagnostic).")
    else:
        st.text(f"Ghostscript version {label}: {version}")


def main():
    st.set_page_config(layout="wide")

    st.title("OCRmyPDF Compare")
    st.write("Run OCRmyPDF on the same PDF with different options.")
    st.warning("This is a testing tool and is not intended for production use.")

    uploaded_pdf = st.file_uploader("Upload a PDF", type=["pdf"])
    if uploaded_pdf is None:
        return

    pdf_bytes = uploaded_pdf.read()

    with pikepdf.open(BytesIO(pdf_bytes)) as p, TemporaryDirectory() as d:
        with st.expander("PDF Metadata"):
            with p.open_metadata() as meta:
                xml_txt = str(meta)
                parser = etree.XMLParser(remove_blank_text=True)
                tree = etree.fromstring(xml_txt, parser=parser)
                st.code(
                    etree.tostring(tree, pretty_print=True).decode("utf-8"),
                    language="xml",
                )
            st.write(p.docinfo)
            st.write("Number of pages:", len(p.pages))

        col1, col2 = st.columns(2)
        with col1:
            env1, args1 = do_column("A", "1", d)
        with col2:
            env2, args2 = do_column("B", "2", d)

        timeout = st.number_input(
            "Command timeout in seconds (0 means no limit)",
            min_value=0,
            value=0,
            step=60,
            key='command_timeout',
        )
        if not st.button("Execute and Compare"):
            return
        with st.spinner("Executing..."):
            Path(d, "input.pdf").write_bytes(pdf_bytes)
            with ExitStack() as documents:
                versions = []
                columns = st.columns(2)
                for label, args, env, filename, column in zip(
                    ('A', 'B'),
                    (args1, args2),
                    (env1, env2),
                    ('output1.pdf', 'output2.pdf'),
                    columns,
                    strict=True,
                ):
                    output_path = Path(d, filename)
                    with column:
                        document = run_version(label, args, env, output_path, timeout)
                        show_ghostscript_version(label, env)
                    if document is not None:
                        documents.enter_context(document)
                    versions.append((label, document, output_path))

                doc1, doc2 = (version[1] for version in versions)
                if doc1 is not None and doc2 is not None and len(doc1) != len(doc2):
                    st.warning(
                        f"Page counts differ: version A has {len(doc1)} pages, "
                        f"and version B has {len(doc2)} pages."
                    )
                for i, pages in enumerate(
                    zip_longest(
                        doc1 if doc1 is not None else (),
                        doc2 if doc2 is not None else (),
                    )
                ):
                    st.write(f"Page {i + 1}")
                    columns = st.columns(2)
                    for label, page, column in zip(
                        ('A', 'B'), pages, columns, strict=True
                    ):
                        with column, st.container(border=True):
                            if page is None:
                                st.info(
                                    f"Version {label} has no page {i + 1} to compare."
                                )
                            else:
                                st.write(page.get_text())

                for (label, document, output_path), column in zip(
                    versions, st.columns(2), strict=True
                ):
                    with column, st.expander("PDF Viewer"):
                        if document is not None:
                            pdf_viewer(output_path, key=f'comparison_pdf_{label}')
                        else:
                            st.info("No valid result PDF is available for preview.")


if __name__ == "__main__":
    main()
