# gea-service

The Python analysis service for the Growth Engineering Assessment.

It shares one Postgres database with the Lovable application and they
communicate only through the `analysis_run` table. There is no HTTP between
them. The application inserts a queued row; this claims it, works, and writes
the result back. The browser watches the row over realtime.

**Parse is real. Everything else is still a stub.**

A parse job reads the client's actual file out of storage, counts what is in
it, profiles every column, and writes a `dataset` and one
`dataset_field_profile` per column. Those figures are true.

Every other job type returns fixture output with realistic timings and
progress, so the screens can be built against it exactly as if the numbers were
real. **Those figures are invented and must never reach a client deliverable.**

Tell them apart by `model_name` on the run: a real parse records `parse-1`, a
fixture records `stub`.

## Running it

```
pip install -r requirements.txt
python worker.py
```

Three environment variables. Keep all of them in your environment, never in
this repository, never in a chat window.

| | |
|---|---|
| `DATABASE_URL` | the connection string `gea-db/apply.py` uses, from the **Connect** button on the Supabase project page |
| `SUPABASE_URL` | `https://<project>.supabase.co` |
| `SUPABASE_SERVICE_KEY` | the service role key, from Project Settings, API Keys |

The service key reads client exports out of storage and bypasses every access
rule in the database. It is the most sensitive credential in the project.

```
setx DATABASE_URL "postgresql://..."          Windows, then a new terminal
setx SUPABASE_URL "https://....supabase.co"
setx SUPABASE_SERVICE_KEY "sb_secret_..."
```

## What parsing actually does

Streams the file with the standard library rather than pandas, so a four
hundred thousand row export never sits in memory whole and there is no binary
wheel to install. Forty thousand rows take about four seconds.

It works out the encoding and the delimiter, strips the byte order mark Excel
leaves on the first column name, and keeps duplicate or unnamed columns as they
are, because a mapping screen has to show what the file really contains.

Three rules it keeps, because the methodology depends on them:

- **Unparseable rows are counted, never dropped.** A row with the wrong number
  of fields is a finding about the client's export, with up to ten examples
  recorded by line number.
- **No imputation, no defaults.** An empty column is empty.
- **Every rate carries its denominator.** `populated_rate` is always stored
  beside `populated_count` and the row count behind it.

A file that cannot be read records a `dataset` with `parse_status` of `failed`
and the reason, rather than leaving nothing. An empty profile and a failed one
look identical on a screen otherwise, and the difference is what the consultant
has to tell the client.

Parquet is not written yet. `parquet_object_path` stays empty rather than
claiming a file that does not exist; it arrives when the analyses need it.

Ctrl-C stops it after the run in flight finishes. A claimed run is always seen
through; it is never abandoned half done.

## What it does

Polls for queued work every two seconds and claims the oldest with
`gea_claim_run()`, which uses `for update skip locked` so several workers can
run without two ever claiming the same job. Reports progress through
`gea_heartbeat_run()`, then writes output and marks the run succeeded.

Polling is the service's business. The browser never polls — it subscribes.

A job type the service does not declare is marked **failed** with the reason,
not quietly succeeded. The contract says never construct a job the service does
not declare, and this is how a mistake gets noticed.

## Idempotency has a consequence worth knowing

`gea_enqueue_run()` returns the existing run when one matches, including a run
that has already succeeded. So "re-run this analysis" cannot simply call
enqueue again — with identical inputs it returns the old run and nothing
happens. A genuine re-run changes something in `parameters`, which is also what
makes the two runs distinguishable afterwards.

## Job types

`parse`, `profile`, `propose_mappings`, `analysis:{key}`, `classify:{key}`,
`synthesise_interview`, `parse_upload:aeo`, `generate:{deliverable}`.

## Replacing the stub

The contract is the shape of what lands in `compute_output`, not this code.
Each real job keeps the same job type, reports progress as a count against a
real total, and writes the same output keys. Until then, `model_name` is
`stub` on every run, which is how you tell fixture output from the real thing.
