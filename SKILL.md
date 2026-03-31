---
name: norwegian-job-finder
description: "Scans for jobs in Norway across multiple people, each configured in their own JSON file in this skill folder. Uses Brave Search for ATS X-Ray queries, board searches, and signal-based prospecting. Requires BRAVE_API_KEY in .env."
---

# Norwegian Job Finder

## When to use

- The daily cron job triggers with a message like "Run daily job scan"
- The person says "check for jobs", "run now", "any new jobs?", or similar
- Optionally scoped to one person: "check jobs for Erin"

## Inputs

Read all `*.json` files in this skill folder (`skills/norwegian-job-finder/`).
Each file defines one independent search for one person. Skip any file that is
not valid JSON or has `"enabled": false`.

### Config schema

```json
{
  "name": "slug-identifier",
  "person": "Display Name",
  "enabled": true,
  "titles": ["list of job title variants to search"],
  "locations": ["Oslo", "Norway", "remote"],
  "industries": ["optional list — used to boost score"],
  "dealbreakers": ["optional list — any match scores the job 0"],
  "min_score": 50,
  "sources": {
    "ats_xray": [
      "site:teamtailor.com \"title\" \"location\"",
      "site:greenhouse.io \"title\" \"country\""
    ],
    "boards": ["jobbnorge", "finn", "linkedin"],
    "signals": {
      "leadership_changes": ["digi.no", "e24.no"],
      "funded_companies": ["shifter.no", "funderbeam.no"]
    }
  },
  "notes": "Optional free-text context visible only to the agent.",
  "updated_at": "YYYY-MM-DD"
}
```

## Scan procedure

Run the full procedure for each enabled config file. Treat each config as
independent — deduplication is tracked per `name` slug.

### Step 1 — ATS X-Ray

For each query string in `sources.ats_xray`, call `web_search` with Brave:

```
query:    <the full query string as-is, e.g. site:teamtailor.com "head of AI" "oslo">
count:    10
country:  "NO"
freshness: "month"
```

If Brave does not honour the `site:` operator (no results or irrelevant results),
retry without the `site:` prefix and add the domain name as a keyword instead,
e.g. `teamtailor.com "head of AI" oslo`.

### Step 2 — Board search

For each board in `sources.boards`, build a query from the config's title
variants and run `web_search` with Brave:

| Board | Domain | Query pattern |
|-------|--------|---------------|
| jobbnorge | jobbnorge.no | `site:jobbnorge.no <titles OR-joined> <location>` |
| finn | finn.no | `site:finn.no/jobb <titles OR-joined> <location>` |
| linkedin | linkedin.com/jobs | `site:linkedin.com/jobs <titles OR-joined> Norway` |

Parameters: `count: 10`, `country: "NO"`, `freshness: "week"`.

LinkedIn may block — skip and note if it fails.

### Step 3 — Signal-based prospecting

For each source in `sources.signals.leadership_changes`:
```
query:    site:<source> (CDO OR "Chief AI" OR "Head of AI" OR "Head of Design" OR "AI Lead") (appointed OR hired OR joins)
count:    5
freshness: "week"
```

For each source in `sources.signals.funded_companies`:
```
query:    site:<source> (funding OR "series A" OR "series B" OR "raised") Norway
count:    5
freshness: "week"
```

## Scoring

For each job found in Steps 1–2, score 0–100:

| Factor            | Weight | Logic                                                   |
|-------------------|--------|---------------------------------------------------------|
| Title match       | 40%    | How many of the config's `titles` appear in the job text |
| Location match    | 20%    | Exact location match or remote-friendly                  |
| Industry match    | 15%    | Company/role in a preferred industry (if configured)     |
| Language fit      | 10%    | Norwegian required vs. person's apparent level           |
| Dealbreaker check | 15%    | Any dealbreaker match → score = 0                        |

## Deduplication

Read previous memory files (`memory/YYYY-MM-DD.md`) going back 30 days.
Skip any job URL already sent for this `name` slug within 30 days.
After the scan, append new job URLs (keyed by slug) to today's memory file.

## Output

Group output by person. For each person:

**Jobs section** — only jobs scoring ≥ `min_score`:

```
### [Person Name]

🎯 **[Job Title]** at [Company]
📍 [Location] | 💰 [Salary if listed] | Score: [X]/100
Why: [1-line reason this matches]
🔗 [Direct link]
```

**Signals section** — if signals were configured and results found:

```
#### Signals for [Person Name]

📡 **[Person/Company]** — [what happened, e.g. "New CDO at Telenor"]
Action: [suggested next step, e.g. "Connect within 30 days"]
🔗 [Source link]
```

If no jobs score ≥ `min_score` and no signals are found for a person, omit
that person's section entirely (stay silent per AGENTS.md rules).

## Error handling

- If `BRAVE_API_KEY` is missing, **STOP.** Return error; do not fall back to other search engines.
- If a source is unreachable or returns no results, skip it and note which.
- If a config file is malformed, skip it and name the file in the output.
- Never fabricate job listings or signals. Only return what was actually found.
