"""Comprehensive QA and unit/integration test suite for converter.py.

Covers Markdown conversion, image resolution, Unicode/Thai/emoji/math rendering,
HTML sanitization & CSP, edge-case inputs (empty, huge, BOM, invalid bytes),
layout options (min/max font sizes, margins, page-break-avoid), missing browser
handling, output path errors, and temp file / process cleanup.
"""

from __future__ import annotations

import stat
import subprocess
import sys
import tempfile as tf
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import psutil
import pymupdf
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import converter
from converter import (
    MARGIN_PRESETS,
    ConversionOptions,
    ConversionResult,
    _is_remote_or_data_uri,
    _resolve_local_image,
    build_css,
    build_html,
    find_browser_executable,
    render_pdf,
    render_pdf_pages_to_pil,
)


def _has_browser() -> bool:
    try:
        find_browser_executable()
    except FileNotFoundError:
        return False
    else:
        return True


HAS_BROWSER = _has_browser()
requires_browser = pytest.mark.skipif(
    not HAS_BROWSER, reason="No headless Edge/Chrome/Chromium available on system"
)


@pytest.fixture
def sample_png(tmp_path: Path) -> Path:
    """Create a valid 40x30 red PNG image in tmp_path."""
    img_dir = tmp_path / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    img_path = img_dir / "sample.png"
    img = Image.new("RGB", (40, 30), color=(220, 30, 30))
    img.save(img_path, format="PNG")
    return img_path


# ---------------------------------------------------------------------------
# 1. Basic Markdown: headings, lists, code blocks, tables
# ---------------------------------------------------------------------------


def test_basic_markdown_html_generation(tmp_path: Path) -> None:
    md = (
        "# Heading 1\n\n"
        "## Heading 2\n\n"
        "### Heading 3\n\n"
        "#### Heading 4\n\n"
        "- Bullet A\n"
        "- Bullet B\n\n"
        "Paragraph separating unordered and ordered lists.\n\n"
        "1. First\n"
        "2. Second\n\n"
        "Inline `code_snippet()` here.\n\n"
        "```python\n"
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "```\n\n"
        "| Col A | Col B |\n"
        "| :--- | ---: |\n"
        "| val 1 | 100 |\n"
        "| val 2 | 200 |\n\n"
        "> A blockquote paragraph.\n\n"
        "---\n"
    )
    opts = ConversionOptions()
    html, suppressed = build_html(md, str(tmp_path), opts)

    assert suppressed == []
    assert "<h1>Heading 1</h1>" in html
    assert "<h2>Heading 2</h2>" in html
    assert "<h3>Heading 3</h3>" in html
    assert "<h4>Heading 4</h4>" in html
    assert "<ul>" in html and "<li>Bullet A</li>" in html
    assert "<ol>" in html and "<li>First</li>" in html
    assert "<code>code_snippet()</code>" in html
    assert "<pre><code" in html and "def add(a: int, b: int)" in html
    assert "<table>" in html and "<th" in html and "<td" in html
    assert "<blockquote>" in html
    assert "<hr" in html


# ---------------------------------------------------------------------------
# 2. Local Images: relative, URL-encoded spaces, parentheses, Unicode, missing
# ---------------------------------------------------------------------------


def test_local_image_relative_and_missing_stripped(
    tmp_path: Path, sample_png: Path
) -> None:
    md = (
        "![Valid](images/sample.png)\n\n"
        "![Missing](images/does_not_exist.png)\n"
    )
    opts = ConversionOptions(suppress_missing_images=True)
    html, suppressed = build_html(md, str(tmp_path), opts)

    assert "images/does_not_exist.png" in suppressed
    assert "does_not_exist.png" not in html
    assert sample_png.resolve().as_uri() in html


def test_local_image_missing_kept_when_suppress_disabled(tmp_path: Path) -> None:
    md = (
        "![Missing](images/missing_file.png)\n\n"
        '<img src="images/missing_inline.png" alt="inline">\n'
    )
    opts = ConversionOptions(suppress_missing_images=False)
    html, suppressed = build_html(md, str(tmp_path), opts)

    assert suppressed == []
    assert 'src="images/missing_file.png"' in html
    assert 'src="images/missing_inline.png"' in html


def test_local_image_url_encoded_spaces_and_unicode(tmp_path: Path) -> None:
    img_dir = tmp_path / "images"
    img_dir.mkdir(exist_ok=True)
    special_img = img_dir / "test image µ ไทย (1).png"
    Image.new("RGB", (20, 20), color=(0, 128, 255)).save(special_img, format="PNG")

    md = "![Special](images/test%20image%20µ%20ไทย%20%281%29.png)"
    opts = ConversionOptions(suppress_missing_images=True)
    html, suppressed = build_html(md, str(tmp_path), opts)

    assert suppressed == []
    assert special_img.resolve().as_uri() in html


def test_local_image_literal_parentheses_and_angle_brackets(tmp_path: Path) -> None:
    """Verify Markdown image links with literal parentheses or <...> resolve cleanly."""
    img_dir = tmp_path / "images"
    img_dir.mkdir(exist_ok=True)
    paren_img = img_dir / "figure (1).png"
    Image.new("RGB", (20, 20), color=(0, 200, 0)).save(paren_img, format="PNG")

    md = (
        "![Figure](images/figure (1).png)\n\n"
        '![Angle](<images/figure (1).png> "Figure Title")\n'
    )
    opts = ConversionOptions(suppress_missing_images=True)
    html, suppressed = build_html(md, str(tmp_path), opts)

    assert suppressed == []
    assert paren_img.resolve().as_uri() in html
    assert "<p>.png)</p>" not in html


def test_local_image_with_optional_markdown_title(
    tmp_path: Path, sample_png: Path
) -> None:
    md = '![Sample](images/sample.png "Optional Title")'
    opts = ConversionOptions(suppress_missing_images=True)
    html, suppressed = build_html(md, str(tmp_path), opts)

    assert suppressed == []
    assert sample_png.resolve().as_uri() in html


def test_preprocess_markdown_skips_code_blocks(tmp_path: Path) -> None:
    """Verify image syntax inside fenced or inline code blocks is not stripped."""
    md = (
        "Inline example: `![Example](images/missing_inline.png)`.\n\n"
        "```markdown\n"
        "![DocExample](images/missing_in_fence.png)\n"
        "```\n"
    )
    html, suppressed = build_html(
        md, str(tmp_path), ConversionOptions(suppress_missing_images=True)
    )
    assert suppressed == []
    assert "missing_inline.png" in html
    assert "missing_in_fence.png" in html


def test_remote_and_data_uri_images_untouched(tmp_path: Path) -> None:
    assert _is_remote_or_data_uri("https://example.com/a.png") is True
    assert _is_remote_or_data_uri("http://example.com/a.png") is True
    assert _is_remote_or_data_uri("data:image/png;base64,iVBORw0KGgo=") is True
    assert _is_remote_or_data_uri("file:///C:/test.png") is False
    assert _is_remote_or_data_uri("images/local.png") is False

    md = (
        "![Remote](https://example.com/logo.png)\n\n"
        '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="dot">'
    )
    html, suppressed = build_html(md, str(tmp_path), ConversionOptions())
    assert suppressed == []
    assert 'src="https://example.com/logo.png"' in html
    assert 'src="data:image/gif;base64,R0lGODlhAQABAAAAACw="' in html


def test_resolve_local_image_oserror_unc_and_traversal_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify _resolve_local_image blocks UNC paths, traversal, and OSError."""
    # 1. UNC / network paths blocked immediately
    assert _resolve_local_image(r"\\attacker.com\share\a.png", str(tmp_path)) == (
        False,
        None,
    )
    assert _resolve_local_image("//attacker.com/share/a.png", str(tmp_path)) == (
        False,
        None,
    )
    assert _resolve_local_image("", str(tmp_path)) == (False, None)

    # 2. Path traversal outside base_dir blocked by default, allowed when opted in
    outside_img = tmp_path / "outside.png"
    Image.new("RGB", (10, 10), color=(0, 0, 0)).save(outside_img, format="PNG")
    sub_base = tmp_path / "sub"
    sub_base.mkdir(exist_ok=True)

    exists_blocked, _ = _resolve_local_image(
        "../outside.png", str(sub_base), allow_outside_base=False
    )
    assert exists_blocked is False

    exists_allowed, res_path = _resolve_local_image(
        "../outside.png", str(sub_base), allow_outside_base=True
    )
    assert exists_allowed is True
    assert res_path == outside_img.resolve()

    # 3. OSError handled gracefully
    def raise_oserror(_self: Path) -> Path:
        raise OSError("Simulated filesystem error")

    monkeypatch.setattr(Path, "resolve", raise_oserror)
    assert _resolve_local_image("images/sample.png", str(tmp_path)) == (False, None)


def test_local_image_ampersand_in_filename(tmp_path: Path) -> None:
    """Verify image filenames containing '&' resolve cleanly in Markdown and HTML."""
    img_dir = tmp_path / "images"
    img_dir.mkdir(exist_ok=True)
    amp_img = img_dir / "a&b.png"
    Image.new("RGB", (20, 20), color=(100, 100, 100)).save(amp_img, format="PNG")

    md = '![Amp](images/a&b.png)\n\n<img src="images/a&amp;b.png" alt="html_amp">'
    html, suppressed = build_html(
        md, str(tmp_path), ConversionOptions(suppress_missing_images=True)
    )
    assert suppressed == []
    assert html.count(amp_img.resolve().as_uri()) == 2


# ---------------------------------------------------------------------------
# 3. Thai text, Emoji, and Math symbols (Ω µ ± ×)
# ---------------------------------------------------------------------------


def test_thai_emoji_and_math_symbols_in_html(tmp_path: Path) -> None:
    md = (
        "# เอกสารทดสอบภาษาไทย 🔔 ⚠️\n\n"
        "ค่าความต้านทาน: 100 Ω ± 5%, กระแส 25 µA, กำลังขยาย 10 × 2 ➔ 20\n"
    )
    html, suppressed = build_html(md, str(tmp_path), ConversionOptions())
    assert suppressed == []
    assert "เอกสารทดสอบภาษาไทย" in html
    assert "🔔" in html and "⚠️" in html
    for sym in ("Ω", "µ", "±", "×", "➔"):
        assert sym in html


# ---------------------------------------------------------------------------
# 4. Inline HTML and Malicious HTML (<script>, onerror=, file:// links, traversal)
# ---------------------------------------------------------------------------


def test_inline_html_and_malicious_html_sanitized(tmp_path: Path) -> None:
    """Verify safe inline HTML is kept while <script>, <iframe>, and ../ are blocked."""
    outside_img = tmp_path / "outside_secret.png"
    Image.new("RGB", (10, 10), color=(0, 0, 0)).save(outside_img, format="PNG")
    sub_base = tmp_path / "subdir"
    sub_base.mkdir(exist_ok=True)

    md = (
        '<div class="custom">Inline HTML block</div>\n\n'
        '<img src="missing_inline.png" onerror="alert(1)">\n\n'
        '<img src="../outside_secret.png" alt="traversal">\n\n'
        "<script>window.pwned = true;</script>\n\n"
        '<iframe src="file:///C:/Windows/win.ini"></iframe>\n\n'
        "[Local File](file:///C:/Windows/win.ini)\n\n"
        "[JS Link](javascript:alert(1))\n"
    )
    opts = ConversionOptions(suppress_missing_images=True)
    html, suppressed = build_html(md, str(sub_base), opts)

    assert '<div class="custom">Inline HTML block</div>' in html
    assert "missing_inline.png" in suppressed
    assert "../outside_secret.png" in suppressed
    assert outside_img.resolve().as_uri() not in html
    assert "<script" not in html
    assert "window.pwned" not in html
    assert "<iframe" not in html
    assert "onerror=" not in html
    assert 'href="file:///C:/Windows/win.ini"' not in html
    assert 'href="javascript:' not in html


def test_title_html_injection_escaped_and_csp_present(tmp_path: Path) -> None:
    """Verify options.title is HTML-escaped and CSP meta tag is present."""
    opts = ConversionOptions(title="</title><script>alert(1)</script>")
    html, _ = build_html("Hello", str(tmp_path), opts)
    assert "<title>&lt;/title&gt;&lt;script&gt;alert(1)&lt;/script&gt;</title>" in html
    assert 'http-equiv="Content-Security-Policy"' in html


# ---------------------------------------------------------------------------
# 5. Empty input, huge input, invalid UTF-8, and BOM
# ---------------------------------------------------------------------------


def test_empty_and_whitespace_input(tmp_path: Path) -> None:
    html_empty, sup1 = build_html("", str(tmp_path), ConversionOptions())
    html_ws, sup2 = build_html("   \n\t\n  ", str(tmp_path), ConversionOptions())
    assert sup1 == [] and sup2 == []
    assert "<body>\n\n</body>" in html_empty
    assert "<body>\n\n</body>" in html_ws


def test_huge_input_performance_and_correctness(tmp_path: Path) -> None:
    """Convert a large ~160 KB Markdown document with 9,000 lines and 1,000 tables."""
    block = (
        "## Section Heading\n\n"
        "Paragraph with **bold**, `code`, Thai สวัสดี, and symbols Ω µ ± ×.\n\n"
        "| Key | Value |\n| --- | --- |\n| A | 1 |\n| B | 2 |\n\n"
    )
    huge_md = block * 1000
    html, suppressed = build_html(huge_md, str(tmp_path), ConversionOptions())
    assert suppressed == []
    assert html.count("<h2>Section Heading</h2>") == 1000


def test_utf8_bom_preserves_first_heading(tmp_path: Path) -> None:
    """Verify a leading UTF-8 BOM (\\ufeff) is stripped so '# Heading' becomes <h1>."""
    bom_md = "\ufeff# Title With BOM\n\nBody text."
    html, _ = build_html(bom_md, str(tmp_path), ConversionOptions())
    assert "<h1>Title With BOM</h1>" in html


def test_invalid_utf8_file_handling_and_surrogates(tmp_path: Path) -> None:
    bad_file = tmp_path / "invalid_utf8.md"
    bad_file.write_bytes(b"# Heading\n\xff\xfeInvalid UTF-8 bytes")

    with pytest.raises(UnicodeDecodeError):
        bad_file.read_text(encoding="utf-8")

    out_pdf = tmp_path / "surrogate.pdf"
    with (
        patch("converter.find_browser_executable", return_value="mock_browser"),
        pytest.raises(UnicodeEncodeError),
    ):
        render_pdf("Bad surrogate: \ud800", str(tmp_path), str(out_pdf))


# ---------------------------------------------------------------------------
# 6. Font size and margin options at minimum and maximum values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("font_size", [7.5, 9.5, 14.0])
@pytest.mark.parametrize(("margin_name", "margin_css"), list(MARGIN_PRESETS.items()))
def test_font_size_and_margin_presets(
    font_size: float, margin_name: str, margin_css: str
) -> None:
    assert margin_name in MARGIN_PRESETS
    opts = ConversionOptions(
        font_size_pt=font_size,
        margin_css=margin_css,
        img_max_width=30,
        img_max_height=400,
    )
    css = build_css(opts)
    assert f"font-size: {font_size}pt;" in css
    assert f"margin: {margin_css};" in css
    assert "max-width: 30px;" in css
    assert "max-height: 400px;" in css
    expected_table_pt = round(max(7.5, font_size - 1.0), 1)
    assert f"font-size: {expected_table_pt}pt;" in css


# ---------------------------------------------------------------------------
# 7. Page-break-avoid options (Keep Q&A / rows together)
# ---------------------------------------------------------------------------


def test_keep_blocks_together_enabled_vs_disabled(tmp_path: Path) -> None:
    md = (
        "### Station 1: Question\n\n"
        "- Answer point 1\n"
        "- Answer point 2\n"
    )
    html_on, _ = build_html(
        md, str(tmp_path), ConversionOptions(keep_blocks_together=True)
    )
    assert '<div class="station-block"><h3>Station 1: Question</h3>' in html_on
    assert "break-inside: avoid;" in html_on

    html_off, _ = build_html(
        md, str(tmp_path), ConversionOptions(keep_blocks_together=False)
    )
    assert '<div class="station-block">' not in html_off
    assert "break-inside: auto;" in html_off


def test_keep_blocks_together_nested_list(tmp_path: Path) -> None:
    """Verify nested lists under <h3> wrap the entire outer list in station-block."""
    md = (
        "### Q1\n\n"
        "- Outer 1\n"
        "    - Inner 1a\n"
        "- Outer 2\n"
    )
    html, _ = build_html(
        md, str(tmp_path), ConversionOptions(keep_blocks_together=True)
    )
    assert html.count('<div class="station-block">') == 1
    assert "<li>Outer 2</li>\n</ul></div>" in html


# ---------------------------------------------------------------------------
# 8. No browser installed & browser discovery
# ---------------------------------------------------------------------------


def test_no_browser_installed_raises_clear_file_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MDTOPDF_BROWSER", raising=False)
    monkeypatch.setattr(Path, "is_file", lambda _self: False)
    monkeypatch.setattr(converter.shutil, "which", lambda _c: None)

    with pytest.raises(FileNotFoundError, match="Could not find Microsoft Edge"):
        find_browser_executable()

    with pytest.raises(FileNotFoundError, match="Could not find Microsoft Edge"):
        render_pdf("# Test", str(tmp_path), str(tmp_path / "out.pdf"))


def test_find_browser_executable_shutil_which_and_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify MDTOPDF_BROWSER override and fallback to shutil.which."""
    fake_browser = tmp_path / "custom_browser.exe"
    fake_browser.write_text("binary", encoding="utf-8")
    monkeypatch.setenv("MDTOPDF_BROWSER", str(fake_browser))
    assert find_browser_executable() == str(fake_browser)

    monkeypatch.delenv("MDTOPDF_BROWSER", raising=False)
    monkeypatch.setattr(Path, "is_file", lambda _self: False)
    monkeypatch.setattr(
        converter.shutil,
        "which",
        lambda cmd: "/usr/local/bin/chromium" if cmd == "chromium" else None,
    )
    assert find_browser_executable() == "/usr/local/bin/chromium"


# ---------------------------------------------------------------------------
# 9. Output path problems: read-only, locked, spaces, non-ASCII characters
# ---------------------------------------------------------------------------


def test_output_path_browser_failure(tmp_path: Path) -> None:
    out_pdf = tmp_path / "failed_output.pdf"
    mock_proc = MagicMock(returncode=1, stderr="Permission denied writing PDF")

    with (
        patch("converter.find_browser_executable", return_value="mock_edge.exe"),
        patch("converter.subprocess.run", return_value=mock_proc),
        pytest.raises(RuntimeError, match="Browser PDF rendering failed"),
    ):
        render_pdf("# Hello", str(tmp_path), str(out_pdf))


def test_output_path_locked_or_unwritable_directory(tmp_path: Path) -> None:
    blocker_file = tmp_path / "blocker_file"
    blocker_file.write_text("not a dir", encoding="utf-8")
    invalid_out = blocker_file / "sub" / "out.pdf"

    with (
        patch("converter.find_browser_executable", return_value="mock_edge.exe"),
        pytest.raises(OSError),
    ):
        render_pdf("# Test", str(tmp_path), str(invalid_out))


@requires_browser
def test_output_path_read_only_or_locked_file(tmp_path: Path) -> None:
    ro_pdf = tmp_path / "readonly.pdf"
    ro_pdf.write_bytes(b"not a valid pdf")
    ro_pdf.chmod(stat.S_IREAD)
    try:
        with pytest.raises((RuntimeError, PermissionError, pymupdf.FileDataError)):
            render_pdf("# Read-only target", str(tmp_path), str(ro_pdf))
    finally:
        ro_pdf.chmod(stat.S_IWRITE | stat.S_IREAD)


# ---------------------------------------------------------------------------
# 10. Temp file cleanup and subprocess timeout / leftover processes
# ---------------------------------------------------------------------------


def test_temp_html_cleaned_up_on_browser_exception_and_timeout(tmp_path: Path) -> None:
    created_temp_files: list[str] = []
    real_mkstemp = converter.tempfile.mkstemp

    def tracking_mkstemp(prefix: str = "", suffix: str = "") -> tuple[int, str]:
        fd, path = real_mkstemp(prefix=prefix, suffix=suffix)
        created_temp_files.append(path)
        return fd, path

    with (
        patch("converter.find_browser_executable", return_value="mock_edge.exe"),
        patch("converter.tempfile.mkstemp", side_effect=tracking_mkstemp),
        patch(
            "converter.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="mock_edge.exe", timeout=60),
        ),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        render_pdf("# Timeout test", str(tmp_path), str(tmp_path / "out.pdf"))

    assert len(created_temp_files) == 1
    assert not Path(created_temp_files[0]).exists()


@requires_browser
def test_real_browser_no_leftover_processes_and_temp_cleanup(tmp_path: Path) -> None:
    temp_root = Path(tf.gettempdir())
    before_htmls = set(temp_root.glob("mdtopdf_*.html"))

    out_pdf = tmp_path / "cleanup_check.pdf"
    res = render_pdf(
        "# Cleanup Test\n\nChecking temp and process cleanup.",
        str(tmp_path),
        str(out_pdf),
    )
    assert Path(res.output_pdf_path).is_file()

    after_htmls = set(temp_root.glob("mdtopdf_*.html"))
    leaked_htmls = after_htmls - before_htmls
    assert leaked_htmls == set(), f"Leaked temp HTML files: {leaked_htmls}"

    lingering = [
        (proc.info["pid"], proc.info["name"])
        for proc in psutil.process_iter(["pid", "name", "cmdline"])
        if str(out_pdf) in " ".join(proc.info.get("cmdline") or [])
    ]
    assert lingering == [], f"Leftover browser processes found: {lingering}"


@requires_browser
def test_real_browser_script_and_local_file_iframe_blocked(tmp_path: Path) -> None:
    """Verify inline <script> and file:/// <iframe> are neutralized in PDF output."""
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("TOP_SECRET_MARKER_98765", encoding="utf-8")
    secret_uri = secret_file.resolve().as_uri()

    script_payload = (
        "<script>document.getElementById('target').textContent = "
        "'SCRIPT_EXECUTED_MARKER';</script>\n\n"
    )
    md = (
        "# Security Check\n\n"
        '<p id="target">SAFE_TEXT</p>\n'
        + script_payload
        + f'<iframe src="{secret_uri}" width="400" height="100"></iframe>\n'
    )
    out_pdf = tmp_path / "security_check.pdf"
    res = render_pdf(md, str(tmp_path), str(out_pdf))

    doc = pymupdf.open(res.output_pdf_path)
    try:
        pdf_text = "\n".join(doc[i].get_text() for i in range(len(doc)))
    finally:
        doc.close()

    assert "SAFE_TEXT" in pdf_text
    assert "SCRIPT_EXECUTED_MARKER" not in pdf_text
    assert "TOP_SECRET_MARKER_98765" not in pdf_text


# ---------------------------------------------------------------------------
# 11. Real Browser End-to-End Conversion Tests (when Edge/Chrome is installed)
# ---------------------------------------------------------------------------


@requires_browser
def test_real_browser_end_to_end_conversion_and_preview(
    tmp_path: Path, sample_png: Path
) -> None:
    assert sample_png.is_file()
    out_dir = tmp_path / "โฟลเดอร์ ทดสอบ (Output)"
    out_pdf = out_dir / "เอกสาร ทดสอบ µ.pdf"

    md = (
        "# รายงานการทดสอบระบบ (System Test) 🔔\n\n"
        "## 1. ข้อมูลทางเทคนิค\n\n"
        "แรงดันไฟฟ้า 220V ± 5%, ความต้านทาน 50 Ω, กระแส 100 µA, ขนาด 4 × 4.\n\n"
        "| ลำดับ | รูปภาพ | รายละเอียด |\n"
        "| :---: | :---: | :--- |\n"
        "| 1 | ![Sample](images/sample.png) | ภาพตัวอย่างที่สมบูรณ์ |\n"
        "| 2 | ![Missing](images/not_found.png) | ภาพที่ถูกตัดออก |\n\n"
        "```python\n"
        "print('Hello World')\n"
        "```\n"
    )

    res: ConversionResult = render_pdf(
        md, str(tmp_path), str(out_pdf), ConversionOptions()
    )

    assert Path(res.output_pdf_path).is_file()
    assert res.page_count >= 1
    assert res.embedded_images >= 1
    assert res.suppressed_images == ["images/not_found.png"]
    assert res.elapsed_seconds > 0

    doc = pymupdf.open(res.output_pdf_path)
    try:
        assert len(doc) == res.page_count
        rect = doc[0].rect
        assert 590 <= rect.width <= 600
        assert 835 <= rect.height <= 848

        extracted_text = doc[0].get_text()
        assert "รายงานการทดสอบระบบ" in extracted_text
        assert "220V" in extracted_text
        assert "Hello World" in extracted_text
    finally:
        doc.close()

    pil_pages = render_pdf_pages_to_pil(res.output_pdf_path, dpi=100)
    assert len(pil_pages) == res.page_count
    assert pil_pages[0].mode == "RGB"
    assert pil_pages[0].width > 500
    assert pil_pages[0].height > 700


# ---------------------------------------------------------------------------
# 12. Audit Verification Tests (Verifying Remediated Edge Cases & Security)
# ---------------------------------------------------------------------------


def test_sanitize_html_preserves_code_blocks_and_prose_with_on_equals(
    tmp_path: Path,
) -> None:
    """Verify F-01 fix: _sanitize_html preserves 'on... = ...' inside <code> and prose."""
    md = (
        "When the sensor has on_state = 1, check the status.\n\n"
        "```python\n"
        "def configure():\n"
        "    online = True\n"
        '    on_error = "retry"\n'
        "    return online\n"
        "```\n"
    )
    html, _ = build_html(md, str(tmp_path), ConversionOptions())

    assert "online = True" in html
    assert "on_error = &quot;retry&quot;" in html or 'on_error = "retry"' in html
    assert "on_state = 1" in html


def test_wrap_h3_list_blocks_does_not_overwrap_across_non_list_h3(
    tmp_path: Path,
) -> None:
    """Verify F-02 fix: _wrap_h3_list_blocks only wraps <h3> immediately followed by list."""
    md = (
        "### 1.1 Overview Without List\n\n"
        "This introductory paragraph should NOT be wrapped in station-block.\n\n"
        "## 2. Main Section\n\n"
        "### 2.1 Checklist With List\n\n"
        "- Item A\n"
        "- Item B\n"
    )
    html, _ = build_html(
        md, str(tmp_path), ConversionOptions(keep_blocks_together=True)
    )

    assert (
        '<div class="station-block"><h3>1.1 Overview Without List</h3>' not in html
    )
    assert '<div class="station-block"><h3>2.1 Checklist With List</h3>' in html


def test_file_uri_enforces_allow_outside_base_dir_and_missing_suppression(
    tmp_path: Path,
) -> None:
    """Verify F-03 fix: file:// URIs and suppress_missing_images=False enforce containment."""
    outside_img = tmp_path / "outside_secret.png"
    Image.new("RGB", (10, 10), color=(255, 0, 0)).save(outside_img, format="PNG")
    outside_uri = outside_img.resolve().as_uri()

    sub_base = tmp_path / "workspace"
    sub_base.mkdir(exist_ok=True)
    missing_inside_uri = (sub_base / "nonexistent_file.png").resolve().as_uri()

    md = (
        f"![OutsideFileURI]({outside_uri})\n\n"
        f"![MissingFileURI]({missing_inside_uri})\n\n"
        "![RemoteFileHost](file://attacker.com/share/a.png)\n\n"
        "![OutsideRel](../outside_secret.png)\n"
    )

    # 1. With suppress_missing_images=True and allow_outside_base_dir=False (default):
    # All four are blocked/suppressed!
    html_sup, suppressed = build_html(
        md,
        str(sub_base),
        ConversionOptions(
            suppress_missing_images=True, allow_outside_base_dir=False
        ),
    )
    assert outside_uri not in html_sup
    assert missing_inside_uri not in html_sup
    assert "attacker.com" not in html_sup
    assert len(suppressed) == 4

    # 2. With suppress_missing_images=False and allow_outside_base_dir=False:
    # Security-disallowed '../outside_secret.png' is still stripped so <base href>
    # cannot resolve it!
    html_nosup, sup_nosup = build_html(
        '![OutsideRel](../outside_secret.png)\n\n<img src="../outside_secret.png">',
        str(sub_base),
        ConversionOptions(
            suppress_missing_images=False, allow_outside_base_dir=False
        ),
    )
    assert "outside_secret.png" not in html_nosup
    assert "../outside_secret.png" in sup_nosup


def test_sanitize_html_blocks_unquoted_href_entity_scheme_slash_onload_and_style(
    tmp_path: Path,
) -> None:
    """Verify F-04 fix: _sanitize_html strips unquoted/entity hrefs, <img/onerror=>, <style>."""
    md = (
        "<a href=file:///C:/Windows/win.ini>Unquoted File</a>\n\n"
        '<a href="&#106;avascript:alert(1)">Entity JS</a>\n\n'
        "<a href='https://example.com'>Safe Single Quote</a>\n\n"
        '<div><img/src="https://example.com/a.png"/onerror="alert(1)"></div>\n\n'
        "<style>body { display: none; }</style>\n"
    )
    html, _ = build_html(md, str(tmp_path), ConversionOptions())

    assert "win.ini" not in html
    assert "alert(1)" not in html
    assert "onerror=" not in html
    assert "display: none" not in html
    assert html.count("<style>") == 1
    assert "https://example.com" in html


def test_local_image_apostrophe_in_filename_resolved(tmp_path: Path) -> None:
    """Verify F-08 fix: Image filenames with single quotes (') resolve cleanly."""
    img_dir = tmp_path / "images"
    img_dir.mkdir(exist_ok=True)
    apos_img = img_dir / "user's_diagram.png"
    Image.new("RGB", (20, 20), color=(10, 20, 30)).save(apos_img, format="PNG")

    md = "![Diagram](images/user's_diagram.png)"
    html, suppressed = build_html(
        md, str(tmp_path), ConversionOptions(suppress_missing_images=True)
    )

    assert suppressed == []
    assert apos_img.resolve().as_uri() in html


def test_preprocess_markdown_double_backtick_inline_code_masked(
    tmp_path: Path,
) -> None:
    """Verify F-10 fix: Double-backtick inline code spans ``... ` ...`` are masked."""
    md = "Double-backtick span: ``prefix ` ![Example](images/missing_in_double_ticks.png)``"
    html, suppressed = build_html(
        md, str(tmp_path), ConversionOptions(suppress_missing_images=True)
    )

    assert suppressed == []
    assert "missing_in_double_ticks.png" in html


def test_resolve_local_image_angle_brackets_and_unclosed_h3_list(
    tmp_path: Path, sample_png: Path
) -> None:
    """Cover angle-bracket wrapping in _resolve_local_image and unclosed list in _wrap_h3_list_blocks."""
    exists, resolved = _resolve_local_image(
        "<images/sample.png>", str(tmp_path), allow_outside_base=False
    )
    assert exists is True
    assert resolved == sample_png.resolve()

    raw_unclosed = "<h3>Unclosed List Heading</h3><ul><li>Item without closing ul"
    wrapped = converter._wrap_h3_list_blocks(raw_unclosed)
    assert wrapped == raw_unclosed


def test_linux_browser_discovery_and_no_sandbox_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Linux candidate discovery, MDTOPDF_BROWSER command name, and --no-sandbox flag."""
    monkeypatch.setenv("MDTOPDF_BROWSER", "custom-chromium-bin")
    monkeypatch.setattr(
        converter.shutil,
        "which",
        lambda cmd: "/opt/bin/custom-chromium" if cmd == "custom-chromium-bin" else None,
    )
    assert find_browser_executable() == "/opt/bin/custom-chromium"

    monkeypatch.delenv("MDTOPDF_BROWSER", raising=False)
    monkeypatch.setattr(converter.sys, "platform", "linux")
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda self: str(self).replace("\\", "/") == "/usr/bin/chromium",
    )
    assert find_browser_executable() == "/usr/bin/chromium"

    captured_cmd: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> MagicMock:
        captured_cmd.extend(cmd)
        return MagicMock(returncode=1, stderr="simulated exit")

    monkeypatch.setenv("CI", "true")
    with (
        patch("converter.find_browser_executable", return_value="/usr/bin/chromium"),
        patch("converter.subprocess.run", side_effect=fake_run),
        pytest.raises(RuntimeError, match="Browser PDF rendering failed"),
    ):
        render_pdf("# Linux CI", str(tmp_path), str(tmp_path / "linux.pdf"))

    assert "--no-sandbox" in captured_cmd


def test_render_pdf_passes_devnull_and_isolation_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify render_pdf passes stdin=DEVNULL and isolation flags to subprocess.run."""
    captured_kwargs: dict[str, Any] = {}
    captured_cmd: list[str] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured_cmd.extend(cmd)
        captured_kwargs.update(kwargs)
        # Create the fake output PDF so _wait_for_file_ready succeeds
        pdf_arg = next(arg for arg in cmd if arg.startswith("--print-to-pdf="))
        out_pdf = Path(pdf_arg.split("=", 1)[1])
        out_pdf.write_bytes(b"%PDF-1.4 simulated pdf")
        return MagicMock(returncode=0, stderr="")

    mock_doc = MagicMock()
    mock_doc.__len__.return_value = 1
    mock_page = MagicMock()
    mock_page.get_images.return_value = []
    mock_doc.__getitem__.return_value = mock_page

    with (
        patch("converter.find_browser_executable", return_value="dummy_browser.exe"),
        patch("converter.subprocess.run", side_effect=fake_run),
        patch("converter._open_pdf_with_retry", return_value=mock_doc),
    ):
        res = render_pdf("# Heading", str(tmp_path), str(tmp_path / "out.pdf"))

    assert res.page_count == 1
    assert captured_kwargs.get("stdin") == subprocess.DEVNULL
    assert any(arg.startswith("--crash-dumps-dir=") for arg in captured_cmd)
    assert "--disable-background-networking" in captured_cmd
    assert "--disable-sync" in captured_cmd
    assert "--disable-default-apps" in captured_cmd


def test_wait_for_file_ready(tmp_path: Path) -> None:
    """Verify _wait_for_file_ready returns True for existing non-empty file and False on timeout."""
    empty_file = tmp_path / "empty.txt"
    empty_file.write_bytes(b"")
    assert converter._wait_for_file_ready(empty_file, timeout=0.05, poll_interval=0.01) is False

    non_empty = tmp_path / "ready.txt"
    non_empty.write_bytes(b"content")
    assert converter._wait_for_file_ready(non_empty, timeout=0.1, poll_interval=0.01) is True


def test_open_pdf_with_retry_retries_on_transient_error() -> None:
    """Verify _open_pdf_with_retry retries on PermissionError and returns document."""
    attempts = [0]
    mock_doc = MagicMock()

    def fake_open(path: str) -> MagicMock:
        attempts[0] += 1
        if attempts[0] < 3:
            raise PermissionError("File in use by scanner")
        return mock_doc

    with patch("converter.pymupdf.open", side_effect=fake_open):
        doc = converter._open_pdf_with_retry("dummy.pdf", retries=5, delay=0.01)

    assert doc is mock_doc
    assert attempts[0] == 3


