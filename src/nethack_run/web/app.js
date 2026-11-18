'use strict';
const $ = id => document.getElementById(id);
let csrf, permissions = {}, credentials = null, selected = null, busy = false;
let refreshEpoch = 0;
const scoreFormat = new Intl.NumberFormat(undefined, {maximumFractionDigits: 2});
let replayId = null, replayPlayer = null, replayEpoch = 0;
async function api(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options, headers: {'X-CSRF-Token': csrf || '', ...options.headers}});
  if (response.status === 401) { location.assign(path.endsWith('/submit') ? '/auth/github' : location.href); throw new Error('Please sign in again'); }
  if (!response.ok) throw new Error(await response.text());
  return response;
}
const name = a => a.character?.name || a.player.model || a.player.github_login || 'Character creation';
const label = a => a.state === 'finished' ? a.outcome : a.state;
const playerLabel = a => {
  if (a.player.player_type === 'human' && a.player.github_login) return `${a.player.github_login} (human)`;
  const player = a.player.model || a.player.player_type || 'Unknown player';
  return a.player.player_type === 'model' || a.player.model || a.player.reasoning_effort
    ? `${player} · Reasoning: ${a.player.reasoning_effort || 'unknown'}` : player;
};
const versionKey = a => a.harness_version || 'unknown';
const versionLabel = a => a.harness_version ? `v${a.harness_version}` : 'Unknown version';
const boardPage = location.pathname === '/leaderboard';
const params = new URLSearchParams(location.search);
selected = location.pathname.startsWith('/games/') ? location.pathname.slice(7) : null;
let visibleIds, latestGames = [], tab = params.get('tab');
const gameURL = id => '/games/' + encodeURIComponent(id);
const tabNames = {playing: 'Playing', paused: 'Paused / saved', finished: 'Finished'};
const groupOf = a => a.state === 'finished' ? 'finished' : ['paused', 'saved'].includes(a.state) ? 'paused' : 'playing';
function pageURL(changes) {
  const query = new URLSearchParams(params);
  for (const [key, value] of Object.entries(changes)) {
    if (value == null) query.delete(key); else query.set(key, value);
  }
  return location.pathname + (query.size ? '?' + query : '');
}
function updateVersions(versions) {
  const select = $('version-filter');
  const current = params.get('version') || 'all';
  const values = new Set(versions);
  if (current !== 'all') values.add(current);
  const sorted = [...values].sort((a, b) => b.localeCompare(a, undefined, {numeric: true}));
  if (select.dataset.versions === JSON.stringify(sorted)) return;
  select.dataset.versions = JSON.stringify(sorted);
  select.replaceChildren(new Option('All versions', 'all'), ...sorted.map(v => new Option(v, v)));
  select.value = current;
}
function setText(node, value) {
  const text = String(value ?? '—');
  if (node.textContent !== text) node.textContent = text;
}
function linked(value, id) {
  return id ? {text: value, href: gameURL(id)} : value;
}
function updateRows(body, records) {
  const existing = new Map([...body.rows].map(row => [row.dataset.key, row]));
  const wanted = new Set(records.map(record => record.key));
  for (const [key, row] of existing) if (!wanted.has(key)) row.remove();
  records.forEach(({key, cells}, index) => {
    const row = existing.get(key) || document.createElement('tr');
    row.dataset.key = key;
    cells.forEach((value, i) => {
      const cell = row.cells[i] || row.insertCell();
      if (value?.href) {
        let link = cell.firstElementChild;
        if (!link) { link = document.createElement('a'); cell.replaceChildren(link); }
        if (link.getAttribute('href') !== value.href) link.href = value.href;
        setText(link, value.text);
      } else setText(cell, value);
    });
    if (body.rows[index] !== row) body.insertBefore(row, body.rows[index] || null);
  });
}
function lists(all) {
  latestGames = all;
  updateVersions(all.map(versionKey));
  setText($('play'), all.some(a => a.permissions.play) ? 'Continue your game' : 'Play NetHack');
  const filtered = all.filter(a => ($('owner-filter').value !== 'mine' || a.is_mine)
    && ($('version-filter').value === 'all' || versionKey(a) === $('version-filter').value));
  const groups = {playing: [], paused: [], finished: []};
  for (const a of filtered) groups[groupOf(a)].push(a);
  if (!Object.hasOwn(groups, tab)) tab = groups.playing.length ? 'playing' : 'finished';
  for (const link of $('tabs').children) {
    const key = link.dataset.tab;
    setText(link, tabNames[key] + ' (' + groups[key].length + ')');
    link.href = pageURL({tab:key, page:null});
    if (key === tab) link.setAttribute('aria-current', 'page');
  }
  const finished = tab === 'finished';
  const sorted = groups[tab].sort((a,b) => (finished
    ? (b.result?.score ?? -Infinity) - (a.result?.score ?? -Infinity)
    : (b.created_at || 0) - (a.created_at || 0)) || a.id.localeCompare(b.id));
  if (!visibleIds) visibleIds = new Set(sorted.map(a => a.id));
  const rows = tab === 'playing' ? sorted.filter(a => visibleIds.has(a.id)) : sorted;
  const waiting = sorted.length - rows.length;
  $('new-games').hidden = !waiting;
  if (waiting) setText($('new-games'), 'Show ' + waiting + (waiting === 1 ? ' new game' : ' new games'));
  const pages = Math.max(1, Math.ceil(rows.length / 25));
  const page = Math.min(pages, Math.max(1, Number.parseInt(params.get('page'), 10) || 1));
  params.set('tab', tab);
  if (page > 1) params.set('page', page); else params.delete('page');
  const url = pageURL({});
  if (url !== location.pathname + location.search) history.replaceState(null, '', url);
  for (const [id, number] of [['prev', page-1], ['next', page+1]]) {
    $(id).hidden = number < 1 || number > pages;
    $(id).href = pageURL({page:number});
  }
  setText($('count'), 'Page ' + page + ' of ' + pages);
  $('empty').hidden = !!rows.length;
  if (!$('columns').children.length) {
    for (const title of finished ? ['Player','Character','Outcome','Score ↓','Moves',''] : ['Player','Character','State','Session','']) {
      const th = document.createElement('th'); th.textContent = title; $('columns').append(th);
    }
  }
  updateRows($('runs'), rows.slice((page-1)*25, page*25).map(a => ({
    key:a.id, cells:[linked(playerLabel(a), a.id), a.character?.name || 'Choosing character', label(a),
      ...(finished ? [a.result?.score?.toLocaleString(), a.result?.turns?.toLocaleString()] : [a.session]),
      linked(finished ? 'Replay' : 'Watch', a.id)]
  })));
}
function showLeaderboard(data) {
  updateVersions(data.versions);
  updateRows($('leaderboard-rows'), data.groups.map(g => ({
    key: JSON.stringify([g.player_type,g.model_key,g.reasoning_effort,g.github_id]),
    cells: [g.rank, g.player || 'Unknown', g.player_type === 'human' ? 'Human' : g.reasoning_effort || 'Unknown',
      linked(scoreFormat.format(g.best_score),g.best_score_id),
      linked(g.max_moves?.toLocaleString(),g.max_moves_id),g.attempt_count,g.ascensions]
  })));
  setText($('leaderboard-summary'), data.scored_attempts + ' scored games · ' + data.excluded_attempts + ' finished without a score');
}
function showDetails(a) {
  const signature = JSON.stringify([a.player,a.human_github_id,a.harness_version,a.owner_github_id,a.id]);
  if ($('attempt-details').dataset.signature === signature) return;
  $('attempt-details').dataset.signature = signature;
  $('attempt-details').replaceChildren();
  const human = a.player.player_type === 'human' && a.human_github_id != null;
  for (const [label, value] of human ? [
    ['Player', a.player.github_login || 'Unknown'], ['Player GitHub ID', a.human_github_id],
    ['Harness version', versionLabel(a)], ['Attempt ID', a.id],
  ] : [
    ['Run owner GitHub ID', a.owner_github_id ?? 'Unassigned'],
    ['Harness version', versionLabel(a)], ['Model', a.player.model || 'Unknown'],
    ['Agent tool', a.player.agent_tool || 'Unknown'], ['Tool version', a.player.tool_version || 'Unknown'],
    ['Reasoning effort', a.player.reasoning_effort || 'Unknown'], ['Attempt ID', a.id],
    ['Notes', a.player.notes || 'None'],
  ]) {
    const term = document.createElement('dt'), description = document.createElement('dd');
    term.textContent = label; description.textContent = value;
    $('attempt-details').append(term, description);
  }
}
const duration = seconds => `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
function showReplay(attempt) {
  const id = attempt?.state === 'finished' ? attempt.id : null;
  if (id === replayId) return;
  const epoch = ++replayEpoch;
  replayId = id;
  if (replayPlayer) { replayPlayer.dispose(); replayPlayer = null; }
  $('replay-player').replaceChildren();
  $('replay-section').hidden = !id;
  $('cast-download').hidden = $('replay-retry').hidden = true;
  if (!id) return;
  $('replay-status').textContent = 'Preparing recording from the recorded moves…';
  (async () => {
    try {
      while (epoch === replayEpoch && selected === id) {
        const response = await api(`/dashboard/attempts/${id}/replay`, {method: 'POST'});
        const result = await response.json();
        if (epoch !== replayEpoch || selected !== id) return;
        if (result.status === 'ready') {
          replayPlayer = AsciinemaPlayer.create(result.url, $('replay-player'), {preload: true, idleTimeLimit: 2, fit: 'width'});
          replayPlayer.addEventListener('error', () => { $('replay-status').textContent = 'The recording could not be loaded.'; $('replay-retry').hidden = false; });
          $('replay-status').textContent = 'Ready to replay.';
          $('cast-download').href = result.url + '?download=1';
          $('cast-download').hidden = false;
          return;
        }
        await new Promise(resolve => setTimeout(resolve, 1000));
      }
    } catch (e) {
      if (epoch === replayEpoch) { $('replay-status').textContent = e.message; $('replay-retry').hidden = false; }
    }
  })();
}
async function refresh() {
  if (busy) return;
  const epoch = ++refreshEpoch;
  try {
    if (boardPage) {
      const query = new URLSearchParams();
      for (const key of ['version','humans']) if (params.has(key)) query.set(key,params.get(key));
      showLeaderboard(await (await api('/dashboard/leaderboard?' + query)).json());
    } else if (!selected) {
      lists(await (await api('/dashboard/attempts')).json());
    } else {
      const id = selected, a = await (await api('/dashboard/attempts/' + id)).json();
      if (epoch !== refreshEpoch || selected !== id) return;
      $('detail').hidden = false;
      setText($('title'), playerLabel(a) + ' · ' + name(a));
      document.title = name(a) + ' | Menace Bench';
      setText($('state'), label(a));
      setText($('metadata'), `${a.character ? [a.character.role, a.character.race, a.character.gender, a.character.align].join(' / ') : 'Choosing a character'} · Session ${a.session} · ${duration(a.session_seconds)} · Last activity ${new Date(a.last_activity * 1000).toLocaleString()}`);
      setText($('screen'), a.screen || 'Waiting for the game screen…');
      $('screen').hidden = a.state === 'finished' || a.permissions.play;
      showReplay(a);
      showTerminal(a);
      permissions = a.permissions;
      $('manage').hidden = !(permissions.delete || permissions.assign_owner || permissions.recover);
      if (!permissions.recover && credentials) clearCredentials();
      $('recover-player').hidden = !permissions.recover;
      $('delete-run').hidden = !permissions.delete;
      $('owner-assignment').hidden = !permissions.assign_owner;
      $('pause').hidden = !permissions.manage || !['running', 'advancing'].includes(a.state);
      $('resume').hidden = !permissions.manage || !['paused', 'saved'].includes(a.state);
      $('download').hidden = $('package-note').hidden = !permissions.manage || a.state !== 'finished';
      $('submit').hidden = !permissions.submit;
      $('submission').hidden = !a.submission;
      if (a.submission) $('submission').href = a.submission.url;
      setText($('result'), a.result ? `Score ${a.result.score} · ${a.result.turns} turns · Depth ${a.result.maximum_depth}\n${a.result.cause_of_death}` : '');
      showDetails(a);
    }
    $('error').textContent = '';
  } catch (e) { if (epoch === refreshEpoch) $('error').textContent = e.message; }
}
// Live play. One WebSocket per selected game; the newest window takes control.
let terminal = null, socket = null, socketId = null, reconnectTimer = null;
function playStatus(text, takeControl = false) {
  $('play-status').textContent = text; $('take-control').hidden = !takeControl;
}
function closeSocket() {
  clearTimeout(reconnectTimer); reconnectTimer = null;
  if (socket) { const old = socket; socket = null; old.onclose = null; old.close(); }
}
function showTerminal(attempt) {
  const id = attempt?.permissions?.play ? attempt.id : null;
  $('play-section').hidden = !id;
  if (id === socketId) return;
  closeSocket(); socketId = id;
  if (!id) return;
  if (!terminal) {
    terminal = new Terminal({cols: 80, rows: 24, cursorBlink: true, convertEol: false, scrollback: 0,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 15,
      theme: {background: '#080e0a', foreground: '#d5e6c9', cursor: '#b6d78b'}});
    terminal.open($('terminal'));
    // Keys are only accepted from the window in control; the server enforces it too.
    terminal.onData(data => { if (socket?.readyState === WebSocket.OPEN && socket.controlling) socket.send(data); });
  }
  terminal.reset();
  connect(id);
}
function connect(id) {
  closeSocket();
  if (socketId !== id) return;
  playStatus('Connecting…');
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/dashboard/attempts/${id}/tty?mode=play`);
  ws.binaryType = 'arraybuffer'; ws.controlling = false; socket = ws;
  ws.onmessage = event => {
    if (typeof event.data !== 'string') { terminal.write(new Uint8Array(event.data)); return; }
    const message = JSON.parse(event.data);
    ws.controlling = message.controlling;
    if (message.error) playStatus(message.error);
    else if (message.taken_over) playStatus('This game is open in another window.', true);
    else if (message.status?.state === 'finished') playStatus('Game over. The recording appears below once it is prepared.');
    else if (message.controlling) playStatus(message.status?.state === 'paused' ? 'Paused. Press a key to continue.' : 'Playing.');
  };
  ws.onopen = () => terminal.focus();
  ws.onclose = event => {
    if (socket !== ws) return;
    socket = null;
    if (event.code === 4001) playStatus('This game is open in another window.', true);
    else if (event.code === 1008) playStatus('Signed out or access changed. Reload to continue.');
    else if (event.code === 1013) { playStatus('Connection was too slow. Reconnecting…'); reconnectTimer = setTimeout(() => connect(id), 2000); }
    else { playStatus('Disconnected. Reconnecting…'); reconnectTimer = setTimeout(() => connect(id), 2000); }
  };
}
$('take-control').onclick = () => { if (socketId) connect(socketId); };
$('play').onclick = async () => {
  if (busy) return;
  busy = true;
  try {
    const game = await (await api('/dashboard/play', {method: 'POST'})).json();
    location.assign(gameURL(game.id));
  } catch (e) { $('error').textContent = e.message; }
  finally { busy = false; }
  await refresh();
  if (terminal && socketId) terminal.focus();
};

async function action(kind) {
  if (!permissions.manage || !selected || busy) return;
  busy = true;
  const id = selected;
  try {
    const response = await api(`/dashboard/attempts/${id}/${kind}`, {method: 'POST'});
    if (kind === 'package') {
      const url = URL.createObjectURL(await response.blob()), link = document.createElement('a');
      link.href = url; link.download = `${id}.tar`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    $('error').textContent = '';
  } catch (e) { $('error').textContent = e.message; }
  finally { busy = false; }
}
function clearCredentials() {
  credentials = null; $('credential-json').value = ''; $('recovered-access').hidden = true;
}
async function recoverPlayer() {
  if (busy || !selected || !permissions.recover) return;
  if (!confirm('Replace the player token for this run? The previous token will stop working.')) return;
  const id = selected; busy = true; clearCredentials();
  try {
    credentials = await (await api(`/dashboard/attempts/${id}/recover-player`, {method:'POST'})).json();
    credentials.base_url = location.origin;
    $('credential-json').value = JSON.stringify(credentials, null, 2);
    $('recovered-access').hidden = false; $('error').textContent = '';
  } catch (e) { $('error').textContent = e.message; }
  finally { busy = false; }
}
$('delete-run').onclick = async () => {
  if (busy || !selected || !permissions.delete) return;
  const id = selected;
  if (!confirm(`Permanently delete run ${id}? Its game, trace, and replay will be removed and it will leave the leaderboard.`)) return;
  busy = true; clearCredentials();
  try {
    await api(`/dashboard/attempts/${id}/delete`, {method:'POST'});
    location.assign('/');
    $('error').textContent = '';
  } catch (e) { $('error').textContent = e.message; }
  finally { busy = false; }
  refresh();
};
$('recover-player').onclick = recoverPlayer;
$('hide-credential').onclick = clearCredentials;
$('save-credential').onclick = () => {
  if (!credentials) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(credentials, null, 2)], {type:'application/json'}));
  const link = document.createElement('a'); link.href = url; link.download = `${credentials.id}-player.json`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$('owner-assignment').onsubmit = async event => {
  event.preventDefault();
  if (busy || !selected || !permissions.assign_owner) return;
  const input = $('owner-github-id').value.trim(), id = selected;
  const owner = input ? Number(input) : null;
  if (input && (!/^\d+$/.test(input) || !Number.isSafeInteger(owner) || owner <= 0)) { $('error').textContent = 'Enter a positive numeric GitHub ID.'; return; }
  if (!confirm('Change the run owner and revoke its existing player and owner tokens?')) return;
  busy = true; clearCredentials();
  try {
    await api(`/dashboard/attempts/${id}/owner`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({owner_github_id:owner})});
    $('owner-github-id').value = ''; $('error').textContent = '';
  } catch (e) { $('error').textContent = e.message; }
  finally { busy = false; }
  refresh();
};
$('submit').onclick = () => {
  if (confirm('Open a pull request adding this run to the public results? This finalizes the package, including its usage report.')) action('submit');
};
$('pause').onclick = () => action('pause'); $('resume').onclick = () => action('resume'); $('download').onclick = () => action('package');
$('version-filter').onchange = e => location.assign(pageURL({version:e.target.value === 'all' ? null : e.target.value, page:null}));
$('owner-filter').value = params.get('scope') === 'mine' ? 'mine' : 'all';
$('owner-filter').onchange = e => location.assign(pageURL({scope:e.target.value, page:null}));
$('humans-filter').checked = params.get('humans') !== '0';
$('humans-filter').onchange = e => location.assign(pageURL({humans:e.target.checked ? null : '0'}));
$('new-games').onclick = () => { visibleIds = null; lists(latestGames); };
$('runs').onclick = e => {
  if (e.target.closest('a') || window.getSelection().toString()) return;
  const row = e.target.closest('tr');
  if (row) location.assign(gameURL(row.dataset.key));
};
$('replay-retry').onclick = () => { replayId = null; refresh(); };
$('logout').onclick = async () => { try { await api('/auth/logout', {method: 'POST'}); location.assign('/'); } catch (e) { $('error').textContent = e.message; } };
(async () => {
  try {
    const me = await (await api('/dashboard/me')).json();
    csrf = me.csrf;
    setText($('account'), me.login);
    setText($('play'), me.has_game ? 'Continue your game' : 'Play NetHack');
    $(boardPage ? 'board-nav' : 'games-nav').setAttribute('aria-current','page');
    $('listing').hidden = !!selected;
    $('board').hidden = $('people').hidden = !boardPage;
    $('games').hidden = $('scope').hidden = $('tabs').hidden = boardPage;
    setText($('heading'), boardPage ? 'Leaderboard' : 'Games');
    document.title = $('heading').textContent + ' | Menace Bench';
    async function poll() {
      if (!document.hidden) await refresh();
      setTimeout(poll, selected ? 1000 : 5000);
    }
    await poll();
  } catch (e) { setText($('error'), e.message); }
})();
