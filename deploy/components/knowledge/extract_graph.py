#!/usr/bin/env python3
"""Extract knowledge graph from wiki articles using claude -p --model haiku."""
import sqlite3
import json
import subprocess
import os
import re
from pathlib import Path

DB_PATH = os.environ.get("KNOWLEDGE_DB", "/opt/agent-stack/knowledge/kb.db")

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def extract_for_article(title, content_preview):
    """Call haiku to extract entities and relationships from article."""
    prompt = f"""Analyze this wiki article and extract its key concepts and relationships.

Article title: {title}

Content (first 2000 chars):
{content_preview[:2000]}

Output ONLY valid JSON, no other text:
{{
  "category": "best fitting category from: Trading, Strategy, AI Research, People, Technical Architecture, Finance, Marketing, Tools & Frameworks, Geopolitics, Business Ideas",
  "concepts": ["concept1", "concept2", ...],
  "relationships": [
    {{"from": "{title}", "to": "concept1", "type": "covers"}},
    {{"from": "concept1", "to": "concept2", "type": "relates_to"}}
  ]
}}

Rules:
- Extract 3-8 key concepts per article
- Concepts should be reusable across articles (e.g. "Risk Management" not "the risk management section")
- Relationship types: covers, relates_to, part_of, builds_on, contradicts, example_of
- Keep concept names short and canonical
- Output ONLY the JSON object"""

    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", prompt],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PATH": "/root/.local/bin:/usr/local/bin:/usr/bin:/bin"}
        )
        if result.returncode != 0:
            print(f"  ERROR: claude exit {result.returncode}: {result.stderr[:200]}")
            return None

        output = result.stdout.strip()
        # Extract JSON from response (might have markdown code blocks)
        json_match = re.search(r'\{[\s\S]*\}', output)
        if json_match:
            return json.loads(json_match.group())
        return None
    except Exception as e:
        print(f"  ERROR: {e}")
        return None


def main():
    db = get_db()

    # Ensure tables exist
    db.executescript("""
        CREATE TABLE IF NOT EXISTS graph_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT UNIQUE NOT NULL,
            article_id INTEGER,
            node_type TEXT DEFAULT 'concept',
            category TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (article_id) REFERENCES articles(id)
        );
        CREATE TABLE IF NOT EXISTS graph_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relationship TEXT DEFAULT 'relates_to',
            weight REAL DEFAULT 1.0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (source_id) REFERENCES graph_nodes(id),
            FOREIGN KEY (target_id) REFERENCES graph_nodes(id),
            UNIQUE(source_id, target_id, relationship)
        );
    """)

    # Get all articles
    articles = db.execute("SELECT id, slug, title, path, tags FROM articles").fetchall()
    print(f"Processing {len(articles)} articles...")

    # Check which articles already have nodes
    existing = set()
    for row in db.execute("SELECT label FROM graph_nodes WHERE article_id IS NOT NULL"):
        existing.add(row["label"])

    new_nodes = 0
    new_edges = 0
    processed = 0

    for article in articles:
        title = article["title"]

        # Skip if already processed
        if title in existing:
            continue

        path = Path(article["path"]) if article["path"] else None
        if not path or not path.exists():
            continue

        content = path.read_text()
        # Strip frontmatter
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                content = content[end+3:].strip()
        elif content.startswith("Categories:"):
            content = content.split("\n", 2)[-1].strip()

        if len(content) < 100:
            continue

        processed += 1
        print(f"[{processed}] Extracting: {title}...")

        data = extract_for_article(title, content)
        if not data:
            print(f"  SKIP: no valid JSON returned")
            continue

        category = data.get("category", "Uncategorized")

        # Ensure article node exists
        db.execute(
            "INSERT OR IGNORE INTO graph_nodes (label, article_id, node_type, category) VALUES (?, ?, 'article', ?)",
            (title, article["id"], category)
        )
        new_nodes += 1

        # Add concept nodes
        for concept in data.get("concepts", []):
            concept = concept.strip()
            if not concept or len(concept) > 100:
                continue
            db.execute(
                "INSERT OR IGNORE INTO graph_nodes (label, node_type, category) VALUES (?, 'concept', ?)",
                (concept, category)
            )

        # Add edges
        for rel in data.get("relationships", []):
            from_label = rel.get("from", "").strip()
            to_label = rel.get("to", "").strip()
            rel_type = rel.get("type", "relates_to")
            if not from_label or not to_label:
                continue

            # Get node IDs
            src = db.execute("SELECT id FROM graph_nodes WHERE label = ?", (from_label,)).fetchone()
            tgt = db.execute("SELECT id FROM graph_nodes WHERE label = ?", (to_label,)).fetchone()
            if src and tgt:
                try:
                    db.execute(
                        "INSERT OR IGNORE INTO graph_edges (source_id, target_id, relationship) VALUES (?, ?, ?)",
                        (src["id"], tgt["id"], rel_type)
                    )
                    new_edges += 1
                except Exception:
                    pass

        db.commit()

        # Brief pause to avoid rate limiting
        import time
        time.sleep(1)

    print(f"\nDone. Processed {processed} articles. Added {new_nodes} nodes, {new_edges} edges.")
    total_nodes = db.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    total_edges = db.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    print(f"Total graph: {total_nodes} nodes, {total_edges} edges.")
    db.close()


if __name__ == "__main__":
    main()
