#!/usr/bin/env python3
"""
convert.py — Convert PDFs to clean single-column text files.

Handles multi-column layouts and tables common in oral history transcripts.

Usage:
    python convert.py [input] [--output-dir OUTPUT_DIR]

    input can be either a single PDF file or a folder containing PDFs.
"""

import argparse
import re
import sys
from pathlib import Path

import pdfplumber


# Vertical threshold (in points) for header/footer zones at top/bottom of page.
# Running headers and footers (page numbers, institution names, "Page X of Y")
# live in these margins; body text does not normally reach them. The zone spans
# the top one-inch (72pt) margin, tall enough to include multi-line running
# headers (e.g. a name line above an accession-ID line) that sit a little below
# the very top of the page, while leaving a page-1 title block — which begins at
# the one-inch content margin — intact.
HEADER_ZONE_HEIGHT = 72
FOOTER_ZONE_HEIGHT = 60

# Pattern for detecting timestamp columns (MM:SS format)
TIMESTAMP_PATTERN = re.compile(r'^\d{1,2}:\d{2}$')

# Pattern for detecting speaker labels (e.g. "EJ:", "BN:", "INTERVIEWER:").
# A single token ending in a colon, short enough to be an initials/name tag.
SPEAKER_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][\w.'\-]{0,19}:$")

# Pattern for a full speaker-label line: one to four capitalized name/initial
# tokens ending in a colon, e.g. "GA:", "Eugene Hunt:", "J. Michael Graves:".
SPEAKER_LINE_PATTERN = re.compile(
    r"^[A-Z][A-Za-z.'\-]*(?:\s+[A-Za-z][A-Za-z.'\-]*){0,3}:$"
)

# The repeated column-header row of the transcript table.
TABLE_HEADER_PATTERN = re.compile(r"^timestamp\s+speaker\s+content$", re.IGNORECASE)

# Start of a speaker turn: an optional leading timestamp, then a speaker label
# (one to four capitalized name/initial tokens) ending in a colon, e.g.
# "00:14 Eugene Hunt:", "1:16:20 J. Michael Graves:", "GA:", "Interviewee:". Used
# by --reflow to decide where a paragraph must break. The timestamp may be
# MM:SS or H:MM:SS. Matched against a reconstructed line (label + content), so the
# colon may be followed by the spoken text rather than the end of string.
TURN_START_PATTERN = re.compile(
    r"^(?:\d{1,2}:\d{2}(?::\d{2})?\s+)?"
    r"[A-Z][A-Za-z.'\-]*(?:\s+[A-Za-z][A-Za-z.'\-]*){0,3}:(?:\s|$)"
)

# An accession / collection ID used as a running header, e.g. "LGBTQ-OH-029",
# "AMN-123". Two or more uppercase letters followed by hyphen-joined groups of
# letters and/or digits, optionally trailed by a running page number so a header
# row that reads as a single line ("LGBTQ-OH-080 2") is still recognized.
ACCESSION_ID_PATTERN = re.compile(r"^[A-Z]{2,}(?:-[A-Za-z0-9]+)+(?:\s+\d+)?$")

# End-of-recording markers that close a transcript, e.g. "End Recording.",
# "End of Recording", "[End of Recording]". These are session boilerplate, not
# spoken words, so they are dropped rather than merged into a speaker's turn.
RECORDING_MARKER_PATTERN = re.compile(
    r"^\[?\s*end\s+(?:of\s+)?recording\.?\s*\]?$", re.IGNORECASE
)

# A short running-header name line: one to four title-cased tokens, e.g.
# "DeLesslin George-Warren", "Ruby Cornwell". Each token starts with a capital
# and may contain internal letters, hyphens, apostrophes, or periods (to allow
# names like "George-Warren" or "J. Michael Graves"). Unlike the all-uppercase
# rule below, this catches title-cased header names.
NAME_HEADER_PATTERN = re.compile(
    r"^[A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){0,3}$"
)

# Marks the boundary between the opening metadata block and the transcript body.
# Transcripts open the interview with one of these cues, sometimes preceded by a
# rule of underscores:
#   - "Begin Interview" / "Begin Recording" / "Begin Transcript" (alston,
#     DeLesslin George-Warren)
#   - "Transcript Tape 1, Side A" (cornwell, graves), which follows the Abstract
# Everything up to and including this marker is front matter and is dropped by
# :func:`strip_front_matter`. The phrases are specific enough that they do not
# recur in the spoken dialogue.
FRONT_MATTER_END_PATTERN = re.compile(
    r"(?:_{3,}\s*)?"
    r"(?:"
    r"begin(?:ning)?\s+(?:the\s+)?(?:interview|recording|transcript)"
    r"|start\s+of\s+(?:the\s+)?(?:interview|recording)"
    r"|transcript\s+tape\s+\d+\s*,?\s*side\s+\w+"
    r")",
    re.IGNORECASE,
)

# A metadata field-label line, e.g. "Interviewee: ...", "Date of Interview: ...".
# Used to confirm that the text preceding a boundary marker really is a front-
# matter block (two or more such labels) before anything is dropped, so a stray
# marker-like phrase can never truncate an interview that has no front matter.
FIELD_LABEL_PATTERN = re.compile(
    r"(?im)^\s*(?:"
    r"interviewee|interviewer\(s\)|interviewer|narrator|"
    r"place of interview|date of interview|interview date|date of \w+|"
    r"location|interview length|length|pronouns|"
    r"original format|digital format|archival copy|listening copy|"
    r"transcriber|transcription|editor|proofreader|dates? of \w+|"
    r"oral history project|project|collection|accession"
    r")\b[^:\n]{0,40}:"
)


def is_boilerplate_content(words):
    """
    Determine if a group of words is a running header/footer or other
    boilerplate that should be dropped rather than kept as transcript text.

    Boilerplate typically includes:
    - A page number (standalone, "Page X", or "Page X of Y")
    - A name (often uppercase) with a page number
    - An institution name
    - The repeated "Oral History Interview with ...: Transcript" running line
    """
    if not words:
        return False

    # Read the line left-to-right: callers may pass words in ``top`` order, which
    # would scramble a header whose parts sit at the same height but different x
    # (e.g. an accession ID on the left and a page number on the right).
    text = " ".join(w["text"] for w in sorted(words, key=lambda w: w["x0"])).strip()

    # Page-number patterns (standalone number, "Page X", "Page X of Y")
    if re.match(r"^\d+$", text):
        return True
    if re.match(r"^page\s+\d+$", text, re.IGNORECASE):
        return True
    if re.search(r"page\s+\d+\s+of\s+\d+", text, re.IGNORECASE):
        return True

    # Name + page number pattern (e.g., "ALSTON 2", "SMITH 15")
    if re.match(r"^[A-Z]+\s+\d+$", text):
        return True

    # Short uppercase text (likely a name like "ALSTON")
    if len(text) <= 30 and text.isupper() and text.replace(" ", "").isalpha():
        return True

    # Accession / collection ID (e.g. "LGBTQ-OH-029")
    if len(text) <= 30 and ACCESSION_ID_PATTERN.match(text):
        return True

    # Short title-cased running-header name (e.g. "DeLesslin George-Warren").
    # Restricted to the margin zones by the callers, so a body line that merely
    # looks like a name is not at risk here.
    if len(text) <= 40 and NAME_HEADER_PATTERN.match(text):
        return True

    # Running interview footer, e.g.
    # "Oral History Interview with Ruby Cornwell: Transcript Page 2 of 27"
    if re.search(r"oral history interview with .+transcript", text, re.IGNORECASE):
        return True

    # Institutional patterns (e.g. "Avery Normal Institute Oral History Project")
    institutional_keywords = [
        "research center", "university", "college", "institute",
        "library", "archives", "museum", "foundation"
    ]
    text_lower = text.lower()
    if any(kw in text_lower for kw in institutional_keywords):
        return True

    return False


def _group_zone_lines(zone_words):
    """Group a set of words into lines by y-coordinate (within 5pt)."""
    zone_sorted = sorted(zone_words, key=lambda w: w["top"])
    lines = []
    current_line = [zone_sorted[0]]
    for word in zone_sorted[1:]:
        if abs(word["top"] - current_line[-1]["top"]) <= 5:
            current_line.append(word)
        else:
            lines.append(current_line)
            current_line = [word]
    lines.append(current_line)
    return lines


def _filter_margin_boilerplate(words, in_zone):
    """
    Drop words in a top/bottom margin zone whose line reads as boilerplate.

    ``in_zone(word)`` selects the candidate margin words. Only lines that match
    :func:`is_boilerplate_content` are removed; anything else in the zone (real
    body text that happens to reach the margin) is preserved.
    """
    if not words:
        return words

    zone_words = [w for w in words if in_zone(w)]
    body_words = [w for w in words if not in_zone(w)]
    if not zone_words:
        return words

    words_to_keep = list(body_words)
    for line_words in _group_zone_lines(zone_words):
        if not is_boilerplate_content(line_words):
            words_to_keep.extend(line_words)

    return words_to_keep


def filter_header_words(words):
    """Remove running-header boilerplate in the top margin zone."""
    return _filter_margin_boilerplate(words, lambda w: w["top"] < HEADER_ZONE_HEIGHT)


def filter_footer_words(words, page_height):
    """Remove running-footer boilerplate in the bottom margin zone."""
    threshold = page_height - FOOTER_ZONE_HEIGHT
    return _filter_margin_boilerplate(words, lambda w: w["top"] > threshold)


def group_into_rows(words, tol=3):
    """
    Group words into visual rows by ``top`` (within ``tol`` points).

    Rows are returned top-to-bottom; words within each row are sorted
    left-to-right by ``x0``.
    """
    if not words:
        return []
    ws = sorted(words, key=lambda w: (round(w["top"]), w["x0"]))
    rows = []
    current = [ws[0]]
    for w in ws[1:]:
        if abs(w["top"] - current[-1]["top"]) <= tol:
            current.append(w)
        else:
            rows.append(sorted(current, key=lambda x: x["x0"]))
            current = [w]
    rows.append(sorted(current, key=lambda x: x["x0"]))
    return rows


def looks_like_transcript_layout(words, page_width):
    """
    Detect the ``Timestamp | Speaker | Content`` transcript-table layout.

    These pages must be reconstructed row-by-row, never split into vertical
    columns: the speaker column and content column are separated by a wide gap
    that ``detect_columns`` would otherwise treat as a column boundary, emitting
    every speaker label first and all the spoken text afterwards.

    A row counts as a labelled transcript row when, after an optional leading
    timestamp, it begins with a left-margin speaker label (``Name:``) followed
    by spoken text on the same row. Several such rows mark the layout.
    """
    labelled = 0
    for row in group_into_rows(words):
        i = 0
        if i < len(row) and TIMESTAMP_PATTERN.match(row[i]["text"]):
            i += 1
        if i >= len(row):
            continue

        # The label must sit in the left margin, not out in the content column.
        if row[i]["x0"] > page_width * 0.45:
            continue

        # Find the colon that closes the label within the first few tokens.
        label_end = None
        for j in range(i, min(i + 4, len(row))):
            if row[j]["text"].endswith(":"):
                label_end = j
                break
        if label_end is None or label_end + 1 >= len(row):
            # No label, or the label stands alone with no spoken text beside it.
            continue

        label_text = " ".join(row[k]["text"] for k in range(i, label_end + 1))
        if not SPEAKER_LINE_PATTERN.match(label_text):
            continue

        labelled += 1

    return labelled >= 3


def drop_table_header_row(words):
    """Remove the repeated ``Timestamp Speaker Content`` column-header row."""
    if not words:
        return words
    drop = set()
    for row in group_into_rows(words):
        text = " ".join(w["text"] for w in row).strip()
        if TABLE_HEADER_PATTERN.match(text):
            drop.update(id(w) for w in row)
    if not drop:
        return words
    return [w for w in words if id(w) not in drop]


def detect_columns(words, page_width):
    """
    Cluster words into columns by their x0 coordinates.

    Returns a sorted list of (x_start, x_end) tuples defining column boundaries.
    Uses a gap-based algorithm: find horizontal gaps > 30% of page width between
    word clusters.
    """
    if not words:
        return [(0, page_width)]

    gap_threshold = page_width * 0.06

    # Collect all x0 values and sort them
    x_starts = sorted(set(round(w["x0"]) for w in words))

    # Find gaps between word x-positions
    column_breaks = [0]
    prev_x = x_starts[0]
    for x in x_starts[1:]:
        if x - prev_x > gap_threshold:
            column_breaks.append((prev_x + x) / 2)
        prev_x = x
    column_breaks.append(page_width)

    columns = []
    for i in range(len(column_breaks) - 1):
        columns.append((column_breaks[i], column_breaks[i + 1]))

    return columns


def collapse_speaker_label_columns(words, columns, page_width):
    """
    Undo a false column split caused by a speaker-label hanging indent.

    Oral-history transcripts put the speaker label (e.g. "EJ:", "BN:") in the
    left margin and indent the spoken text. The wide gap between the label and
    the indented text looks like a column boundary to ``detect_columns``, which
    causes the whole stack of labels to be emitted before any of the text.

    When the leftmost detected column contains only speaker labels and each of
    those labels shares a line (same ``top``) with content in a column to its
    right, the split is spurious: it is one logical column with a hanging
    indent. In that case collapse everything into a single column so the
    row-by-row line reconstruction re-attaches each label to its own line.
    """
    if len(columns) < 2:
        return columns

    left_start, left_end = columns[0]
    left_words = [
        w for w in words if left_start <= (w["x0"] + w["x1"]) / 2 < left_end
    ]
    right_words = [
        w for w in words if (w["x0"] + w["x1"]) / 2 >= left_end
    ]

    if not left_words or not right_words:
        return columns

    # Look only at left-column words that share a row (same ``top``) with text
    # in a right-hand column. Standalone left words such as a running page
    # header ("Barbara Nicodemus") don't align with any text row; they collapse
    # back into place correctly regardless, so we ignore them here.
    right_tops = [w["top"] for w in right_words]
    aligned_left = [
        w for w in left_words
        if any(abs(w["top"] - rt) <= 3 for rt in right_tops)
    ]

    # Need a few aligned tokens, and every one of them must be a speaker label.
    # In a genuine two-column layout the aligned left words are ordinary prose,
    # so this guard leaves real columns untouched.
    if len(aligned_left) < 2:
        return columns
    if not all(SPEAKER_LABEL_PATTERN.match(w["text"]) for w in aligned_left):
        return columns

    return [(0, page_width)]


def filter_timestamp_words(words):
    """
    Remove words that are timestamps (MM:SS format) at the left margin.

    This handles oral history transcripts where timestamps appear in a
    narrow left column. We identify the leftmost x-position where timestamps
    appear and filter out all timestamp words near that position.
    """
    if not words:
        return words

    # Find timestamp words and their x-positions
    timestamp_words = [w for w in words if TIMESTAMP_PATTERN.match(w["text"])]
    if not timestamp_words:
        return words

    # Find the typical x-position for timestamps (should be near left margin)
    timestamp_x_positions = [w["x0"] for w in timestamp_words]
    min_x = min(timestamp_x_positions)

    # Only filter if timestamps are near the left margin (first 20% of positions)
    all_x = [w["x0"] for w in words]
    x_range = max(all_x) - min(all_x)
    if x_range > 0 and (min_x - min(all_x)) > x_range * 0.2:
        # Timestamps aren't at the left margin, don't filter
        return words

    # Filter out words that are timestamps near the left margin
    margin_threshold = 30  # points
    filtered = [
        w for w in words
        if not (TIMESTAMP_PATTERN.match(w["text"]) and w["x0"] < min_x + margin_threshold)
    ]

    return filtered


def words_to_text(words, columns, strip_timestamps=False, page_width=None):
    """
    Assign words to columns, reconstruct lines within each column,
    and return the full text reading left-to-right, top-to-bottom per column.
    """
    if not words:
        return ""

    # Filter out timestamp words if requested (before column assignment)
    if strip_timestamps:
        words = filter_timestamp_words(words)
        if not words:
            return ""

    # Assign each word to a column
    col_words = [[] for _ in columns]
    for word in words:
        word_center_x = (word["x0"] + word["x1"]) / 2
        assigned = False
        for i, (col_start, col_end) in enumerate(columns):
            if col_start <= word_center_x < col_end:
                col_words[i].append(word)
                assigned = True
                break
        if not assigned:
            # Fallback: assign to last column
            col_words[-1].append(word)

    lines_text = []
    for col in col_words:
        if not col:
            continue
        # Sort words top-to-bottom, then left-to-right
        col_sorted = sorted(col, key=lambda w: (round(w["top"] / 3), w["x0"]))

        # Group words into lines by proximity of y-coordinate (within 3pt)
        lines = []
        current_line = [col_sorted[0]]
        for word in col_sorted[1:]:
            if abs(word["top"] - current_line[-1]["top"]) <= 3:
                current_line.append(word)
            else:
                lines.append(current_line)
                current_line = [word]
        lines.append(current_line)

        # Join words within each line, then join lines
        for line in lines:
            line_sorted = sorted(line, key=lambda w: w["x0"])
            lines_text.append(" ".join(w["text"] for w in line_sorted))

    return "\n".join(lines_text)


def table_to_text(table):
    """
    Format a pdfplumber table (list of rows, each a list of cell strings)
    as a readable text block. Cells are joined by ' | ', rows by newlines.
    """
    if not table:
        return ""
    rows = []
    for row in table:
        cells = [cell.strip() if cell else "" for cell in row]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def is_transcript_document(pages, sample=10):
    """
    Decide whether a document uses the ``Timestamp | Speaker | Content``
    transcript-table layout by sampling its early pages.

    The layout must be judged for the document as a whole: an individual page
    may hold a single long monologue with too few speaker turns to recognize on
    its own, yet it still shares the same three-column structure and must be read
    row-by-row. The table is declared on the opening pages, so a sample suffices.
    """
    for page in pages[:sample]:
        words = filter_header_words(page.extract_words())
        words = filter_footer_words(words, page.height)
        if words and looks_like_transcript_layout(words, page.width):
            return True
    return False


def process_page(page, strip_timestamps=False, force_rows=False):
    """
    Extract text from a single pdfplumber page.

    Tables are extracted first and rendered as text blocks. Remaining
    words are processed with column detection.

    ``force_rows`` forces row-by-row reconstruction (single column) for pages of
    a known transcript document, so a page with few speaker turns can't be
    mis-split into stacked speaker and content columns.
    """
    parts = []
    page_width = page.width

    # Extract tables
    tables = page.extract_tables()
    table_bboxes = []
    if tables:
        for i, table in enumerate(tables):
            table_text = table_to_text(table)
            if table_text.strip():
                parts.append(table_text)
            # Record table bounding boxes to exclude those words later
            try:
                tbl_obj = page.find_tables()[i]
                table_bboxes.append(tbl_obj.bbox)
            except (IndexError, AttributeError):
                pass

    # Extract words, excluding those inside table bounding boxes
    all_words = page.extract_words()
    if table_bboxes:
        def in_table(word):
            wx0, wy0, wx1, wy1 = word["x0"], word["top"], word["x1"], word["bottom"]
            for tx0, ty0, tx1, ty1 in table_bboxes:
                if wx0 >= tx0 and wy0 >= ty0 and wx1 <= tx1 and wy1 <= ty1:
                    return True
            return False
        words = [w for w in all_words if not in_table(w)]
    else:
        words = all_words

    # Filter out running header/footer boilerplate and the repeated
    # "Timestamp Speaker Content" column header.
    words = filter_header_words(words)
    words = filter_footer_words(words, page.height)
    words = drop_table_header_row(words)

    if words:
        # Transcript-table pages (Timestamp | Speaker | Content) must be read
        # row-by-row. Only fall back to column detection for genuinely
        # multi-column pages (e.g. back-matter subject-heading lists).
        if force_rows or looks_like_transcript_layout(words, page_width):
            columns = [(0, page_width)]
        else:
            columns = detect_columns(words, page_width)
            columns = collapse_speaker_label_columns(words, columns, page_width)
        text = words_to_text(words, columns, strip_timestamps, page_width)
        if text.strip():
            parts.append(text)

    return "\n\n".join(parts)


def page_row_records(page, strip_timestamps=False):
    """
    Reconstruct a transcript page as a list of visual-row records for reflow.

    Each record is ``{"page", "top", "right", "text"}`` where ``right`` is the
    row's right edge (used to tell a wrapped line, which reaches the text-block
    margin, from a paragraph- or list-final line, which ends short).
    """
    words = page.extract_words()
    words = filter_header_words(words)
    words = filter_footer_words(words, page.height)
    words = drop_table_header_row(words)
    if strip_timestamps:
        words = filter_timestamp_words(words)

    records = []
    for row in group_into_rows(words):
        text = " ".join(w["text"] for w in row)
        # Drop end-of-recording markers so they aren't merged into the final
        # speaker turn during reflow.
        if RECORDING_MARKER_PATTERN.match(text.strip()):
            continue
        records.append({
            "page": page.page_number,
            "top": min(w["top"] for w in row),
            "right": max(w["x1"] for w in row),
            "text": text,
        })
    return records


def reflow_records(records, page_width):
    """
    Merge soft-wrapped visual rows into flowing paragraphs.

    A new speaker turn (:data:`TURN_START_PATTERN`) always starts a fresh
    paragraph. Beyond that the rule depends on what kind of block is in progress:

    - Inside a speaker turn, rows keep joining until the next turn label,
      regardless of line length or vertical gaps. Paragraph breaks within a
      turn are merged too, so a speaker's words are condensed fully into one
      paragraph rather than split at every indented paragraph.
    - Outside a turn (front matter, abstract, back-matter lists), a row that
      ended short of the text-block right margin closes the block: a wrapped body
      line runs to the margin, so a short line marks a paragraph or list-item
      end. This keeps the abstract flowing while leaving ragged lists such as the
      subject headings one item per line.

    A turn split across a page boundary is rejoined, because the first row of the
    new page is neither a new turn nor separated by a same-page gap.

    Returns a list of paragraph strings.
    """
    if not records:
        return []

    # Text-block right margin: the widest row reaches it. A row is "full" (a
    # wrapped line) when its right edge is within a tolerance of that margin.
    margin = max(r["right"] for r in records)
    tol = page_width * 0.12

    # Typical single-line leading: the median of small same-page row gaps.
    gaps = sorted(
        b["top"] - a["top"]
        for a, b in zip(records, records[1:])
        if b["page"] == a["page"] and 0 < b["top"] - a["top"] < page_width
    )
    body_leading = gaps[len(gaps) // 2] if gaps else 0

    def same_page_section_break(rec, prev):
        if rec["page"] != prev["page"] or body_leading <= 0:
            return False
        return rec["top"] - prev["top"] > body_leading * 1.7

    paragraphs = []
    current = None
    current_is_turn = False
    prev = None
    for rec in records:
        text = rec["text"]
        is_turn = bool(TURN_START_PATTERN.match(text))

        if current is None or is_turn:
            start_new = True
        elif current_is_turn:
            # Inside a speaker turn: keep merging every wrapped line and every
            # paragraph until the next speaker label, so a speaker's words are
            # condensed fully. Vertical paragraph gaps within a turn are not
            # section breaks — only a new turn ends it.
            start_new = False
        else:
            # Front matter / abstract / lists: a short prior line ends the block.
            start_new = (prev["right"] < margin - tol
                         or same_page_section_break(rec, prev))

        if start_new:
            if current is not None:
                paragraphs.append(current)
            current = text
            current_is_turn = is_turn
        elif (current.endswith("-") and not current.endswith("--")
              and current[-2:-1].isalpha() and text[:1].isalpha()):
            # A single hyphen between two letters at a line break is a word split
            # across lines (e.g. "under-" / "standing"): rejoin with no space. An
            # em-dash ("--") or a dash flanked by spaces is punctuation, not a
            # word split, so it falls through to a normal space-join.
            current += text
        else:
            current += " " + text
        prev = rec

    if current is not None:
        paragraphs.append(current)
    return paragraphs


def strip_front_matter(text):
    """
    Remove the opening metadata block (and Abstract, if present) from a
    transcript, returning only the interview body.

    The block runs from the top of the document to a boundary marker such as
    "Begin Interview", "Begin Recording", or "Transcript Tape 1, Side A"
    (see :data:`FRONT_MATTER_END_PATTERN`). Everything up to and including that
    marker is dropped.

    Stripping happens only when the text before the marker holds at least two
    metadata field labels ("Interviewee:", "Date of Interview:", ...). That guard
    means a transcript with no front matter — one that opens directly on a
    speaker turn — is returned untouched even if a marker-like phrase appears in
    the dialogue.
    """
    if not text:
        return text

    match = FRONT_MATTER_END_PATTERN.search(text)
    if not match:
        return text

    preamble = text[:match.start()]
    if len(FIELD_LABEL_PATTERN.findall(preamble)) < 2:
        return text

    return text[match.end():].lstrip()


def convert_pdf(pdf_path, output_path, strip_timestamps=False, reflow=True,
                remove_front_matter=True):
    """
    Convert a single PDF file to a text file.
    Text flows continuously without page break markers.

    With ``reflow`` (the default), transcript documents have their soft-wrapped
    lines merged into flowing paragraphs (one per speaker turn). Pass
    ``reflow=False`` to preserve every PDF line break instead.

    With ``remove_front_matter`` (the default), the opening metadata block and
    Abstract are dropped so the output begins at the first spoken turn. Pass
    ``remove_front_matter=False`` to keep them.
    """
    pdf_path = Path(pdf_path)
    output_path = Path(output_path)

    with pdfplumber.open(pdf_path) as pdf:
        force_rows = is_transcript_document(pdf.pages)

        if reflow and force_rows:
            records = []
            for page in pdf.pages:
                records.extend(page_row_records(page, strip_timestamps))
            paragraphs = reflow_records(records, pdf.pages[0].width)
            full_text = "\n".join(paragraphs)
        else:
            if reflow:
                print("  Note: reflow applies to transcript documents only; "
                      "writing line-preserving output.")
            page_texts = []
            for page in pdf.pages:
                text = process_page(page, strip_timestamps, force_rows=force_rows)
                if text.strip():
                    page_texts.append(text)
            full_text = "\n\n".join(page_texts)

    if remove_front_matter:
        full_text = strip_front_matter(full_text)

    output_path.write_text(full_text, encoding="utf-8")
    print(f"  Written: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Convert PDFs to clean plain-text files."
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=".",
        help="PDF file or folder containing PDFs (default: current directory)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Destination folder for .txt files (default: same as each PDF)",
    )
    parser.add_argument(
        "--strip-timestamps",
        action="store_true",
        help="Remove timestamp columns (MM:SS format) from output",
    )
    parser.add_argument(
        "--no-reflow",
        action="store_true",
        help="Preserve every PDF line break instead of merging soft-wrapped "
             "lines into flowing paragraphs (reflow is on by default for "
             "transcript documents)",
    )
    parser.add_argument(
        "--keep-front-matter",
        action="store_true",
        help="Keep the opening metadata block and Abstract (removed by default, "
             "so output begins at the first spoken turn)",
    )
    parser.add_argument(
        "-r", "--recursive",
        action="store_true",
        help="Recursively process PDFs in subdirectories",
    )
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    # Determine if input is a single file or a directory
    if input_path.is_file():
        if input_path.suffix.lower() != ".pdf":
            print(f"Error: '{input_path}' is not a PDF file.", file=sys.stderr)
            sys.exit(1)
        pdfs = [input_path]
        print(f"Converting: {input_path.name}")
    elif input_path.is_dir():
        if args.recursive:
            pdfs = sorted(input_path.rglob("*.pdf"))
        else:
            pdfs = sorted(input_path.glob("*.pdf"))
        if not pdfs:
            print(f"No PDF files found in '{input_path}'.")
            sys.exit(0)
        mode = "recursively " if args.recursive else ""
        print(f"Converting {len(pdfs)} PDF(s) {mode}in '{input_path}'...")
    else:
        print(f"Error: '{input_path}' is not a valid file or directory.", file=sys.stderr)
        sys.exit(1)

    for pdf_path in pdfs:
        dest_dir = output_dir if output_dir else pdf_path.parent
        output_path = dest_dir / (pdf_path.stem + ".txt")
        print(f"  Processing: {pdf_path.name}")
        try:
            convert_pdf(pdf_path, output_path, args.strip_timestamps,
                        reflow=not args.no_reflow,
                        remove_front_matter=not args.keep_front_matter)
        except Exception as e:
            print(f"  ERROR processing {pdf_path.name}: {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
