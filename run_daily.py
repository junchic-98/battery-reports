"""
run_daily.py — Daily Battery Paper Report Agent
Fetch RSS feeds → deduplicate → score → generate HTML report → open in browser.

Usage:
    python run_daily.py

Edit config/journals.yaml, config/filters.yaml, data/keywords.json to customise.
"""

import sys, io
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import json, logging, re, sqlite3, time, webbrowser, os, math
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import feedparser, yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from Levenshtein import ratio as lev_ratio



# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("agent")

# ── Paths ─────────────────────────────────────────────────────────────────────
try:
    _file_path = __file__
except NameError:
    _file_path = 'fakepyfile'

if 'fakepyfile' in _file_path or _file_path == '<string>':
    ROOT = Path.cwd()
else:
    ROOT = Path(_file_path).parent
DB_PATH     = ROOT / "data" / "papers.db"
OUT_DIR     = ROOT / "out"
TEMPLATE    = ROOT / "template.html"
JOURNALS    = ROOT / "config" / "journals.yaml"
FILTERS     = ROOT / "config" / "filters.yaml"
KEYWORDS    = ROOT / "data" / "keywords.json"

# ── Data model ────────────────────────────────────────────────────────────────
@dataclass
class Paper:
    title:     str
    journal:   str
    url:       str
    doi:       Optional[str]          = None
    authors:   list[str]              = field(default_factory=list)
    abstract:  Optional[str]          = None
    published: Optional[datetime]     = None
    score:     float                  = 0.0

# ═══════════════════════════════════════════════════════════════════════════════
# FETCH
# ═══════════════════════════════════════════════════════════════════════════════
def _parse_date(entry) -> Optional[datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try: return datetime(*t[:6], tzinfo=timezone.utc)
            except: pass
    return None

def _extract_doi(entry) -> Optional[str]:
    for attr in ("prism_doi", "dc_identifier"):
        v = getattr(entry, attr, None)
        if v and str(v).startswith("10."): return str(v).strip()
    for link in getattr(entry, "links", []):
        if "doi.org/10." in link.get("href", ""):
            return link["href"].split("doi.org/")[-1].strip()
    eid = getattr(entry, "id", "") or ""
    if "doi.org/10." in eid: return eid.split("doi.org/")[-1].strip()
    return None

def _extract_authors(entry) -> list[str]:
    names = [a.get("name","").strip() for a in getattr(entry,"authors",[])]
    names = [n for n in names if n]
    if not names:
        single = getattr(entry, "author", "").strip()
        if single: names = [single]
    return names

def _extract_abstract(entry) -> Optional[str]:
    for attr in ("summary", "content"):
        val = getattr(entry, attr, None)
        if val is None: continue
        if isinstance(val, list): val = " ".join(v.get("value","") for v in val)
        text = " ".join(re.sub(r"<[^>]+>", " ", str(val)).split())
        if text: return text
    return None

def _sanitize_xml(raw: bytes) -> str:
    """Remove illegal XML characters that cause 'not well-formed' errors."""
    text = raw.decode("utf-8", errors="replace")
    # XML 1.0 legal chars: #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD]
    return re.sub(r'[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD]', '', text)

def _fetch_feed(url: str, max_retries: int = 3, backoff: float = 2.0):
    """Fetch and parse an RSS feed. Uses feedparser natively first; if that fails,
    falls back to requests library + XML sanitization (handles Springer bot-challenge)."""
    import requests as _req

    # Stage 1: Try feedparser's native URL fetching (handles most feeds well)
    for attempt in range(1, max_retries + 1):
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "BatteryPaperAgent/1.0"})
            if feed.get("bozo") and not feed.entries:
                raise RuntimeError(feed.get("bozo_exception", "Unknown parse error"))
            return feed
        except Exception as e:
            if attempt < max_retries:
                time.sleep(backoff)
            else:
                log.warning("    Native parse failed: %s — trying requests fallback...", e)

    # Stage 2: Use requests library (better TLS handling, bypasses some bot-challenges)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/rss+xml,application/xml,text/xml,*/*",
        }
        resp = _req.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        raw = resp.content
        # Detect bot-challenge HTML pages
        if raw.lstrip().startswith(b'<!DOCTYPE html') or raw.lstrip().startswith(b'<html'):
            raise RuntimeError("Server returned HTML bot-challenge page instead of RSS")
        clean_xml = _sanitize_xml(raw)
        feed = feedparser.parse(clean_xml)
        if feed.get("bozo") and not feed.entries:
            raise RuntimeError(feed.get("bozo_exception", "Sanitized parse also failed"))
        log.info("    Fallback fetch succeeded!")
        return feed
    except Exception as e:
        raise RuntimeError(f"Both native and fallback fetch failed: {e}")

def fetch_all(journals: list[dict], delay: float = 0.5) -> list[Paper]:
    enabled = [j for j in journals if j.get("enabled", True)]
    log.info("Fetching from %d journals...", len(enabled))
    papers = []
    for i, j in enumerate(enabled):
        name, url = j.get("name","?"), j.get("url","")
        log.info("  [%d/%d] %s", i+1, len(enabled), name)
        try:
            feed = _fetch_feed(url)
        except Exception as e:
            log.warning("    Feed failed after retries: %s", e); continue
        count_before = len(papers)
        for e in feed.entries:
            title = (getattr(e,"title","") or "").strip()
            link  = (getattr(e,"link","")  or "").strip()
            if not title or not link: continue
            
            abstract = _extract_abstract(e)
            if abstract:
                abstract = re.sub(rf"(?i)\b{re.escape(name)}\b", "", abstract).strip()
                abstract = re.sub(r"^[,\-\.\s]+", "", abstract)

            papers.append(Paper(title=title, journal=name, url=link,
                                doi=_extract_doi(e), authors=_extract_authors(e),
                                abstract=abstract, published=_parse_date(e)))
        log.info("    -> %d papers", len(papers) - count_before)
        if i < len(enabled)-1: time.sleep(delay)
    log.info("Total fetched: %d", len(papers))
    return papers

# ═══════════════════════════════════════════════════════════════════════════════
# DEDUPLICATE
# ═══════════════════════════════════════════════════════════════════════════════
_SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doi TEXT, title_norm TEXT NOT NULL, url TEXT NOT NULL,
    journal TEXT, seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doi   ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_title ON papers(title_norm);
"""

def _norm_title(t: str) -> str:
    return " ".join(re.sub(r"[^\w\s]"," ", t.lower()).split())

def deduplicate(papers: list[Paper], max_age_days: int = 30,
                sim_threshold: float = 0.85) -> list[Paper]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript(_SCHEMA); conn.commit()
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=max_age_days)).isoformat()
    cur.execute("SELECT title_norm FROM papers WHERE seen_at > ?", (cutoff,))
    existing = [r[0] for r in cur.fetchall()]

    unique, stats = [], {"old":0, "doi":0, "sim":0, "ok":0}
    for p in papers:
        if p.published and p.published < now - timedelta(days=max_age_days):
            stats["old"] += 1; continue
        n = _norm_title(p.title)
        if p.doi:
            cur.execute("SELECT 1 FROM papers WHERE doi=?", (p.doi,))
            if cur.fetchone(): stats["doi"] += 1; continue
        if any(lev_ratio(n, ex) >= sim_threshold for ex in existing):
            stats["sim"] += 1; continue
        cur.execute("INSERT INTO papers(doi,title_norm,url,journal,seen_at) VALUES(?,?,?,?,?)",
                    (p.doi, n, p.url, p.journal, now.isoformat()))
        existing.append(n)
        unique.append(p); stats["ok"] += 1

    conn.commit(); conn.close()
    log.info("Dedup: %d kept | %d too old | %d DOI dups | %d title dups",
             stats["ok"], stats["old"], stats["doi"], stats["sim"])
    return unique

# ═══════════════════════════════════════════════════════════════════════════════
# SCORE
# ═══════════════════════════════════════════════════════════════════════════════
_DASH = str.maketrans({"\u2013":"-","\u2014":"-","\u2012":"-",
                        "\u2212":"-","\u2010":"-","\u2011":"-"})

def _norm(s: str) -> str:
    return s.lower().translate(_DASH)


def score_all(papers: list[Paper]) -> list[Paper]:
    """Score papers based on keyword occurrences.

    Each keyword hit adds points based on its configured weight.
    An optional impact‑factor boost (log10 of the journal impact factor) is added.
    The final score is capped at 10.0.
    """
    if not KEYWORDS.exists():
        log.warning("keywords.json not found — scoring disabled.")
        return papers
    data = json.loads(KEYWORDS.read_text(encoding="utf-8"))

    if_path = ROOT / "data" / "impact_factors.json"
    ifs = json.loads(if_path.read_text(encoding="utf-8")) if if_path.exists() else {}

    def _highlight(text: str, kws: set) -> str:
        """Wrap keyword occurrences in <mark> tags for visual highlighting."""
        if not text or not kws:
            return text
        # Sort longer keywords first to avoid partial overlaps
        for kw in sorted(kws, key=len, reverse=True):
            esc = re.escape(kw)
            # Case‑insensitive whole‑word match
            text = re.sub(rf"(?i)(?<![\w-])({esc})(?![\w-])", r"<mark>\1</mark>", text)
        return text

    # Support both new 'concepts' format and old 'keywords' format
    if "concepts" in data:
        concepts = data["concepts"]
        max_possible = sum(c.get("weight", 0) * 2 for c in concepts.values())
        
        for p in papers:
            tt, ta = _norm(p.title or ""), _norm(p.abstract or "")
            raw = 0
            hits = set()
            for c_name, c_data in concepts.items():
                w = c_data.get("weight", 0)
                kws = sorted(c_data.get("keywords", []), key=lambda x: len(_norm(x)), reverse=True)
                
                hit_title = False
                for k in kws:
                    nk = _norm(k)
                    pattern = rf"(?<![\w-]){re.escape(nk)}(?![\w-])"
                    if re.search(pattern, tt):
                        raw += w * 2
                        tt = re.sub(pattern, " ", tt)
                        hits.add(k)
                        hit_title = True
                        break # Only count concept once
                
                if hit_title:
                    continue
                
                for k in kws:
                    nk = _norm(k)
                    pattern = rf"(?<![\w-]){re.escape(nk)}(?![\w-])"
                    if re.search(pattern, ta):
                        raw += w
                        ta = re.sub(pattern, " ", ta)
                        hits.add(k)
                        break # Only count concept once
            
            p.score = round(min(10.0, (raw / max_possible) * 30), 1) if max_possible else 0.0
            if p.score > 0.0:
                jname = (p.journal or "").lower().strip()
                j_score = ifs.get(jname, 0.0)
                if j_score > 1.0:
                    p.score = round(min(10.0, p.score + math.log10(j_score)), 1)

            if p.abstract and len(p.abstract) > 600:
                p.abstract = p.abstract[:600] + "…"
            p.title = _highlight(p.title, hits)
            p.abstract = _highlight(p.abstract, hits)

    else:
        # Fallback to old format
        kws = data.get("keywords", data) if isinstance(data, dict) else {}
        if not kws: return papers
        max_possible = sum(w * 2 for w in kws.values())
        sorted_kws = sorted(kws.items(), key=lambda x: len(_norm(x[0])), reverse=True)
        for p in papers:
            tt, ta = _norm(p.title or ""), _norm(p.abstract or "")
            raw = 0
            hits = set()
            for k, w in sorted_kws:
                nk = _norm(k)
                pattern = rf"(?<![\w-]){re.escape(nk)}(?![\w-])"
                if re.search(pattern, tt):
                    raw += w * 2
                    tt = re.sub(pattern, " ", tt)
                    hits.add(k)
                elif re.search(pattern, ta):
                    raw += w
                    ta = re.sub(pattern, " ", ta)
                    hits.add(k)
            p.score = round(min(10.0, (raw / max_possible) * 30), 1) if max_possible else 0.0
            if p.score > 0.0:
                jname = (p.journal or "").lower().strip()
                j_score = ifs.get(jname, 0.0)
                if j_score > 1.0:
                    p.score = round(min(10.0, p.score + math.log10(j_score)), 1)

            if p.abstract and len(p.abstract) > 600:
                p.abstract = p.abstract[:600] + "…"
            p.title = _highlight(p.title, hits)
            p.abstract = _highlight(p.abstract, hits)

    high = sum(1 for p in papers if p.score >= 5.0)
    log.info("Scored %d papers (%d with score >= 5.0)", len(papers), high)
    return papers

# ═══════════════════════════════════════════════════════════════════════════════
# REPORT
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_date(dt) -> str:
    if isinstance(dt, str):
        try: dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except: return dt
    return dt.strftime("%b %d, %Y") if dt else "Unknown date"

def generate_report(papers: list[Paper]) -> Path:
    date    = datetime.now(timezone.utc)
    date_str = date.strftime("%Y-%m-%d")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    html_path = OUT_DIR / f"{date_str}.html"
    json_path = OUT_DIR / f"{date_str}.json"

    # --- Load & Merge with Daily Cache ---
    existing = {}
    if json_path.exists():
        try:
            for item in json.loads(json_path.read_text(encoding="utf-8")):
                p = Paper(**item)
                if isinstance(p.published, str):
                    try: p.published = datetime.fromisoformat(p.published)
                    except: p.published = None
                existing[p.url] = p
        except Exception as e:
            log.warning("Could not load daily json cache: %s", e)

    for p in papers:
        existing[p.url] = p

    papers = list(existing.values())
    
    def json_default(obj):
        if isinstance(obj, datetime): return obj.isoformat()
        raise TypeError(f"Not serializable: {type(obj)}")

    try:
        json_path.write_text(json.dumps([asdict(p) for p in papers], default=json_default, separators=(',', ':')), encoding="utf-8")
    except Exception as e:
        log.warning("Could not save daily json cache: %s", e)
    # ---------------------------------------
    past_reports = sorted([f.stem for f in OUT_DIR.glob("*.html") if "index" not in f.name and re.match(r"\d{4}-\d{2}-\d{2}", f.stem)], reverse=True)
    all_reports = sorted(list(set(past_reports + [date_str])), reverse=True)
    
    # Write a dynamic JS file so old static reports can dynamically fetch the latest sidebar
    (OUT_DIR / "sidebar.js").write_text(f"const ALL_REPORTS = {json.dumps(all_reports)};", encoding="utf-8")

    # 1) Generate search_index.json (slim: no full abstract)
    search_index = []
    for report_date in all_reports:
        fpath = OUT_DIR / f"{report_date}.json"
        if fpath.exists():
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                for item in data:
                    search_index.append({
                        'title': item.get('title', ''),
                        'journal': item.get('journal', ''),
                        'authors': item.get('authors', []),
                        'score': item.get('score', 0),
                        'url': item.get('url', ''),
                        'doi': item.get('doi'),
                        'published': item.get('published'),
                        'report_date': report_date,
                        'abstract_snippet': (item.get('abstract') or '')[:200],
                    })
            except Exception as e:
                log.warning("Could not load %s for search index: %s", fpath, e)
    try:
        (OUT_DIR / "search_index.json").write_text(json.dumps(search_index, default=json_default, separators=(',', ':')), encoding="utf-8")
        log.info("Search index: %d entries, %.1f KB", len(search_index), len(json.dumps(search_index, default=json_default, separators=(',', ':'))) / 1024)
    except Exception as e:
        log.warning("Could not save search_index.json: %s", e)

    # Collect weekly reports for sidebar
    weekly_reports = sorted([f.stem for f in OUT_DIR.glob("weekly-*.html")], reverse=True)

    # 2) Generate Weekly Report (if today is Sunday)
    if date.weekday() == 6:
        weekly_papers = []
        for i in range(7):
            d_str = (date - timedelta(days=i)).strftime("%Y-%m-%d")
            fpath = OUT_DIR / f"{d_str}.json"
            if fpath.exists():
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    for item in data:
                        p = Paper(**item)
                        if p.score >= 7.0:
                            weekly_papers.append(p)
                except: pass
                
        seen_doi, seen_title = set(), set()
        unique_weekly = []
        for p in weekly_papers:
            if p.doi and p.doi in seen_doi: continue
            if p.title and p.title.lower() in seen_title: continue
            if p.doi: seen_doi.add(p.doi)
            if p.title: seen_title.add(p.title.lower())
            unique_weekly.append(p)
            
        unique_weekly.sort(key=lambda x: x.score, reverse=True)
        
        env_wk = Environment(loader=FileSystemLoader(str(ROOT)), autoescape=select_autoescape(["html"]))
        env_wk.filters["fmt_date"] = _fmt_date
        tmpl_wk = env_wk.get_template("template.html")
        
        wk_out = tmpl_wk.render(date=date, date_str=f"Weekly Top Papers ({date_str})",
                           papers=unique_weekly,
                           journal_counts={}, 
                           total=len(unique_weekly),
                           past_reports=past_reports, 
                           weekly_reports=weekly_reports, 
                           is_index=False)
        wk_path = OUT_DIR / f"weekly-{date_str}.html"
        wk_path.write_text(wk_out, encoding="utf-8")
        log.info("Weekly report generated: %s", wk_path)
        if f"weekly-{date_str}" not in weekly_reports:
            weekly_reports.insert(0, f"weekly-{date_str}")

    sorted_papers = sorted(papers, key=lambda p: p.score, reverse=True)
    journal_counts = {}
    for p in papers:
        journal_counts[p.journal] = journal_counts.get(p.journal, 0) + 1

    env = Environment(loader=FileSystemLoader(str(ROOT)),
                      autoescape=select_autoescape(["html"]))
    env.filters["fmt_date"] = _fmt_date

    tmpl = env.get_template("template.html")
    html_out = tmpl.render(date=date, date_str=date_str,
                       papers=sorted_papers,
                       journal_counts=dict(sorted(journal_counts.items())),
                       total=len(papers),
                       past_reports=past_reports, weekly_reports=weekly_reports, is_index=False)
                       
    html_index = tmpl.render(date=date, date_str=date_str,
                       papers=sorted_papers,
                       journal_counts=dict(sorted(journal_counts.items())),
                       total=len(papers),
                       past_reports=past_reports, weekly_reports=weekly_reports, is_index=True)

    html_path.write_text(html_out, encoding="utf-8")
    
    # Also save as index.html for web hosting (e.g., GitHub Pages)
    index_path = ROOT / "index.html"
    index_path.write_text(html_index, encoding="utf-8")
    
    log.info("Report: %s (and index.html)", html_path)
    return html_path

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
def apply_custom_rules(papers: list[Paper]) -> list[Paper]:
    """Exclude Li/Na/K-ion papers except for battery-related ones in top journals."""
    top_journals = {"nature", "nature energy", "nature chemistry", "science"}
    
    def is_unwanted_ion(p: Paper) -> bool:
        text = (p.title + " " + (p.abstract or "")).lower()
        unwanted_kws = [
            "lithium-ion", "li-ion", "lithium ion",
            "sodium-ion", "na-ion", "sodium ion",
            "potassium-ion", "k-ion", "potassium ion"
        ]
        if any(kw in text for kw in unwanted_kws):
            return True
        if re.search(r'\b(lib|libs|sib|sibs|pib|pibs|kib|kibs)\b', text):
            return True
        return False
        
    def is_battery(p: Paper) -> bool:
        text = (p.title + " " + (p.abstract or "")).lower()
        return bool(re.search(r'\b(batteries|battery|batter|anode|cathode|electrolyte|electrolytes|energy storage)\b', text))

    filtered = []
    dropped = 0
    for p in papers:
        j = (p.journal or "").lower().strip()
        unwanted = is_unwanted_ion(p)
        bat = is_battery(p)
        
        if j in top_journals:
            if bat:
                p.score = max(p.score, 10.0)
                filtered.append(p)
            else:
                dropped += 1
        elif unwanted:
            dropped += 1
        else:
            filtered.append(p)
            
    if dropped > 0:
        log.info("Custom rule: excluded %d unwanted ion or off-topic top-journal papers.", dropped)
    return filtered

def main():
    """Main entry point for the daily report.
    Loads configuration, fetches papers, applies filtering, scoring, and generates the HTML report.
    """
    # Load journal config
    if not JOURNALS.exists():
        print("[ERROR] config/journals.yaml not found."); sys.exit(1)
    journals = yaml.safe_load(JOURNALS.read_text(encoding="utf-8")).get("journals", [])

    # Load optional report-specific config (overrides defaults)
    report_cfg_path = ROOT / "config" / "report_config.json"
    if report_cfg_path.exists():
        rcfg = json.loads(report_cfg_path.read_text(encoding="utf-8"))
    else:
        rcfg = {}
    # Defaults: keep top 20 papers with score >= 1.0
    max_age = rcfg.get("max_age_days", 30)
    min_score = rcfg.get("min_score", 1.0)
    top_n = rcfg.get("top_n", 20)

    # Optional title‑exclusion filter from filters.yaml
    exclude = []
    if FILTERS.exists():
        filters_yaml = yaml.safe_load(FILTERS.read_text(encoding="utf-8"))
        exclude = [t.lower() for t in filters_yaml.get("exclusion", {}).get("exclude_if_title_contains", [])]

    # Fetch papers
    papers = fetch_all(journals)
    if not papers:
        print("[WARN] No papers fetched."); sys.exit(0)

    # Title exclusion filter
    if exclude:
        before = len(papers)
        papers = [p for p in papers if not any(e in p.title.lower() for e in exclude)]
        log.info("Title filter: removed %d papers.", before - len(papers))

    papers = deduplicate(papers, max_age_days=max_age)
    if not papers:
        print("[WARN] No new papers after dedup. Proceeding to regenerate report anyway.")

    # Score papers
    papers = score_all(papers)

    papers = apply_custom_rules(papers)

    # Apply minimum score filter
    if min_score > 0:
        before = len(papers)
        papers = [p for p in papers if p.score >= min_score]
        log.info("Score filter: dropped %d off‑topic papers. %d remain.", before - len(papers), len(papers))
    if not papers:
        print("[WARN] No papers above minimum_score threshold."); sys.exit(0)

    # Keep only top N papers (default 20)
    if top_n > 0 and len(papers) > top_n:
        papers = sorted(papers, key=lambda p: p.score, reverse=True)[:top_n]
        log.info("Kept top %d papers.", top_n)

    # Generate the HTML report
    html_path = generate_report(papers)

    print(f"\n[OK] {len(papers)} papers | Report: {html_path}\n")

    # Open in default browser
    try:
        webbrowser.open(html_path.as_uri())
    except Exception as e:
        log.warning("Could not open browser: %s", e)

    print("Done!")

if __name__ == "__main__":
    main()
