"""
finetune_yours.py  ── Use if skipping Optuna (uses paper's known-good params)
FIX: same tokenization fix as hyperparam_search.py
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
    EarlyStoppingCallback,
)

JSONL_PATH   = "training_data_augmented.jsonl"
MODEL_NAME   = "HuggingFaceTB/SmolLM-360M"
SAVE_PATH    = "./smollm_finetuned_yours"
LOGS_PATH    = "./training_logs_yours.json"
SEED         = 42
VAL_FRACTION = 0.1
MAX_LENGTH   = 512

LR           = 3.17e-5
WEIGHT_DECAY = 0.06
BATCH_SIZE   = 4
GRAD_ACCUM   = 8
NUM_EPOCHS   = 3

print("Loading dataset...")
with open(JSONL_PATH) as f:
    examples = [json.loads(line) for line in f if line.strip()]

random.seed(SEED)
unique_puzzles = list({e["puzzle"] for e in examples})
random.shuffle(unique_puzzles)
split_idx     = int(len(unique_puzzles) * (1 - VAL_FRACTION))
train_puzzles = set(unique_puzzles[:split_idx])
val_puzzles   = set(unique_puzzles[split_idx:])

train_rows = [e for e in examples if e["puzzle"] in train_puzzles]
val_rows   = [e for e in examples if e["puzzle"] in val_puzzles]
assert train_puzzles.isdisjoint(val_puzzles)

train_dataset = Dataset.from_list(train_rows)
val_dataset   = Dataset.from_list(val_rows)

print(f"Train: {len(train_puzzles)} puzzles → {len(train_rows)} rows")
print(f"Val:   {len(val_puzzles)} puzzles → {len(val_rows)} rows")

print(f"Loading {MODEL_NAME}...")
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, use_fast=False)
model     = AutoModelForCausalLM.from_pretrained(MODEL_NAME, device_map="auto")

if tokenizer.pad_token is None:
    tokenizer.add_special_tokens({"pad_token": "[PAD]"})
    model.resize_token_embeddings(len(tokenizer))

model.gradient_checkpointing_enable()

# ── CORRECT tokenization: concatenate then mask prompt ───────────────────────

def preprocess(batch):
    all_input_ids, all_attention, all_labels = [], [], []

    for prompt, completion in zip(batch["prompt"], batch["completion"]):
        prompt_text     = f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
        completion_text = f"{completion}<|im_end|>"

        prompt_ids     = tokenizer(prompt_text,     add_special_tokens=False)["input_ids"]
        completion_ids = tokenizer(completion_text, add_special_tokens=False)["input_ids"]

        full_ids = prompt_ids + completion_ids

        if len(full_ids) > MAX_LENGTH:
            full_ids   = full_ids[-MAX_LENGTH:]
            prompt_len = max(0, MAX_LENGTH - len(completion_ids))
        else:
            prompt_len = len(prompt_ids)

        pad_len   = MAX_LENGTH - len(full_ids)
        input_ids = full_ids + [tokenizer.pad_token_id] * pad_len
        attention = [1] * len(full_ids) + [0] * pad_len
        labels    = [-100] * prompt_len + full_ids[prompt_len:] + [-100] * pad_len

        assert len(input_ids) == MAX_LENGTH
        assert len(labels)    == MAX_LENGTH

        all_input_ids.append(input_ids)
        all_attention.append(attention)
        all_labels.append(labels)

    return {"input_ids": all_input_ids, "attention_mask": all_attention, "labels": all_labels}

print("Tokenizing...")
tok_train = train_dataset.map(preprocess, batched=True, remove_columns=train_dataset.column_names)
tok_val   = val_dataset.map(preprocess,   batched=True, remove_columns=val_dataset.column_names)

ex = tok_train[0]
label_tokens = [l for l in ex["labels"] if l != -100]
print(f"Sanity check — decoded completion preview: {tokenizer.decode(label_tokens[:30])!r}")

training_args = TrainingArguments(
    output_dir=SAVE_PATH,
    per_device_train_batch_size=BATCH_SIZE,
    per_device_eval_batch_size=BATCH_SIZE,
    gradient_accumulation_steps=GRAD_ACCUM,
    learning_rate=LR,
    weight_decay=WEIGHT_DECAY,
    lr_scheduler_type="cosine",
    num_train_epochs=NUM_EPOCHS,
    eval_strategy="steps",
    eval_steps=25,
    save_strategy="steps",
    save_steps=25,
    load_best_model_at_end=True,
    metric_for_best_model="eval_loss",
    logging_steps=10,
    logging_dir="./train_logs",
    bf16=True,
    fp16=False,
    push_to_hub=False,
    remove_unused_columns=False,
    save_total_limit=2,
    report_to="none",
    seed=SEED,
)

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=tok_train,
    eval_dataset=tok_val,
    callbacks=[EarlyStoppingCallback(early_stopping_patience=3)],
)

trainer.train()
trainer.save_model(SAVE_PATH)
tokenizer.save_pretrained(SAVE_PATH)

with open(LOGS_PATH, "w") as f:
    json.dump(trainer.state.log_history, f, indent=2)

print(f"\nDone. Model → {SAVE_PATH}")
