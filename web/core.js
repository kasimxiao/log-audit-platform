// 公共：Cognito 登录、API 调用、UI 工具
const CFG = window.LA_CONFIG;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const today = (d = 0) => { const t = new Date(Date.now() + d * 864e5); return t.toISOString().slice(0, 10); };

// ---------------------------------------------------------------- Cognito
async function cognito(target, body) {
  const r = await fetch(`https://cognito-idp.${CFG.region}.amazonaws.com/`, { method: 'POST',
    headers: { 'Content-Type': 'application/x-amz-json-1.1', 'X-Amz-Target': `AWSCognitoIdentityProviderService.${target}` },
    body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.message || j.__type || '登录失败');
  return j;
}
const Auth = {
  get tok() { return JSON.parse(sessionStorage.getItem('la_tok') || 'null'); },
  set tok(v) { v ? sessionStorage.setItem('la_tok', JSON.stringify(v)) : sessionStorage.removeItem('la_tok'); },
  save(res) { const a = res.AuthenticationResult; const old = this.tok || {};
    this.tok = { id: a.IdToken, refresh: a.RefreshToken || old.refresh, exp: Date.now() + (a.ExpiresIn - 120) * 1000 }; },
  async login(u, p) {
    const r = await cognito('InitiateAuth', { AuthFlow: 'USER_PASSWORD_AUTH', ClientId: CFG.clientId, AuthParameters: { USERNAME: u, PASSWORD: p } });
    if (r.ChallengeName === 'NEW_PASSWORD_REQUIRED') return { challenge: r.Session, username: u };
    this.save(r); return {};
  },
  async newPassword(session, u, p) {
    const r = await cognito('RespondToAuthChallenge', { ChallengeName: 'NEW_PASSWORD_REQUIRED', ClientId: CFG.clientId, Session: session,
      ChallengeResponses: { USERNAME: u, NEW_PASSWORD: p } });
    this.save(r);
  },
  async idToken() {
    const t = this.tok; if (!t) return null;
    if (Date.now() < t.exp) return t.id;
    try { const r = await cognito('InitiateAuth', { AuthFlow: 'REFRESH_TOKEN_AUTH', ClientId: CFG.clientId, AuthParameters: { REFRESH_TOKEN: t.refresh } });
      this.save(r); return this.tok.id; } catch { this.tok = null; return null; }
  },
  logout() { this.tok = null; location.reload(); },
};

// ---------------------------------------------------------------- API
async function api(method, path, body) {
  const tok = await Auth.idToken();
  if (!tok) { location.reload(); throw new Error('未登录'); }
  const r = await fetch(path, { method, headers: { Authorization: tok, 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  let j = {}; try { j = await r.json(); } catch { }
  if (!r.ok) throw new Error(j.message || `HTTP ${r.status}`);
  return j;
}
const GET = p => api('GET', p), POST = (p, b = {}) => api('POST', p, b), PUT = (p, b = {}) => api('PUT', p, b), DEL = p => api('DELETE', p);

async function pollQuery(qid, { geo = false, onTick } = {}) {
  for (let i = 0; i < 400; i++) {
    const r = await GET(`/api/query/${qid}${geo ? '?geo=1' : ''}`);
    if (r.state === 'SUCCEEDED') return r;
    if (r.state === 'FAILED' || r.state === 'CANCELLED') throw new Error('查询失败：' + (r.reason || r.state));
    onTick && onTick(r); await sleep(i < 5 ? 800 : 1500);
  }
  throw new Error('查询超时');
}
// 启动查询并等待：start 返回 {query_id, sql}
async function runQuery(startPromise, opts = {}) { const s = await startPromise; const r = await pollQuery(s.query_id, opts); r.sql = s.sql; return r; }

// ---------------------------------------------------------------- UI
function toast(msg, err = false) {
  const d = document.createElement('div'); d.className = 'toast' + (err ? ' err' : ''); d.textContent = msg;
  document.body.appendChild(d); setTimeout(() => d.remove(), err ? 6000 : 3000);
}
async function guard(btn, fn) {
  const old = btn ? btn.innerHTML : ''; if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> ' + old; }
  try { return await fn(); } catch (e) { toast(e.message, true); console.error(e); } finally { if (btn) { btn.disabled = false; btn.innerHTML = old; } }
}
function modal(title, html, onMount) {
  const bg = document.createElement('div'); bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal"><div class="row"><h3>${esc(title)}</h3><span class="spacer"></span><button class="sm" data-x>关闭</button></div>${html}</div>`;
  document.body.appendChild(bg);
  const close = () => bg.remove();
  bg.addEventListener('click', e => { if (e.target === bg || e.target.dataset.x !== undefined) close(); });
  onMount && onMount(bg.querySelector('.modal'), close);
  return close;
}
function table(cols, rows, { render = {}, empty = '无数据', cls = '' } = {}) {
  if (!rows || !rows.length) return `<div class="muted small">${empty}</div>`;
  return `<div class="tw"><table class="t ${cls}"><thead><tr>${cols.map(c => `<th>${esc(c.label ?? c)}</th>`).join('')}</tr></thead><tbody>` +
    rows.map((r, i) => `<tr>${cols.map((c, j) => { const k = c.key ?? j; const v = Array.isArray(r) ? r[k] : r[k];
      return `<td>${render[k] ? render[k](v, r, i) : esc(v)}</td>`; }).join('')}</tr>`).join('') + '</tbody></table></div>';
}
// Athena 结果表 + 导出 CSV
function resultTable(res, opts = {}) {
  const id = 'rt' + Math.random().toString(36).slice(2, 8);
  window._rt = window._rt || {}; window._rt[id] = res;
  const meta = `<div class="row small muted" style="margin:6px 0">${res.rows.length} 行${res.truncated ? '（已截断）' : ''} · 扫描 ${fmtBytes(res.bytes_scanned)} · ${res.elapsed_ms} ms
    <span class="spacer"></span><button class="sm" onclick="exportCsv('${id}')">导出 CSV</button>
    ${res.sql ? `<button class="sm" onclick="this.closest('.qres').querySelector('.cmd').hidden^=1">隐藏/显示 SQL</button>` : ''}</div>`;
  const sql = res.sql ? `<div class="cmd"><div class="small muted">执行的 SQL · Athena 工作组 logaudit · 查询 ID ${esc(res.query_id || '')}</div><pre class="sql">${esc(res.sql)}</pre></div>` : '';
  return `<div class="qres">${sql}${meta}${table(res.columns, res.rows, opts)}</div>`;
}
function exportCsv(id) {
  const r = window._rt[id]; const q = v => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const csv = '﻿' + [r.columns.map(q).join(','), ...r.rows.map(x => x.map(q).join(','))].join('\n');
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' })); a.download = `query-${Date.now()}.csv`; a.click();
}
const fmtBytes = b => { b = Number(b || 0); return b < 1024 ? b + ' B' : b < 1048576 ? (b / 1024).toFixed(1) + ' KB' : b < 1073741824 ? (b / 1048576).toFixed(1) + ' MB' : (b / 1073741824).toFixed(2) + ' GB'; };
const fmtNum = n => n == null ? '-' : Number(n).toLocaleString('zh-CN');
const badge = (txt, kind) => `<span class="badge b-${kind}">${esc(txt)}</span>`;

// 轻量 Markdown（标题/粗体/列表/表格/代码块）
function md(s) {
  const blocks = []; s = String(s || '').replace(/```(\w*)\n([\s\S]*?)```/g, (_, l, c) => { blocks.push(`<pre class="${l === 'sql' ? 'sql' : ''}">${esc(c.trim())}</pre>`); return `\u0000${blocks.length - 1}\u0000`; });
  const lines = esc(s).split('\n'); const out = []; let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const hdr = l.split('|').slice(1, -1); i += 2; const rows = [];
      while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) { rows.push(lines[i].split('|').slice(1, -1)); i++; }
      out.push(`<table><tr>${hdr.map(h => `<th>${h.trim()}</th>`).join('')}</tr>${rows.map(r => `<tr>${r.map(c => `<td>${c.trim()}</td>`).join('')}</tr>`).join('')}</table>`); continue;
    }
    if (/^#{1,4} /.test(l)) out.push(`<b>${l.replace(/^#+ /, '')}</b>`);
    else if (/^\s*[-*] /.test(l)) out.push('• ' + l.replace(/^\s*[-*] /, ''));
    else out.push(l);
    i++;
  }
  return out.join('<br>').replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/(<br>)?\u0000(\d+)\u0000(<br>)?/g, (_, a, n) => blocks[n]);
}

// 图表：参考调色板（dataviz skill palette.md，已验证）
const SERIES = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'];
const STATUS = { good: '#0ca30c', warning: '#fab219', serious: '#ec835a', critical: '#d03b3b' };
const charts = {};
function chart(id, cfg) {
  if (charts[id]) charts[id].destroy();
  const el = document.getElementById(id); if (!el) return;
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily; Chart.defaults.color = '#52514e';
  cfg.options = Object.assign({ responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
    plugins: { legend: { position: 'top', align: 'start', labels: { boxWidth: 10, boxHeight: 10 } } },
    scales: cfg.type === 'doughnut' ? undefined : { x: { grid: { display: false }, ticks: { maxRotation: 0, autoSkip: true } },
      y: { beginAtZero: true, grid: { color: '#ececea' }, border: { display: false } } } }, cfg.options || {});
  charts[id] = new Chart(el, cfg);
}
