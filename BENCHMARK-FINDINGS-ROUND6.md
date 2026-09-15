# Benchmark findings, rounds 6–9 (2026-09-13)

Covers everything after the external audit: 31 commits on `solver` from `2a2cb80` to `659bd34`, 186 tests passing.
Every change is listed with the failure *class* it covers, so it can be checked against the rule that nothing is
tuned to a sample.

## Results

| Round | What changed before it | Full pass | Unverified | With failures | Lost to environment | Cost |
|---|---|---|---|---|---|---|
| 6 (+2 reruns) | 7 audit fixes | 2 | 1 | 7 | 9 rows across 3 attempts (DNS) | $0.10 + reruns |
| 7 | 12 generic fixes from adjudication | 1 (false, see F6) | 2 | 3 | 4 DNS, 2 hung connections | $0.10 |
| 8 | 3 more fixes | 0 | 4 | 6 | 0 | $0.21 |
| 9 (stopped at 4 of 10) | oracle self-consistency regen | 0 | 2 | 2 | 0 | — |

The "full pass" column is not comparable across rounds. Each round made `passed_all_gates` stricter: round 6 added
degraded-stress and thin-coverage demotion, round 7 added regenerated-oracle disagreement, round 8 added "at least one
real check must have run". Two of round 6's passes and round 7's one pass would not qualify today. The comparable
signal is the second table.

## Who lost each run: the system or the model

| Round | System-caused | Model-caused | Notes |
|---|---|---|---|
| 6 | 4 of 7 failures | 3 | invalid max-size input, shrinker built invalid inputs, candidate leaked into oracle, unactionable verdict minted a worse candidate |
| 8 | 4 of 4 unverified | 6 failures | all four: the oracle's validate() rejected most of its own gen() |
| 9 (partial) | 1 of 2 unverified | 2 failures | one gen() crashed at import; the other a 47 MB gen_max that the prompt fix did not prevent |

Every system-caused loss found has a fix on the branch. The model-caused failures are the same six problems every
round: implicit treap with reversal, persistent branching versions, grapheme and UTF-16 indexing, capture-avoiding
substitution, a schema state machine, and a wire-format encoder with a 7-field output. The strong model does not solve
them in two attempts plus two repairs, and the fast model misreads their output formats when writing the oracle.

## Changes, by failure class

**Verification honesty (audit items 4, 5, and F6).** Degraded evidence and thin coverage demote the status. A candidate
that was never gated cannot be a full pass. *Class:* any run where the gate ran against weaker input than its name
claims.

**Every gate input passes the oracle's validate().** The max-size input and every shrunk counterexample are now
validated, not just generated tiers. *Class:* generators that violate stated preconditions; shrinkers that drive values
outside them. Seen as three repairs spent on a panic caused by an invalid input.

**The oracle never sees the candidate.** The adjudication context carried the repair model's prose about the candidate,
and a regenerated oracle inherited the candidate's bug exactly. *Class:* any regeneration.

**A regenerated oracle is cross-checked against the one it replaces.** Above `oracle_regen_max_disagreement` the diff
evidence is marked degraded. *Class:* two references from the same prose that cannot both be right.

**An oracle verdict with no regeneration left ends the loop** instead of minting the repair's unchanged code as a new
candidate. *Class:* orchestrator control flow.

**The repair model sees the reference and the disagreement rate.** It was asked to judge a reference it could not see;
it now also learns whether the disagreement is on a few inputs or most. *Class:* adjudication quality.

**One I/O convention per language, given to all three prompts.** Rust: stdin is a token stream. Python: container
types, list when the statement is silent. *Class:* three models inventing three readings of the same shape.

**rustc's own import suggestion is harvested and the compile retried once.** *Class:* missing `use`, zero tokens.

**Batches abandon after five consecutive per-case timeouts.** *Class:* a dead reference or candidate that would
otherwise cost the full per-case limit for every remaining case.

**The medium tier yields to repair when the budget is already late.** *Class:* an 80-second reference pass that
leaves no time to re-gate a repair.

**Transport errors and 429/5xx are retried with backoff, bounded by the call's own timeout.** *Class:* a DNS blip at
call start that cost the whole problem.

**An oracle whose validate() rejects most of its own gen() is regenerated once**, with the rejected inputs as evidence
and a guard that keeps the original if the replacement is worse. *Class:* the single largest cause of unverified runs
in round 8.

**Prompt rules:** generated counts never exceed the stated maximum; nothing runs at import time in the stress module;
gen and validate are written from one precondition list; worst-case inputs drive bounded quantities to their bound.

**Operational:** default run directories are timestamped; an over-cap harness result says so; the cost cap covers
every optional call; docs match config.

## What was deliberately not done

- Hard phase deadlines (audit item 1): would kill the oracle call before its measured 38–128 s latency.
- Max-size correctness via a fast reference (audit item 3): contradicts the literal-oracle invariant.
- Exact-type comparison (audit item 10): the oracle is wrong one time in four; stricter comparison converts its
  sloppiness into false failures.
- Container sandboxing: a runtime dependency the repo rules forbid; documented limitation.

## Open items, in priority order

1. **Hung connections.** One call to a model returns in 7 s while its twin hangs for the full 120 s cap. Without
   streaming, a hang is indistinguishable from a slow model and costs the whole cap. Streaming with a no-bytes-for-N-
   seconds abort, feeding the existing retry, is the fix. Roughly 30 lines in `llm.py`. Not done: it changes the client.
2. **The fast model is the weakest link.** `deepseek/deepseek-chat-v3-0324` writes gen and validate that disagree,
   misreads output formats, and takes 40–150 s. It is one config line to try a stronger oracle author; the cost cap
   was never approached ($0.02–0.03 per problem against $0.10).
3. **The strong model's ceiling.** The same six problems fail every round. Also one config line.
4. **`validate_reject_frac_regen = 0.5`** is calibrated on three points, all above 0.8.
5. The machine's network flapped in three of four rounds. Check `done error=` in a run's log before diagnosing any
   `no_candidate` row.
