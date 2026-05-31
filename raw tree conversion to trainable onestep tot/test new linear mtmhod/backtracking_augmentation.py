"""
backtracking_augmentation.py
==============================
Generates augmented backtracking training examples using verified dead‑end
branches from the tree JSON.

Features:
  - Flags trees that have solutions but failed to produce a clean training example.
  - Writes a separate file with problematic tree filenames.
  - Depth‑2 near‑miss support: generates 2‑step dead‑end traces where a promising
    path reaches 24 but still has numbers left over — a harder and more instructive
    signal than depth‑1 weak moves.
  - NEAR‑MISS EXCLUSION: only states that contain 24 and the only remaining
    number(s) is/are 1 are excluded (e.g., [24, 1] or [24, 1, 1]).
    Genuine dead‑ends like [24, 2] are kept.
"""

import json
import math
import re
import random
from pathlib import Path
from typing import Optional, List, Dict, Tuple

# Re‑use solver from verify_training_data.py


# Re‑use core utilities from extract_training_example.py
from extract_training_example import (
    format_step,
    fmt_num,
    trace_solution_path,
    build_final_expression,
    get_relevant_deadend_patterns,
    should_include_deadend_context,
    format_deadend_context,
    extract_training_example,
    _operands_in_state,
    HIT_COUNT_THRESHOLD,
)

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────

BACKTRACK_RATIO       = 0.40   # Increased from 0.20: implicit format has same complexity as clean
PRUNED_VALUE_THRESHOLD = 0.1   # Node is a dead end if value ≤ this
NEAR_MISS_PARENT_MIN  = 0.5    # Depth‑1 parent must have value ≥ this to be "promising"
EXCLUDE_NEAR_MISS_DEAD_ENDS = True   # Exclude states like [24, 1] but keep [24, 2]

# ─────────────────────────────────────────────
#  In-context examples prepended to every prompt
#  (3 clean + 1 backtracking, using their verified puzzles in our format)
#  This anchors SmolLM-360M to the output format at inference time —
#  a 360M model cannot reliably recover the format from weights alone.
# ─────────────────────────────────────────────

IN_CONTEXT_HEADER = (
    "Here are some solved examples:\n\n"
    "Numbers: 4 4 6 8. Target: 24.\n"
    "Use each number exactly once with +, -, *, / to reach 24.\n"
    "Steps:\n"
    "4 + 8 = 12 (left: 4 6 12)\n"
    "6 - 4 = 2 (left: 2 12)\n"
    "2 * 12 = 24 (left: 24)\n"
    "Answer: (6 - 4) * (4 + 8) = 24\n\n"
    "Numbers: 1 4 8 8. Target: 24.\n"
    "Use each number exactly once with +, -, *, / to reach 24.\n"
    "Steps:\n"
    "8 / 4 = 2 (left: 1 2 8)\n"
    "1 + 2 = 3 (left: 3 8)\n"
    "3 * 8 = 24 (left: 24)\n"
    "Answer: (1 + 8 / 4) * 8 = 24\n\n"
    "Numbers: 5 5 5 9. Target: 24.\n"
    "Use each number exactly once with +, -, *, / to reach 24.\n"
    "Steps:\n"
    "5 + 5 = 10 (left: 5 9 10)\n"
    "10 + 5 = 15 (left: 9 15)\n"
    "15 + 9 = 24 (left: 24)\n"
    "Answer: ((5 + 5) + 5) + 9 = 24\n\n"
    # Backtracking example: dead-end path terminates at 1 number (≠24),
    # then "Backtrack." signals an explicit restart from the original numbers.
    # SmolLM-360M needs to see this marker at least once in-context before
    # generating it reliably — putting it here anchors the format without
    # consuming training signal.
    "Numbers: 4 9 10 13. Target: 24.\n"
    "Use each number exactly once with +, -, *, / to reach 24.\n"
    "Steps:\n"
    "4 + 9 = 13 (left: 10 13 13)\n"
    "13 - 10 = 3 (left: 3 13)\n"
    "13 + 3 = 16 (left: 16)\n"
    "Backtrack. (to: 4 9 10 13).\n"
    "13 - 10 = 3 (left: 3 4 9)\n"
    "9 - 3 = 6 (left: 4 6)\n"
    "4 * 6 = 24 (left: 24)\n"
    "Answer: 4 * (9 - (13 - 10)) = 24\n\n"
    "Now solve this puzzle:\n"
)


# ─────────────────────────────────────────────
#  Helper: near‑miss detection refined
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
#  Depth-1 dead-end extension
# ─────────────────────────────────────────────
 
# ===== NEW FUNCTION =====
def extend_dead_end_to_terminal(state: List[float]) -> Optional[List[str]]:
    """
    Given a dead‑end state (length 2 or 3), repeatedly apply arithmetic operations
    to reduce it to a single number, ensuring every intermediate state is also a
    dead‑end (cannot reach 24). Returns a list of formatted step lines, or None
    if no valid extension exists.
    """
    steps = []
    current = [float(x) for x in state]

    # We need len(current)-1 operations to reach one number
    for _ in range(len(current) - 1):
        best_distance = -1.0
        best_step = None
        best_new_state = None

        n = len(current)
        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                a, b = current[i], current[j]
                for op in ('+', '-', '*', '/'):
                    try:
                        if op == '+':
                            res = a + b
                        elif op == '-':
                            res = a - b
                        elif op == '*':
                            res = a * b
                        elif op == '/':
                            if abs(b) < 1e-12:
                                continue
                            res = a / b
                        else:
                            continue
                    except Exception:
                        continue

                    # Build the new state after this operation
                    rest = [current[k] for k in range(n) if k != i and k != j]
                    new_state = [res] + rest

                    # Must be a dead‑end (cannot reach 24)
                    if can_reach_24(new_state):
                        continue

                    # Exclude near‑miss states if desired (optional)
                    if EXCLUDE_NEAR_MISS_DEAD_ENDS and _state_is_near_miss(new_state):
                        continue

                    # Prefer result farthest from 24 (clearest negative signal)
                    distance = abs(res - 24)
                    if distance > best_distance:
                        best_distance = distance
                        # Format the step line using the same style as format_step
                        left_str = ' '.join(fmt_num(x) for x in new_state)
                        step_line = f"{fmt_num(a)} {op} {fmt_num(b)} = {fmt_num(res)} (left: {left_str})"
                        best_step = step_line
                        best_new_state = new_state

        if best_step is None:
            return None  # Cannot extend this dead‑end to a terminal state
        steps.append(best_step)
        current = best_new_state

    # After the loop, current has length 1. Ensure it is not 24.
    if math.isclose(current[0], 24.0, abs_tol=1e-6):
        return None   # terminal state accidentally became a solution – skip
    return steps



def extend_dead_end_by_one_step(state: list) -> Optional[str]:
    """
    Given a 3-number dead-end state, find one arithmetic operation that
    produces a 2-number state which is ALSO a dead end (can't reach 24).
    Returns a formatted step line like '5 + 1 = 6 (left: 6 1)', or None.
 
    Why this exists:
        Depth-1 dead ends stop at 3 numbers remaining.  In the implicit
        backtracking format the restart signal is the `left:` count rising —
        but 3→3 (different numbers) is ambiguous.  Extending by one step so
        the dead end terminates at 2 numbers makes the restart unambiguous:
        the model always sees `(left: A B)` followed by `(left: D E F)`,
        a clear count increase from 2→3.  This matches the terminal-state
        pattern used in the reference dataset.
 
    Strategy: prefer the operation whose result is furthest from 24
    (most obviously wrong), to maximise the negative learning signal.
    """
    nums = [float(x) for x in state]
    candidates = []
 
    for i in range(len(nums)):
        for j in range(len(nums)):
            if i == j:
                continue
            a, b = nums[i], nums[j]
            for op in ['+', '-', '*', '/']:
                try:
                    if op == '+':   res = a + b
                    elif op == '-': res = a - b
                    elif op == '*': res = a * b
                    elif op == '/':
                        if abs(b) < 1e-12:
                            continue
                        res = a / b
                    else:
                        continue
                except Exception:
                    continue
 
                remaining = [nums[k] for k in range(len(nums)) if k != i and k != j]
                new_state = [res] + remaining
 
                # Must be a genuine dead end at the 2-number level
                if can_reach_24(new_state):
                    continue
 
                # Exclude near-miss states (e.g. [24, 1])
                if _state_is_near_miss(new_state):
                    continue
 
                left_str  = ' '.join(fmt_num(x) for x in new_state)
                step_line = (
                    f"{fmt_num(a)} {op} {fmt_num(b)} = {fmt_num(res)} "
                    f"(left: {left_str})"
                )
                distance  = abs(res - 24)   # further from 24 = clearer dead end
                candidates.append((distance, step_line))
 
    if not candidates:
        return None
 
    candidates.sort(key=lambda x: -x[0])   # highest distance first
    return candidates[0][1]
def can_reach_24(nums: List[float], tol: float = 1e-6) -> bool:
    """
    Return True if the list of numbers (length <= 4) can be combined with
    +, -, *, / to make exactly 24 (within tolerance).
    """
    if not nums:
        return False
    if len(nums) == 1:
        return math.isclose(nums[0], 24.0, rel_tol=tol, abs_tol=tol)
    # Try every pair (i, j) and operation, reduce list, recurse
    n = len(nums)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            a, b = nums[i], nums[j]
            # Remaining numbers (all except i and j)
            rest = [nums[k] for k in range(n) if k != i and k != j]
            # Try all operations
            for op in ['+', '-', '*', '/']:
                try:
                    if op == '+':
                        res = a + b
                    elif op == '-':
                        res = a - b
                    elif op == '*':
                        res = a * b
                    elif op == '/':
                        if abs(b) < 1e-12:
                            continue
                        res = a / b
                    else:
                        continue
                    if can_reach_24(rest + [res], tol):
                        return True
                except:
                    continue
    return False
def _state_is_near_miss(state: list) -> bool:
    """
    True when state contains 24 and:
      - all other numbers are 1, OR
      - there exists a number (other than 24) that appears at least twice.
    Examples: [24, 1], [24, 13, 13], [24, 5, 5, 5] -> True.
              [24, 2], [24, 3, 4] -> False.
    """
    if len(state) < 2:
        return False
    # Check for presence of 24
    has_24 = any(math.isclose(float(x), 24.0, abs_tol=1e-6) for x in state)
    if not has_24:
        return False

    # Count occurrences of numbers other than 24
    counts = {}
    for x in state:
        fx = float(x)
        if math.isclose(fx, 24.0, abs_tol=1e-6):
            continue
        # Round to avoid floating‑point noise
        key = round(fx, 6)
        counts[key] = counts.get(key, 0) + 1

    # If any number appears at least twice, it's a near‑miss
    if any(cnt >= 2 for cnt in counts.values()):
        return True

    # Also catch the all‑ones case (covered by above if 1 appears ≥2)
    return False


# ─────────────────────────────────────────────
#  Find candidate depth‑1 dead‑end nodes
# ─────────────────────────────────────────────

def find_depth1_dead_ends(nodes_list: list, exclude_near_miss: bool = True) -> list:
    """Return all depth‑1 nodes that are verified dead ends, optionally skipping near‑miss states."""
    dead_ends = []
    for node in nodes_list:
        if node["depth"] != 1:
            continue
        if node.get("is_solution", False):
            continue
        if exclude_near_miss and _state_is_near_miss(node.get("state", [])):
            continue
        is_pruned   = node.get("is_pruned", False)
        is_low_value = node.get("value", 1.0) <= PRUNED_VALUE_THRESHOLD
        has_code    = bool(node.get("codeact", {}).get("code", "").strip())
        if not ((is_pruned or is_low_value) and has_code):
            continue
        # ── True dead-end guard: reject if 24 is still reachable from this state ──
        state = [float(x) for x in node.get("state", [])]
        if state and can_reach_24(state):
            continue
        dead_ends.append(node)
    return dead_ends


# ─────────────────────────────────────────────
#  Find candidate depth‑2 near‑miss dead‑end pairs
# ─────────────────────────────────────────────

def find_depth2_dead_ends(nodes_list: list, solution_path_ids: set, exclude_near_miss: bool = True) -> list:
    """
    Return (parent_node, child_node) pairs that form a promising‑but‑wrong
    depth‑2 dead end.  If exclude_near_miss is True, skip child nodes whose
    state is a near‑miss (contains 24 with only 1's left).
    """
    node_map = {n["id"]: n for n in nodes_list}
    results = []

    for child in nodes_list:
        if child["depth"] != 2:
            continue
        if child.get("is_solution", False):
            continue
        if exclude_near_miss and _state_is_near_miss(child.get("state", [])):
            continue

        parent = node_map.get(child["parent_id"])
        if parent is None:
            continue

        # Parent guards
        if parent["depth"] != 1:
            continue
        if parent["id"] in solution_path_ids:
            continue
        if parent.get("value", 0.0) < NEAR_MISS_PARENT_MIN:
            continue
        if not parent.get("codeact", {}).get("code", "").strip():
            continue

        # Child dead‑end check
        has_code     = bool(child.get("codeact", {}).get("code", "").strip())
        is_pruned    = child.get("is_pruned", False)
        is_low_value = child.get("value", 1.0) <= PRUNED_VALUE_THRESHOLD

        if not has_code:
            continue
        if not (is_pruned or is_low_value):
            continue
        # ── True dead-end guard: reject if 24 is still reachable from child's state ──
        child_state = [float(x) for x in child.get("state", [])]
        if child_state and can_reach_24(child_state):
            continue

        near_miss = _state_is_near_miss(child.get("state", []))
        results.append((parent, child, near_miss))

    # Sort: near‑miss (if any) first, but only if we didn't exclude them
    results.sort(key=lambda t: (0 if t[2] else 1, t[1].get("value", 0.0)))
    return results


# ─────────────────────────────────────────────
#  Build one backtracking example
#  (supports depth‑1 and depth‑2 dead‑end paths)
# ─────────────────────────────────────────────

def extract_backtracking_example(
    tree: dict,
    dead_end_db: dict,
    dead_end_path: list,        # [node] for depth‑1; [parent_node, child_node] for depth‑2
    solution_steps: list,
    solution_path: list,
    puzzle_str: str,
    backtrack_depth: int = 1,
    extra_steps: Optional[List[str]] = None,  # extra step appended to depth-1 dead ends
) -> Optional[dict]:
    """
    Build a backtracking training example using implicit failed-path format.

    dead_end_path is an ordered list of nodes forming the dead‑end branch
    (root‑excluded).  All dead-end steps are rendered as plain arithmetic
    lines — NO [dead end] tag, NO [restart:] marker.

    For depth-1 dead ends, extension_step (if provided) is appended after the
    node's step, making the path terminate at 2 numbers remaining.  This ensures
    the restart signal is always an unambiguous count rise:
        (left: A B)       ← dead end terminates at 2 numbers
        (left: D E F)     ← solution starts, count rises 2→3, clear restart

    Completion structure:
        <dead-end step>                    ← depth-1 node step
        [<extension_step>]                 ← programmatic 2-number terminal (depth-1 only)
        <dead-end step 1>                  ← depth-2: parent step
        <dead-end step 2>                  ← depth-2: child step (2 numbers remaining)
        <solution step 1>
        ...
        Answer: <expr>
    """
    # Format every node in the dead‑end path
    dead_end_lines = []
    for node in dead_end_path:
        line = format_step(node)
        if line is None:
            return None
        dead_end_lines.append(line)

    # Type‑A operand check for depth‑2: child's operands must exist in parent's state
    if backtrack_depth == 2 and len(dead_end_path) == 2:
        parent_node, child_node = dead_end_path
        m_op = re.match(r'(-?\d+\.?\d*)\s*[+\-*/]\s*(-?\d+\.?\d*)', dead_end_lines[1])
        if m_op:
            a_val, b_val = float(m_op.group(1)), float(m_op.group(2))
            if not _operands_in_state(a_val, b_val, parent_node["state"]):
                return None

    # ── Final true dead-end guard ────────────────────────────────────────────
    # The state after the last dead-end step must genuinely have no path to 24.
    # This catches any edge cases that slipped through the candidate finders.
    dead_end_node = dead_end_path[-1]
    dead_end_state = [float(x) for x in dead_end_node.get("state", [])]
    if dead_end_state and can_reach_24(dead_end_state):
        return None   # Not a true dead end — skip rather than mislabel

    final_expr = build_final_expression(solution_path, solution_steps)

    nodes_list = tree["nodes"]
    puzzle_numbers = tree["nodes"][0]["state"]
    explored_states = set()
    for n in nodes_list:
        if n["depth"] > 0 and n["state"] and len(n["state"]) > 1:
            key = str(sorted(int(float(x)) for x in n["state"]))
            explored_states.add(key)

    relevant_patterns = get_relevant_deadend_patterns(
        puzzle_numbers, dead_end_db, explored_states
    )
    include_deadend_ctx = should_include_deadend_context(tree, relevant_patterns)

    prompt_lines = [
        IN_CONTEXT_HEADER + f"Numbers: {puzzle_str}. Target: 24.",
        "Use each number exactly once with +, -, *, / to reach 24.",
        "Steps:",
    ]

    # Completion: dead-end steps (+ optional terminal extension), then
    # "Backtrack." as an explicit restart signal, then the solution steps.
    #
    # Why "Backtrack." and not a special token or longer phrase:
    #   - It's already in SmolLM's pretraining vocabulary (code + math corpora
    #     use "backtrack" extensively), so no new token is needed.
    #   - One short line is the minimum overhead a 360M model can reliably
    #     learn to both generate and condition on.
    #   - Placing it ONLY at the transition (dead-end → solution) is consistent:
    #     the model learns a single rule — "Backtrack. appears when (left: X)
    #     has one number that is not 24."
    #   - The in-context header example already shows this pattern, which anchors
    #     the format before the model has to produce it from weights alone.
    completion_lines = list(dead_end_lines)
    if extra_steps:
        completion_lines.extend(extra_steps)
    completion_lines.append(f"Backtrack. (to: {puzzle_str})")    # ← explicit restart marker
    completion_lines.extend(solution_steps)
    completion_lines.append(f"Answer: {final_expr}")

    return {
        "puzzle":           puzzle_str,
        "prompt":           "\n".join(prompt_lines),
        "completion":       "\n".join(completion_lines),
        "example_type":     "backtracking",
        "backtrack_depth":  backtrack_depth,
        "has_deadend_ctx":  False,
        "extended": bool(extra_steps),
        "dead_end_step":    dead_end_lines[-1],
        "dead_end_node_id": dead_end_node["id"],
        "dead_end_value":   dead_end_node.get("value", 0.0),
        "is_near_miss":     _state_is_near_miss(dead_end_node.get("state", [])),
        "solution_steps":   len(solution_steps),
    }


# ─────────────────────────────────────────────
#  Get ALL backtracking candidates for one tree
#  (depth‑1 weak moves + depth‑2 near‑misses, with refined near‑miss filtering)
# ─────────────────────────────────────────────

def get_backtracking_examples_for_tree(tree: dict, dead_end_db: dict, exclude_near_miss: bool = True) -> list:
    """
    Return up to two backtracking examples per tree — at most one depth‑1 and
    at most one depth‑2 — both filtered by exclude_near_miss.
    """
    nodes_list = tree["nodes"]
    nodes      = {n["id"]: n for n in nodes_list}
    solution_ids = tree.get("solutions", [])

    if not solution_ids:
        return []

    # ── Find shortest valid solution path ──────────────────────────────────
    best_sol   = None
    best_steps = None
    best_path  = None
    for sol_id in solution_ids:
        path  = trace_solution_path(nodes, sol_id)
        steps = []
        valid = True
        for k, n in enumerate(path[1:], 1):
            line = format_step(n)
            if line is None:
                valid = False
                break
            m_op = re.match(r'(-?\d+\.?\d*)\s*[+\-*/]\s*(-?\d+\.?\d*)', line)
            if m_op:
                a_val, b_val = float(m_op.group(1)), float(m_op.group(2))
                if not _operands_in_state(a_val, b_val, path[k - 1]["state"]):
                    valid = False
                    break
            steps.append(line)
        if valid and steps:
            if best_sol is None or len(steps) < len(best_steps):
                best_sol, best_steps, best_path = sol_id, steps, path

    if best_sol is None:
        return []

    solution_path_ids = {n["id"] for n in best_path}
    puzzle_numbers    = nodes[0]["state"]
    puzzle_str        = " ".join(str(int(float(x))) for x in puzzle_numbers)
    examples          = []

    # ── Depth‑1 candidate (weakest non‑solution first move) ────────────────
    # extend_dead_end_by_one_step brings the path from 3 numbers remaining
    # to 2, so the restart signal (count rises 2→3 at solution start) is
    # always unambiguous — matching the reference dataset's terminal pattern.
    # If no valid extension exists the example is still generated; the
    # ambiguous 3→3 case is rare and better than dropping it entirely.
    # Depth‑1 candidate
    depth1_dead_ends = find_depth1_dead_ends(nodes_list, exclude_near_miss)
    for de in depth1_dead_ends:
        line = format_step(de)
        if line and line != best_steps[0]:
            # Extend from 3 numbers → 1 number
            extra = extend_dead_end_to_terminal(de.get("state", []))
            if extra is None:
                continue   # cannot reach terminal dead‑end – skip
            ex = extract_backtracking_example(
                tree, dead_end_db,
                dead_end_path=[de],
                solution_steps=best_steps,
                solution_path=best_path,
                puzzle_str=puzzle_str,
                backtrack_depth=1,
                extra_steps=extra,
            )
            if ex:
                examples.append(ex)
            break

    # Depth‑2 candidate
    depth2_candidates = find_depth2_dead_ends(nodes_list, solution_path_ids, exclude_near_miss)
    for parent_node, child_node, is_near_miss in depth2_candidates:
        parent_line = format_step(parent_node)
        if parent_line and parent_line == best_steps[0]:
            continue
        # Extend from child's state (2 numbers) → 1 number
        child_state = child_node.get("state", [])
        extra = extend_dead_end_to_terminal(child_state) if len(child_state) > 1 else []
        if extra is None:
            continue
        ex = extract_backtracking_example(
            tree, dead_end_db,
            dead_end_path=[parent_node, child_node],
            solution_steps=best_steps,
            solution_path=best_path,
            puzzle_str=puzzle_str,
            backtrack_depth=2,
            extra_steps=extra,
        )
        if ex:
            examples.append(ex)
        break

    return examples


# ─────────────────────────────────────────────
#  Batch processor with flagging
# ─────────────────────────────────────────────

def generate_augmented_dataset(
    tree_dir: str,
    dead_end_db_path: str,
    output_path: str = "training_data_augmented.jsonl",
    target_backtrack_ratio: float = BACKTRACK_RATIO,
    flag_file: Optional[str] = "flagged_problematic_trees.txt",
    seed: int = 42,
    exclude_near_miss: bool = True,
) -> dict:
    """
    Process all tree JSONs and generate mixed dataset.

    Args:
        tree_dir:               Directory containing game24_tree_*.json files.
        dead_end_db_path:       Path to dead_end_db.json.
        output_path:            Output JSONL file path.
        target_backtrack_ratio: Fraction of dataset to be backtracking.
        flag_file:              If provided, write problematic tree filenames to this file.
        seed:                   Random seed.
        exclude_near_miss:      If True, skip dead‑end nodes that are [24, 1]‑like.
    """
    random.seed(seed)

    with open(dead_end_db_path, encoding='utf-8') as f:
        dead_end_db = json.load(f)

    tree_files = sorted(Path(tree_dir).glob("game24_tree_*.json"))
    print(f"Found {len(tree_files)} tree files.")
    print(f"Excluding near‑miss dead‑ends (24 with only 1's): {exclude_near_miss}")

    clean_examples = []
    backtrack_candidates = []
    clean_failed_despite_solution = []   # (filename, reason)
    backtrack_failed_despite_solution = []
    skipped_no_solution = 0

    for tree_path in tree_files:
        with open(tree_path, encoding='utf-8') as f:
            tree = json.load(f)

        has_solution = len(tree.get("solutions", [])) > 0

        # Clean example
        clean = extract_training_example(tree, dead_end_db)
        if clean is None:
            if has_solution:
                clean_failed_despite_solution.append((tree_path.name, "extract_training_example returned None"))
            else:
                skipped_no_solution += 1
            continue

        clean["example_type"] = "clean"
        # Patch the prompt to include in-context examples.
        # extract_training_example builds a minimal prompt; we prepend
        # IN_CONTEXT_HEADER here so clean and backtracking examples are
        # uniform — the model sees the same 4 anchoring examples every time.
        if not clean["prompt"].startswith("Here are some solved examples:"):
            clean["prompt"] = IN_CONTEXT_HEADER + clean["prompt"]
        clean_examples.append(clean)

        # Backtracking candidates
        bt_examples = get_backtracking_examples_for_tree(tree, dead_end_db, exclude_near_miss)
        if not bt_examples and has_solution:
            backtrack_failed_despite_solution.append(tree_path.name)
        backtrack_candidates.extend(bt_examples)

    n_clean = len(clean_examples)
    if n_clean == 0:
        print("No valid clean examples found.")
        return {}

    # Ratio control: how many backtracking examples we need
    max_bt = int(round(n_clean * target_backtrack_ratio / (1 - target_backtrack_ratio)))
    if len(backtrack_candidates) > max_bt:
        backtrack_examples = random.sample(backtrack_candidates, max_bt)
        print(f"Sampled {max_bt} backtracking examples from {len(backtrack_candidates)} candidates to hit {target_backtrack_ratio:.0%} target ratio.")
    else:
        backtrack_examples = backtrack_candidates
        actual_ratio = len(backtrack_examples) / (n_clean + len(backtrack_examples)) if backtrack_examples else 0
        print(f"Only {len(backtrack_examples)} backtracking candidates available — actual ratio will be {actual_ratio:.1%}.")

    # Flag file
    if flag_file:
        with open(flag_file, 'w', encoding='utf-8') as f:
            f.write("Trees with solutions but clean example extraction failed:\n")
            for fname, reason in clean_failed_despite_solution:
                f.write(f"  {fname}  (reason: {reason})\n")
            if not clean_failed_despite_solution:
                f.write("  None\n")
            f.write("\nTrees with solutions but no backtracking candidate (may be OK if no dead‑end existed):\n")
            for fname in backtrack_failed_despite_solution:
                f.write(f"  {fname}\n")
            if not backtrack_failed_despite_solution:
                f.write("  None\n")
        print(f"Flagged problematic trees written to {flag_file}")

    # Interleave and shuffle
    all_examples = clean_examples + backtrack_examples
    random.shuffle(all_examples)

    with open(output_path, "w") as f:
        for ex in all_examples:
            f.write(json.dumps(ex) + "\n")

    n_bt = len(backtrack_examples)
    n_total = len(all_examples)
    bt_depth1   = sum(1 for e in backtrack_examples if e.get("backtrack_depth", 1) == 1)
    bt_depth2   = sum(1 for e in backtrack_examples if e.get("backtrack_depth", 1) == 2)
    bt_nearmiss = sum(1 for e in backtrack_examples if e.get("is_near_miss", False))
    stats = {
        "total_trees": len(tree_files),
        "skipped_no_solution": skipped_no_solution,
        "clean_examples": n_clean,
        "backtracking_examples": n_bt,
        "  backtrack_depth1": bt_depth1,
        "  backtrack_depth2": bt_depth2,
        "  backtrack_near_miss": bt_nearmiss,
        "total_examples": n_total,
        "backtrack_ratio": round(n_bt / n_total, 3) if n_total else 0,
        "clean_with_deadend_ctx": sum(1 for e in clean_examples if e.get("has_deadend_ctx")),
        "avg_solution_steps": round(sum(e["solution_steps"] for e in clean_examples) / n_clean, 2),
        "clean_failed_despite_solution": len(clean_failed_despite_solution),
        "backtrack_failed_despite_solution": len(backtrack_failed_despite_solution),
        "output": output_path,
        "flag_file": flag_file if flag_file else None,
        "exclude_near_miss": exclude_near_miss,
    }

    print("\n── Dataset stats ──")
    print(json.dumps(stats, indent=2))
    return stats


# ─────────────────────────────────────────────
#  Quick test on a single tree (for debugging)
# ─────────────────────────────────────────────

if __name__ == "__main__":
    TREE_PATH = "game24_tree_1_3_6_11_20260525_175624.json"
    DB_PATH   = "dead_end_db.json"

    with open(TREE_PATH, encoding='utf-8') as f:
        tree = json.load(f)
    with open(DB_PATH, encoding='utf-8') as f:
        db = json.load(f)

    print("=" * 60)
    print("CLEAN EXAMPLE")
    print("=" * 60)
    clean = extract_training_example(tree, db)
    if clean:
        clean["example_type"] = "clean"
        print(f"Puzzle: {clean['puzzle']}")
        print(f"Has dead‑end INPUT context: {clean['has_deadend_ctx']}")
        print()
        print("── PROMPT ──")
        print(clean["prompt"])
        print()
        print("── COMPLETION ──")
        print(clean["completion"])
    else:
        print("No valid clean example extracted.")

    print()
    print("=" * 60)
    print("BACKTRACKING EXAMPLES  (depth‑1 and depth‑2)")
    print("=" * 60)
    bt_examples = get_backtracking_examples_for_tree(tree, db, exclude_near_miss=True)
    if bt_examples:
        for i, bt in enumerate(bt_examples, 1):
            depth     = bt.get("backtrack_depth", 1)
            near_miss = bt.get("is_near_miss", False)
            label = f"depth-{depth}" + (" [near-miss]" if near_miss else "")
            print(f"\n[Backtracking example {i} — {label}]")
            print(f"Dead‑end node id: {bt['dead_end_node_id']} | value: {bt['dead_end_value']}")
            print(f"Dead‑end step:    {bt['dead_end_step']}")
            print()
            print("── PROMPT ──")
            print(bt["prompt"])
            print()
            print("── COMPLETION ──")
            print(bt["completion"])
    else:
        print("No backtracking examples could be generated for this tree.")