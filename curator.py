"""
DocQA Curator — Homebase Companion
===================================
Runs on your Windows machine. Three jobs:

  1. INGEST    — vision-enhanced PDF → chunks → embeddings → homebase KB
  2. FEEDBACK  — import user feedback exports; an LLM drafts improved golden
                 pairs from the flagged queries + actual document text;
                 you review each (approve / edit / skip); approved pairs
                 merge into the homebase KB
  3. EXPORT    — produce a versioned, distributable kb.json for users

The homebase KB (homebase.json) accumulates everything: vision-processed
docs, your curator golden pairs, and approved user contributions.
Nothing is sent anywhere — all model calls go to local Ollama.

Setup:
  1. Install Ollama: https://ollama.com
  2. ollama pull moondream          (vision — fast, light)
     ollama pull qwen2-vl:7b        (vision — better quality, slower)
     ollama pull qwen2.5:7b         (text — drafts golden pairs)
  3. pip install pymupdf sentence-transformers ollama pillow tqdm

Usage:
  python curator.py ingest   --pdfs ./pdfs --homebase homebase.json
  python curator.py ingest   --pdfs ./pdfs --homebase homebase.json --vision qwen2-vl:7b
  python curator.py feedback --homebase homebase.json --files fb1.json fb2.json
  python curator.py export   --homebase homebase.json --output kb-v2.json
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import fitz  # PyMuPDF
import ollama
from PIL import Image
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

# ── Config ────────────────────────────────────────────────────────────────────
CFG = {
    "vision_model":  "moondream",
    "draft_model":   "qwen2.5:7b",
    "embed_model":   "all-MiniLM-L6-v2",     # must match app
    "chunk_words":   200,
    "chunk_overlap": 40,
    "page_dpi":      150,
    "kb_schema":     "1",                     # must match app CFG.KB_SCHEMA_VER
    "progress_file": ".curator_progress.json",
}

VISION_PROMPT = """You are analyzing a page from an aviation reference document.
Describe all content on this page in clear, structured prose.
Rules:
- Preserve ALL numbers, times, and section references exactly as shown
- For tables: describe each row/column relationship in plain sentences
- For regulatory text: preserve section numbers and exact requirement wording
- For lists: convert to prose preserving every item
- For diagrams/charts: describe what they show and all labeled values
- Completeness is critical — do not summarize or omit data
- Output only the description, no preamble"""

DRAFT_PROMPT = """You are an expert aviation reference curator writing an authoritative answer.

A user asked this question and marked the system's answer as inadequate:

QUESTION: {query}

THE INADEQUATE ANSWER WAS:
{bad_answer}
{issues_block}{correction_block}
RELEVANT SOURCE PASSAGES (marked where the user judged them):
{passages}

Write a complete, accurate answer using ONLY the source passages. Where the user
supplied a correction, treat it as a strong signal about what was wrong, but verify
it against the passages. Include all relevant conditions and edge cases. Reference
specific sections and page numbers. Output only the answer text."""

ISSUE_LABELS = {
    "missing":    "Left out important information",
    "wrong":      "Stated something incorrect",
    "sources":    "Retrieved the wrong passages",
    "incomplete": "Right idea but not enough detail",
    "wording":    "Correct but confusingly worded",
}

# ══════════════════════════════════════════════════════════════════
# SHARED
# ══════════════════════════════════════════════════════════════════
def load_homebase(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {
        "schema":   CFG["kb_schema"],
        "version":  0,
        "exported": None,
        "docs":     [],
        "chunks":   [],
        "golden":   [],
    }


def save_homebase(hb, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hb, f)
    print(f"  Homebase saved → {path}")


def get_embedder():
    print(f"  Loading embedding model ({CFG['embed_model']})…")
    return SentenceTransformer(CFG["embed_model"])


def check_ollama(model):
    try:
        models = [m["name"] for m in ollama.list().get("models", [])]
    except Exception:
        print("✗ Ollama not running. Start it, then retry.")
        sys.exit(1)
    if not any(model.split(":")[0] in m for m in models):
        print(f"✗ Model '{model}' not found. Run: ollama pull {model}")
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════
# INGEST — vision-enhanced PDF processing
# ══════════════════════════════════════════════════════════════════
class Progress:
    def __init__(self, path):
        self.path = path
        self.data = {"completed": {}}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.data = json.load(f)
            except Exception:
                pass

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f)

    def is_done(self, doc_id, page):
        return str(page) in self.data["completed"].get(doc_id, {})

    def mark(self, doc_id, page, chunks):
        self.data["completed"].setdefault(doc_id, {})[str(page)] = chunks
        self.save()

    def chunks_for(self, doc_id):
        pages = self.data["completed"].get(doc_id, {})
        out = []
        for p in sorted(pages, key=int):
            out.extend(pages[p])
        return out


def render_page(page, dpi):
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def describe_page(b64, model):
    r = ollama.chat(model=model, messages=[
        {"role": "user", "content": VISION_PROMPT, "images": [b64]}
    ])
    return r["message"]["content"].strip()


def split_sections(text):
    pat = re.compile(
        r'(?m)^(?:\d+[\.\d]*[A-Z]?[\.\d]*\s+[A-Z]|Section\s+\d+|[A-Z]\.\s+[A-Z]|§\s*\d+)'
    )
    idx = [m.start() for m in pat.finditer(text)]
    if len(idx) < 2:
        return None
    parts = []
    for i, s in enumerate(idx):
        e = idx[i + 1] if i + 1 < len(idx) else len(text)
        parts.append(text[s:e].strip())
    return [p for p in parts if len(p.split()) > 10]


def chunk_text(text, doc_id, doc_name, page):
    def mk(idx, t):
        return {"id": f"{doc_id}_{page}_{idx}", "docId": doc_id, "docName": doc_name,
                "page": page, "text": t, "embedding": None, "source": "vision"}
    secs = split_sections(text)
    if secs:
        return [mk(i, s) for i, s in enumerate(secs)]
    words = text.split()
    out, i, idx = [], 0, 0
    while i < len(words):
        e = min(i + CFG["chunk_words"], len(words))
        out.append(mk(idx, " ".join(words[i:e])))
        idx += 1
        i += CFG["chunk_words"] - CFG["chunk_overlap"]
        if i >= len(words):
            break
    return out


def cmd_ingest(args):
    CFG["vision_model"] = args.vision
    CFG["page_dpi"] = args.dpi
    check_ollama(args.vision)

    pdfs = sorted(Path(args.pdfs).glob("*.pdf"))
    if not pdfs:
        print(f"No PDFs in {args.pdfs}"); sys.exit(1)

    hb = load_homebase(args.homebase)
    existing_names = {d["name"] for d in hb["docs"]}
    embedder = get_embedder()
    progress = Progress(str(Path(args.pdfs) / CFG["progress_file"]))

    print(f"\n  Vision model : {args.vision}")
    print(f"  PDFs         : {len(pdfs)}  ({len(existing_names)} already in homebase)")

    for pdf_path in pdfs:
        name = pdf_path.stem
        if name in existing_names and not args.reprocess:
            print(f"  ↷ Skipping {name} (already in homebase; --reprocess to redo)")
            continue

        doc_id = f"doc_{abs(hash(str(pdf_path)))}"
        pdf = fitz.open(str(pdf_path))
        n = pdf.page_count
        chunks = []
        print(f"\n  {name} ({n} pages)")

        for pg in tqdm(range(1, n + 1), desc="  Pages", unit="pg"):
            if progress.is_done(doc_id, pg):
                continue
            b64 = render_page(pdf[pg - 1], CFG["page_dpi"])
            for attempt in range(3):
                try:
                    desc = describe_page(b64, args.vision)
                    break
                except Exception as e:
                    if attempt == 2:
                        desc = f"[Page {pg} — description unavailable: {e}]"
                    else:
                        time.sleep(2)
            page_chunks = chunk_text(desc, doc_id, name, pg)
            progress.mark(doc_id, pg, page_chunks)

        chunks = progress.chunks_for(doc_id)
        pdf.close()

        # Embed
        texts = [c["text"] for c in chunks]
        for i in tqdm(range(0, len(texts), 32), desc="  Embed", unit="batch"):
            vecs = embedder.encode(texts[i:i+32], normalize_embeddings=True).tolist()
            for j, v in enumerate(vecs):
                chunks[i + j]["embedding"] = v

        # Replace doc in homebase if reprocessing
        hb["docs"]   = [d for d in hb["docs"] if d["name"] != name]
        hb["chunks"] = [c for c in hb["chunks"] if c["docName"] != name]
        hb["docs"].append({
            "id": doc_id, "name": name, "pages": n,
            "chunkCount": len(chunks), "chunksIndexed": len(chunks),
            "size": os.path.getsize(pdf_path),
            "imported": int(time.time() * 1000),
            "status": "indexed", "source": "vision-curator",
        })
        hb["chunks"].extend(chunks)
        save_homebase(hb, args.homebase)

    if os.path.exists(progress.path):
        os.remove(progress.path)
    print(f"\n  ✓ Ingest complete — {len(hb['docs'])} docs, {len(hb['chunks'])} chunks in homebase")


# ══════════════════════════════════════════════════════════════════
# FEEDBACK — review user exports, draft + approve golden pairs
# ══════════════════════════════════════════════════════════════════
def find_chunks(hb, chunk_ids):
    by_id = {c["id"]: c for c in hb["chunks"]}
    return [by_id[i] for i in chunk_ids if i in by_id]


def draft_answer(entry, chunks, model):
    ratings = entry.get("sourceRatings") or {}
    parts = []
    for c in chunks:
        mark = ratings.get(c["id"])
        tag = "  [USER: USEFUL]" if mark == "up" else "  [USER: OFF-TOPIC]" if mark == "down" else ""
        parts.append(f"[{c['docName']} - Page {c['page']}]{tag}\n{c['text']}")
    passages = "\n\n---\n\n".join(parts) or "(no passages found - chunk IDs may be from a different KB version)"

    issues = entry.get("issues") or []
    issues_block = ""
    if issues:
        lines = "\n".join(f"  - {ISSUE_LABELS.get(i, i)}" for i in issues)
        issues_block = f"\nTHE USER TAGGED THESE PROBLEMS:\n{lines}\n"

    correction = entry.get("correctedAnswer") or entry.get("user_suggestion")
    correction_block = ""
    if correction:
        correction_block = f"\nTHE USER'S CORRECTED VERSION (verify before trusting):\n{correction}\n"

    prompt = DRAFT_PROMPT.format(
        query=entry["query"],
        bad_answer=entry.get("answer") or "(retrieval-only, no generated answer)",
        issues_block=issues_block,
        correction_block=correction_block,
        passages=passages,
    )
    r = ollama.chat(model=model, messages=[{"role": "user", "content": prompt}])
    return r["message"]["content"].strip()


def review_loop(items, hb, embedder, draft_model):
    """Interactive terminal review. Returns count approved."""
    approved = 0
    existing_queries = {g["query"].lower().strip() for g in hb["golden"]}

    for n, entry in enumerate(items, 1):
        q = entry["query"]
        if q.lower().strip() in existing_queries:
            print(f"\n[{n}/{len(items)}] ↷ Skipping (golden pair already exists): {q}")
            continue

        print(f"\n{'═'*70}")
        print(f"[{n}/{len(items)}] QUERY: {q}")
        print(f"{'─'*70}")
        if entry.get("answer"):
            print(f"BAD ANSWER:\n{entry['answer'][:400]}{'…' if len(entry.get('answer',''))>400 else ''}")
        if entry.get("issues"):
            print("TAGGED: " + ", ".join(ISSUE_LABELS.get(i, i) for i in entry["issues"]))
        ratings = entry.get("sourceRatings") or {}
        if ratings:
            bad = sum(1 for v in ratings.values() if v == "down")
            good = sum(1 for v in ratings.values() if v == "up")
            print(f"SOURCES: {good} marked useful, {bad} marked off-topic")
        corrected = entry.get("correctedAnswer") or entry.get("user_suggestion")
        if corrected:
            print(f"\nUSER'S CORRECTION:\n{corrected}")

        chunks = find_chunks(hb, entry.get("chunkIds", []))
        print(f"\n  Drafting improved answer with {draft_model} "
              f"({len(chunks)} source passages)… (may take a minute on CPU)")
        try:
            draft = draft_answer(entry, chunks, draft_model)
        except Exception as e:
            print(f"  ⚠ Draft failed: {e}"); continue

        print(f"\n{'─'*70}\nDRAFT GOLDEN ANSWER:\n{draft}\n{'─'*70}")
        choice = input("[a]pprove / [e]dit / [s]kip / [q]uit review > ").strip().lower()

        if choice == "q":
            break
        if choice == "s" or choice == "":
            continue
        final = draft
        if choice == "e":
            print("Enter corrected answer (end with a line containing only 'END'):")
            lines = []
            while True:
                line = input()
                if line.strip() == "END":
                    break
                lines.append(line)
            final = "\n".join(lines).strip() or draft

        vec = embedder.encode([q], normalize_embeddings=True)[0].tolist()
        hb["golden"].append({
            "id": f"gp_{int(time.time()*1000)}_{n}",
            "created": int(time.time() * 1000),
            "query": q,
            "answer": final,
            "authority": "curator",
            "queryEmbedding": vec,
            "sourceFeedbackId": entry.get("id"),
        })
        existing_queries.add(q.lower().strip())
        approved += 1
        print("  ⭐ Saved as curator golden pair")

    return approved


def cmd_feedback(args):
    CFG["draft_model"] = args.draft_model
    check_ollama(args.draft_model)

    hb = load_homebase(args.homebase)
    if not hb["chunks"]:
        print("✗ Homebase is empty — run ingest first so drafts can use document text.")
        sys.exit(1)

    # Merge all feedback files
    flagged, user_golden = [], []
    for fp in args.files:
        with open(fp, encoding="utf-8") as f:
            pkg = json.load(f)
        if pkg.get("schema") != "feedback-1":
            print(f"  ⚠ {fp}: unexpected schema, skipping"); continue
        entries = pkg.get("entries", [])
        golden  = pkg.get("golden", [])
        flagged.extend(e for e in entries
                       if e.get("rating") == "down" or e.get("correctedAnswer") or e.get("issues"))
        user_golden.extend(g for g in golden if g.get("authority") == "pending")
        print(f"  {fp}: {len(entries)} entries, "
              f"{sum(1 for e in entries if e.get('rating')=='down')} flagged, "
              f"{len([g for g in golden if g.get('authority')=='pending'])} user suggestions")

    # Attach user suggestions to their flagged queries
    for e in flagged:
        e["_related_golden"] = [g for g in user_golden
                                if g.get("sourceFeedbackId") == e.get("id")]
        if e["_related_golden"]:
            e["user_suggestion"] = e["_related_golden"][0]["answer"]

    # Dedupe by query
    seen, unique = set(), []
    for e in flagged:
        k = e["query"].lower().strip()
        if k not in seen:
            seen.add(k); unique.append(e)

    # Also surface user suggestions whose feedback entry wasn't flagged/included
    orphan_suggestions = [g for g in user_golden
                          if not any(e.get("id") == g.get("sourceFeedbackId") for e in flagged)]
    for g in orphan_suggestions:
        if g["query"].lower().strip() not in seen:
            unique.append({
                "id": g.get("sourceFeedbackId"),
                "query": g["query"],
                "answer": None,
                "user_suggestion": g["answer"],
                "chunkIds": [],
            })
            seen.add(g["query"].lower().strip())

    # Aggregate: which passages are repeatedly judged off-topic? That points at
    # a retrieval/chunking problem rather than anything the LLM can fix.
    bad_counts, good_counts = {}, {}
    for e in flagged:
        for cid, val in (e.get("sourceRatings") or {}).items():
            (bad_counts if val == "down" else good_counts)[cid] = \
                (bad_counts if val == "down" else good_counts).get(cid, 0) + 1
    if bad_counts:
        by_id = {c["id"]: c for c in hb["chunks"]}
        worst = sorted(bad_counts.items(), key=lambda x: -x[1])[:10]
        print("\n  Passages most often marked off-topic:")
        for cid, n in worst:
            c = by_id.get(cid)
            where = f"{c['docName']} p.{c['page']}" if c else cid
            print(f"    {n}x  {where}")
        print("  (repeat offenders usually mean a chunking or retrieval issue,")
        print("   not something a better answer can fix)")

    # Tag frequency tells you whether failures are retrieval or synthesis.
    tag_counts = {}
    for e in flagged:
        for t in (e.get("issues") or []):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    if tag_counts:
        print("\n  Reported problems:")
        for t, n in sorted(tag_counts.items(), key=lambda x: -x[1]):
            print(f"    {n}x  {ISSUE_LABELS.get(t, t)}")

    if not unique:
        print("\n  No flagged queries or suggestions to review."); return

    print(f"\n  {len(unique)} items to review")
    embedder = get_embedder()
    approved = review_loop(unique, hb, embedder, args.draft_model)

    if approved:
        save_homebase(hb, args.homebase)
    print(f"\n  ✓ Review complete — {approved} golden pairs added "
          f"({len(hb['golden'])} total in homebase)")


# ══════════════════════════════════════════════════════════════════
# EXPORT — distributable KB for users
# ══════════════════════════════════════════════════════════════════
def cmd_export(args):
    hb = load_homebase(args.homebase)
    if not hb["docs"]:
        print("✗ Homebase is empty."); sys.exit(1)

    hb["version"] = hb.get("version", 0) + 1
    pkg = {
        "schema":   CFG["kb_schema"],
        "version":  hb["version"],
        "exported": datetime.now(timezone.utc).isoformat(),
        "docs":     hb["docs"],
        "chunks":   hb["chunks"],
        "golden":   hb["golden"],
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(pkg, f)
    save_homebase(hb, args.homebase)  # persist version bump

    mb = os.path.getsize(args.output) / 1e6
    print(f"\n  ✓ Exported kb v{hb['version']}")
    print(f"    {len(hb['docs'])} docs · {len(hb['chunks'])} chunks · "
          f"{len(hb['golden'])} golden pairs · {mb:.1f} MB")
    print(f"    → {args.output}")
    print(f"\n  Distribute this file. Users import via DocQA → Models → Import KB.")


# ══════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="DocQA Curator — Homebase Companion")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("ingest", help="Vision-process PDFs into homebase")
    p1.add_argument("--pdfs", required=True)
    p1.add_argument("--homebase", default="homebase.json")
    p1.add_argument("--vision", default=CFG["vision_model"])
    p1.add_argument("--dpi", type=int, default=CFG["page_dpi"])
    p1.add_argument("--reprocess", action="store_true",
                    help="Re-process docs already in homebase (for updated PDFs)")

    p2 = sub.add_parser("feedback", help="Review user feedback exports")
    p2.add_argument("--homebase", default="homebase.json")
    p2.add_argument("--files", nargs="+", required=True)
    p2.add_argument("--draft-model", default=CFG["draft_model"])

    p3 = sub.add_parser("export", help="Export distributable kb.json")
    p3.add_argument("--homebase", default="homebase.json")
    p3.add_argument("--output", required=True)

    args = ap.parse_args()
    {"ingest": cmd_ingest, "feedback": cmd_feedback, "export": cmd_export}[args.cmd](args)


if __name__ == "__main__":
    main()
