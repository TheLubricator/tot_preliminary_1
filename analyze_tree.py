import json

p = r'g:\class codes\tot_preliminary_1\teacher\raw_tree\game24_tree_4_6_7_10_20260423_180323.json'
d = json.load(open(p, encoding='utf-8'))

depths = {}
for n in d['nodes']:
    depth = n['depth']
    depths[depth] = depths.get(depth, 0) + 1

print('=== NODE COUNT BY DEPTH ===')
for k in sorted(depths.keys()):
    print(f'Depth {k}: {depths[k]} nodes')

print(f'\nTotal nodes: {len(d["nodes"])}')
print(f'Solutions found: {d["metadata"]["statistics"]["solutions_found"]}')
print(f'API calls: {d["metadata"]["statistics"]["api_calls"]}')
print(f'Max steps: {d["metadata"]["parameters"]["max_steps"]}')

# Check if any depth-2+ nodes have is_solution=true
solutions = [n for n in d['nodes'] if n.get('is_solution', False)]
print(f'Nodes marked as solution: {len(solutions)}')

# Check depth 1 values
depth1_nodes = [n for n in d['nodes'] if n.get('depth') == 1]
print(f'\nDepth 1 nodes (with num_children):')
for n in depth1_nodes:
    print(f'  id={n["id"]}: state={n["state"]}, value={n["value"]}, num_children={n.get("num_children", 0)}')

# Check if depth-1 nodes are "selected" or "pruned"
# If only id=2 was in the queue for depth 1, the others were pruned
print(f'\n=== DID ALL 5 DEPTH-1 NODES GET PROPOSALS? ===')
print(f'If only node 2 generated children, then either:')
print(f'  1. Only node 2 was in queue for depth 1 (WRONG - all 5 were created)')
print(f'  2. Only node 2s proposals succeeded (dedup/dead-end filtering)')
print(f'  3. Only node 2 got evaluated (evaluation loop bug)')
print(f'\nDepth-1 nodes have num_children set. Check which ones > 0:')
for n in sorted(depth1_nodes, key=lambda x: x['id']):
    has_children = n.get("num_children", 0) > 0
    marker = "✓ HAS CHILDREN" if has_children else "✗ NO CHILDREN"
    print(f'  id={n["id"]}: num_children={n.get("num_children", 0)} {marker}')
print(f'\nIf ONLY id=2 has children, then:')
print(f'  - At depth 0 end, queue was set to include only id=2')
print(f'  - Even though all 5 were in next_queue and evaluated')
print(f'  - Beam selection [:n_select_sample] is broken OR')
print(f'  - queue = selected is somehow only getting 1 node')

# Check depth 2 to see parent and why they ranked high
print(f'\nDepth 2 nodes (with parent info):')
depth2_nodes = [n for n in d['nodes'] if n.get('depth') == 2]
for n in depth2_nodes:
    parent_id = n.get('parent_id')
    parent = next((p for p in d['nodes'] if p['id'] == parent_id), None)
    parent_state = parent['state'] if parent else 'unknown'
    parent_value = parent['value'] if parent else 'unknown'
    state = n['state']
    print(f'  id={n["id"]}: state={state}, value={n["value"]}')
    print(f'    └─ parent id={parent_id}, state={parent_state}, parent_value={parent_value}')
