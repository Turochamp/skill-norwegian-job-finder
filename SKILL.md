---
name: norwegian-job-finder
description: "Scans for jobs in Norway using a Python script for search aggregation and deduplication, then the LLM scores and formats results. Each person has their own config JSON."
---

# Norwegian Job Finder

## When to use

- The daily cron job triggers with a message like "Run daily job scan"
- The person says "check for jobs", "run now", "any new jobs?", or similar
- Optionally scoped to one person: "check jobs for Erin"

## Workflow

### Step 1 — Run the search script

The Python script handles all deterministic work: reading the config, building
queries, calling Brave Search API, deduplicating against 30-day memory, and
outputting structured JSON.

```bash
python3 workspace/skills/norwegian-job-finder/scripts/scan_jobs.py --config <filename>.json
```

The script outputs JSON with this structure:
```json
{
  "status": "ok",
  "config": {
    "name": "slug",
    "person": "Display Name",
    "titles": ["..."],
    "locations": ["..."],
    "industries": ["..."],
    "dealbreakers": ["..."],
    "min_score": 50,
    "notes": "Context about the person..."
  },
  "jobs": [
    {"title": "...", "url": "...", "description": "...", "age": "...", "source_phase": "...", "source_query": "..."}
  ],
  "signals": [
    {"title": "...", "url": "...", "description": "...", "source_phase": "...", "source_query": "..."}
  ],
  "query_stats": {"total": 20, "succeeded": 15, "failed": 2, "empty": 3},
  "dedup": {"jobs_skipped": 3, "signals_skipped": 0, "seen_urls_loaded": 12}
}
```

If the script returns `"status": "error"`, report the error and stop.

### Step 2 — Score and filter (LLM)

Using the JSON output, score each job 0–100:

| Factor            | Weight | Logic                                                   |
|-------------------|--------|---------------------------------------------------------|
| Title match       | 40%    | How many of the config's `titles` appear in the job text |
| Location match    | 20%    | Exact location match or remote-friendly                  |
| Industry match    | 15%    | Company/role in a preferred industry (if configured)     |
| Language fit      | 10%    | Norwegian required vs. person's apparent level           |
| Dealbreaker check | 15%    | Any dealbreaker match → score = 0                        |

Use the `config.notes` field for additional context about the person when scoring.
Only keep jobs scoring ≥ `config.min_score`.

### Step 3 — Format output (LLM)

**Jobs section** — only jobs scoring ≥ `min_score`:

```
### [Person Name]

🎯 **[Job Title]** at [Company]
📍 [Location] | 💰 [Salary if listed] | Score: [X]/100
Why: [1-line reason this matches]
🔗 [Direct link]
```

**Signals section** — if signals were found:

```
#### Signals for [Person Name]

📡 **[Person/Company]** — [what happened]
Action: [suggested next step]
🔗 [Source link]
```

If no jobs score ≥ `min_score` and no signals are found, omit output entirely
(stay silent per AGENTS.md rules).

### Step 4 — Update dedup memory

After formatting, append new job URLs to today's memory file
(`workspace/memory/YYYY-MM-DD.md`) keyed by config slug so they won't be
reported again in the next 30 days.

## Config files

Each `*.json` file in this skill folder defines one search config:

```json
{
  "name": "slug-identifier",
  "person": "Display Name",
  "enabled": true,
  "titles": ["list of job title variants to search"],
  "locations": ["Oslo", "Norway", "remote"],
  "industries": ["optional — used to boost score"],
  "dealbreakers": ["optional — any match scores the job 0"],
  "min_score": 50,
  "sources": {
    "ats_xray": ["site:teamtailor.com \"title\" \"location\""],
    "boards": ["jobbnorge", "finn", "linkedin"],
    "signals": {
      "leadership_changes": ["digi.no"],
      "funded_companies": ["shifter.no"]
    }
  },
  "notes": "Optional free-text context about the person.",
  "updated_at": "YYYY-MM-DD"
}
```

## Error handling

- If the script returns a Brave API auth error, **STOP.** Do not retry.
- If `query_stats.failed > 0`, note which sources failed in your output.
- Never fabricate job listings or signals.
