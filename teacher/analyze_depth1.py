import json

with open('raw_tree/game24_tree_2_2_7_12_20260418_191112.json', 'r', encoding='utf-8') as f:
    tree = json.load(f)

root = tree['nodes'][0]
print('=' * 90)
print('PUZZLE: [2, 2, 7, 12]')
print('=' * 90)
print(f"\nDepth 1 Nodes (from root):")
print(f"Total at depth 1: {root['num_children']}")
print()

depth1_nodes = []
for child_id_str in root['children'].keys():
    child_id = int(child_id_str)
    child = tree['nodes'][child_id]
    depth1_nodes.append(child)

# Sort by value to see order
depth1_nodes_sorted = sorted(depth1_nodes, key=lambda x: x['value'], reverse=True)

for i, node in enumerate(depth1_nodes_sorted, 1):
    has_children = 'YES' if node['num_children'] > 0 else 'NO'
    judgment = 'unknown'
    if node['evaluation'] and 'reasoning' in node['evaluation']:
        reasoning_str = str(node['evaluation']['reasoning']).lower()
        if 'sure' in reasoning_str:
            judgment = 'sure'
        elif 'likely' in reasoning_str:
            judgment = 'likely'
    
    print(f"{i}. ID {node['id']:2d}: value={node['value']:6.3f} | state={str(node['state']):20s} | judgment={judgment:8s} | expanded={has_children}")

print(f"\n" + '=' * 90)
print("KEY OBSERVATION:")
print('=' * 90)
highest_value_node = depth1_nodes_sorted[0]
expanded_nodes = [n['id'] for n in depth1_nodes if n['num_children'] > 0]
print(f"Node with HIGHEST value: ID {highest_value_node['id']} (value={highest_value_node['value']:.3f})")
print(f"Nodes that WERE expanded: {expanded_nodes}")
print()
if highest_value_node['id'] in expanded_nodes:
    print("✅ CONSISTENT: Highest-value node WAS expanded")
else:
    print("⚠️  INCONSISTENT: Highest-value node was NOT expanded!")

# Show depth-2 nodes
print(f"\n" + '=' * 90)
print("DEPTH 2 ANALYSIS:")
print('=' * 90)
for depth1 in depth1_nodes:
    if depth1['num_children'] > 0:
        print(f"\nNode {depth1['id']} expanded to:")
        for child_id_str in depth1['children'].keys():
            child_id = int(child_id_str)
            child = tree['nodes'][child_id]
            print(f"  - ID {child['id']:2d}: state={child['state']}, value={child['value']:.3f}, children={child['num_children']}")
