"""
build_index.py
Reads all notes/*.md, splits into chunks, embeds with Gemini,
and writes public/index.json for the Cloudflare Worker to use.

Runs automatically via .github/workflows/build-index.yml on every
push to notes/, or manually via Actions → Build RAG Index → Run workflow.
"""

import os
import json
import hashlib
import pathlib
import google.generativeai as genai

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
EMBED_MODEL    = "models/text-embedding-004"
CHUNK_WORDS    = 400   # words per chunk
OVERLAP_WORDS  = 60    # overlap between chunks

genai.configure(api_key=GEMINI_API_KEY)

CACHE_FILE = ".rag-cache.json"


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


def chunk_text(text, size=CHUNK_WORDS, overlap=OVERLAP_WORDS):
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i:i+size])
        chunks.append(chunk)
        if i + size >= len(words):
            break
        i += size - overlap
    return chunks


def embed_single(text):
    result = genai.embed_content(
        model=EMBED_MODEL,
        content=text,
        task_type="retrieval_document",
    )
    return result["embedding"]


def main():
    notes_dir = pathlib.Path("notes")
    cache = load_cache()
    all_chunks = []

    md_files = sorted(notes_dir.glob("*.md"))
    if not md_files:
        print("No notes found — nothing to index.")
        return

    for md_file in md_files:
        date = md_file.stem
        text = md_file.read_text()
        file_hash = hashlib.md5(text.encode()).hexdigest()

        if date in cache and cache[date].get("hash") == file_hash:
            print(f"  [cache] {date} — unchanged, reusing embeddings")
            all_chunks.extend(cache[date]["chunks"])
            continue

        print(f"  [embed] {date} — new or changed")
        chunks = chunk_text(text)
        chunk_objs = []
        for chunk in chunks:
            embedding = embed_single(chunk)
            chunk_objs.append({
                "date": date,
                "text": chunk,
                "embedding": embedding,
            })

        cache[date] = {"hash": file_hash, "chunks": chunk_objs}
        all_chunks.extend(chunk_objs)

    save_cache(cache)

    os.makedirs("public", exist_ok=True)
    out_path = "public/index.json"
    with open(out_path, "w") as f:
        json.dump(all_chunks, f)

    print(f"\nDone — {len(all_chunks)} chunks from {len(md_files)} notes → {out_path}")


if __name__ == "__main__":
    main()
