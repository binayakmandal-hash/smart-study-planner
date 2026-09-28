"""
Smart Study Planner - syllabus reader.

Pipeline
--------
PDF / image
  -> per page: digital words (PyMuPDF) or OCR boxes (RapidOCR > EasyOCR > Tesseract)
  -> table-grid detection (ruling lines found in the page image)
  -> rows:  [module cell | topic cell | hours cell]
  -> state machine:  Subject -> Module (with syllabus hours) -> Topic (+ subtopics)

Why this is faster than before
------------------------------
* Every page is read (no silent 6-page cap) but OCR only runs on pages that have
  no digital text.
* RapidOCR (ONNX) is used when installed - it is several times faster than
  EasyOCR on CPU.  EasyOCR / Tesseract remain as fall-backs.
* OCR results are cached per file (SHA-256), so re-uploading the same syllabus
  is instant.
* The Flask app runs OCR in a background thread and shows a live progress bar.
"""

import hashlib
import json
import os
import re
import threading

from PIL import Image

try:
    import numpy as np
except Exception:  # pragma: no cover - numpy is a hard dependency of the app
    np = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OCR_CACHE_DIR = os.path.join(BASE_DIR, "ocr_cache")
CACHE_VERSION = "v3"
# read-only cache shipped with the repo (makes the sample syllabus instant online)
SAMPLE_CACHE_DIR = os.path.join(BASE_DIR, "sample", "ocr_cache")

# Rendering resolution used for OCR and for table-line detection.
OCR_DPI = int(os.environ.get("STUDYPLANNER_OCR_DPI", "150"))
MAX_PDF_PAGES = 80          # safety net for giant handbooks (not a silent cut:
                            # the result reports how many pages were read)
MIN_DIGITAL_CHARS = 30

_engine_lock = threading.Lock()
_ocr_engine = None          # (name, engine_object) once initialised
_ocr_engine_failed = set()


# =============================================================================
# 1. OCR ENGINES
# =============================================================================

def _init_rapidocr():
    try:
        from rapidocr_onnxruntime import RapidOCR  # rapidocr-onnxruntime 1.x
        return ("rapidocr", RapidOCR())
    except Exception:
        pass
    try:
        from rapidocr import RapidOCR  # rapidocr 2.x / 3.x
        return ("rapidocr2", RapidOCR())
    except Exception:
        return None


def _init_easyocr():
    try:
        import torch
        import easyocr
        torch.set_num_threads(os.cpu_count() or 4)
        return ("easyocr", easyocr.Reader(["en"], gpu=False, verbose=False))
    except Exception:
        return None


def _init_tesseract():
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return ("tesseract", pytesseract)
    except Exception:
        return None


def get_ocr_engine():
    """Returns (name, engine) for the fastest OCR engine available, or None."""
    global _ocr_engine
    with _engine_lock:
        if _ocr_engine is not None:
            return _ocr_engine
        forced = os.environ.get("STUDYPLANNER_OCR", "").lower().strip()
        order = [
            ("rapidocr", _init_rapidocr),
            ("easyocr", _init_easyocr),
            ("tesseract", _init_tesseract),
        ]
        if forced:
            order.sort(key=lambda item: 0 if item[0] == forced else 1)
        for name, init in order:
            if name in _ocr_engine_failed:
                continue
            engine = init()
            if engine:
                _ocr_engine = engine
                print(f"[Syllabus OCR] using engine: {engine[0]}")
                return _ocr_engine
            _ocr_engine_failed.add(name)
        return None


# Backwards-compatible name used by older code.
def get_ocr_reader():
    engine = get_ocr_engine()
    return engine[1] if engine else False


def _box_to_rect(box):
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def ocr_image_tokens(image):
    """
    Runs OCR on a PIL image and returns a list of tokens:
    {"text", "x0", "y0", "x1", "y1", "conf"} in image pixel coordinates.
    """
    engine = get_ocr_engine()
    if not engine:
        return []
    name, ocr = engine
    gray = image.convert("L")
    tokens = []

    try:
        if name == "rapidocr":
            result, _ = ocr(np.array(gray.convert("RGB")))
            for item in result or []:
                box, text, score = item[0], item[1], item[2]
                x0, y0, x1, y1 = _box_to_rect(box)
                tokens.append({"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                               "conf": float(score)})

        elif name == "rapidocr2":
            out = ocr(np.array(gray.convert("RGB")))
            boxes = getattr(out, "boxes", None)
            txts = getattr(out, "txts", None)
            scores = getattr(out, "scores", None)
            if boxes is not None and txts is not None:
                for i, text in enumerate(txts):
                    x0, y0, x1, y1 = _box_to_rect(boxes[i])
                    conf = float(scores[i]) if scores is not None else 1.0
                    tokens.append({"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                                   "conf": conf})

        elif name == "easyocr":
            result = ocr.readtext(np.array(gray), detail=1, paragraph=False,
                                  canvas_size=max(gray.size), mag_ratio=1.0)
            for box, text, conf in result:
                x0, y0, x1, y1 = _box_to_rect(box)
                tokens.append({"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                               "conf": float(conf)})

        elif name == "tesseract":
            data = ocr.image_to_data(gray, output_type=ocr.Output.DICT, config="--psm 3")
            for i, text in enumerate(data["text"]):
                if not text or not text.strip():
                    continue
                try:
                    conf = float(data["conf"][i])
                except (TypeError, ValueError):
                    conf = 0.0
                if conf < 0:
                    continue
                x, y = data["left"][i], data["top"][i]
                w, h = data["width"][i], data["height"][i]
                tokens.append({"text": text, "x0": x, "y0": y, "x1": x + w, "y1": y + h,
                               "conf": conf / 100.0})
    except Exception as err:
        print(f"[Syllabus OCR] {name} error:", err)

    return [t for t in tokens if t["text"] and t["text"].strip()]


def extract_text_from_image(image_path_or_image):
    """Plain-text OCR of one image (kept for backwards compatibility)."""
    if isinstance(image_path_or_image, str):
        image = Image.open(image_path_or_image)
    else:
        image = image_path_or_image
    page = {"width": image.width, "height": image.height,
            "tokens": ocr_image_tokens(image), "hlines": [], "vsegs": []}
    return "\n".join(item["text"] for item in page_items(page))


# =============================================================================
# 2. TABLE RULING-LINE DETECTION
# =============================================================================

def _runs(mask_1d):
    """Returns (start, end) pairs of consecutive True values in a 1-D bool array."""
    padded = np.concatenate([[False], mask_1d, [False]])
    diff = np.diff(padded.astype(np.int8))
    starts = np.nonzero(diff == 1)[0]
    ends = np.nonzero(diff == -1)[0]
    return list(zip(starts.tolist(), ends.tolist()))


def detect_table_lines(image):
    """
    Finds long horizontal and vertical ruling lines in a page image.
    Returns (hlines, vsegs):
        hlines: [(y, x0, x1)]   vsegs: [(x, y0, y1)]
    """
    if np is None:
        return [], []
    gray = np.asarray(image.convert("L"))
    height, width = gray.shape
    dark = gray < 150

    # ---- horizontal lines: long dark runs in a row --------------------------
    min_h_len = int(width * 0.25)
    raw_h = []
    row_counts = dark.sum(axis=1)
    for y in np.nonzero(row_counts > min_h_len)[0].tolist():
        for x0, x1 in _runs(dark[y]):
            if x1 - x0 >= min_h_len:
                raw_h.append((y, x0, x1))
    hlines = []
    for y, x0, x1 in sorted(raw_h):
        if hlines and y - hlines[-1][0] <= 3 and not (x1 < hlines[-1][1] or x0 > hlines[-1][2]):
            py, px0, px1 = hlines[-1]
            hlines[-1] = (py, min(px0, x0), max(px1, x1))
        else:
            hlines.append((y, x0, x1))

    # ---- vertical lines: long dark runs in a column --------------------------
    min_v_len = max(20, int(height * 0.02))
    col_counts = dark.sum(axis=0)
    raw_v = []
    for x in np.nonzero(col_counts > min_v_len)[0].tolist():
        col = dark[:, x]
        # bridge tiny gaps (scan noise) before measuring runs
        for y0, y1 in _runs(col):
            if y1 - y0 >= min_v_len:
                raw_v.append((x, y0, y1))
    raw_v.sort()
    vsegs = []
    for x, y0, y1 in raw_v:
        merged = False
        for i, (vx, vy0, vy1) in enumerate(vsegs):
            if abs(vx - x) <= 4 and not (y1 < vy0 - 6 or y0 > vy1 + 6):
                vsegs[i] = (vx, min(vy0, y0), max(vy1, y1))
                merged = True
                break
        if not merged:
            vsegs.append((x, y0, y1))

    return hlines, vsegs


# =============================================================================
# 3. PAGE EXTRACTION (+ cache)
# =============================================================================

def _file_sha256(file_path):
    digest = hashlib.sha256()
    with open(file_path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _cache_path(file_hash):
    return os.path.join(OCR_CACHE_DIR, f"{file_hash}_{CACHE_VERSION}.json")


def load_cached_pages(file_path):
    try:
        file_hash = _file_sha256(file_path)
        for path in (_cache_path(file_hash),
                     os.path.join(SAMPLE_CACHE_DIR, f"{file_hash}_{CACHE_VERSION}.json")):
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
    except Exception as err:
        print("[Syllabus OCR] cache read warning:", err)
    return None


def _save_cached_pages(file_path, pages):
    try:
        os.makedirs(OCR_CACHE_DIR, exist_ok=True)
        with open(_cache_path(_file_sha256(file_path)), "w", encoding="utf-8") as handle:
            json.dump(pages, handle)
    except Exception as err:
        print("[Syllabus OCR] cache write warning:", err)


def pdf_needs_ocr(file_path):
    """True when at least one page has no usable digital text (scanned PDF)."""
    try:
        import pymupdf
        with pymupdf.open(file_path) as doc:
            for page in doc:
                if len((page.get_text("text") or "").strip()) < MIN_DIGITAL_CHARS and page.get_images(full=False):
                    return True
    except Exception:
        return True
    return False


def needs_background_processing(file_path, file_type):
    """Scanned files that are not cached yet should be processed in the background."""
    if load_cached_pages(file_path) is not None:
        return False
    file_type = (file_type or "").lower()
    if file_type in ("png", "jpg", "jpeg"):
        return True
    if file_type == "pdf":
        return pdf_needs_ocr(file_path)
    return False


def _page_from_image(image, digital_words=None, scale=1.0):
    hlines, vsegs = detect_table_lines(image)
    if digital_words is not None:
        tokens = [
            {"text": w[4], "x0": w[0] * scale, "y0": w[1] * scale,
             "x1": w[2] * scale, "y1": w[3] * scale, "conf": 1.0}
            for w in digital_words if str(w[4]).strip()
        ]
        source = "digital"
    else:
        tokens = ocr_image_tokens(image)
        source = "ocr"
    return {
        "width": image.width,
        "height": image.height,
        "tokens": tokens,
        "hlines": hlines,
        "vsegs": vsegs,
        "source": source,
    }


def extract_pages(file_path, file_type="pdf", progress_callback=None, use_cache=True):
    """
    Reads every page and returns a list of page dicts (tokens + ruling lines).
    progress_callback(done_pages, total_pages, message) is called after each page.
    """
    file_type = (file_type or "pdf").lower()

    if use_cache:
        cached = load_cached_pages(file_path)
        if cached is not None:
            if progress_callback:
                progress_callback(len(cached), len(cached), "Loaded from cache")
            return cached

    pages = []

    if file_type in ("png", "jpg", "jpeg"):
        image = Image.open(file_path)
        image.load()
        if progress_callback:
            progress_callback(0, 1, "Reading image")
        pages.append(_page_from_image(image))
        if progress_callback:
            progress_callback(1, 1, "Image read")

    else:
        import pymupdf
        with pymupdf.open(file_path) as doc:
            total = min(len(doc), MAX_PDF_PAGES)
            for index in range(total):
                page = doc[index]
                text = (page.get_text("text") or "").strip()
                scale = OCR_DPI / 72.0
                pix = page.get_pixmap(dpi=OCR_DPI, alpha=False, colorspace=pymupdf.csGRAY)
                image = Image.frombytes("L", [pix.width, pix.height], pix.samples)

                if len(text) >= MIN_DIGITAL_CHARS:
                    words = page.get_text("words")
                    pages.append(_page_from_image(image, digital_words=words, scale=scale))
                    message = f"Page {index + 1}: digital text"
                elif page.get_images(full=False) or page.get_drawings():
                    pages.append(_page_from_image(image))
                    message = f"Page {index + 1}: OCR"
                else:
                    pages.append({"width": image.width, "height": image.height, "tokens": [],
                                  "hlines": [], "vsegs": [], "source": "blank"})
                    message = f"Page {index + 1}: blank"

                if progress_callback:
                    progress_callback(index + 1, total, message)

    if use_cache and any(p.get("source") == "ocr" for p in pages):
        _save_cached_pages(file_path, pages)
    return pages


# =============================================================================
# 4. LAYOUT -> ITEMS  (text lines and table rows, in reading order)
# =============================================================================

def _group_into_lines(tokens):
    """Groups tokens that sit on the same visual line; returns list of (y, x0, x1, text)."""
    if not tokens:
        return []
    heights = sorted(max(1.0, t["y1"] - t["y0"]) for t in tokens)
    median_h = heights[len(heights) // 2]
    ordered = sorted(tokens, key=lambda t: ((t["y0"] + t["y1"]) / 2.0, t["x0"]))
    lines = []
    for tok in ordered:
        cy = (tok["y0"] + tok["y1"]) / 2.0
        if lines and abs(cy - lines[-1]["cy"]) <= median_h * 0.55:
            lines[-1]["tokens"].append(tok)
            n = len(lines[-1]["tokens"])
            lines[-1]["cy"] = (lines[-1]["cy"] * (n - 1) + cy) / n
        else:
            lines.append({"cy": cy, "tokens": [tok]})
    result = []
    for line in lines:
        toks = sorted(line["tokens"], key=lambda t: t["x0"])
        text = " ".join(t["text"].strip() for t in toks)
        result.append((line["cy"], toks[0]["x0"], toks[-1]["x1"], re.sub(r"\s+", " ", text).strip()))
    return result


def _dedupe_positions(values, tolerance=8):
    out = []
    for v in sorted(values):
        if not out or v - out[-1] > tolerance:
            out.append(v)
    return out


def page_items(page):
    """
    Converts a page into an ordered list of items:
        {"kind": "text", "text", "y", "x0", "x1", "width"}
        {"kind": "row",  "module", "content": [lines], "hours", "y"}
    """
    width = page.get("width") or 1000
    tokens = page.get("tokens") or []
    hlines = page.get("hlines") or []
    vsegs = page.get("vsegs") or []

    # Only treat a grid as a table when it has at least two vertical rulings
    tall_vsegs = [v for v in vsegs if (v[2] - v[1]) > 25]

    in_table = {}
    outside = []
    for tok in tokens:
        cx = (tok["x0"] + tok["x1"]) / 2.0
        cy = (tok["y0"] + tok["y1"]) / 2.0
        xs = _dedupe_positions([x for x, y0, y1 in tall_vsegs if y0 - 4 <= cy <= y1 + 4])
        if len(xs) >= 2 and xs[0] - 2 <= cx <= xs[-1] + 2:
            # --- which column? ---
            zone = 0
            for i in range(len(xs) - 1):
                if xs[i] <= cx <= xs[i + 1]:
                    zone = i
                    break
            zones = len(xs) - 1
            table_w = xs[-1] - xs[0]
            first_w = xs[1] - xs[0]
            if zones >= 3:
                col = "module" if zone == 0 else ("hours" if zone == zones - 1 else "content")
            elif zones == 2:
                if first_w < table_w * 0.3:
                    col = "module" if zone == 0 else "content"
                else:
                    col = "content" if zone == 0 else "hours"
            else:
                col = "content"
            # --- which row band? ---
            above = [y for y, x0, x1 in hlines if y < cy and x0 - 4 <= cx <= x1 + 4]
            band = max(above) if above else -1
            in_table.setdefault(band, {"module": [], "content": [], "hours": []})[col].append(tok)
        else:
            outside.append(tok)

    items = []
    for cy, x0, x1, text in _group_into_lines(outside):
        items.append({"kind": "text", "text": text, "y": cy, "x0": x0, "x1": x1, "width": width})

    first_band = min(in_table.keys()) if in_table else None
    for band, cols in in_table.items():
        content_lines = [text for _, _, _, text in _group_into_lines(cols["content"])]
        module_text = " ".join(text for _, _, _, text in _group_into_lines(cols["module"]))
        hours_text = " ".join(text for _, _, _, text in _group_into_lines(cols["hours"]))
        all_toks = cols["module"] + cols["content"] + cols["hours"]
        y = min(t["y0"] for t in all_toks) if all_toks else band
        items.append({
            "kind": "row",
            "module": module_text.strip(),
            "content": content_lines,
            "hours": hours_text.strip(),
            "y": y,
            "first_on_page": band == first_band,
        })

    items.sort(key=lambda item: item["y"])
    return items


# =============================================================================
# 5. TEXT CLEANING HELPERS
# =============================================================================

OCR_FIXES = [
    (r"\bwrile\b", "write"), (r"\bcreale\b", "create"), (r"\bnCw\b", "new"),
    (r"\bt0\b", "to"), (r"\bandFile\b", "and File"), (r"\bCodeIAECer\b", "CodeIgniter"),
    (r"\bCode[lI1|\[][^\s,)]{3,7}", "CodeIgniter"), (r"\bDeletg\b", "Delete"), (r"\bDelet\b", "Delete"),
    (r"\bFrameWork\b", "Framework"), (r"\bHerjburg", "Herzberg"), (r"\bMcGragor", "McGregor"),
    (r",(?=[A-Za-z(])", ", "), (r"\s+,", ","), (r"(?<=\w)\s+\.(?=\s|$)", "."), (r"\(\s+", "("), (r"\s+\)", ")"),
]

ACRONYMS = {
    "php": "PHP", "mysql": "MySQL", "sql": "SQL", "html": "HTML", "css": "CSS", "oop": "OOP",
    "dbms": "DBMS", "ai": "AI", "ml": "ML", "api": "API", "apis": "APIs", "os": "OS",
    "it": "IT", "cms": "CMS", "jvm": "JVM", "mvc": "MVC", "orm": "ORM", "crud": "CRUD",
    "xss": "XSS", "nosql": "NoSQL", "tcp/ip": "TCP/IP", "evs": "EVS", "bca": "BCA",
    "iot": "IoT", "ui": "UI", "ux": "UX", "c++": "C++", "c#": "C#", "dsa": "DSA",
}
SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with", "vs", "vs."}

NOISE_LINE = re.compile(
    r"^(?:"
    r"page\s*\d+(?:\s*of\s*\d+)?|\d+\s*/\s*\d+|\[\s*\d+\s*\]|---\s*page.*---|"
    r"semester\s*[-–—:]?\s*[0-9ivx]+|www\..+|https?://\S+|all rights reserved|confidential"
    r")$",
    re.IGNORECASE,
)

META_LINE = re.compile(
    r"^(?:detailed\s+syllabus|name\s+of\s+the\s+topic|course\s+content|subject\s+name|subject\s+code|"
    r"course\s+code|paper\s+code|course\s+objectives?|course\s+outcomes?|learning\s+outcomes?|"
    r"contact\s+hours|credits?|hours|marks|semester|page(?:\s+number)?|suggested\s+readings?|"
    r"text\s*books?|reference\s*books?|references?|module|modu\s*le|module\s+no\.?|module\s+number|"
    r"unit|sl\.?\s*no\.?|s\.?\s*no\.?|sr\.?\s*no\.?|no\s*[:.]?|index)\s*[:.\-]*\s*$",
    re.IGNORECASE,
)

HEADER_WORDS = re.compile(r"name\s+of\s+the\s+topic|course\s+content|module\s+(?:no|number)|^modu|detailed\s+syllabus",
                          re.IGNORECASE)

SKIP_SECTION = re.compile(
    r"^(?:suggested\s+readings?|text\s*books?|reference\s*books?|references?|recommended\s+books?|"
    r"books?\s+recommended|course\s+outcomes?|course\s+objectives?|learning\s+outcomes?|"
    r"after\s+(?:successful\s+)?completion|scheme\s+of\s+exam|evaluation\s+scheme)\b",
    re.IGNORECASE,
)

SUBJECT_LINE = re.compile(
    r"^(?:subject\s*(?:name|title)?|course\s*(?:title|name)|paper\s*(?:title|name))\s*[:\-–]\s*(.+)$",
    re.IGNORECASE,
)
COURSE_CODE_HEADER = re.compile(
    r"^((?:BCA|MCA|CS|IT|BSC|B\.?SC|B\.?TECH)[A-Z0-9\-]*\s*[-:]?\s*[0-9IVX][0-9IVX\.\-]*)\s*[:\-–]\s*([A-Za-z].{2,})$",
    re.IGNORECASE,
)
CODE_LINE = re.compile(r"^(?:subject|course|paper)\s*code\s*[:\-–]?\s*([A-Za-z0-9][A-Za-z0-9\-\s]*)$", re.IGNORECASE)
CREDIT_LINE = re.compile(r"^credits?\s*[:\-–]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
CONTACT_LINE = re.compile(r"^contact\s*hours?\s*[:\-–]?\s*(\d+)", re.IGNORECASE)
PRACTICAL_LINE = re.compile(r"^(?:practical|laboratory|lab)\s*[:\-]?\s*$", re.IGNORECASE)

MODULE_TEXT = re.compile(
    r"^(?:unit|module|mod\.?|chapter|block)\s*[-:.]?\s*([IVXivx]{1,5}|\d{1,2})\b\s*[:.\-–)]?\s*(.*)$",
    re.IGNORECASE,
)
M_LABEL = re.compile(r"^[Mm]\s?[-.]?\s?([0-9lI|]{1,2})\b\s*[:.\-–]?\s*(.*)$")
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}


def normalize_ocr_text(text):
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.replace("", "").replace("□", "").replace("☐", "").replace("�", "")
    for pattern, replacement in OCR_FIXES:
        text = re.sub(pattern, replacement, text)
    return re.sub(r"\s+", " ", text).strip()


def smart_title(name):
    name = normalize_ocr_text(name).strip(" :-–")
    words = name.split()
    is_shouting = name.upper() == name
    out = []
    for i, word in enumerate(words):
        key = word.lower().strip(",.")
        if key in ACRONYMS:
            out.append(ACRONYMS[key])
        elif i > 0 and key in SMALL_WORDS:
            out.append(key)
        elif is_shouting or word.islower() or word.istitle():
            out.append(word[:1].upper() + word[1:].lower())
        else:
            out.append(word)  # keep mixed case like "JavaScript"
    return " ".join(out)


def _to_int(label):
    label = (label or "").strip()
    if re.fullmatch(r"[IVXivx]{1,5}", label):
        return ROMAN.get(label.lower())
    if re.fullmatch(r"[0-9lI|]{1,2}", label) and re.search(r"\d", label):
        label = label.replace("l", "1").replace("I", "1").replace("|", "1")
    return int(label) if label.isdigit() else None


def parse_module_label(text):
    """'M1', '1', 'Unit II', 'MODULE 3' -> (number, remainder) or (None, text)."""
    text = (text or "").strip()
    if not text:
        return None, ""
    m = M_LABEL.match(text)
    if m:
        num = _to_int(m.group(1))
        if num:
            return num, m.group(2).strip()
    m = MODULE_TEXT.match(text)
    if m:
        num = _to_int(m.group(1))
        if num:
            return num, m.group(2).strip()
    m = re.match(r"^(\d{1,2})\s*[.)]?\s*$", text)
    if m:
        return int(m.group(1)), ""
    m = re.match(r"^([IVX]{1,4})\s*[.)]?\s*$", text)
    if m:
        return ROMAN.get(m.group(1).lower()), ""
    return None, text


def parse_module_cell(text):
    """Module column of a table: tolerant to OCR slips such as 'MS' for 'M5'."""
    text = (text or "").strip()
    number, _ = parse_module_label(text)
    if number:
        return number
    m = re.match(r"^[Mm]\s?([0-9lI|SOZB])\s*[.:]?$", text)
    if m:
        return int(m.group(1).translate(str.maketrans("lI|SOZB", "1115028")))
    m = re.search(r"\b(\d{1,2})\b", text)
    if m and len(text) <= 6:
        return int(m.group(1))
    return None


def _is_noise(line):
    if not line:
        return True
    if NOISE_LINE.match(line):
        return True
    if len(line) <= 2 and not line.isdigit():
        return True
    letters = sum(ch.isalpha() for ch in line)
    return letters < 2 and not re.fullmatch(r"\d{1,3}", line)


def clean_text(text):
    """
    Cleans plain extracted text (kept for the text-based pipeline and tests):
    removes page markers/numbers, URLs, outcome boilerplate, collapses spaces.
    """
    if not text:
        return ""
    cleaned = []
    seen_recent = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line or NOISE_LINE.match(line) or re.fullmatch(r"\d+", line):
            continue
        if re.match(r"^(?:co\d+\s*:|after\s+(?:successful\s+)?completion|students?\s+(?:will|shall)\s+be\s+able)",
                    line, re.IGNORECASE):
            continue
        if len(line) < 2 and not line.isalnum():
            continue
        if line in seen_recent:
            continue
        seen_recent = (seen_recent + [line])[-3:]
        cleaned.append(line)
    return "\n".join(cleaned)


# =============================================================================
# 6. CURRICULUM STATE MACHINE
# =============================================================================

HEADING_STOPWORDS = {"case study", "note", "e.g", "eg", "example", "examples", "i.e", "ie"}


class _Builder:
    def __init__(self):
        self.subjects = []          # list of subject dicts (ordered)
        self.subject = None
        self.module = None
        self.topic = None
        self.state = "preamble"     # preamble | meta | content | skip
        self.pending_lab = False
        self.last_line_complete = True

    # ---- subjects -------------------------------------------------------------
    def start_subject(self, raw_name, code=""):
        name = re.sub(r"\s*\((?:theory|practical)\)\s*$", "", raw_name, flags=re.IGNORECASE)
        name = re.split(r"\s+(?:subject\s*code|course\s*code|credit|contact\s*hours)\b", name, flags=re.IGNORECASE)[0]
        name = smart_title(name)
        if len(name) < 3 or HEADER_WORDS.search(name) or name.lower().startswith("modu"):
            return False
        is_lab = self.pending_lab or bool(re.search(r"\b(lab|laboratory|practical)\b", name, re.IGNORECASE))
        self.subject = {"name": name, "code": code, "credit": None, "contact_hours": None,
                        "is_lab": is_lab, "modules": []}
        self.subjects.append(self.subject)
        self.module = None
        self.topic = None
        self.state = "meta"
        self.pending_lab = False
        self.last_line_complete = True
        return True

    # ---- modules --------------------------------------------------------------
    def start_module(self, number=None, title=""):
        if self.subject is None:
            self.start_subject("General Syllabus")
        modules = self.subject["modules"]
        if number is None:
            number = len(modules) + 1
        # same number repeated (e.g. label re-read on next page) -> continue it
        if modules and modules[-1]["number"] == number:
            self.module = modules[-1]
        else:
            self.module = {"number": number, "title": "", "hours": None, "topics": []}
            modules.append(self.module)
        self.topic = None
        self.state = "content"
        self.last_line_complete = True
        title = normalize_ocr_text(title or "")
        if title:
            hours_match = re.search(r"\s(\d{1,2})\s*$", title)
            if hours_match and len(title.split()) > 1:
                self.set_hours(hours_match.group(1))
                title = title[:hours_match.start()].strip()
            self.add_line(title, allow_title=True, module_title=True)

    def set_hours(self, hours_text):
        if self.module is None:
            return
        m = re.search(r"\d{1,3}(?:\.\d)?", hours_text or "")
        if m and self.module["hours"] is None:
            value = float(m.group(0))
            if 0 < value <= 60:
                self.module["hours"] = value

    # ---- topics ---------------------------------------------------------------
    def _new_topic(self, title, first_text=""):
        topic = {"title": title, "lines": []}
        if first_text:
            topic["lines"].append(first_text)
        self.module["topics"].append(topic)
        self.topic = topic
        if not self.module["title"] and title:
            self.module["title"] = title

    def add_line(self, line, allow_title=False, module_title=False):
        line = normalize_ocr_text(line)
        if _is_noise(line) or META_LINE.match(line) or HEADER_WORDS.search(line):
            return
        if self.module is None:
            self.start_module(None)

        # 1. "Heading:" on its own line
        m = re.match(r"^(.{3,90}?)\s*:\s*$", line)
        if m and len(m.group(1).split()) <= 10:
            self._new_topic(m.group(1).strip())
            self.last_line_complete = True
            return

        # 2. "Short Heading: description ..." at the start of a line
        m = re.match(r"^([A-Z][A-Za-z0-9/&'()\- ]{2,60}?)\s*:\s+(.+)$", line)
        if m and m.group(1).strip().lower() not in HEADING_STOPWORDS:
            phrase = m.group(1).strip()
            words = phrase.split()
            title_case = all(w[:1].isupper() for w in words if len(w) > 3)
            if (self.last_line_complete and len(words) <= 6) or (len(words) <= 4 and title_case):
                self._new_topic(phrase, m.group(2).strip())
                self.last_line_complete = bool(re.search(r"[.:;)]\s*$", line))
                return

        # 3. Bold-style title with no punctuation (lab tables), or a module title
        looks_like_title = (
            len(line.split()) <= 9 and not re.search(r"[.,;]\s*$", line)
            and line[:1].isupper() and not re.search(r"\b(?:is|are|will|should|can)\b", line)
        )
        if allow_title and looks_like_title and (module_title or self.topic is None or self._topic_has_body()):
            if self.topic is None or self._topic_has_body():
                self._new_topic(line)
                self.last_line_complete = True
                return

        # 4. otherwise it is description text of the current topic
        if self.topic is None:
            self._new_topic("", line)
        else:
            self.topic["lines"].append(line)
        self.last_line_complete = bool(re.search(r"[.:;)]\s*$", line))

    def _topic_has_body(self):
        return bool(self.topic and self.topic["lines"])

    # ---- items ----------------------------------------------------------------
    def feed_text(self, item):
        line = normalize_ocr_text(item["text"])
        if not line:
            return
        width = item.get("width") or 1000

        if PRACTICAL_LINE.match(line):
            self.pending_lab = True
            return

        m = SUBJECT_LINE.match(line)
        if m and not HEADER_WORDS.search(m.group(1)):
            if self.start_subject(m.group(1)):
                return
        m = COURSE_CODE_HEADER.match(line)
        if m and self.state in ("preamble", "skip", "content", "meta"):
            if self.start_subject(m.group(2), code=m.group(1).strip()):
                return

        if self.subject is not None:
            m = CODE_LINE.match(line)
            if m:
                self.subject["code"] = m.group(1).replace(" ", "").strip()
                return
            m = CREDIT_LINE.match(line)
            if m and self.state != "content":
                self.subject["credit"] = float(m.group(1))
                return
            m = CONTACT_LINE.match(line)
            if m:
                self.subject["contact_hours"] = float(m.group(1))
                return

        if SKIP_SECTION.match(line):
            self.state = "skip" if self.state == "content" or self.subject is None else "meta"
            return
        if re.match(r"^detailed\s+syllabus|^course\s+content", line, re.IGNORECASE) or \
                re.search(r"name\s+of\s+the\s+topic", line, re.IGNORECASE):
            if self.subject is not None:
                self.state = "content"
            return

        # explicit unit/module line switches to content mode
        number, rest = parse_module_label(line)
        explicit_label = bool(MODULE_TEXT.match(line) or M_LABEL.match(line))
        if explicit_label and number and self.subject is not None and self.state in ("meta", "content"):
            self.start_module(number, rest)
            return

        if self.state != "content":
            return

        # stand-alone numbers: right side -> hours, left side -> module number
        if re.fullmatch(r"\d{1,2}", line):
            cx = (item.get("x0", 0) + item.get("x1", 0)) / 2.0
            if cx > width * 0.72:
                self.set_hours(line)
            elif cx < width * 0.25:
                self.start_module(int(line))
            return

        self.add_line(line)

    def feed_row(self, item):
        module_text = normalize_ocr_text(item.get("module", ""))
        content = [normalize_ocr_text(c) for c in item.get("content", []) if c and c.strip()]
        hours = item.get("hours", "")

        joined = " ".join([module_text] + content)
        if HEADER_WORDS.search(joined):
            if self.subject is not None:
                self.state = "content"
            return
        if self.subject is None or self.state == "skip":
            # a table that is not part of a subject's syllabus (e.g. scheme table)
            if self.subject is None:
                return
        if not content and not re.search(r"\d", module_text):
            return  # header fragments such as "No:" / "E NO ."

        number = parse_module_cell(module_text)
        if number is None and content:
            number_inline, rest = parse_module_label(content[0])
            if number_inline and M_LABEL.match(content[0]):
                number, content[0] = number_inline, rest

        first_line = content[0] if content else ""
        looks_like_heading = bool(
            re.search(r":\s*$", first_line)
            or (first_line[:1].isupper() and len(first_line.split()) <= 9
                and not re.search(r"[.,;]\s*$", first_line))
        )
        if number is not None:
            self.start_module(number)
        elif self.module is None or not item.get("first_on_page") or looks_like_heading:
            # every table row is one module; only a page-top row without a
            # heading is the continuation of the previous page's module
            self.start_module(None if self.module is None else self.module["number"] + 1)
        self.state = "content"

        if hours:
            self.set_hours(hours)

        for index, line in enumerate(content):
            if SKIP_SECTION.match(line):
                continue
            self.add_line(line, allow_title=(index == 0))

    # ---- output ---------------------------------------------------------------
    def result(self):
        return self.subjects


def _split_subtopics(text):
    """Splits a topic description into readable subtopic items (sentences / dash lists)."""
    text = normalize_ocr_text(text)
    if not text:
        return []
    text = re.sub(r"\b(vs|viz|etc|e\.g|i\.e|Dr|Mr|Ms|No)\.", lambda m: m.group(1) + "\u2024", text)
    text = re.sub(r"(?<=[a-z0-9)])\.\s+(?=[A-Za-z])", ".\n", text)
    parts = []
    for chunk in re.split(r"\n|;|\s[–—]\s|\s-\s", text):
        chunk = chunk.strip(" .,:-–")
        if len(chunk) < 3:
            continue
        if len(chunk) > 80:
            # long run-on sentence: break at commas into ~60 character groups
            buffer = ""
            for piece in chunk.split(", "):
                joins = re.match(r"^(?:and|or|&)\b", piece.strip(), re.IGNORECASE)
                if buffer and len(buffer) + len(piece) > 70 and not joins:
                    parts.append(buffer.strip(" ,"))
                    buffer = piece
                else:
                    buffer = f"{buffer}, {piece}" if buffer else piece
            if buffer.strip(" ,"):
                parts.append(buffer.strip(" ,"))
        else:
            parts.append(chunk)
    seen, unique = set(), []
    for part in parts:
        key = part.lower()
        if key not in seen:
            seen.add(key)
            unique.append((part[:1].upper() + part[1:]).replace("\u2024", "."))
    return unique


def _title_from_paragraph(text):
    """Derives a short title from a case-study paragraph."""
    text = normalize_ocr_text(text)
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].strip(" .")
    m = re.match(r"^(?:A|An|The)\s+(.{2,40}?)\s+(?:wants?|needs?|requires?|has|is)\s+(?:to\s+)?(?:a\s+|an\s+)?(.+)$",
                 first, re.IGNORECASE)
    if m:
        who = smart_title(m.group(1))
        what = re.split(r",|\s(?:based|including|where|so that|which)\s", m.group(2))[0]
        what = " ".join(what.split()[:6]).rstrip(",;")
        return f"{who} – {what}"
    words = first.split()
    return " ".join(words[:8]) + ("…" if len(words) > 8 else "")


def calculate_topic_difficulty(topic_name, subtopics):
    """
    Estimates topic difficulty on a scale from 1.0 (introductory) to 5.0 (advanced)
    based on technical keywords, depth, and subtopic count.
    """
    score = 2.0
    text_to_eval = (topic_name + " " + " ".join(subtopics)).lower()

    high_difficulty_keywords = [
        "architecture", "algorithm", "concurrency", "distributed", "multithreading",
        "optimization", "security", "cryptography", "compiler", "kernel", "protocol",
        "normalization", "indexing", "transaction", "deadlock", "asymptotic", "trees",
        "graphs", "dynamic programming", "neural", "calculus", "differential",
        "pointers", "memory management", "socket", "ajax", "session management",
        "regular expressions", "machine learning", "deep learning", "microprocessor",
        "pipeline", "cache", "inheritance", "polymorphism", "recursion", "leverage",
        "capital budgeting", "cost of capital", "serialization", "framework", "orm",
        "authentication", "exception", "working capital", "dividend", "irr", "npv",
    ]
    low_difficulty_keywords = [
        "introduction", "overview", "basics", "fundamentals", "history", "definition",
        "features", "syntax", "variables", "data types", "operators", "constants",
        "comments", "installation", "environment", "simple", "components", "meaning",
    ]
    for kw in high_difficulty_keywords:
        if kw in text_to_eval:
            score += 0.45
    for kw in low_difficulty_keywords:
        if kw in text_to_eval:
            score -= 0.35

    num_subs = len(subtopics)
    if num_subs > 5:
        score += 0.5
    elif num_subs > 2:
        score += 0.25
    return round(max(1.0, min(5.0, score)), 2)


def build_curriculum(subject_list):
    """
    Turns the raw builder output into the app's structure:
    { subject_name: {code, credit, contact_hours, is_lab, modules:[...], topics:[...], total_topics} }
    Each topic carries unit_name, subtopics, difficulty_score and syllabus_hours.
    """
    curriculum = {}
    for subj in subject_list:
        name = subj["name"]
        if name in curriculum:
            base, n = name, 2
            while f"{base} ({n})" in curriculum:
                n += 1
            name = f"{base} ({n})"

        modules_out = []
        flat_topics = []
        seen_titles = set()
        modules = [m for m in subj["modules"] if any(t["title"] or t["lines"] for t in m["topics"])]

        # modules without syllabus hours share the remaining contact hours evenly
        known = sum(m["hours"] or 0 for m in modules)
        unknown = [m for m in modules if not m["hours"]]
        if unknown and subj.get("contact_hours"):
            share = max(0.0, (subj["contact_hours"] - known)) / len(unknown)
            for m in unknown:
                m["hours_estimated"] = round(share, 1) if share > 0 else None

        for module in modules:
            topics = []
            for t in module["topics"]:
                description = " ".join(t["lines"]).strip()
                title = t["title"].strip(" :-–") if t["title"] else ""
                if not title:
                    title = _title_from_paragraph(description) if description else ""
                if not title:
                    continue
                title = normalize_ocr_text(title)
                key = re.sub(r"[^a-z0-9]", "", title.lower())
                if key in seen_titles:
                    # merge duplicate topics (e.g. repeated on two pages)
                    for existing in topics:
                        if re.sub(r"[^a-z0-9]", "", existing["topic_name"].lower()) == key:
                            existing["subtopics"].extend(_split_subtopics(description))
                    continue
                seen_titles.add(key)
                subtopics = _split_subtopics(description)
                topics.append({
                    "topic_name": title,
                    "description": description,
                    "subtopics": subtopics,
                })
            if not topics:
                continue

            module_title = module["title"] or topics[0]["topic_name"]
            unit_name = f"Module {module['number']}: {module_title}"
            hours = module.get("hours") or module.get("hours_estimated")
            weights = [max(1, len(t["description"].split()) + 4) for t in topics]
            total_w = float(sum(weights))
            for t, w in zip(topics, weights):
                t["unit_name"] = unit_name
                t["module_number"] = module["number"]
                t["module_hours"] = hours
                t["syllabus_hours"] = round(hours * w / total_w, 2) if hours else None
                t["subtopic_count"] = len(t["subtopics"])
                t["topic_length"] = len(t["topic_name"]) + sum(len(s) for s in t["subtopics"])
                t["word_count"] = len(t["topic_name"].split()) + len(t["description"].split())
                t["difficulty_score"] = calculate_topic_difficulty(t["topic_name"], t["subtopics"])
                t["is_lab"] = subj["is_lab"]
            modules_out.append({
                "number": module["number"],
                "title": module_title,
                "unit_name": unit_name,
                "hours": hours,
                "hours_from_syllabus": bool(module.get("hours")),
                "topics": topics,
            })
            flat_topics.extend(topics)

        curriculum[name] = {
            "code": subj.get("code", ""),
            "credit": subj.get("credit"),
            "contact_hours": subj.get("contact_hours"),
            "is_lab": subj["is_lab"],
            "modules": modules_out,
            "topics": flat_topics,
            "total_topics": len(flat_topics),
        }
    return curriculum


def parse_pages(pages):
    builder = _Builder()
    for page in pages:
        for item in page_items(page):
            if item["kind"] == "row":
                builder.feed_row(item)
            else:
                builder.feed_text(item)
    return build_curriculum(builder.result())


def detect_subjects_and_topics(cleaned_text, include_empty=False):
    """
    Text-only parser (used for plain text and by tests).
    Returns {subject: {code, modules, topics, total_topics, ...}}.
    """
    if not cleaned_text:
        return {}
    builder = _Builder()
    for line in cleaned_text.split("\n"):
        line = line.strip()
        if line:
            builder.feed_text({"kind": "text", "text": line, "x0": 0, "x1": 0, "width": 1000})
    curriculum = build_curriculum(builder.result())
    if not include_empty:
        curriculum = {k: v for k, v in curriculum.items() if v["total_topics"] > 0}
    return curriculum


def pages_to_text(pages):
    """Human-readable text of the extracted pages (stored for reference/debugging)."""
    out = []
    for index, page in enumerate(pages, start=1):
        out.append(f"--- Page {index} ---")
        for item in page_items(page):
            if item["kind"] == "row":
                cells = [item["module"], " ".join(item["content"]), item["hours"]]
                out.append(" | ".join(c for c in cells if c))
            else:
                out.append(item["text"])
    return "\n".join(out)


def extract_text_from_pdf(file_path):
    """Kept for backwards compatibility: returns the full text of every page."""
    return pages_to_text(extract_pages(file_path, "pdf"))


# =============================================================================
# 7. VALIDATION + MAIN ENTRY POINT
# =============================================================================

FALSE_SUBJECT = re.compile(r"^(?:modu|modul|module|unit|detailed|name of|general subject$)", re.IGNORECASE)


def validate_curriculum(subjects):
    issues = []
    if not subjects:
        issues.append("No usable curriculum content was detected.")
    for name, data in subjects.items():
        if FALSE_SUBJECT.match(name):
            issues.append(f"Suspicious subject name: {name}")
        for topic in data.get("topics", []):
            if META_LINE.match(topic["topic_name"]):
                issues.append(f"Metadata detected as topic: {topic['topic_name']}")
    return issues


def process_syllabus(file_path, file_type, progress_callback=None):
    """
    Main entry point.  Returns a dict with subjects, counts, warnings and the
    extracted text.  Every page is processed.
    """
    file_type = (file_type or "").lower()
    empty = {"raw_text": "", "cleaned_text": "", "subjects": {}, "total_subjects": 0,
             "total_topics": 0, "total_modules": 0, "skipped_subjects": [], "warnings": [], "pages": 0}

    if file_type not in ("pdf", "png", "jpg", "jpeg"):
        return dict(empty, success=False,
                    error=f"Unsupported file type: {file_type}. Supported types: PDF, PNG, JPG, JPEG.")
    try:
        pages = extract_pages(file_path, file_type, progress_callback=progress_callback)
        if any(p.get("source") == "ocr" for p in pages) and not any(p.get("tokens") for p in pages):
            return dict(empty, success=False,
                        error="Syllabus processing failed: no OCR engine could read this scanned file. "
                              "Install one with:  pip install rapidocr-onnxruntime")

        raw_text = pages_to_text(pages)
        curriculum = parse_pages(pages)

        # Fallback for unusual layouts: plain text parser
        if not any(v["total_topics"] for v in curriculum.values()):
            curriculum = detect_subjects_and_topics(clean_text(raw_text), include_empty=True)

        skipped = [name for name, data in curriculum.items() if data["total_topics"] == 0]
        subjects = {name: data for name, data in curriculum.items() if data["total_topics"] > 0}
        warnings = validate_curriculum(subjects)
        if not subjects:
            return dict(empty, success=False, raw_text=raw_text, pages=len(pages),
                        error="No usable curriculum content was detected.")

        return {
            "success": True,
            "error": None,
            "raw_text": raw_text,
            "cleaned_text": clean_text(raw_text),
            "subjects": subjects,
            "skipped_subjects": skipped,
            "total_subjects": len(subjects),
            "total_modules": sum(len(s["modules"]) for s in subjects.values()),
            "total_topics": sum(s["total_topics"] for s in subjects.values()),
            "warnings": warnings,
            "pages": len(pages),
            "ocr_pages": sum(1 for p in pages if p.get("source") == "ocr"),
        }
    except Exception as err:
        import traceback
        traceback.print_exc()
        return dict(empty, success=False, error=f"Syllabus processing failed. Please try again. ({err})")
