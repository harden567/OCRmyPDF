# SPDX-FileCopyrightText: 2026 OCRmyPDF contributors
# SPDX-License-Identifier: MPL-2.0

from __future__ import annotations

import importlib.util
import shlex
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pymupdf
import pytest

MISC = Path(__file__).parent.parent / 'misc'


class UploadedPDF(BytesIO):
    name = 'input.pdf'


class RecordingUI:
    """Record Streamlit output while exercising real PDF and subprocess code."""

    def __init__(self, uploads, commands=None):
        """Initialize uploads, editable commands and captured UI messages."""
        self.uploads = uploads
        self.commands = commands or {}
        self.messages = []
        self.previews = []
        self.numbers = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def set_page_config(self, **_kwargs):
        pass

    def file_uploader(self, _label, **kwargs):
        return self.uploads.get(kwargs.get('key', 'input'))

    def text_area(self, _label, *, key, **_kwargs):
        return self.commands.get(key, '')

    def columns(self, count):
        return [self] * count

    def expander(self, *_args, **_kwargs):
        return self

    container = expander
    spinner = expander

    def button(self, *_args, **_kwargs):
        return True

    def number_input(self, _label, *, key, value, **_kwargs):
        return self.numbers.get(key, value)

    def _record(self, kind, *values, **_kwargs):
        self.messages.append((kind, ' '.join(map(str, values))))

    def title(self, *values, **kwargs):
        self._record('title', *values, **kwargs)

    def write(self, *values, **kwargs):
        self._record('write', *values, **kwargs)

    def text(self, *values, **kwargs):
        self._record('text', *values, **kwargs)

    def code(self, *values, **kwargs):
        self._record('code', *values, **kwargs)

    def error(self, *values, **kwargs):
        self._record('error', *values, **kwargs)

    def warning(self, *values, **kwargs):
        self._record('warning', *values, **kwargs)

    def info(self, *values, **kwargs):
        self._record('info', *values, **kwargs)

    def success(self, *values, **kwargs):
        self._record('success', *values, **kwargs)

    def preview(self, path, **_kwargs):
        # Opening the actual preview target also detects missing/broken output.
        with pymupdf.open(path) as document:
            self.previews.append([page.get_text() for page in document])

    @property
    def visible_text(self):
        return '\n'.join(text for kind, text in self.messages if kind != 'code')


def make_pdf(path, pages, prefix):
    with pymupdf.open() as document:
        for index in range(pages):
            page = document.new_page()
            page.insert_text((50, 50), f'{prefix} page {index + 1}')
        document.save(path)
    return path


def load_comparison(name, monkeypatch, ui):
    spec = importlib.util.spec_from_file_location(name, MISC / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'st', ui)
    monkeypatch.setattr(module, 'pdf_viewer', ui.preview)
    return module


def copy_command(source):
    return shlex.join(
        [
            sys.executable,
            '-c',
            'import shutil,sys; shutil.copyfile(sys.argv[1], sys.argv[2])',
            str(source),
            '{out}',
        ]
    )


@pytest.mark.parametrize('left_pages,right_pages', [(1, 3), (3, 1)])
def test_pdf_compare_displays_pages_missing_from_either_side(
    tmp_path, monkeypatch, left_pages, right_pages
):
    left = make_pdf(tmp_path / 'left.pdf', left_pages, 'Left')
    right = make_pdf(tmp_path / 'right.pdf', right_pages, 'Right')
    ui = RecordingUI(
        {
            'pdf1': UploadedPDF(left.read_bytes()),
            'pdf2': UploadedPDF(right.read_bytes()),
        }
    )
    module = load_comparison('pdf_compare', monkeypatch, ui)

    module.main()

    assert 'Page 3' in ui.visible_text
    assert ('Left page 3' if left_pages == 3 else 'Right page 3') in ui.visible_text
    assert any(
        kind == 'warning' and 'page' in text.lower() for kind, text in ui.messages
    )
    assert any(kind == 'info' and 'page' in text.lower() for kind, text in ui.messages)
    assert len(ui.previews) == 2


def comparison_ui(tmp_path, monkeypatch, command_a=None, command_b=None):
    source_a = make_pdf(tmp_path / 'source-a.pdf', 1, 'A')
    source_b = make_pdf(tmp_path / 'source-b.pdf', 3, 'B')
    ui = RecordingUI(
        {'input': UploadedPDF(source_a.read_bytes())},
        {
            'args1': command_a or copy_command(source_a),
            'args2': command_b or copy_command(source_b),
        },
    )
    module = load_comparison('ocrmypdf_compare', monkeypatch, ui)
    # Isolate the historical optional diagnostic in behavior tests.
    monkeypatch.setattr(module, 'check_output', lambda *_args, **_kwargs: 'test-gs')
    return module, ui


def test_ocr_comparison_includes_extra_output_pages(tmp_path, monkeypatch):
    module, ui = comparison_ui(tmp_path, monkeypatch)

    module.main()

    assert 'Page 3' in ui.visible_text
    assert 'B page 3' in ui.visible_text
    assert any(
        kind == 'warning' and 'page' in text.lower() for kind, text in ui.messages
    )
    assert len(ui.previews) == 2


def test_ocr_comparison_reports_failed_side_and_still_shows_success(
    tmp_path, monkeypatch
):
    command = shlex.join(
        [
            sys.executable,
            '-c',
            "import sys; sys.stderr.write('recognition failed'); sys.exit(7)",
        ]
    )
    module, ui = comparison_ui(tmp_path, monkeypatch, command_a=command)

    module.main()

    errors = '\n'.join(text for kind, text in ui.messages if kind == 'error')
    assert 'A' in errors and '7' in errors
    assert 'recognition failed' in ui.visible_text
    assert len(ui.previews) == 1
    assert 'B page 3' in ui.visible_text


@pytest.mark.parametrize('output', [None, 'not a pdf'])
def test_ocr_comparison_rejects_missing_or_invalid_success_output(
    tmp_path, monkeypatch, output
):
    script = (
        'pass'
        if output is None
        else "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('not a pdf')"
    )
    command = shlex.join([sys.executable, '-c', script, '{out}'])
    module, ui = comparison_ui(tmp_path, monkeypatch, command_a=command)

    module.main()

    errors = '\n'.join(text for kind, text in ui.messages if kind == 'error')
    assert 'A' in errors
    assert len(ui.previews) == 1
    assert 'B page 3' in ui.visible_text


def test_ocr_comparison_missing_command_does_not_hide_other_result(
    tmp_path, monkeypatch
):
    command = shlex.join([str(tmp_path / 'missing-ocr-executable'), '{in_}', '{out}'])
    module, ui = comparison_ui(tmp_path, monkeypatch, command_a=command)

    module.main()

    assert any(kind == 'error' and 'A' in text for kind, text in ui.messages)
    assert len(ui.previews) == 1
    assert 'B page 3' in ui.visible_text


def test_ocr_comparison_timeout_does_not_hide_other_result(tmp_path, monkeypatch):
    module, ui = comparison_ui(tmp_path, monkeypatch)
    real_run = subprocess.run
    attempts = 0

    def timeout_first(args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.TimeoutExpired(args, 300, stderr='timed-out recognition')
        return real_run(args, **kwargs)

    monkeypatch.setattr(module, 'run', timeout_first)

    module.main()

    assert any(kind == 'error' and 'A' in text for kind, text in ui.messages)
    assert 'timed' in ui.visible_text.lower()
    assert len(ui.previews) == 1


def test_ocr_comparison_does_not_require_ghostscript_for_valid_outputs(
    tmp_path, monkeypatch
):
    module, ui = comparison_ui(tmp_path, monkeypatch)

    def missing_ghostscript(*_args, **_kwargs):
        raise FileNotFoundError('gs is not installed')

    monkeypatch.setattr(module, 'check_output', missing_ghostscript)

    module.main()

    assert len(ui.previews) == 2
    assert 'B page 3' in ui.visible_text


def test_ocr_comparison_reports_both_failed_sides(tmp_path, monkeypatch):
    command = shlex.join([sys.executable, '-c', 'import sys; sys.exit(8)'])
    module, ui = comparison_ui(tmp_path, monkeypatch, command, command)

    module.main()

    errors = '\n'.join(text for kind, text in ui.messages if kind == 'error')
    assert 'A' in errors and 'B' in errors
    assert not ui.previews


def test_ocr_comparison_preserves_paths_with_spaces(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / 'PDF with spaces.pdf', 2, 'Spaced')
    module, ui = comparison_ui(tmp_path, monkeypatch, command_a=copy_command(source))

    module.main()

    assert len(ui.previews) == 2
    assert 'Spaced page 2' in ui.visible_text


def test_ocr_comparison_applies_configured_timeout(tmp_path, monkeypatch):
    command = shlex.join([sys.executable, '-c', 'import time; time.sleep(3)'])
    module, ui = comparison_ui(tmp_path, monkeypatch, command_a=command)
    ui.numbers['command_timeout'] = 1

    module.main()

    assert any(kind == 'error' and 'A' in text for kind, text in ui.messages)
    assert 'timed' in ui.visible_text.lower()
    assert len(ui.previews) == 1
    assert 'B page 3' in ui.visible_text


def test_ocr_comparison_substitutes_whole_paths_after_tokenizing_template(
    tmp_path, monkeypatch
):
    ui = RecordingUI({}, {'args1': 'ocrmypdf {in_} {out}'})
    module = load_comparison('ocrmypdf_compare', monkeypatch, ui)
    directory = tmp_path / 'PDF files with spaces'

    _env, args = module.do_column('A', '1', directory)

    assert args == [
        'ocrmypdf',
        str(directory / 'input.pdf'),
        str(directory / 'output1.pdf'),
    ]


def test_ocr_comparison_malformed_command_does_not_hide_other_result(
    tmp_path, monkeypatch
):
    module, ui = comparison_ui(tmp_path, monkeypatch, command_a='"unterminated')

    module.main()

    assert any(kind == 'error' and 'A' in text for kind, text in ui.messages)
    assert len(ui.previews) == 1
    assert 'B page 3' in ui.visible_text


def test_ocr_comparison_can_preview_identical_pdf_results(tmp_path, monkeypatch):
    import streamlit
    from streamlit.testing.v1 import AppTest

    source = make_pdf(tmp_path / 'identical.pdf', 1, 'Identical')
    monkeypatch.setattr(
        streamlit,
        'file_uploader',
        lambda *_args, **_kwargs: UploadedPDF(source.read_bytes()),
    )
    app = AppTest.from_file(str(MISC / 'ocrmypdf_compare.py'), default_timeout=20).run()
    command = copy_command(source)
    app.text_area[0].set_value(command)
    app.text_area[2].set_value(command)

    app.button[0].click().run()

    assert not app.exception
    assert len(app.get('component_instance')) == 2
