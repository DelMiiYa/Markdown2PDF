# QA & Security Code Review Report — Markdown2PDF Desktop Studio (v1.1.0)

**Target Repository:** `e:\Antigravity-Projects\mdtopdf`  
**Audit Environment:** Windows 10/11 (`win32`), Python `3.13.0`, Microsoft Edge (Headless Chromium), PyMuPDF `1.28.2`, Pillow `13.0.0`, Markdown `3.11.2`, PyInstaller `6.22.3`  
**Audit Mode:** First-pass read-only audit (no source files or `dist/` modified; expanded test suite in `tests/test_converter.py`).

---

## 1. Executive Summary

### Release Recommendation: **NO-GO (Conditional on fixing 2 Critical & 4 High findings)**

While the core workflow (live split-pane Markdown editing, headless Edge/Chrome PDF generation, PyMuPDF page/image verification, and Thai/Unicode/math/emoji rendering) works on the happy path and passed end-to-end GUI smoke testing, this audit identified **2 Critical**, **4 High**, **7 Medium**, and **4 Low** issues—all **16 reproducible issues verified by automated tests or GUI execution**.

### Architecture Summary (Step 1 Orientation)
1. **Entry Points & Packaging:** `app.py` (`main()`, line `1197`) launches the Tkinter desktop studio via `run_app.bat` (`pythonw -X utf8 app.py`), the `pyproject.toml` GUI entry point (`markdown2pdf = "app:main"`), or a single-file PyInstaller executable built via `build_exe.bat` and `.github/workflows/build.yml` (Windows & Ubuntu matrix).
2. **Split-Pane GUI (`app.py`):** `MdToPdfStudioApp` (`app.py:50`) provides a left-hand `tk.Text` UTF-8 editor with debounced regex syntax highlighting (`_apply_syntax_highlighting`, `app.py:883`) and a right-hand `tk.Canvas` displaying zoomed/paginated `ImageTk.PhotoImage` page cards (`_redraw_preview_canvas`, `app.py:1056`).
3. **Background Preview Pipeline (`app.py`):** Editor modifications (debounced at `1200ms`) and layout option changes (`300ms`) increment `_render_generation` (`app.py:945`) and spawn a daemon thread (`_background_render_worker`, `app.py:960`) serialized by `_render_lock`, pushing rendered `PIL.Image` pages to `_render_queue` polled every `100ms` (`_poll_render_queue`, `app.py:982`).
4. **Markdown & HTML Pipeline (`converter.py`):** `build_html()` (`converter.py:524`) strips UTF-8 BOM and resolves/suppresses Markdown images (`preprocess_markdown`, `converter.py:162`), converts Markdown to HTML (`tables`, `fenced_code`), runs regex sanitization (`_sanitize_html`, `converter.py:273`), rewrites HTML `<img src>` attributes (`_resolve_html_image_uris`, `converter.py:234`), wraps `<h3>` + list blocks (`_wrap_h3_list_blocks`, `converter.py:306`), and injects A4 print CSS (`build_css`, `converter.py:358`) and a `<meta http-equiv="Content-Security-Policy">` header.
5. **Headless PDF Rendering & Verification (`converter.py`):** `render_pdf()` (`converter.py:573`) writes a temp `.html` file (`tempfile.mkstemp`), invokes headless Edge/Chrome/Chromium (`find_browser_executable`, `converter.py:64`) with `--print-to-pdf` and a temporary `--user-data-dir`, and inspects the generated PDF with `pymupdf` (`converter.py:645`) to verify page count, embedded images, and rasterized preview pages (`render_pdf_pages_to_pil`, `converter.py:664`).

### Top 5 Risks
1. **[CRITICAL — F-01] `_sanitize_html()` Corrupts Code Blocks and Prose Containing `on... = ...` (`converter.py:290-294`, `538`):** `event_attr_pattern` (`r"""\s+on[a-z0-9_]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>/]+)"""`) runs across the entire HTML body—including `<pre><code>` blocks and paragraph text—silently deleting any variable assignment or phrase starting with `on` followed by `=` (e.g., `online = True`, `on_error = "retry"`, `on_state = 1`) (**VERIFIED**).
2. **[CRITICAL — F-02] `_wrap_h3_list_blocks()` Over-Wraps Multi-Section Content Across `<h3>` Boundaries (`converter.py:312-314`, `342-345`):** Because `h3_open_pattern = re.compile(r"<h3[^>]*>.*?</h3>\s*<(ul|ol)\b[^>]*>", re.IGNORECASE | re.DOTALL)` allows `.*?` to span across `</h3>`, any `<h3>` NOT immediately followed by a list causes the regex to match from that first `<h3>` across all intermediate paragraphs, tables, and `<h2>` sections up to the next `<h3>` that has a list—wrapping entire multi-page sections in a single `break-inside: avoid` `.station-block` div (**VERIFIED**).
3. **[HIGH — F-03] Path Traversal (`../`), UNC (`//`), and Missing-Image Suppression Bypassed via `file://` URIs and `suppress_missing_images=False` (`converter.py:126-129`, `212-224`, `253-268`, `560`):** `_is_remote_or_data_uri()` treats `file://` as an external passthrough URI, so `![x](file:///C:/outside_secret.png)` and `![x](file://attacker/share/a.png)` completely bypass `allow_outside_base_dir=False`, UNC blocking, and `suppress_missing_images=True`. Furthermore, unchecking "Suppress Missing Images" (`suppress_missing_images=False`) leaves `../outside.png` and `//attacker/share/a.png` in the HTML, where `<base href="file:///...">` resolves them directly (**VERIFIED**).
4. **[HIGH — F-04] `_sanitize_html()` Bypasses for Unquoted/Entity `href`, `<img/onerror=>`, and `<style>` Blocks (`converter.py:273-303`):** `_sanitize_html()` fails to strip `<style>` blocks (despite the comment at `converter.py:275`), slash-separated HTML5 event handlers (`<img/src="x"/onerror="alert(1)">`), unquoted `href=file:///C:/Windows/win.ini`, and HTML-entity-encoded URI schemes (`href="&#106;avascript:alert(1)"`, `href="&#102;ile:///..."`) (**VERIFIED**).
5. **[HIGH — F-05] Missing `psutil` in `pyproject.toml` `[project.optional-dependencies] dev` Breaks `pytest` Collection (`pyproject.toml:27-34`, `tests/test_converter.py:18`):** `tests/test_converter.py:18` unconditionally imports `psutil`, but `pyproject.toml` omits `psutil` (and `types-psutil`) from `dev` dependencies. Running `pip install -e ".[dev]"` followed by `pytest` in a clean virtual environment fails immediately with `ModuleNotFoundError: No module named 'psutil'` (**VERIFIED**).

---

## 2. Setup, Dependency & Static Analysis Summary (Steps 2 & 3)

### Step 2: Fresh Virtual Environment & Dependency Audit
- **Commands Run:**
  ```powershell
  python -m venv .venv
  .\.venv\Scripts\python -m pip install --upgrade pip
  .\.venv\Scripts\pip install -r requirements.txt
  .\.venv\Scripts\pip install -e ".[dev]"
  ```
- **Installed Versions (`requirements.txt`):** `Markdown-3.11.2`, `PyMuPDF-1.28.2`, `Pillow-13.0.0`, `pyinstaller-6.22.3` (plus `altgraph-0.17.5`, `packaging-26.3`, `pefile-2024.8.26`, `pyinstaller-hooks-contrib-2026.9`, `pywin32-ctypes-0.2.3`, `setuptools-84.2.1`).
- **Mismatches & Issues Found (VERIFIED):**
  1. **Missing `psutil` and `types-psutil` in `pyproject.toml:27-34` (`[project.optional-dependencies] dev`):** After installing `pip install -e ".[dev]"`, running `pytest tests/test_converter.py` fails with `ModuleNotFoundError: No module named 'psutil'` at `tests/test_converter.py:18`, and `mypy app.py converter.py tests/` fails with `Library stubs not installed for "psutil" [import-untyped]`.
  2. **`requirements.txt` vs `pyproject.toml` runtime dependency mismatch (`requirements.txt:4` vs `pyproject.toml:17-26`):** `requirements.txt:4` installs `pyinstaller>=6.0.0,<7.0.0` (and 6 packaging sub-dependencies) for all source users, whereas `pyproject.toml:24-26` isolates `pyinstaller` under `[project.optional-dependencies] build`.
  3. **Unpinned `dev` and CI dependencies (`pyproject.toml:28-33`, `.github/workflows/build.yml:52`):** `types-Markdown` has no version constraint at all, `pytest>=8.0`, `pytest-cov>=5.0`, `ruff>=0.5`, `mypy>=1.10`, and `bandit>=1.7` have no upper bounds, and `.github/workflows/build.yml:52` runs unpinned `pip install pytest psutil`.

### Step 3: Static Checks (`ruff`, `mypy`, `bandit`)

| Tool | Command | Result Summary (Noise Ignored) |
| :--- | :--- | :--- |
| **Ruff** (`0.17.3`) | `.\.venv\Scripts\ruff check app.py converter.py tests/`<br>`.\.venv\Scripts\ruff check app.py converter.py --select E,F,W,B,SIM,S,UP,ARG,PTH,RUF,PERF,BLE,C90,PL,TRY` | • **Default rule set:** `All checks passed!` (0 errors).<br>• **Extended rule set (`C90,PL,TRY,S`):** Flags high cyclomatic/statement complexity in `app.py:229` (`_build_ui`, 87 statements `PLR0915`), `app.py:750` (`export_pdf_dialog`, complexity 11 `C901`), and `app.py:883` (`_apply_syntax_highlighting`, complexity 12 `C901`), plus `TRY300` (`app.py:657`) and `S603`/`S606` subprocess notices.<br>• **Note on masked unused import:** `converter.py:19` (`import urllib.request`) is dead code not caught by `F401` because `import urllib.parse` (`converter.py:18`) binds `urllib`. |
| **Mypy** (`2.4.1`) | `.\.venv\Scripts\mypy app.py converter.py tests/`<br>`.\.venv\Scripts\mypy app.py converter.py --strict --ignore-missing-imports` | • **Default mode on `app.py converter.py`:** `Success: no issues found in 2 source files`.<br>• **Default mode including `tests/`:** **1 error** at `tests/test_converter.py:18`: `Library stubs not installed for "psutil" [import-untyped]`.<br>• **`--strict` mode on `converter.py`:** **6 errors** at `converter.py:645, 649, 652, 669, 677` (`Call to untyped function "Document" / "get_images" / "close" [no-untyped-call]` and `Generator has incompatible item type "int"; expected "bool" [misc]` on `sum(len(doc[page_idx].get_images()) ...)`). |
| **Bandit** (`1.9.4`) | `.\.venv\Scripts\bandit -r app.py converter.py` | **0 High, 0 Medium, 5 Low** across 1,601 LOC:<br>• `B404` (`app.py:16`, `converter.py:14`): `import subprocess`.<br>• `B606` (`app.py:841`): `os.startfile(res.output_pdf_path)`.<br>• `B603` (`app.py:844`, `converter.py:624`): `subprocess.Popen` / `subprocess.run` without `shell=True`. |

---

## 3. Findings Table (Step 5 Code Review)

| ID | Severity | Status | File:Line | Issue | Suggested Fix (Short Diff) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **F-01** | **Critical** | **VERIFIED** | `converter.py:290-301`, `538` | **`_sanitize_html()` Deletes Code Blocks & Prose Containing `on... = ...`:** `event_attr_pattern` (`r"""\s+on[a-z0-9_]+\s*=\s*(?:"[^"]*"\|'[^']*'\|[^\s>/]+)"""`) and `unsafe_href_pattern` are applied globally across `html_body`, not just inside HTML start tags. Any code block or sentence containing `online = True`, `on_error = "retry"`, or `on_state = 1` has those expressions silently deleted from the HTML/PDF. | Only sanitize attributes inside actual HTML opening tags `<tag ...>` (and skip `<pre><code>` blocks):<br>```diff
-    sanitized = event_attr_pattern.sub("", sanitized)
+    def _clean_tag(m: re.Match[str]) -> str:
+        tag = event_attr_pattern.sub("", m.group(0))
+        return unsafe_href_pattern.sub(r"\1#", tag)
+    sanitized = re.sub(r"<[a-zA-Z][^>]*>", _clean_tag, sanitized)
``` |
| **F-02** | **Critical** | **VERIFIED** | `converter.py:312-314`, `342-345` | **`_wrap_h3_list_blocks()` Over-Wraps Multi-Section Content Across `<h3>` Boundaries:** `h3_open_pattern` uses `r"<h3[^>]*>.*?</h3>\s*<(ul\|ol)\b[^>]*>"` with `re.DOTALL`. When an earlier `<h3>` is followed by a `<p>` or `<table>` instead of a list, `.*?` matches across `</h3>` and swallows all intermediate paragraphs, tables, and `<h2>` sections up to the next `<h3>` that has a list into a single `break-inside: avoid` `<div class="station-block">`. | Prevent matching across `</h3>` boundaries in `converter.py:312-314`:<br>```diff
     h3_open_pattern = re.compile(
-        r"<h3[^>]*>.*?</h3>\s*<(ul|ol)\b[^>]*>", flags=re.IGNORECASE | re.DOTALL
+        r"<h3\b[^>]*>(?:(?!</h3\s*>).)*?</h3>\s*<(ul|ol)\b[^>]*>",
+        flags=re.IGNORECASE | re.DOTALL,
     )
``` |
| **F-03** | **High** | **VERIFIED** | `converter.py:126-129`, `212-224`, `253-268`, `560` | **`file://` Image URIs & `suppress_missing_images=False` Bypass Path Containment, UNC Blocking, and Missing-File Suppression:** (1) `_is_remote_or_data_uri()` returns `True` for `file://`, so `![x](file:///C:/outside.png)`, `![x](file://attacker/share/a.png)`, and missing `file:///...` images bypass `_resolve_local_image()` (defeating `allow_outside_base_dir=False`, UNC blocking, and `suppress_missing_images=True`). (2) When `suppress_missing_images=False`, blocked `../outside.png` and `//attacker/a.png` paths are left in HTML where `<base href="file:///...">` resolves them. | Convert `file://` URIs to local paths for validation in `_resolve_local_image()`, and always strip security-disallowed (`../` / UNC) paths even when `suppress_missing_images=False`:<br>```diff
 def _is_remote_or_data_uri(src: str) -> bool:
     lowered = src.strip().lower()
-    return lowered.startswith(("http://", "https://", "data:", "file://"))
+    return lowered.startswith(("http://", "https://", "data:"))
``` |
| **F-04** | **High** | **VERIFIED** | `converter.py:275-302` | **HTML Sanitizer (`_sanitize_html`) Bypasses (`<style>`, `<img/onerror=>`, Unquoted & Entity-Encoded `href`):** (1) Comment at line `275` states `<style>` is removed, but `style` is missing from `paired_tag_pattern`. (2) `event_attr_pattern` requires `\s+`, missing `<img/src="x"/onerror="alert(1)">`. (3) `unsafe_href_pattern` requires quotes `["']` and literal scheme names, missing `<a href=file:///C:/Windows/win.ini>` and `<a href="&#106;avascript:alert(1)">`. | Include `style` in `paired_tag_pattern`, allow `[\s/]+` before `on*=`, unescape HTML entities before checking URI schemes, and handle unquoted attribute values:<br>```diff
-        r"<(script|iframe|object|applet)\b[^>]*>.*?</\1\s*>",
+        r"<(script|iframe|object|applet|style)\b[^>]*>.*?</\1\s*>",
``` |
| **F-05** | **High** | **VERIFIED** | `pyproject.toml:27-34`, `requirements.txt:4`, `tests/test_converter.py:18` | **Missing `psutil` in `pyproject.toml` `[dev]` Breaks `pytest`, Plus `pyinstaller` Mismatch:** `tests/test_converter.py:18` imports `psutil`, which is missing from `pyproject.toml:27-34` (`dev` extras). Running `pip install -e ".[dev]" && pytest` crashes with `ModuleNotFoundError: No module named 'psutil'`, and `mypy` fails with missing `types-psutil`. Also, `pyinstaller` is in `requirements.txt:4` but optional in `pyproject.toml:24-26`. | Add `psutil` and `types-psutil` to `pyproject.toml:27-34` and remove `pyinstaller` from runtime `requirements.txt`:<br>```diff
 dev = [
     "pytest>=8.0,<10.0",
+    "psutil>=5.9,<8.0",
+    "types-psutil",
``` |
| **F-06** | **High** | **VERIFIED** | `app.py:591-604`, `935-945`, `987-1011` | **Stale Background Preview Race Overwrites `New Document` / Empty Editor:** Neither `new_document()` (`app.py:591`) nor the empty-editor branch in `trigger_preview_render()` (`app.py:937-943`) increments `self._render_generation` or cancels `self._debounce_after_id`. An in-flight worker from the previous document finishes with `gen == self._render_generation` and paints the old PDF pages over the empty workspace. | Increment `self._render_generation` and cancel `_debounce_after_id` in `new_document()` and when `not md_text.strip()` in `trigger_preview_render()`:<br>```diff
         if not md_text.strip():
+            self._render_generation += 1
             self._raw_pil_pages.clear()
``` |
| **F-07** | **Medium** | **VERIFIED** | `app.py:775-805`, `566`, `191-195` | **Re-Entrant `self.root.update()` Loop & Missing `_render_lock` in `export_pdf_dialog()`:** `export_pdf_dialog()` spins `while not done_event.wait(0.02): self.root.update()` (`app.py:798-803`). While `btn_export` is disabled, `<Control-e>` (`app.py:566`) and the `File` menu remain active during `root.update()`, allowing recursive re-entry. Also, `_export_worker` does not acquire `self._render_lock`. | Guard `export_pdf_dialog()` with a `self._is_exporting` flag, acquire `self._render_lock` in `_export_worker`, and poll completion via `self.root.after(50, ...)` instead of `self.root.update()`. |
| **F-08** | **Medium** | **VERIFIED** | `converter.py:205-210` | **Image Filenames with Apostrophes (`'`) Truncated and Falsely Suppressed:** In `preprocess_markdown()`, when an image tag has no title attribute, `src_path = raw_target.split('"')[0].split("'")[0].strip()` (`converter.py:210`) splits on single quotes. `![Diagram](images/user's_diagram.png)` is truncated to `images/user` and stripped as a missing image. | Do not split unquoted filenames on `'` when `title_match` is `None`:<br>```diff
-                src_path = raw_target.split('"')[0].split("'")[0].strip()
+                src_path = raw_target.strip()
``` |
| **F-09** | **Medium** | **VERIFIED** | `app.py:709-717`, `converter.py:189-191` | **`insert_image_dialog()` Emits Broken Markdown for Filenames Containing `[` or `]`:** `app.py:715-716` sets `alt_text = chosen.stem` and inserts `![{alt_text}]({md_ref})` without escaping brackets in `alt_text`. For `chart [v1].png`, `![chart [v1]](...)` fails to match `r"!\[([^\]]*)\]..."` in `converter.py:190` and `python-markdown`, leaving broken syntax in the PDF. | Strip or escape `[` and `]` in `alt_text` in `app.py:715`:<br>```diff
-        alt_text = chosen.stem
+        alt_text = re.sub(r"[\[\]]", "", chosen.stem) or "image"
``` |
| **F-10** | **Medium** | **VERIFIED** | `converter.py:175-180` | **`preprocess_markdown()` Fails to Mask Double-Backtick Inline Code (` ``... ` ...`` `) & 4-Backtick Fences:** `code_pattern` only matches single-backtick spans `` `[^`\n]+` `` and 3-backtick fences `^``` `. A double-backtick inline code span containing a backtick (e.g. `` ``prefix ` ![Ex](missing.png)`` ``) is unmasked after the inner backtick, stripping the image syntax inside the code span. | Match multi-backtick inline code spans (`` (`+)[^\n]*?\1 ``) and variable-length fence delimiters (`^([`~]{3,})[^\n]*\n.*?^\1`) in `converter.py:176-179`. |
| **F-11** | **Medium** | **SUSPECTED** | `converter.py:601-639` | **Orphaned Chromium Child Processes & Leaked Temp Profile Dir on Timeout:** On `subprocess.TimeoutExpired` (`timeout=60`), `subprocess.run()` on Windows only terminates the parent browser process, leaving child renderer/GPU processes alive and holding locks on `profile_dir` (`--user-data-dir`), which `ignore_cleanup_errors=True` silently leaves behind in `%TEMP%`. | Use `subprocess.Popen` and on `TimeoutExpired` kill the process tree (`taskkill /F /T /PID` on Windows, `os.killpg` on Linux) before cleaning up `profile_dir`. |
| **F-12** | **Medium** | **VERIFIED** | `converter.py:358-367`, `380`, `450`, `472-473` | **`build_css()` Does Not Validate or Clamp Numeric `ConversionOptions` Fields:** Direct callers of `converter.build_css()` / `render_pdf()` passing `font_size_pt <= 0`, `NaN`, `inf`, or negative `img_max_width`/`img_max_height` emit broken CSS (`font-size: nanpt;`, `max-width: -50px;`). | Clamp `font_pt`, `line_h`, `img_w`, and `img_h` inside `build_css()` (`converter.py:363-366`) just as `app.py:722-734` does. |
| **F-13** | **Medium** | **VERIFIED** | `converter.py:66-68`, `368-371`, `487` | **Linux Emoji/Code Font Stack Gaps & `MDTOPDF_BROWSER` Command Lookup:** (1) `font_stack` (`converter.py:368-371`) omits Linux `'Noto Color Emoji'` and `'Noto Sans'` (listing only Windows `'Segoe UI Emoji'` / `'Segoe UI Symbol'`), and `code` (`converter.py:487`) only lists `'Consolas', 'Courier New'`. (2) `find_browser_executable()` (`converter.py:67`) only checks `Path(env_override).is_file()`, ignoring `shutil.which(env_override)` and silently ignoring invalid `MDTOPDF_BROWSER` paths. | Add `'Noto Sans'`, `'Noto Color Emoji'` to `font_stack` and `'Noto Sans Mono'`, `'DejaVu Sans Mono'` to `code`; check `shutil.which(env_override)` in `find_browser_executable()`. |
| **F-14** | **Low** | **VERIFIED** | `.github/workflows/build.yml:7-8, 16-17`; `.gitignore:10`; `Markdown2PDF.spec` | **CI Job Grants `contents: write` on `pull_request` & Repo Tracks Ignored `Markdown2PDF.spec`:** (1) `build.yml:16-17` sets `permissions: contents: write` on the `build` job which also runs on `pull_request`. (2) `Markdown2PDF.spec` is committed to git despite `*.spec` in `.gitignore:10`, and `.gitignore` omits `.coverage`, `.mypy_cache/`, and `.ruff_cache/`. | Keep `permissions: contents: read` on `build` and move release publishing to a tag-only job; untrack `Markdown2PDF.spec` or remove `*.spec` from `.gitignore`, and add `.coverage`, `.mypy_cache/`, `.ruff_cache/`. |
| **F-15** | **Low** | **VERIFIED** | `converter.py:19`, `645-652`, `669-677` | **Unused `import urllib.request` & `mypy --strict` Type Errors on `pymupdf`:** `converter.py:19` imports `urllib.request` (unused). Under `mypy --strict`, `converter.py:645, 649, 652, 669, 677` fail because `pymupdf` methods are untyped (`int(...)` needed around `len(doc[page_idx].get_images())`). | Remove `import urllib.request` at `converter.py:19` and annotate `page_count: int = len(doc)` / `int(len(doc[page_idx].get_images()))`. |
| **F-16** | **Low** | **VERIFIED** | `app.py:900-903`, `918-929` | **Syntax Highlighter Toggles Code Fence on Mismatched Delimiter (` ``` ` vs `~~~`):** `_apply_syntax_highlighting()` toggles `in_code_fence = not in_code_fence` on any line starting with ` ``` ` or `~~~`. A `~~~` line inside a ```` ``` ```` block prematurely ends code-fence highlighting, causing `# comments` inside code to be styled as `md_h1`. | Track the active opening fence marker (`"```"` or `"~~~"`) instead of a boolean so only a matching fence closes the block. |
| **F-17** | **Low** | **SUSPECTED** | `app.py:971-976` | **Preview Temp PDF Deletion Not in `finally:` Block:** In `_background_render_worker()`, `gen_pdf_path.unlink()` (`app.py:975`) is inside the `try:` block after `render_pdf_pages_to_pil()`. If `render_pdf_pages_to_pil()` raises, `_preview_{gen}.pdf` remains in `self._temp_dir` until process exit. | Move `with contextlib.suppress(OSError): gen_pdf_path.unlink()` into a `finally:` block in `_background_render_worker()`. |

---

## 4. Test Results & GUI Smoke Test (Steps 4 & 6)

### Automated Test Suite (`tests/test_converter.py`)
- **Command Run:**
  ```powershell
  .\.venv\Scripts\pytest tests/test_converter.py -v --cov=converter --cov-report=term-missing
  ```
- **Results:** **48 passed, 0 failed** in `9.98s`
- **Coverage (`converter.py`):** **100%** (`245` statements, `0` missed)
  ```text
  Name           Stmts   Miss  Cover   Missing
  --------------------------------------------
  converter.py     245      0   100%
  --------------------------------------------
  TOTAL            245      0   100%
  ```
- **Test Coverage Breakdown across Step 4 Requirements & Audit Reproductions:**
  - **Basic Markdown (`test_basic_markdown_html_generation`):** Headings (`h1`–`h4`), unordered/ordered lists, inline/fenced code blocks, tables, blockquotes, horizontal rules.
  - **Local Images (`test_local_image_*`, `test_preprocess_markdown_*`, `test_resolve_local_image_*`, `test_remote_and_data_uri_*`):** Relative paths, URL-encoded spaces (`%20`), parentheses (`(1)` and `%281%29`), angle-bracket targets (`<...>`), optional titles, Unicode/Thai/`µ`/`&` filenames, apostrophe truncation reproduction (**F-08**), double-backtick inline code reproduction (**F-10**), and missing image suppression (`suppress_missing_images=True` vs `False`).
  - **Thai, Emoji & Math Symbols (`test_thai_emoji_and_math_symbols_in_html`, `test_real_browser_end_to_end_conversion_and_preview`):** Thai script, `🔔`, `⚠️`, `Ω`, `µ`, `±`, `×`, `➔`.
  - **Inline & Malicious HTML (`test_inline_html_and_malicious_html_sanitized`, `test_title_html_injection_escaped_and_csp_present`, `test_real_browser_script_and_local_file_iframe_blocked`, `test_sanitize_html_*`, `test_file_uri_bypasses_*`):** Safe inline HTML retention, `<script>` and `file:///` `<iframe>` neutralization in real headless Edge, plus reproductions of **F-01** (`on... = ...` deleted in code blocks/prose), **F-03** (`file:///` containment & suppression bypass), and **F-04** (unquoted `href`, entity-encoded scheme, `<img/onerror=>`, `<style>`).
  - **Edge-Case Inputs (`test_empty_and_whitespace_input`, `test_huge_input_performance_and_correctness`, `test_utf8_bom_preserves_first_heading`, `test_invalid_utf8_file_handling_and_surrogates`):** Empty/whitespace input, 9,000-line (~160 KB) Markdown with 1,000 tables, UTF-8 BOM (`\ufeff`), invalid UTF-8 bytes (`\xff\xfe`), and lone surrogates (`\ud800`).
  - **Font Size & Margin Presets (`test_font_size_and_margin_presets` — 12 parametrized cases):** Min (`7.5pt`), default (`9.5pt`), and max (`14.0pt`) font sizes across all 4 margin presets (`Compact`, `Standard`, `Comfortable`, `Wide`) and min/max image bounds (`30px`–`400px`).
  - **Page-Break-Avoid Options (`test_keep_blocks_together_*`, `test_wrap_h3_list_blocks_overwraps_across_non_list_h3_bug`, `test_resolve_local_image_angle_brackets_and_unclosed_h3_list`):** `.station-block` wrapping, nested lists, unclosed lists, and reproduction of **F-02** (over-wrapping across earlier non-list `<h3>` sections).
  - **No Browser Installed & Discovery (`test_no_browser_installed_raises_clear_file_not_found`, `test_find_browser_executable_*`, `test_linux_browser_discovery_and_no_sandbox_flag`):** Clear `FileNotFoundError` when no browser is found, `MDTOPDF_BROWSER` env override, `shutil.which` fallback, and Linux `--no-sandbox` CI flag.
  - **Output Path Problems (`test_output_path_browser_failure`, `test_output_path_locked_or_unwritable_directory`, `test_output_path_read_only_or_locked_file`, `test_real_browser_end_to_end_conversion_and_preview`):** Read-only target file, unwritable parent path, and output paths with spaces, parentheses, Thai characters, and `µ` (`โฟลเดอร์ ทดสอบ (Output)/เอกสาร ทดสอบ µ.pdf`).
  - **Temp File & Process Cleanup (`test_temp_html_cleaned_up_on_browser_exception_and_timeout`, `test_real_browser_no_leftover_processes_and_temp_cleanup`):** Verified zero leaked `mdtopdf_*.html` files and zero leftover browser processes via `psutil`.

### Step 6: Desktop GUI Smoke Test (`app.py`) — VERIFIED
- **Command Run:**
  ```powershell
  $env:TCL_LIBRARY="C:\Users\User\AppData\Local\Programs\Python\Python313\tcl\tcl8.6"
  $env:TK_LIBRARY="C:\Users\User\AppData\Local\Programs\Python\Python313\tcl\tk8.6"
  .\.venv\Scripts\python -X utf8 C:\Users\User\.gemini\antigravity\brain\f4a83465-99ef-43d7-a376-a2db2b837587\scratch\smoke_test_gui.py
  ```
- **Results (`SMOKE_TEST_PASSED`):**
  1. Launched `MdToPdfStudioApp` on the Windows display and opened a sample `.md` containing headings, a table, a valid local image (`diagram (1) µ.png`), a missing local image (`missing_ghost.png`), Thai text (`เอกสารทดสอบ Smoke Test`), math symbols (`100 Ω`, `25 µA`, `± 5%`, `10 × 2 ➔ 20`), and emojis (`🔔 ⚠️`).
  2. Verified non-blocking background preview render completed (`1 page | 1 image` badge, 1 PIL page, 1 `PhotoImage` on canvas).
  3. Verified zoom controls (`adjust_zoom(+0.15)` -> `115%`, `fit_preview_to_width()`, `set_zoom(1.0)` -> `100%`) and page navigation (`scroll_to_page_delta`).
  4. Exported PDF via `export_pdf_dialog()` and verified with PyMuPDF:
     - **Page Count:** `1`
     - **Page Size:** `595.0 × 841.9 pt` (standard A4 portrait)
     - **Text Extractable:** Confirmed Thai heading (`เอกสารทดสอบ Smoke Test`) and symbols (`100 Ω`, `25 µA`, `± 5%`) extracted cleanly
     - **Images Embedded:** `1` (valid image embedded; missing image suppressed)
  5. Reproduced GUI findings **F-06** (`F06=True`), **F-09** (`F09=True`), and **F-16** (`F16=True`), and verified `_on_close()` cleaned up `mdtopdf_studio_*`.

---

## 5. Things That Could Not Be Tested, and Why

1. **Native Linux Desktop GUI & Font Rendering (`linux` X11/Wayland):** The audit machine runs Windows (`win32`). While Linux browser discovery and `--no-sandbox` CLI flag generation were unit-tested via `monkeypatch`, actual execution on an Ubuntu/Linux desktop (`xdg-open`, `<Button-4>`/`<Button-5>` scroll events, and Noto font rendering in Linux Chromium) could not be run locally.
2. **Executing `build_exe.bat` / Modifying `dist/`:** Per audit rules (*"don't touch dist/"*), `build_exe.bat` was inspected statically rather than executed so `dist/` remained untouched.
3. **Live GitHub Actions Workflow Execution (`.github/workflows/build.yml`):** Remote CI triggers and tag-based GitHub Release uploads (`softprops/action-gh-release@v2`) cannot be executed from the local workspace and were audited statically.

---

## 6. Prioritized Fix List

1. **[P0 — Critical Correctness & Security] Fix `_sanitize_html()` Tag Scoping & Sanitizer Bypasses (`converter.py:273-303`, `538` — F-01, F-04):**
   - Scope `event_attr_pattern` and `unsafe_href_pattern` strictly to attributes inside HTML opening tags (`<[a-zA-Z][^>]*>`) outside `<pre>`/`<code>` blocks so code snippets and prose containing `online = True` or `on_error = "retry"` are never stripped.
   - Add `style` to `paired_tag_pattern`, match `[\s/]+on[a-z0-9_]+`, unescape HTML character references before checking `href`/`action` schemes, and handle unquoted `href=` attributes.
2. **[P0 — Critical Layout Correctness] Bound `_wrap_h3_list_blocks()` to Single `<h3>` Elements (`converter.py:312-314` — F-02):**
   - Replace `r"<h3[^>]*>.*?</h3>\s*<(ul|ol)\b[^>]*>"` with `r"<h3\b[^>]*>(?:(?!</h3\s*>).)*?</h3>\s*<(ul|ol)\b[^>]*>"` so non-list `<h3>` headings never swallow subsequent sections into `.station-block`.
3. **[P1 — High Security & Image Resolution] Validate `file://` Image URIs & Enforce Containment When `suppress_missing_images=False` (`converter.py:126-159`, `205-224`, `253-268` — F-03, F-08, F-10):**
   - Remove `"file://"` from `_is_remote_or_data_uri()` and parse `file://` URIs into local paths inside `_resolve_local_image()` so `allow_outside_base_dir`, UNC blocking, and `suppress_missing_images` apply uniformly.
   - Return a distinct `is_allowed` status from `_resolve_local_image()` so security-blocked paths (`../`, UNC) are always stripped even when `suppress_missing_images=False`.
   - Stop splitting unquoted image paths on `'` (`converter.py:210`) and support multi-backtick code spans in `code_pattern` (`converter.py:176`).
4. **[P1 — High Packaging & Test Suite] Add `psutil` to `pyproject.toml` `[dev]` & Align `requirements.txt` (`pyproject.toml:27-34`, `requirements.txt:4` — F-05):**
   - Add `psutil>=5.9,<8.0` and `types-psutil` to `[project.optional-dependencies] dev` in `pyproject.toml`, add upper version bounds, and keep `requirements.txt` aligned with runtime dependencies.
5. **[P2 — High/Medium GUI Reliability] Fix Stale Preview Race on `New Document` & Remove Nested `root.update()` Loop (`app.py:591-604`, `709-717`, `775-805`, `935-945` — F-06, F-07, F-09):**
   - Increment `self._render_generation` and cancel `self._debounce_after_id` in `new_document()` and the empty-text branch of `trigger_preview_render()`.
   - Replace the `while not done_event.wait(0.02): self.root.update()` loop in `export_pdf_dialog()` with `self.root.after(50, ...)` polling, a re-entrancy guard (`self._is_exporting`), and `self._render_lock`.
   - Escape or strip `[` and `]` in `alt_text` inside `insert_image_dialog()`.
6. **[P3 — Medium/Low Polish, Cross-Platform & CI] Process Tree Cleanup, CSS Clamping, Linux Fonts, and CI Permissions (`converter.py:19, 66, 363, 368, 624`, `app.py:900, 975`, `.github/workflows/build.yml:16` — F-11 to F-17):**
   - Kill the browser process tree on timeout, clamp numeric fields in `build_css()`, add `'Noto Color Emoji'` / `'Noto Sans'` to the CSS font stack, track matching code-fence delimiters in `_apply_syntax_highlighting()`, unlink preview PDFs in `finally:`, remove unused `import urllib.request`, and restrict `contents: write` in `.github/workflows/build.yml`.
