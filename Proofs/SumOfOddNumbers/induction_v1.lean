import Mathlib
import Statements.SumOfOddNumbers

/-!
# The sum of the first `n` odd numbers, by induction

A direct induction on `n`. The successor step peels off the last summand with
`Finset.sum_range_succ`, rewrites with the induction hypothesis, and closes the
arithmetic identity `k ^ 2 + (2 * k + 1) = (k + 1) ^ 2` with `ring`.
-/

namespace ProofCommons.SumOfOddNumbers.induction_v1

open Finset

theorem proof : Statement := by
  intro n
  induction n with
  | zero => simp
  | succ k ih => rw [sum_range_succ, ih]; ring

end ProofCommons.SumOfOddNumbers.induction_v1
