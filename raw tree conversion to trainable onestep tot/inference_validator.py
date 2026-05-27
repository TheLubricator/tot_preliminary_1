"""
inference_validator.py
=======================
Fix 2 (Inference-side): Deterministic step validator that intercepts each
SmolLM-generated step, verifies it with Python arithmetic, and injects
[dead end] + retry signals when a step is wrong.

Why this is needed alongside the training fix:
  Training teaches SmolLM what [dead end] means and what comes after it.
  But SmolLM cannot detect its own arithmetic errors at generation time —
  it needs an external signal telling it "that step was wrong."
  Training alone (Fix 1) is not enough; inference alone (Fix 2) is not enough.
  Together they form a self-correcting loop.

Architecture:
  StepValidator         — pure arithmetic checker (zero LLM calls)
  ConstrainedSolver     — wraps any text generator with the retry loop
  Game24EvalHarness     — runs the full evaluation on a dataset

Usage (minimal):
    from inference_validator import ConstrainedSolver

    def my_model(prompt: str) -> str:
        # Your SmolLM inference call here
        ...

    solver = ConstrainedSolver(model_fn=my_model)
    result = solver.solve("2 3 4 9")
    print(result)

Research basis:
  Friend's convo Fix 2 — "The validator does what the paper's LLM evaluator
  did — but deterministically. Zero LLM calls needed."
  Paper's Multi-Step ToT: used an LLM to score each branch as
  sure/maybe/impossible. This validator does the same with Python arithmetic.
"""

import re
import math
from dataclasses import dataclass, field
from typing import Optional, Callable

# ─────────────────────────────────────────────
#  Constants  (matching the training format)
# ─────────────────────────────────────────────

DEAD_END_TAG   = "[dead end]"
MAX_STEP_RETRIES    = 3     # Retries per failed step
MAX_FULL_RESTARTS   = 5     # Full solution restarts if step fails all retries
TOLERANCE           = 1e-6  # Float comparison tolerance for arithmetic check


# ─────────────────────────────────────────────
#  Data types
# ─────────────────────────────────────────────

@dataclass
class StepValidation:
    """Result of validating a single generated step."""
    is_valid: bool
    error: str = ""
    a: float = 0.0
    op: str = ""
    b: float = 0.0
    result: float = 0.0
    new_numbers: list = field(default_factory=list)


@dataclass
class SolverResult:
    """Full outcome of one solve() call."""
    success: bool
    answer: str = ""            # e.g. "((2*9)/3)*4 = 24"
    steps: list = field(default_factory=list)
    full_output: str = ""       # The complete generated text (steps + answer line)
    total_retries: int = 0
    total_restarts: int = 0
    steps_generated: int = 0    # Total steps generated (including failed ones)
    failure_reason: str = ""


# ─────────────────────────────────────────────
#  StepValidator
# ─────────────────────────────────────────────

STEP_PATTERN = re.compile(
    r'(-?\d+\.?\d*)\s*([+\-*/])\s*(-?\d+\.?\d*)\s*=\s*(-?\d+\.?\d*)'
)

class StepValidator:
    """
    Deterministic arithmetic validator for a single Game of 24 step.

    A step is valid if and only if:
      1. It can be parsed as "a OP b = result"
      2. a and b are both in the current available numbers
      3. result == a OP b (within TOLERANCE)
      4. 24 is still reachable from the new remaining numbers
         (handled by the caller via the should_prune check)
    """

    def validate(self, step_line: str, available_numbers: list) -> StepValidation:
        """
        Validate one step line against the current list of available numbers.

        Args:
            step_line:         e.g. "2 * 9 = 18 (left: 18 3 4)"
                               or   "9 / 2 = 4.5 (left: 4.5 3 4)"
            available_numbers: list of floats currently available to use

        Returns:
            StepValidation with is_valid=True and new_numbers on success,
            or is_valid=False and error message on failure.
        """
        # ── Parse the arithmetic expression ──
        m = STEP_PATTERN.search(step_line)
        if not m:
            return StepValidation(
                is_valid=False,
                error=f"Cannot parse step '{step_line}' — expected format: 'a OP b = result'"
            )

        a     = float(m.group(1))
        op    = m.group(2)
        b     = float(m.group(3))
        result = float(m.group(4))

        # ── Check a and b are available ──
        avail = list(available_numbers)   # copy so we don't mutate

        def consume(val: float, pool: list) -> Optional[list]:
            """Remove val from pool if present. Returns new pool or None."""
            for i, v in enumerate(pool):
                if abs(v - val) < TOLERANCE:
                    return pool[:i] + pool[i+1:]
            return None

        pool_after_a = consume(a, avail)
        if pool_after_a is None:
            return StepValidation(
                is_valid=False,
                error=f"{self._fmt(a)} not in available numbers {self._fmt_list(avail)}"
            )

        pool_after_b = consume(b, pool_after_a)
        if pool_after_b is None:
            return StepValidation(
                is_valid=False,
                error=f"{self._fmt(b)} not available after using {self._fmt(a)} "
                      f"(remaining: {self._fmt_list(pool_after_a)})"
            )

        # ── Verify arithmetic ──
        try:
            expected = self._apply_op(a, op, b)
        except ZeroDivisionError:
            return StepValidation(
                is_valid=False,
                error=f"Division by zero: {self._fmt(a)} / {self._fmt(b)}"
            )

        if abs(expected - result) > TOLERANCE:
            return StepValidation(
                is_valid=False,
                error=(f"Arithmetic error: {self._fmt(a)} {op} {self._fmt(b)} = "
                       f"{self._fmt(expected)}, not {self._fmt(result)}")
            )

        # ── Build new remaining numbers ──
        new_numbers = [result] + pool_after_b

        # ── Check solution ──
        if len(new_numbers) == 1 and abs(new_numbers[0] - 24) < TOLERANCE:
            # This step is the final step — valid solution
            pass
        elif len(new_numbers) == 0:
            return StepValidation(
                is_valid=False,
                error="No numbers left after this step"
            )

        return StepValidation(
            is_valid=True,
            a=a, op=op, b=b, result=result,
            new_numbers=new_numbers
        )

    # ── helpers ──
    @staticmethod
    def _apply_op(a: float, op: str, b: float) -> float:
        if op == '+': return a + b
        if op == '-': return a - b
        if op == '*': return a * b
        if op == '/':
            if abs(b) < TOLERANCE:
                raise ZeroDivisionError
            return a / b
        raise ValueError(f"Unknown operator: {op}")

    @staticmethod
    def _fmt(x: float) -> str:
        return str(int(x)) if x == int(x) else str(x)

    @staticmethod
    def _fmt_list(lst: list) -> str:
        def f(x): return str(int(x)) if x == int(x) else str(x)
        return "[" + ", ".join(f(v) for v in lst) + "]"


# ─────────────────────────────────────────────
#  ConstrainedSolver
# ─────────────────────────────────────────────

class ConstrainedSolver:
    """
    Wraps a text-generation model with the constrained step-by-step
    inference loop from Fix 2.

    The loop:
      For each step position:
        1. Ask the model to generate the next step (using accumulated prompt)
        2. Validate with StepValidator
        3a. Valid → append to accepted steps, continue
        3b. Invalid → append "[dead end]", retry (up to MAX_STEP_RETRIES)
        3c. All retries failed → trigger full restart (up to MAX_FULL_RESTARTS)

    This teaches the same recovery pattern that is in the training data:
      "9 / 2 = 4.5 (left: 4.5 3 4) [dead end]
       2 * 9 = 18 ..."

    But SmolLM now sees [dead end] as an external signal it learned to
    respond to — generating a different operation next.
    """

    def __init__(
        self,
        model_fn: Callable[[str], str],
        max_step_retries: int = MAX_STEP_RETRIES,
        max_full_restarts: int = MAX_FULL_RESTARTS,
        verbose: bool = False,
    ):
        """
        Args:
            model_fn:          Function that takes a prompt string and returns
                               the model's next-token completion string.
                               Should return the NEXT SINGLE STEP only —
                               use stop tokens or truncation in your model call
                               to enforce this (stop at newline).
            max_step_retries:  Retries per failed step (default: 3).
            max_full_restarts: Full solution restarts (default: 5).
            verbose:           Print step-by-step trace for debugging.
        """
        self.model_fn = model_fn
        self.max_step_retries = max_step_retries
        self.max_full_restarts = max_full_restarts
        self.verbose = verbose
        self.validator = StepValidator()

    def _build_base_prompt(
        self,
        puzzle_numbers: list,
        dead_end_context: Optional[str] = None,
    ) -> str:
        """Build the fixed prompt prefix (matches training format)."""
        nums_str = " ".join(
            str(int(x)) if x == int(x) else str(x)
            for x in puzzle_numbers
        )
        lines = [
            f"Numbers: {nums_str}. Target: 24.",
            "Use each number exactly once with +, -, *, / to reach 24.",
        ]
        if dead_end_context:
            lines.append(dead_end_context)
        lines.append("Steps:")
        return "\n".join(lines)

    def _extract_answer(self, generated_text: str) -> Optional[str]:
        """Extract the Answer line from generated text."""
        for line in generated_text.split("\n"):
            if line.strip().startswith("Answer:"):
                return line.strip()
        return None

    def solve(
        self,
        puzzle: str,
        dead_end_context: Optional[str] = None,
    ) -> SolverResult:
        """
        Solve a Game of 24 puzzle with constrained step-by-step inference.

        Args:
            puzzle:            Space-separated numbers, e.g. "2 3 4 9"
            dead_end_context:  Optional dead-end hint block for the prompt
                               (from get_relevant_deadend_patterns in
                               extract_training_example.py).

        Returns:
            SolverResult with success status, accepted steps, and stats.
        """
        try:
            puzzle_numbers = [float(x) for x in puzzle.strip().split()]
        except ValueError:
            return SolverResult(
                success=False,
                failure_reason=f"Cannot parse puzzle numbers: '{puzzle}'"
            )

        base_prompt = self._build_base_prompt(puzzle_numbers, dead_end_context)
        total_retries = 0
        total_restarts = 0
        steps_generated = 0

        for restart_num in range(self.max_full_restarts):
            if restart_num > 0:
                total_restarts += 1
                if self.verbose:
                    print(f"\n↺ Full restart {restart_num}/{self.max_full_restarts - 1}")

            result = self._run_one_attempt(
                base_prompt=base_prompt,
                initial_numbers=puzzle_numbers,
                restart_num=restart_num,
            )

            total_retries  += result["retries"]
            steps_generated += result["steps_generated"]

            if result["success"]:
                return SolverResult(
                    success=True,
                    answer=result["answer"],
                    steps=result["accepted_steps"],
                    full_output=result["full_output"],
                    total_retries=total_retries,
                    total_restarts=total_restarts,
                    steps_generated=steps_generated,
                )

        # All restarts failed
        return SolverResult(
            success=False,
            total_retries=total_retries,
            total_restarts=total_restarts,
            steps_generated=steps_generated,
            failure_reason=(
                f"Failed after {self.max_full_restarts} full attempts. "
                f"Total step retries: {total_retries}."
            ),
        )

    def _run_one_attempt(
        self,
        base_prompt: str,
        initial_numbers: list,
        restart_num: int,
    ) -> dict:
        """
        Run one full solution attempt from scratch.
        Returns a dict with success status and stats.
        """
        prompt_so_far = base_prompt + "\n"
        remaining_numbers = list(initial_numbers)
        accepted_steps = []
        full_output_lines = []
        attempt_retries = 0
        attempt_steps_generated = 0
        max_steps = len(initial_numbers) - 1  # e.g. 3 steps for 4 numbers

        for step_pos in range(max_steps):
            if self.verbose:
                print(f"\n  Step {step_pos + 1}: remaining = {remaining_numbers}")

            step_accepted = False

            for retry in range(self.max_step_retries):
                # ── Generate next step ──
                generated = self.model_fn(prompt_so_far).strip()
                # Take only the first line (one step at a time)
                generated_line = generated.split("\n")[0].strip()
                attempt_steps_generated += 1

                if self.verbose:
                    print(f"    [attempt {retry + 1}] generated: '{generated_line}'")

                # Check if model returned an Answer line instead of a step
                if generated_line.startswith("Answer:"):
                    # Model thinks it's done — this is only valid if 1 number left == 24
                    if len(remaining_numbers) == 1 and abs(remaining_numbers[0] - 24) < TOLERANCE:
                        answer_line = generated_line
                        full_output_lines.append(answer_line)
                        return {
                            "success": True,
                            "answer": answer_line,
                            "accepted_steps": accepted_steps,
                            "full_output": "\n".join(full_output_lines),
                            "retries": attempt_retries,
                            "steps_generated": attempt_steps_generated,
                        }
                    else:
                        # Premature answer — treat as invalid step
                        error_msg = (f"Premature Answer at step {step_pos + 1} "
                                     f"with {remaining_numbers} remaining")
                        if self.verbose:
                            print(f"    ✗ {error_msg}")
                        tagged = f"{generated_line} {DEAD_END_TAG}"
                        prompt_so_far += tagged + "\n"
                        full_output_lines.append(tagged)
                        attempt_retries += 1
                        continue

                # ── Validate ──
                validation = self.validator.validate(generated_line, remaining_numbers)

                if validation.is_valid:
                    if self.verbose:
                        print(f"    ✓ valid")
                    prompt_so_far += generated_line + "\n"
                    full_output_lines.append(generated_line)
                    accepted_steps.append(generated_line)
                    remaining_numbers = validation.new_numbers
                    step_accepted = True
                    break
                else:
                    if self.verbose:
                        print(f"    ✗ invalid: {validation.error}")
                    tagged = f"{generated_line} {DEAD_END_TAG}"
                    prompt_so_far += tagged + "\n"
                    full_output_lines.append(tagged)
                    attempt_retries += 1

            if not step_accepted:
                # All retries for this step failed — trigger full restart
                if self.verbose:
                    print(f"  ✗ Step {step_pos + 1} failed all {self.max_step_retries} retries.")
                return {
                    "success": False,
                    "answer": "",
                    "accepted_steps": accepted_steps,
                    "full_output": "\n".join(full_output_lines),
                    "retries": attempt_retries,
                    "steps_generated": attempt_steps_generated,
                }

            # ── Check if solved after this step ──
            if len(remaining_numbers) == 1 and abs(remaining_numbers[0] - 24) < TOLERANCE:
                # Ask model for the Answer line
                generated = self.model_fn(prompt_so_far).strip()
                answer_line = generated.split("\n")[0].strip()
                if not answer_line.startswith("Answer:"):
                    answer_line = f"Answer: = 24"  # Fallback
                full_output_lines.append(answer_line)
                return {
                    "success": True,
                    "answer": answer_line,
                    "accepted_steps": accepted_steps,
                    "full_output": "\n".join(full_output_lines),
                    "retries": attempt_retries,
                    "steps_generated": attempt_steps_generated,
                }

        # Reached max_steps without solving
        return {
            "success": False,
            "answer": "",
            "accepted_steps": accepted_steps,
            "full_output": "\n".join(full_output_lines),
            "retries": attempt_retries,
            "steps_generated": attempt_steps_generated,
        }


# ─────────────────────────────────────────────
#  Game24EvalHarness
# ─────────────────────────────────────────────

class Game24EvalHarness:
    """
    Evaluation harness: runs the ConstrainedSolver across a dataset and
    reports accuracy, retry statistics, and comparison vs paper baseline.
    """

    def __init__(self, solver: ConstrainedSolver):
        self.solver = solver

    def evaluate(self, puzzles: list, verbose_failures: bool = False) -> dict:
        """
        Args:
            puzzles: list of puzzle strings, e.g. ["2 3 4 9", "1 1 4 6", ...]

        Returns:
            {
                "accuracy": 0.XX,
                "solved": N,
                "total": N,
                "avg_retries_per_puzzle": X.X,
                "avg_restarts_per_puzzle": X.X,
                "avg_steps_generated_per_puzzle": X.X,
                "efficiency_ratio": X.X,  # accepted_steps / steps_generated
                "results": [...per puzzle details...]
            }
        """
        results = []
        solved = 0

        for i, puzzle in enumerate(puzzles):
            result = self.solver.solve(puzzle)
            results.append({
                "puzzle": puzzle,
                "success": result.success,
                "answer": result.answer,
                "steps": result.steps,
                "retries": result.total_retries,
                "restarts": result.total_restarts,
                "steps_generated": result.steps_generated,
                "failure_reason": result.failure_reason,
            })

            if result.success:
                solved += 1
            elif verbose_failures:
                print(f"[{i+1}/{len(puzzles)}] FAIL '{puzzle}': {result.failure_reason}")

        total = len(puzzles)
        total_retries = sum(r["retries"] for r in results)
        total_restarts = sum(r["restarts"] for r in results)
        total_steps_gen = sum(r["steps_generated"] for r in results)
        total_accepted = sum(len(r["steps"]) for r in results)

        return {
            "accuracy": round(solved / total, 4) if total else 0,
            "solved": solved,
            "total": total,
            "avg_retries_per_puzzle": round(total_retries / total, 2) if total else 0,
            "avg_restarts_per_puzzle": round(total_restarts / total, 2) if total else 0,
            "avg_steps_generated_per_puzzle": round(total_steps_gen / total, 2) if total else 0,
            "efficiency_ratio": round(total_accepted / max(total_steps_gen, 1), 3),
            "results": results,
        }


# ─────────────────────────────────────────────
#  Self-test (validator unit tests — no model needed)
# ─────────────────────────────────────────────

def _run_validator_tests():
    v = StepValidator()
    PASS = "✓"
    FAIL = "✗"

    tests = [
        # (description, step_line, available, expect_valid)
        ("correct multiply",       "2 * 9 = 18 (left: 18 3 4)",  [2, 3, 4, 9], True),
        ("correct divide",         "18 / 3 = 6 (left: 6 4)",     [18, 3, 4],   True),
        ("correct final step",     "6 * 4 = 24 (left: 24)",      [6, 4],       True),
        ("arithmetic error",       "2 * 9 = 19 (left: 19 3 4)",  [2, 3, 4, 9], False),
        ("number not available",   "5 + 9 = 14 (left: 14 3 4)",  [2, 3, 4, 9], False),
        ("using same number twice","9 * 9 = 81 (left: 81 3 4)",  [2, 3, 4, 9], False),
        ("division by zero",       "9 / 0 = inf",                [2, 3, 4, 9], False),
        ("dead-end fractions",     "9 / 2 = 4.5 (left: 4.5 3 4)",[2, 3, 4, 9], True),
        ("subtraction",            "9 - 2 = 7 (left: 7 3 4)",   [2, 3, 4, 9], True),
        ("addition",               "3 + 4 = 7 (left: 7 2 9)",   [2, 3, 4, 9], True),
    ]

    print("StepValidator unit tests")
    print("─" * 60)
    all_pass = True
    for desc, step, avail, expected in tests:
        r = v.validate(step, avail)
        ok = r.is_valid == expected
        all_pass = all_pass and ok
        status = PASS if ok else FAIL
        detail = "" if ok else f"\n   → got is_valid={r.is_valid}, error='{r.error}'"
        print(f"  {status} {desc}{detail}")

    print()
    if all_pass:
        print("All tests passed.")
    else:
        print("Some tests FAILED — check output above.")
    return all_pass


def _demo_constrained_solver():
    """
    Demo: simulate SmolLM that makes an arithmetic error on step 1,
    then recovers correctly when it sees [dead end] in the prompt.
    The mock model inspects the prompt to decide what to generate next.
    """

    def mock_model(prompt: str) -> str:
        """
        Prompt-aware mock model that:
          - Step 1, no [dead end] yet → arithmetic ERROR (validator rejects it)
          - Step 1, after [dead end] injected → correct first step
          - Step 2 → correct second step  
          - Step 3 → correct third step
          - After solution → Answer line
        """
        if "Answer:" in prompt:
            return "Answer: ((2 * 9) / 3) * 4 = 24"

        # Count accepted steps by looking for "(left:" lines without [dead end]
        accepted_steps = [
            line for line in prompt.split("\n")
            if "(left:" in line and DEAD_END_TAG not in line
        ]
        n_accepted = len(accepted_steps)

        if n_accepted == 0:
            # First step — return WRONG arithmetic if no [dead end] yet
            if DEAD_END_TAG not in prompt:
                # Wrong: 2 * 9 = 19 is arithmetically wrong (actually 18)
                return "2 * 9 = 19 (left: 19 3 4)"
            else:
                # Model saw [dead end] and now generates the CORRECT step
                return "2 * 9 = 18 (left: 18 3 4)"

        elif n_accepted == 1:
            return "18 / 3 = 6 (left: 6 4)"

        elif n_accepted == 2:
            return "6 * 4 = 24 (left: 24)"

        return "Answer: ((2 * 9) / 3) * 4 = 24"

    solver = ConstrainedSolver(model_fn=mock_model, verbose=True)
    result = solver.solve("2 3 4 9")

    print("\n── ConstrainedSolver demo ──")
    print(f"Success:        {result.success}")
    print(f"Answer:         {result.answer}")
    print(f"Accepted steps: {result.steps}")
    print(f"Total retries:  {result.total_retries}")
    print(f"Full output:")
    print(result.full_output)


if __name__ == "__main__":
    print("=" * 60)
    _run_validator_tests()
    print()
    print("=" * 60)
    _demo_constrained_solver()
