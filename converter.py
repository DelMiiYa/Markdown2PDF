"""Core Markdown-to-PDF conversion engine for MD-to-PDF Desktop Studio.

Handles Markdown pre-filtering, HTML sanitization, Thai-optimized print CSS,
headless Chromium rendering, and PyMuPDF verification & preview image generation.
"""

from __future__ import annotations

import contextlib
import html
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    allow_outside_base_dir: bool = False


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
    env_override = os.environ.get("MDTOPDF_BROWSER", "").strip()
    if env_override:
        if Path(env_override).is_file():
            return env_override
        which_override = shutil.which(env_override)
        if which_override:
            return which_override

    candidates: list[str] = []
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        candidates.extend(
            [
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
        )
        if local_app_data:
            base_local = Path(local_app_data)
            candidates.extend(
                [
                    str(base_local / r"Microsoft\Edge\Application\msedge.exe"),
                    str(base_local / r"Google\Chrome\Application\chrome.exe"),
                ]
            )
    else:
        candidates.extend(
            [
                "/usr/bin/microsoft-edge",
                "/usr/bin/microsoft-edge-stable",
                "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable",
                "/usr/bin/chromium",
                "/usr/bin/chromium-browser",
                "/snap/bin/chromium",
            ]
        )

    for path in candidates:
        if path and Path(path).is_file():
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
        "Could not find Microsoft Edge, Google Chrome, or Chromium "
        "for headless PDF rendering."
    )


def _is_remote_or_data_uri(src: str) -> bool:
    """Return True if the image source is an external HTTP(S) or data URI."""
    lowered = src.strip().lower()
    return lowered.startswith(("http://", "https://", "data:"))


def _parse_local_candidate(
    src: str, base_dir: str, allow_outside_base: bool = False
) -> tuple[bool, bool, Path | None]:
    """Parse and validate a local image path or file:// URI against base_dir.

    Returns (exists, is_allowed, resolved_path).
    """
    cleaned = html.unescape(src.strip())
    if cleaned.startswith("<") and cleaned.endswith(">"):
        cleaned = cleaned[1:-1].strip()

    if cleaned.lower().startswith("file:"):
        parsed = urllib.parse.urlsplit(cleaned)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            return False, False, None
        uri_path = urllib.parse.unquote(parsed.path)
        if sys.platform == "win32" and re.match(r"^/[a-zA-Z]:", uri_path):
            uri_path = uri_path[1:]
        unquoted = uri_path
    else:
        unquoted = urllib.parse.unquote(cleaned)

    if not unquoted or unquoted.startswith(("\\\\", "//")):
        return False, False, None

    try:
        base_resolved = Path(base_dir).resolve()
        candidate = Path(unquoted)
        if not candidate.is_absolute():
            candidate = base_resolved / candidate
        resolved = candidate.resolve()
        if not allow_outside_base and not resolved.is_relative_to(base_resolved):
            return False, False, None
        return resolved.is_file(), True, resolved
    except (OSError, ValueError):
        return False, False, None


def _resolve_local_image(
    src: str, base_dir: str, allow_outside_base: bool = False
) -> tuple[bool, Path | None]:
    """Resolve a local image path (HTML-escaped, URL-encoded, or file://) against base_dir.

    Blocks Windows UNC/network paths and enforces base_dir containment unless
    allow_outside_base is True.
    Returns (exists, resolved_path).
    """
    exists, is_allowed, resolved = _parse_local_candidate(
        src, base_dir, allow_outside_base
    )
    if not is_allowed:
        return False, None
    return exists, resolved


def preprocess_markdown(
    md_text: str, base_dir: str, options: ConversionOptions
) -> tuple[str, list[str]]:
    """Inspect Markdown image references against base_dir outside of code blocks.

    Strips leading UTF-8 BOM, rewrites valid local image paths with spaces or
    parentheses into file:/// URIs, and strips missing or disallowed local image
    tags.
    """
    md_text = md_text.lstrip("\ufeff")
    suppressed: list[str] = []

    # Mask fenced (3+ backticks/tildes) and inline (1+ backticks) code blocks
    masked_blocks: list[str] = []
    code_pattern = re.compile(
        r"(^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n.*?^[ \t]{0,3}\2[ \t]*$|(`+)(?:(?!\3)[^\n])+\3)",
        flags=re.MULTILINE | re.DOTALL,
    )

    def _mask_code(match: re.Match[str]) -> str:
        idx = len(masked_blocks)
        masked_blocks.append(match.group(0))
        return f"\x00CODEBLOCK_{idx}\x00"

    masked_md = code_pattern.sub(_mask_code, md_text)

    # Match ![alt](path) including balanced single-level parentheses or <...>
    md_img_pattern = re.compile(
        r"!\[([^\]]*)\]\(\s*(<[^>]+>|(?:[^()\s]+|\([^()]*\)|[ \t]+)+)\s*\)"
    )

    def replace_md_img(match: re.Match[str]) -> str:
        alt_text = match.group(1)
        raw_target = match.group(2).strip()

        title_suffix = ""
        if raw_target.startswith("<") and ">" in raw_target:
            angle_end = raw_target.find(">")
            src_path = raw_target[1:angle_end].strip()
            rem = raw_target[angle_end + 1 :].strip()
            if rem:
                title_suffix = f" {rem}"
        else:
            title_match = re.match(r'^(.*?)\s+("[^"]*"|\'[^\']*\')\s*$', raw_target)
            if title_match:
                src_path = title_match.group(1).strip()
                title_suffix = f" {title_match.group(2)}"
            else:
                src_path = raw_target.strip()

        if _is_remote_or_data_uri(src_path):
            return match.group(0)

        exists, is_allowed, resolved_path = _parse_local_candidate(
            src_path, base_dir, options.allow_outside_base_dir
        )
        if exists and is_allowed and resolved_path is not None:
            return f"![{alt_text}]({resolved_path.as_uri()}{title_suffix})"

        if not is_allowed or options.suppress_missing_images:
            suppressed.append(src_path)
            return ""
        return match.group(0)

    cleaned = md_img_pattern.sub(replace_md_img, masked_md)

    for idx, block in enumerate(masked_blocks):
        cleaned = cleaned.replace(f"\x00CODEBLOCK_{idx}\x00", block)

    return cleaned, suppressed


def _resolve_html_image_uris(
    html_body: str, base_dir: str, options: ConversionOptions, suppressed: list[str]
) -> str:
    """Rewrite relative <img src="..."> attributes in generated HTML to file:/// URIs.

    Always strips security-disallowed paths (UNC or outside base_dir), and strips
    missing local HTML <img> tags when options.suppress_missing_images is True.
    """
    img_tag_pattern = re.compile(
        r'<img\b([^>]*?)\bsrc=(["\'])([^"\']+)\2([^>]*?)/?>', re.IGNORECASE
    )

    def replace_html_img(match: re.Match[str]) -> str:
        before, quote, src, after = (
            match.group(1),
            match.group(2),
            match.group(3),
            match.group(4),
        )
        unescaped_src = html.unescape(src.strip())
        if _is_remote_or_data_uri(unescaped_src):
            return match.group(0)

        exists, is_allowed, resolved_path = _parse_local_candidate(
            unescaped_src, base_dir, options.allow_outside_base_dir
        )
        if exists and is_allowed and resolved_path is not None:
            file_uri = resolved_path.as_uri()
            return f"<img{before}src={quote}{file_uri}{quote}{after} />"

        if not is_allowed or options.suppress_missing_images:
            if unescaped_src not in suppressed:
                suppressed.append(unescaped_src)
            return ""

        return match.group(0)

    return img_tag_pattern.sub(replace_html_img, html_body)


def _sanitize_html(html_body: str) -> str:
    """Strip dangerous HTML elements, inline event handlers, and unsafe URI schemes."""
    # Remove paired dangerous blocks (<script>, <iframe>, <object>, <applet>, <style>)
    paired_tag_pattern = re.compile(
        r"<(script|iframe|object|applet|style)\b[^>]*>.*?</\1\s*>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    sanitized = paired_tag_pattern.sub("", html_body)

    # Remove standalone/void dangerous tags
    void_tag_pattern = re.compile(
        r"</?(?:script|iframe|object|embed|applet|style|meta|link|base|frame|frameset|form)\b"
        r"(?:[^>'\"/]|\"[^\"]*\"|'[^']*'|/)*>",
        flags=re.IGNORECASE,
    )
    sanitized = void_tag_pattern.sub("", sanitized)

    event_attr_pattern = re.compile(
        r"""(?:[\s/]+)on[a-z0-9_]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>/]+)""",
        flags=re.IGNORECASE,
    )
    attr_uri_pattern = re.compile(
        r"""(\b(?:href|action|formaction)\s*=\s*)(?:"([^"]*)"|'([^']*)'|([^\s>"']+))""",
        flags=re.IGNORECASE,
    )

    def _clean_uri_attr(attr_match: re.Match[str]) -> str:
        prefix = attr_match.group(1)
        raw_val = (
            attr_match.group(2)
            or attr_match.group(3)
            or attr_match.group(4)
            or ""
        )
        normalized = re.sub(r"[\x00-\x20]+", "", html.unescape(raw_val)).lower()
        if normalized.startswith(("javascript:", "vbscript:", "file:", "data:")):
            return f'{prefix}"#"'
        return attr_match.group(0)

    def _sanitize_opening_tag(tag_match: re.Match[str]) -> str:
        tag_text = tag_match.group(0)
        tag_text = event_attr_pattern.sub("", tag_text)
        return attr_uri_pattern.sub(_clean_uri_attr, tag_text)

    opening_tag_pattern = re.compile(
        r"<[a-zA-Z][a-zA-Z0-9:-]*(?:[^>'\"/]|\"[^\"]*\"|'[^']*'|/)*>"
    )
    return opening_tag_pattern.sub(_sanitize_opening_tag, sanitized)


def _wrap_h3_list_blocks(html_body: str) -> str:
    """Wrap <h3> headings and their immediately following top-level <ul>/<ol> in a div.

    Correctly tracks nested <ul>/<ol> tags so nested lists do not close the wrapper
    prematurely, and never spans across intermediate </h3> tags.
    """
    h3_open_pattern = re.compile(
        r"<h3\b[^>]*>(?:(?!</h3\s*>).)*?</h3\s*>\s*<(ul|ol)\b[^>]*>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    list_token_pattern = re.compile(r"<(/?)(ul|ol)\b[^>]*>", flags=re.IGNORECASE)

    result: list[str] = []
    pos = 0
    while True:
        match = h3_open_pattern.search(html_body, pos)
        if not match:
            result.append(html_body[pos:])
            break

        result.append(html_body[pos : match.start()])
        depth = 1
        scan_pos = match.end()
        closed_end = -1

        while depth > 0:
            tok = list_token_pattern.search(html_body, scan_pos)
            if not tok:
                break
            if tok.group(1) == "/":
                depth -= 1
            else:
                depth += 1
            scan_pos = tok.end()
            if depth == 0:
                closed_end = scan_pos

        if closed_end != -1:
            block = html_body[match.start() : closed_end]
            result.append(f'<div class="station-block">{block}</div>')
            pos = closed_end
        else:
            result.append(html_body[match.start() : match.end()])
            pos = match.end()

    return "".join(result)


def _clean_css_token(value: str) -> str:
    """Strip characters that could break out of a CSS property or <style> block."""
    return re.sub(r"[;<>{}\\]", "", value).strip()


def build_css(options: ConversionOptions) -> str:
    """Generate the print-ready CSS stylesheet."""
    accent = _clean_css_token(options.accent_color) or "#0d47a1"
    page_size = _clean_css_token(options.page_size) or "A4 portrait"
    margin_css = _clean_css_token(options.margin_css) or "12mm 14mm"
    raw_font = float(options.font_size_pt)
    font_pt = min(72.0, max(6.0, raw_font if math.isfinite(raw_font) else 9.5))
    raw_lh = float(options.line_height)
    line_h = min(3.0, max(1.0, raw_lh if math.isfinite(raw_lh) else 1.45))
    img_w = max(16, int(options.img_max_width))
    img_h = max(16, int(options.img_max_height))

    font_stack = (
        "'Leelawadee UI', 'Tahoma', 'Noto Sans Thai', 'Noto Sans', "
        "'Segoe UI Symbol', 'Segoe UI Emoji', 'Noto Color Emoji', sans-serif"
    )

    return f"""
@page {{
    size: {page_size};
    margin: {margin_css};
}}
body {{
    font-family: {font_stack};
    font-size: {font_pt}pt;
    line-height: {line_h};
    color: #1a1a1a;
    margin: 0;
}}
h1 {{
    font-size: {round(font_pt * 1.63, 1)}pt;
    color: {accent};
    text-align: center;
    margin-top: 0;
    margin-bottom: 6px;
    line-height: 1.25;
}}
h2 {{
    font-size: {round(font_pt * 1.21, 1)}pt;
    color: {accent};
    border-bottom: 1.5px solid #1976d2;
    padding-bottom: 3px;
    margin-top: 12px;
    margin-bottom: 6px;
    break-after: avoid;
    page-break-after: avoid;
}}
h3 {{
    font-size: {round(font_pt * 1.03, 1)}pt;
    color: #1565c0;
    margin-top: 8px;
    margin-bottom: 4px;
    break-after: avoid;
    page-break-after: avoid;
}}
h4 {{
    font-size: {font_pt}pt;
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
    font-size: {round(max(7.5, font_pt - 1.0), 1)}pt;
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
    max-width: {img_w}px;
    max-height: {img_h}px;
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
    font-family: 'Consolas', 'Noto Sans Mono', 'DejaVu Sans Mono', 'Courier New', monospace;
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
    """Convert Markdown text to a complete, sanitized, print-styled HTML document.

    Returns (full_html_string, suppressed_images_list).
    """
    cleaned_md, suppressed = preprocess_markdown(md_text, base_dir, options)

    html_body = markdown.markdown(
        cleaned_md, extensions=["tables", "fenced_code"]
    )

    # Sanitize raw/inline HTML before resolving local images
    html_body = _sanitize_html(html_body)

    # Resolve local image paths to absolute file:/// URIs
    html_body = _resolve_html_image_uris(html_body, base_dir, options, suppressed)

    # Keep h3 + list blocks (such as Q&A stations) together across page breaks
    if options.keep_blocks_together:
        html_body = _wrap_h3_list_blocks(html_body)

    base_uri = html.escape(Path(base_dir).resolve().as_uri() + "/", quote=True)
    safe_title = html.escape(options.title)
    css = build_css(options)
    csp = (
        "default-src 'none'; img-src file: https: http: data:; "
        "style-src 'unsafe-inline'; font-src file: data:;"
    )

    full_html = f"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<base href="{base_uri}">
<title>{safe_title}</title>
<style>
{css}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
    return full_html, suppressed


def _wait_for_file_ready(
    path: Path, timeout: float = 3.0, poll_interval: float = 0.05
) -> bool:
    """Poll until path exists and has non-zero size, absorbing filesystem/AV flush latency."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return True
        except OSError:
            pass
        time.sleep(poll_interval)
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _open_pdf_with_retry(
    pdf_path: str, retries: int = 25, delay: float = 0.06
) -> Any:
    """Open a PDF with PyMuPDF, retrying transient file-sharing locks from AV or browser."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return pymupdf.open(pdf_path)
        except (PermissionError, pymupdf.FileDataError, OSError) as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(delay)
    if last_exc:
        raise last_exc
    return pymupdf.open(pdf_path)


def render_pdf(
    md_text: str,
    base_dir: str,
    output_pdf_path: str,
    options: ConversionOptions | None = None,
) -> ConversionResult:
    """Render Markdown text into a print-ready PDF file using Headless Edge/Chrome.

    Verifies page count and embedded images with PyMuPDF.
    """
    if options is None:
        options = ConversionOptions()

    t0 = time.perf_counter()
    browser_exe = find_browser_executable()
    full_html, suppressed = build_html(md_text, base_dir, options)

    out_path = Path(output_pdf_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Write temporary HTML file in system temp directory so the workspace stays clean
    fd, temp_html_str = tempfile.mkstemp(prefix="mdtopdf_", suffix=".html")
    temp_html_path = Path(temp_html_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(full_html)

        temp_html_uri = temp_html_path.resolve().as_uri()
        with tempfile.TemporaryDirectory(
            prefix="mdtopdf_profile_", ignore_cleanup_errors=True
        ) as profile_dir:
            cmd = [
                browser_exe,
                "--headless",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                f"--user-data-dir={profile_dir}",
                f"--crash-dumps-dir={profile_dir}",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-default-apps",
                "--no-pdf-header-footer",
                f"--print-to-pdf={out_path}",
            ]
            if sys.platform.startswith("linux") and (
                os.environ.get("CI") == "true"
                or (hasattr(os, "geteuid") and os.geteuid() == 0)
            ):
                cmd.append("--no-sandbox")
            cmd.append(temp_html_uri)

            # Prevent console window flash on Windows GUI apps
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                timeout=60,
                check=False,
            )
            file_ready = _wait_for_file_ready(out_path, timeout=3.0)
            if proc.returncode != 0 or not file_ready:
                raise RuntimeError(
                    f"Browser PDF rendering failed (exit {proc.returncode}): "
                    f"{proc.stderr}"
                )
    finally:
        if temp_html_path.exists():
            with contextlib.suppress(OSError):
                temp_html_path.unlink()

    # Inspect generated PDF with PyMuPDF
    doc: Any = _open_pdf_with_retry(str(out_path))  # type: ignore[no-untyped-call]
    try:
        page_count = len(doc)
        embedded_images = sum(
            len(doc[page_idx].get_images()) for page_idx in range(page_count)
        )
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
    """Render all pages of a PDF file into PIL Image objects for GUI preview display."""
    images: list[Image.Image] = []
    doc: Any = _open_pdf_with_retry(pdf_path)  # type: ignore[no-untyped-call]
    try:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            images.append(img)
    finally:
        doc.close()
    return images
