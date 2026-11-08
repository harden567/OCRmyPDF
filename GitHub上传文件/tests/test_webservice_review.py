# SPDX-FileCopyrightText: 2026 OCRmyPDF contributors
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import io
from pathlib import Path

import pymupdf
import pytest
import streamlit
from streamlit.testing.v1 import AppTest

SCRIPT = Path(__file__).parent.parent / 'misc' / '_webservice.py'


def pdf_bytes(pages=1, text='Scanned document'):
    with pymupdf.open() as document:
        for number in range(pages):
            page = document.new_page()
            page.insert_text((50, 50), f'{text} {number + 1}')
        return document.tobytes()


class UploadedPDF(io.BytesIO):
    name = 'scan.pdf'


@pytest.fixture
def review_app(monkeypatch):
    import subprocess

    upload = {'value': UploadedPDF(pdf_bytes())}
    execution = {'calls': [], 'output': pdf_bytes(), 'returncode': 0}
    monkeypatch.setattr(
        streamlit, 'file_uploader', lambda *_args, **_kwargs: upload['value']
    )

    class Process:
        def __init__(self, args, **_kwargs):
            execution['calls'].append(args)
            self.returncode = execution['returncode']
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO(
                b'OCR finished\n' if self.returncode == 0 else b'Recognition failed\n'
            )
            if self.returncode == 0:
                Path(args[-1]).write_bytes(execution['output'])

        def poll(self):
            return self.returncode

        def communicate(self, **_kwargs):
            return self.stdout.getvalue(), self.stderr.getvalue()

    monkeypatch.setattr(subprocess, 'Popen', Process)
    app = AppTest.from_file(SCRIPT, default_timeout=20).run()
    return app, upload, execution


def run_ocr(app):
    app.button(key='run_button').click().run()
    assert not app.exception
    return app


def test_completed_result_survives_review_reruns_without_running_ocr_again(review_app):
    app, _upload, execution = review_app
    run_ocr(app)

    assert app.session_state['ocr_result']['output_bytes'] == execution['output']
    app.checkbox(key='review_page_1').check().run()
    app.text_area(key='review_note_1').set_value('Check the stamp').run()
    app.checkbox(key='confirm_export').check().run()

    assert len(execution['calls']) == 1
    assert not app.exception
    assert app.session_state['review_page_1']
    assert app.session_state['review_note_1'] == 'Check the stamp'
    assert app.session_state['ocr_result']['output_bytes'] == execution['output']
    assert any('1 of 1' in caption.value for caption in app.caption)


def test_changing_processing_settings_discards_result_and_review(review_app):
    app, _upload, execution = review_app
    run_ocr(app)
    app.checkbox(key='review_page_1').check().run()
    app.checkbox(key='confirm_export').check().run()

    app.selectbox(key='output_type').set_value('pdf').run()

    assert app.session_state['ocr_result'] is None
    assert 'review_page_1' not in app.session_state
    assert 'confirm_export' not in app.session_state
    assert len(execution['calls']) == 1
    assert not app.exception


def test_changing_input_discards_old_result_and_review(review_app):
    app, upload, execution = review_app
    run_ocr(app)
    app.checkbox(key='review_page_1').check().run()
    upload['value'] = UploadedPDF(pdf_bytes(text='A different input'))

    app.run()

    assert app.session_state['ocr_result'] is None
    assert 'review_page_1' not in app.session_state
    assert len(execution['calls']) == 1
    assert not app.exception


def test_failed_processing_can_be_retried_without_stale_result(review_app):
    app, _upload, execution = review_app
    execution['returncode'] = 7
    run_ocr(app)

    assert app.session_state['ocr_result'] is None
    assert any('7' in error.value for error in app.error)
    assert not app.session_state['running']

    execution['returncode'] = 0
    run_ocr(app)

    assert len(execution['calls']) == 2
    assert app.session_state['ocr_result']['output_bytes'] == execution['output']


def test_extra_candidate_pages_are_available_for_review(review_app):
    app, _upload, execution = review_app
    execution['output'] = pdf_bytes(pages=3)
    run_ocr(app)
    app.selectbox(key='review_page_number').set_value(3).run()

    assert app.session_state['ocr_result']['input_pages'] == 1
    assert app.session_state['ocr_result']['output_pages'] == 3
    assert any('no page 3' in notice.value.lower() for notice in app.info)
    assert app.checkbox(key='review_page_3') is not None
    assert not app.exception


def test_existing_signature_and_compression_controls_reach_command(review_app):
    app, _upload, execution = review_app
    app.checkbox(key='invalidate_signatures').check().run()
    app.selectbox(key='pdfa_compression').set_value('lossless').run()
    run_ocr(app)

    args = execution['calls'][0]
    assert '--invalidate-digital-signatures' in args
    assert '--pdfa-image-compression=lossless' in args


def test_malformed_input_is_reported_without_a_streamlit_exception(review_app):
    app, upload, execution = review_app
    upload['value'] = UploadedPDF(b'not a PDF')

    app.run()

    assert not app.exception
    assert app.error
    assert not execution['calls']


def test_review_marks_and_notes_survive_switching_pages(review_app):
    app, upload, execution = review_app
    upload['value'] = UploadedPDF(pdf_bytes(pages=2))
    execution['output'] = pdf_bytes(pages=2)
    app.run()
    run_ocr(app)
    app.checkbox(key='review_page_1').check().run()
    app.text_area(key='review_note_1').set_value('First page checked').run()
    app.selectbox(key='review_page_number').set_value(2).run()
    app.checkbox(key='review_page_2').check().run()
    app.selectbox(key='review_page_number').set_value(1).run()

    assert app.checkbox(key='review_page_1').value
    assert app.text_area(key='review_note_1').value == 'First page checked'
    assert any('2 of 2' in caption.value for caption in app.caption)
    assert len(execution['calls']) == 1
    assert not app.exception


def test_empty_input_is_reported_as_an_invalid_pdf(review_app):
    app, upload, execution = review_app
    upload['value'] = UploadedPDF(b'')

    app.run()

    assert not app.exception
    assert app.error
    assert not execution['calls']


def test_quiet_verbosity_is_forwarded_as_a_valid_cli_flag(review_app):
    app, _upload, execution = review_app
    next(box for box in app.selectbox if box.label == 'Verbosity level').set_value(
        -1
    ).run()
    run_ocr(app)

    assert '--quiet' in execution['calls'][0]
    assert not any(arg.startswith('--verbose=-') for arg in execution['calls'][0])
