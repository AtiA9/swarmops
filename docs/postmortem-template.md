# Postmortem: <title>

- **Date:** <YYYY-MM-DD>
- **Author(s):**
- **Status:** draft / reviewed / final
- **Severity:**

This is a **blameless** postmortem. The goal is to understand what the system (and
our understanding of it) allowed to happen, not to find who to blame. Assume everyone
involved made reasonable decisions with the information they had at the time.

## Summary

One or two sentences: what happened, what was the impact, how long did it last.

## Impact

- Who/what was affected (units, alerts, dashboards, data)?
- Duration (start → detection → mitigation → resolution)?
- Any real alerts missed, or delayed past the SLO in `docs/SLO.md`?

## Timeline

All times in UTC.

| Time | Event |
|---|---|
| | First deviation from normal (even if not yet noticed) |
| | Detected (by whom/what - alert, dashboard, manual check) |
| | Diagnosis started |
| | Root cause identified |
| | Mitigated (impact stopped getting worse) |
| | Resolved (back to normal) |

## Root cause

What actually caused this, technically. Not "human error" - what in the system made
that error possible or likely?

## 5 Whys

1. Why did the impact happen? →
2. Why did *that* happen? →
3. Why did *that* happen? →
4. Why did *that* happen? →
5. Why did *that* happen? → (this is usually where the actionable fix lives)

## What went well

## What went poorly

## Where we got lucky

(If nowhere - say so. Not every incident has a "got lucky" line.)

## Action items

| Action | Owner | Priority | Ticket/PR |
|---|---|---|---|
| | | | |

## Related

- Runbook used (if any): `docs/runbooks/...`
- Relevant Grafana dashboard link / time range:
- Relevant PR(s):
