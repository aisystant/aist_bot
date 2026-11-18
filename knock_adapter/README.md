# knock_adapter (provisional)

The single place that makes outgoing calls to Knock (notification delivery core),
WP-568 F17 item 5. Standard library only; the folder name and the repository are
**not confirmed yet** (moving it is a plain `git mv`). Not wired into the bot, not
in the Docker image (the Dockerfile copies folders by name), so nothing is deployed
by merging it.

Add `tzdata` to the image requirements before wiring: slim images may have no
time zone database (the adapter then refuses with `timezone_unknown:<name>`).

## Contract

- Domain types only (`Person`, `RuleSpec`, `Result`, `BatchResult`); no Knock types leak out.
- Every call returns a `Result`; nothing but programming errors raises.
- `Outcome.OUTCOME_UNKNOWN` (timeout, 5xx, connection cut, unreadable body on a
  mutating call) is **never repeated blindly**. The result carries `reconcile_key`
  `(operation, person_id, rule_id)`. The caller (notification center) keeps the
  "waiting for reconciliation" list and uses `find_schedules(person_id, rule_id)`.
  Operations at the provider may be background, so "not found yet" is not "absent".
- Safe to repeat: reads, deletes ("not found" = success), HTTP 429. Person upsert
  is repeated only while `person_upsert_idempotent` is true (assumption, verify live).
  Workflow trigger is repeated only with an idempotency key and only after
  `trigger_idempotency_verified` is switched on by the live check.
- One time budget per call (default 90 s) includes the time spent inside the calls.
  Per-call timeout 10 s (30 s for bulk), at most 5 attempts, pauses 1-2-4-8 s with
  +-25 % jitter, `Retry-After` honoured (capped at 60 s), 4 calls in flight overall
  (2 chunks of one bulk operation). All values are starting values.
- Time checks: a first occurrence inside a daylight-saving gap is **rejected**
  (measured: the provider hangs for more than a minute); a future occurrence in a gap
  is only a **warning** (not measured; the pilot decides after the live check). Minutes
  of recurring schedules must be multiples of 5; otherwise an explicit error with the
  nearest values, never silent rounding. `change_timezone` dry-runs all of the
  person's schedules and writes nothing if one would start in a gap.
- The management key (template deployment) is never given to this adapter.

## Not verified against the real Knock (live check, needs the pilot's runtime key)

Paths and bodies of schedules (`repeats`, `data`, `recipients`), listing a person's
schedules with their data, what the person read returns while a bulk creation runs,
whether a schedule id of ours is accepted, an idempotency key on workflow trigger,
`Retry-After` presence, rate limits at larger volumes, behaviour next to a gap and of
an existing daily schedule reaching a transition day, a hung create (did it happen?),
timezone change on live schedules, which of two repeated autumn hours fires.

## Tests

`tests/test_knock_adapter_*.py` (71 tests, fake clock and in-memory fake Knock, plus a
real local HTTP server for the hard timeout).
