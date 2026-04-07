#!/usr/bin/env python3
"""Ingest internal project markdown files into the knowledge base.

Scans ~/projects/ for .md files (max depth 2), skips symlinks and .bak files,
and inserts them into the wiki as pre-compiled articles under the "Internal" category.
Also ingests /root/GLOBAL.md.

Safe to run multiple times -- skips unchanged files (by mtime), updates changed ones.
"""

import os
import re
import json
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

BASE = Path("/opt/knowledge")
RAW_INTERNAL = BASE / "raw" / "internal"
WIKI = BASE / "wiki"
DB_PATH = BASE / "kb.db"
PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", os.path.expanduser("~/projects")))
GLOBAL_MD = Path(os.path.expanduser("~/GLOBAL.md"))


def slugify(title: str) -> str:
    """Convert a title to a URL-safe slug."""
    slug = title.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug.strip('-')[:80]


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def scan_project_files():
    """Scan ~/projects/ for .md files at max depth 2, skipping symlinks and .bak files."""
    files = []

    if not PROJECTS_DIR.exists():
        return files

    for item in sorted(PROJECTS_DIR.iterdir()):
        if not item.is_dir() or item.name == "node_modules":
            continue
        project_name = item.name

        # Depth 1: .md files directly in ~/projects/<project>/
        for md_file in sorted(item.glob("*.md")):
            if md_file.is_symlink():
                continue
            if md_file.name.endswith(".bak"):
                continue
            if md_file.name == "PROJECT.md.bak":
                continue
            files.append((project_name, md_file))

    return files


def ingest_file(project_name, md_path, db):
    """Ingest a single markdown file. Returns (action, title) or None if skipped."""

    source_path = str(md_path)
    mtime = os.path.getmtime(md_path)

    # Build title like "terminal/PROJECT.md" or "GLOBAL.md"
    if project_name:
        title = f"{project_name}/{md_path.name}"
    elif md_path.name.startswith("copilot"):
        title = f"copilot/{md_path.name}"
    else:
        title = f"root/{md_path.name}"

    slug = slugify(f"internal-{title}")

    # Check if entry already exists in entries table
    existing_entry = db.execute(
        "SELECT id, raw_path FROM entries WHERE type = 'internal' AND source = ?",
        (source_path,)
    ).fetchone()

    # Check if article already exists
    existing_article = db.execute(
        "SELECT id, path FROM articles WHERE slug = ?",
        (slug,)
    ).fetchone()

    # If both exist, check mtime to decide whether to update
    if existing_entry and existing_article:
        raw_path = existing_entry["raw_path"]
        if raw_path and Path(raw_path).exists():
            raw_mtime = os.path.getmtime(raw_path)
            if mtime <= raw_mtime:
                return None  # No change, skip

    # Read the content
    try:
        content = md_path.read_text(errors="replace")
    except Exception as e:
        print(f"  ERROR reading {md_path}: {e}")
        return None

    if not content.strip():
        return None

    # Save raw copy
    raw_name = f"{project_name}_{md_path.name}" if project_name else md_path.name
    raw_path = str(RAW_INTERNAL / raw_name)
    Path(raw_path).write_text(content)

    # Build wiki article content (pre-compiled, no LLM needed)
    cats_yaml = json.dumps(["Internal"])
    tags_yaml = json.dumps([project_name] if project_name else [])
    wiki_path = str(WIKI / f"{slug}.md")

    frontmatter = f"""---
title: "{title}"
slug: "{slug}"
categories: {cats_yaml}
project: "{project_name or ''}"
source: "{source_path}"
created: {datetime.now().isoformat()}
---

"""
    Path(wiki_path).write_text(frontmatter + content)

    # Upsert entry in entries table
    if existing_entry:
        db.execute(
            "UPDATE entries SET title = ?, raw_path = ?, status = 'compiled', compiled_at = datetime('now') WHERE id = ?",
            (title, raw_path, existing_entry["id"])
        )
        entry_id = existing_entry["id"]
    else:
        cur = db.execute(
            "INSERT INTO entries (type, source, title, raw_path, status, compiled_at) VALUES ('internal', ?, ?, ?, 'compiled', datetime('now'))",
            (source_path, title, raw_path)
        )
        entry_id = cur.lastrowid

    # Upsert article in articles table
    if existing_article:
        db.execute(
            "UPDATE articles SET title = ?, path = ?, tags = ?, updated_at = datetime('now') WHERE slug = ?",
            (title, wiki_path, tags_yaml, slug)
        )
        action = "updated"
    else:
        db.execute(
            "INSERT INTO articles (slug, title, path, source_ids, tags, updated_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (slug, title, wiki_path, json.dumps([entry_id]), tags_yaml)
        )
        action = "added"

    # Update FTS index
    fts_content = content
    if fts_content.startswith("---"):
        end = fts_content.find("---", 3)
        if end != -1:
            fts_content = fts_content[end+3:].strip()

    db.execute("DELETE FROM articles_fts WHERE slug = ?", (slug,))
    db.execute(
        "INSERT INTO articles_fts (slug, title, content) VALUES (?, ?, ?)",
        (slug, title, fts_content)
    )

    return (action, title)


def run_ingest():
    """Main ingestion function. Returns summary dict."""
    RAW_INTERNAL.mkdir(parents=True, exist_ok=True)
    WIKI.mkdir(parents=True, exist_ok=True)

    project_files = scan_project_files()

    # Also add all root-level MD files
    all_files = []
    root_dir = Path("/root")
    for md_file in sorted(root_dir.glob("*.md")):
        if md_file.is_symlink() or md_file.name.endswith(".bak"):
            continue
        all_files.append(("", md_file))
    all_files.extend(project_files)

    added = []
    updated = []
    skipped = 0
    errors = 0

    db = get_db()
    try:
        for project_name, md_path in all_files:
            try:
                result = ingest_file(project_name, md_path, db)
                if result is None:
                    skipped += 1
                elif result[0] == "added":
                    added.append(result[1])
                elif result[0] == "updated":
                    updated.append(result[1])
            except Exception as e:
                print(f"  ERROR processing {md_path}: {e}")
                errors += 1

        db.commit()
    finally:
        db.close()

    summary = {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "total_scanned": len(all_files),
    }

    print(f"\n=== Internal Ingestion Summary ===")
    print(f"Scanned: {len(all_files)} files")
    print(f"Added:   {len(added)}")
    print(f"Updated: {len(updated)}")
    print(f"Skipped: {skipped} (unchanged)")
    print(f"Errors:  {errors}")

    if added:
        print(f"\nNew articles:")
        for t in added:
            print(f"  + {t}")
    if updated:
        print(f"\nUpdated articles:")
        for t in updated:
            print(f"  ~ {t}")

    return summary


if __name__ == "__main__":
    run_ingest()
