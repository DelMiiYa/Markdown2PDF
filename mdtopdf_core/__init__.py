"""mdtopdf_core: GUI-free Markdown-to-PDF engine (single source of truth).

Consumed by the desktop app (``app.py``) and by the downstream CLI/skill repo.
Must never import tkinter or any GUI toolkit.
"""

from mdtopdf_core.converter import (
    MARGIN_PRESETS,
    ConversionOptions,
    ConversionResult,
    find_browser_executable,
    render_pdf,
    render_pdf_pages_to_pil,
)

__version__ = "1.2.0"

__all__ = [
    "MARGIN_PRESETS",
    "ConversionOptions",
    "ConversionResult",
    "__version__",
    "find_browser_executable",
    "render_pdf",
    "render_pdf_pages_to_pil",
]
