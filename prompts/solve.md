You are solving a hard algorithmic problem. The statement below is the only specification. Where your memory of a standard algorithm disagrees with the statement, the statement wins.

Target language: {{language}}. Contract: {{contract}}

Work out the rules, traps, and algorithm first. Respond with exactly these blocks, in order, each terminated by a line `===END===`. Keep the design compact but explicit.

===DESIGN===
State representation, the invariant for every operation, the maximum-constraint complexity, and one hand trace of the hardest boundary case. Before writing code, reject any approach that iterates a count or capacity the statement bounds by a huge number, materializes or rescans a structure whose described size exceeds memory, or recurses to a depth proportional to the input.
===END===

===CODE===
The complete solution, and nothing else. {{language_rules}}

This block must contain only final, runnable code. Do not think out loud inside it: no commentary
on approaches, no "this is wrong, let me reconsider", no abandoned or half-written constructs, no
alternatives left in place. If you change your mind about the approach, delete the old code and
write the new one -- do not narrate the change. A block that trails off mid-statement, or leaves a
`for`/`while`/`if` with its body replaced by comments, is a syntax error and scores zero however
good the reasoning around it was. Put all deliberation in ===RULES=== and ===TRAPS===.
===END===
===RULES===
Numbered restatement of every behavioral sentence of the statement, quoting the text. List ambiguities separately with the reading you chose.
===END===
===TRAPS===
For EACH item below, one line: what a naive solution would do and why it fails here, or "n/a".
- counts/repetitions/capacities up to 10^18 that must not be iterated
- structures that must not be materialized (enormous layouts, exponential unfoldings, cell enumeration)
- persistence/branching across versions
- unbounded reversals or splices on long sequences
- recursion depth linear in input size (Python recursion limit)
- integer overflow (Rust i64) — use i128/u128 for sums of 10^18 quantities
- custom Unicode/grapheme/byte rules that differ from the standard library
- wording that redefines behavior after exhaustion, reset, or removal
===END===
===ALGORITHM===
Data structures, per-operation complexity against the stated maximum sizes, overflow and recursion treatment. Written as a record of the reasoning behind the code above, not a plan for it.
===END===

Problem statement:

{{statement}}
