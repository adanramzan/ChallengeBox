A solution failed a concrete check. Make the smallest change that fixes the demonstrated failure. Keep the public contract. Do not switch algorithms unless the failure proves the algorithm wrong. {{language_rules}}

Failure kind: {{kind}}
Input:
{{input}}
Expected (from an independent literal reference, which may itself be wrong):
{{expected}}
Actual:
{{actual}}
Details:
{{details}}
{{previous_attempt}}
First, in one sentence, name the specific expression or line that produces the wrong value on this
input. Then change only what that sentence names. A rewrite of the whole approach is almost never the
right response to a single failing input; if you believe the approach itself is fundamentally wrong,
say so plainly before ===VERDICT=== instead of silently rewriting it.

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
===END===
===CODE===
the full corrected solution (repeat the current one unchanged if VERDICT is oracle)
===END===

Statement:

{{statement}}

Current solution:

{{code}}
