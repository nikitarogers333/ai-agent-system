"""Compilation module -- LLM reads raw materials and produces structured wiki articles."""

import os
import re
import subprocess
import json
from pathlib import Path
from datetime import datetime


def slugify(title: str) -> str:
    """Convert a title to a URL-safe slug."""
    slug = title.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug.strip('-')[:80]


def compile_entries(entry: dict, existing_articles: list, wiki_dir: str) -> tuple:
    """
    Compile a single raw entry into wiki articles using Claude CLI.
    Returns (new_articles, updated_articles) where each is a list of dicts.
    """
    raw_path = entry.get("raw_path")
    if not raw_path or not Path(raw_path).exists():
        raise RuntimeError(f"Raw file not found: {raw_path}")

    raw_content = Path(raw_path).read_text()

    # Truncate if extremely long (keep first 80k chars)
    if len(raw_content) > 80000:
        raw_content = raw_content[:80000] + "\n\n[...truncated...]"

    # Build existing wiki context -- use FTS to find relevant articles instead of listing all
    existing_section = ""
    if existing_articles:
        # Extract keywords from raw content title for FTS search
        title = entry.get("title", "")
        # Get first 500 chars of raw content for keyword extraction
        raw_preview = raw_content[:500].replace("---", "").replace("\n", " ")
        search_terms = f"{title} {raw_preview}"[:200]

        # Try FTS search for relevant articles
        relevant = []
        try:
            import sqlite3 as _sql
            conn = _sql.connect(os.environ.get("KNOWLEDGE_DB", "/opt/agent-stack/knowledge/kb.db"))
            conn.row_factory = _sql.Row
            # Clean search terms for FTS (remove special chars)
            clean_terms = re.sub(r'[^\w\s]', ' ', search_terms)
            words = [w for w in clean_terms.split() if len(w) > 3][:15]
            fts_query = " OR ".join(words) if words else title
            rows = conn.execute(
                "SELECT slug, title, snippet(articles_fts, 2, '', '', '...', 20) as preview FROM articles_fts WHERE articles_fts MATCH ? ORDER BY rank LIMIT 20",
                (fts_query,)
            ).fetchall()
            relevant = [dict(r) for r in rows]
            conn.close()
        except Exception:
            # Fallback: use first 30 existing articles
            relevant = existing_articles[:30]

        if relevant:
            existing_section = "EXISTING WIKI (update these if relevant, use ===UPDATE: Title=== instead of ===ARTICLE:):\n"
            for art in relevant:
                existing_section += f"- [[{art['title']}]]: {art.get('preview', '')[:120]}\n"
            existing_section += "\n"
        elif existing_articles:
            # No FTS results, just list titles
            existing_section = "EXISTING WIKI TITLES (link to these where relevant):\n"
            for art in existing_articles[:50]:
                existing_section += f"- [[{art['title']}]]\n"
            existing_section += "\n"

    prompt = f"""Read the material below and create structured wiki articles. Output ONLY in this exact format, no preamble or commentary:

===ARTICLE: Article Title===
Categories: Category1, Category2
article content in markdown
===END===

Rules:
- Write encyclopedia-style articles with ## section headers
- Use [[Article Title]] for links between articles
- Create 1-5 focused articles depending on how many distinct topics the material covers
- Be thorough and detailed, not just summaries
- Never use em dashes
- The Categories line must be the FIRST line of each article (before any content). Pick 1-3 from: AI Business, AI Research, Technical Architecture, People, Tools & Frameworks, Strategy, Business Ideas, Marketing, Finance, or create a new one if none fit.
- Start your output immediately with ===ARTICLE:

{existing_section}MATERIAL ({entry.get('type', 'unknown')} from {entry.get('source', 'unknown')}):
---
{raw_content}
---

Output the wiki articles now:"""

    # Call Claude CLI
    result = subprocess.run(
        ["/root/.local/bin/claude", "-p", prompt],
        capture_output=True, text=True, timeout=600  # 10 min timeout
    )

    if result.returncode != 0:
        raise RuntimeError(f"Claude CLI failed (exit {result.returncode}): {result.stderr[:300]}")

    output = result.stdout.strip()
    if not output:
        raise RuntimeError("Claude CLI returned empty output")

    # Parse output
    new_articles = []
    updated_articles = []

    # Find all ARTICLE blocks
    article_pattern = r'===ARTICLE:\s*(.+?)===\s*\n(.*?)===END==='
    for match in re.finditer(article_pattern, output, re.DOTALL):
        title = match.group(1).strip()
        content = match.group(2).strip()
        slug = slugify(title)

        # Extract categories from first line
        categories = []
        content_lines = content.split("\n")
        if content_lines and content_lines[0].lower().startswith("categories:"):
            cat_line = content_lines[0].split(":", 1)[1].strip()
            categories = [c.strip() for c in cat_line.split(",") if c.strip()]
            content = "\n".join(content_lines[1:]).strip()

        # Write to wiki directory
        path = str(Path(wiki_dir) / f"{slug}.md")
        cats_yaml = json.dumps(categories)
        frontmatter = f"""---
title: "{title}"
slug: "{slug}"
categories: {cats_yaml}
created: {datetime.now().isoformat()}
sources:
  - "{entry.get('source', '')}"
---

"""
        Path(path).write_text(frontmatter + content)

        new_articles.append({
            "slug": slug,
            "title": title,
            "path": path,
            "preview": content[:200],
            "categories": categories
        })

    # Find all UPDATE blocks
    update_pattern = r'===UPDATE:\s*(.+?)===\s*\n(.*?)===END==='
    for match in re.finditer(update_pattern, output, re.DOTALL):
        title = match.group(1).strip()
        content = match.group(2).strip()

        # Find the existing article by title
        existing = None
        for art in existing_articles:
            if art["title"].lower() == title.lower():
                existing = art
                break

        if existing:
            slug = existing["slug"]
            path = existing["path"]
        else:
            # Treat as new if we can't find the existing one
            slug = slugify(title)
            path = str(Path(wiki_dir) / f"{slug}.md")

        # Read existing frontmatter if file exists
        if Path(path).exists():
            old_content = Path(path).read_text()
            if old_content.startswith("---"):
                end = old_content.find("---", 3)
                if end != -1:
                    frontmatter = old_content[:end+3] + "\n"
                    # Add updated timestamp
                    frontmatter = re.sub(
                        r'updated:.*\n', '', frontmatter
                    )
                    frontmatter = frontmatter.rstrip() + f"\nupdated: {datetime.now().isoformat()}\n---\n\n"
                    # Remove old closing ---
                    frontmatter = frontmatter.replace("---\n---", "---")
                    Path(path).write_text(frontmatter + content)
                else:
                    Path(path).write_text(content)
            else:
                Path(path).write_text(content)
        else:
            frontmatter = f"""---
title: "{title}"
slug: "{slug}"
created: {datetime.now().isoformat()}
sources:
  - "{entry.get('source', '')}"
---

"""
            Path(path).write_text(frontmatter + content)

        if existing:
            updated_articles.append({
                "slug": slug,
                "title": title,
                "path": path,
            })
        else:
            new_articles.append({
                "slug": slug,
                "title": title,
                "path": path,
                "preview": content[:200]
            })

    if not new_articles and not updated_articles:
        # The LLM might not have followed the format strictly.
        # Save the entire output as a single article.
        title = entry.get("title", "Untitled")
        slug = slugify(title)
        path = str(Path(wiki_dir) / f"{slug}.md")
        frontmatter = f"""---
title: "{title}"
slug: "{slug}"
created: {datetime.now().isoformat()}
sources:
  - "{entry.get('source', '')}"
---

"""
        Path(path).write_text(frontmatter + output)
        new_articles.append({
            "slug": slug,
            "title": title,
            "path": path,
            "preview": output[:200]
        })

    return (new_articles, updated_articles)


def lint_wiki(wiki_dir: str) -> str:
    """Run a linting pass on the wiki to find inconsistencies and missing connections."""
    wiki_path = Path(wiki_dir)
    articles = []

    for md_file in sorted(wiki_path.glob("*.md")):
        content = md_file.read_text()
        # Strip frontmatter
        body = content
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                body = content[end+3:].strip()

        # Extract title from frontmatter
        title = md_file.stem
        title_match = re.search(r'title:\s*"?(.+?)"?\s*$', content, re.MULTILINE)
        if title_match:
            title = title_match.group(1)

        # Find backlinks
        backlinks = re.findall(r'\[\[(.+?)\]\]', body)

        articles.append({
            "file": md_file.name,
            "title": title,
            "slug": md_file.stem,
            "content": body,
            "backlinks": backlinks,
            "char_count": len(body)
        })

    if not articles:
        return "No articles to lint."

    # Build lint report
    all_titles = {a["title"].lower() for a in articles}
    all_slugs = {a["slug"] for a in articles}

    issues = []
    for art in articles:
        for link in art["backlinks"]:
            if link.lower() not in all_titles and slugify(link) not in all_slugs:
                issues.append(f"Broken link: [[{link}]] in '{art['title']}'")
        if art["char_count"] < 100:
            issues.append(f"Stub article: '{art['title']}' ({art['char_count']} chars)")

    if not issues:
        return f"All clean. {len(articles)} articles, no issues found."

    # Build a summary for the LLM to fix
    article_list = "\n".join(
        f"- **{a['title']}** ({a['char_count']} chars, links to: {', '.join(a['backlinks']) or 'none'})"
        for a in articles
    )

    prompt = f"""You are maintaining a knowledge base wiki. Here is a health check report.

ARTICLES:
{article_list}

ISSUES FOUND:
{chr(10).join('- ' + i for i in issues)}

For each issue, suggest a fix. For broken links, suggest which existing article the link should point to (or suggest creating a new stub article). For stub articles, suggest what content should be added.

Don't use em dashes.

Be concise. Output one suggestion per line."""

    result = subprocess.run(
        ["/root/.local/bin/claude", "-p", prompt],
        capture_output=True, text=True, timeout=120
    )

    suggestions = result.stdout.strip() if result.returncode == 0 else "Lint check failed"

    # Save lint report
    report = f"""# Wiki Lint Report
Generated: {datetime.now().isoformat()}

## Issues ({len(issues)})
{"".join(chr(10) + "- " + i for i in issues)}

## Suggestions
{suggestions}
"""
    Path(wiki_dir).parent.joinpath("lint_report.md").write_text(report)

    return f"Found {len(issues)} issues across {len(articles)} articles. Report saved."
