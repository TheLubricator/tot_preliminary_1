"""
hyperparam_search.py  ── Run this FIRST
Finds best hyperparameters via Optuna, then immediately does the final
3-epoch run with the winning params.

Changes vs. the suggested version:
  1. bf16=True / fp16=False  (RTX 5080 Blackwell tensor cores)
  2. Full 3-epoch trials     (you have the hardware; 1-epoch shortcuts
                              gave weaker signal)
  3. batch_size search space includes 16  (16 GB VRAM is plenty)
  4. gradient_accumulation_steps halved to 4 when batch_size=16
     so effective batch stays reasonable (~64)
  5. evaluation_strategy -> eval_strategy  (HF >=4.46 deprecation fix)
  6. N_TRIALS = 10  (raises to 20 trivially if you want more coverage)
  7. remove_columns list is explicit – avoids silent column-name bugs
  8. model reload uses local cache path to skip repeated downloads
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

# ── Config ────────────────────────────────────────────────────────────────────

JSONL_PATH  = "training_data_augmented.jsonl"
MODEL_NAME  = "HuggingFaceTB/SmolLM-360M"
SAVE_PATH   = "./smollm_finetuned_best"
SEED        = 42
MAX_LENGTH  = 512    # max combined char len in dataset is ~526 chars → ~200 tokens;
                     # 512 tokens is ample and saves VRAM vs 1024
N_TRIALS    = 10

# ── Load & split (puzzle-level) ───────────────────────────────────────────────

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

# ── Tokenizer (reused across all trials) ─────────────────────────────────────

print(f"\nLoading tokenizer for {MODEL_NAME}...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, use_fast=False)
if tokenizer.pad_token is None:
    tokenizer.add_special_tokens({"pad_token": "[PAD]"})

# ── Tokenize once ─────────────────────────────────────────────────────────────
# NOTE: your dataset has two distinct prompt shapes:
#   (a) plain prompts  (has_deadend_ctx=False)
#   (b) prompts with "Known dead-end states:" block  (has_deadend_ctx=True)
# Both use the same "prompt" / "completion" keys, so no special casing needed.

REMOVE_COLS = train_dataset.column_names  # puzzle, prompt, completion, example_type, …

def preprocess(batch):
    inputs = [
        f"<|im_start|>user\n{p}<|im_end|>\n<|im_start|>assistant"
        for p in batch["prompt"]
    ]
    outputs = [f"{c}<|im_end|>" for c in batch["completion"]]

    model_inputs = tokenizer(
        inputs, max_length=MAX_LENGTH, padding="max_length", truncation=True
    )
    labels = tokenizer(
        outputs, max_length=MAX_LENGTH, padding="max_length", truncation=True
    )["input_ids"]

    model_inputs["labels"] = [
        [(t if t != tokenizer.pad_token_id else -100) for t in seq]
        for seq in labels
    ]
    return model_inputs

print("Tokenizing (once, shared across all trials)...")
tok_train = train_dataset.map(preprocess, batched=True, remove_columns=REMOVE_COLS)
tok_val   = val_dataset.map(preprocess,   batched=True, remove_columns=val_dataset.column_names)

# ── Fresh model loader ────────────────────────────────────────────────────────

def load_fresh_model():
    """Reload from local HF cache. Run the one-liner below first to populate:
       python -c "from transformers import AutoModelForCausalLM; AutoModelForCausalLM.from_pretrained('HuggingFaceTB/SmolLM-360M')"
    """
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
    # Keep effective batch size sensible regardless of per-device batch
    grad_accum = 4 if batch_size == 16 else 8

    training_args = TrainingArguments(
        output_dir=f"./optuna_trial_{trial.number}",
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size,
        gradient_accumulation_steps=grad_accum,
        num_train_epochs=3,           # full trials — RTX 5080 makes this ~2 min each
        learning_rate=lr,
        weight_decay=weight_decay,
        lr_scheduler_type=scheduler,
        eval_strategy="epoch",        # 'evaluation_strategy' deprecated in HF >=4.46
        logging_steps=20,
        bf16=True,                    # Blackwell (RTX 50xx) native precision
        fp16=False,
        push_to_hub=False,
        remove_unused_columns=True,
        save_strategy="no",
        report_to="none",
        seed=SEED,
    )

    model = load_fresh_model()
    trainer = Trainer(
        model=model,
        tokenizer=tokenizer,
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

print(f"\nStarting Optuna: {N_TRIALS} trials × 3 epochs each")
print("Est. time on RTX 5080: ~2 min/trial → ~20-25 min total\n")

optuna.logging.set_verbosity(optuna.logging.WARNING)
study = optuna.create_study(
    direction="minimize",
    study_name="smollm_finetune",
    sampler=optuna.samplers.TPESampler(seed=SEED),
)
study.optimize(objective, n_trials=N_TRIALS)

best       = study.best_params
best_loss  = study.best_value

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
print("All trial results → optuna_results.json")

# ── Final full run with best params ──────────────────────────────────────────

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
    remove_unused_columns=True,
    save_total_limit=2,
    report_to="none",
    seed=SEED,
)

final_trainer = Trainer(
    model=final_model,
    tokenizer=tokenizer,
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
print("Logs → training_logs_best.json")
