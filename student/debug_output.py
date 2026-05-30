"""
debug_output.py  ── shows raw model output for 5 test puzzles
Usage: python debug_output.py --model ./smollm_finetuned_best --csv 24.csv
"""

import argparse, csv, re
from transformers import AutoModelForCausalLM, AutoTokenizer, StoppingCriteria, StoppingCriteriaList
import torch

parser = argparse.ArgumentParser()
parser.add_argument("--model", default="./smollm_finetuned_best")
parser.add_argument("--csv",   default="24.csv")
parser.add_argument("--n",     type=int, default=5)
parser.add_argument("--max_new_tokens", type=int, default=300)
# Use greedy by default — it shows the model's true learned mode.
# Pass --sample to re-enable stochastic decoding once format adherence works.
parser.add_argument("--sample", action="store_true",
                    help="Use sampling (temperature/top_p) instead of greedy decoding")
args = parser.parse_args()

with open(args.csv) as f:
    rows = list(csv.DictReader(f))
test_puzzles = [r["Puzzles"].strip() for r in rows if 901 <= int(r["Rank"]) <= 1000]

print(f"Loading {args.model} ...")
tokenizer = AutoTokenizer.from_pretrained(args.model, use_fast=False)
# --- ADD THIS BLOCK ---
im_end_id = tokenizer.convert_tokens_to_ids("<|im_end|>")
print(f"<|im_end|> token ID: {im_end_id}")

# If this prints None or the unknown token ID, the stop won't work
unk_id = tokenizer.unk_token_id
if im_end_id is None or im_end_id == unk_id:
    print("WARNING: <|im_end|> not in vocabulary — looping will not stop!")
else:
    print("OK: stop token found")
# --- END BLOCK ---
model     = AutoModelForCausalLM.from_pretrained(args.model, device_map="auto")

# In debug_output.py, after loading the model add:
import os
# Find the latest checkpoint
checkpoints = sorted([
    d for d in os.listdir("./smollm_finetuned_best") 
    if d.startswith("checkpoint-")
], key=lambda x: int(x.split("-")[1]))

if checkpoints:
    latest = f"./smollm_finetuned_best/{checkpoints[-1]}"
    print(f"Loading checkpoint: {latest}")
    model = AutoModelForCausalLM.from_pretrained(latest, device_map="auto")
else:
    model = AutoModelForCausalLM.from_pretrained(args.model, device_map="auto")
model.eval()
device = next(model.parameters()).device
# ── Add this function ABOVE generate_with_backtrack_control ──────────────────

def _verify_and_correct(new_text: str, available: list[float]) -> tuple[bool, list[float], str]:
    """
    Check one generated line against the current available-number pool.
    Returns (ok, new_pool, corrected_text).

    Three things are verified in order:
      1. Both operands exist in `available` (consumed exactly once each).
      2. The arithmetic result is correct.
      3. The (left: ...) annotation matches the new pool.

    If operands are missing → ok=False (caller should trigger backtrack).
    If arithmetic is wrong OR (left:) is wrong → ok=True but text is corrected
    in-place so the model's context always has consistent numbers.

    Lines without an arithmetic pattern (e.g. "Answer: ...") pass through
    unchanged with ok=True and the pool unmodified.
    """
    m = re.match(
        r'\s*(-?\d+(?:\.\d+)?)\s*([+\-\*/])\s*(-?\d+(?:\.\d+)?)\s*=\s*(-?\d+(?:\.\d+)?)'
        r'(?:\s*\(left:\s*([^)]*)\))?',
        new_text.strip()
    )
    if not m:
        return True, available, new_text   # not an arithmetic line – pass through

    a, op, b, claimed_res = (
        float(m.group(1)), m.group(2), float(m.group(3)), float(m.group(4))
    )

    # ── 1. Operand availability check ────────────────────────────────────────
    pool = available[:]
    for val in (a, b):
        for i, v in enumerate(pool):
            if abs(v - val) < 0.05:
                pool.pop(i)
                break
        else:
            return False, available, new_text   # operand not available → backtrack

    # ── 2. Arithmetic correctness ─────────────────────────────────────────────
    ops = {'+': a + b, '-': a - b, '*': a * b}
    if op == '/':
        if abs(b) < 1e-9:
            return False, available, new_text
        ops['/'] = a / b
    true_res = ops[op]
    pool.append(true_res)

    # ── 3. Rebuild the line with correct result and (left:) ───────────────────
    def _fmt(x: float) -> str:
        return str(int(x)) if x == int(x) else f"{x:.4g}"

    left_str = ' '.join(_fmt(v) for v in sorted(pool))
    corrected = f"{_fmt(a)} {op} {_fmt(b)} = {_fmt(true_res)} (left: {left_str})\n"
    return True, pool, corrected
# ─────────────────────────────────────────────────────────────────────────────
#  Controlled generation — Problem 4 fix (looping / no termination)
#
#  Root cause of looping: when the model reaches a dead-end (left: X) with
#  one number remaining that isn't 24, it has no termination signal. It
#  repeats the same failed path until max_new_tokens is exhausted.
#
#  Fix: generate ONE LINE at a time using a newline StoppingCriteria.
#  After each line, inspect the (left: ...) state and trigger "Backtrack.\n"
#  injection under two conditions:
#
#    Trigger 1 — Terminal dead-end:  (left: X)  where X ≠ 24
#      This is the direct cause of the loop. Every training backtrack example
#      (after the augmentation fix) ends at a 1-number terminal before
#      "Backtrack.", so the model learns this exact transition at training time.
#
#    Trigger 2 — State revisit:  same (left: A B ...) seen earlier this run
#      Catches longer loops where the model revisits a 2- or 3-number state
#      it already tried. Cleared on each Backtrack. so the solution path can
#      legitimately share intermediate states with the dead-end path.
#
#  Why inject "Backtrack.\n" rather than a random restart token?
#    The training data (after backtracking_augmentation.py fix) consistently
#    places "Backtrack." at dead-end → solution transitions. The model learns
#    to condition on it: after "Backtrack." it expects to restart from the
#    original numbers. Injecting the exact same string at inference therefore
#    activates a learned behaviour, not an untrained one.
# ─────────────────────────────────────────────────────────────────────────────

class _NewlineStop(StoppingCriteria):
    """Stop generation the moment a newline token is produced."""
    def __init__(self, newline_token_id: int):
        self.newline_id = newline_token_id

    def __call__(self, input_ids, scores, **kwargs):
        return int(input_ids[0, -1]) == self.newline_id


def generate_with_backtrack_control(
    model,
    tokenizer,
    prompt: str,
    *,
    max_steps: int = 24,          # upper bound on total generated lines
    max_new_tokens_per_step: int = 50,   # generous per-line budget
    max_backtracks: int = 6,      # hard cap on injections (prevents inf loops)
    do_sample: bool = False,
    im_end_id: int = None,
) -> str:
    """
    Generate a 24-game solution line by line, auto-injecting 'Backtrack.\\n'
    whenever the model hits a dead-end or revisits a state it already tried.

    Returns the raw generated text (without the prompt).
    """
    # ── one-time setup ──────────────────────────────────────────────────────
    # Encode '\n' and look up its token id. Most BPE tokenisers have a single
    # newline token; we take the last id produced by encoding '\n' alone.
    newline_id = tokenizer.encode('\n', add_special_tokens=False)[-1]

    eos_ids = [tokenizer.eos_token_id]
    if im_end_id is not None and im_end_id != tokenizer.unk_token_id:
        eos_ids.append(im_end_id)

    stop_at_newline = StoppingCriteriaList([_NewlineStop(newline_id)])

    # Pre-tokenise the backtrack signal so we don't re-encode it every loop.
    # add_special_tokens=False avoids a stray BOS being prepended.
    backtrack_ids = tokenizer.encode("Backtrack.\n", add_special_tokens=False)
    backtrack_tensor = torch.tensor([backtrack_ids], device=device)

    current_ids = tokenizer(prompt, return_tensors="pt").input_ids.to(device)

    visited_states: set = set()
    output_lines: list  = []
    backtrack_count: int = 0
    # build once, before the loop
    numbers_str = re.search(r'Numbers: ([^.]+)\.', prompt).group(1)
    restart_hint = tokenizer.encode(
        f"(back to: {numbers_str})\n", add_special_tokens=False
    )
    restart_tensor = torch.tensor([restart_hint], device=device)
    # ── step loop ────────────────────────────────────────────────────────────
    for _step in range(max_steps):
        with torch.no_grad():
            out = model.generate(
                current_ids,
                max_new_tokens=max_new_tokens_per_step,
                do_sample=do_sample,
                stopping_criteria=stop_at_newline,
                pad_token_id=tokenizer.pad_token_id,
                eos_token_id=eos_ids,
            )

        new_ids  = out[0, current_ids.shape[1]:]
        new_text = tokenizer.decode(new_ids, skip_special_tokens=False)
        output_lines.append(new_text)

        # ── termination checks ───────────────────────────────────────────────
        if any(tok in new_text for tok in ("<|im_end|>", "<|endoftext|>")):
            break
        if "Answer:" in new_text:
            break

        # ── parse (left: ...) ────────────────────────────────────────────────
        m = re.search(r'\(left:\s*([^)]+)\)', new_text)
        if not m:
            current_ids = out
            continue

        try:
            nums  = [float(x) for x in m.group(1).strip().split()]
        except ValueError:
            current_ids = out
            continue

        state = tuple(sorted(round(x, 4) for x in nums))

        # ── Backtrack. injection logic ────────────────────────────────────────
        inject = False

        # Trigger 1: terminal dead-end — 1 number left and it isn't 24
        if len(state) == 1 and abs(state[0] - 24.0) > 0.01:
            inject = True

        # Trigger 2: state already visited this run
        elif state in visited_states:
            inject = True

        if inject and backtrack_count < max_backtracks:
            out = torch.cat([out, backtrack_tensor], dim=1)
            output_lines.append("Backtrack.\n")
            visited_states.clear()   # solution path may share states with dead-end
            backtrack_count += 1
        else:
            visited_states.add(state)

        current_ids = out

    return "".join(output_lines)

# Must match IN_CONTEXT_HEADER in backtracking_augmentation.py exactly.
# Any difference (even a trailing space) will shift the model out of distribution.
BACKTRACK_SYSTEM = (
    "Use numbers and basic arithmetic operations (+, -, *, /) to obtain 24. "
    "Each step, you are only allowed to choose two of the remaining numbers to obtain a new number.\n"
    "Step 1: Start by considering possible operations for each pair of numbers.\n"
    "Step 2: Try a path (a pair of two numbers), see if the remaining numbers can possibly reach the goal 24. If not, backtrack and attempt another.\n"
    "Step 3: Branch out to try different orders of operations and combinations, evaluating each outcome.\n"
    "Step 4: If one path doesn't lead to a solution, backtrack and try alternative operations.\n\n"
)
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
    "Numbers: 4 9 10 13. Target: 24.\n"
    "Use each number exactly once with +, -, *, / to reach 24.\n"
    "Steps:\n"
    "4 + 9 = 13 (left: 10 13 13)\n"
    "13 - 10 = 3 (left: 3 13)\n"
    "13 + 3 = 16 (left: 16)\n"
    "Backtrack.\n"
    "13 - 10 = 3 (left: 3 4 9)\n"
    "9 - 3 = 6 (left: 4 6)\n"
    "4 * 6 = 24 (left: 24)\n"
    "Answer: 4 * (9 - (13 - 10)) = 24\n\n"
    "Now solve this puzzle:\n"
)

def make_prompt(puzzle):
    # IN_CONTEXT_HEADER must be identical to the one used during training.
    # The puzzle block must also match — same field order, same "Steps:" line.
    user_content = (
        f"{BACKTRACK_SYSTEM}"
        f"{IN_CONTEXT_HEADER}"          # your existing 4 examples
        f"Numbers: {puzzle}. Target: 24.\n"
        f"Use each number exactly once with +, -, *, / to reach 24.\n"
        f"Steps:"
    )
    return f"<|im_start|>user\n{user_content}<|im_end|>\n<|im_start|>assistant\n"

for puzzle in test_puzzles[:5]:
    prompt = make_prompt(puzzle)

    print(f"Stop token IDs: {[tokenizer.eos_token_id, im_end_id]}")  # sanity check

    response = generate_with_backtrack_control(
        model, tokenizer, prompt,
        max_steps=24,
        max_new_tokens_per_step=50,
        max_backtracks=6,
        do_sample=args.sample,
        im_end_id=im_end_id,
    )

    print(f"\n{'='*60}")
    print(f"PUZZLE: {puzzle}")
    print(f"FULL PROMPT:")
    print(repr(prompt))
    print(f"RAW OUTPUT (with special tokens):")
    print(repr(response))
    print(f"DECODED:")
    print(response)