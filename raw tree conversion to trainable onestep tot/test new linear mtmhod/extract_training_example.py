"""
extract_training_example.py
============================
Converts a CodeAct Tree-of-Thought JSON + dead_end_db.json into
clean (prompt, completion) training pairs for fine-tuning SmolLM-360M.

Key design decisions (research-grounded):
  1. format_step() parses numbers from CODE + OBSERVATION fields only —
     never from the natural-language thought (arXiv 2506.10343).
  2. Dead-end context is encoded in the INPUT prompt, not in the OUTPUT —
     keeping the SLM's generation format identical to the paper's clean
     "Steps + Answer" style (avoids [Exploring]/[Backtrack] tag complexity).
  3. Dead-end context is SELECTIVELY included — only for problems where
     the tree's dead-end memory actually fired AND high-confidence
     generalizable patterns exist. This targets ~20-30% of training
     examples, not 100%, preventing format overload on a 360M model.
"""

import json
import math
import re
import os
from pathlib import Path
from typing import Optional
from fractions import Fraction

# ─────────────────────────────────────────────
#  STEP 1  —  Core step formatter
#  Parses from CODE (structured) + OBSERVATION (Python-executed ground truth)
#  Never parses numbers from the natural-language thought field.
# ─────────────────────────────────────────────

def _operands_in_state(a: float, b: float, state: list, tol: float = 1e-3) -> bool:
    """
    Verify that both operands a and b exist in `state` (each consumed once).
    Used to catch corrupt tree nodes whose code references an intermediate value
    that is not reachable from the parent state (Type A corruption).
    """
    remaining = [float(x) for x in state]
    for val in (a, b):
        found = False
        for i, v in enumerate(remaining):
            if math.isclose(v, val, abs_tol=tol, rel_tol=tol):
                remaining.pop(i)
                found = True
                break
        if not found:
            return False
    return True




def fmt_num(x: float) -> str:
    """Format a number for training data display.
    
    - Integers: no decimal point  →  "8", "-3"
    - Terminating decimals: fixed point  →  "0.5", "2.25"  
    - Non-terminating (e.g. 4/13): fraction form  →  "4/13", "-8/3"
    
    This prevents the truncation bug where 4/13 was stored as 0.3077,
    causing cascading ARITHMETIC + POOL_STATE + NO_NEW_NUMBERS errors.
    """
    f = Fraction(x).limit_denominator(10**9)
    
    # Integer
    if f.denominator == 1:
        return str(f.numerator)
    
    # Check if terminating decimal (denominator = 2^a * 5^b only)
    d = f.denominator
    while d % 2 == 0: d //= 2
    while d % 5 == 0: d //= 5
    
    if d == 1:
        # Terminating — show as decimal, strip trailing zeros
        return f"{float(f):g}"
    else:
        # Non-terminating — show as exact fraction p/q
        return str(f)


def format_step(node: dict) -> Optional[str]:
    """
    Convert a CodeAct tree node into a clean arithmetic line.

    Input node has:
        codeact.code        → 'res = numbers[0] * numbers[3]  # 2 * 9 = 18'
        codeact.observation → '[18, 3, 4]'
        state               → [18, 3, 4]

    Output: '2 * 9 = 18 (left: 18 3 4)'

    Strategy (from arXiv 2506.10343):
        Parse operands + operator from the Python CODE string (structured, reliable).
        Use OBSERVATION as the ground-truth result (Python-executed, never hallucinated).
        Use STATE for the remaining numbers (verified by the tree builder).
    """
    code = node["codeact"]["code"]
    observation = node["codeact"]["observation"].strip()
    state = node["state"]

    # --- Parse operands + operator from code ---
    # Priority 1: explicit literal assignment  →  result = 6.0 * 4
    match = re.search(
        r'(?:res|result)\s*=\s*(-?\d+\.?\d*)\s*([+\-*/])\s*(-?\d+\.?\d*)',
        code
    )
    # Priority 2: inline comment  →  # 2 * 9 = 18
    if not match:
        match = re.search(
            r'#\s*(-?\d+\.?\d*)\s*([+\-*/])\s*(-?\d+\.?\d*)',
            code
        )
    # Priority 3: index-based ops with no literals — resolve from the numbers declaration
    if not match:
        nums_decl = re.search(r'numbers\s*=\s*\[([^\]]+)\]', code)
        idx_op    = re.search(
            r'(?:res|result)\s*=\s*numbers\[(\d+)\]\s*([+\-*/])\s*numbers\[(\d+)\]',
            code
        )
        if nums_decl and idx_op:
            nums = [float(x.strip()) for x in nums_decl.group(1).split(',')]
            i, j = int(idx_op.group(1)), int(idx_op.group(3))
            op_p3 = idx_op.group(2)
            if i < len(nums) and j < len(nums):
                a_str, b_str, op = str(nums[i]), str(nums[j]), op_p3

    if not match:
        return None  # Node cannot be formatted; caller should skip or log

    a_str, op, b_str = match.group(1), match.group(2), match.group(3)
    a, b = float(a_str), float(b_str)

    # --- Compute result directly from operands ---
    ops = {'+': lambda x, y: x + y, '-': lambda x, y: x - y,
           '*': lambda x, y: x * y, '/': lambda x, y: x / y}
    try:
        result = ops[op](a, b)
    except ZeroDivisionError:
        return None

    # --- Verify result appears in observation (the new state list) ---
    if observation:
        obs_numbers = [float(x) for x in re.findall(r'-?\d+\.?\d*', observation)]
        if not any(abs(x - result) < 1e-6 for x in obs_numbers):
            return None   # corrupted node – result not in new state
    else:
        return None

    # --- Verify result is also consistent with the node's own state field ---
    # Catches Type B corruption: observation has the correct result but state stores
    # a wrong value (e.g. sign flip: obs=[-12, 2] but state=[12, 2]).  Using state
    # for left_str while obs gives the real result would produce an inconsistent line.
    state_floats = [float(x) for x in state]
    if not any(math.isclose(x, result, abs_tol=1e-3, rel_tol=1e-3) for x in state_floats):
        return None   # obs/state inconsistency — reject this node

    # --- Remaining state ---
    left_str = ' '.join(fmt_num(float(x)) for x in state)

    return f'{fmt_num(a)} {op} {fmt_num(b)} = {fmt_num(result)} (left: {left_str})'


# ─────────────────────────────────────────────
#  STEP 2  —  Path tracer
# ─────────────────────────────────────────────

def trace_solution_path(nodes: dict, leaf_id: int) -> list:
    """Walk parent pointers from leaf to root, return root→leaf order."""
    path = []
    node = nodes[leaf_id]
    while node is not None:
        path.append(node)
        pid = node["parent_id"]
        node = nodes.get(pid) if pid is not None else None
    return list(reversed(path))  # root first


# ─────────────────────────────────────────────
#  STEP 3  —  Dead-end selectivity
#  Research basis: Encoding negative examples in the INPUT (not the OUTPUT)
#  is consistent with how Constitutional AI and execution-grounded supervision
#  (arXiv 2506.10343) handle negative knowledge — as context, not as a
#  generation target. This keeps SmolLM's output format identical to the
#  paper's clean "Steps + Answer" style.
#
#  Selectivity criterion (prevents 100% dead-end saturation):
#    Condition A — The tree's dead-end memory actually fired (skipped > 0).
#                  This means the puzzle genuinely benefited from the DB.
#    Condition B — ≥ MIN_PATTERNS high-confidence patterns exist for
#                  states reachable from this puzzle's numbers.
#                  hit_count ≥ HIT_COUNT_THRESHOLD filters out
#                  singleton patterns that are noise, not generalizable.
#
#  Expected outcome: ~20-30% of training examples include dead-end context,
#  matching the typical hard-negative ratio in curriculum learning literature.
# ─────────────────────────────────────────────

HIT_COUNT_THRESHOLD = 5   # Pattern must have appeared in ≥ 3 problems
MIN_PATTERNS = 2          # Need at least 2 qualifying patterns to include context
MAX_DEADENDS_IN_PROMPT = 4  # Cap to avoid context bloat


def get_relevant_deadend_patterns(
    puzzle_numbers: list,
    dead_end_db: dict,
    explored_states: set,
) -> list:
    """
    Return high-confidence dead-end patterns relevant to this puzzle.

    Relevance = the pattern's sorted_vals are a reachable intermediate state
    (i.e., present in explored_states) OR share numbers with the puzzle.
    """
    patterns = dead_end_db.get("patterns", [])
    puzzle_set = set(int(float(x)) for x in puzzle_numbers)

    relevant = []
    for p in patterns:
        if p.get("hit_count", 0) < HIT_COUNT_THRESHOLD:
            continue  # Filter noise

        # STRICT MATCH ONLY: This exact state was actively explored and pruned in our tree
        state_key = str(sorted(int(float(x)) for x in p["sorted_vals"]))
        if state_key in explored_states:
            relevant.append(p)

    # Sort by hit_count descending (most generalizable first), cap for prompt length
    relevant.sort(key=lambda x: -x["hit_count"])
    return relevant[:MAX_DEADENDS_IN_PROMPT]


def should_include_deadend_context(
    tree: dict,
    relevant_patterns: list,
) -> bool:
    """
    Always returns False — dead-end context blocks are disabled.

    Rationale: mixing two prompt formats (plain vs. with dead-end hints)
    in the same training set forces the model to learn two conditional
    behaviors simultaneously.  At 360M params and ~1 200 examples that
    splits the signal too thin.  Every prompt is now:

        Numbers: X X X X. Target: 24.
        Use each number exactly once with +, -, *, / to reach 24.
        Steps:

    The negative-example signal that the dead-end context was carrying
    is instead provided implicitly through the backtracking completions
    (failed paths concatenated before the solution), which use only
    arithmetic tokens the model already knows from pre-training.
    """
    return False


def format_deadend_context(relevant_patterns: list) -> str:
    """
    Format dead-end patterns as a compact INPUT-side context block.
    Encoded in the PROMPT, not in the completion — SLM output stays clean.
    """
    lines = ["Known dead-end states (avoid these intermediate results):"]
    for p in relevant_patterns:
        sv = p["sorted_vals"]
        sv_fmt = ", ".join(fmt_num(float(x)) for x in sv)
        lines.append(f"  - [{sv_fmt}] is a dead end (seen in {p['hit_count']} puzzles)")
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  STEP 4  —  Final expression builder
# ─────────────────────────────────────────────

def build_final_expression(path: list, steps: list) -> str:
    """
    Reconstruct a clean nested arithmetic expression from the solution steps.

    Uses a pool (ordered list of (value, expression) pairs) instead of a
    value-keyed dict.  The dict approach breaks whenever two numbers have the
    same value — either duplicate originals (e.g. 1 1 2 6) or two operations
    that produce the same intermediate result (e.g. 13-1=12 and 2+10=12).
    In both cases the dict silently overwrites the first slot with the second,
    causing both operands in the next step to expand to the SAME sub-expression
    and therefore reuse numbers that were already consumed.

    Pool approach: each (value, expression) slot is consumed exactly once via
    consume(), which removes and returns the FIRST matching slot — preserving
    order and avoiding any double-expansion of duplicate values.

    Example — puzzle 1 1 2 6, steps: 1+1=2, 2+2=4, 4*6=24
      Old (buggy):  ((1+1) + (1+1)) * 6  — uses four 1s, zero 2s
      New (correct): (2 + (1+1)) * 6     — uses 2, 1, 1, 6  ✓

    Example — puzzle 1 2 10 13, steps: 13-1=12, 2+10=12, 12+12=24
      Old (buggy):  (2+10) + (2+10)       — uses 2 and 10 twice, ignores 1 and 13
      New (correct): (13-1) + (2+10)      — uses 1, 2, 10, 13  ✓
    """
    step_pat = re.compile(r'(-?\d+\.?\d*)\s*([+\-*/])\s*(-?\d+\.?\d*)\s*=\s*(-?\d+\.?\d*)')

    parsed = []
    for s in steps:
        m = step_pat.search(s)
        if m:
            parsed.append((
                float(m.group(1)), m.group(2),
                float(m.group(3)), float(m.group(4)),
            ))

    if not parsed:
        leaf = path[-1]
        return leaf.get("action", "?") + " = 24"

    def fmt(x: float) -> str:
        return str(int(x)) if x == int(x) else f"{x:.4g}"

    def consume(pool: list, val: float, tol: float = 1e-6) -> str:
        """
        Remove and return the expression string for the first pool slot whose
        value matches val.  Falls back to fmt(val) if no slot matches (should
        not happen on valid input, but prevents a hard crash on edge cases).
        """
        for i, (v, expr) in enumerate(pool):
            if abs(v - val) < tol:
                pool.pop(i)
                return expr
        return fmt(val)   # fallback — keeps generation alive on unexpected input

    # Initialise pool from the original numbers (root node state).
    # Order matches the root state list so consume() is deterministic.
    root_state = [float(x) for x in path[0]["state"]]
    pool: list = [(v, fmt(v)) for v in root_state]

    final_expr = fmt(24.0)   # safety fallback
    for a_val, op, b_val, res_val in parsed:
        a_expr = consume(pool, a_val)
        b_expr = consume(pool, b_val)

        # Parenthesise compound sub-expressions (contain a space → have an operator)
        a_expr_p = f"({a_expr})" if " " in a_expr else a_expr
        b_expr_p = f"({b_expr})" if " " in b_expr else b_expr

        final_expr = f"{a_expr_p} {op} {b_expr_p}"
        pool.append((res_val, final_expr))   # push result back; consumed by later steps

    return f"{final_expr} = 24"


# ─────────────────────────────────────────────
#  STEP 5  —  Main extractor
# ─────────────────────────────────────────────

def extract_training_example(
    tree: dict,
    dead_end_db: dict,
    prefer_shortest: bool = True,
) -> Optional[dict]:
    """
    Extract one (prompt, completion) training pair from a tree JSON.

    Args:
        tree:            Parsed tree JSON (one puzzle).
        dead_end_db:     Parsed dead_end_db.json (shared across all puzzles).
        prefer_shortest: If True, pick the solution with fewest steps.

    Returns:
        {
            "puzzle":          "2 3 4 9",
            "prompt":          "<full input text>",
            "completion":      "<clean step-by-step answer>",
            "has_deadend_ctx": bool,
            "solution_steps":  int,
            "solution_node_id": int,
        }
        or None if no valid solution can be extracted.
    """
    nodes_list = tree["nodes"]
    nodes = {n["id"]: n for n in nodes_list}
    solution_ids = tree["solutions"]
    metadata = tree["metadata"]

    if not solution_ids:
        return None

    # ── Collect explored intermediate states for dead-end matching ──
    explored_states = set()
    for n in nodes_list:
        if n["depth"] > 0 and n["state"] and len(n["state"]) > 1:
            key = str(sorted(int(float(x)) for x in n["state"]))
            explored_states.add(key)

    # ── Pick best solution path ──
    candidate_paths = []
    for sol_id in solution_ids:
        path = trace_solution_path(nodes, sol_id)
        steps = []
        valid = True
        for k, n in enumerate(path[1:], 1):   # k indexes into path; path[k-1] is parent
            line = format_step(n)
            if line is None:
                valid = False
                break
            # Type A guard: operands must exist in the parent node's state.
            # Catches corrupt leaf nodes whose code skips an intermediate step.
            m_op = re.match(r'(-?\d+\.?\d*)\s*[+\-*/]\s*(-?\d+\.?\d*)', line)
            if m_op:
                a_val, b_val = float(m_op.group(1)), float(m_op.group(2))
                if not _operands_in_state(a_val, b_val, path[k - 1]["state"]):
                    valid = False
                    break
            steps.append(line)
        if valid and steps:
            candidate_paths.append((sol_id, path, steps))

    if not candidate_paths:
        return None

    # Prefer shortest (most elegant) or keep all for augmentation
    candidate_paths.sort(key=lambda x: len(x[2]))
    sol_id, path, steps = candidate_paths[0] if prefer_shortest else candidate_paths[-1]

    # ── Puzzle info ──
    root = nodes[0]
    puzzle_numbers = root["state"]
    puzzle_str = " ".join(str(int(float(x))) for x in puzzle_numbers)

    # ── Dead-end selectivity ──
    relevant_patterns = get_relevant_deadend_patterns(
        puzzle_numbers, dead_end_db, explored_states
    )
    include_deadend = should_include_deadend_context(tree, relevant_patterns)

    # ── Final expression ──
    final_expr = build_final_expression(path, steps)

    # ── Build prompt ──
    prompt_lines = [
        f"Numbers: {puzzle_str}. Target: 24.",
        "Use each number exactly once with +, -, *, / to reach 24.",
    ]
    if include_deadend:
        deadend_block = format_deadend_context(relevant_patterns)
        prompt_lines.append(deadend_block)
    prompt_lines.append("Steps:")

    # ── Build completion ──
    completion_lines = steps + [f"Answer: {final_expr}"]

    return {
        "puzzle": puzzle_str,
        "prompt": "\n".join(prompt_lines),
        "completion": "\n".join(completion_lines),
        "has_deadend_ctx": include_deadend,
        "solution_steps": len(steps),
        "solution_node_id": sol_id,
        "deadend_patterns_used": len(relevant_patterns) if include_deadend else 0,
        "deadend_memory_skipped": metadata["statistics"].get("deadend_memory_skipped", 0),
    }


# ─────────────────────────────────────────────
#  STEP 6  —  Batch processor
# ─────────────────────────────────────────────

def process_tree_directory(
    tree_dir: str,
    dead_end_db_path: str,
    output_path: str = "training_data.jsonl",
) -> dict:
    """
    Process all tree JSON files in a directory.

    Returns summary statistics about the generated dataset.
    """
    with open(dead_end_db_path, encoding='utf-8') as f:
        dead_end_db = json.load(f)

    tree_files = list(Path(tree_dir).glob("game24_tree_*.json"))
    print(f"Found {len(tree_files)} tree files.")

    results = []
    skipped = 0
    deadend_ctx_count = 0

    for tree_path in sorted(tree_files):
        with open(tree_path, encoding='utf-8') as f:
            tree = json.load(f)

        example = extract_training_example(tree, dead_end_db)
        if example is None:
            skipped += 1
            continue

        results.append(example)
        if example["has_deadend_ctx"]:
            deadend_ctx_count += 1

    # Write JSONL
    with open(output_path, "w") as f:
        for ex in results:
            f.write(json.dumps(ex) + "\n")

    stats = {
        "total_trees": len(tree_files),
        "valid_examples": len(results),
        "skipped": skipped,
        "with_deadend_context": deadend_ctx_count,
        "deadend_ctx_ratio": round(deadend_ctx_count / max(len(results), 1), 3),
        "avg_steps": round(sum(r["solution_steps"] for r in results) / max(len(results), 1), 2),
        "output": output_path,
    }
    print(json.dumps(stats, indent=2))
    return stats


# # ─────────────────────────────────────────────
# #  Quick test on the uploaded tree
# # ─────────────────────────────────────────────

# if __name__ == "__main__":
#     TREE_PATH = "/mnt/user-data/uploads/game24_tree_2_3_4_9_20260525_184010.json"
#     DB_PATH = "/mnt/user-data/uploads/dead_end_db.json"

#     with open(TREE_PATH, encoding='utf-8') as f:
#         tree = json.load(f)
#     with open(DB_PATH, encoding='utf-8') as f:
#         db = json.load(f)

#     example = extract_training_example(tree, db)
#     if example:
#         print("=" * 60)
#         print(f"Puzzle: {example['puzzle']}")
#         print(f"Has dead-end context: {example['has_deadend_ctx']}")
#         print(f"Dead-end patterns used: {example['deadend_patterns_used']}")
#         print(f"Dead-end memory skipped during search: {example['deadend_memory_skipped']}")
#         print(f"Solution steps: {example['solution_steps']}")
#         print()
#         print("── PROMPT ──")
#         print(example["prompt"])
#         print()
#         print("── COMPLETION ──")
#         print(example["completion"])
#     else:
#         print("No valid training example could be extracted.")