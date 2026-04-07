"""Ingestion module -- converts URLs and files to raw markdown."""

import re
import subprocess
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse


def detect_type(url: str) -> str:
    """Detect the type of a URL."""
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        # Check if it's a channel URL
        if any(x in url_lower for x in ["/@", "/channel/", "/c/", "/user/"]):
            if "/watch" not in url_lower:
                return "youtube_channel"
        return "youtube"
    if "github.com" in url_lower:
        return "github"
    if "arxiv.org" in url_lower:
        return "arxiv"
    if url_lower.endswith(".pdf"):
        return "pdf"
    return "article"


def extract_youtube_id(url: str) -> str:
    """Extract video ID from a YouTube URL."""
    patterns = [
        r'(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:embed/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return url  # fallback: assume the whole thing is an ID


def ingest_url(url: str, entry_type: str, raw_dir: str) -> tuple:
    """
    Ingest a URL and save as raw markdown.
    Returns (title, raw_path).
    """
    if entry_type == "youtube_channel":
        return ingest_youtube_channel(url, raw_dir)
    elif entry_type == "youtube":
        return ingest_youtube(url, raw_dir)
    elif entry_type == "github":
        return ingest_github(url, raw_dir)
    elif entry_type == "arxiv":
        return ingest_article(url, raw_dir)
    else:
        return ingest_article(url, raw_dir)


def ingest_youtube(url: str, raw_dir: str) -> tuple:
    """Ingest a YouTube video transcript."""
    video_id = extract_youtube_id(url)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Get transcript
    result = subprocess.run(
        ["yt-transcript", url],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(f"yt-transcript failed: {result.stderr[:300]}")

    transcript = result.stdout.strip()
    if not transcript:
        raise RuntimeError("Empty transcript returned")

    # Try to extract title from first line or use video ID
    lines = transcript.split("\n")
    title = f"YouTube: {video_id}"
    # Often the first line is a title or metadata
    if lines and not lines[0].startswith("["):
        title = lines[0].strip()[:100]

    raw_name = f"youtube_{video_id}_{timestamp}.md"
    raw_path = str(Path(raw_dir) / raw_name)

    md = f"""---
title: "{title}"
type: youtube
source: "{url}"
video_id: "{video_id}"
date: {datetime.now().isoformat()}
---

# {title}

**Source:** {url}
**Type:** YouTube Video Transcript

---

{transcript}
"""
    Path(raw_path).write_text(md)
    return (title, raw_path)


def ingest_youtube_channel(url: str, raw_dir: str) -> tuple:
    """Ingest a YouTube channel -- list recent videos as a catalog entry."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Use yt-dlp to get channel video list
    result = subprocess.run(
        ["yt-dlp", "--flat-playlist", "--no-download",
         "--playlist-end", "30",
         "--print", "%(id)s\t%(title)s\t%(view_count)s\t%(upload_date)s",
         url],
        capture_output=True, text=True, timeout=60
    )

    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp channel fetch failed: {result.stderr[:300]}")

    lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
    if not lines:
        raise RuntimeError("No videos found on channel")

    # Extract channel name from URL
    parsed = urlparse(url)
    path_parts = parsed.path.strip("/").split("/")
    channel_name = path_parts[-1].lstrip("@") if path_parts else "unknown"
    title = f"YouTube Channel: {channel_name}"

    # Build video list
    video_list = []
    for line in lines:
        parts = line.split("\t")
        vid_id = parts[0] if len(parts) > 0 else ""
        vid_title = parts[1] if len(parts) > 1 else vid_id
        views = parts[2] if len(parts) > 2 else "?"
        date = parts[3] if len(parts) > 3 else "?"
        video_list.append(f"- **{vid_title}** ({views} views, {date}) https://youtube.com/watch?v={vid_id}")

    raw_name = f"youtube_channel_{channel_name}_{timestamp}.md"
    raw_path = str(Path(raw_dir) / raw_name)

    md = f"""---
title: "{title}"
type: youtube_channel
source: "{url}"
channel: "{channel_name}"
date: {datetime.now().isoformat()}
video_count: {len(lines)}
---

# {title}

**Source:** {url}
**Type:** YouTube Channel ({len(lines)} recent videos)

---

## Recent Videos

{chr(10).join(video_list)}
"""
    Path(raw_path).write_text(md)
    return (title, raw_path)


def ingest_github(url: str, raw_dir: str) -> tuple:
    """Ingest a GitHub repository or file."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    parsed = urlparse(url)
    path_parts = parsed.path.strip("/").split("/")

    owner = path_parts[0]

    # Handle profile pages (no repo specified)
    if len(path_parts) < 2 or path_parts[1] in ("", "?tab=repositories"):
        return ingest_github_profile(url, owner, raw_dir)

    repo = path_parts[1]
    title = f"GitHub: {owner}/{repo}"

    # Use gh CLI to get repo info
    repo_info = ""
    try:
        result = subprocess.run(
            ["gh", "repo", "view", f"{owner}/{repo}", "--json",
             "name,description,url,stargazerCount,forkCount,primaryLanguage,readme,repositoryTopics"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            title = f"GitHub: {data.get('name', repo)}"
            description = data.get("description", "No description")
            stars = data.get("stargazerCount", 0)
            forks = data.get("forkCount", 0)
            lang = data.get("primaryLanguage", {})
            lang_name = lang.get("name", "Unknown") if lang else "Unknown"
            topics = [t.get("name", "") for t in data.get("repositoryTopics", [])]
            readme = data.get("readme", "")

            repo_info = f"""## Repository Info

- **Description:** {description}
- **Stars:** {stars:,}
- **Forks:** {forks:,}
- **Primary Language:** {lang_name}
- **Topics:** {', '.join(topics) if topics else 'None'}

## README

{readme}
"""
    except Exception:
        pass

    # If gh failed, try the headless browser
    if not repo_info:
        try:
            result = subprocess.run(
                [os.environ.get("HEADLESS_BROWSER", "h.sh"), "nav", url],
                capture_output=True, text=True, timeout=30
            )
            import time
            time.sleep(3)
            result = subprocess.run(
                [os.environ.get("HEADLESS_BROWSER", "h.sh"), "extract"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                repo_info = data.get("data", {}).get("text", "")
        except Exception:
            repo_info = "(Could not fetch repository content)"

    # Check if URL points to a specific file (blob)
    specific_content = ""
    if len(path_parts) > 3 and path_parts[2] in ("blob", "tree"):
        file_path = "/".join(path_parts[3:])
        title = f"GitHub: {owner}/{repo}/{file_path}"
        try:
            # Try raw content URL
            branch = path_parts[3] if len(path_parts) > 3 else "main"
            remaining = "/".join(path_parts[4:]) if len(path_parts) > 4 else ""
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{remaining}"
            result = subprocess.run(
                ["curl", "-sL", raw_url],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode == 0 and result.stdout:
                specific_content = f"\n## File Content: {remaining}\n\n```\n{result.stdout[:10000]}\n```\n"
        except Exception:
            pass

    raw_name = f"github_{owner}_{repo}_{timestamp}.md"
    raw_path = str(Path(raw_dir) / raw_name)

    md = f"""---
title: "{title}"
type: github
source: "{url}"
owner: "{owner}"
repo: "{repo}"
date: {datetime.now().isoformat()}
---

# {title}

**Source:** {url}
**Type:** GitHub Repository

---

{repo_info}
{specific_content}
"""
    Path(raw_path).write_text(md)
    return (title, raw_path)


def ingest_github_profile(url: str, owner: str, raw_dir: str) -> tuple:
    """Ingest a GitHub user/org profile page with their top repos."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    title = f"GitHub Profile: {owner}"

    # Use gh CLI to list top repos
    repo_info = ""
    try:
        result = subprocess.run(
            ["gh", "repo", "list", owner, "--limit", "20", "--json",
             "name,description,stargazerCount,primaryLanguage,url,updatedAt",
             "--jq", '.[] | "- **\\(.name)** (\\(.stargazerCount) stars, \\(.primaryLanguage.name // "unknown")) - \\(.description // "no description") [\\(.url)]"'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            repo_info = f"## Repositories\n\n{result.stdout.strip()}"
    except Exception:
        pass

    # Also get user bio
    user_info = ""
    try:
        result = subprocess.run(
            ["gh", "api", f"users/{owner}", "--jq",
             '"**\\(.name // .login)** - \\(.bio // \"no bio\")\\n\\nFollowers: \\(.followers) | Following: \\(.following) | Public repos: \\(.public_repos)\\nLocation: \\(.location // \"unknown\") | Company: \\(.company // \"none\")"'],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            user_info = f"## Profile\n\n{result.stdout.strip()}"
    except Exception:
        pass

    if not repo_info and not user_info:
        repo_info = "(Could not fetch profile data)"

    raw_name = f"github_{owner}_profile_{timestamp}.md"
    raw_path = str(Path(raw_dir) / raw_name)

    md = f"""---
title: "{title}"
type: github
source: "{url}"
owner: "{owner}"
date: {datetime.now().isoformat()}
---

# {title}

**Source:** {url}
**Type:** GitHub User Profile

---

{user_info}

{repo_info}
"""
    Path(raw_path).write_text(md)
    return (title, raw_path)


def ingest_article(url: str, raw_dir: str) -> tuple:
    """Ingest a web article using the headless browser."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")

    title = f"Article: {domain}"
    content = ""

    # Use headless browser to extract
    try:
        subprocess.run(
            [os.environ.get("HEADLESS_BROWSER", "h.sh"), "nav", url],
            capture_output=True, text=True, timeout=30
        )
        import time
        time.sleep(4)

        result = subprocess.run(
            [os.environ.get("HEADLESS_BROWSER", "h.sh"), "extract"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            inner = data.get("data", {})
            page_title = inner.get("title", "").strip()
            content = inner.get("text", "").strip()
            if page_title:
                title = page_title
    except Exception as e:
        # Fallback: try curl + markdownify
        try:
            result = subprocess.run(
                ["curl", "-sL", "-A", "Mozilla/5.0", "--max-time", "15", url],
                capture_output=True, text=True, timeout=20
            )
            if result.returncode == 0 and result.stdout:
                from markdownify import markdownify as md_convert
                content = md_convert(result.stdout, strip=["script", "style", "nav", "footer"])
                # Try to extract title from HTML
                import re
                title_match = re.search(r'<title[^>]*>([^<]+)</title>', result.stdout, re.IGNORECASE)
                if title_match:
                    title = title_match.group(1).strip()
        except Exception:
            content = f"(Failed to fetch content: {str(e)[:200]})"

    if not content:
        content = "(No content could be extracted from this URL)"

    raw_name = f"article_{domain.replace('.', '_')}_{timestamp}.md"
    raw_path = str(Path(raw_dir) / raw_name)

    md = f"""---
title: "{title}"
type: article
source: "{url}"
domain: "{domain}"
date: {datetime.now().isoformat()}
---

# {title}

**Source:** {url}
**Type:** Web Article

---

{content}
"""
    Path(raw_path).write_text(md)
    return (title, raw_path)


def ingest_file(filepath: str, raw_dir: str) -> tuple:
    """Ingest an uploaded file (already saved)."""
    path = Path(filepath)
    title = path.stem
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    raw_name = f"file_{title}_{timestamp}.md"
    raw_path = str(Path(raw_dir) / raw_name)

    content = ""
    if path.suffix.lower() in ('.md', '.txt'):
        content = path.read_text(errors="replace")
    else:
        content = f"*Binary file: {path.name}*\n\nStored at: {filepath}"

    md = f"""---
title: "{title}"
type: file
source: "{filepath}"
date: {datetime.now().isoformat()}
---

# {title}

{content}
"""
    Path(raw_path).write_text(md)
    return (title, raw_path)
