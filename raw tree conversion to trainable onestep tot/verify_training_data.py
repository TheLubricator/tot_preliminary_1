#!/usr/bin/env python3
"""
verify_training_data.py - Check arithmetic correctness of training examples.
Now correctly handles backtracking examples by resetting state on [restart: ...].
"""

import json
import re
import sys
import math
from typing import List, Tuple, Optional

# ------------------------------------------------------------
#  Parsing helpers
# ------------------------------------------------------------
def parse_puzzle_numbers(prompt: str) -> List[float]:
    match = re.search(r'Numbers:\s*([\d\s]+)\.\s*Target:', prompt)
    if not match:
        raise ValueError(f"Cannot parse numbers from prompt: {prompt[:100]}")
    return [float(x) for x in match.group(1).split()]

def parse_restart_numbers(line: str) -> Optional[List[float]]:
    """Extract numbers from '[restart: 1 2 3 4]' or similar."""
    match = re.search(r'\[restart:\s*([\d\.\s]+)\]', line)
    if not match:
        return None
    return [float(x) for x in match.group(1).split()]

def parse_completion_steps(completion: str) -> List[Tuple[str, str, str, float, List[float], bool]]:
    """
    Parse all steps from completion, including dead‑end steps (before restart).
    Each step is (a_str, op, b_str, result, left_numbers, is_after_restart)
    but we will handle state reset separately.
    """
    lines = completion.strip().split('\n')
    steps = []
    for line in lines:
        if line.startswith('Answer:'):
            continue
        # Skip [restart: ...] lines themselves; they are handled separately.
        if '[restart:' in line and not re.search(r'^\s*\d+', line):
            continue
        # Match step pattern: 4 * 5 = 20 (left: 20 1 4)
        match = re.match(r'(-?\d+\.?\d*)\s*([+\-*/])\s*(-?\d+\.?\d*)\s*=\s*(-?\d+\.?\d*)\s*\(left:\s*([^)]+)\)', line)
        if not match:
            # Also handle steps without explicit (left: ...) – should not happen in our data
            continue
        a_str, op, b_str, res_str, left_str = match.groups()
        result = float(res_str)
        left_numbers = [float(x) for x in left_str.split()]
        steps.append((a_str, op, b_str, result, left_numbers))
    return steps

def evaluate_operation(a: float, op: str, b: float) -> float:
    if op == '+': return a + b
    if op == '-': return a - b
    if op == '*': return a * b
    if op == '/':
        if b == 0:
            raise ZeroDivisionError
        return a / b
    raise ValueError(f"Unknown operator {op}")

# ------------------------------------------------------------
#  Verification with restart handling
# ------------------------------------------------------------
def verify_example(entry: dict, tol: float = 1e-3) -> Tuple[bool, str]:
    prompt = entry.get('prompt', '')
    completion = entry.get('completion', '')
    if not prompt or not completion:
        return False, "Missing prompt or completion"

    try:
        initial_numbers = parse_puzzle_numbers(prompt)
    except Exception as e:
        return False, f"Failed to parse puzzle numbers: {e}"

    lines = completion.strip().split('\n')
    # We'll process lines sequentially, handling restarts.
    current = initial_numbers[:]   # mutable state
    step_num = 0
    skip_until_restart = False   # not needed; we just reset on restart

    for line_num, line in enumerate(lines, 1):
        # Detect restart
        if '[restart:' in line:
            restart_nums = parse_restart_numbers(line)
            if restart_nums is None:
                return False, f"Invalid restart marker at line {line_num}"
            current = restart_nums[:]
            continue   # skip this line as a step

        # Try to parse a step
        step_match = re.match(r'(-?\d+\.?\d*)\s*([+\-*/])\s*(-?\d+\.?\d*)\s*=\s*(-?\d+\.?\d*)\s*\(left:\s*([^)]+)\)', line)
        if not step_match:
            # Possibly an answer line or empty
            continue

        a_str, op, b_str, res_str, left_str = step_match.groups()
        a_val = float(a_str)
        b_val = float(b_str)
        expected_res = float(res_str)
        left_after = [float(x) for x in left_str.split()]
        step_num += 1

        # Check that a_val and b_val exist in current list (allow tolerance)
        remaining = list(current)
        found_a = found_b = False
        for i, v in enumerate(remaining):
            if not found_a and math.isclose(v, a_val, rel_tol=tol, abs_tol=tol):
                remaining.pop(i)
                found_a = True
                break
        for i, v in enumerate(remaining):
            if not found_b and math.isclose(v, b_val, rel_tol=tol, abs_tol=tol):
                remaining.pop(i)
                found_b = True
                break
        if not found_a or not found_b:
            return False, f"Step {step_num}: operand(s) {a_str} and/or {b_str} not available in {current}"

        try:
            computed = evaluate_operation(a_val, op, b_val)
        except ZeroDivisionError:
            return False, f"Step {step_num}: division by zero"
        if not math.isclose(computed, expected_res, rel_tol=tol, abs_tol=tol):
            return False, f"Step {step_num}: computed {computed} but expected {expected_res}"

        new_current = remaining + [computed]
        new_sorted = sorted(new_current)
        left_sorted = sorted(left_after)
        if len(new_sorted) != len(left_sorted):
            return False, f"Step {step_num}: length mismatch – left has {len(left_sorted)} numbers"
        for x, y in zip(new_sorted, left_sorted):
            if not math.isclose(x, y, rel_tol=tol, abs_tol=tol):
                return False, f"Step {step_num}: left numbers mismatch – expected {left_sorted} but got {new_sorted}"
        current = new_current

    # After all steps, we must have exactly one number and it must be 24
    if len(current) != 1:
        return False, f"Final state has {len(current)} numbers, expected 1"
    if not math.isclose(current[0], 24, rel_tol=tol, abs_tol=tol):
        return False, f"Final result {current[0]} != 24"
    return True, ""

# ------------------------------------------------------------
#  Main
# ------------------------------------------------------------
def main():
    if len(sys.argv) != 2:
        print("Usage: python verify_training_data.py <training_data.jsonl>")
        sys.exit(1)

    jsonl_path = sys.argv[1]
    valid = 0
    total = 0
    invalid_summary = []

    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"✘ Line {line_num}: JSON decode error – {e}")
                invalid_summary.append((line_num, "JSON error", entry.get('puzzle', '?')))
                continue
            total += 1
            is_valid, error = verify_example(entry)
            if is_valid:
                valid += 1
                print(f"✔ Line {line_num}: valid")
            else:
                puzzle = entry.get('puzzle') or entry.get('prompt', '').split('Numbers:')[1].split('.')[0].strip()
                print(f"✘ Line {line_num}: invalid – {error}")
                invalid_summary.append((line_num, error, puzzle))

    print("\n" + "=" * 50)
    print(f"SUMMARY: {valid} valid / {total} total")
    if valid == total:
        print("All examples are arithmetically correct.")
        sys.exit(0)
    else:
        print(f"\nFailed examples: {len(invalid_summary)}")
        for ln, err, puz in invalid_summary[:20]:   # show first 20
            print(f"  Line {ln}: {puz} -> {err}")
        if len(invalid_summary) > 20:
            print(f"  ... and {len(invalid_summary)-20} more")
        sys.exit(1)

if __name__ == '__main__':
    main()