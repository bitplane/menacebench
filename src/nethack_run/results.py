"""Check submitted run packages and build the static results site.

Used by the menacebench-results CI. Pull requests are treated as data: only
``attempts/<uuid>/`` package files may change, every package must load and
match its digests, and packages recorded in a published official image are
replayed in that image (network off, read-only). Packages from development
builds or unpublished images cannot be replayed by CI; they are listed as
unverified and kept out of the rankings.
"""
import html
import json
from pathlib import Path
import re
import subprocess
import uuid

from .engine import ROOT
from .leaderboard import leaderboard
from .package import load_package, verify

PACKAGE_FILES = {'attempt.json', 'events.jsonl.gz'}
ALLOWED = re.compile(r'attempts/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/'
                     r'(attempt\.json|events\.jsonl\.gz|disqualified\.json)')


def official_images():
    return json.loads((ROOT/'config/official-images.json').read_text())


def check(folder: Path):
    """Classify one package folder as verified, unverified or failed."""
    folder = Path(folder)
    record = {'attempt_id': folder.name, 'status': 'failed', 'reason': None, 'image_digest': None}
    try:
        manifest, _ = load_package(folder)
        if manifest['attempt_id'] != folder.name or str(uuid.UUID(folder.name)) != folder.name:
            raise ValueError('Folder name does not match the attempt ID')
    except (OSError, ValueError, KeyError, TypeError) as error:
        record['reason'] = f'Invalid package: {error}'
        return record
    image = record['image_digest'] = manifest['image_digest']
    if image is None:
        return record | {'status': 'unverified', 'reason': 'Recorded with a development build'}
    if image not in official_images():
        return record | {'status': 'unverified', 'reason': 'Recorded in an image that is not published'}
    from .images import dispatch
    try:
        if not dispatch(folder):
            verify(folder)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        return record | {'reason': f'Replay failed in its recorded image: {error}'}
    return record | {'status': 'verified', 'reason': 'Replayed in its recorded official image'}


def check_changes(name_status):
    """Return problems with a pull request's ``git diff --name-status`` lines."""
    problems = []
    for line in name_status:
        if not line.strip(): continue
        status, *paths = line.split('\t')
        for path in paths:
            match = ALLOWED.fullmatch(path)
            if not match:
                problems.append(f'{path}: only attempts/<attempt-id>/ package files may change')
            elif status[0] != 'A' and match.group(2) in PACKAGE_FILES:
                problems.append(f'{path}: submitted packages are immutable ({status})')
    return problems


def changed_attempts(name_status):
    return sorted({m.group(1) for line in name_status if line.strip()
                   for path in line.split('\t')[1:] if (m := ALLOWED.fullmatch(path))})


def summary(manifest):
    """The leaderboard row for a package; mirrors Attempt.summary() for finished runs."""
    player = manifest['player_metadata']
    # github_login is only ever set by the dashboard, whose games are owned by the player.
    human = manifest.get('initial_owner_github_id') if player.get('player_type') == 'human' and player.get('github_login') else None
    return {'id': manifest['attempt_id'], 'harness_version': manifest.get('harness_version'), 'player': player,
            'state': 'finished', 'result': manifest['result'], 'outcome': manifest['outcome'],
            'last_activity': 0, 'human_github_id': human}


def load_results(root: Path, statuses):
    rows = []
    for folder in sorted((Path(root)/'attempts').glob('*')):
        if not folder.is_dir() or not (folder/'attempt.json').is_file(): continue
        status = statuses.get(folder.name, {'status': 'failed', 'reason': 'Not checked'})
        if status['status'] == 'failed': continue  # never published
        manifest = json.loads((folder/'attempt.json').read_bytes())
        disqualified = folder/'disqualified.json'
        reason = json.loads(disqualified.read_bytes()).get('reason') if disqualified.exists() else None
        rows.append({'manifest': manifest, 'status': status, 'disqualified': reason})
    return rows


STYLE = """:root{color-scheme:dark;font:15px/1.55 system-ui,sans-serif;background:#101715;color:#e4eae2;--muted:#a1b1a5;--line:#2b3931;--accent:#b6d78b}
body{margin:0}main{max-width:1200px;margin:auto;padding:40px 16px}a{color:var(--accent)}h1{font-size:clamp(30px,4vw,48px);letter-spacing:-1px;margin:8px 0}
.eyebrow{font:11px monospace;letter-spacing:2px;color:var(--accent)}.muted,.fine{color:var(--muted)}.fine{font-size:12px}
section{margin:28px 0;padding:20px;border:1px solid var(--line);border-radius:8px;background:#162019}.head{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}
h2{margin:0;font-size:22px}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}th{font-size:12px;color:var(--muted)}
.tag{font:11px monospace;color:var(--muted);margin-left:8px}.unverified{color:#e8c37a}label{display:flex;gap:8px;align-items:center;cursor:pointer}
nav a{margin-right:16px}"""


def esc(value):
    return html.escape('—' if value is None else str(value))


def page(title, body):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title><style>{STYLE}</style></head><body><main>'
            f'<nav><a href="index.html">Leaderboard</a><a href="disqualified.html">Disqualified</a></nav>{body}</main></body></html>\n')


def board_table(board, ident):
    if not board['groups']:
        return f'<p id="{ident}" class="muted">No verified scored attempts yet.</p>'
    rows = []
    for g in board['groups']:
        human = g['player_type'] == 'human'
        name = esc(g['player'] or ('Unknown person' if human else 'Unknown model')) + ('<span class="tag">human</span>' if human else '')
        rows.append(f"<tr><td>{g['rank']}</td><td>{name}</td><td>{'—' if human else esc(g['reasoning_effort'] or 'Unknown')}</td>"
                    f"<td>{g['average_score']:.2f}</td><td>{g['attempt_count']}</td><td>{g['best_score']}</td><td>{g['ascensions']}</td></tr>")
    return (f'<div class="scroll" id="{ident}"><table><thead><tr><th>Rank</th><th>Player</th><th>Reasoning effort</th>'
            '<th>Average score</th><th>Attempts</th><th>Best score</th><th>Ascensions</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def attempt_rows(rows, repo_url, reason=False):
    from .submission import player_label
    out = []
    for row in rows:
        m = row['manifest']; result = m['result'] or {}; character = m.get('character') or {}
        status = row['status']
        cells = [player_label(m), ' / '.join(str(character[k]) for k in ('role', 'race', 'gender', 'align') if k in character) or None,
                 m['outcome'], result.get('score'), result.get('turns'), m['key_count'], m.get('harness_version')]
        tds = ''.join(f'<td>{esc(c)}</td>' for c in cells)
        badge = (f'<td class="{status["status"]}" title="{esc(status["reason"])}">{esc(status["status"])}</td>' if not reason
                 else f'<td>{esc(row["disqualified"])}</td>')
        link = f'<td><a href="{esc(repo_url)}/attempts/{esc(m["attempt_id"])}">{esc(m["attempt_id"][:8])}</a></td>'
        out.append(f'<tr>{tds}{badge}{link}</tr>')
    return ''.join(out)


def build_site(root: Path, output: Path, statuses, repo_url, cname=None):
    rows = load_results(root, statuses)
    ranked = [summary(r['manifest']) for r in rows if r['status']['status'] == 'verified' and not r['disqualified']]
    boards = {'with_humans': leaderboard(ranked), 'models_only': leaderboard(ranked, humans=False)}
    listed = [r for r in rows if not r['disqualified']]
    disqualified = [r for r in rows if r['disqualified']]
    head = ('<tr><th>Player</th><th>Character</th><th>Outcome</th><th>Score</th><th>Turns</th><th>Keys</th>'
            '<th>Version</th><th>{}</th><th>Package</th></tr>')
    index = page('menacebench results', f'''<p class="eyebrow">MENACEBENCH</p><h1>NetHack results</h1>
<p class="muted">People and models playing the same pinned NetHack build. Every package is a seed plus the keys the game consumed;
CI replays each one in its recorded image before it is ranked. <a href="{esc(repo_url.rsplit('/tree/', 1)[0])}">Submissions repository</a></p>
<section><div class="head"><h2>Leaderboard</h2><label><input id="humans" type="checkbox" checked> Humans</label></div>
<p class="fine">Average score by model and reasoning effort, and by GitHub account for people. Only verified, completed attempts with a score count,
including quits and zero scores. Untick Humans to compare models only.</p>
{board_table(boards['with_humans'], 'with-humans')}{board_table(boards['models_only'], 'models-only')}</section>
<section><h2>Attempts</h2><p class="fine">Unverified attempts were recorded with a development build or an unpublished image, so CI could not
replay them; they are listed but not ranked.</p><div class="scroll"><table><thead>{head.format('Status')}</thead>
<tbody>{attempt_rows(listed, repo_url)}</tbody></table></div></section>
<script>const box=document.getElementById('humans');const show=()=>{{document.getElementById('with-humans').hidden=!box.checked;document.getElementById('models-only').hidden=box.checked}};box.onchange=show;show();</script>''')
    disqualified_page = page('Disqualified attempts', f'''<p class="eyebrow">MENACEBENCH</p><h1>Disqualified attempts</h1>
<p class="muted">These attempts broke the player rules. They stay in the repository and are excluded from rankings.</p>
<section><div class="scroll"><table><thead>{head.format('Reason')}</thead><tbody>{attempt_rows(disqualified, repo_url, reason=True)}</tbody></table></div>
{'' if disqualified else '<p class="muted">None.</p>'}</section>''')
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    (output/'index.html').write_text(index)
    (output/'disqualified.html').write_text(disqualified_page)
    data = {'leaderboard': boards, 'attempts': [{'manifest': r['manifest'], 'status': r['status'], 'disqualified': r['disqualified']} for r in rows]}
    (output/'data.json').write_text(json.dumps(data, indent=1, sort_keys=True))
    (output/'.nojekyll').write_text('')
    if cname: (output/'CNAME').write_text(cname+'\n')
    return data
