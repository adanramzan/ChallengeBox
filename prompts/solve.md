You are solving a hard algorithmic problem. The statement below is the only specification. Where your memory of a standard algorithm disagrees with the statement, the statement wins.

Target language: {{language}}. Contract: {{contract}}

Settle the approach quickly, weighing the statement's rules, the trap list further
down, and the cost of your approach at the stated maxima. Reject, before the first line of code, any
approach that iterates a count or capacity the statement bounds by a huge number, materializes or
rescans a structure whose described size exceeds memory, or recurses to a depth proportional to the
input. Then emit ===CODE=== FIRST. The blocks after it are a record of the thinking you have already
done, not a plan for it -- keep each one inside its stated length.

Respond with exactly these blocks, in this order, each terminated by a line `===END===`.

===CODE===
The complete solution, and nothing else. {{language_rules}}

This block must contain only final, runnable code. Do not think out loud inside it: no commentary
on approaches, no "this is wrong, let me reconsider", no abandoned or half-written constructs, no
alternatives left in place. If you change your mind about the approach, delete the old code and
write the new one -- do not narrate the change. A block that trails off mid-statement, or leaves a
`for`/`while`/`if` with its body replaced by comments, is a syntax error and scores zero however
good the reasoning around it was. Put all deliberation in ===RULES=== and ===TRAPS===.
===END===

===EXAMPLES===
Three to five lines and nothing else — no comments, no prose, no blank lines with text in them.
Each line is one Python literal 2-tuple `(args, expected)`. `args` is {{gen_returns}}, exactly as the
entrypoint will be called with it; `expected` is exactly what must come back — the return value for a
Python function, the complete stdout text for a Rust program.
Every example must satisfy every precondition the statement states — an example whose input the statement forbids is thrown away and its check is lost — and every container in it must be spelled exactly as this contract says: {{io_rules}}
Hand-trace every one of them from the statement. Never obtain them by running the code above.
Include at least these three: the smallest legal input; one input sitting at a stated numeric limit
whose answer is cheap to reason about; and the input you consider most likely to be misread — the
deepest state the statement reaches, or the clause a naive reading gets wrong.
===END===

===RULES===
Numbered restatement of the behavioral sentences of the statement — the ones that change what the
answer is — quoting the text. At most 15 numbered lines, one line each, ambiguities included: list
an ambiguity as its own numbered line with the reading you chose. No setup, no plan for the code.
===END===

===DESIGN===
At most 20 lines: the state representation, the invariant every operation maintains, the
per-operation complexity against the stated maximum sizes, how overflow and recursion depth are
treated, and one hand trace of the hardest boundary case. A record of the reasoning behind the code
above, not a plan for it.
===END===

===TRAPS===
One line for each item below that ACTUALLY APPLIES to this problem: what a naive solution would do
and why it fails here. Skip every item that does not apply — do not write "n/a" lines for them.
- counts/repetitions/capacities up to 10^18 that must not be iterated
- structures that must not be materialized (enormous layouts, exponential unfoldings, cell enumeration)
- persistence/branching across versions
- unbounded reversals or splices on long sequences
- recursion depth linear in input size (Python recursion limit)
- integer overflow (Rust i64) — use i128/u128 for sums of 10^18 quantities
- custom Unicode/grapheme/byte rules that differ from the standard library
- wording that redefines behavior after exhaustion, reset, or removal
===END===

{{previous_attempt}}
Problem statement:

{{statement}}
