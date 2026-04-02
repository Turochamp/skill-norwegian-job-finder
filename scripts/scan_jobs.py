#!/usr/bin/env python3
"""
Norwegian Job Finder — search aggregation script.

Reads a single config JSON, builds queries, calls Brave Search API,
deduplicates against 30-day history, and outputs structured JSON to stdout.
The LLM handles scoring, filtering, and formatting the final digest.

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

SKILL_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = SKILL_DIR.parent.parent  # ~/.openclaw/workspace
MEMORY_DIR = WORKSPACE_DIR / "memory"
OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"

BRAVE_API_URL = "https://api.search.brave.com/res/v1/web/search"
DEDUP_WINDOW_DAYS = 30

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


# ---------------------------------------------------------------------------
# Brave Search API
# ---------------------------------------------------------------------------

def get_brave_api_key():
    """Load Brave API key from openclaw.json."""
    with open(OPENCLAW_CONFIG) as f:
        config = json.load(f)
    try:
        return config["plugins"]["entries"]["brave"]["config"]["webSearch"]["apiKey"]
    except KeyError:
        raise ValueError("Brave API key not configured in openclaw.json")


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
            log.debug("Query %r → %d results", query, len(results))
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
            raise  # Auth errors are fatal
        log.warning("Brave API error %d for query %r: %s", e.code, query, body)
        return []
    except Exception as e:
        log.warning("Search failed for query %r: %s", query, e)
        return []


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------

def build_ats_queries(cfg):
    """Build ATS X-Ray queries from config."""
    queries = []
    for q in cfg["sources"].get("ats_xray", []):
        queries.append({"phase": "ats_xray", "query": q, "freshness": "month"})
        # Fallback: strip site: prefix and add domain as keyword
        if q.startswith("site:"):
            parts = q.split(" ", 1)
            domain = parts[0].replace("site:", "")
            rest = parts[1] if len(parts) > 1 else ""
            queries.append({
                "phase": "ats_xray_fallback",
                "query": f"{domain} {rest}".strip(),
                "freshness": "month",
            })
    return queries


def build_board_queries(cfg):
    """Build job board queries from config.

    Brave's site: operator is unreliable for Norwegian job boards (jobbnorge,
    finn return zero results). Strategy:
    - arbeidsplassen.nav.no works reliably with Brave — always include it
    - For jobbnorge/finn: use keyword queries (domain as keyword, no site:)
    - For linkedin: site: works, keep it
    - Use both Norwegian and English title variants
    """
    queries = []
    titles_en = [t for t in cfg["titles"] if t.isascii()][:3]
    titles_no = [t for t in cfg["titles"] if not t.isascii() or t[0].isupper() and not t.isascii()]
    # Grab Norwegian titles (non-ASCII) separately; fall back to all titles
    titles_no = [t for t in cfg["titles"] if any(c in t for c in "æøåÆØÅ")][:3]

    location_str = cfg["locations"][0] if cfg["locations"] else "Norway"

    # English title OR-string
    en_str = " OR ".join(f'"{t}"' for t in titles_en) if titles_en else ""
    # Norwegian title OR-string
    no_str = " OR ".join(f'"{t}"' for t in titles_no) if titles_no else ""

    # arbeidsplassen.nav.no — reliable with Brave, primary source for Norwegian jobs
    if en_str:
        queries.append({
            "phase": "board_arbeidsplassen",
            "query": f'arbeidsplassen.nav.no ({en_str}) {location_str}',
            "freshness": "week",
        })
    if no_str:
        queries.append({
            "phase": "board_arbeidsplassen_no",
            "query": f'arbeidsplassen.nav.no ({no_str})',
            "freshness": "week",
        })

    for board in cfg["sources"].get("boards", []):
        if board == "linkedin":
            # site: works for LinkedIn
            if en_str:
                queries.append({
                    "phase": "board_linkedin",
                    "query": f'site:linkedin.com/jobs ({en_str}) Norway',
                    "freshness": "week",
                })
        elif board == "jobbnorge":
            # site: unreliable — use domain as keyword
            if en_str:
                queries.append({
                    "phase": "board_jobbnorge",
                    "query": f'jobbnorge.no ({en_str}) {location_str}',
                    "freshness": "week",
                })
            if no_str:
                queries.append({
                    "phase": "board_jobbnorge_no",
                    "query": f'jobbnorge.no ({no_str})',
                    "freshness": "week",
                })
        elif board == "finn":
            # site: unreliable — use domain as keyword
            if en_str:
                queries.append({
                    "phase": "board_finn",
                    "query": f'finn.no jobb ({en_str}) {location_str}',
                    "freshness": "week",
                })

    return queries


def build_signal_queries(cfg):
    """Build signal-based prospecting queries from config."""
    queries = []
    signals = cfg["sources"].get("signals", {})

    # Build title-based signal query from config titles
    signal_titles = cfg["titles"][:4]
    title_or = " OR ".join(f'"{t}"' for t in signal_titles)

    for source in signals.get("leadership_changes", []):
        queries.append({
            "phase": "signal_leadership",
            "query": f"site:{source} ({title_or}) (appointed OR hired OR joins)",
            "freshness": "week",
            "count": 5,
        })

    for source in signals.get("funded_companies", []):
        queries.append({
            "phase": "signal_funding",
            "query": f'site:{source} (funding OR "series A" OR "series B" OR "raised") Norway',
            "freshness": "week",
            "count": 5,
        })

    for source in signals.get("startup_ecosystems", []):
        queries.append({
            "phase": "signal_startup",
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
        # Parse date from filename
        try:
            file_date = datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if file_date < cutoff:
            continue

        content = f.read_text(errors="replace")
        # Look for URLs in sections mentioning this slug
        in_slug_section = False
        for line in content.splitlines():
            if slug in line:
                in_slug_section = True
            elif line.startswith("#"):
                in_slug_section = False
            if in_slug_section and "http" in line:
                # Extract URLs
                for word in line.split():
                    if word.startswith("http"):
                        url = word.rstrip(",;)>—")
                        seen.add(url)

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
    api_key = get_brave_api_key()

    slug = cfg["name"]
    person = cfg["person"]
    queries = build_all_queries(cfg)

    log.info("Scanning for %s (%s): %d queries", person, slug, len(queries))

    # Execute all queries
    all_results = {"jobs": [], "signals": []}
    query_stats = {"total": len(queries), "succeeded": 0, "failed": 0, "empty": 0}
    executed_queries = []

    for q in queries:
        phase = q["phase"]
        query_str = q["query"]
        freshness = q.get("freshness", "month")
        count = q.get("count", 10)

        # Skip fallback queries if primary returned results
        if phase == "ats_xray_fallback":
            # Check if the primary query (previous item) got results
            if executed_queries and executed_queries[-1].get("result_count", 0) > 0:
                log.debug("Skipping fallback for %r (primary had results)", query_str)
                continue

        results = brave_search(query_str, api_key, count=count, freshness=freshness)

        entry = {"phase": phase, "query": query_str, "result_count": len(results)}
        executed_queries.append(entry)

        if results:
            query_stats["succeeded"] += 1
            bucket = "signals" if "signal" in phase else "jobs"
            for r in results:
                r["source_phase"] = phase
                r["source_query"] = query_str
                all_results[bucket].append(r)
        else:
            query_stats["empty" if results is not None else "failed"] += 1

        # Rate limit: small delay between API calls
        time.sleep(0.3)

    # Dedup
    seen_urls = load_seen_urls(slug)
    new_jobs, jobs_skipped = dedup_results(all_results["jobs"], seen_urls)
    new_signals, signals_skipped = dedup_results(all_results["signals"], seen_urls)

    log.info(
        "Results for %s: %d new jobs (%d deduped), %d new signals (%d deduped)",
        person, len(new_jobs), jobs_skipped, len(new_signals), signals_skipped,
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
        "dedup": test_dedup,
        "load_config": test_load_config,
        "brave_api_key": test_brave_api_key,
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
        "titles": ["Head of AI", "AI Lead", "Designleder"],
        "locations": ["Oslo", "Norway"],
        "sources": {
            "ats_xray": ['site:teamtailor.com "head of AI" "oslo"'],
            "boards": ["jobbnorge", "finn", "linkedin"],
            "signals": {
                "leadership_changes": ["digi.no"],
                "funded_companies": ["shifter.no"],
            },
        },
    }
    queries = build_all_queries(cfg)
    phases = [q["phase"] for q in queries]
    assert "ats_xray" in phases, f"Missing ats_xray in {phases}"
    assert "ats_xray_fallback" in phases, f"Missing fallback in {phases}"
    assert "board_arbeidsplassen" in phases, f"Missing board_arbeidsplassen in {phases}"
    assert "board_jobbnorge" in phases, f"Missing board_jobbnorge in {phases}"
    assert "board_finn" in phases, f"Missing board_finn in {phases}"
    assert "board_linkedin" in phases, f"Missing board_linkedin in {phases}"
    assert "signal_leadership" in phases, f"Missing signal_leadership in {phases}"
    assert "signal_funding" in phases, f"Missing signal_funding in {phases}"
    # Verify no site: used for Norwegian boards
    for q in queries:
        if q["phase"] in ("board_jobbnorge", "board_jobbnorge_no", "board_finn"):
            assert "site:" not in q["query"], f"site: used for {q['phase']}: {q['query']}"
    return f"{len(queries)} queries built across {len(set(phases))} phases"


def test_dedup():
    results = [
        {"url": "https://example.com/job1", "title": "Job 1"},
        {"url": "https://example.com/job2", "title": "Job 2"},
        {"url": "https://example.com/job1", "title": "Job 1 dup"},
        {"url": "https://example.com/job3", "title": "Job 3"},
    ]
    seen = {"https://example.com/job2"}
    new, skipped = dedup_results(results, seen)
    assert len(new) == 2, f"Expected 2 new, got {len(new)}"
    assert skipped == 2, f"Expected 2 skipped, got {skipped}"
    assert new[0]["url"] == "https://example.com/job1"
    assert new[1]["url"] == "https://example.com/job3"
    return "dedup correctly filters seen + in-batch duplicates"


def test_load_config():
    configs = list(SKILL_DIR.glob("*.json"))
    assert len(configs) >= 1, f"No config files found in {SKILL_DIR}"
    for c in configs:
        cfg = load_config(c)
        assert "name" in cfg, f"{c.name} missing 'name'"
        assert "titles" in cfg, f"{c.name} missing 'titles'"
    return f"loaded {len(configs)} configs: {', '.join(c.stem for c in configs)}"


def test_brave_api_key():
    key = get_brave_api_key()
    assert key and len(key) > 10, "API key looks invalid"
    return "API key accessible"


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
            error_msg = f"Brave API auth error ({e.code}). Check API key."
            print(json.dumps({"status": "error", "error": error_msg}))
            sys.exit(1)
        raise
    except Exception as e:
        log.exception("Scan failed")
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
