import json

with open('raw_tree/game24_tree_2_2_7_12_20260418_191112.json', 'r', encoding='utf-8') as f:
    tree = json.load(f)

# Get all depth-2 nodes
depth2_nodes = [n for n in tree['nodes'] if n['depth'] == 2]
depth2_nodes_sorted = sorted(depth2_nodes, key=lambda x: x['value'], reverse=True)

print('All DEPTH-2 nodes (sorted by value - highest first):')
print('=' * 90)
for i, node in enumerate(depth2_nodes_sorted, 1):
    parent_id = node['parent_id']
    print(f"{i}. ID {node['id']:2d}: value={node['value']:6.3f} | state={str(node['state']):20s} | parent={parent_id:2d} | children={node['num_children']}")

print()
print('=' * 90)
print('INSIGHT:')
print('=' * 90)
top5 = depth2_nodes_sorted[:5]
print('Top 5 depth-2 nodes by value:')
for i, node in enumerate(top5, 1):
    print(f"  {i}. ID {node['id']:2d} (value={node['value']:.3f}) from parent ID {node['parent_id']}")
