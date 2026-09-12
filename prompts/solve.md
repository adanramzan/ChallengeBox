You are solving a hard algorithmic problem. The statement below is the only specification. Where your memory of a standard algorithm disagrees with the statement, the statement wins.

Target language: {{language}}. Contract: {{contract}}

Work out the rules, traps, and algorithm in your head first, but write the CODE block first in your response — output tokens are limited, and code cut off by the budget is worse than analysis cut off by it. Respond with exactly these blocks, in order, each terminated by a line `===END===`. Keep reasoning brief; the budget is small.

===CODE===
The complete solution. {{language_rules}}
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
