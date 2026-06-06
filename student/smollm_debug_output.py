"""
debug_output.py  ── shows raw model output for 5 test puzzles
Usage: python debug_output.py --model ./smollm_finetuned_best --csv 24.csv
"""

import argparse, csv
from transformers import AutoModelForCausalLM, AutoTokenizer
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
model     = AutoModelForCausalLM.from_pretrained(args.model, device_map="auto")
model.eval()
device = next(model.parameters()).device

# Must match IN_CONTEXT_HEADER in backtracking_augmentation.py exactly.
# Any difference (even a trailing space) will shift the model out of distribution.
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
    "Backtrack. (to: 4 9 10 13).\n"
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

for puzzle in test_puzzles[:args.n]:
    prompt = make_prompt(puzzle)
    inputs = tokenizer(prompt, return_tensors="pt").to(device)

    # Find the ID for your chat end token
    im_end_id = tokenizer.convert_tokens_to_ids("<|im_end|>")

    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=args.max_new_tokens,
            do_sample=args.sample,
            **({
                "temperature":        0.3,
                "top_p":              0.95,
            } if args.sample else {}),
            pad_token_id=tokenizer.pad_token_id,
            # FORCE the model to stop at either standard EOS or <|im_end|>
            eos_token_id=[tokenizer.eos_token_id, im_end_id] if im_end_id is not None else tokenizer.eos_token_id,
        )

    gen_ids  = out[0][inputs["input_ids"].shape[1]:]
    response = tokenizer.decode(gen_ids, skip_special_tokens=False)

    print(f"\n{'='*60}")
    print(f"PUZZLE: {puzzle}")
    # print(f"FULL PROMPT:")
    # print(repr(prompt))
    print(f"RAW OUTPUT (with special tokens):")
    print(repr(response))
    print(f"DECODED:")
    print(response)