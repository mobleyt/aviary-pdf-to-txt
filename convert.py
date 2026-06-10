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


# Vertical threshold (in points) for header zone at top of page
HEADER_ZONE_HEIGHT = 60

# Pattern for detecting timestamp columns (MM:SS format)
TIMESTAMP_PATTERN = re.compile(r'^\d{1,2}:\d{2}$')

# Pattern for detecting speaker labels (e.g. "EJ:", "BN:", "INTERVIEWER:").
# A single token ending in a colon, short enough to be an initials/name tag.
SPEAKER_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][\w.'\-]{0,19}:$")


def is_header_content(words):
    """
    Determine if a group of words represents header content.

    Headers typically include:
    - A name (often uppercase) with a page number
    - An institution name
    - Short lines at the very top of the page
    """
    if not words:
        return False

    text = " ".join(w["text"] for w in words).strip()

    # Check for page number pattern (standalone number or "Page X")
    if re.match(r"^\d+$", text):
        return True
    if re.match(r"^page\s+\d+$", text, re.IGNORECASE):
        return True

    # Check for name + page number pattern (e.g., "ALSTON 2", "SMITH 15")
    if re.match(r"^[A-Z]+\s+\d+$", text):
        return True

    # Check for short uppercase text (likely a name like "ALSTON")
    if len(text) <= 30 and text.isupper() and text.replace(" ", "").isalpha():
        return True

    # Check for institutional patterns
    institutional_keywords = [
        "research center", "university", "college", "institute",
        "library", "archives", "museum", "foundation"
    ]
    text_lower = text.lower()
    if any(kw in text_lower for kw in institutional_keywords):
        return True

    return False


def filter_header_words(words):
    """
    Remove words that appear to be page header content.

    Filters words in the top header zone that match header patterns.
    """
    if not words:
        return words

    # Separate words into header zone and body
    header_zone_words = [w for w in words if w["top"] < HEADER_ZONE_HEIGHT]
    body_words = [w for w in words if w["top"] >= HEADER_ZONE_HEIGHT]

    if not header_zone_words:
        return words

    # Group header zone words into lines by y-coordinate
    header_zone_words_sorted = sorted(header_zone_words, key=lambda w: w["top"])
    header_lines = []
    current_line = [header_zone_words_sorted[0]]

    for word in header_zone_words_sorted[1:]:
        if abs(word["top"] - current_line[-1]["top"]) <= 5:
            current_line.append(word)
        else:
            header_lines.append(current_line)
            current_line = [word]
    header_lines.append(current_line)

    # Check each line - if it looks like header content, exclude it
    words_to_keep = list(body_words)
    for line_words in header_lines:
        if not is_header_content(line_words):
            words_to_keep.extend(line_words)

    return words_to_keep


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


def process_page(page, strip_timestamps=False):
    """
    Extract text from a single pdfplumber page.

    Tables are extracted first and rendered as text blocks. Remaining
    words are processed with column detection.
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

    # Filter out page header content
    words = filter_header_words(words)

    if words:
        columns = detect_columns(words, page_width)
        columns = collapse_speaker_label_columns(words, columns, page_width)
        text = words_to_text(words, columns, strip_timestamps, page_width)
        if text.strip():
            parts.append(text)

    return "\n\n".join(parts)


def convert_pdf(pdf_path, output_path, strip_timestamps=False):
    """
    Convert a single PDF file to a text file.
    Text flows continuously without page break markers.
    """
    pdf_path = Path(pdf_path)
    output_path = Path(output_path)

    page_texts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = process_page(page, strip_timestamps)
            if text.strip():
                page_texts.append(text)

    full_text = "\n\n".join(page_texts)
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
            convert_pdf(pdf_path, output_path, args.strip_timestamps)
        except Exception as e:
            print(f"  ERROR processing {pdf_path.name}: {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
