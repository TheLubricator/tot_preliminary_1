from backtracking_augmentation import generate_augmented_dataset

# Adjust paths to match your setup
tree_dir = "./raw_tree"                # folder with 500 tree JSONs
dead_end_db_path = "dead_end_db.json"  # path to dead_end_db.json
output_path = "training_data_augmented.jsonl"

# Optional: adjust the target ratio (default 0.20 = 20% backtracking)
stats = generate_augmented_dataset(
    tree_dir=tree_dir,
    dead_end_db_path=dead_end_db_path,
    output_path=output_path,
    target_backtrack_ratio=0.20,   # you can change this
    seed=42
)

print(stats)