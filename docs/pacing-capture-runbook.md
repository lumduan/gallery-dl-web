# Capturing the evidence for the Instagram pacing fix

PR-1 (`fix(pacing): capture per-request evidence…`) shipped the capture pipeline and stopped
unpaced media downloads erasing the back-off. It did **not** fix the reported bug: Instagram's
HTTP-200 throttle is still undetected, because nothing in the stack classifies it.

Writing that classifier needs real bytes. The signatures in the original brief
(`{"status":"fail"}`, `checkpoint_required`, `feedback_required`, …) are **leads, not spec** — the
whole point of this exercise is to find out what Instagram actually sends before matching on it.

> **Why not just guess?** Because the last classifier written from plausible-looking wording missed
> the most common real failure. `CLAUDE.md` records it: the first login-wall matcher matched
> gallery-dl's own `AuthRequired` prose, and Instagram never emits that prose — the real failure was
> a urllib3 debug line reading `… HTTP/1.1" 401 42`, with the word "Unauthorized" nowhere on it.

## Run it in three steps, not one

The run-to-block is **irreversible and unrepeatable within a day**, so two cheap gates come first.

### 1. The pipeline is already proven (nothing for you to do)

`backend/tests/gallerydl/test_capture_e2e.py` drives a real `requests.Session` against a loopback
server and asserts a JSON body is captured from inside the response hook. Its negative control is
the point: restore the old "only read an already-buffered body" rule and it fails with
`"body": ""` — the exact silent failure that would have wasted a block.

```bash
cd backend && uv run pytest tests/gallerydl/test_capture_e2e.py -q --no-cov
```

### 2. A short run — confirms capture against real Meta traffic

Pick a **small public profile** (roughly 30–50 images). At the Instagram default floor of 4 s that
is a few minutes and stays far short of the ~800 s / ~1.9 req/s regime where blocks happen.

Leave pacing at its defaults. You are testing the sensor, not the pacer.

```bash
JOB=$(curl -s -X POST localhost:8000/api/jobs \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.instagram.com/<handle>/"}' | jq -r .id)

# when it finishes:
curl -s localhost:8000/api/jobs/$JOB | jq '.final_summary.pacing_telemetry'
```

**Healthy output** — a list of up to 50 entries, mixing extractor requests and media downloads:

```json
[
  { "i": 0, "delay": 4.0, "floor": 4.0, "ceiling": 30.0, "status": 200,
    "url": "i.instagram.com/api/v1/feed/user/…", "content_type": "application/json",
    "body": "{\"items\":[…", "streamed": false, "classified": "clean", "rule": null },
  { "i": 1, "delay": 4.0, "floor": 4.0, "ceiling": 30.0, "status": 200,
    "url": "scontent.cdninstagram.com/v/…", "content_type": "image/jpeg",
    "body": "", "streamed": true, "classified": "clean", "rule": null }
]
```

Check three things and stop if any is wrong:

| Check | Why it matters |
|---|---|
| `pacing_telemetry` exists at all | absent ⇒ pacing was `fixed`, or nothing was observed |
| non-streamed entries have a **non-empty** `body` | empty ⇒ the sensor is blind; do **not** proceed to step 3 |
| `streamed: true` entries have `body: ""` | non-empty ⇒ media is being buffered, which is a bug |

### 3. The run to a block

Only after step 2 looks right. Use the profile that reproduces it — the one that dies around 800 s.
Let it run until it fails.

Then send me **either** of these, whichever exists:

```bash
# normal failure — telemetry rides on the terminal event
curl -s localhost:8000/api/jobs/$JOB | jq '.final_summary'

# killed by the stall detector — it arrives as its own non-terminal event instead
curl -sN localhost:8000/api/jobs/$JOB/events | grep -A1 'pacing-telemetry'
```

## What is safe to paste

The record is built to be shareable: `url` is host + path with the **query dropped whole**
(Instagram signs media URLs there), only `Content-Type` and `Content-Length` are read, no request
header is ever touched, and the body prefix is capped at 500 bytes and credential-masked.

That said, redaction of a free-form body is best-effort by nature. **Skim it before pasting** — if
anything looks like a token that survived, say so instead and I will tighten the masking first.

## What I will do with it

Answer three questions, then write the signature table against the answers:

1. **Is the `{"status": "fail"}` envelope real?** gallery-dl never handles it — `instagram.py` has no
   `status`/`fail` handling at all — so a 200-shaped throttle currently becomes `KeyError: 'items'`.
   The captured body settles what it actually looks like.
2. **Does the `checkpoint_required` / `feedback_required` family appear?** Those need `TERMINAL`
   (stop the run), not `THROTTLE` (back off) — retrying into a checkpoint extends the block.
3. **Does the empty-page soft limit fire?** `instagram.py:1371-1377` raises
   `"<user>'s posts are private"` when `has_next_page` is true and `edges` is empty. If that appears
   in a run against a public profile, gallery-dl is misreporting a soft limit as a privacy setting.
