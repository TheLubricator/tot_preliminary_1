#!/usr/bin/env python3
"""
Arithmetic correctness checker for 24-game training data.

Checks per completion:
  1. ARITHMETIC      — each step's a OP b = c is mathematically correct
  2. POOL_STATE      — (left: ...) matches the computed pool after each step
  3. NO_NEW_NUMBERS  — both operands actually exist in the current pool
  4. FALSE_DEADEND   — backtrack only from states that truly cannot reach 24
  5. BACKTRACK_TARGET— backtrack "to:" target exists in state history
  6. ANSWER_EVAL     — the Answer expression evaluates to 24
  7. ANSWER_NUMBERS  — the Answer uses exactly the puzzle's numbers (multiset)
  8. NO_ANSWER       — completion ends without an Answer line

Usage:
    python3 check_arithmetic.py [--verbose] [--file PATH]
"""

import json
import re
import sys
import argparse
from fractions import Fraction
from collections import Counter

TARGET = Fraction(24)
INPUT_PATH = "training_data_augmented_v2.jsonl"

# ─── Exhaustive 24-reachability solver ───────────────────────────────────────

def can_reach_24(nums: list) -> bool:
    """Return True if any combination of +,-,*,/ on nums reaches exactly 24."""
    nums = [Fraction(n) for n in nums]
    if len(nums) == 1:
        return nums[0] == TARGET
    for i in range(len(nums)):
        for j in range(len(nums)):
            if i == j:
                continue
            a, b = nums[i], nums[j]
            rest = [nums[k] for k in range(len(nums)) if k != i and k != j]
            candidates = [a + b, a - b, a * b]
            if b != 0:
                candidates.append(a / b)
            for c in candidates:
                if can_reach_24(rest + [c]):
                    return True
    return False


# ─── Parsing helpers ─────────────────────────────────────────────────────────

def parse_frac(s: str) -> Fraction:
    s = s.strip()
    if '.' in s:
        return Fraction(s).limit_denominator(10 ** 9)
    return Fraction(s)

def pool_str(pool: list) -> str:
    return '[' + ', '.join(str(n) for n in sorted(pool)) + ']'


# ─── Regexes ─────────────────────────────────────────────────────────────────

STEP_RE = re.compile(
    r'^(-?[\d./]+)\s*([+\-*/])\s*(-?[\d./]+)\s*=\s*(-?[\d./]+)\s*\(left:\s*(.*?)\)\s*$'
)
BACKTRACK_RE = re.compile(r'^Backtrack\.\s*\(to:\s*(.*?)\)\s*\.?\s*$')
ANSWER_RE    = re.compile(r'^Answer:\s*(.*?)\s*=\s*(-?[\d./]+)\s*$')


# ─── Per-record checker ───────────────────────────────────────────────────────

def check_record(record: dict) -> list:
    """
    Returns a list of (error_type, line_number, message) tuples.
    Empty list = clean record.
    """
    errors = []
    puzzle_nums  = [Fraction(x) for x in record['puzzle'].split()]
    lines        = record['completion'].strip().split('\n')

    current_pool  = sorted(puzzle_nums)
    state_history = [sorted(puzzle_nums)]   # index 0 = initial state
    found_answer  = False

    for line_idx, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue

        # ── Step line ────────────────────────────────────────────────────
        m = STEP_RE.match(line)
        if m:
            a_s, op, b_s, res_s, left_s = m.groups()
            try:
                a, b, result = parse_frac(a_s), parse_frac(b_s), parse_frac(res_s)
            except Exception as e:
                errors.append(('PARSE', line_idx, f"Cannot parse numbers in: '{line}' — {e}"))
                continue

            # 1. Arithmetic correctness
            if   op == '+': expected = a + b
            elif op == '-': expected = a - b
            elif op == '*': expected = a * b
            elif op == '/':
                if b == 0:
                    errors.append(('DIV_ZERO', line_idx, f"Division by zero: '{line}'"))
                    continue
                expected = a / b

            if expected != result:
                errors.append(('ARITHMETIC', line_idx,
                    f"{a} {op} {b} should be {expected}, stated {result}"))

            # 2. Operands must exist in pool (no new numbers)
            pool_copy = list(current_pool)
            for operand, label in [(a, 'first'), (b, 'second')]:
                if operand in pool_copy:
                    pool_copy.remove(operand)
                else:
                    errors.append(('NO_NEW_NUMBERS', line_idx,
                        f"{label} operand {operand} not in pool {pool_str(current_pool)}"))

            # Compute new pool using expected (not stated) result
            pool_copy.append(expected)
            pool_copy.sort()

            # 3. Left-state matches
            try:
                left_nums = sorted([parse_frac(x) for x in left_s.split()])
            except Exception as e:
                errors.append(('PARSE', line_idx, f"Cannot parse left state '{left_s}': {e}"))
                left_nums = pool_copy

            if left_nums != pool_copy:
                errors.append(('POOL_STATE', line_idx,
                    f"(left:) states {left_nums}, computed {pool_copy}"))

            current_pool = pool_copy
            state_history.append(list(current_pool))
            continue

        # ── Backtrack line ───────────────────────────────────────────────
        m = BACKTRACK_RE.match(line)
        if m:
            try:
                to_nums = sorted([parse_frac(x) for x in m.group(1).split()])
            except Exception as e:
                errors.append(('PARSE', line_idx, f"Cannot parse backtrack target: {e}"))
                continue

            # 4. Current state must be a genuine dead end
            if can_reach_24(current_pool):
                errors.append(('FALSE_DEADEND', line_idx,
                    f"Backtrack from {pool_str(current_pool)} but it CAN reach 24 — not a dead end"))

            # 5. Target must exist in previous history
            history_sorted = [sorted(s) for s in state_history]
            if to_nums not in history_sorted[:-1]:   # exclude current state
                errors.append(('BACKTRACK_TARGET', line_idx,
                    f"Backtrack target {to_nums} not found in state history"))
            else:
                # Restore state and trim history
                for idx in range(len(state_history) - 1, -1, -1):
                    if sorted(state_history[idx]) == to_nums:
                        state_history = state_history[:idx + 1]
                        break
                current_pool = to_nums
            continue

        # ── Answer line ──────────────────────────────────────────────────
        m = ANSWER_RE.match(line)
        if m:
            expr, res_s = m.groups()
            found_answer = True

            # 6a. Stated result should be 24
            try:
                stated = parse_frac(res_s)
                if stated != TARGET:
                    errors.append(('ANSWER_VALUE', line_idx,
                        f"Answer states result = {stated}, expected 24"))
            except Exception as e:
                errors.append(('PARSE', line_idx, f"Cannot parse answer value '{res_s}': {e}"))

            # 6b. Expression evaluates to 24
            try:
                eval_val = Fraction(eval(expr))    # safe: only arithmetic in these expressions
                if eval_val != TARGET:
                    errors.append(('ANSWER_EVAL', line_idx,
                        f"Expression '{expr}' evaluates to {eval_val}, not 24"))
            except Exception as e:
                errors.append(('ANSWER_EVAL', line_idx,
                    f"Cannot evaluate expression '{expr}': {e}"))

            # 7. Numbers in expression must match puzzle numbers exactly
            try:
                expr_nums = sorted([Fraction(x) for x in re.findall(r'\d+', expr)])
                puzzle_sorted = sorted(puzzle_nums)
                if expr_nums != puzzle_sorted:
                    errors.append(('ANSWER_NUMBERS', line_idx,
                        f"Answer uses {expr_nums} but puzzle is {puzzle_sorted}"))
            except Exception as e:
                errors.append(('PARSE', line_idx, f"Cannot extract numbers from answer expr: {e}"))

            # Pool should be [24] at this point
            if current_pool != [TARGET]:
                errors.append(('POOL_STATE', line_idx,
                    f"Pool before Answer is {pool_str(current_pool)}, expected [24]"))
            continue

        # ── Unrecognised line ────────────────────────────────────────────
        errors.append(('UNKNOWN_LINE', line_idx, f"Unrecognised format: '{line}'"))

    # 8. Must have an Answer line
    if not found_answer:
        errors.append(('NO_ANSWER', 0, "Completion has no Answer line"))

    return errors


# ─── Main ─────────────────────────────────────────────────────────────────────

CATEGORY_DESC = {
    'ARITHMETIC':       'Step arithmetic wrong  (a OP b ≠ stated result)',
    'POOL_STATE':       'Left-state mismatch    (wrong numbers in pool)',
    'NO_NEW_NUMBERS':   'Operand not in pool    (new number introduced)',
    'FALSE_DEADEND':    'Fake dead end          (pool can still reach 24)',
    'BACKTRACK_TARGET': 'Bad backtrack target   (not in state history)',
    'ANSWER_VALUE':     'Answer value ≠ 24      (stated result wrong)',
    'ANSWER_EVAL':      'Answer expression ≠ 24 (eval mismatch)',
    'ANSWER_NUMBERS':   'Answer numbers wrong   (don\'t match puzzle)',
    'NO_ANSWER':        'No Answer line         (incomplete completion)',
    'DIV_ZERO':         'Division by zero',
    'PARSE':            'Parse error            (malformed line)',
    'UNKNOWN_LINE':     'Unrecognised line      (format not understood)',
}

def main():
    parser = argparse.ArgumentParser(description="Check 24-game training data correctness")
    parser.add_argument('--file',    default=INPUT_PATH,  help="Path to .jsonl file")
    parser.add_argument('--verbose', action='store_true', help="Print every error with full context")
    args = parser.parse_args()

    with open(args.file) as f:
        records = [json.loads(line) for line in f]

    total             = len(records)
    records_with_errors = 0
    error_counts      = Counter()
    error_log         = []   # (rec_idx, puzzle, errors)
    first_example     = {}   # category -> first occurrence detail

    print(f"Checking {total} records …", flush=True)

    for rec_idx, record in enumerate(records):
        errs = check_record(record)
        if errs:
            records_with_errors += 1
            error_log.append((rec_idx, record['puzzle'], errs))
            for etype, line_no, msg in errs:
                error_counts[etype] += 1
                if etype not in first_example:
                    first_example[etype] = {
                        'idx': rec_idx + 1,
                        'puzzle': record['puzzle'],
                        'line': line_no,
                        'msg': msg,
                        'completion': record['completion'],
                    }

    clean = total - records_with_errors

    # ── Summary ───────────────────────────────────────────────────────────
    W = 60
    print("\n" + "=" * W)
    print("  ARITHMETIC CORRECTNESS REPORT")
    print("=" * W)
    print(f"  Total records    : {total}")
    print(f"  ✓ Clean          : {clean}  ({clean/total*100:.1f}%)")
    print(f"  ✗ With errors    : {records_with_errors}  ({records_with_errors/total*100:.1f}%)")
    print()

    total_errors = sum(error_counts.values())
    print(f"  Total error occurrences: {total_errors}")
    print()

    if error_counts:
        print("  ERRORS BY CATEGORY")
        print("  " + "-" * (W - 2))
        for etype, cnt in error_counts.most_common():
            desc = CATEGORY_DESC.get(etype, etype)
            bar  = "█" * min(cnt, 30)
            pct  = cnt / total * 100
            print(f"  {etype:<20} {cnt:>5}  ({pct:5.1f}%)  {desc}")
        print()

        print("  FIRST EXAMPLE PER ERROR TYPE")
        print("  " + "-" * (W - 2))
        for etype, ex in first_example.items():
            print(f"\n  [{etype}]  Record #{ex['idx']}, Puzzle: {ex['puzzle']}, Line {ex['line']}")
            print(f"  → {ex['msg']}")
            print("  Completion:")
            for cline in ex['completion'].split('\n'):
                print(f"      {cline}")

    else:
        print("  ✓ ALL RECORDS CORRECT — no errors found!")

    # ── Verbose: every error ───────────────────────────────────────────────
    if args.verbose and error_log:
        print("\n" + "=" * W)
        print("  VERBOSE ERROR LOG")
        print("=" * W)
        for rec_idx, puzzle, errs in error_log:
            print(f"\n  Record #{rec_idx+1}  Puzzle: {puzzle}")
            for etype, line_no, msg in errs:
                print(f"    Line {line_no:>3}  [{etype}]  {msg}")

    # ── Machine-readable summary ───────────────────────────────────────────
    print("\n" + "=" * W)
    print("  JSON SUMMARY (for scripting)")
    print("=" * W)
    summary = {
        "total": total,
        "clean": clean,
        "with_errors": records_with_errors,
        "clean_pct": round(clean / total * 100, 2),
        "error_counts": dict(error_counts),
    }
    print(json.dumps(summary, indent=4))


if __name__ == '__main__':
    main()
