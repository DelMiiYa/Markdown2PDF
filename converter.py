"""
Core Markdown-to-PDF conversion engine for MD-to-PDF Desktop Studio.
Handles Markdown pre-filtering, Thai-optimized print CSS, headless Chromium
rendering, and PyMuPDF verification & preview image generation.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import markdown
import pymupdf
from PIL import Image


@dataclass
class ConversionOptions:
    """Configuration options for Markdown-to-PDF conversion."""

    font_size_pt: float = 9.5
    line_height: float = 1.45
    margin_css: str = "12mm 14mm"
    page_size: str = "A4 portrait"
    img_max_width: int = 90
    img_max_height: int = 70
    suppress_missing_images: bool = True
    keep_blocks_together: bool = True
    accent_color: str = "#0d47a1"
    title: str = "Markdown Document"


@dataclass
class ConversionResult:
    """Metadata and verification results from a PDF conversion."""

    output_pdf_path: str
    page_count: int
    embedded_images: int
    suppressed_images: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0


MARGIN_PRESETS: dict[str, str] = {
    "Compact (10×12mm)": "10mm 12mm",
    "Standard (12×14mm)": "12mm 14mm",
    "Comfortable (15×18mm)": "15mm 18mm",
    "Wide (18×20mm)": "18mm 20mm",
}


def find_browser_executable() -> str:
    """Locate Microsoft Edge, Google Chrome, or Chromium on Windows or Linux."""
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    win_candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    if local_app_data:
        win_candidates.extend(
            [
                os.path.join(local_app_data, r"Microsoft\Edge\Application\msedge.exe"),
                os.path.join(local_app_data, r"Google\Chrome\Application\chrome.exe"),
            ]
        )

    linux_candidates = [
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ]

    for path in win_candidates + linux_candidates:
        if path and os.path.isfile(path):
            return path

    for cmd in (
        "msedge",
        "microsoft-edge",
        "microsoft-edge-stable",
        "chrome",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        found = shutil.which(cmd)
        if found:
            return found

    raise FileNotFoundError(
        "Could not find Microsoft Edge, Google Chrome, or Chromium for headless PDF rendering."
    )


def _is_remote_or_data_uri(src: str) -> bool:
    """Return True if the image source is an external URL or data URI."""
    lowered = src.strip().lower()
    return lowered.startswith(("http://", "https://", "data:", "file://"))


def _resolve_local_image(src: str, base_dir: str) -> tuple[bool, Optional[Path]]:
    """
    Resolve a local image path (possibly URL-encoded) against base_dir.
    Returns (exists, resolved_path).
    """
    unquoted = urllib.parse.unquote(src.strip())
    candidate = Path(unquoted)
    if not candidate.is_absolute():
        candidate = Path(base_dir) / candidate
    try:
        resolved = candidate.resolve()
        return resolved.is_file(), resolved
    except OSError:
        return False, None


def preprocess_markdown(
    md_text: str, base_dir: str, options: ConversionOptions
) -> tuple[str, list[str]]:
    """
    Inspect Markdown image references against base_dir.
    If options.suppress_missing_images is True, strips missing local image tags
    so no broken-image icon appears in the rendered PDF.
    """
    suppressed: list[str] = []

    md_img_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

    def replace_md_img(match: re.Match) -> str:
        raw_src = match.group(2).strip()
        # Strip optional title in markdown image syntax: ![alt](path "title")
        src_path = raw_src.split('"')[0].split("'")[0].strip()
        if _is_remote_or_data_uri(src_path):
            return match.group(0)

        exists, _ = _resolve_local_image(src_path, base_dir)
        if not exists and options.suppress_missing_images:
            suppressed.append(src_path)
            return ""
        return match.group(0)

    cleaned = md_img_pattern.sub(replace_md_img, md_text)
    return cleaned, suppressed


def _resolve_html_image_uris(
    html_body: str, base_dir: str, options: ConversionOptions, suppressed: list[str]
) -> str:
    """
    Rewrite relative <img src="..."> attributes in generated HTML to resolved
    file:/// URIs so headless Edge/Chrome can load them from any temp folder.
    Also suppresses missing raw HTML <img> tags when configured.
    """
    img_tag_pattern = re.compile(r'<img\b([^>]*?)\bsrc=(["\'])([^"\']+)\2([^>]*?)/?>', re.IGNORECASE)

    def replace_html_img(match: re.Match) -> str:
        before, quote, src, after = (
            match.group(1),
            match.group(2),
            match.group(3),
            match.group(4),
        )
        if _is_remote_or_data_uri(src):
            return match.group(0)

        exists, resolved_path = _resolve_local_image(src, base_dir)
        if exists and resolved_path is not None:
            file_uri = resolved_path.as_uri()
            return f'<img{before}src={quote}{file_uri}{quote}{after} />'

        if options.suppress_missing_images:
            if src not in suppressed:
                suppressed.append(src)
            return ""

        return match.group(0)

    return img_tag_pattern.sub(replace_html_img, html_body)


def build_css(options: ConversionOptions) -> str:
    """Generate the print-ready CSS stylesheet."""
    accent = options.accent_color
    return f"""
@page {{
    size: {options.page_size};
    margin: {options.margin_css};
}}
body {{
    font-family: 'Leelawadee UI', 'Tahoma', 'Noto Sans Thai', 'Segoe UI Symbol', 'Segoe UI Emoji', sans-serif;
    font-size: {options.font_size_pt}pt;
    line-height: {options.line_height};
    color: #1a1a1a;
    margin: 0;
}}
h1 {{
    font-size: {round(options.font_size_pt * 1.63, 1)}pt;
    color: {accent};
    text-align: center;
    margin-top: 0;
    margin-bottom: 6px;
    line-height: 1.25;
}}
h2 {{
    font-size: {round(options.font_size_pt * 1.21, 1)}pt;
    color: {accent};
    border-bottom: 1.5px solid #1976d2;
    padding-bottom: 3px;
    margin-top: 12px;
    margin-bottom: 6px;
    break-after: avoid;
    page-break-after: avoid;
}}
h3 {{
    font-size: {round(options.font_size_pt * 1.03, 1)}pt;
    color: #1565c0;
    margin-top: 8px;
    margin-bottom: 4px;
    break-after: avoid;
    page-break-after: avoid;
}}
h4 {{
    font-size: {options.font_size_pt}pt;
    color: #1e3a8a;
    margin-top: 6px;
    margin-bottom: 3px;
    break-after: avoid;
    page-break-after: avoid;
}}
h2 + p, h2 + ul, h2 + ol, h2 + table,
h3 + p, h3 + ul, h3 + ol, h3 + table {{
    break-before: avoid;
    page-break-before: avoid;
}}
.station-block {{
    break-inside: avoid;
    page-break-inside: avoid;
    margin-bottom: 6px;
}}
.station-block h3 {{
    margin-top: 5px;
    margin-bottom: 2px;
}}
.station-block ul, .station-block ol {{
    margin-top: 2px;
    margin-bottom: 5px;
}}
p {{
    margin: 3px 0 6px 0;
}}
ul, ol {{
    margin: 3px 0 6px 0;
    padding-left: 20px;
}}
li {{
    margin-bottom: 2px;
}}
table {{
    width: 100%;
    border-collapse: collapse;
    font-size: {round(max(7.5, options.font_size_pt - 1.0), 1)}pt;
    margin: 6px 0 10px 0;
}}
th, td {{
    border: 1px solid #a0aab2;
    padding: 4px 6px;
    vertical-align: middle;
}}
th {{
    background-color: #f0f4f9;
    color: {accent};
    font-weight: 600;
    text-align: center;
}}
tr {{
    break-inside: {"avoid" if options.keep_blocks_together else "auto"};
    page-break-inside: {"avoid" if options.keep_blocks_together else "auto"};
}}
tbody tr:nth-child(even) {{
    background-color: #fbfcfd;
}}
td img {{
    max-width: {options.img_max_width}px;
    max-height: {options.img_max_height}px;
    width: auto;
    height: auto;
    display: block;
    margin: 0 auto;
    object-fit: contain;
}}
p > img {{
    max-width: 100%;
    height: auto;
    display: block;
    margin: 6px auto;
}}
code {{
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 0.88em;
    background-color: #f3f4f6;
    padding: 1px 4px;
    border-radius: 3px;
    border: 1px solid #e0e0e0;
}}
pre {{
    background-color: #f6f8fa;
    border: 1px solid #d0d7de;
    border-radius: 4px;
    padding: 8px 10px;
    overflow-x: auto;
    font-size: 0.85em;
    break-inside: avoid;
    page-break-inside: avoid;
}}
pre code {{
    background: none;
    border: none;
    padding: 0;
}}
blockquote {{
    margin: 6px 0;
    padding: 4px 12px;
    border-left: 3.5px solid #1976d2;
    background-color: #f8fafc;
    color: #334155;
}}
hr {{
    border: none;
    border-top: 1px solid #cfd8dc;
    margin: 10px 0;
}}
"""


def build_html(
    md_text: str, base_dir: str, options: ConversionOptions
) -> tuple[str, list[str]]:
    """
    Convert Markdown text to a complete, print-styled HTML document.
    Returns (full_html_string, suppressed_images_list).
    """
    cleaned_md, suppressed = preprocess_markdown(md_text, base_dir, options)

    html_body = markdown.markdown(
        cleaned_md, extensions=["tables", "fenced_code"]
    )

    # Resolve local image paths to absolute file:/// URIs
    html_body = _resolve_html_image_uris(html_body, base_dir, options, suppressed)

    # Keep h3 + list blocks (such as Q&A stations) together across page breaks
    if options.keep_blocks_together:
        block_pattern = r"(<h3[^>]*>.*?</h3>\s*<(?:ul|ol)>.*?</(?:ul|ol)>)"
        html_body = re.sub(
            block_pattern,
            r'<div class="station-block">\1</div>',
            html_body,
            flags=re.DOTALL,
        )

    base_uri = Path(base_dir).resolve().as_uri() + "/"
    css = build_css(options)

    full_html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<base href="{base_uri}">
<title>{options.title}</title>
<style>
{css}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
    return full_html, suppressed


def render_pdf(
    md_text: str,
    base_dir: str,
    output_pdf_path: str,
    options: Optional[ConversionOptions] = None,
) -> ConversionResult:
    """
    Render Markdown text into a print-ready PDF file using Headless Edge/Chrome,
    then verify page count and embedded images with PyMuPDF.
    """
    if options is None:
        options = ConversionOptions()

    t0 = time.perf_counter()
    browser_exe = find_browser_executable()
    full_html, suppressed = build_html(md_text, base_dir, options)

    out_path = Path(output_pdf_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Write temporary HTML file in system temp directory so the workspace stays clean
    fd, temp_html_path = tempfile.mkstemp(prefix="mdtopdf_", suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(full_html)

        temp_html_uri = Path(temp_html_path).resolve().as_uri()
        cmd = [
            browser_exe,
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={out_path}",
            temp_html_uri,
        ]
        # Prevent console window flash on Windows GUI apps
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=creationflags,
            timeout=60,
        )
        if proc.returncode != 0 or not out_path.is_file():
            raise RuntimeError(
                f"Browser PDF rendering failed (exit {proc.returncode}): {proc.stderr}"
            )
    finally:
        if os.path.exists(temp_html_path):
            try:
                os.remove(temp_html_path)
            except OSError:
                pass

    # Inspect generated PDF with PyMuPDF
    doc = pymupdf.open(str(out_path))
    try:
        page_count = len(doc)
        embedded_images = sum(len(page.get_images()) for page in doc)
    finally:
        doc.close()

    elapsed = time.perf_counter() - t0
    return ConversionResult(
        output_pdf_path=str(out_path),
        page_count=page_count,
        embedded_images=embedded_images,
        suppressed_images=suppressed,
        elapsed_seconds=elapsed,
    )


def render_pdf_pages_to_pil(
    pdf_path: str, dpi: int = 120
) -> list[Image.Image]:
    """
    Render all pages of a PDF file into PIL Image objects for GUI preview display.
    """
    images: list[Image.Image] = []
    doc = pymupdf.open(pdf_path)
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            images.append(img)
    finally:
        doc.close()
    return images
