# QA & Security Code Review Report — Markdown2PDF Desktop Studio

**Target Repository:** `e:\Antigravity-Projects\mdtopdf`  
**Audit Environment:** Windows 10/11 (`win32`), Python `3.13.0`, Microsoft Edge (Headless Chromium), PyMuPDF `2.0`, Pillow `13.0.0`, Markdown `3.11.2`  
**Audit Mode:** First-pass read-only audit (no existing source files or `dist/` modified; test suite added under `tests/test_converter.py`).

---

## 1. Executive Summary

### Release Recommendation: **NO-GO (Conditional on fixing Critical & High findings)**

While the core happy-path workflow (editing Markdown, live A4 preview rendering via headless Edge/Chrome + PyMuPDF, and PDF export with Thai/math/emoji typography) works as advertised and passed end-to-end GUI smoke testing, the audit uncovered **2 Critical**, **6 High**, **9 Medium**, and **7 Low** issues that should be resolved before production distribution.

### Architecture Summary (Step 1 Orientation)
1. **Entry Points & Packaging:** `app.py` (`main()`) is the Tkinter desktop GUI entry point, launched via `run_app.bat` (`pythonw -X utf8 app.py`), `pyproject.toml` (`markdown2pdf = "app:main"`), or packaged into a single-file executable via PyInstaller (`build_exe.bat` locally and `.github/workflows/build.yml` on GitHub Actions for Windows and Ubuntu).
2. **GUI Layer (`app.py`):** `MdToPdfStudioApp` presents a split-pane workspace: a left-hand `tk.Text` UTF-8 editor with regex-based Markdown syntax highlighting, and a right-hand `tk.Canvas` displaying zoomed/paginated `ImageTk.PhotoImage` page cards.
3. **Background Preview Pipeline (`app.py`):** Editor changes (debounced at 1200 ms) and layout option changes (debounced at 300 ms) increment a generation counter (`_render_generation`) and spawn a daemon `threading.Thread` (`_background_render_worker`), pushing rendered PIL pages to a `queue.Queue` polled every 100 ms on the main thread (`_poll_render_queue`).
4. **Conversion Engine (`converter.py`):** `build_html()` pre-filters Markdown via `preprocess_markdown()` (optionally stripping missing local images), converts Markdown to HTML via `python-markdown` (`tables`, `fenced_code`), rewrites relative `<img src>` paths to `file:///` URIs (`_resolve_html_image_uris()`), wraps `<h3>` + list blocks in `.station-block` divs, and injects A4 print CSS (`build_css()`).
5. **PDF Rendering & Verification (`converter.py`):** `render_pdf()` writes the HTML to a temporary file (`tempfile.mkstemp`), invokes headless Edge/Chrome/Chromium (`find_browser_executable()`) with `--print-to-pdf`, then opens the resulting PDF with `pymupdf` to count pages, count embedded images, and rasterize pages into PIL images (`render_pdf_pages_to_pil()`).

### Top 5 Risks
1. **[CRITICAL — F-01] Unsanitized HTML/JS & `file:///` Local File Exfiltration (`converter.py:359-392`, `422-430`):** Raw HTML in Markdown is rendered from a local `file:///` origin in headless Chromium with a `<base href="file:///...">` tag. Malicious `.md` files can execute arbitrary `<script>` payloads and embed arbitrary local files (e.g., `<iframe src="file:///C:/Windows/win.ini">`) directly into the preview/exported PDF (**VERIFIED**).
2. **[CRITICAL — F-02] `UnicodeDecodeError` in `subprocess.run` on Non-UTF-8 Windows Codepages (`converter.py:433-439`):** `subprocess.run(..., capture_output=True, text=True)` omits `encoding="utf-8", errors="replace"`. When run without `-X utf8` (including the PyInstaller `.exe` and the `pip install .` `markdown2pdf` script), Chromium's UTF-8 `stderr` output crashes Python's background `_readerthread` with `UnicodeDecodeError: 'charmap' codec can't decode byte 0x82` on Windows locales such as `cp874` (Thai) or `cp1252` (**VERIFIED**).
3. **[HIGH — F-03] Unpinned `PyMuPDF>=1.24.0` Pulls `PyMuPDF 2.0` + `onnxruntime` + `numpy` (`requirements.txt:2`, `pyproject.toml:19`):** Installing from `requirements.txt` or `pyproject.toml` now resolves to `PyMuPDF 2.0`, which pulls in 10 heavy transitive packages (`onnxruntime`, `numpy`, `networkx`, `protobuf`, `pymupdf4llm`, `pymupdf-layout`, `psutil`, etc., >100 MB), bloating the environment and PyInstaller single-file executables (**VERIFIED**).
4. **[HIGH — F-04] Broken Image Resolution for Filenames with Literal Parentheses or `&` (`converter.py:142`, `181`):** Despite `README.md:17` claiming support for parentheses in image paths, `md_img_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")` truncates at the first `)` inside `![alt](images/figure (1).png)`, falsely stripping the image and leaving `.png)` in the PDF. Similarly, filenames with `&` (`![alt](images/a&b.png)`) become `a&amp;b.png` in HTML and fail resolution in `_resolve_html_image_uris()` due to missing `html.unescape()` (**VERIFIED**).
5. **[HIGH — F-05] Unbounded Concurrent Headless Browser Processes & Missing `--user-data-dir` (`app.py:75`, `883-888`; `converter.py:423-430`):** `app.py` defines `self._is_rendering = False` (`app.py:75`) but never uses it, spawning overlapping headless browser processes on rapid option changes, and `converter.py` runs Chromium without an isolated `--user-data-dir`, risking profile lock collisions and synchronous UI freezes up to 60s during `export_pdf_dialog()` (`app.py:754`).

---

## 2. Setup, Dependency & Static Analysis Summary (Steps 2 & 3)

### Step 2: Virtual Environment & Dependency Audit
- **Command Run:** `python -m venv .venv && .\.venv\Scripts\pip install -r requirements.txt`
- **Installed Versions (Python 3.13.0 on Windows):**
  - Direct: `markdown-3.11.2`, `PyMuPDF-2.0`, `Pillow-13.0.0`, `pyinstaller-6.22.3`
  - Transitive pulled by `PyMuPDF-2.0`: `onnxruntime-1.31.1`, `numpy-2.5.4`, `networkx-3.7`, `protobuf-7.36.2`, `flatbuffers-25.12.19`, `pymupdf-layout-2.0`, `pymupdf4llm-2.0`, `psutil-7.2.2`, `pyyaml-6.0.3`, `tabulate-0.10.0`
- **Mismatches & Issues Found (VERIFIED):**
  1. **Unpinned upper bounds (`requirements.txt:1-4`, `pyproject.toml:18-20`):** All dependencies use `>=` without upper bounds or lockfiles. `PyMuPDF>=1.24.0` jumped across a major version to `2.0`, pulling ML dependencies (`onnxruntime`, `numpy`) that `Markdown2PDF` never uses.
  2. **`requirements.txt` vs `pyproject.toml` mismatch (`requirements.txt:4` vs `pyproject.toml:17-21`):** `pyinstaller>=6.0.0` is listed as a core requirement in `requirements.txt` (forcing source users to install `pyinstaller`, `pefile`, `altgraph`, `pywin32-ctypes`, `setuptools`), but is absent from `pyproject.toml` (which has no `[project.optional-dependencies]`).
  3. **Cross-drive Python 3.13 Tcl/Tk discovery on Windows:** When `.venv` is created on `E:\` from a base Python 3.13.0 installed on `C:\Users\User\AppData\Local\Programs\Python\Python313`, `_tkinter` searches `C:/.../Python313/lib/tcl8.6` instead of `C:/.../Python313/tcl/tcl8.6` unless `TCL_LIBRARY` and `TK_LIBRARY` are set.

### Step 3: Static Checks (`ruff`, `mypy`, `bandit`)

| Tool | Command | Result Summary (Noise Filtered) |
| :--- | :--- | :--- |
| **Ruff** (`0.17.3`) | `.\.venv\Scripts\ruff check app.py converter.py --select E,F,W,B,SIM,S,UP,ARG,PTH,RUF,PERF,BLE` | **50 findings** (145 with `--select ALL`):<br>• `ARG002` (`app.py:895`): Unused method parameter `out_pdf_str` in `_background_render_worker`.<br>• `BLE001` (6× in `app.py:40,43,600,631,755,908`): Blind `except Exception:` blocks.<br>• `RUF046` (`app.py:1027`): Unnecessary `int()` cast on `round(self._zoom_level * 100)` (already returns `int`).<br>• `SIM105` / `S110` (`app.py:41,903`, `converter.py:446`): `try/except/pass` blocks instead of `contextlib.suppress`.<br>• `UP045` (8×): `Optional[X]` used despite `from __future__ import annotations`.<br>• `E501` (11×): Lines exceeding 88 chars. *(Ignored noise: `RUF001` multiplication/minus signs in UI labels, `D*` docstring formatting).* |
| **Mypy** (`2.4.1`) | `.\.venv\Scripts\mypy app.py converter.py --ignore-missing-imports --check-untyped-defs` | **5 errors in 2 files:**<br>• `converter.py:455, 478`: `"Document" has no attribute "__iter__"` (and resulting generator item type error) because `pymupdf.Document` lacks `__iter__` in its type stubs.<br>• `app.py:1062, 1068`: `Unsupported operand types for & ("str" and "int")` on `event.state & 0x0004` (`tk.Event.state` is typed as `int \| str`). |
| **Bandit** (`1.9.4`) | `.\.venv\Scripts\bandit -r app.py converter.py` | **7 Low-severity alerts (0 Medium/High):**<br>• `B603` (`converter.py:433`, `app.py:791`): `subprocess.run` / `subprocess.Popen` without `shell=True` (safe from shell injection, though `xdg-open` in `app.py:791` uses partial path `B607`).<br>• `B606` (`app.py:789`): `os.startfile(res.output_pdf_path)`.<br>• `B110` (`app.py:43`): `try/except/pass` in HiDPI init. |

---

## 3. Findings Table (Step 5 Code Review)

| ID | Severity | Status | File:Line | Issue | Suggested Fix (Short Diff) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **F-01** | **Critical** | **VERIFIED** | `converter.py:359-392`, `422-430` | **Arbitrary JS Execution & Local File (`file:///`) Exfiltration:** `markdown.markdown()` passes raw HTML (`<script>`, `<iframe>`, `<object>`, `onerror=`) directly into a temp `.html` file loaded via `file:///` in headless Chromium. Verified by test that `<script>` executes and `<iframe src="file:///...">` embeds arbitrary local files into the PDF. | Strip/sanitize dangerous tags (`<script>`, `<iframe>`, `<object>`, `<embed>`, `on*=`) or add a strict CSP meta tag in `build_html()` (`converter.py:382`):<br>```diff
 <head>
 <meta charset="utf-8">
+<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src file: https: http: data:; style-src 'unsafe-inline'; font-src 'self' data:;">
 <base href="{base_uri}">
``` |
| **F-02** | **Critical** | **VERIFIED** | `converter.py:433-439` | **`UnicodeDecodeError` in `subprocess.run` Reader Thread:** `subprocess.run(..., text=True)` omits `encoding="utf-8"`. When launched without `-X utf8` (e.g., PyInstaller `.exe` or `pyproject.toml` GUI script), Chromium's UTF-8 `stderr` output crashes `_readerthread` on Windows ANSI codepages (`cp874`, `cp1252`) when paths contain Thai/Unicode chars. | Explicitly set `encoding="utf-8"` and `errors="replace"` in `converter.py:433`:<br>```diff
         proc = subprocess.run(
             cmd,
             capture_output=True,
             text=True,
+            encoding="utf-8",
+            errors="replace",
             creationflags=creationflags,
             timeout=60,
         )
``` |
| **F-03** | **High** | **VERIFIED** | `requirements.txt:1-4`, `pyproject.toml:18-20` | **Unpinned Dependencies Pull `PyMuPDF 2.0` (`onnxruntime`, `numpy`) & Mismatched `pyinstaller`:** `PyMuPDF>=1.24.0` resolves to `2.0`, adding >100 MB of unused ML dependencies (`onnxruntime`, `numpy`, `networkx`). Also, `pyinstaller` is in `requirements.txt:4` but missing from `pyproject.toml`. | Cap `PyMuPDF>=1.24.0,<2.0.0` in both files and move `pyinstaller` to `[project.optional-dependencies]`:<br>```diff
-PyMuPDF>=1.24.0
+PyMuPDF>=1.24.0,<2.0.0
``` |
| **F-04** | **High** | **VERIFIED** | `converter.py:142`, `181` | **Broken Image Resolution for Parentheses & `&` in Filenames:** (1) `md_img_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")` stops at the first `)` in `![alt](images/figure (1).png)`, falsely stripping the image and leaving `.png)` as text. (2) `_resolve_html_image_uris` passes HTML-escaped `src` (`a&amp;b.png`) to `_resolve_local_image` without `html.unescape()`. | Support one level of balanced parentheses in `converter.py:142` and unescape HTML entities in `converter.py:181`:<br>```diff
-md_img_pattern = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
+md_img_pattern = re.compile(r"!\[([^\]]*)\]\(((?:[^()]+|\([^()]*\))+)\)")
...
-exists, resolved_path = _resolve_local_image(src, base_dir)
+exists, resolved_path = _resolve_local_image(html.unescape(src), base_dir)
``` |
| **F-05** | **High** | **VERIFIED** | `converter.py:116-129` | **Path Traversal & Windows UNC/SMB Hash Leak via Image Paths:** `_resolve_local_image()` allows arbitrary `../` traversal outside `base_dir`, absolute paths, and Windows UNC paths (`//attacker.com/share/a.png`). On Windows, calling `.resolve()` / `.is_file()` on a `//host/share` path initiates outbound SMB/NTLMv2 authentication, leaking credentials and hanging the thread. | Reject UNC/network drives and optionally restrict relative paths or block `\\` prefixes in `converter.py:121`:<br>```diff
 unquoted = urllib.parse.unquote(src.strip())
+if unquoted.startswith(("\\\\", "//")):
+    return False, None
 candidate = Path(unquoted)
``` |
| **F-06** | **High** | **VERIFIED** | `converter.py:367-374` | **Malformed HTML when `keep_blocks_together` Wraps Nested Lists:** The non-greedy regex `r"(<h3[^>]*>.*?</h3>\s*<(?:ul|ol)>.*?</(?:ul|ol)>)"` closes `<div class="station-block">` at the **first inner** `</ul>` of a nested list, producing invalid HTML (`</ul></div></li><li>...</ul>`). | Avoid non-greedy regex across potentially nested `<ul>/<ol>` blocks, or rely on CSS `h3, h3 + ul, h3 + ol { break-inside: avoid; }`:<br>```diff
-block_pattern = r"(<h3[^>]*>.*?</h3>\s*<(?:ul|ol)>.*?</(?:ul|ol)>)"
+block_pattern = r"(<h3[^>]*>.*?</h3>\s*<(?:ul|ol)>(?:(?!</?(?:ul|ol)\b).)*?</(?:ul|ol)>)"
``` |
| **F-07** | **High** | **VERIFIED** | `app.py:724-765` | **Main UI Thread Blocks Synchronously During PDF Export:** `export_pdf_dialog()` calls `render_pdf()` directly on the Tkinter UI thread (`app.py:754`). Because headless Chromium can take several seconds (or up to the 60s timeout), the GUI freezes ("Not Responding"). | Run `render_pdf()` in a background `threading.Thread` (like preview rendering) and disable `btn_export` until completion:<br>```diff
-res = render_pdf(md_text, str(self.base_dir), out_path, options)
+# Dispatch export to a worker thread and poll via self.root.after()
``` |
| **F-08** | **High** | **SUSPECTED** | `app.py:75`, `883-907`; `converter.py:423-430` | **Unbounded Concurrent Browser Processes & Default Profile Collision:** `self._is_rendering` (`app.py:75`) is never used. Rapid option changes spawn multiple parallel headless browser processes. Moreover, `converter.py:423` omits `--user-data-dir`, so concurrent headless instances (or an already-open desktop browser) can collide on the default profile lock. | Pass a temporary `--user-data-dir` per invocation or serialize renders with a `threading.Lock` in `_background_render_worker`:<br>```diff
+with tempfile.TemporaryDirectory(prefix="mdtopdf_prof_") as prof_dir:
     cmd = [
         browser_exe,
         "--headless",
+        f"--user-data-dir={prof_dir}",
``` |
| **F-09** | **Medium** | **VERIFIED** | `app.py:599`, `838`; `converter.py:357` | **UTF-8 BOM (`\ufeff`) Breaks First-Line Heading & Syntax Highlighting:** `app.py:599` reads files with `encoding="utf-8"` instead of `"utf-8-sig"`, and `converter.py` does not strip leading `\ufeff`. A BOM before `# Heading` causes Python-Markdown to render `<p>\ufeff# Heading</p>` instead of `<h1>` (**VERIFIED**). | Use `encoding="utf-8-sig"` in `app.py:599` and `lstrip("\ufeff")` in `converter.py:135`:<br>```diff
-text = resolved.read_text(encoding="utf-8")
+text = resolved.read_text(encoding="utf-8-sig")
``` |
| **F-10** | **Medium** | **VERIFIED** | `converter.py:383-384`, `198-203` | **HTML/CSS Injection via Unescaped `options.title` and CSS Options:** `options.title` (set from `self.current_file.stem` at `app.py:712`) is interpolated into `<title>{options.title}</title>` without `html.escape()`. Filenames containing `<` or `&` inject raw HTML into `<head>`. | Escape `options.title` and `base_uri` with `html.escape()` in `converter.py:383-384`:<br>```diff
-<base href="{base_uri}">
-<title>{options.title}</title>
+<base href="{html.escape(base_uri, quote=True)}">
+<title>{html.escape(options.title)}</title>
``` |
| **F-11** | **Medium** | **VERIFIED** | `converter.py:142-157` | **`preprocess_markdown()` Strips Image Syntax Inside Code Blocks:** `preprocess_markdown()` runs regex replacement on raw Markdown text before `markdown.markdown()` parses fenced (` ``` `) or inline (`` ` ``) code blocks, corrupting code examples that contain `![alt](missing.png)`. | Move missing-image suppression into `_resolve_html_image_uris()` (which only runs on actual HTML `<img>` tags outside `<code>`/`<pre>`) or skip fenced code blocks. |
| **F-12** | **Medium** | **SUSPECTED** | `.github/workflows/build.yml:11-12`, `15-66`, `23` | **CI Security & Packaging Flaws:** (1) Top-level `permissions: contents: write` (`build.yml:11-12`) grants write access even on `pull_request` events. (2) No `pytest` or linter step runs before building/releasing binaries. (3) `output_name: Markdown2PDF-windows.exe` (`build.yml:23`) differs from `build_exe.bat:4` (`Markdown2PDF.exe`). | Restrict `permissions: contents: read` at workflow level and grant `contents: write` only on release; add a test step before PyInstaller. |
| **F-13** | **Medium** | **SUSPECTED** | `app.py:512`, `964-1020` | **UI Lag on Window Resize from Synchronous Lanczos Resizing:** `<Configure>` on `preview_canvas` triggers `_redraw_preview_canvas()` on every pixel of window drag, synchronously running `pil_img.resize(..., Image.Resampling.LANCZOS)` for every page when `_zoom_level != 1.0`. | Debounce `<Configure>` redraws (e.g. `self.root.after(80, ...)`) and cache resized `ImageTk.PhotoImage` objects keyed by `(page_idx, scaled_w, scaled_h)`. |
| **F-14** | **Medium** | **SUSPECTED** | `app.py:1077-1082`; `converter.py:414` | **Temp Directory Cleanup Race on Window Close & Crash Leak:** If `_on_close()` runs `shutil.rmtree(self._temp_dir)` while a daemon `_background_render_worker` thread is starting `render_pdf()`, `out_path.parent.mkdir(parents=True, exist_ok=True)` (`converter.py:414`) recreates `self._temp_dir` after `_on_close()`, leaking the temp folder. | Register `atexit.register(shutil.rmtree, self._temp_dir, True)` and check a `self._closing` flag before/after rendering in `_background_render_worker`. |
| **F-15** | **Medium** | **SUSPECTED** | `converter.py:433-449` | **Orphaned Browser Child Processes & Leaked Temp HTML on Timeout:** On timeout (`60s`), `subprocess.run` only kills the root browser process on Windows, potentially leaving child renderer processes holding `temp_html_path` open so `os.remove(temp_html_path)` silently fails in `finally:`. | Use `subprocess.Popen` and on `TimeoutExpired` terminate the process tree (e.g. `taskkill /F /T /PID` on Windows or `os.killpg` on Linux) before deleting `temp_html_path`. |
| **F-16** | **Medium** | **VERIFIED** | `converter.py:455`, `478`; `app.py:1062`, `1068` | **Type-Safety Errors Flagged by `mypy`:** (1) `for page in doc:` in `converter.py:455, 478` fails `mypy` because `pymupdf.Document` lacks `__iter__` in its stub. (2) `event.state & 0x0004` in `app.py:1062, 1068` fails `mypy` because `event.state` is typed `int \| str`. | Iterate via `for i in range(len(doc)): page = doc[i]` in `converter.py`, and guard `isinstance(event.state, int) and (event.state & 0x0004)` in `app.py`. |
| **F-17** | **Medium** | **SUSPECTED** | `app.py:127-149`, `386`; `converter.py:60-90` | **Cross-Platform Font & Browser Discovery Gaps on Linux/Windows:** (1) `app.py` hardcodes Windows fonts (`Leelawadee UI`, `Segoe UI`) which do not exist on Linux. (2) `find_browser_executable()` (`converter.py:87`) checks `win_candidates + linux_candidates` on all OSes without checking `sys.platform`. (3) `build.yml:40-44` & `README.md` omit Linux Thai/emoji font packages (`fonts-noto-core`, `fonts-noto-color-emoji`). | Gate browser candidate lists by `sys.platform`, select platform-appropriate GUI font families in `app.py`, and document Linux font packages in `README.md`. |
| **F-18** | **Low** | **VERIFIED** | `app.py:63`, `75`, `878`, `885`, `895` | **Dead Code (`_is_rendering`, `_preview_pdf_path`, `out_pdf_str`):** `self._preview_pdf_path` (`app.py:63`), `self._is_rendering` (`app.py:75`), and parameter `out_pdf_str` in `_background_render_worker` (`app.py:895`) are dead/unused code. | Remove `self._preview_pdf_path`, `out_pdf_str`, and either use or remove `self._is_rendering`. |
| **F-19** | **Low** | **SUSPECTED** | `app.py:694-709`, `281-335` | **Unclamped Spinbox Inputs & Missing `<FocusOut>` Binding:** Typing an extreme value (e.g., `-10`, `0`, `9999`) into the Font Size or Image Max W/H spinboxes bypasses the Spinbox range (`7.5–14.0`, `30–400`) because `_build_conversion_options()` does not clamp `font_pt` or upper bounds, and `<FocusOut>` is not bound. | Clamp `font_pt = min(14.0, max(7.5, float(...)))` and `img_w = min(400, max(30, int(...)))` in `_build_conversion_options()`, and bind `<FocusOut>` on spinboxes. |
| **F-20** | **Low** | **SUSPECTED** | `app.py:837-860` | **Editor Line Desync & Code Block False Positives in Syntax Highlighting:** `_apply_syntax_highlighting()` iterates over `text.splitlines()` (`app.py:837`), which splits on `\r`, `\v`, `\f`, `\x85`, `\u2028` whereas `tk.Text` line indices only split on `\n`. It also highlights `# comments` inside fenced code blocks as `md_h1`. | Use `text.split("\n")` instead of `text.splitlines()` in `app.py:837` and track ` ``` ` fence state to skip heading tags inside code blocks. |
| **F-21** | **Low** | **SUSPECTED** | `app.py:1065` | **High-Precision Touchpad Scrolling Truncated to Zero:** `int(-1 * (event.delta / 120))` in `_on_preview_mousewheel` evaluates to `0` on Windows precision touchpads that emit `abs(event.delta) < 120` (e.g., `30` or `60`), making touchpad two-finger scrolling unresponsive. | Ensure a minimum step of `±1` when `event.delta != 0`:<br>```diff
-self.preview_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
+step = -1 if event.delta > 0 else 1
+self.preview_canvas.yview_scroll(int(-event.delta / 120) or step, "units")
``` |
| **F-22** | **Low** | **SUSPECTED** | `app.py:538` | **Caps Lock `Ctrl+S` Triggers `Save As` Instead of `Save`:** Binding `<Control-S>` (`app.py:538`) matches `Ctrl+s` when Caps Lock is active on Windows/Linux. | Bind `<Control-Shift-S>` and `<Control-Shift-s>` explicitly instead of relying on `<Control-S>` alone. |
| **F-23** | **Low** | **SUSPECTED** | `run_app.bat:3` | **`run_app.bat` Suppresses Startup Errors via `pythonw`:** `start "" pythonw -X utf8 app.py %*` uses global `pythonw`, which silently exits with no console or dialog if dependencies (`markdown`, `pymupdf`, `PIL`) are missing or only installed in `.venv`. | Check for `.venv\Scripts\pythonw.exe` first in `run_app.bat` and fall back to `python` on failure. |
| **F-24** | **Low** | **VERIFIED** | `pyproject.toml:11`, `27` | **Deprecated PEP 621 License Table & Missing UTF-8 Mode in Entry Point:** `license = {text = "MIT"}` triggers setuptools deprecation warnings under PEP 639, and `[project.gui-scripts] markdown2pdf = "app:main"` launches without `-X utf8`. | Use `license = "MIT"` in `pyproject.toml:11` and ensure all file/subprocess I/O explicitly specifies `encoding="utf-8"`. |

---

## 4. Test Results & Smoke Test Verification (Steps 4 & 6)

### Automated Test Suite (`tests/test_converter.py`)
- **Command Run:**
  ```powershell
  .\.venv\Scripts\pytest tests/test_converter.py -v --cov=converter --cov-report=term-missing
  ```
- **Results:** **39 passed, 0 failed, 1 warning** in `19.39s`
- **Line Coverage (`converter.py`):** **99%** (`146` statements, `2` missed — lines `448-449`, the `except OSError: pass` fallback when deleting the temp HTML file fails).
- **Warning Captured During Real Browser Test:**
  `PytestUnhandledThreadExceptionWarning: Exception in thread Thread-8 (_readerthread) -> UnicodeDecodeError: 'charmap' codec can't decode byte 0x82 in position 793` (directly reproducing and verifying **F-02**).
- **Test Cases Covered in `tests/test_converter.py`:**
  1. `test_basic_markdown_html_generation`: Headings (`h1`–`h4`), unordered/ordered lists, inline/fenced code blocks, aligned tables, blockquotes, horizontal rules.
  2. `test_local_image_relative_and_missing_stripped`: Valid local PNG resolved to `file:///` URI; missing local image stripped with `suppress_missing_images=True`.
  3. `test_local_image_missing_kept_when_suppress_disabled`: Missing image retained when `suppress_missing_images=False`.
  4. `test_local_image_url_encoded_spaces_and_unicode`: Local image with URL-encoded spaces, parentheses (`%281%29`), Thai characters, and `µ`.
  5. `test_local_image_literal_parentheses_in_markdown_bug`: Reproduces & verifies **F-04** (`![Figure](images/figure (1).png)` truncation bug).
  6. `test_local_image_ampersand_in_filename_html_entity_bug`: Reproduces & verifies **F-04** (`images/a&b.png` -> `a&amp;b.png` lookup failure).
  7. `test_local_image_with_optional_markdown_title`: `![Sample](images/sample.png "Optional Title")`.
  8. `test_remote_and_data_uri_images_untouched`: `http://`, `https://`, `data:`, and `file://` URIs preserved.
  9. `test_resolve_local_image_oserror_returns_false`: `OSError` during path resolution handled gracefully.
  10. `test_thai_emoji_and_math_symbols_in_html`: Thai script, `🔔`, `⚠️`, `Ω`, `µ`, `±`, `×`, `➔`.
  11. `test_inline_html_and_malicious_html_passthrough`: Inline HTML, `<script>`, `onerror=`, `file://` links, and `../` path traversal (**F-01**, **F-05**).
  12. `test_title_html_injection_in_build_html`: Unescaped `<title>` injection (**F-10**).
  13. `test_empty_and_whitespace_input`: Empty string `""` and whitespace-only input.
  14. `test_huge_input_performance_and_correctness`: 9,000-line (~160 KB) Markdown document with 1,000 headings and 1,000 tables.
  15. `test_utf8_bom_breaks_first_heading`: Reproduces & verifies **F-09** (`\ufeff# Title With BOM`).
  16. `test_invalid_utf8_file_handling_and_surrogates`: Invalid UTF-8 bytes (`\xff\xfe`) and lone surrogate (`\ud800`) handling.
  17. `test_font_size_and_margin_presets` (12 parametrized cases): Min (`7.5pt`), default (`9.5pt`), and max (`14.0pt`) font sizes across all 4 margin presets (`Compact`, `Standard`, `Comfortable`, `Wide`), plus min/max image dimensions (`30px`, `400px`).
  18. `test_keep_blocks_together_enabled_vs_disabled` & `test_keep_blocks_together_nested_list_malformed_html_bug`: `.station-block` wrapping and nested-list HTML corruption (**F-06**).
  19. `test_no_browser_installed_raises_clear_file_not_found` & `test_find_browser_executable_shutil_which_fallback`: Missing browser raises clear `FileNotFoundError`; `shutil.which` fallback works.
  20. `test_output_path_IsADirectory_or_browser_failure`, `test_output_path_locked_or_unwritable_directory`, `test_output_path_read_only_or_locked_file`: Output path errors and read-only target handling.
  21. `test_temp_html_cleaned_up_on_browser_exception_and_timeout`: Temp `.html` file removed even on `subprocess.TimeoutExpired`.
  22. `test_real_browser_no_leftover_processes_and_temp_cleanup`: Real headless Edge conversion verifying zero leaked `mdtopdf_*.html` files and zero lingering browser child processes via `psutil`.
  23. `test_real_browser_script_execution_and_local_file_iframe`: Real headless Edge conversion verifying `<script>` execution and `<iframe src="file:///...">` local file exfiltration into the PDF (**F-01**).
  24. `test_real_browser_end_to_end_conversion_and_preview`: Real headless Edge conversion with non-ASCII/space output path (`โฟลเดอร์ ทดสอบ (Output)/เอกสาร ทดสอบ µ.pdf`), verifying A4 dimensions (`595 x 842 pt`), Thai/math/code text extraction, embedded image count, and `render_pdf_pages_to_pil()`.

### Step 6: Desktop GUI Smoke Test Results (VERIFIED)
- **Command Run:**
  ```powershell
  $env:TCL_LIBRARY="C:\Users\User\AppData\Local\Programs\Python\Python313\tcl\tcl8.6"
  $env:TK_LIBRARY="C:\Users\User\AppData\Local\Programs\Python\Python313\tcl\tk8.6"
  .\.venv\Scripts\python -X utf8 C:\Users\User\.gemini\antigravity\brain\322548b4-a18f-4c89-a6f3-1a92ed1a6f30\scratch\smoke_test_gui.py
  ```
- **Outcome:** **PASSED**
  - Launched `MdToPdfStudioApp` on the Windows display with a sample `.md` containing headings, a table, an existing local image (`diagram (1) µ.png`), a missing image (`missing_ghost.png`), Thai text (`เอกสารทดสอบ Smoke Test`), math symbols (`100 Ω ± 5%`, `25 µA`, `10 × 2 ➔ 20`), and emojis (`🔔 ⚠️`).
  - Live preview rendered in background thread (`1 page | 1 image`, `len(_raw_pil_pages) == 1`, `len(_tk_page_images) == 1`).
  - Verified GUI zoom (`adjust_zoom(0.15)` -> `115%`, `fit_preview_to_width()`, `set_zoom(1.0)` -> `100%`) and page navigation (`scroll_to_page_delta`).
  - Exported PDF via `export_pdf_dialog()` and verified with PyMuPDF:
    - **Page Count:** `1`
    - **Page Size:** `595.0 × 841.9 pt` (exact A4 portrait)
    - **Extractable Text:** Confirmed Thai headings, `100 Ω ± 5%`, and `25 µA` present in PDF text layer
    - **Embedded Images:** `1` (valid image embedded; missing image cleanly suppressed)
  - Verified `_on_close()` deleted the temporary `mdtopdf_studio_*` directory cleanly.

---

## 5. Things That Could Not Be Tested, and Why

1. **Native Linux (X11/Wayland) Execution & Linux Font Rendering:** The audit host is Windows (`win32`). Linux-specific paths in `find_browser_executable()` (`converter.py:77-85`), `xdg-open` PDF launch (`app.py:790-791`), Linux `<Button-4>`/`<Button-5>` mousewheel scrolling (`app.py:514-519`, `1067-1071`), and Linux Ubuntu PyInstaller packaging in `.github/workflows/build.yml` could only be inspected statically (`SUSPECTED`).
2. **PyInstaller Standalone Executable Build (`build_exe.bat` / `dist/`):** Per audit rules (*"don't touch dist/"*), `build_exe.bat` was not executed because PyInstaller writes directly to `dist\Markdown2PDF.exe`.
3. **GitHub Actions CI Workflow Execution (`.github/workflows/build.yml`):** Cannot trigger remote GitHub Actions workflows or tag releases locally; reviewed statically.

---

## 6. Prioritized Fix List

1. **[P0 — Critical Security] Lock down HTML/JS & Local File Access (`converter.py:359-392`, `116-129`):**
   - Inject a strict `<meta http-equiv="Content-Security-Policy" ...>` header blocking all scripts (`script-src 'none'`) and frames (`frame-src 'none'; object-src 'none'`) in `build_html()`.
   - Strip dangerous raw HTML tags/attributes (`<script>`, `<iframe>`, `<object>`, `<embed>`, `on*=`) or sanitize HTML output.
   - Block Windows UNC/network paths (`\\` or `//`) in `_resolve_local_image()` and escape `options.title` with `html.escape()`.
2. **[P0 — Critical Reliability] Fix `subprocess.run` UTF-8 Decoding (`converter.py:433-439`):**
   - Add `encoding="utf-8", errors="replace"` to `subprocess.run()` so non-UTF-8 Windows codepages (`cp874`, `cp1252`) never crash `_readerthread`.
3. **[P1 — High Packaging/Deps] Pin `PyMuPDF<2.0.0` & Align `pyproject.toml` / `requirements.txt` (`requirements.txt:1-4`, `pyproject.toml:18-20`):**
   - Pin `PyMuPDF>=1.24.0,<2.0.0` to avoid pulling `onnxruntime` and `numpy`, and move `pyinstaller>=6.0.0` out of runtime dependencies into `[project.optional-dependencies]`.
4. **[P1 — High Correctness] Fix Image Resolution for Parentheses, `&`, and Code Blocks (`converter.py:132-193`) & Strip UTF-8 BOM (`app.py:599`, `converter.py:135`):**
   - Update `md_img_pattern` to support balanced parentheses `((?:[^()]+|\([^()]*\))+)`, call `html.unescape(src)` in `_resolve_html_image_uris()`, avoid stripping image syntax inside fenced/inline code blocks, and read files with `encoding="utf-8-sig"`.
5. **[P1 — High Correctness] Fix `keep_blocks_together` Nested List HTML Corruption (`converter.py:367-374`):**
   - Fix the regex or use CSS break rules so nested `<ul>/<ol>` lists inside `<h3>` sections do not produce misnested `</div>` tags.
6. **[P2 — High GUI/Concurrency] Move PDF Export Off the UI Thread & Isolate Browser Profiles (`app.py:724-765`, `883-907`; `converter.py:423-430`):**
   - Run `export_pdf_dialog()` rendering on a background worker thread, serialize or limit concurrent background preview renders, and pass a temporary `--user-data-dir` to headless Chromium.
7. **[P3 — Medium/Low Polish & CI] Fix `mypy` Errors, Resize Lag, CI Permissions, and Spinbox Clamping (`converter.py:455,478`, `app.py:512,694,837,1062`, `.github/workflows/build.yml`):**
   - Replace `for page in doc:` with indexed iteration (`doc[i]`), guard `event.state` bitmasking, debounce canvas `<Configure>` resizes, clamp spinbox values, use `text.split("\n")` in syntax highlighting, and tighten `.github/workflows/build.yml` permissions + add a `pytest` CI step.
