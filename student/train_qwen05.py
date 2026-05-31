"""
train_qwen05.py  ── Fine-tuning Qwen2.5-0.5B-Instruct on the 24-game
=====================================================================
Key differences from train_final.py (SmolLM-360M):

  1. Model: Qwen/Qwen2.5-0.5B-Instruct
     - Has a proper instruct chat template (apply_chat_template)
     - Stronger math pretraining — arithmetic steps more reliable
     - No need to add a pad token; Qwen sets pad=eos by convention,
       but we override it to avoid masking confusion

  2. Chat formatting: uses apply_chat_template instead of manual
     <|im_start|>/<|im_end|> construction — Qwen's template handles
     all special tokens correctly

  3. Hyperparams: conservative starting point
     - Lower LR (1e-5) — Qwen instruct is already well-tuned;
       aggressive LR will damage instruction following
     - 8 epochs instead of 12 — Qwen converges faster than SmolLM
       on structured tasks due to better priors
     - grad_accum=4, batch=4 → eff_batch=16 (same as SmolLM run)

  4. MAX_LENGTH=1024 — same as SmolLM run; Qwen context is 32k but
     we don't need more than 1024 for this task
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
from transformers import EarlyStoppingCallback
# ── Config ────────────────────────────────────────────────────────────────────

JSONL_PATH  = "training_data_augmented_v2.jsonl"
MODEL_NAME  = "Qwen/Qwen2.5-0.5B-Instruct"
SAVE_PATH   = "./qwen05_finetuned"
SEED        = 42
MAX_LENGTH  = 1024

# Conservative hyperparams for an instruct model
# Qwen is already instruction-tuned — we want to add a skill, not overwrite
LR           = 1.5e-5
WEIGHT_DECAY = 0.01
BATCH_SIZE   = 4
GRAD_ACCUM   = 4          # eff_batch = 16
SCHEDULER    = "cosine"   # cosine works better than constant_with_warmup for instruct models
NUM_EPOCHS   = 5
WARMUP_RATIO = 0.06

# ── System prompt (identical to SmolLM run for fair comparison) ───────────────

SYSTEM_PROMPT = (
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

# ── Load & split (puzzle-level, same as SmolLM run) ───────────────────────────

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
       set(e["puzzle"] for e in val_rows)), "Puzzle leakage!"

print(f"Train: {len(train_rows)} rows | Val: {len(val_rows)} rows")

eff_batch       = BATCH_SIZE * GRAD_ACCUM
steps_per_epoch = len(train_rows) // eff_batch
total_steps     = steps_per_epoch * NUM_EPOCHS
print(f"Eff batch: {eff_batch} | Steps/epoch: {steps_per_epoch} | "
      f"Total steps: {total_steps}\n")

train_dataset = Dataset.from_list(train_rows)
val_dataset   = Dataset.from_list(val_rows)

# ── Tokenizer ─────────────────────────────────────────────────────────────────

print("Loading tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

# Qwen sets pad_token = eos_token by default, which causes label masking issues.
# Use a dedicated pad token instead.
if tokenizer.pad_token is None or tokenizer.pad_token == tokenizer.eos_token:
    tokenizer.add_special_tokens({"pad_token": "<|pad|>"})

tokenizer.padding_side = "right"  # required for causal LM training

# ── Tokenize using apply_chat_template ───────────────────────────────────────
#
# Qwen2.5-Instruct has a well-defined chat template that handles all special
# tokens. We use it directly rather than manually constructing im_start/im_end.
# The trick for SFT: tokenize prompt and completion separately, then mask
# the prompt portion with -100 so the model only trains on the completion.

def preprocess(batch):
    all_input_ids = []
    all_attention  = []
    all_labels     = []

    for prompt_field, completion in zip(batch["prompt"], batch["completion"]):
        # Build the user content — system prompt + in-context header + puzzle
        user_content = SYSTEM_PROMPT + IN_CONTEXT_HEADER + prompt_field.split("Now solve this puzzle:\n")[-1].strip()

        # --- Tokenize prompt portion only (no completion) ---
        prompt_messages = [{"role": "user", "content": user_content}]
        prompt_text = tokenizer.apply_chat_template(
            prompt_messages,
            tokenize=False,
            add_generation_prompt=True,   # adds the assistant turn opener
        )
        prompt_ids = tokenizer(prompt_text, add_special_tokens=False)["input_ids"]

        # --- Tokenize completion ---
        # Append eos so the model learns to stop
        completion_text = completion + tokenizer.eos_token
        completion_ids  = tokenizer(completion_text, add_special_tokens=False)["input_ids"]

        # Truncate completion if prompt + completion > MAX_LENGTH
        max_completion = MAX_LENGTH - len(prompt_ids)
        if max_completion <= 0:
            continue   # prompt too long — skip
        completion_ids = completion_ids[:max_completion]

        full_ids = prompt_ids + completion_ids
        pad_len  = MAX_LENGTH - len(full_ids)

        input_ids = full_ids + [tokenizer.pad_token_id] * pad_len
        attention  = [1] * len(full_ids) + [0] * pad_len
        labels     = (
            [-100] * len(prompt_ids)   # mask prompt
            + completion_ids           # train on completion
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

# ── Sanity check ──────────────────────────────────────────────────────────────

print("Sanity check — completion labels:")
for idx in range(min(3, len(tok_train))):
    ex          = tok_train[idx]
    label_toks  = [l for l in ex["labels"] if l != -100]
    decoded     = tokenizer.decode(label_toks[:40])
    try:
        first_comp = next(i for i, l in enumerate(ex["labels"]) if l != -100)
    except StopIteration:
        first_comp = len(ex["labels"])

    print(f"  [{idx}] prompt={first_comp} tok | completion={len(label_toks)} tok | "
          f"total={first_comp + len(label_toks)}/{MAX_LENGTH}")
    print(f"        first 40 decoded: {decoded!r}")

    if len(label_toks) == 0:
        raise ValueError(f"Example {idx} has NO completion tokens — check prompt length vs MAX_LENGTH.")
    if first_comp > MAX_LENGTH - 20:
        raise ValueError(f"Example {idx}: prompt ({first_comp} tok) leaves < 20 tokens for completion.")

print("  Sanity check passed\n")

# ── Model ─────────────────────────────────────────────────────────────────────

print("Loading model...")
model = AutoModelForCausalLM.from_pretrained(
    MODEL_NAME,
    device_map="auto",
    torch_dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16,
)

# Resize embeddings only if we added a new pad token
if len(tokenizer) != model.config.vocab_size:
    model.resize_token_embeddings(len(tokenizer))

model.gradient_checkpointing_enable()

# ── Training args ─────────────────────────────────────────────────────────────

EVAL_SAVE_STEPS = max(10, steps_per_epoch)

training_args = TrainingArguments(
    output_dir=SAVE_PATH,

    per_device_train_batch_size=BATCH_SIZE,
    per_device_eval_batch_size=BATCH_SIZE,
    gradient_accumulation_steps=GRAD_ACCUM,
    num_train_epochs=NUM_EPOCHS,

    learning_rate=LR,
    weight_decay=WEIGHT_DECAY,
    lr_scheduler_type=SCHEDULER,
    warmup_ratio=WARMUP_RATIO,

    eval_strategy="steps",
    eval_steps=EVAL_SAVE_STEPS,
    save_strategy="steps",
    save_steps=EVAL_SAVE_STEPS,
    load_best_model_at_end=True,
    metric_for_best_model="eval_loss",
    greater_is_better=False,
    save_total_limit=3,

    logging_steps=5,
    logging_dir="./train_logs_qwen05",

    # Qwen is BF16-native; hard-set rather than runtime check
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
    callbacks=[EarlyStoppingCallback(early_stopping_patience=2)]
)

# ── Train ─────────────────────────────────────────────────────────────────────

print("=" * 60)
print(f"Model:   {MODEL_NAME}")
print(f"Epochs:  {NUM_EPOCHS} | Eff batch: {eff_batch} | LR: {LR:.1e}")
print(f"Sched:   {SCHEDULER} | Total steps: ~{total_steps}")
print("=" * 60 + "\n")

trainer.train()
trainer.save_model(SAVE_PATH)
tokenizer.save_pretrained(SAVE_PATH)

with open("training_logs_qwen05.json", "w") as f:
    json.dump(trainer.state.log_history, f, indent=2)

print(f"\nDone. Model saved → {SAVE_PATH}")
print(f"Logs   → training_logs_qwen05.json")
final_eval = trainer.state.log_history[-1]
print(f"Final eval loss: {final_eval.get('eval_loss', 'see logs')}")
