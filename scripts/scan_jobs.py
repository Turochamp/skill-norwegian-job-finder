#!/usr/bin/env python3
"""
Norwegian Job Finder — search aggregation script.

Uses Serper.dev (Google Search) for X-ray/board queries (reliable site: filtering)
and Brave Search for signal queries. Deduplicates against 30-day history.
Outputs structured JSON for the LLM to score, filter, and format.

Usage:
  python3 scan_jobs.py --config erin-gallup.json
  python3 scan_jobs.py --config michael-ai-leadership.json --verbose
  python3 scan_jobs.py --config erin-gallup.json --dry-run
  python3 scan_jobs.py --test
  python3 scan_jobs.py --test --test-only build_queries

stdout: JSON  |  stderr: logging  |  exit 0: ok  |  exit 1: failure
"""

import argparse
import gzip
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

SKILL_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = SKILL_DIR.parent.parent  # ~/.openclaw/workspace
MEMORY_DIR = WORKSPACE_DIR / "memory"
OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"

BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
SERPER_API_URL = "https://google.serper.dev/search"
DEDUP_WINDOW_DAYS = 30

# Domains that are aggregators, not direct job sources
AGGREGATOR_DOMAINS = {
    "glassdoor.com", "indeed.com", "jooble.org", "jobtoday.com",
    "salary.com", "ziprecruiter.com", "simplyhired.com", "monster.com",
    "careerbuilder.com",
}

log = logging.getLogger("scan_jobs")


def setup_logging(verbose=False, quiet=False):
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(name)s [%(levelname)s] %(message)s",
    )


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path):
    """Load and validate a job finder config JSON."""
    with open(config_path) as f:
        cfg = json.load(f)

    required = ["name", "person", "titles", "locations", "sources"]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise ValueError(f"Config missing required keys: {missing}")

    if not cfg.get("enabled", True):
        raise ValueError(f"Config {cfg['name']} is disabled")

    return cfg


def _load_openclaw_config():
    """Load openclaw.json."""
    with open(OPENCLAW_CONFIG) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Search APIs
# ---------------------------------------------------------------------------

def get_brave_api_key():
    """Load Brave API key from openclaw.json."""
    config = _load_openclaw_config()
    try:
        return config["plugins"]["entries"]["brave"]["config"]["webSearch"]["apiKey"]
    except KeyError:
        raise ValueError("Brave API key not configured in openclaw.json")


def get_serper_api_key():
    """Load Serper API key from openclaw.json."""
    config = _load_openclaw_config()
    try:
        return config["plugins"]["entries"]["serper"]["config"]["apiKey"]
    except KeyError:
        raise ValueError("Serper API key not configured in openclaw.json")


def brave_search(query, api_key, count=10, country="NO", freshness="month"):
    """Call Brave Search API. Returns list of result dicts or empty list."""
    params = urllib.parse.urlencode({
        "q": query,
        "count": count,
        "country": country,
        "freshness": freshness,
    })
    url = f"{BRAVE_API_URL}?{params}"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "X-Subscription-Token": api_key,
    }

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8"))
            results = data.get("web", {}).get("results", [])
            log.debug("[brave] %r → %d results", query, len(results))
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "description": r.get("description", ""),
                    "age": r.get("age", ""),
                }
                for r in results
            ]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        if e.code in (401, 403):
            log.error("Brave API auth error %d: %s", e.code, body)
            raise
        log.warning("Brave API error %d for %r: %s", e.code, query, body)
        return []
    except Exception as e:
        log.warning("Brave search failed for %r: %s", query, e)
        return []


def serper_search(query, api_key, count=10, gl="no"):
    """Call Serper.dev Google Search API. Returns list of result dicts."""
    payload = json.dumps({
        "q": query,
        "num": min(count, 10),
        "gl": gl,
    }).encode("utf-8")

    headers = {
        "X-API-KEY": api_key,
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(SERPER_API_URL, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("organic", [])
            log.debug("[serper] %r → %d results", query, len(results))
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("link", ""),
                    "description": r.get("snippet", ""),
                    "age": r.get("date", ""),
                }
                for r in results
            ]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        if e.code in (401, 403):
            log.error("Serper API auth error %d: %s", e.code, body)
            raise
        log.warning("Serper API error %d for %r: %s", e.code, query, body)
        return []
    except Exception as e:
        log.warning("Serper search failed for %r: %s", query, e)
        return []


# ---------------------------------------------------------------------------
# Result filtering
# ---------------------------------------------------------------------------

def get_domain(url):
    """Extract the registrable domain from a URL."""
    try:
        host = urlparse(url).hostname or ""
        parts = host.split(".")
        if len(parts) >= 3 and parts[-2] == "nav":
            return ".".join(parts[-3:])
        return ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:
        return ""


def is_aggregator(url):
    """Check if URL is from a known job aggregator."""
    return get_domain(url) in AGGREGATOR_DOMAINS


def filter_signals(results):
    """Remove low-quality signal results: homepages, /apply pages."""
    filtered = []
    for r in results:
        url = r["url"]
        path = urlparse(url).path.rstrip("/")

        if not path or path == "/":
            log.debug("Signal filtered (homepage): %s", url[:80])
            continue

        skip_paths = ["/apply", "/about", "/contact", "/careers", "/jobs"]
        if any(path == p or path.startswith(p + "/") for p in skip_paths):
            log.debug("Signal filtered (generic path): %s", url[:80])
            continue

        filtered.append(r)
    return filtered


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------

def _split_titles(cfg):
    """Split titles into English and Norwegian lists."""
    norwegian_markers = set("æøåÆØÅ")
    norwegian_suffixes = {"leder", "sjef", "direktør", "ansvarlig", "rådgiver"}

    titles_no = []
    titles_en = []
    for t in cfg["titles"]:
        is_norwegian = (
            any(c in norwegian_markers for c in t)
            or t[0].islower()
            or any(t.lower().endswith(w) for w in norwegian_suffixes)
        )
        if is_norwegian:
            titles_no.append(t)
        else:
            titles_en.append(t)

    return titles_en[:5], titles_no[:5]


def build_ats_queries(cfg):
    """Build ATS X-Ray queries using Serper (Google).

    Uses site: operator in the query string — reliable via Google/Serper.
    """
    queries = []
    for q in cfg["sources"].get("ats_xray", []):
        queries.append({
            "phase": "ats_xray",
            "engine": "serper",
            "query": q,
        })
    return queries


def build_board_queries(cfg):
    """Build job board queries using Serper (Google) with site: operator."""
    queries = []
    titles_en, titles_no = _split_titles(cfg)
    location_str = cfg["locations"][0] if cfg["locations"] else "Norway"

    en_str = " OR ".join(f'"{t}"' for t in titles_en[:3]) if titles_en else ""
    no_str = " OR ".join(f'"{t}"' for t in titles_no[:3]) if titles_no else ""

    board_domains = {
        "jobbnorge": "jobbnorge.no",
        "finn": "finn.no",
        "linkedin": "linkedin.com/jobs",
    }
    # Always include arbeidsplassen
    extra_boards = [("arbeidsplassen", "arbeidsplassen.nav.no")]

    all_boards = extra_boards + [
        (b, board_domains[b])
        for b in cfg["sources"].get("boards", [])
        if b in board_domains
    ]

    for board_name, domain in all_boards:
        if en_str:
            queries.append({
                "phase": f"board_{board_name}",
                "engine": "serper",
                "query": f"site:{domain} ({en_str}) {location_str}",
            })
        if no_str and board_name != "linkedin":
            queries.append({
                "phase": f"board_{board_name}_no",
                "engine": "serper",
                "query": f"site:{domain} ({no_str})",
            })

    return queries


def build_signal_queries(cfg):
    """Build signal queries using Brave (site: works for news/VC sites)."""
    queries = []
    signals = cfg["sources"].get("signals", {})

    signal_titles = cfg["titles"][:4]
    title_or = " OR ".join(f'"{t}"' for t in signal_titles)

    for source in signals.get("leadership_changes", []):
        queries.append({
            "phase": "signal_leadership",
            "engine": "brave",
            "query": f"site:{source} ({title_or}) (appointed OR hired OR joins)",
            "freshness": "week",
            "count": 5,
        })

    for source in signals.get("funded_companies", []):
        queries.append({
            "phase": "signal_funding",
            "engine": "brave",
            "query": f'site:{source} (funding OR "series A" OR "series B" OR "raised") Norway',
            "freshness": "week",
            "count": 5,
        })

    for source in signals.get("startup_ecosystems", []):
        queries.append({
            "phase": "signal_startup",
            "engine": "brave",
            "query": f'site:{source} (funding OR startup OR launch OR accelerator) Norway',
            "freshness": "week",
            "count": 5,
        })

    return queries


def build_all_queries(cfg):
    """Build all query phases for a config."""
    return build_ats_queries(cfg) + build_board_queries(cfg) + build_signal_queries(cfg)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def load_seen_urls(slug):
    """Load URLs seen in the last 30 days for this slug from memory files."""
    seen = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=DEDUP_WINDOW_DAYS)

    if not MEMORY_DIR.exists():
        return seen

    for f in sorted(MEMORY_DIR.glob("????-??-??.md")):
        try:
            file_date = datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if file_date < cutoff:
            continue

        content = f.read_text(errors="replace")
        in_slug_section = False
        for line in content.splitlines():
            if slug in line:
                in_slug_section = True
            elif line.startswith("#"):
                in_slug_section = False
            if in_slug_section and "http" in line:
                for word in line.split():
                    if word.startswith("http"):
                        seen.add(word.rstrip(",;)>—"))

    log.info("Loaded %d seen URLs for slug %r (last %d days)", len(seen), slug, DEDUP_WINDOW_DAYS)
    return seen


def dedup_results(results, seen_urls):
    """Remove already-seen URLs from results. Returns (new, skipped_count)."""
    new = []
    skipped = 0
    seen_in_batch = set()
    for r in results:
        url = r["url"]
        if url in seen_urls or url in seen_in_batch:
            skipped += 1
            continue
        seen_in_batch.add(url)
        new.append(r)
    return new, skipped


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def run_scan(config_path, dry_run=False):
    """Run the full scan for one config file. Returns output dict."""
    cfg = load_config(config_path)
    brave_key = get_brave_api_key()
    serper_key = get_serper_api_key()

    slug = cfg["name"]
    person = cfg["person"]
    queries = build_all_queries(cfg)

    log.info("Scanning for %s (%s): %d queries", person, slug, len(queries))

    all_results = {"jobs": [], "signals": []}
    query_stats = {
        "total": len(queries),
        "serper_queries": 0,
        "brave_queries": 0,
        "succeeded": 0,
        "failed": 0,
        "empty": 0,
    }

    for q in queries:
        phase = q["phase"]
        engine = q.get("engine", "brave")
        query_str = q["query"]

        if engine == "serper":
            results = serper_search(query_str, serper_key, count=q.get("count", 10))
            query_stats["serper_queries"] += 1
        else:
            results = brave_search(
                query_str, brave_key,
                count=q.get("count", 10),
                freshness=q.get("freshness", "month"),
            )
            query_stats["brave_queries"] += 1

        if results:
            query_stats["succeeded"] += 1
            is_signal = "signal" in phase
            bucket = "signals" if is_signal else "jobs"

            if is_signal:
                results = filter_signals(results)

            for r in results:
                r["source_phase"] = phase
                r["source_query"] = query_str
            all_results[bucket].extend(results)
        else:
            query_stats["empty"] += 1

        time.sleep(0.3)

    # Remove aggregator results from jobs
    pre_filter = len(all_results["jobs"])
    all_results["jobs"] = [r for r in all_results["jobs"] if not is_aggregator(r["url"])]
    aggregator_filtered = pre_filter - len(all_results["jobs"])
    if aggregator_filtered:
        log.info("Filtered %d aggregator results", aggregator_filtered)

    # Dedup
    seen_urls = load_seen_urls(slug)
    new_jobs, jobs_skipped = dedup_results(all_results["jobs"], seen_urls)
    new_signals, signals_skipped = dedup_results(all_results["signals"], seen_urls)

    log.info(
        "Results for %s: %d new jobs (%d deduped, %d aggregator-filtered), "
        "%d new signals (%d deduped)",
        person, len(new_jobs), jobs_skipped, aggregator_filtered,
        len(new_signals), signals_skipped,
    )

    return {
        "status": "ok",
        "config": {
            "name": slug,
            "person": person,
            "titles": cfg["titles"],
            "locations": cfg["locations"],
            "industries": cfg.get("industries", []),
            "dealbreakers": cfg.get("dealbreakers", []),
            "min_score": cfg.get("min_score", 50),
            "notes": cfg.get("notes", ""),
        },
        "jobs": new_jobs,
        "signals": new_signals,
        "query_stats": query_stats,
        "dedup": {
            "jobs_skipped": jobs_skipped,
            "signals_skipped": signals_skipped,
            "aggregator_filtered": aggregator_filtered,
            "seen_urls_loaded": len(seen_urls),
        },
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def run_tests(test_only=None, verbose=False):
    """Run inline tests."""
    tests = {
        "build_queries": test_build_queries,
        "split_titles": test_split_titles,
        "domain_extraction": test_domain_extraction,
        "aggregator_filter": test_aggregator_filter,
        "signal_filter": test_signal_filter,
        "dedup": test_dedup,
        "load_config": test_load_config,
        "api_keys": test_api_keys,
    }

    if test_only:
        if test_only not in tests:
            print(f"Unknown test: {test_only}. Available: {', '.join(tests)}", file=sys.stderr)
            sys.exit(1)
        tests = {test_only: tests[test_only]}

    passed = failed = 0
    for name, fn in tests.items():
        try:
            msg = fn()
            print(f"[ok] {name}: {msg}")
            passed += 1
        except Exception as e:
            print(f"[FAIL] {name}: {e}")
            failed += 1

    total = passed + failed
    print(f"\n{passed}/{total} passed")
    sys.exit(1 if failed > 0 else 0)


def test_build_queries():
    cfg = {
        "name": "test",
        "person": "Test",
        "titles": ["Head of AI", "AI Lead", "Designleder", "UX-leder"],
        "locations": ["Oslo", "Norway"],
        "sources": {
            "ats_xray": [
                'site:teamtailor.com "head of AI" "oslo"',
                'site:jobbnorge.no "kunstig intelligens"',
            ],
            "boards": ["jobbnorge", "finn", "linkedin"],
            "signals": {
                "leadership_changes": ["digi.no"],
                "funded_companies": ["shifter.no"],
            },
        },
    }
    queries = build_all_queries(cfg)

    serper_qs = [q for q in queries if q.get("engine") == "serper"]
    brave_qs = [q for q in queries if q.get("engine") == "brave"]
    assert len(serper_qs) > 0, "No Serper queries"
    assert len(brave_qs) > 0, "No Brave queries"

    # ATS and board queries should use Serper
    for q in queries:
        if q["phase"].startswith("ats_") or q["phase"].startswith("board_"):
            assert q["engine"] == "serper", f"{q['phase']} should use serper"

    # Signal queries should use Brave
    for q in queries:
        if q["phase"].startswith("signal_"):
            assert q["engine"] == "brave", f"{q['phase']} should use brave"

    # ATS queries should contain site: in query string
    ats_qs = [q for q in queries if q["phase"] == "ats_xray"]
    assert "site:teamtailor.com" in ats_qs[0]["query"]

    # Board queries should use site: (reliable via Google/Serper)
    board_qs = [q for q in queries if q["phase"].startswith("board_")]
    for q in board_qs:
        assert "site:" in q["query"], f"Board query {q['phase']} missing site: → {q['query']}"

    # Arbeidsplassen should always be included
    phases = [q["phase"] for q in queries]
    assert "board_arbeidsplassen" in phases, "Missing arbeidsplassen board"

    return f"{len(queries)} queries ({len(serper_qs)} Serper, {len(brave_qs)} Brave)"


def test_split_titles():
    cfg = {"titles": [
        "Head of AI", "AI Lead",
        "Designleder", "UX-leder", "Kreativ leder",
        "Produktleder",
    ]}
    en, no = _split_titles(cfg)
    assert "Head of AI" in en
    assert "Designleder" in no
    assert "UX-leder" in no
    return f"{len(en)} English, {len(no)} Norwegian"


def test_domain_extraction():
    assert get_domain("https://www.jobbnorge.no/ledige-stillinger/123") == "jobbnorge.no"
    assert get_domain("https://arbeidsplassen.nav.no/stilling/abc") == "arbeidsplassen.nav.no"
    assert get_domain("https://www.glassdoor.com/Job/norway") == "glassdoor.com"
    assert get_domain("https://finn.no/jobb/123") == "finn.no"
    return "all domain extractions correct"


def test_aggregator_filter():
    assert is_aggregator("https://www.glassdoor.com/Job/norway-design-jobs.htm")
    assert is_aggregator("https://no.jooble.org/jobb-designer/Oslo")
    assert not is_aggregator("https://www.jobbnorge.no/ledige-stillinger/123")
    assert not is_aggregator("https://arbeidsplassen.nav.no/stilling/abc")
    return "aggregator detection correct"


def test_signal_filter():
    results = [
        {"url": "https://katapult.vc/", "title": "Homepage"},
        {"url": "https://startuplab.no/apply/accelerator", "title": "Apply"},
        {"url": "https://katapult.vc/ocean/cohort-2025/", "title": "New cohort"},
        {"url": "https://shifter.no/nyheter/funding-round/123", "title": "Real article"},
    ]
    filtered = filter_signals(results)
    urls = [r["url"] for r in filtered]
    assert "https://katapult.vc/" not in urls
    assert "https://startuplab.no/apply/accelerator" not in urls
    assert "https://katapult.vc/ocean/cohort-2025/" in urls
    assert "https://shifter.no/nyheter/funding-round/123" in urls
    return f"filtered {len(results) - len(filtered)} junk, kept {len(filtered)}"


def test_dedup():
    results = [
        {"url": "https://example.com/job1", "title": "Job 1"},
        {"url": "https://example.com/job2", "title": "Job 2"},
        {"url": "https://example.com/job1", "title": "Job 1 dup"},
        {"url": "https://example.com/job3", "title": "Job 3"},
    ]
    seen = {"https://example.com/job2"}
    new, skipped = dedup_results(results, seen)
    assert len(new) == 2
    assert skipped == 2
    return "dedup correctly filters seen + in-batch duplicates"


def test_load_config():
    configs = list(SKILL_DIR.glob("*.json"))
    assert len(configs) >= 1
    for c in configs:
        cfg = load_config(c)
        assert "name" in cfg
    return f"loaded {len(configs)} configs: {', '.join(c.stem for c in configs)}"


def test_api_keys():
    msgs = []
    try:
        get_brave_api_key()
        msgs.append("Brave OK")
    except Exception as e:
        msgs.append(f"Brave MISSING ({e})")

    try:
        get_serper_api_key()
        msgs.append("Serper OK")
    except Exception as e:
        msgs.append(f"Serper MISSING ({e})")

    return ", ".join(msgs)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Norwegian Job Finder — search aggregation")
    parser.add_argument("--config", help="Config JSON filename (looked up in skill dir)")
    parser.add_argument("--dry-run", action="store_true", help="Search but don't persist")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--test-only", help="Run specific test")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose, quiet=args.quiet)

    if args.test:
        run_tests(test_only=args.test_only, verbose=args.verbose)
        return

    if not args.config:
        parser.error("--config is required (e.g. --config erin-gallup.json)")

    config_path = SKILL_DIR / args.config
    if not config_path.exists():
        log.error("Config not found: %s", config_path)
        print(json.dumps({"status": "error", "error": f"Config not found: {args.config}"}))
        sys.exit(1)

    try:
        output = run_scan(config_path, dry_run=args.dry_run)
        print(json.dumps(output, indent=2, default=str))
        sys.exit(0)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print(json.dumps({"status": "error", "error": f"API auth error ({e.code})."}))
            sys.exit(1)
        raise
    except Exception as e:
        log.exception("Scan failed")
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
