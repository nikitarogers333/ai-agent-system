#!/usr/bin/env python3
"""Knowledge Base server -- Karpathy-style LLM wiki builder."""

import os
import re
import json
import sqlite3
import asyncio
import subprocess
import time
import threading
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager
from queue import Queue

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

import hashlib
import urllib.request
import urllib.parse

from ingest import ingest_url, ingest_file as do_ingest_file, detect_type
from compile import compile_entries, lint_wiki
from ingest_internal import run_ingest as run_internal_ingest
from arxiv_feed import fetch_arxiv_papers, get_arxiv_papers, get_arxiv_paper, save_paper_to_wiki, compute_arxiv_features, get_similar_papers

BASE = Path(os.environ.get("KNOWLEDGE_DIR", "/opt/agent-stack/knowledge"))
RAW = BASE / "raw"
WIKI = BASE / "wiki"
UPLOADS = BASE / "uploads"
DB_PATH = BASE / "kb.db"


def strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter (---...---) from markdown content."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3:].strip()
    return text

app = FastAPI(title="Knowledge Base")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                source TEXT NOT NULL,
                title TEXT,
                raw_path TEXT,
                status TEXT DEFAULT 'pending',
                error TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                compiled_at TEXT
            );
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                path TEXT NOT NULL,
                source_ids TEXT DEFAULT '[]',
                tags TEXT DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
                slug, title, content, tokenize='porter'
            );
            CREATE TABLE IF NOT EXISTS council_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT NOT NULL,
                content TEXT NOT NULL,
                thread_id INTEGER DEFAULT NULL,
                session TEXT DEFAULT NULL,
                source TEXT DEFAULT 'chat',
                loop_id TEXT DEFAULT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
        """)
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
        # Migrate: add session/source/loop_id columns if missing
        try:
            db.execute("ALTER TABLE council_messages ADD COLUMN session TEXT DEFAULT NULL")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE council_messages ADD COLUMN source TEXT DEFAULT 'chat'")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE council_messages ADD COLUMN loop_id TEXT DEFAULT NULL")
        except Exception:
            pass
        # Rebuild FTS index on startup
        rebuild_fts(db)

def rebuild_fts(db):
    """Rebuild the FTS index from wiki article files."""
    db.execute("DELETE FROM articles_fts")
    rows = db.execute("SELECT slug, title, path FROM articles").fetchall()
    for row in rows:
        path = Path(row["path"])
        if not path.exists():
            continue
        content = strip_frontmatter(path.read_text())
        db.execute(
            "INSERT OR REPLACE INTO articles_fts (slug, title, content) VALUES (?, ?, ?)",
            (row["slug"], row["title"], content)
        )
    db.commit()


def index_article_fts(db, slug: str, title: str, path: str):
    """Index a single article in FTS."""
    content = ""
    if Path(path).exists():
        content = strip_frontmatter(Path(path).read_text())
    # Delete old entry if exists, then insert
    db.execute("DELETE FROM articles_fts WHERE slug = ?", (slug,))
    db.execute(
        "INSERT INTO articles_fts (slug, title, content) VALUES (?, ?, ?)",
        (slug, title, content)
    )


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Compilation state (simple in-memory tracking)
# ---------------------------------------------------------------------------

compile_status = {"running": False, "progress": "", "last_run": None, "error": None}

# ---------------------------------------------------------------------------
# Routes -- UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(BASE / "public" / "index.html")

# ---------------------------------------------------------------------------
# Routes -- Entries (raw materials)
# ---------------------------------------------------------------------------

@app.post("/api/submit")
async def submit_url(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    url = body.get("url", "").strip()
    project = body.get("project", "").strip()
    if not url:
        raise HTTPException(400, "No URL provided")

    entry_type = detect_type(url)

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO entries (type, source, status) VALUES (?, ?, 'pending')",
            (entry_type, url)
        )
        entry_id = cur.lastrowid

    # Run ingestion in background
    background_tasks.add_task(run_ingestion, entry_id, url, entry_type, project)

    return {"id": entry_id, "type": entry_type, "source": url, "status": "pending", "project": project}


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    # Save file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = file.filename.replace(" ", "_").replace("/", "_")
    save_name = f"{timestamp}_{safe_name}"
    save_path = UPLOADS / save_name
    content = await file.read()
    save_path.write_bytes(content)

    # Determine type
    ext = Path(file.filename).suffix.lower()
    if ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
        entry_type = "screenshot"
    elif ext == '.pdf':
        entry_type = "pdf"
    elif ext in ('.md', '.txt'):
        entry_type = "text"
    else:
        entry_type = "file"

    # For text/markdown files, copy to raw directly
    raw_path = None
    status = "pending"
    title = file.filename

    if ext in ('.md', '.txt'):
        raw_name = f"file_{timestamp}.md"
        raw_path = str(RAW / raw_name)
        text_content = content.decode("utf-8", errors="replace")
        md = f"---\ntitle: {file.filename}\ntype: file\ndate: {datetime.now().isoformat()}\n---\n\n{text_content}"
        Path(raw_path).write_text(md)
        status = "ingested"
    elif entry_type == "screenshot":
        # Save a raw markdown referencing the image
        raw_name = f"screenshot_{timestamp}.md"
        raw_path = str(RAW / raw_name)
        md = f"---\ntitle: {file.filename}\ntype: screenshot\ndate: {datetime.now().isoformat()}\nimage: {save_path}\n---\n\n![{file.filename}]({save_path})\n\n*Uploaded screenshot. Content needs vision analysis during compilation.*"
        Path(raw_path).write_text(md)
        status = "ingested"

    with get_db() as db:
        db.execute(
            "INSERT INTO entries (type, source, title, raw_path, status) VALUES (?, ?, ?, ?, ?)",
            (entry_type, str(save_path), title, raw_path, status)
        )

    return {"status": "ok", "filename": save_name, "type": entry_type}


@app.get("/api/entries")
async def list_entries():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM entries ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/entries/{entry_id}")
async def get_entry(entry_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Entry not found")
    result = dict(row)
    # Include raw content if available
    if result.get("raw_path") and Path(result["raw_path"]).exists():
        result["content"] = Path(result["raw_path"]).read_text()
    return result


@app.delete("/api/entries/{entry_id}")
async def delete_entry(entry_id: int):
    with get_db() as db:
        row = db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Entry not found")
        # Delete raw file
        if row["raw_path"] and Path(row["raw_path"]).exists():
            Path(row["raw_path"]).unlink()
        db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Routes -- Wiki
# ---------------------------------------------------------------------------

@app.get("/api/wiki")
async def list_articles():
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM articles ORDER BY updated_at DESC"
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        # Include first 200 chars as preview
        if Path(d["path"]).exists():
            content = strip_frontmatter(Path(d["path"]).read_text())
            # Clean preview: strip markdown headers and extra whitespace
            preview = re.sub(r'^#{1,4}\s+.*$', '', content, flags=re.MULTILINE).strip()
            preview = re.sub(r'\*\*(.+?)\*\*', r'\1', preview)  # strip bold
            preview = re.sub(r'\[\[(.+?)\]\]', r'\1', preview)  # strip wiki links
            preview = re.sub(r'\n+', ' ', preview)  # collapse newlines
            d["preview"] = preview[:200]
        results.append(d)
    return results


@app.get("/api/wiki/{slug}")
async def get_article(slug: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM articles WHERE slug = ?", (slug,)).fetchone()
    if not row:
        raise HTTPException(404, "Article not found")
    result = dict(row)
    if Path(result["path"]).exists():
        content = strip_frontmatter(Path(result["path"]).read_text())
        result["content"] = content
    return result


@app.put("/api/wiki/{slug}")
async def update_article(slug: str, body: dict):
    content = body.get("content", "")
    with get_db() as db:
        row = db.execute("SELECT * FROM articles WHERE slug = ?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Article not found")
        wiki_path = row["path"]
        # For internal articles, also update the source file
        if slug.startswith("internal-"):
            entry = db.execute("SELECT source FROM entries WHERE type = 'internal' AND id IN (SELECT json_each.value FROM articles, json_each(articles.source_ids) WHERE articles.slug = ?)", (slug,)).fetchone()
            if entry and entry["source"] and Path(entry["source"]).exists():
                Path(entry["source"]).write_text(content)
        # Update wiki file (with frontmatter)
        frontmatter = f"---\ntitle: \"{row['title']}\"\nslug: \"{slug}\"\n---\n\n"
        Path(wiki_path).write_text(frontmatter + content)
        # Update FTS
        db.execute("DELETE FROM articles_fts WHERE slug = ?", (slug,))
        db.execute("INSERT INTO articles_fts (slug, title, content) VALUES (?, ?, ?)", (slug, row["title"], content))
        db.execute("UPDATE articles SET updated_at = datetime('now') WHERE slug = ?", (slug,))
    return {"ok": True}


@app.put("/api/wiki/{slug}/tags")
async def update_article_tags(slug: str, body: dict):
    """Set project tags on an article. body: {"tags": ["trading", "playbook"]}"""
    tags = body.get("tags", [])
    if not isinstance(tags, list):
        raise HTTPException(400, "tags must be a list")
    with get_db() as db:
        row = db.execute("SELECT * FROM articles WHERE slug = ?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Article not found")
        db.execute("UPDATE articles SET tags = ?, updated_at = datetime('now') WHERE slug = ?",
                   (json.dumps(tags), slug))
    return {"ok": True, "tags": tags}


@app.get("/api/projects")
async def list_projects():
    """Return all project names from /root/projects/ for tag selection."""
    projects_dir = "/root/projects"
    try:
        projects = sorted([d for d in os.listdir(projects_dir)
                          if os.path.isdir(os.path.join(projects_dir, d)) and not d.startswith('.')])
    except Exception:
        projects = []
    return {"projects": projects}


@app.delete("/api/wiki/{slug}")
async def delete_article(slug: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM articles WHERE slug = ?", (slug,)).fetchone()
        if not row:
            raise HTTPException(404, "Article not found")
        if Path(row["path"]).exists():
            Path(row["path"]).unlink()
        db.execute("DELETE FROM articles WHERE slug = ?", (slug,))
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Routes -- Compile
# ---------------------------------------------------------------------------

@app.post("/api/compile")
async def trigger_compile(request: Request, background_tasks: BackgroundTasks):
    if compile_status["running"]:
        raise HTTPException(409, "Compilation already in progress")

    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    entry_ids = body.get("entry_ids")  # None means compile all uncompiled

    background_tasks.add_task(run_compilation, entry_ids)
    return {"status": "started"}


@app.get("/api/compile/status")
async def get_compile_status():
    return compile_status


@app.post("/api/lint")
async def trigger_lint(background_tasks: BackgroundTasks):
    if compile_status["running"]:
        raise HTTPException(409, "Compilation already in progress")
    background_tasks.add_task(run_lint)
    return {"status": "started"}


# ---------------------------------------------------------------------------
# Routes -- Search
# ---------------------------------------------------------------------------

@app.get("/api/search")
async def search_wiki(q: str = "", limit: int = 20):
    if not q or len(q) < 2:
        raise HTTPException(400, "Query too short")
    with get_db() as db:
        # FTS5 search with snippet
        try:
            rows = db.execute("""
                SELECT slug, title, snippet(articles_fts, 2, '<b>', '</b>', '...', 30) as snippet,
                       rank
                FROM articles_fts
                WHERE articles_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (q, limit)).fetchall()
        except Exception:
            # Fallback: wrap in quotes for literal search
            rows = db.execute("""
                SELECT slug, title, snippet(articles_fts, 2, '<b>', '</b>', '...', 30) as snippet,
                       rank
                FROM articles_fts
                WHERE articles_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (f'"{q}"', limit)).fetchall()
    return [{"slug": r["slug"], "title": r["title"], "snippet": r["snippet"]} for r in rows]


@app.get("/api/ask")
async def ask_wiki(q: str = "", limit: int = 5):
    """Return full article content for the top matches. Designed for Claude to query programmatically."""
    if not q or len(q) < 2:
        raise HTTPException(400, "Query too short")
    with get_db() as db:
        try:
            rows = db.execute("""
                SELECT slug, title, content, rank
                FROM articles_fts
                WHERE articles_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (q, limit)).fetchall()
        except Exception:
            rows = db.execute("""
                SELECT slug, title, content, rank
                FROM articles_fts
                WHERE articles_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (f'"{q}"', limit)).fetchall()
    return [{"slug": r["slug"], "title": r["title"], "content": r["content"][:3000]} for r in rows]


# ---------------------------------------------------------------------------
# Routes -- Research (auto-ingest from YouTube search)
# ---------------------------------------------------------------------------

@app.post("/api/research")
async def research_topic(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    query = body.get("query", "").strip()
    max_results = body.get("max_results", 10)
    sources = body.get("sources", ["youtube"])  # default to YouTube for backward compat
    if not query:
        raise HTTPException(400, "No query provided")
    if max_results > 30:
        max_results = 30

    background_tasks.add_task(run_research, query, max_results, sources)
    return {"status": "started", "query": query, "max_results": max_results, "sources": sources}


# ---------------------------------------------------------------------------
# Routes -- Categories
# ---------------------------------------------------------------------------

@app.get("/api/categories")
async def list_categories():
    """Return all categories with article counts."""
    with get_db() as db:
        rows = db.execute("SELECT slug, title, path FROM articles").fetchall()

    categories = {}
    for row in rows:
        path = Path(row["path"])
        if not path.exists():
            continue
        content = path.read_text()
        # Extract categories from frontmatter -- supports both formats:
        #   Categories: [Trading, Strategy]
        #   Categories: Finance, AI Research
        cat_match = re.search(r'(?i)categories:\s*\[(.+?)\]', content)
        if not cat_match:
            cat_match = re.search(r'(?i)^categories:\s*(.+)$', content, re.MULTILINE)
        if cat_match:
            try:
                cats = json.loads(f"[{cat_match.group(1)}]")
            except Exception:
                cats = [c.strip().strip('"\'') for c in cat_match.group(1).split(",")]
        else:
            cats = ["Uncategorized"]

        for cat in cats:
            if cat not in categories:
                categories[cat] = []
            categories[cat].append({"slug": row["slug"], "title": row["title"]})

    return categories


# ---------------------------------------------------------------------------
# Routes -- arXiv feed
# ---------------------------------------------------------------------------

@app.get("/api/arxiv/papers")
async def arxiv_list_papers(limit: int = 50, offset: int = 0, hours: int = 0, q: str = ""):
    return get_arxiv_papers(limit=limit, offset=offset, time_filter=hours if hours > 0 else None, search=q if q else None)

@app.get("/api/arxiv/paper/{paper_id:path}")
async def arxiv_get_paper(paper_id: str):
    paper = get_arxiv_paper(paper_id)
    if not paper:
        raise HTTPException(404, "Paper not found")
    return paper

@app.get("/api/arxiv/similar/{paper_id:path}")
async def arxiv_similar(paper_id: str, n: int = 10):
    return get_similar_papers(paper_id, n=n)

@app.post("/api/arxiv/fetch")
async def arxiv_fetch(request: Request, background_tasks: BackgroundTasks):
    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    num = body.get("num", 200)
    background_tasks.add_task(run_arxiv_fetch, num)
    return {"status": "started", "num": num}

@app.post("/api/arxiv/save/{paper_id:path}")
async def arxiv_save_to_wiki(paper_id: str, background_tasks: BackgroundTasks):
    result = save_paper_to_wiki(paper_id, str(WIKI), str(DB_PATH))
    if not result:
        raise HTTPException(404, "Paper not found")
    return result

@app.get("/api/arxiv/stats")
async def arxiv_stats():
    from arxiv_feed import PAPERS_DB, CompressedSqliteDict
    try:
        pdb = CompressedSqliteDict(PAPERS_DB, tablename="papers", flag="r")
        total = len(pdb)
        pdb.close()
    except Exception:
        total = 0
    return {"total": total}

# ---------------------------------------------------------------------------
# Routes -- Feed (combined auto-fetched content from all sources)
# ---------------------------------------------------------------------------

feed_cache = {"items": [], "fetched_at": None}
FEED_CACHE_TTL = 6 * 3600  # 6 hours


@app.get("/api/feed")
async def get_feed(limit: int = 50):
    """Return combined feed items from arXiv + HN + GitHub trending. Cached for 6 hours."""
    now = datetime.now()
    if (feed_cache["fetched_at"] is None
            or (now - feed_cache["fetched_at"]).total_seconds() > FEED_CACHE_TTL
            or not feed_cache["items"]):
        try:
            items = _build_feed()
            feed_cache["items"] = items
            feed_cache["fetched_at"] = now
        except Exception as e:
            # Return stale cache if available, otherwise error
            if feed_cache["items"]:
                pass
            else:
                return {"items": [], "error": str(e)}

    return {"items": feed_cache["items"][:limit]}


@app.post("/api/feed/refresh")
async def refresh_feed():
    """Force-refresh the feed cache."""
    try:
        items = _build_feed()
        feed_cache["items"] = items
        feed_cache["fetched_at"] = datetime.now()
        return {"status": "ok", "count": len(items)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/feed/save")
async def save_feed_item(request: Request, background_tasks: BackgroundTasks):
    """Ingest a feed item into the wiki queue by source type and URL/ID."""
    body = await request.json()
    source = body.get("source", "")
    url = body.get("url", "").strip()
    title = body.get("title", "")
    item_id = body.get("id", "")

    if source == "arxiv" and item_id:
        result = save_paper_to_wiki(item_id, str(WIKI), str(DB_PATH))
        if not result:
            raise HTTPException(404, "Paper not found")
        return result

    if not url:
        raise HTTPException(400, "No URL provided")

    # For HN and GitHub items, submit the URL for ingestion
    entry_type = detect_type(url)
    with get_db() as db:
        existing = db.execute("SELECT id FROM entries WHERE source = ?", (url,)).fetchone()
        if existing:
            return {"status": "already_exists", "id": existing["id"]}
        cur = db.execute(
            "INSERT INTO entries (type, source, title, status) VALUES (?, ?, ?, 'pending')",
            (entry_type, url, title)
        )
        entry_id = cur.lastrowid

    background_tasks.add_task(run_ingestion, entry_id, url, entry_type)
    return {"status": "queued", "id": entry_id}


def _build_feed():
    """Fetch items from all feed sources and merge them."""
    items = []

    # 1) arXiv -- latest papers from local DB
    try:
        arxiv_data = get_arxiv_papers(limit=20, offset=0, time_filter=72)
        for p in arxiv_data.get("papers", []):
            items.append({
                "id": p["id"],
                "source": "arxiv",
                "title": p["title"],
                "description": p["summary"][:200] + "..." if len(p.get("summary", "")) > 200 else p.get("summary", ""),
                "url": p.get("url", f"https://arxiv.org/abs/{p['id']}"),
                "time": p.get("time_str", ""),
                "tags": p.get("tags", []),
                "_ts": p.get("time", 0),
            })
    except Exception as e:
        print(f"Feed: arXiv error: {e}")

    # 2) Web -- RSS feeds from curated sources
    try:
        web_items = _fetch_rss_feeds(20)
        items.extend(web_items)
    except Exception as e:
        print(f"Feed: RSS error: {e}")

    # 3) GitHub trending -- repos created recently with high stars
    try:
        gh_items = _fetch_github_trending(20)
        items.extend(gh_items)
    except Exception as e:
        print(f"Feed: GitHub trending error: {e}")

    # Sort all items by timestamp (most recent first)
    items.sort(key=lambda x: x.get("_ts", 0), reverse=True)
    return items


RSS_FEEDS = [
    ("https://news.ycombinator.com/rss", "HN"),
    ("https://techcrunch.com/feed/", "TechCrunch"),
    ("https://feeds.arstechnica.com/arstechnica/technology-lab", "Ars Technica"),
    ("https://www.theverge.com/rss/index.xml", "The Verge"),
    ("https://www.wired.com/feed/rss", "Wired"),
    ("https://feeds.feedburner.com/venturebeat/SZYF", "VentureBeat"),
]

def _fetch_rss_feeds(limit=20):
    """Fetch latest articles from curated RSS feeds."""
    import xml.etree.ElementTree as ET
    items = []

    for feed_url, source_name in RSS_FEEDS:
        try:
            req = urllib.request.Request(feed_url, headers={"User-Agent": "Nikipedia/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                xml_data = resp.read().decode(errors='replace')
            root = ET.fromstring(xml_data)

            # Handle both RSS and Atom formats
            entries = root.findall('.//item') or root.findall('.//{http://www.w3.org/2005/Atom}entry')
            for entry in entries[:5]:
                # RSS format
                title = entry.findtext('title', '')
                link = entry.findtext('link', '')
                desc = entry.findtext('description', '')
                pub_date = entry.findtext('pubDate', '')

                # Atom format fallback
                if not title:
                    title = entry.findtext('{http://www.w3.org/2005/Atom}title', '')
                if not link:
                    link_el = entry.find('{http://www.w3.org/2005/Atom}link')
                    link = link_el.get('href', '') if link_el is not None else ''
                if not desc:
                    desc = entry.findtext('{http://www.w3.org/2005/Atom}summary', '')
                if not pub_date:
                    pub_date = entry.findtext('{http://www.w3.org/2005/Atom}updated', '')

                # Clean description
                import re
                desc = re.sub(r'<[^>]+>', '', desc or '')[:200]

                # Parse timestamp
                ts = 0
                time_str = ""
                if pub_date:
                    try:
                        from email.utils import parsedate_to_datetime
                        dt = parsedate_to_datetime(pub_date)
                        ts = dt.timestamp()
                    except Exception:
                        try:
                            dt = datetime.fromisoformat(pub_date.replace('Z', '+00:00'))
                            ts = dt.timestamp()
                        except Exception:
                            pass
                if ts:
                    age = datetime.now().timestamp() - ts
                    if age < 3600:
                        time_str = f"{int(age/60)}m ago"
                    elif age < 86400:
                        time_str = f"{int(age/3600)}h ago"
                    else:
                        time_str = f"{int(age/86400)}d ago"

                if title and link:
                    items.append({
                        "id": f"web-{hash(link) & 0xffffffff}",
                        "source": "web",
                        "title": title.strip(),
                        "description": f"{source_name} | {desc.strip()}",
                        "url": link.strip(),
                        "time": time_str,
                        "tags": [source_name],
                        "_ts": ts,
                    })
        except Exception as e:
            print(f"RSS error ({source_name}): {e}")
            continue

    return items[:limit]


def _fetch_github_trending(limit=20):
    """Fetch recently-created high-star repos from GitHub API."""
    items = []
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    url = f"https://api.github.com/search/repositories?q=stars:>50+created:>{cutoff}&sort=stars&order=desc&per_page={limit}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Nikipedia/1.0",
        "Accept": "application/vnd.github.v3+json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())

    for repo in data.get("items", [])[:limit]:
        created = repo.get("created_at", "")
        time_str = ""
        if created:
            try:
                cdt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age = (datetime.now().timestamp() - cdt.timestamp())
                if age < 3600:
                    time_str = f"{int(age/60)}m ago"
                elif age < 86400:
                    time_str = f"{int(age/3600)}h ago"
                else:
                    time_str = f"{int(age/86400)}d ago"
            except Exception:
                time_str = created[:10]

        gh_ts = 0
        try:
            if created:
                gh_ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
        items.append({
            "id": f"gh-{repo['full_name']}",
            "source": "github",
            "title": repo.get("full_name", ""),
            "description": repo.get("description", "") or "",
            "url": repo.get("html_url", ""),
            "time": time_str,
            "tags": [repo.get("language", "")] if repo.get("language") else [],
            "stars": repo.get("stargazers_count", 0),
            "_ts": gh_ts,
        })

    return items


# ---------------------------------------------------------------------------
# Routes -- Search (multi-source: GitHub, Web)
# ---------------------------------------------------------------------------

@app.get("/api/search/github")
async def search_github(q: str = ""):
    """Search GitHub repositories."""
    if not q or len(q) < 2:
        raise HTTPException(400, "Query too short")
    url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(q)}&sort=stars&order=desc&per_page=15"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Nikipedia/1.0",
        "Accept": "application/vnd.github.v3+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        raise HTTPException(502, f"GitHub API error: {e}")

    results = []
    for repo in data.get("items", []):
        results.append({
            "source": "github",
            "title": repo.get("full_name", ""),
            "description": repo.get("description", "") or "",
            "url": repo.get("html_url", ""),
            "stars": repo.get("stargazers_count", 0),
            "language": repo.get("language", ""),
        })
    return results


@app.get("/api/search/web")
async def search_web(q: str = ""):
    """Search web via DuckDuckGo HTML (lite version)."""
    if not q or len(q) < 2:
        raise HTTPException(400, "Query too short")

    results = _search_ddg(q)
    return results


def _search_ddg(query: str, max_results: int = 15):
    """Scrape DuckDuckGo HTML lite for search results."""
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return []

    results = []
    # Parse DDG HTML lite results -- each result is in a <a class="result__a"> with href
    # and snippet in <a class="result__snippet">
    import re as _re
    # Extract result blocks
    blocks = _re.findall(r'<a rel="nofollow" class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?<a class="result__snippet"[^>]*>(.*?)</a>', html, _re.DOTALL)
    for href, title_html, snippet_html in blocks[:max_results]:
        # Clean up DDG redirect URL
        actual_url = href
        if "uddg=" in href:
            m = _re.search(r'uddg=([^&]+)', href)
            if m:
                actual_url = urllib.parse.unquote(m.group(1))
        title = _re.sub(r'<[^>]+>', '', title_html).strip()
        snippet = _re.sub(r'<[^>]+>', '', snippet_html).strip()
        if title and actual_url:
            results.append({
                "source": "web",
                "title": title,
                "description": snippet,
                "url": actual_url,
            })

    return results


# ---------------------------------------------------------------------------
# Routes -- Internal ingestion
# ---------------------------------------------------------------------------

@app.post("/api/ingest-internal")
async def trigger_ingest_internal(background_tasks: BackgroundTasks):
    """Ingest internal project markdown files into the wiki."""
    background_tasks.add_task(run_internal_ingestion)
    return {"status": "started"}


# ---------------------------------------------------------------------------
# Routes -- static files (uploads, raw images)
# ---------------------------------------------------------------------------

@app.get("/uploads/{filename}")
async def serve_upload(filename: str):
    path = UPLOADS / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

def run_ingestion(entry_id: int, url: str, entry_type: str, project: str = ""):
    try:
        title, raw_path = ingest_url(url, entry_type, str(RAW))
        with get_db() as db:
            db.execute(
                "UPDATE entries SET title = ?, raw_path = ?, status = 'ingested' WHERE id = ?",
                (title, raw_path, entry_id)
            )
        # Store project tag for later use during compilation
        if project:
            _entry_project_tags[entry_id] = project
        # Auto-compile immediately after ingestion
        threading.Thread(target=run_compilation, args=([entry_id],), daemon=True).start()
    except Exception as e:
        with get_db() as db:
            db.execute(
                "UPDATE entries SET status = 'error', error = ? WHERE id = ?",
                (str(e)[:500], entry_id)
            )

# Map entry IDs to project tags (set during ingestion, consumed during compilation)
_entry_project_tags: dict = {}


def run_compilation(entry_ids=None):
    global compile_status
    compile_status = {"running": True, "progress": "Starting...", "last_run": None, "error": None}
    try:
        with get_db() as db:
            if entry_ids:
                placeholders = ",".join("?" * len(entry_ids))
                rows = db.execute(
                    f"SELECT * FROM entries WHERE id IN ({placeholders}) AND status = 'ingested'",
                    entry_ids
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM entries WHERE status = 'ingested'"
                ).fetchall()

            if not rows:
                compile_status = {"running": False, "progress": "Nothing to compile", "last_run": datetime.now().isoformat(), "error": None}
                return

            entries = [dict(r) for r in rows]

            # Get existing wiki articles for context
            wiki_rows = db.execute("SELECT slug, title, path FROM articles").fetchall()
            existing_articles = []
            for wr in wiki_rows:
                d = dict(wr)
                if Path(d["path"]).exists():
                    content = strip_frontmatter(Path(d["path"]).read_text())
                    d["preview"] = content[:300]
                existing_articles.append(d)

        # Process entries one at a time
        for i, entry in enumerate(entries):
            compile_status["progress"] = f"Compiling {i+1}/{len(entries)}: {entry.get('title', entry['source'])}"

            try:
                new_articles, updated_articles = compile_entries(entry, existing_articles, str(WIKI))

                with get_db() as db:
                    # Determine project tag for this entry
                    project_tag = _entry_project_tags.pop(entry["id"], "")
                    tags_list = [project_tag] if project_tag else []

                    # Save new articles
                    for art in new_articles:
                        # Merge with existing tags if article already exists
                        existing_row = db.execute("SELECT tags FROM articles WHERE slug = ?", (art["slug"],)).fetchone()
                        if existing_row and existing_row["tags"]:
                            try:
                                existing_tags = json.loads(existing_row["tags"])
                                for t in tags_list:
                                    if t and t not in existing_tags:
                                        existing_tags.append(t)
                                tags_list = existing_tags
                            except (json.JSONDecodeError, TypeError):
                                pass
                        db.execute(
                            """INSERT OR REPLACE INTO articles (slug, title, path, source_ids, tags, updated_at)
                               VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                            (art["slug"], art["title"], art["path"], json.dumps([entry["id"]]), json.dumps(tags_list))
                        )
                        index_article_fts(db, art["slug"], art["title"], art["path"])
                        existing_articles.append({"slug": art["slug"], "title": art["title"], "path": art["path"], "preview": art.get("preview", "")})

                    # Update existing articles
                    for art in updated_articles:
                        # Merge source_ids
                        row = db.execute("SELECT source_ids FROM articles WHERE slug = ?", (art["slug"],)).fetchone()
                        source_ids = json.loads(row["source_ids"]) if row else []
                        if entry["id"] not in source_ids:
                            source_ids.append(entry["id"])
                        db.execute(
                            "UPDATE articles SET path = ?, updated_at = datetime('now'), source_ids = ? WHERE slug = ?",
                            (art["path"], json.dumps(source_ids), art["slug"])
                        )
                        index_article_fts(db, art["slug"], art["title"], art["path"])

                    # Mark entry as compiled
                    db.execute(
                        "UPDATE entries SET status = 'compiled', compiled_at = datetime('now') WHERE id = ?",
                        (entry["id"],)
                    )

            except Exception as e:
                with get_db() as db:
                    db.execute(
                        "UPDATE entries SET status = 'error', error = ? WHERE id = ?",
                        (f"Compile error: {str(e)[:400]}", entry["id"])
                    )

        compile_status = {"running": False, "progress": f"Done. Compiled {len(entries)} entries.", "last_run": datetime.now().isoformat(), "error": None}

        # Email summaries of newly compiled articles
        try:
            _email_compilation_summary(entries)
        except Exception as email_err:
            print(f"Email notification failed: {email_err}")

    except Exception as e:
        compile_status = {"running": False, "progress": "", "last_run": datetime.now().isoformat(), "error": str(e)}


def _email_compilation_summary(entries):
    """Log compilation summary. Configure NOTIFICATION_EMAIL in .env for email alerts."""
    summaries = []
    for entry in entries:
        title = entry.get('title', entry.get('source', 'Unknown'))
        entry_type = entry.get('type', '')
        with get_db() as db:
            arts = db.execute(
                "SELECT title, slug FROM articles WHERE source_ids LIKE ?",
                (f'%{entry["id"]}%',)
            ).fetchall()
        article_titles = [a['title'] for a in arts]
        summaries.append(f"Source: {title} | Type: {entry_type} | Articles: {', '.join(article_titles) if article_titles else 'none'}")

    if summaries:
        print(f"Compilation complete: {len(entries)} entries processed")
        for s in summaries:
            print(f"  {s}")


def run_lint():
    global compile_status
    compile_status = {"running": True, "progress": "Linting wiki...", "last_run": None, "error": None}
    try:
        result = lint_wiki(str(WIKI))
        compile_status = {"running": False, "progress": f"Lint complete. {result}", "last_run": datetime.now().isoformat(), "error": None}
    except Exception as e:
        compile_status = {"running": False, "progress": "", "last_run": datetime.now().isoformat(), "error": str(e)}


def run_internal_ingestion():
    """Background task: ingest internal project markdown files."""
    global compile_status
    compile_status = {"running": True, "progress": "Ingesting internal project files...", "last_run": None, "error": None}
    try:
        summary = run_internal_ingest()
        msg = f"Internal ingestion done. Added: {len(summary['added'])}, Updated: {len(summary['updated'])}, Skipped: {summary['skipped']}"
        compile_status = {"running": False, "progress": msg, "last_run": datetime.now().isoformat(), "error": None}
    except Exception as e:
        compile_status = {"running": False, "progress": "", "last_run": datetime.now().isoformat(), "error": str(e)}


def run_research(query: str, max_results: int, sources: list = None):
    """Search multiple sources for content on a topic and auto-ingest results."""
    if sources is None:
        sources = ["youtube"]
    global compile_status
    compile_status = {"running": True, "progress": f"Researching: {query}...", "last_run": None, "error": None}
    total_ingested = 0
    try:
        # YouTube
        if "youtube" in sources:
            try:
                compile_status["progress"] = f"Searching YouTube for: {query}"
                result = subprocess.run(
                    ["yt-dlp", f"ytsearch{max_results}:{query}",
                     "--flat-playlist", "--no-download", "--print", "%(id)s\t%(title)s\t%(view_count)s\t%(duration)s"],
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode == 0:
                    lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
                    for i, line in enumerate(lines):
                        parts = line.split("\t")
                        if len(parts) < 2:
                            continue
                        video_id = parts[0]
                        video_title = parts[1] if len(parts) > 1 else video_id
                        url = f"https://www.youtube.com/watch?v={video_id}"
                        compile_status["progress"] = f"[YT] Ingesting {i+1}/{len(lines)}: {video_title[:60]}"
                        with get_db() as db:
                            existing = db.execute("SELECT id FROM entries WHERE source = ?", (url,)).fetchone()
                            if existing:
                                continue
                        try:
                            run_ingestion_sync(url, "youtube")
                            total_ingested += 1
                        except Exception:
                            pass
            except Exception as e:
                print(f"Research YouTube error: {e}")

        # arXiv
        if "arxiv" in sources:
            try:
                compile_status["progress"] = f"Searching arXiv for: {query}"
                arxiv_data = get_arxiv_papers(limit=max_results, offset=0, search=query)
                papers = arxiv_data.get("papers", [])
                for i, p in enumerate(papers):
                    compile_status["progress"] = f"[arXiv] Saving {i+1}/{len(papers)}: {p['title'][:60]}"
                    try:
                        save_paper_to_wiki(p["id"], str(WIKI), str(DB_PATH))
                        total_ingested += 1
                    except Exception:
                        pass
            except Exception as e:
                print(f"Research arXiv error: {e}")

        # GitHub
        if "github" in sources:
            try:
                compile_status["progress"] = f"Searching GitHub for: {query}"
                gh_url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(query)}&sort=stars&order=desc&per_page={min(max_results, 10)}"
                req = urllib.request.Request(gh_url, headers={
                    "User-Agent": "Nikipedia/1.0",
                    "Accept": "application/vnd.github.v3+json",
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    gh_data = json.loads(resp.read().decode())
                repos = gh_data.get("items", [])
                for i, repo in enumerate(repos):
                    repo_url = repo.get("html_url", "")
                    if not repo_url:
                        continue
                    compile_status["progress"] = f"[GH] Ingesting {i+1}/{len(repos)}: {repo['full_name']}"
                    with get_db() as db:
                        existing = db.execute("SELECT id FROM entries WHERE source = ?", (repo_url,)).fetchone()
                        if existing:
                            continue
                    try:
                        run_ingestion_sync(repo_url, "github")
                        total_ingested += 1
                    except Exception:
                        pass
            except Exception as e:
                print(f"Research GitHub error: {e}")

        # Web
        if "web" in sources:
            try:
                compile_status["progress"] = f"Searching web for: {query}"
                web_results = _search_ddg(query, max_results=min(max_results, 10))
                for i, wr in enumerate(web_results):
                    item_url = wr.get("url", "")
                    if not item_url:
                        continue
                    compile_status["progress"] = f"[Web] Ingesting {i+1}/{len(web_results)}: {wr['title'][:60]}"
                    with get_db() as db:
                        existing = db.execute("SELECT id FROM entries WHERE source = ?", (item_url,)).fetchone()
                        if existing:
                            continue
                    try:
                        run_ingestion_sync(item_url, "article")
                        total_ingested += 1
                    except Exception:
                        pass
            except Exception as e:
                print(f"Research Web error: {e}")

        # Compile all uncompiled entries
        if total_ingested > 0:
            compile_status["progress"] = f"Ingested {total_ingested} items from {', '.join(sources)}. Starting compilation..."
            run_compilation(None)
        else:
            compile_status = {"running": False, "progress": f"Search complete. No new items to ingest from {', '.join(sources)}.", "last_run": datetime.now().isoformat(), "error": None}

    except Exception as e:
        compile_status = {"running": False, "progress": "", "last_run": datetime.now().isoformat(), "error": str(e)}


def run_arxiv_fetch(num: int):
    """Background task: fetch papers from arXiv."""
    global compile_status
    compile_status = {"running": True, "progress": f"Fetching up to {num} arXiv papers...", "last_run": None, "error": None}
    try:
        result = fetch_arxiv_papers(num=num)
        compile_status = {"running": False, "progress": f"arXiv: {result['new']} new papers fetched, {result['total']} total", "last_run": datetime.now().isoformat(), "error": None}
    except Exception as e:
        compile_status = {"running": False, "progress": "", "last_run": datetime.now().isoformat(), "error": str(e)}


def run_ingestion_sync(url: str, entry_type: str):
    """Synchronous ingestion (for use inside research pipeline)."""
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO entries (type, source, status) VALUES (?, ?, 'pending')",
            (entry_type, url)
        )
        entry_id = cur.lastrowid

    try:
        title, raw_path = ingest_url(url, entry_type, str(RAW))
        with get_db() as db:
            db.execute(
                "UPDATE entries SET title = ?, raw_path = ?, status = 'ingested' WHERE id = ?",
                (title, raw_path, entry_id)
            )
    except Exception as e:
        with get_db() as db:
            db.execute(
                "UPDATE entries SET status = 'error', error = ? WHERE id = ?",
                (str(e)[:500], entry_id)
            )


# ---------------------------------------------------------------------------
# Council (group chat with AI agents)
# ---------------------------------------------------------------------------

# SSE subscribers for council updates
council_subscribers: list[Queue] = []

# Atlas/Spark removed — chat is now a pure group chat across tmux sessions.
# Messages without @mention are posted but don't auto-trigger any agent.


def council_broadcast(msg: dict):
    """Send a message to all SSE subscribers."""
    dead = []
    for q in council_subscribers:
        try:
            q.put_nowait(msg)
        except Exception:
            dead.append(q)
    for q in dead:
        try:
            council_subscribers.remove(q)
        except ValueError:
            pass


@app.get("/api/council")
async def council_list():
    """Return top-level posts with reply counts, newest first."""
    with get_db() as db:
        rows = db.execute(
            "SELECT m.*, (SELECT COUNT(*) FROM council_messages r WHERE r.thread_id = m.id) as reply_count FROM council_messages m WHERE m.thread_id IS NULL ORDER BY m.id ASC LIMIT 50"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/council/thread/{thread_id}")
async def council_thread(thread_id: int):
    """Return a thread: the original post + all replies."""
    with get_db() as db:
        parent = db.execute("SELECT * FROM council_messages WHERE id = ?", (thread_id,)).fetchone()
        if not parent:
            raise HTTPException(404, "Thread not found")
        replies = db.execute(
            "SELECT * FROM council_messages WHERE thread_id = ? ORDER BY id ASC", (thread_id,)
        ).fetchall()
    return {"post": dict(parent), "replies": [dict(r) for r in replies]}


@app.post("/api/council")
async def council_post(request: Request):
    """Post a message from Nik. @session routes to tmux. Otherwise triggers council agents."""
    body = await request.json()
    content = body.get("content", "").strip()
    thread_id = body.get("thread_id", None)
    if not content:
        raise HTTPException(400, "No content provided")

    # Parse @mentions (supports multiple: @splaybook @smoped what's up)
    import re as _re
    mentions = _re.findall(r'@(\w+)', content)
    # Strip all @mentions from content to get the actual message
    actual_message = _re.sub(r'@\w+\s*', '', content).strip()

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO council_messages (author, content, thread_id, session, source) VALUES (?, ?, ?, ?, ?)",
            ("Niki", content, thread_id, ','.join(mentions) if mentions else None, "chat")
        )
        msg_id = cur.lastrowid
        row = db.execute("SELECT * FROM council_messages WHERE id = ?", (msg_id,)).fetchone()

    msg = dict(row)
    council_broadcast({"type": "message", "message": msg})

    target_thread = thread_id if thread_id else msg_id

    if 'all' in mentions:
        thread = threading.Thread(
            target=broadcast_to_all_sessions,
            args=(actual_message, target_thread),
            daemon=True
        )
        thread.start()
    elif mentions:
        for sess in mentions:
            t = threading.Thread(
                target=route_to_tmux_session,
                args=(sess, actual_message, target_thread),
                daemon=True
            )
            t.start()
            time.sleep(0.3)
    # No @mention = pure post, no auto-trigger.

    return msg


@app.post("/api/council/tmux")
async def council_tmux_post(request: Request):
    """Bridge endpoint: tmux scripts POST messages here to appear in chat.
    Supports @session mentions for session-to-session routing."""
    body = await request.json()
    author = body.get("author", "").strip()
    content = body.get("content", "").strip()
    session = body.get("session", "").strip()
    thread_id = body.get("thread_id", None)
    loop_id = body.get("loop_id", None)
    source = body.get("source", "tmux")
    if not author or not content:
        raise HTTPException(400, "author and content required")

    # Check for @session targeting (session-to-session communication)
    import re as _re
    session_match = _re.match(r'^@(\w+)\s+(.*)', content, _re.DOTALL)
    target_session = session_match.group(1) if session_match else None

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO council_messages (author, content, thread_id, session, source, loop_id) VALUES (?, ?, ?, ?, ?, ?)",
            (author, content, thread_id, session, source, loop_id)
        )
        msg_id = cur.lastrowid
        row = db.execute("SELECT * FROM council_messages WHERE id = ?", (msg_id,)).fetchone()

    msg = dict(row)
    council_broadcast({"type": "message", "message": msg})

    # Route @mentions to target session
    if target_session:
        target_thread = thread_id if thread_id else msg_id
        t = threading.Thread(
            target=route_to_tmux_session,
            args=(target_session, session_match.group(2), target_thread),
            daemon=True
        )
        t.start()

    return msg


@app.get("/api/council/sessions")
async def council_sessions():
    """List active tmux sessions for the chat UI."""
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5
        )
        sessions = [s.strip() for s in result.stdout.strip().split('\n') if s.strip()]
        return {"sessions": sessions}
    except Exception:
        return {"sessions": []}


# ---------------------------------------------------------------------------
# Routes -- Knowledge Graph / Tree
# ---------------------------------------------------------------------------

@app.get("/api/graph")
async def get_graph():
    """Return full knowledge graph as nodes + edges for tree visualization."""
    with get_db() as db:
        nodes = db.execute("""
            SELECT gn.id, gn.label, gn.article_id, gn.node_type, gn.category,
                   a.slug, a.title as article_title
            FROM graph_nodes gn
            LEFT JOIN articles a ON gn.article_id = a.id
        """).fetchall()
        edges = db.execute("""
            SELECT source_id, target_id, relationship, weight
            FROM graph_edges
        """).fetchall()
    return {
        "nodes": [dict(n) for n in nodes],
        "edges": [dict(e) for e in edges]
    }


@app.get("/api/graph/tree")
async def get_graph_tree():
    """Return knowledge tree from tree_structure.json with article previews."""
    tree_path = BASE / "tree_structure.json"
    if not tree_path.exists():
        return {"tree": {}, "stats": {"articles": 0, "categories": 0}}

    tree = json.loads(tree_path.read_text())

    # Build slug->path lookup for previews
    with get_db() as db:
        articles = db.execute("SELECT slug, title, path FROM articles").fetchall()
    slug_map = {}
    for a in articles:
        slug_map[a["slug"]] = {"title": a["title"], "path": a["path"]}

    # Add previews to articles in tree
    def enrich(node):
        if "__articles__" in node:
            for art in node["__articles__"]:
                info = slug_map.get(art.get("slug", ""), {})
                p = info.get("path", "")
                if p and Path(p).exists():
                    content = strip_frontmatter(Path(p).read_text()[:500])
                    if content.startswith("Categories:"):
                        content = content.split("\n", 2)[-1].strip()
                    # Get first meaningful line after heading
                    lines = [l.strip() for l in content.split("\n") if l.strip() and not l.startswith("#")]
                    art["preview"] = lines[0][:200] if lines else ""
                else:
                    art["preview"] = ""
        for k, v in node.items():
            if k != "__articles__" and isinstance(v, dict):
                enrich(v)

    enrich(tree)

    # Count stats
    def count(node):
        a = len(node.get("__articles__", []))
        c = 0
        for k, v in node.items():
            if k != "__articles__" and isinstance(v, dict):
                sa, sc = count(v)
                a += sa
                c += sc + 1
        return a, c

    total_articles, total_cats = count(tree)

    return {"tree": tree, "stats": {"articles": total_articles, "categories": total_cats}}


@app.post("/api/graph/extract")
async def extract_graph_from_articles():
    """Run entity/relationship extraction on all articles. Returns count of new nodes/edges."""
    import subprocess
    result = subprocess.run(
        ["python3", str(BASE / "extract_graph.py")],
        capture_output=True, text=True, timeout=300
    )
    return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}


@app.get("/api/loops")
async def get_loops():
    """Return status and results.tsv data for all project loops."""
    import csv, io
    projects_dir = os.environ.get("PROJECTS_DIR", "/root/projects")
    pid_dir = os.environ.get("TASKQ_DIR", "/opt/agent-stack/taskq") + "/pids"
    results = []
    try:
        for entry in os.listdir(projects_dir):
            tsv_path = os.path.join(projects_dir, entry, "results.tsv")
            if not os.path.isfile(tsv_path):
                continue
            pid_file = os.path.join(pid_dir, f"{entry}.pid")
            running = False
            pid = None
            if os.path.isfile(pid_file):
                try:
                    pid = int(open(pid_file).read().strip())
                    os.kill(pid, 0)
                    running = True
                except (ValueError, ProcessLookupError, PermissionError):
                    running = False
            rows = []
            try:
                with open(tsv_path, 'r') as f:
                    reader = csv.DictReader(f, delimiter='\t')
                    for row in reader:
                        rows.append(row)
            except Exception:
                pass
            # Use TSV modification time for sorting by last activity
            last_modified = os.path.getmtime(tsv_path)
            results.append({
                "project": entry,
                "running": running,
                "pid": pid,
                "iterations": len(rows),
                "rows": rows[-20:],
                "last_modified": last_modified
            })
        # Sort: running first, then by most recently modified
        results.sort(key=lambda x: (0 if x["running"] else 1, -x["last_modified"]))
    except Exception as e:
        return {"projects": [], "error": str(e)}
    return {"projects": results}


def route_to_tmux_session(session_name: str, message: str, thread_id: int):
    """Send message to tmux session as Claude Code user input, capture response, post back to chat."""
    # Verify session exists
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", f"={session_name}"],
            capture_output=True, timeout=5
        )
        if result.returncode != 0:
            _post_system_msg(f"Session '{session_name}' not found", thread_id)
            return
    except Exception:
        _post_system_msg(f"Failed to reach session '{session_name}'", thread_id)
        return

    council_broadcast({"type": "typing", "agent": session_name})

    reply_file = f"/tmp/council-reply-{session_name}-{thread_id}.txt"
    try:
        os.remove(reply_file)
    except Exception:
        pass

    # Build the message to send as Claude Code user input.
    # Short enough to send directly via tmux send-keys.
    # The session's Claude Code instance will process it as a normal user message.
    user_input = f"[From Nikipedia chat — reply by writing to {reply_file}] {message}"

    # Send directly as Claude Code user input via tmux
    # Use send-keys with the text, then Enter to submit
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, "-l", user_input],
        timeout=5
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, "Enter"],
        timeout=5
    )

    # Wait for reply (up to 3 min)
    for _ in range(36):
        time.sleep(5)
        if os.path.exists(reply_file):
            break
    else:
        _post_system_msg(f"Timeout waiting for {session_name} to respond", thread_id)
        council_broadcast({"type": "done"})
        return

    try:
        response = open(reply_file).read().strip()
    except Exception:
        response = "(could not read response)"

    with get_db() as db:
        cur = db.execute(
            "INSERT INTO council_messages (author, content, thread_id, session, source) VALUES (?, ?, ?, ?, ?)",
            (session_name, response, thread_id, session_name, "tmux")
        )
        msg_id = cur.lastrowid
        row = db.execute("SELECT * FROM council_messages WHERE id = ?", (msg_id,)).fetchone()

    council_broadcast({"type": "message", "message": dict(row)})
    council_broadcast({"type": "done"})

    # Cleanup
    for f in [reply_file]:
        try:
            os.remove(f)
        except Exception:
            pass


def broadcast_to_all_sessions(message: str, thread_id: int):
    """Send message to all active Claude Code sessions in parallel."""
    # Skip non-Claude sessions
    SKIP = {'main', 'voice'}
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5
        )
        sessions = [s.strip() for s in result.stdout.strip().split('\n') if s.strip() and s.strip() not in SKIP]
    except Exception:
        _post_system_msg("Failed to list sessions", thread_id)
        return

    _post_system_msg(f"Broadcasting to {len(sessions)} sessions...", thread_id)

    threads = []
    for sess in sessions:
        t = threading.Thread(
            target=route_to_tmux_session,
            args=(sess, message, thread_id),
            daemon=True
        )
        t.start()
        threads.append(t)
        time.sleep(0.5)  # stagger slightly to avoid tmux contention


def _post_system_msg(content: str, thread_id: int):
    """Post a system message to chat."""
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO council_messages (author, content, thread_id, source) VALUES (?, ?, ?, ?)",
            ("System", content, thread_id, "system")
        )
        msg_id = cur.lastrowid
        row = db.execute("SELECT * FROM council_messages WHERE id = ?", (msg_id,)).fetchone()
    council_broadcast({"type": "message", "message": dict(row)})


@app.post("/api/council/clear")
async def council_clear():
    """Clear all council messages."""
    with get_db() as db:
        db.execute("DELETE FROM council_messages")
    council_broadcast({"type": "cleared"})
    return {"status": "cleared"}


@app.get("/api/council/stream")
async def council_stream():
    """SSE endpoint for real-time council updates."""
    q: Queue = Queue()
    council_subscribers.append(q)

    async def event_generator():
        try:
            yield "data: {\"type\": \"connected\"}\n\n"
            while True:
                try:
                    # Check queue with short timeout to allow cancellation
                    msg = await asyncio.get_event_loop().run_in_executor(None, lambda: q.get(timeout=30))
                    yield f"data: {json.dumps(msg)}\n\n"
                except Exception:
                    # Send keepalive
                    yield ": keepalive\n\n"
        finally:
            try:
                council_subscribers.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    init_db()
    # Migrate arxiv-sanity-lite data on first run
    from arxiv_feed import migrate_from_arxiv_sanity, ARXIV_DIR
    if not (ARXIV_DIR / "papers.db").exists():
        try:
            n = migrate_from_arxiv_sanity()
            if n > 0:
                print(f"Migrated {n} papers from arxiv-sanity-lite")
        except Exception as e:
            print(f"arXiv migration skipped: {e}")
    # Auto-retry stuck/error items and auto-compile ingested items
    try:
        with get_db() as db:
            stuck = db.execute("SELECT COUNT(*) as c FROM entries WHERE status IN ('pending', 'error', 'ingested')").fetchone()
            if stuck and stuck["c"] > 0:
                print(f"Auto-retrying {stuck['c']} stuck/error/ingested items")
                db.execute("UPDATE entries SET status = 'pending' WHERE status = 'error'")
                threading.Thread(target=run_compilation, args=(None,), daemon=True).start()
    except Exception as e:
        print(f"Auto-retry skipped: {e}")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=4090)
