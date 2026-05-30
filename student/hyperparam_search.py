"""
hyperparam_search.py  ── Run this FIRST
Finds best hyperparameters via Optuna, then does the final 3-epoch run.

FIX: tokenization now concatenates prompt+completion into one sequence
and masks the prompt portion of labels with -100, so loss is computed
only on the completion tokens in their correct position.
"""

import json
import random
import torch
import optuna
from datasets import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    DataCollatorForSeq2Seq,
    Trainer,
    TrainingArguments,
)

JSONL_PATH  = "training_data_augmented.jsonl"
MODEL_NAME  = "HuggingFaceTB/SmolLM-360M"
SAVE_PATH   = "./smollm_finetuned_best"
SEED        = 42
MAX_LENGTH  = 512
N_TRIALS    = 10

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
       set(e["puzzle"] for e in val_rows)), "Leakage!"

print(f"Train: {len(train_rows)} rows | Val: {len(val_rows)} rows")

train_dataset = Dataset.from_list(train_rows)
val_dataset   = Dataset.from_list(val_rows)

# ── Tokenizer ─────────────────────────────────────────────────────────────────

print(f"Loading tokenizer...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, use_fast=False)
if tokenizer.pad_token is None:
    tokenizer.add_special_tokens({"pad_token": "[PAD]"})

# ── Tokenize: concatenate prompt+completion, mask prompt labels ───────────────
#
# CORRECT approach:
#   full sequence = [prompt_tokens] + [completion_tokens]
#   labels        = [-100 * len(prompt)] + [completion_tokens]
#
# This means:
#   - The model sees the full context when predicting each completion token
#   - Loss is only computed on the completion tokens
#   - The model learns to continue a prompt, not predict in isolation

def preprocess(batch):
    all_input_ids = []
    all_attention = []
    all_labels    = []

    for prompt, completion in zip(batch["prompt"], batch["completion"]):
        # Build the full chat-formatted text
        prompt_text     = f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
        completion_text = f"{completion}<|im_end|>"

        prompt_ids     = tokenizer(prompt_text,     add_special_tokens=False)["input_ids"]
        completion_ids = tokenizer(completion_text, add_special_tokens=False)["input_ids"]

        full_ids = prompt_ids + completion_ids

        # Truncate to MAX_LENGTH if needed (keep from the right — preserve completion)
        if len(full_ids) > MAX_LENGTH:
            full_ids = full_ids[-MAX_LENGTH:]
            # Recalculate how much of the prompt survived truncation
            prompt_len = max(0, MAX_LENGTH - len(completion_ids))
        else:
            prompt_len = len(prompt_ids)

        # Pad to MAX_LENGTH
        pad_len      = MAX_LENGTH - len(full_ids)
        input_ids    = full_ids + [tokenizer.pad_token_id] * pad_len
        attention    = [1] * len(full_ids) + [0] * pad_len

        # Labels: -100 for prompt tokens and padding, completion token ids elsewhere
        labels = (
            [-100] * prompt_len                          # mask prompt
            + full_ids[prompt_len:]                      # completion tokens as labels
            + [-100] * pad_len                           # mask padding
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
print(f"  {len(tok_train)} train | {len(tok_val)} val")

# Sanity check: verify labels look right on one example
ex = tok_train[0]
label_tokens = [l for l in ex["labels"] if l != -100]
print(f"  Sanity check — non-masked label tokens in example 0: {len(label_tokens)} "
      f"(should be ~completion length, not 0)")
print(f"  Decoded completion preview: {tokenizer.decode(label_tokens[:30])!r}")

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
        num_train_epochs=3,
        learning_rate=lr,
        weight_decay=weight_decay,
        lr_scheduler_type=scheduler,
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

print(f"\nStarting Optuna: {N_TRIALS} trials × 3 epochs each\n")

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

print("\nFinal 3-epoch run with best params...")
grad_accum_final = 4 if best["batch_size"] == 16 else 8

final_model = load_fresh_model()
final_args  = TrainingArguments(
    output_dir=SAVE_PATH,
    per_device_train_batch_size=best["batch_size"],
    per_device_eval_batch_size=best["batch_size"],
    gradient_accumulation_steps=grad_accum_final,
    num_train_epochs=3,
    learning_rate=best["learning_rate"],
    weight_decay=best["weight_decay"],
    lr_scheduler_type=best["lr_scheduler_type"],
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
