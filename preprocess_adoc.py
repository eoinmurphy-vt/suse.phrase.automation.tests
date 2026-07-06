import os
import sys
import re
import chardet
import datetime
from pathlib import Path

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
run_id = os.getenv("GITHUB_RUN_ID", "local")
LOG_FILE = f"{LOG_DIR}/preprocess_log_{timestamp}_{run_id}.txt"

stats = {"processed": 0, "errors": 0, "normalized": 0}


def log(msg: str):
    print(msg)
    with open(LOG_FILE, "a", encoding="utf-8") as logf:
        logf.write(msg + "\n")


def detect_and_read_utf8(path):
    """Detect encoding and return text decoded as UTF-8. Tries strict UTF-8 first."""
    
    # 1. ATTEMPT STRICT UTF-8 FIRST (Prevents mojibake corruption)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
            return text
    except UnicodeDecodeError:
        pass # If the file genuinely isn't UTF-8, continue to fallback

    # 2. FALLBACK TO CHARDET (Only if strict UTF-8 fails)
    with open(path, "rb") as f:
        raw = f.read()

    info = chardet.detect(raw)
    encoding = info.get("encoding") or "utf-8"
    confidence = info.get("confidence", 0)

    try:
        text = raw.decode(encoding)
        log(f"⚠ Warning: {path} read using fallback encoding {encoding} ({confidence:.2f})")
    except Exception:
        log(f"⚠ Decode failed ({encoding}), forcing UTF-8 replacement")
        stats["errors"] += 1
        text = raw.decode("utf-8", errors="replace")

    return text


def normalize_ascii(text: str) -> str:
    """Basic cleanup: normalize whitespace, fix accidental CRLF, sanitize soft breaks."""
    before = text

    text = text.replace("\r\n", "\n").replace("\r", "\n")      # Force LF only
    text = re.sub(r'\u00A0', ' ', text)                      # Non-breaking space
    text = re.sub(r'\t', '    ', text)                       # Tabs → spaces
    text = re.sub(r'[ ]{2,}$', '', text, flags=re.MULTILINE) # Trailing spaces

    if text != before:
        stats["normalized"] += 1

    return text


def preprocess_content(text: str) -> str:
    """
    Transformations for translation prep.
    Includes robust literal monospace conversion that handles artifacts, 
    preserves spacing, enforces strict single-line matching, and protects code blocks.
    """
    
    # --- STEP 1: PROTECT CODE BLOCKS ---
    protected_blocks = []
    def protect(match):
        protected_blocks.append(match.group(0))
        return f"__PROTECTED_BLOCK_{len(protected_blocks)-1}__"
        
    # Finds anything between 4 dashes (----) or 4 dots (....)
    text = re.sub(r'^(-{4,}|\.{4,})$.*?^\1$', protect, text, flags=re.MULTILINE | re.DOTALL)


    # --- STEP 2: SANITIZE (Cleanup previous errors) ---
    # Remove the specific artifacts "+` and `+" (and optional spaces attached to the +)
    sanitize_start = re.compile(r'([\"\'“‘])\+[ \t]*(\`)')
    text = sanitize_start.sub(r'\1\2', text)

    sanitize_end = re.compile(r'(\`)[ \t]*\+([\"\'”’])')
    text = sanitize_end.sub(r'\1\2', text)


    # --- STEP 3: APPLY FORMATTING (Strict Single-Line, Space-Safe) ---
    pattern = re.compile(
        r'([\"\'\u201c\u201d\u2018\u2019])?'       # 1. Start Quote
        r'(?:\+*)'                                 # 2. Pre-Junk (PLUS ONLY)
        r'\`([^\`\n]+)\`'                          # 3. Backtick Block (No \n)
        r'(?:\+*)'                                 # 4. Post-Junk (PLUS ONLY)
        r'([\"\'\u201c\u201d\u2018\u2019])?'       # 5. End Quote
    )

    def replacement(match):
        start_quote = match.group(1)
        content = match.group(2)
        end_quote = match.group(3)
        
        # Helper: Ensure inner content is wrapped in +...+
        clean_content = content
        
        # Safety check: Only strip wrappers if length > 1. 
        # Prevents stripping a single '+' which would result in '++'.
        if len(clean_content) > 1 and clean_content.startswith('+') and clean_content.endswith('+'):
            clean_content = clean_content[1:-1]
        
        final_inner = f'+{clean_content}+'

        # Context Logic
        is_double = start_quote in ['"', '“', '”'] and end_quote in ['"', '“', '”']
        is_single = start_quote in ["'", "‘", "’"] and end_quote in ["'", "‘", "’"]
        
        if is_double:
            return f'&quot;`{final_inner}`&quot;'
        elif is_single:
            return f'&apos;`{final_inner}`&apos;'
        else:
            # Preserve surrounding text (including spaces) if quotes didn't match
            prefix = start_quote if start_quote else ""
            suffix = end_quote if end_quote else ""
            return f'{prefix}`{final_inner}`{suffix}'

    text = pattern.sub(replacement, text)

    # --- STEP 4: OTHER TRANSFORMATIONS ---
    text = re.sub(r'\[monospaced\]#([^#]+)#', r'[literal]#\1#', text)

    # --- STEP 5: RESTORE CODE BLOCKS ---
    for i, block in enumerate(protected_blocks):
        text = text.replace(f"__PROTECTED_BLOCK_{i}__", block)

    return text


def main():
    if len(sys.argv) < 2:
        log("ERROR: No input file provided.")
        sys.exit(1)

    src_file = sys.argv[1]
    if not os.path.isfile(src_file):
        log(f"ERROR: File does not exist: {src_file}")
        sys.exit(1)

    log(f"Starting preprocess: {src_file}")

    # Compute output path (mirror directory structure)
    rel_path = os.path.relpath(src_file, "source")
    out_path = os.path.join("processed", rel_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    text = detect_and_read_utf8(src_file)
    text = normalize_ascii(text)
    text = preprocess_content(text)

    # Output is already strictly UTF-8 here
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    stats["processed"] += 1
    log(f"Saved processed file → {out_path}")

    # Summary
    log("\nSummary:")
    log(f"  ✅ Processed: {stats['processed']}")
    log(f"  ⚠️ Encoding issues fixed: {stats['errors']}")
    log(f"  ✅ Normalized files: {stats['normalized']}")

    log(f"\nCompleted at {datetime.datetime.now()}")


if __name__ == "__main__":
    main()