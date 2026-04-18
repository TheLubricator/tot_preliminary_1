import json

with open('raw_tree/game24_tree_2_2_7_12_20260418_191112.json', 'r', encoding='utf-8') as f:
    tree = json.load(f)

# Find node 2
node2 = tree['nodes'][2]
print('Node 2 Details:')
print('=' * 90)
print(f"ID: {node2['id']}")
print(f"State: {node2['state']}")
print(f"Value: {node2['value']}")
print(f"Depth: {node2['depth']}")
print()

if node2['evaluation']:
    eval_data = node2['evaluation']
    print('Evaluation:')
    print(f"  llm_judgments: {eval_data.get('llm_judgments', 'MISSING')}")
    print(f"  reasoning: {eval_data.get('reasoning', 'MISSING')}")
    if 'score_breakdown' in eval_data and 'vote_counts' in eval_data['score_breakdown']:
        print(f"  vote_counts: {eval_data['score_breakdown']['vote_counts']}")
    print()
    print('Full reasoning:')
    for line in eval_data.get('reasoning', []):
        print(f'  {line}')
else:
    print('No evaluation data')

print()
print('=' * 90)
print('COMPARISON WITH NODE 1:')
print('=' * 90)
node1 = tree['nodes'][1]
print(f"Node 1 llm_judgments: {node1['evaluation']['llm_judgments']}")
print(f"Node 1 reasoning: {node1['evaluation']['reasoning']}")
