---
name: norwegian-job-finder
description: >
  Scans Norwegian job portals (Arbeidsplassen/NAV, Finn.no, LinkedIn) for
  opportunities matching the person's profile. Reads profile.json from the
  workspace, queries job sources, scores matches, and returns a ranked
  summary. Use when the daily cron fires or when the person asks to check now.
env:
  - ARBEIDSPLASSEN_API_URL (optional, defaults to public endpoint)
---

# Norwegian Job Finder

## When to use

- The daily cron job triggers with a message like "Run daily job scan"
- The person says "check for jobs", "run now", "any new jobs?", or similar

## Inputs

Read `norwegian-job-finder.json` from the workspace root. Expected structure:

```json
{
  "bootstrapped": true,
  "role": "Senior Backend Developer",
  "skills": ["Python", "Kubernetes", "PostgreSQL"],
  "industries": ["fintech", "energy", "saas"],
  "locations": ["Oslo", "Bergen", "remote"],
  "norwegian_level": "conversational",
  "min_salary": 750000,
  "dealbreakers": ["no consulting", "permanent only"],
  "optimizing_for": "work-life balance",
  "updated_at": "2026-03-28"
}
```

If `bootstrapped` is false, reply: "I need to set up your profile first. Let's chat."

## Scan procedure

### 1. Arbeidsplassen (NAV)

Use the browser tool or fetch to query:
```
https://arbeidsplassen.nav.no/stillinger?q=<role keywords>&published=now/d
```

Extract from the results page:
- Job title
- Company name
- Location
- Published date
- Direct link

### 2. Finn.no

Use the browser tool to query:
```
https://www.finn.no/job/fulltime/search.html?q=<role keywords>&sort=PUBLISHED_DESC
```

Extract the same fields. Finn often has Norwegian-language listings — match
against skills even if the listing language differs from the profile language.

### 3. LinkedIn (best-effort)

Use the browser tool to query:
```
https://www.linkedin.com/jobs/search/?keywords=<role>&location=Norway&f_TPR=r86400
```

LinkedIn may block or require login. If it fails, skip and note it.

## Scoring

For each job found, score 0–100 based on:

| Factor              | Weight | Logic                                              |
|---------------------|--------|----------------------------------------------------|
| Skill match         | 40%    | How many listed skills appear in the job text       |
| Location match      | 20%    | Exact location match or remote-friendly             |
| Industry match      | 15%    | Company/role in a preferred industry                |
| Language fit        | 10%    | Norwegian required vs person's level                |
| Dealbreaker check   | 15%    | Fails any dealbreaker → score = 0                   |

## Output

Return only jobs scoring ≥ 50. Format as:

```
🎯 **[Job Title]** at [Company]
📍 [Location] | 💰 [Salary if listed] | Score: [X]/100
Why: [1-line reason this matches]
🔗 [Direct link]
```

Group by source (Arbeidsplassen / Finn / LinkedIn).
If no jobs score ≥ 50, return nothing (stay silent per AGENTS.md rules).

## Deduplication

Read today's memory file (`memory/YYYY-MM-DD.md`) and previous days.
Skip any job URL already sent in the last 30 days.
After the scan, append new job URLs to today's memory file.

## Error handling

- If a source is unreachable, skip it and note which source was skipped.
- If profile.json is malformed, tell the person and ask them to re-bootstrap.
- Never fabricate job listings. Only return what was actually found.
