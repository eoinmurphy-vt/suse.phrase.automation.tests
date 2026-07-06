import os
import re
import datetime
import chardet
from pathlib import Path

# --- CONFIGURATION ---
TARGET_PRODUCT = os.getenv('SPECIFIC_PRODUCT', '').strip()
SRC_DIR = os.getenv("SRC_DIR", "translated")
DST_DIR = os.getenv("DST_DIR", "final")
LOG_DIR = "logs"

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DST_DIR, exist_ok=True)

timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
run_id = os.getenv("GITHUB_RUN_ID", "local")
LOG_FILE = f"{LOG_DIR}/postprocess_log_{timestamp}_{run_id}.txt"

stats = {"processed": 0, "errors": 0, "skipped": 0, "cleaned": 0}

def log(msg):
    print(msg)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

def detect_and_convert_to_utf8(file_path):
    with open(file_path, "rb") as f:
        raw = f.read()
    info = chardet.detect(raw)
    encoding = info.get("encoding") or "utf-8"
    try:
        text = raw.decode(encoding)
    except Exception:
        stats["errors"] += 1
        log(f"⚠ Decode failed for {file_path}, forcing UTF-8")
        text = raw.decode("utf-8", errors="replace")
    return text

def cleanup_text(text, lang_code=""):
    before = text

    # --- STAGE 0: GLOBAL PHRASE TM CLEANUP (BACKTICKS) ---
    # Runs globally. This catches Phrase TM's backtick garbage
    text = re.sub(r'&quot;`\+([^`\n]+)\+`&quot;', r'"`\1`"', text)
    text = re.sub(r'&apos;`\+([^`\n]+)\+`&apos;', r"'`\1`'", text)
    text = re.sub(r'`\+([^`\n]+)\+`', r'`\1`', text)

    # --- STAGE 1: PROTECT MULTILINE CODE BLOCKS ---
    # Protects Tables, Certs, and JSON logs from the aggressive cleanup below.
    protected_multi = []

    def protect_multi(match):
        protected_multi.append(match.group(0))
        return f"__MULTI_BLOCK_{len(protected_multi) - 1}__"

    text = re.sub(r'^(-{4,}|\.{4,})$.*?^\1$', protect_multi, text, flags=re.MULTILINE | re.DOTALL)

    # --- STAGE 2: PROTECT KUBERNETES VERSIONS ---
    # Protects semantic versions like "v1.24.8+rke2r1" from the aggressive cleanup.
    protected_tech = []

    def protect_tech(match):
        protected_tech.append(match.group(0))
        return f"__TECH_BLOCK_{len(protected_tech) - 1}__"

    text = re.sub(r'\bv[0-9]+(?:\.[0-9]+)+[\w\-\+]*', protect_tech, text)

    # --- STAGE 3: AGGRESSIVE PHRASE TM CLEANUP ---
    # Now safe to run because Tables, Certs, and Versions are successfully hidden!
    text = re.sub(r'\[literal\]#([^#]+)#', r'[monospaced]#\1#', text, flags=re.IGNORECASE)
    text = re.sub(r'\+([A-Za-z0-9/_\.-]+)\+', r'\1', text)
    text = re.sub(r'\\\^\[(.*?)\]\^', r'^[\1]^', text)

    # --- STAGE 4: PROTECT INLINE CODE BLOCKS ---
    # Protects newly cleaned backticks so the typography rules below don't break code operators (!=).
    protected_inline = []

    def protect_inline(match):
        protected_inline.append(match.group(0))
        return f"__INLINE_BLOCK_{len(protected_inline) - 1}__"

    text = re.sub(r'`[^`\n]+`', protect_inline, text)

    # --- STAGE 5: TYPOGRAPHY & CJK CLEANUP ---
    # Fix broken CJK links
    text = re.sub(r'([^\s\[\(\<"\'\n:`\\?=&*_])(https?://)', r'\1 \2', text)

    # Apply French typography rules ONLY if the target language is French
    if lang_code.startswith("fr"):
        text = re.sub(r'([^\s]) ([:;?!])(?=\s|$|["\'»)])(?!:|=)', r'\1{nbsp}\2', text)

    # --- STAGE 6: RESTORE ALL BLOCKS ---
    # Unpack everything in the reverse order
    for i, block in enumerate(protected_inline):
        text = text.replace(f"__INLINE_BLOCK_{i}__", block)

    for i, block in enumerate(protected_tech):
        text = text.replace(f"__TECH_BLOCK_{i}__", block)

    for i, block in enumerate(protected_multi):
        text = text.replace(f"__MULTI_BLOCK_{i}__", block)

    # Final whitespace cleanup
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r'[ ]{2,}$', '', text, flags=re.MULTILINE)

    if text != before: stats["cleaned"] += 1
    return text

def get_target_lang(phrase_folder):
    """
    Determines the target language code.
    - Standard: de_de -> de
    - Chinese/Exceptions: zh_cn -> zh-cn
    """
    folder = phrase_folder.lower()
    
    # EXCEPTIONS: Languages where Region is required
    exceptions = {
        "zh_sg": "zh-cn",
        "zh_hk": "zh-tw"
    }
    
    if folder in exceptions:
        return exceptions[folder]
        
    # DEFAULT: Take the first part (fr_fr -> fr)
    return folder.split("_")[0]

def map_output_path(src_path: str, rel: str) -> str:
    """
    Input structure from Phrase: translated/fr_fr/suse-repo-b/path/to/file.adoc
    Target structure:            final/suse-repo-b/fr/path/to/file.adoc
    """
    parts = rel.split(os.sep)
    
    if len(parts) < 3: 
        log(f"⚠ Skipping invalid path depth: {rel}")
        return None

    # 1. Parse Structure
    lang_folder = parts[0]   # 'fr_fr' or 'zh_cn'
    repo_id = parts[1]       # 'suse-repo-b'
    
    # --- FIXED CHECK ---
    # If a specific product was typed in, and this file doesn't match it, return None to skip it!
    if TARGET_PRODUCT and repo_id != TARGET_PRODUCT:
        return None
    # -------------------
    
    # 2. Get Smart Language Code
    lang_code = get_target_lang(lang_folder)

    # 3. Get Content Path
    remainder_path = os.path.join(*parts[2:])
    
    # 4. Construct Path: final / REPO_ID / LANG_CODE / CONTENT
    return os.path.join(DST_DIR, repo_id, lang_code, remainder_path)

# --- MAIN ---
with open(LOG_FILE, "w", encoding="utf-8") as f:
    f.write(f"Postprocess started: {datetime.datetime.now()}\n\n")

# FAIL-SAFE LOGIC
force_all = os.getenv("FORCE_ALL", "false").lower() == "true"
files_to_process = []

if force_all:
    # Manual override requested: bulldoze the whole directory
    files_to_process = [str(p) for p in Path(SRC_DIR).rglob("*.adoc")]
    log("⚠ MANUAL OVERRIDE TRIGGERED: Processing ALL files.")
else:
    # Delta processing: Only read the files modified by Git
    try:
        with open("changed_files.txt", "r") as f:
            for line in f:
                filepath = line.strip()
                # Ensure the changed file is an AsciiDoc file and exists in our source directory
                if filepath.endswith('.adoc') and filepath.startswith(SRC_DIR) and os.path.exists(filepath):
                    files_to_process.append(filepath)
    except FileNotFoundError:
        log("No changed_files.txt manifest found. Skipping delta processing.")

# Loop through smart list instead of the whole drive
for src_path in files_to_process:
    rel = os.path.relpath(src_path, SRC_DIR)
    dst_path = map_output_path(src_path, rel)

    if not dst_path:
        stats["skipped"] += 1
        continue

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    text = detect_and_convert_to_utf8(src_path)

    # Use your smart function to get the language code (e.g., 'fr', 'zh-cn')
    lang_folder = rel.split(os.sep)[0]
    smart_lang_code = get_target_lang(lang_folder)

    # Pass the smart language code into the cleanup function
    text = cleanup_text(text, smart_lang_code)

    with open(dst_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)

    stats["processed"] += 1
    log(f"✓ Processed: {rel} -> {dst_path}")

log(f"\n=== Test Complete ===")
log(f"Summary: Processed={stats['processed']}, Cleaned={stats['cleaned']}, Errors={stats['errors']}, Skipped={stats['skipped']}")
