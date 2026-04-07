"""arXiv feed integration -- fetch, browse, and save papers to Nikipedia wiki."""

import os
import sys
import time
import logging
import pickle
import zlib
import sqlite3
import urllib.request
from pathlib import Path

import feedparser
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sqlitedict import SqliteDict

logger = logging.getLogger(__name__)

ARXIV_DIR = Path("/opt/knowledge/arxiv_data")
ARXIV_DIR.mkdir(exist_ok=True)
PAPERS_DB = str(ARXIV_DIR / "papers.db")
FEATURES_FILE = str(ARXIV_DIR / "features.p")

# Default categories to fetch
DEFAULT_QUERY = "cat:cs.AI+OR+cat:cs.LG+OR+cat:cs.CL+OR+cat:cs.CV+OR+cat:cs.NE+OR+cat:cs.RO"


# ---------------------------------------------------------------------------
# Compressed SqliteDict (same as arxiv-sanity-lite)
# ---------------------------------------------------------------------------

class CompressedSqliteDict(SqliteDict):
    def __init__(self, *args, **kwargs):
        def encode(obj):
            return sqlite3.Binary(zlib.compress(pickle.dumps(obj, pickle.HIGHEST_PROTOCOL)))
        def decode(obj):
            return pickle.loads(zlib.decompress(bytes(obj)))
        super().__init__(*args, **kwargs, encode=encode, decode=decode)


# ---------------------------------------------------------------------------
# Fetch from arXiv API
# ---------------------------------------------------------------------------

def fetch_arxiv_papers(num=200, query=None):
    """Fetch papers from arXiv API and store in local DB. Returns count of new papers."""
    q = query or DEFAULT_QUERY
    pdb = CompressedSqliteDict(PAPERS_DB, tablename="papers", flag="c", autocommit=True)
    mdb = SqliteDict(PAPERS_DB, tablename="metas", flag="c", autocommit=True)
    prev_count = len(pdb)

    total_new = 0
    for start in range(0, num, 100):
        batch_size = min(100, num - start)
        base_url = "http://export.arxiv.org/api/query?"
        params = f"search_query={q}&sortBy=lastUpdatedDate&start={start}&max_results={batch_size}"
        url = base_url + params

        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
        except Exception as e:
            logger.error(f"arXiv API error at offset {start}: {e}")
            break

        parsed = feedparser.parse(data)
        batch_new = 0
        for entry in parsed.entries:
            paper = _encode_entry(entry)
            if paper["_id"] not in pdb:
                batch_new += 1
            pdb[paper["_id"]] = paper
            mdb[paper["_id"]] = {"_time": paper["_time"]}

        total_new += batch_new
        logger.info(f"arXiv fetch offset={start}: {batch_new} new, {len(pdb)} total")

        # Stop early if we're getting all duplicates
        if batch_new == 0:
            break

        time.sleep(3)  # Be nice to arXiv API

    pdb.close()
    mdb.close()

    # Recompute features after fetching
    compute_arxiv_features()

    return {"new": total_new, "total": len(CompressedSqliteDict(PAPERS_DB, tablename="papers", flag="r"))}


def _encode_entry(entry):
    """Convert feedparser entry to a clean dict."""
    d = {}
    for k in entry.keys():
        v = entry[k]
        if isinstance(v, feedparser.FeedParserDict) or isinstance(v, dict):
            d[k] = {kk: vv for kk, vv in v.items()}
        elif isinstance(v, list):
            d[k] = [
                ({kk: vv for kk, vv in item.items()} if isinstance(item, (dict, feedparser.FeedParserDict)) else item)
                for item in v
            ]
        else:
            d[k] = v

    # Parse ID
    raw_url = d.get("id", "")
    ix = raw_url.rfind("/")
    if ix >= 0:
        idv = raw_url[ix + 1:]
        parts = idv.split("v")
        if len(parts) == 2:
            d["_idv"] = idv
            d["_id"] = parts[0]
            d["_version"] = int(parts[1])
        else:
            d["_idv"] = idv
            d["_id"] = idv
            d["_version"] = 1
    else:
        d["_idv"] = raw_url
        d["_id"] = raw_url
        d["_version"] = 1

    d["_time"] = time.mktime(d.get("updated_parsed", time.localtime()))
    d["_time_str"] = time.strftime("%b %d %Y", d.get("updated_parsed", time.localtime()))

    return d


# ---------------------------------------------------------------------------
# Read papers
# ---------------------------------------------------------------------------

def get_arxiv_papers(limit=100, offset=0, time_filter=None, search=None):
    """Get papers sorted by recency. Returns list of paper dicts."""
    pdb = CompressedSqliteDict(PAPERS_DB, tablename="papers", flag="r")
    papers = list(pdb.values())
    pdb.close()

    # Time filter (hours)
    if time_filter and time_filter > 0:
        cutoff = time.time() - time_filter * 3600
        papers = [p for p in papers if p.get("_time", 0) > cutoff]

    # Search filter
    if search:
        sq = search.lower()
        papers = [p for p in papers if sq in p.get("title", "").lower() or sq in p.get("summary", "").lower()]

    # Sort by time descending
    papers.sort(key=lambda p: p.get("_time", 0), reverse=True)

    total = len(papers)
    papers = papers[offset:offset + limit]

    # Slim down for API response
    result = []
    for p in papers:
        authors = [a.get("name", "") for a in p.get("authors", [])]
        tags = [t.get("term", "") for t in p.get("tags", [])]
        pdf_link = ""
        for link in p.get("links", []):
            if link.get("type") == "application/pdf":
                pdf_link = link.get("href", "")
                break
        result.append({
            "id": p.get("_id", ""),
            "title": p.get("title", "").replace("\n", " ").strip(),
            "summary": p.get("summary", "").replace("\n", " ").strip(),
            "authors": authors,
            "tags": tags,
            "time_str": p.get("_time_str", ""),
            "time": p.get("_time", 0),
            "pdf": pdf_link,
            "url": f"https://arxiv.org/abs/{p.get('_id', '')}",
        })

    return {"papers": result, "total": total}


def get_arxiv_paper(paper_id):
    """Get a single paper by ID."""
    pdb = CompressedSqliteDict(PAPERS_DB, tablename="papers", flag="r")
    paper = pdb.get(paper_id)
    pdb.close()
    if not paper:
        return None

    authors = [a.get("name", "") for a in paper.get("authors", [])]
    tags = [t.get("term", "") for t in paper.get("tags", [])]
    pdf_link = ""
    for link in paper.get("links", []):
        if link.get("type") == "application/pdf":
            pdf_link = link.get("href", "")
            break

    return {
        "id": paper.get("_id", ""),
        "title": paper.get("title", "").replace("\n", " ").strip(),
        "summary": paper.get("summary", "").strip(),
        "authors": authors,
        "tags": tags,
        "time_str": paper.get("_time_str", ""),
        "time": paper.get("_time", 0),
        "pdf": pdf_link,
        "url": f"https://arxiv.org/abs/{paper.get('_id', '')}",
    }


# ---------------------------------------------------------------------------
# TF-IDF features and similarity
# ---------------------------------------------------------------------------

def compute_arxiv_features():
    """Compute TF-IDF features for all papers."""
    pdb = CompressedSqliteDict(PAPERS_DB, tablename="papers", flag="r")
    papers = list(pdb.values())
    pdb.close()

    if len(papers) < 5:
        logger.warning("Too few papers to compute features")
        return

    pids = [p["_id"] for p in papers]
    texts = [p.get("title", "") + " " + p.get("summary", "") for p in papers]

    v = TfidfVectorizer(
        input="content",
        encoding="utf-8",
        strip_accents="unicode",
        lowercase=True,
        analyzer="word",
        stop_words="english",
        token_pattern=r"(?u)\b[a-zA-Z_][a-zA-Z0-9_]+\b",
        ngram_range=(1, 2),
        max_features=5000,
        norm="l2",
        sublinear_tf=True,
        max_df=0.1,
        min_df=5,
    )

    try:
        X = v.fit_transform(texts)
    except ValueError:
        # Not enough documents for min_df
        v.min_df = 2
        X = v.fit_transform(texts)

    features = {
        "pids": pids,
        "x": X,
        "vocab": v.vocabulary_,
        "idf": v.idf_,
    }

    with open(FEATURES_FILE, "wb") as f:
        pickle.dump(features, f, -1)

    logger.info(f"Computed arXiv features: {X.shape}")


def get_similar_papers(paper_id, n=10):
    """Find similar papers using TF-IDF cosine similarity."""
    if not os.path.exists(FEATURES_FILE):
        return []

    with open(FEATURES_FILE, "rb") as f:
        features = pickle.load(f)

    pids = features["pids"]
    X = features["x"]

    if paper_id not in pids:
        return []

    idx = pids.index(paper_id)
    vec = X[idx]
    sims = (X @ vec.T).toarray().flatten()
    sims[idx] = -1  # Exclude self

    top_idx = np.argsort(sims)[::-1][:n]

    pdb = CompressedSqliteDict(PAPERS_DB, tablename="papers", flag="r")
    results = []
    for i in top_idx:
        if sims[i] <= 0:
            break
        pid = pids[i]
        p = pdb.get(pid)
        if p:
            results.append({
                "id": pid,
                "title": p.get("title", "").replace("\n", " ").strip(),
                "tags": [t.get("term", "") for t in p.get("tags", [])],
                "time_str": p.get("_time_str", ""),
                "similarity": round(float(sims[i]), 3),
            })
    pdb.close()
    return results


# ---------------------------------------------------------------------------
# Save to Nikipedia wiki
# ---------------------------------------------------------------------------

def save_paper_to_wiki(paper_id, wiki_dir, db_path):
    """Save an arXiv paper as a raw entry + trigger it for compilation."""
    paper = get_arxiv_paper(paper_id)
    if not paper:
        return None

    raw_dir = Path("/opt/knowledge/raw")
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    raw_name = f"arxiv_{paper_id.replace('.', '_')}_{timestamp}.md"
    raw_path = str(raw_dir / raw_name)

    authors_str = ", ".join(paper["authors"][:10])
    tags_str = ", ".join(paper["tags"])

    md = f"""---
title: "{paper['title']}"
type: arxiv
source: "{paper['url']}"
arxiv_id: "{paper_id}"
date: {datetime.now().isoformat()}
---

# {paper['title']}

**Authors:** {authors_str}
**arXiv:** [{paper_id}]({paper['url']})
**Categories:** {tags_str}
**Published:** {paper['time_str']}

---

## Abstract

{paper['summary']}
"""

    Path(raw_path).write_text(md)

    # Insert as entry in the KB database
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "INSERT INTO entries (type, source, title, raw_path, status) VALUES (?, ?, ?, ?, 'ingested')",
        ("arxiv", paper["url"], paper["title"], raw_path)
    )
    entry_id = cur.lastrowid
    conn.commit()
    conn.close()

    return {"entry_id": entry_id, "title": paper["title"]}


# ---------------------------------------------------------------------------
# Migrate existing arxiv-sanity-lite data
# ---------------------------------------------------------------------------

def migrate_from_arxiv_sanity():
    """Import papers from the standalone arxiv-sanity-lite install if it exists."""
    old_db = "/opt/arxiv-sanity-lite/data/papers.db"
    if not os.path.exists(old_db):
        return 0

    old_pdb = CompressedSqliteDict(old_db, tablename="papers", flag="r")
    new_pdb = CompressedSqliteDict(PAPERS_DB, tablename="papers", flag="c", autocommit=True)
    new_mdb = SqliteDict(PAPERS_DB, tablename="metas", flag="c", autocommit=True)

    count = 0
    for k, v in old_pdb.items():
        if k not in new_pdb:
            new_pdb[k] = v
            new_mdb[k] = {"_time": v.get("_time", 0)}
            count += 1

    old_pdb.close()
    new_pdb.close()
    new_mdb.close()

    if count > 0:
        compute_arxiv_features()

    return count


if __name__ == "__main__":
    # CLI: fetch or migrate
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        n = migrate_from_arxiv_sanity()
        print(f"Migrated {n} papers")
    elif len(sys.argv) > 1 and sys.argv[1] == "fetch":
        num = int(sys.argv[2]) if len(sys.argv) > 2 else 200
        result = fetch_arxiv_papers(num=num)
        print(f"Fetched {result['new']} new papers, {result['total']} total")
    else:
        print("Usage: python arxiv_feed.py [fetch [N] | migrate]")
