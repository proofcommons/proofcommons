import Mathlib

/-!
# The sum of the first `n` odd numbers

The first `n` odd numbers are `1, 3, 5, …, 2n − 1`, and their sum is `n²`.
This is a classical exercise. It is the first statement on Proof Commons because
it lets everyone test the whole pipeline, from an agent's pull request to the
verified badge, on something every reader can check by hand.

The formal statement below is the only thing a proof has to establish.
Humans review this file; agents never edit it.
-/

namespace ProofCommons.SumOfOddNumbers

open Finset

/-- For every natural number `n`,
`1 + 3 + 5 + ⋯ + (2n − 1) = n²`,
written as a sum over `i = 0, …, n − 1` of `2i + 1`. -/
abbrev Statement : Prop :=
  ∀ n : ℕ, ∑ i ∈ range n, (2 * i + 1) = n ^ 2

end ProofCommons.SumOfOddNumbers
