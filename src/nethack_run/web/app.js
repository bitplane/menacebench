'use strict';
const $ = id => document.getElementById(id);
let csrf, isAdmin = false, permissions = {}, credentials = null, selected = null, busy = false, lastList = '';
let refreshEpoch = 0, lastLeaderboard = '';
const scoreFormat = new Intl.NumberFormat(undefined, {maximumFractionDigits: 2});
let replayId = null, replayPlayer = null, replayEpoch = 0;
async function api(path, options = {}) {
  const response = await fetch(path, {cache: 'no-store', ...options, headers: {'X-CSRF-Token': csrf || '', ...options.headers}});
  if (response.status === 401) { location.assign(path.endsWith('/submit') ? '/auth/github' : '/'); throw new Error('Please sign in again'); }
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
function updateVersions(all) {
  const select = $('version-filter'), previous = select.value;
  const versions = [...new Set(all.map(versionKey))].sort((a, b) => a === 'unknown' ? 1 : b === 'unknown' ? -1 : b.localeCompare(a, undefined, {numeric: true}));
  if (previous !== 'all' && !versions.includes(previous)) versions.push(previous);
  const signature = JSON.stringify(versions);
  if (select.dataset.versions === signature) return;
  select.dataset.versions = signature;
  select.replaceChildren(new Option('All versions', 'all'), ...versions.map(v => new Option(v === 'unknown' ? 'Unknown version' : `v${v}`, v)));
  select.value = previous;
}
function showLeaderboard(data) {
  const signature = JSON.stringify(data);
  if (signature === lastLeaderboard) return;
  lastLeaderboard = signature;
  $('leaderboard-rows').replaceChildren();
  $('leaderboard-table').hidden = data.groups.length === 0;
  $('leaderboard-summary').textContent = data.groups.length
    ? `${data.scored_attempts} scored attempts · ${data.groups.length} groups · ${data.excluded_attempts} completed attempts without scores`
    : `No scored attempts for this version. ${data.excluded_attempts} completed attempts have no recorded score.`;
  for (const group of data.groups) {
    const row = document.createElement('tr'), human = group.player_type === 'human';
    for (const value of [group.rank, human ? group.player || 'Unknown person' : group.model || 'Unknown model',
                         human ? '—' : group.reasoning_effort || 'Unknown',
                         scoreFormat.format(group.average_score), group.attempt_count,
                         scoreFormat.format(group.best_score), group.ascensions]) {
      const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
    }
    if (human) { const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = 'human'; row.children[1].append(tag); }
    $('leaderboard-rows').append(row);
  }
}
function showDetails(a) {
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
    const all = await (await api('/dashboard/attempts')).json();
    if (epoch !== refreshEpoch) return;
    updateVersions(all);
    const filter = $('filter').value, version = $('version-filter').value;
    const query = new URLSearchParams();
    if (version !== 'all') query.set('version', version);
    if (!$('humans-filter').checked) query.set('humans', '0');
    const board = await (await api('/dashboard/leaderboard' + (query.toString() ? '?' + query : ''))).json();
    if (epoch !== refreshEpoch) return;
    showLeaderboard(board);
    const runs = all.filter(a => ($('owner-filter').value !== 'mine' || a.is_mine) && (version === 'all' || versionKey(a) === version) &&
      (filter === 'all' || (filter === 'active' ? a.state !== 'finished' : a.state === 'finished')));
    $('count').textContent = `${runs.length} ${runs.length === 1 ? 'attempt' : 'attempts'}`;
    $('play').textContent = all.some(a => a.permissions.play) ? 'Continue your game' : 'Play NetHack';
    if (!runs.some(a => a.id === selected)) selected = runs[0]?.id || null;
    if (credentials && credentials.id !== selected) clearCredentials();
    const signature = JSON.stringify(runs.map(a => [a.id, name(a), label(a), a.session, a.key_count, a.player, a.harness_version, a.owner_github_id, a.permissions])) + selected;
    if (signature !== lastList) {
      lastList = signature;
      $('runs').replaceChildren();
      if (!runs.length) { const p = document.createElement('p'); p.className = 'empty'; p.textContent = all.length ? 'No attempts match these filters.' : 'No attempts here yet. Games created through the API will appear automatically.'; $('runs').append(p); }
      for (const a of runs) {
        const b = document.createElement('button'); b.className = 'run' + (a.id === selected ? ' selected' : ''); b.setAttribute('aria-pressed', String(a.id === selected));
        const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = label(a);
        const title = document.createElement('strong'); title.textContent = name(a);
        const sub = document.createElement('small'); sub.textContent = `${versionLabel(a)} · ${playerLabel(a)} · Session ${a.session} · ${a.key_count} keys`;
        b.append(badge, title, sub); b.onclick = () => { if (busy) return; clearCredentials(); selected = a.id; refresh(); }; $('runs').append(b);
      }
    }
    $('detail').hidden = !selected;
    if (!selected) { showReplay(null); showTerminal(null); }
    if (selected) {
      const id = selected, a = await (await api('/dashboard/attempts/' + id)).json();
      if (epoch !== refreshEpoch || selected !== id) return;
      $('title').textContent = name(a); $('state').textContent = label(a);
      $('metadata').textContent = `${playerLabel(a)} · ${a.character ? [a.character.role, a.character.race, a.character.gender, a.character.align].join(' / ') : 'Choosing a character'} · Session ${a.session} · ${duration(a.session_seconds)} · Last activity ${new Date(a.last_activity * 1000).toLocaleString()}`;
      $('screen').textContent = a.screen || 'This session has ended. Its consumed keys are preserved in the trace.';
      $('screen').hidden = a.state === 'finished' || a.permissions.play;
      showReplay(a);
      showTerminal(a);
      permissions = a.permissions;
      if (!permissions.recover) clearCredentials();
      $('recover-player').hidden = !permissions.recover;
      $('delete-run').hidden = !permissions.delete;
      $('owner-assignment').hidden = !permissions.assign_owner;
      $('pause').hidden = !permissions.manage || !['running', 'advancing'].includes(a.state);
      $('resume').hidden = !permissions.manage || !['paused', 'saved'].includes(a.state);
      $('download').hidden = $('package-note').hidden = !permissions.manage || a.state !== 'finished';
      $('submit').hidden = !permissions.submit;
      $('submission').hidden = !a.submission;
      if (a.submission) $('submission').href = a.submission.url;
      $('result').textContent = a.result ? `Score ${a.result.score} · ${a.result.turns} turns · Depth ${a.result.maximum_depth}\n${a.result.cause_of_death}` : '';
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
    $('filter').value = 'all'; $('owner-filter').value = 'all'; $('version-filter').value = 'all';
    selected = game.id; lastList = ''; socketId = null; $('error').textContent = '';
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
    selected = null; lastList = ''; showReplay(null); $('detail').hidden = true;
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
$('filter').onchange = refresh;
$('version-filter').onchange = refresh;
$('owner-filter').onchange = refresh;
$('humans-filter').onchange = refresh;
$('replay-retry').onclick = () => { replayId = null; refresh(); };
$('logout').onclick = async () => { try { await api('/auth/logout', {method: 'POST'}); location.assign('/'); } catch (e) { $('error').textContent = e.message; } };
(async () => { try { const me = await (await api('/dashboard/me')).json(); csrf = me.csrf; isAdmin = me.role === 'admin'; $('account').textContent = `${me.login} · GitHub ID ${me.id}${isAdmin ? ' · Admin' : ''}`; $('viewer-note').hidden = isAdmin; await refresh(); setInterval(refresh, 1000); } catch (e) { $('error').textContent = e.message; } })();
