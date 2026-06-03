# PDF to Text Converter

Converts PDF files to plain text. Handles multi-column layouts and tables common in oral history transcripts.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python convert.py [input] [--output-dir OUTPUT_DIR]
```

`input` can be either a single PDF file or a folder containing PDFs.

**Convert a single PDF:**
```bash
python convert.py document.pdf
```

**Convert all PDFs in the current directory:**
```bash
python convert.py
```

**Convert all PDFs in a specific folder:**
```bash
python convert.py /path/to/pdfs
```

**Write output files to a different folder:**
```bash
python convert.py /path/to/pdfs --output-dir /path/to/output
python convert.py document.pdf --output-dir /path/to/output
```

## Output

Each PDF produces a `.txt` file with the same base name (e.g. `alston-legacy.pdf` → `alston-legacy.txt`). By default the `.txt` file is written alongside the PDF. Use `--output-dir` to collect all output in one place.


## How It Works

- **Tables** are extracted and rendered as `cell | cell` rows before the surrounding text.
- **Multi-column text** is detected by finding horizontal gaps between words. Columns are read left-to-right, each top-to-bottom, so the output reads naturally even when the source PDF uses a two-column layout.
- **Single-column pages** are passed through as-is.
