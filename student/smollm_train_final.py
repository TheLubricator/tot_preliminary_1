"""
train_final.py  ── Standalone final training run
================================================
Skips Optuna entirely — best hyperparams are already known from the search:
    lr=2.571e-05, wd=0.037, batch_size=4, scheduler=constant_with_warmup

Key changes vs the 5-epoch run that produced garbage output:
  1. 12 epochs instead of 5 — eval_loss was still falling steeply at epoch 5
     (0.711 → 0.601 → 0.543).  Projecting forward, expect ~0.25 at epoch 12.
     Format adherence typically kicks in reliably below ~0.35.
  2. grad_accumulation_steps halved (8 → 4) — gives eff_batch=16 instead of 32,
     doubling gradient steps per epoch from ~13 to ~27.  Total steps go from
     65 (5 epochs, eff_bs=32) to ~324 (12 epochs, eff_bs=16).  More frequent
     weight updates = faster convergence on a 446-example dataset.
  3. warmup_ratio=0.06 — constant_with_warmup with 0 warmup steps is just
     a flat LR. Now 6% of steps (~19 steps) are used for warmup.
  4. MAX_LENGTH=1024 — required for the in-context header (~280 tokens) +
     puzzle prompt + completion to fit without truncating completions.
"""

import json
import random
import torch
from datasets import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

# ── Config ────────────────────────────────────────────────────────────────────

JSONL_PATH  = "training_data_augmented.jsonl"
MODEL_NAME  = "HuggingFaceTB/SmolLM-360M"
SAVE_PATH   = "./smollm_finetuned_best"
SEED        = 42
MAX_LENGTH  = 1024

# Best hyperparams from Optuna Trial 6 (eval_loss=0.5425, only trial that worked)
BEST_LR            = 2.571914202538463e-05
BEST_WEIGHT_DECAY  = 0.03697089110510541
BEST_BATCH_SIZE    = 4
BEST_SCHEDULER     = "constant_with_warmup"

# Gradient accumulation: halved from 8 → 4
# eff_batch = BEST_BATCH_SIZE * GRAD_ACCUM = 4 * 4 = 16
# steps/epoch = 401 // 16 ≈ 25   (using ~401 train rows from 90% split of 446)
# total steps = 25 * 12 epochs ≈ 300 — enough to converge
GRAD_ACCUM  = 4
NUM_EPOCHS  = 12

# ── Load & split ──────────────────────────────────────────────────────────────

print("Loading dataset...")
with open(JSONL_PATH) as f:
    examples = [json.loads(line) for line in f if line.strip()]

random.seed(SEED)
unique_puzzles = list({e["puzzle"] for e in examples})
random.shuffle(unique_puzzles)
split_idx     = int(len(unique_puzzles) * 0.9)
train_puzzles = set(unique_puzzles[:split_idx])

train_rows = [e for e in examples if e["puzzle"] in train_puzzles]
val_rows   = [e for e in examples if e["puzzle"] not in train_puzzles]
assert set(e["puzzle"] for e in train_rows).isdisjoint(
       set(e["puzzle"] for e in val_rows)), "Puzzle leakage between train/val!"

print(f"Train: {len(train_rows)} rows | Val: {len(val_rows)} rows")

eff_batch       = BEST_BATCH_SIZE * GRAD_ACCUM
steps_per_epoch = len(train_rows) // eff_batch
total_steps     = steps_per_epoch * NUM_EPOCHS
print(f"Eff batch: {eff_batch} | Steps/epoch: {steps_per_epoch} | "
      f"Total steps: {total_steps} (target ≥ 150)\n")

train_dataset = Dataset.from_list(train_rows)
val_dataset   = Dataset.from_list(val_rows)

# ── Tokenizer ─────────────────────────────────────────────────────────────────

print("Loading tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, use_fast=False)
if tokenizer.pad_token is None:
    tokenizer.add_special_tokens({"pad_token": "[PAD]"})

# ── Tokenize ──────────────────────────────────────────────────────────────────
BACKTRACK_SYSTEM = (
    "Use numbers and basic arithmetic operations (+, -, *, /) to obtain 24. "
    "Each step, you are only allowed to choose two of the remaining numbers to obtain a new number.\n"
    "Step 1: Start by considering possible operations for each pair of numbers.\n"
    "Step 2: Try a path (a pair of two numbers), see if the remaining numbers can possibly reach the goal 24. If not, backtrack and attempt another.\n"
    "Step 3: Branch out to try different orders of operations and combinations, evaluating each outcome.\n"
    "Step 4: If one path doesn't lead to a solution, backtrack and try alternative operations.\n\n"
)

def preprocess(batch):
    all_input_ids = []
    all_attention = []
    all_labels    = []

    for prompt, completion in zip(batch["prompt"], batch["completion"]):
        full_prompt = BACKTRACK_SYSTEM + prompt
        prompt_text     = f"<|im_start|>user\n{full_prompt}<|im_end|>\n<|im_start|>assistant\n"
        completion_text = f"{completion}<|im_end|>"

        prompt_ids     = tokenizer(prompt_text,     add_special_tokens=False)["input_ids"]
        completion_ids = tokenizer(completion_text, add_special_tokens=False)["input_ids"]

        max_completion = MAX_LENGTH - len(prompt_ids)
        if max_completion <= 0:
            continue   # prompt alone exceeds MAX_LENGTH — skip
        completion_ids = completion_ids[:max_completion]

        full_ids  = prompt_ids + completion_ids
        pad_len   = MAX_LENGTH - len(full_ids)
        input_ids = full_ids + [tokenizer.pad_token_id] * pad_len
        attention = [1] * len(full_ids) + [0] * pad_len
        labels    = (
            [-100] * len(prompt_ids)   # mask prompt
            + completion_ids           # supervise on completion only
            + [-100] * pad_len         # mask padding
        )

        assert len(input_ids) == MAX_LENGTH
        assert len(labels)    == MAX_LENGTH

        all_input_ids.append(input_ids)
        all_attention.append(attention)
        all_labels.append(labels)

    return {
        "input_ids":      all_input_ids,
        "attention_mask": all_attention,
        "labels":         all_labels,
    }

print("Tokenizing...")
tok_train = train_dataset.map(preprocess, batched=True, remove_columns=train_dataset.column_names)
tok_val   = val_dataset.map(preprocess,   batched=True, remove_columns=val_dataset.column_names)
print(f"  {len(tok_train)} train | {len(tok_val)} val after tokenization\n")

# ── Sanity checks ─────────────────────────────────────────────────────────────

print("Sanity check — completion labels:")
for idx in range(min(3, len(tok_train))):
    ex           = tok_train[idx]
    label_tokens = [l for l in ex["labels"] if l != -100]
    decoded      = tokenizer.decode(label_tokens[:40])
    try:
        first_comp   = next(i for i, l in enumerate(ex["labels"]) if l != -100)
    except StopIteration:
        first_comp = len(ex["labels"])
    comp_count   = len(label_tokens)
    print(f"  [{idx}] prompt={first_comp} tok | completion={comp_count} tok | "
          f"total={first_comp+comp_count}/{MAX_LENGTH}")
    print(f"        first 40 decoded: {decoded!r}")
    if comp_count == 0:
        raise ValueError(f"Example {idx} has NO completion tokens — check MAX_LENGTH and prompt length.")
    if first_comp > MAX_LENGTH - 20:
        raise ValueError(f"Example {idx}: prompt ({first_comp} tok) leaves < 20 tokens for completion.")

print("  Sanity check passed\n")

# ── Model ─────────────────────────────────────────────────────────────────────

print("Loading model...")
model = AutoModelForCausalLM.from_pretrained(MODEL_NAME, device_map="auto")
model.resize_token_embeddings(len(tokenizer))
model.gradient_checkpointing_enable()

# ── Training args ─────────────────────────────────────────────────────────────

# eval_steps and save_steps: every 25 steps ≈ once per epoch at eff_batch=16
EVAL_SAVE_STEPS = max(10, steps_per_epoch)

training_args = TrainingArguments(
    output_dir=SAVE_PATH,

    # Best hyperparams from Optuna
    per_device_train_batch_size=BEST_BATCH_SIZE,
    per_device_eval_batch_size=BEST_BATCH_SIZE,
    learning_rate=BEST_LR,
    weight_decay=BEST_WEIGHT_DECAY,
    lr_scheduler_type=BEST_SCHEDULER,

    # Key changes from the 5-epoch run
    gradient_accumulation_steps=GRAD_ACCUM,   # 8 → 4, doubles steps/epoch
    num_train_epochs=NUM_EPOCHS,               # 5 → 12
    warmup_ratio=0.06,                         # ~19 actual warmup steps

    # Evaluation & checkpointing
    eval_strategy="steps",
    eval_steps=EVAL_SAVE_STEPS,
    save_strategy="steps",
    save_steps=EVAL_SAVE_STEPS,
    load_best_model_at_end=True,
    metric_for_best_model="eval_loss",
    greater_is_better=False,
    save_total_limit=3,

    # Logging
    logging_steps=5,
    logging_dir="./train_logs_final",

    # Precision — bf16 preferred; falls back gracefully if not supported
    bf16=torch.cuda.is_bf16_supported(),
    fp16=not torch.cuda.is_bf16_supported(),

    push_to_hub=False,
    remove_unused_columns=False,
    report_to="none",
    seed=SEED,
)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=tok_train,
    eval_dataset=tok_val,
)

# ── Train ─────────────────────────────────────────────────────────────────────

print("="*60)
print(f"Training: {NUM_EPOCHS} epochs | eff_batch={eff_batch} | "
      f"lr={BEST_LR:.2e} | sched={BEST_SCHEDULER}")
print(f"Expected total steps: ~{total_steps}  (target ≥ 150)")
print("="*60 + "\n")

trainer.train()
trainer.save_model(SAVE_PATH)
tokenizer.save_pretrained(SAVE_PATH)

with open("training_logs_final.json", "w") as f:
    json.dump(trainer.state.log_history, f, indent=2)

print(f"\nDone. Model saved → {SAVE_PATH}")
print(f"Logs saved → training_logs_final.json")
print(f"\nFinal eval loss: {trainer.state.log_history[-1].get('eval_loss', 'see logs')}")
