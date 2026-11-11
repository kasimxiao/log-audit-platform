// 主机日志查询（实时 / 历史）+ 审计场景
let LIVE = { region: null, iid: null };
function goLive(region, iid) { LIVE = { region, iid }; location.hash = '#hostlogs'; }

async function onlineHosts(region) {
  const r = await GET(`/api/hosts?region=${region}`); ST.hosts = r.hosts; return r.hosts;
}
function hostOptions(hosts, sel, onlyOnline) {
  return hosts.filter(h => !onlyOnline || h.ssm === 'online').map(h => `<option value="${h.instance_id}" ${h.instance_id === sel ? 'selected' : ''}>${esc(h.name || h.instance_id)} (${h.instance_id})</option>`).join('');
}

PAGES.hostlogs = {
  title: '主机日志查询', async render(el) {
    const tab = PAGES.hostlogs.tab || 'live';
    el.innerHTML = `<h1>主机日志查询</h1><p class="sub">实时查询：经 SSM 直接在主机上读取（看当天最新、未同步的日志）；历史检索：查询已同步到 S3 的日志（Athena，跨主机、跨天）。</p>
      <div class="tabs"><a data-t="live" class="${tab === 'live' ? 'on' : ''}">实时查询（主机）</a><a data-t="hist" class="${tab === 'hist' ? 'on' : ''}">历史检索（S3）</a></div><div id="qBody"></div>`;
    $$('.tabs a', el).forEach(a => a.onclick = () => { PAGES.hostlogs.tab = a.dataset.t; PAGES.hostlogs.render(el); });
    tab === 'live' ? liveTab($('#qBody')) : histTab($('#qBody'));
  }
};

async function liveTab(el) {
  const region = LIVE.region || ST.region;
  el.innerHTML = `<div class="card"><div class="row"><label>区域 <input id="lRegion" value="${esc(region)}" size="14"></label>
    <label>主机 <select id="lHost"><option>加载中…</option></select></label>
    <label>目录 <input id="lDir" value="/var/log/" size="24"></label><button id="lLs" class="primary">浏览目录</button></div>
    <div class="small muted" id="lRoots" style="margin-top:6px"></div></div>
    <div class="grid g2"><div class="card"><h3>文件</h3><div id="lFiles" class="muted small">选择主机后浏览目录</div></div>
    <div class="card"><h3>读取</h3>
      <div class="row"><label>文件 <input id="lPath" size="34" placeholder="/var/log/secure"></label></div>
      <div class="row" style="margin-top:8px"><label>方式 <select id="lMode"><option value="tail">tail 末尾 N 行</option><option value="grep">grep 正则检索</option>
        <option value="last">last 解析 wtmp/btmp</option><option value="lastlog">lastlog 快照</option><option value="journal">journalctl（单元名）</option></select></label>
        <label>行数 <input id="lN" type="number" value="200" style="width:80px"></label></div>
      <div class="row" style="margin-top:8px"><label>正则 <input id="lPat" size="30" placeholder="Failed password|Invalid user"></label>
        <label><input type="checkbox" id="lIc"> 忽略大小写</label><label>上下文 <input id="lCtx" type="number" value="0" style="width:50px"></label></div>
      <div class="row" style="margin-top:8px"><label>预置 <select id="lPreset"><option value="">— 审计场景正则 —</option></select></label><span class="spacer"></span><button id="lGo" class="primary">执行</button></div>
    </div></div>
    <div class="card"><div class="row"><h3 style="margin:0">输出</h3><span class="spacer"></span><span id="lMeta" class="small muted"></span></div><div id="lOut" style="margin-top:8px"></div></div>`;
  GET('/api/live/roots').then(r => $('#lRoots').textContent = `允许访问的根目录：${r.roots.join('、')}（管理员可在"数据源与设置"修改）`);
  GET('/api/scenarios').then(r => $('#lPreset').innerHTML += r.scenarios.filter(s => s.grep).map(s => `<option value="${esc(s.grep)}">${esc(s.name)}</option>`).join(''));
  $('#lPreset').onchange = () => { if ($('#lPreset').value) { $('#lPat').value = $('#lPreset').value; $('#lMode').value = 'grep'; } };
  const loadH = async () => { const hs = await onlineHosts($('#lRegion').value.trim()); $('#lHost').innerHTML = hostOptions(hs, LIVE.iid, true) || '<option value="">（该区域无 SSM 在线主机）</option>'; };
  $('#lRegion').onchange = loadH; await guard(null, loadH);
  $('#lLs').onclick = e => guard(e.target, async () => {
    const r = await liveRun({ mode: 'ls', path: $('#lDir').value.trim() });
    if (r.error) { $('#lFiles').innerHTML = cmdBlock(r) + `<span class="muted">${esc(r.error)}</span>`; return; }
    $('#lFiles').innerHTML = cmdBlock(r) + table([{ key: 'path', label: '路径' }, { key: 'size', label: '大小' }, { key: 'mtime', label: '修改时间' }], r.entries.filter(x => x.type === 'file'),
      { cls: 'files', render: { path: v => `<span class="f" data-p="${esc(v)}">${esc(v)}</span>`, size: v => fmtBytes(v) } });
    $$('#lFiles .f').forEach(s => s.parentElement.onclick = () => { $('#lPath').value = s.dataset.p;
      const b = s.dataset.p.split('/').pop(); $('#lMode').value = /^(wtmp|btmp)/.test(b) ? 'last' : /^lastlog/.test(b) ? 'lastlog' : $('#lMode').value === 'last' ? 'tail' : $('#lMode').value; });
  });
  $('#lGo').onclick = e => guard(e.target, async () => {
    const mode = $('#lMode').value;
    const r = await liveRun({ mode, path: $('#lPath').value.trim(), pattern: $('#lPat').value, lines: +$('#lN').value, ignore_case: $('#lIc').checked, context: +$('#lCtx').value });
    $('#lMeta').textContent = r.error ? '' : `${r.truncated ? '已截断 · ' : ''}${fmtBytes(r.bytes)}`;
    $('#lOut').innerHTML = cmdBlock(r) + (r.error ? `<div class="muted">错误：${esc(r.error)}</div>` : r.nomatch ? '<div class="muted">无匹配</div>' :
      `<pre>${highlight(esc(r.text), mode === 'grep' ? $('#lPat').value : '')}</pre>`);
  });
}
// 展示主机上执行的命令（SSM Run Command）
function cmdBlock(r) {
  if (!r || !r.command) return '';
  const host = (ST.hosts.find(h => h.instance_id === (r.instance_id || $('#lHost')?.value)) || {}).name || r.instance_id || $('#lHost')?.value || '';
  return `<div class="cmd"><div class="small muted">执行的命令 · SSM AWS-RunShellScript（root）· ${esc(host)} · ${esc(r.command_id || '')} · ${esc(r.status || '')}</div>
    <pre class="cmdline">$ ${esc(r.command)}</pre>${r.script ? `<details class="small"><summary>实际下发的完整脚本（路径/正则经 base64 传入，不做 eval）</summary><pre class="sql">${esc(r.script)}</pre></details>` : ''}</div>`;
}
function highlight(html, pat) { if (!pat) return html; try { return html.replace(new RegExp(`(${pat})`, 'g' + ($('#lIc')?.checked ? 'i' : '')), '<span class="hl">$1</span>'); } catch { return html; } }
async function liveRun(body) {
  const iid = $('#lHost').value; if (!iid) throw new Error('请选择 SSM 在线的主机');
  const s = await POST('/api/live', { region: $('#lRegion').value.trim(), instance_id: iid, ...body });
  const tgt = body.mode === 'ls' ? '#lFiles' : '#lOut';
  $(tgt).innerHTML = cmdBlock({ ...s, status: 'InProgress' }) + '<span class="spin"></span> 主机执行中…';
  for (let i = 0; i < 90; i++) { await sleep(i < 3 ? 700 : 1200); const r = await GET(`/api/live/${s.command_id}`);
    if (!['Pending', 'InProgress', 'Delayed'].includes(r.status)) { if (r.status !== 'Success' && !r.text && !r.entries) r.error = r.error || r.status; return r; } }
  throw new Error('主机未在 2 分钟内返回');
}

function hostFilterHtml() {
  return `<label>开始 <input type="date" id="fFrom" value="${today(-7)}"></label><label>结束 <input type="date" id="fTo" value="${today()}"></label>
    <label>主机 <select id="fHosts" multiple size="1" style="min-width:200px"></select></label>
    <label>日志类型 <select id="fType"><option value="">全部</option>${LOGTYPES.map(t => `<option>${t}</option>`).join('')}</select></label>`;
}
async function fillHostFilter() {
  try { const hs = ST.hosts.length ? ST.hosts : await onlineHosts(ST.region);
    $('#fHosts').innerHTML = hostOptions(hs, null, false); $('#fHosts').size = Math.min(4, Math.max(1, hs.length)); } catch { }
}
function hostFilter() {
  return { date_from: $('#fFrom').value, date_to: $('#fTo').value, instance_ids: [...$('#fHosts').selectedOptions].map(o => o.value),
    logtypes: $('#fType')?.value ? [$('#fType').value] : [] };
}

function histTab(el) {
  el.innerHTML = `<div class="card"><div class="row">${hostFilterHtml()}</div>
    <div class="row" style="margin-top:8px"><label>关键字 <input id="fKw" size="34" placeholder="Accepted password"></label><label><input type="checkbox" id="fRe"> 正则</label>
      <label>条数 <input id="fLim" type="number" value="500" style="width:80px"></label><span class="spacer"></span><button id="fGo" class="primary">检索</button></div>
    <div class="small muted" style="margin-top:6px">按同步日期（dt）分区；主机多选按住 ⌘/Ctrl。不选主机 = 全部主机。</div></div><div class="card" id="fOut"></div>`;
  fillHostFilter();
  $('#fGo').onclick = e => guard(e.target, async () => {
    $('#fOut').innerHTML = '<span class="spin"></span> 查询中…';
    const r = await runQuery(POST('/api/hostlogs/query', { ...hostFilter(), keyword: $('#fKw').value, regex: $('#fRe').checked, limit: +$('#fLim').value }));
    $('#fOut').innerHTML = resultTable(r, { render: { 5: v => `<code>${esc(v)}</code>` } });
  });
}

// ---------------------------------------------------------------- 审计场景
const LEVEL = { critical: ['严重', 'crit'], high: ['高', 'serious'], medium: ['中', 'warn'], low: ['低', 'info'], info: ['信息', 'info'] };
PAGES.scenarios = {
  title: '审计场景', async render(el) {
    el.innerHTML = `<h1>审计场景</h1><p class="sub">基于已同步到 S3 的主机日志，一键执行预置审计分析。</p>
      <div class="card"><div class="row">${hostFilterHtml().replace(/<label>日志类型[\s\S]*<\/label>$/, '')}</div></div>
      <div class="grid g3" id="scList"></div><div class="card" id="scOut" hidden></div>`;
    fillHostFilter();
    const r = await GET('/api/scenarios');
    $('#scList').innerHTML = r.scenarios.map(s => `<div class="card" style="margin:0"><div class="row"><b>${esc(s.name)}</b><span class="spacer"></span>${badge(...LEVEL[s.level])}</div>
      <p class="small muted" style="min-height:38px">${esc(s.desc.replace('{threshold}', s.params?.threshold ?? ''))}</p>
      <div class="row">${s.params?.threshold ? `<label class="small">阈值 <input class="scTh" data-id="${s.id}" type="number" value="${s.params.threshold}" style="width:60px"></label>` : ''}
      <span class="spacer"></span><button class="sm primary" data-run="${s.id}">执行</button></div></div>`).join('');
    $$('[data-run]').forEach(b => b.onclick = () => guard(b, async () => {
      const id = b.dataset.run; const th = $(`.scTh[data-id="${id}"]`);
      $('#scOut').hidden = false; $('#scOut').innerHTML = '<span class="spin"></span> 查询中…';
      const f = hostFilter(); delete f.logtypes;
      const res = await runQuery(POST(`/api/scenarios/${id}/run`, { ...f, ...(th ? { threshold: +th.value } : {}) }));
      const s = r.scenarios.find(x => x.id === id);
      $('#scOut').innerHTML = `<h3>${esc(s.name)} ${badge(...LEVEL[s.level])}</h3>` + resultTable(res, { empty: '未发现（所选范围内无命中）',
        render: { sensitive: v => v === 'true' ? badge('敏感', 'crit') : '', suspicious: v => v === 'true' ? badge('可疑', 'crit') : '', line: v => `<code>${esc(v)}</code>` },
      }).replace(/<th>(\w+)<\/th>/g, (m, c) => `<th>${c}</th>`);
      // 列名渲染：resultTable 以索引渲染，敏感列按名称二次处理
      const cols = res.columns; $$('#scOut tbody tr').forEach(tr => [...tr.children].forEach((td, i) => {
        if (['sensitive', 'suspicious'].includes(cols[i])) td.innerHTML = td.textContent === 'true' ? badge('是', 'crit') : '';
        if (cols[i] === 'line') td.innerHTML = `<code>${td.innerHTML}</code>`; }));
      $('#scOut').scrollIntoView({ behavior: 'smooth' });
    }));
  }
};
