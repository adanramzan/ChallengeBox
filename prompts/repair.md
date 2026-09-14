A solution failed a concrete check. Make the smallest change that fixes the demonstrated failure. Keep the public contract. Do not switch algorithms unless the failure proves the algorithm wrong. {{language_rules}}

Failure kind: {{kind}}
Input:
{{input}}
Expected ({{expected_source}}):
{{expected}}
Actual:
{{actual}}
Details:
{{details}}

Scope of the disagreement: this solution {{agreement}}.

When it disagrees on most of the inputs, this is not a boundary bug: the two sides are reading one
rule of the statement differently. Re-read the statement for the rule they disagree about, name that
rule, and fix the reading — a tweak at the failing value will not move the other cases. When only a
few inputs disagree, the boundary-bug framing above is the right one.
{{previous_attempt}}
First, in one sentence, name the specific expression or line that produces the wrong value on this
input. Then change only what that sentence names. A rewrite of the whole approach is almost never the
right response to a single failing input; if you believe the approach itself is fundamentally wrong,
say so plainly before ===VERDICT=== instead of silently rewriting it.

If the failure proves this approach cannot meet the stated limits, or the algorithm is wrong at its core, the verdict is `approach`: say so plainly in one sentence, name the quantity it is too expensive in, and return the current code unchanged. Do not patch one output while keeping an approach that iterates a count the statement bounds by a huge number, or rescans the whole structure once per operation — a smaller edit cannot change the complexity of an approach. A fresh solution will be written from a different algorithm, and this verdict is what asks for one.

Then hand-trace the statement on this input, and decide which side is actually wrong.

Both sides were written by a model from the same prose, so either can be the one at fault. Measured
across real runs of this system, roughly one disagreement in four is the reference's mistake, not the
solution's — yet the verdict comes back `candidate` almost every time. That gap is a bias, not a fact
about the code, and it is expensive: blaming the solution for the reference's error spends a scarce
repair attempt making correct code worse.

So before concluding `candidate`, trace what the **reference** would produce on this input and check
that against the statement's own words. Ask specifically whether the expected value is what the
statement requires here — the right number of output fields, the right tie-break, the right behaviour
in the boundary or exhausted case. If the expected value is not what the statement says, the verdict
is `oracle`, even when the solution also looks imperfect. Only conclude `candidate` once you can point
to the sentence the solution violates.

Respond with:

===VERDICT===
candidate   (if the solution is wrong)  or  oracle   (if the reference is wrong and the solution is right)
or  approach   (if no patch can meet the stated limits: the algorithm itself has to change)
===END===
===CODE===
the full corrected solution (repeat the current one unchanged if VERDICT is oracle or approach)
===END===

Statement:

{{statement}}

Current solution:

{{code}}

Reference implementation — slow and literal by design, and written by a different model from the same
statement; it may be the wrong one. Use it only to locate where the two readings of the statement
diverge. Never copy its approach, its data structures, or its output into the solution.

{{reference}}
