"""Model comparisons derived from durable attempt results, never game files."""
from collections import defaultdict


def leaderboard(attempts, version=None):
    groups = defaultdict(list)
    excluded = 0
    for a in attempts:
        if version is not None and (a.info.get('harness_version') or 'unknown') != version:
            continue
        player = a.info['metadata']
        if a.state != 'finished' or player.get('player_type') == 'human':
            continue
        if not a.result or type(a.result.get('score')) is not int:
            excluded += 1
            continue
        key = (player.get('model') or None, player.get('reasoning_effort') or None)
        groups[key].append(a)
    rows = []
    for (model, effort), runs in groups.items():
        scores = [a.result['score'] for a in runs]
        rows.append({'model': model, 'reasoning_effort': effort,
                     'attempt_count': len(runs), 'average_score': sum(scores)/len(scores),
                     'best_score': max(scores), 'ascensions': sum(a.outcome == 'ascended' for a in runs)})
    rows.sort(key=lambda row: (-row['average_score'], row['model'] or '', row['reasoning_effort'] or ''))
    for index, row in enumerate(rows):
        row['rank'] = rows[index-1]['rank'] if index and rows[index-1]['average_score'] == row['average_score'] else index+1
    return {'groups': rows, 'scored_attempts': sum(row['attempt_count'] for row in rows),
            'excluded_attempts': excluded}
