"""Model and human comparisons derived from durable attempt results, never game files."""
from collections import defaultdict


def model_key(name):
    """Formatting-insensitive identity; never replace the reported trace name."""
    return ''.join(char for char in (name or '').casefold() if char.isalnum()) or None


def leaderboard(attempts, version=None, humans=True):
    """Rank attempt summaries (``Attempt.summary()`` rows); a pure function CI can reuse.

    Models group by normalized model name and reasoning effort. Humans share the
    same table, one group per GitHub account, unless ``humans`` is false.
    """
    groups = defaultdict(list)
    display_names = {}
    logins = {}
    excluded = 0
    # First completed matching attempt supplies the readable model spelling.
    # Break timestamp ties by ID so refresh order cannot rename a group.
    for a in sorted(attempts, key=lambda a: (a.get('last_activity', 0), a.get('id', ''))):
        if version is not None and (a.get('harness_version') or 'unknown') != version:
            continue
        player = a['player']
        human = player.get('player_type') == 'human'
        if a['state'] != 'finished' or (human and not humans):
            continue
        if not a['result'] or type(a['result'].get('score')) is not int:
            excluded += 1
            continue
        if human:
            account = a.get('human_github_id')
            # GitHub logins can be renamed; the newest spelling is shown.
            if player.get('github_login'): logins[account] = player['github_login']
            groups[('human', account, None)].append(a)
            continue
        normalized = model_key(player.get('model'))
        display_names.setdefault(normalized, player.get('model').strip() if normalized else None)
        groups[('model', normalized, player.get('reasoning_effort') or None)].append(a)
    rows = []
    for (kind, identity, effort), runs in groups.items():
        scores = [a['result']['score'] for a in runs]
        row = {'player_type': kind, 'attempt_count': len(runs), 'average_score': sum(scores)/len(scores),
               'best_score': max(scores), 'ascensions': sum(a['outcome'] == 'ascended' for a in runs)}
        best = max(runs, key=lambda a: a['result']['score'])
        longest = max((a for a in runs if type(a['result'].get('turns')) is int),
                      key=lambda a: a['result']['turns'], default=None)
        row.update(best_score_id=best.get('id'), max_moves=longest['result']['turns'] if longest else None,
                   max_moves_id=longest.get('id') if longest else None)
        if kind == 'human':
            row |= {'player': logins.get(identity), 'github_id': identity,
                    'model': None, 'model_key': None, 'reasoning_effort': None}
        else:
            row |= {'player': display_names[identity], 'model': display_names[identity],
                    'model_key': identity, 'reasoning_effort': effort}
        rows.append(row)
    rows.sort(key=lambda row: (-row['best_score'], row['player_type'] != 'model', row['model_key'] or '',
                               row['reasoning_effort'] or '', str(row.get('github_id') or '')))
    for index, row in enumerate(rows):
        row['rank'] = rows[index-1]['rank'] if index and rows[index-1]['best_score'] == row['best_score'] else index+1
    return {'groups': rows, 'scored_attempts': sum(row['attempt_count'] for row in rows),
            'excluded_attempts': excluded}
