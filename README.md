# Markdown2PDF — Desktop Studio

A standalone desktop application for **Windows and Linux** that converts Markdown documents (with images, complex tables, inline HTML, and Thai/Unicode/Emoji typography) into clean, print-ready A4 PDFs.

## Key Features

- **Split-Pane Live Workspace:**
  - **Left Pane:** UTF-8 Markdown editor with syntax highlighting for headings, bold text, inline code, image links, and tables.
  - **Right Pane:** Pixel-accurate A4 print preview studio rendered via Headless Chromium and PyMuPDF, complete with page navigation, zoom (`−` / `+` / `Fit Width`), and embedded image verification badges.
- **Smart Local Image Resolution:**
  - Opening any `.md` file automatically sets its folder as the base directory so relative `images/...` paths (including URL-encoded spaces, parentheses, and Unicode characters like `µ`) resolve seamlessly in-place.
  - **Suppress Missing Images:** Automatically strips broken local image links before rendering so no broken-image icons appear in your PDF.
- **Print-Ready Layout Controls:**
  - Adjustable base font size (`7.5pt`–`14pt`), page margin presets (`Compact`, `Standard`, `Comfortable`, `Wide`), and table thumbnail size constraints (`W × H px`).
  - **Keep Q&A / Rows Together:** Prevents table rows and `<h3>` heading + bullet blocks from splitting across page breaks.
- **Thai, Math Symbol & Emoji Typography:**
  - Built-in font stack (`Leelawadee UI`, `Tahoma`, `Noto Sans Thai`, `Segoe UI Symbol`, `Segoe UI Emoji`) renders Thai script, technical symbols (`Ω`, `µ`, `±`, `×`, `➔`), and emojis (`🔔`, `⚠️`) cleanly without missing glyph boxes.

---

## Prerequisites

- **OS:** Windows 10/11 or Linux (x86_64)
- **Browser Engine:** Microsoft Edge, Google Chrome, or Chromium installed on the system (used in headless mode for high-fidelity Skia PDF output)
- **Python:** Python 3.10+ (when running from source)

---

## Installation & Usage

### Option 1: Run from Source

1. Clone the repository and install dependencies:
   ```bash
   git clone https://github.com/DelMiiYa/Markdown2PDF.git
   cd Markdown2PDF
   pip install -r requirements.txt
   ```
2. Launch the desktop app:
   - **Windows:** Double-click `run_app.bat` or run:
     ```powershell
     python -X utf8 app.py
     ```
   - **Linux:**
     ```bash
     python3 -X utf8 app.py
     ```

### Option 2: Install as a Python Package

```bash
pip install .
markdown2pdf
```

### Option 3: Build Standalone Executable (`PyInstaller`)

- **Windows (`dist\Markdown2PDF.exe`):**
  ```bat
  build_exe.bat
  ```
- **Linux (`dist/Markdown2PDF`):**
  ```bash
  pyinstaller --noconfirm --clean --onefile --windowed --name Markdown2PDF --collect-submodules markdown app.py
  ```

---

## Keyboard Shortcuts

| Shortcut | Action |
| :--- | :--- |
| `Ctrl+N` | New Markdown document |
| `Ctrl+O` | Open `.md` file |
| `Ctrl+S` | Save `.md` file |
| `Ctrl+Shift+S` | Save `.md` file as... |
| `Ctrl+I` | Insert local image at cursor |
| `F5` | Refresh A4 print preview |
| `Ctrl+E` | Export print-ready PDF |
| `Ctrl++` / `Ctrl+-` / `Ctrl+0` | Zoom In / Zoom Out / Reset Zoom (100%) |
