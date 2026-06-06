"""
hyperparam_search.py  ── FIXED VERSION
Key fixes from the degenerate output bug:
  1. MAX_LENGTH increased to 768 (completions were getting truncated/masked)
  2. Truncation now keeps the START of the sequence (prompt + beginning of completion)
     rather than the end — so the prompt context is never lost
  3. Added a stronger sanity check that actually prints decoded labels so you
     can verify the model is learning the right thing before wasting GPU time
  4. Removed the broken prompt_len recalculation on truncation
"""

import json
import random
import torch
import optuna
from datasets import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
)

JSONL_PATH  = "training_data_augmented.jsonl"
MODEL_NAME  = "HuggingFaceTB/SmolLM-360M"
SAVE_PATH   = "./smollm_finetuned_best"
SEED        = 42
# FIX: 1024 to accommodate IN_CONTEXT_HEADER (~280 tokens) + puzzle prompt +
# completion. The old 768 was fine for minimal prompts but too tight with
# the 4 in-context examples now prepended to every prompt.
MAX_LENGTH  = 1024
N_TRIALS    = 10

# ── Load & split (unchanged — puzzle-level split is correct) ──────────────────

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
       set(e["puzzle"] for e in val_rows)), "Leakage!"

print(f"Train: {len(train_rows)} rows | Val: {len(val_rows)} rows")

train_dataset = Dataset.from_list(train_rows)
val_dataset   = Dataset.from_list(val_rows)

# ── Tokenizer ─────────────────────────────────────────────────────────────────

print("Loading tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, use_fast=False)
if tokenizer.pad_token is None:
    tokenizer.add_special_tokens({"pad_token": "[PAD]"})

# ── Tokenize ──────────────────────────────────────────────────────────────────
#
# FIX 2: Truncation strategy.
#
# OLD (broken):
#   full_ids = full_ids[-MAX_LENGTH:]          # keep the END
#   prompt_len = max(0, MAX_LENGTH - len(completion_ids))  # WRONG recalculation
#
# The old code truncated from the LEFT, cutting off the start of the prompt.
# Then it recalculated prompt_len incorrectly, causing completion tokens to be
# masked as -100. The model trained on mostly -100 labels → learned nothing.
#
# NEW (correct):
#   Tokenize prompt and completion separately.
#   If prompt + completion > MAX_LENGTH, truncate the COMPLETION from the right.
#   The prompt must always be fully present — it's the context the model needs.
#   Labels: -100 for prompt tokens, completion token ids for the rest.

def preprocess(batch):
    all_input_ids = []
    all_attention = []
    all_labels    = []

    for prompt, completion in zip(batch["prompt"], batch["completion"]):
        prompt_text     = f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
        completion_text = f"{completion}<|im_end|>"

        prompt_ids     = tokenizer(prompt_text,     add_special_tokens=False)["input_ids"]
        completion_ids = tokenizer(completion_text, add_special_tokens=False)["input_ids"]

        # FIX: Truncate the COMPLETION if total is too long — never truncate the prompt
        max_completion = MAX_LENGTH - len(prompt_ids)
        if max_completion <= 0:
            # Prompt itself exceeds MAX_LENGTH — skip this example
            # (shouldn't happen with 768 limit and these prompt lengths)
            continue
        completion_ids = completion_ids[:max_completion]

        full_ids   = prompt_ids + completion_ids
        prompt_len = len(prompt_ids)

        # Pad to MAX_LENGTH
        pad_len   = MAX_LENGTH - len(full_ids)
        input_ids = full_ids + [tokenizer.pad_token_id] * pad_len
        attention  = [1] * len(full_ids) + [0] * pad_len

        # Labels: mask prompt and padding, keep completion
        labels = (
            [-100] * prompt_len          # mask prompt — don't train on it
            + completion_ids             # train on completion tokens
            + [-100] * pad_len           # mask padding
        )

        assert len(input_ids) == MAX_LENGTH, f"input_ids length mismatch: {len(input_ids)}"
        assert len(labels)    == MAX_LENGTH, f"labels length mismatch: {len(labels)}"

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
print(f"  {len(tok_train)} train | {len(tok_val)} val")

# ── FIX 3: Stronger sanity check ─────────────────────────────────────────────
# Check multiple examples. If non-masked tokens = 0 for any of them,
# something is wrong with the tokenization before you waste GPU time.

print("\nSanity check on tokenization:")
for idx in range(min(3, len(tok_train))):
    ex = tok_train[idx]
    label_tokens  = [l for l in ex["labels"] if l != -100]
    prompt_masked = sum(1 for l in ex["labels"] if l == -100 and
                        ex["input_ids"][ex["labels"].index(l)] != tokenizer.pad_token_id)
    decoded = tokenizer.decode(label_tokens[:40])
    print(f"  Example {idx}: {len(label_tokens)} completion tokens")
    print(f"    First 40 decoded: {decoded!r}")
    if len(label_tokens) == 0:
        raise ValueError(
            f"Example {idx} has NO label tokens! "
            "The model would train on all -100 and learn nothing. "
            "Check your MAX_LENGTH and prompt lengths."
        )

print("  Sanity check passed — labels look correct\n")

# Extra check: make sure the in-context header isn't eating all the space.
# With IN_CONTEXT_HEADER ≈ 280 tokens + puzzle ≈ 25 tokens, prompt should
# be ~305 tokens, leaving ~720 tokens for completions at MAX_LENGTH=1024.
print("Prompt-length spot check (first 3 examples):")
for idx in range(min(3, len(tok_train))):
    ex    = tok_train[idx]
    ids   = ex["input_ids"]
    lbls  = ex["labels"]
    # prompt tokens = everything before the first non-(-100) label
    try:
        first_completion_pos = next(i for i, l in enumerate(lbls) if l != -100)
    except StopIteration:
        first_completion_pos = len(lbls)
    prompt_tok_count      = first_completion_pos
    completion_tok_count  = sum(1 for l in lbls if l != -100)
    print(f"  Example {idx}: prompt={prompt_tok_count} tokens, "
          f"completion={completion_tok_count} tokens, "
          f"total={prompt_tok_count + completion_tok_count}/{MAX_LENGTH}")
    if prompt_tok_count > MAX_LENGTH - 20:
        raise ValueError(
            f"Example {idx}: prompt alone ({prompt_tok_count} tokens) leaves "
            f"fewer than 20 tokens for the completion. Increase MAX_LENGTH "
            f"or shorten IN_CONTEXT_HEADER."
        )
print()

# ── Model loader ──────────────────────────────────────────────────────────────

def load_fresh_model():
    m = AutoModelForCausalLM.from_pretrained(MODEL_NAME, device_map="auto")
    m.resize_token_embeddings(len(tokenizer))
    m.gradient_checkpointing_enable()
    return m

# ── Optuna objective ──────────────────────────────────────────────────────────

def objective(trial):
    lr           = trial.suggest_float("learning_rate", 5e-6, 1e-4, log=True)
    weight_decay = trial.suggest_float("weight_decay", 0.0, 0.2)
    batch_size   = trial.suggest_categorical("batch_size", [4, 8, 16])
    scheduler    = trial.suggest_categorical(
        "lr_scheduler_type", ["cosine", "linear", "constant_with_warmup"]
    )
    grad_accum = 4 if batch_size == 16 else 8

    training_args = TrainingArguments(
        output_dir=f"./optuna_trial_{trial.number}",
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size,
        gradient_accumulation_steps=grad_accum,
        # FIX: 5 epochs — 503 examples at eff. batch 32 = ~16 steps/epoch.
        # 3 epochs was only ~48 gradient steps total, not enough for a 360M
        # model to overwrite pretraining distributions on a structured format.
        num_train_epochs=5,
        learning_rate=lr,
        weight_decay=weight_decay,
        lr_scheduler_type=scheduler,
        # FIX: explicit warmup so constant_with_warmup actually warms up.
        # 0.06 * total_steps gives ~5 warmup steps per trial — small but real.
        warmup_ratio=0.06,
        eval_strategy="epoch",
        logging_steps=20,
        bf16=True,
        fp16=False,
        push_to_hub=False,
        remove_unused_columns=False,
        save_strategy="no",
        report_to="none",
        seed=SEED,
    )

    model = load_fresh_model()
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tok_train,
        eval_dataset=tok_val,
    )
    trainer.train()
    eval_loss = trainer.evaluate()["eval_loss"]

    print(f"  Trial {trial.number}: lr={lr:.2e}, wd={weight_decay:.3f}, "
          f"bs={batch_size}, sched={scheduler} → eval_loss={eval_loss:.4f}")

    del model, trainer
    torch.cuda.empty_cache()
    return eval_loss

# ── Run search ────────────────────────────────────────────────────────────────

print(f"Starting Optuna: {N_TRIALS} trials × 5 epochs each\n")

optuna.logging.set_verbosity(optuna.logging.WARNING)
study = optuna.create_study(
    direction="minimize",
    study_name="smollm_finetune",
    sampler=optuna.samplers.TPESampler(seed=SEED),
)
study.optimize(objective, n_trials=N_TRIALS)

best      = study.best_params
best_loss = study.best_value

print("\n" + "="*50)
print(f"Best eval loss: {best_loss:.4f}")
print("Best params:")
for k, v in best.items():
    print(f"  {k}: {v}")
print("="*50)

with open("optuna_results.json", "w") as f:
    json.dump({
        "best_params": best,
        "best_eval_loss": best_loss,
        "all_trials": [{"number": t.number, "params": t.params, "value": t.value}
                       for t in study.trials],
    }, f, indent=2)
print("Results → optuna_results.json")

# ── Final run ─────────────────────────────────────────────────────────────────

print("\nFinal 5-epoch run with best params...")
grad_accum_final = 4 if best["batch_size"] == 16 else 8

final_model = load_fresh_model()
final_args  = TrainingArguments(
    output_dir=SAVE_PATH,
    per_device_train_batch_size=best["batch_size"],
    per_device_eval_batch_size=best["batch_size"],
    gradient_accumulation_steps=grad_accum_final,
    num_train_epochs=5,
    learning_rate=best["learning_rate"],
    weight_decay=best["weight_decay"],
    lr_scheduler_type=best["lr_scheduler_type"],
    warmup_ratio=0.06,
    eval_strategy="steps",
    eval_steps=25,
    save_strategy="steps",
    save_steps=25,
    load_best_model_at_end=True,
    metric_for_best_model="eval_loss",
    logging_steps=10,
    logging_dir="./train_logs_best",
    bf16=True,
    fp16=False,
    push_to_hub=False,
    remove_unused_columns=False,
    save_total_limit=2,
    report_to="none",
    seed=SEED,
)

final_trainer = Trainer(
    model=final_model,
    args=final_args,
    train_dataset=tok_train,
    eval_dataset=tok_val,
)
final_trainer.train()
final_trainer.save_model(SAVE_PATH)
tokenizer.save_pretrained(SAVE_PATH)

with open("training_logs_best.json", "w") as f:
    json.dump(final_trainer.state.log_history, f, indent=2)

print(f"\nDone! Model → {SAVE_PATH}")
