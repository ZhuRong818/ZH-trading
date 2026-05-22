# Production Notes

Use async jobs with stages:

```text
queued -> planned -> searched -> fetched -> scored -> ranked
```

Store long-term:

- job params
- search query text
- author IDs / usernames
- tweet IDs used as evidence
- derived features
- scores and explanations

Avoid long-term storage of raw tweet text unless needed for short-term cache/debug.

Cost controls:

- page budgets per query
- candidate caps after search
- staged fetch: first 20 posts for all candidates, then deeper fetch only for top-K
- cache query pages and candidate recent posts
- send only compact packets to LLM

Monitoring:

- unique authors per 100 search tweets
- hard filter reject rate
- spam false accept rate
- cost per accepted KOL
- LLM JSON parse failure rate
- provider fallback rate

