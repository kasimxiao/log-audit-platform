// 主机管理 / 采集模板 / 同步记录
const SSM_BADGE = { online: ['SSM 在线', 'good'], offline: ['SSM 离线', 'serious'], no_profile: ['无实例角色', 'crit'], not_registered: ['未注册 SSM', 'warn'] };
const ST = { region: localStorage.getItem('la_region') || CFG.region, hosts: [], templates: [], hints: {} };

async function loadTemplates() { ST.templates = (await GET('/api/templates')).templates; return ST.templates; }

PAGES.hosts = {
  title: '主机管理', async render(el) {
    el.innerHTML = `<h1>主机管理</h1><p class="sub">按区域罗列 EC2，查看 SSM 纳管状态，配置日志同步（模板 + 自定义路径）。每日 02:00（北京时间）自动同步到 S3。</p>
    <div class="card"><div class="row"><label>区域 <select id="hRegion"><option>${esc(ST.region)}</option></select></label>
      <label>标签筛选 <input id="hTk" placeholder="键，如 Project" size="10"><input id="hTv" placeholder="值（可空）" size="10"></label>
      <button id="hLoad" class="primary">查询</button><span class="spacer"></span>
      ${IS_ADMIN ? '<button id="hBulk">按标签批量纳管</button><button id="hTag">给选中主机打标签</button><button id="hSync">立即同步选中</button>' : ''}</div>
      <div id="hSum" class="row small muted" style="margin-top:8px"></div></div>
    <div class="card"><div id="hList" class="muted">加载中…</div></div>`;
    $('#hLoad').onclick = e => guard(e.target, loadHosts);
    $('#hRegion').onchange = () => { ST.region = $('#hRegion').value; localStorage.setItem('la_region', ST.region); loadHosts(); };
    if (IS_ADMIN) { $('#hBulk').onclick = bulkEnroll; $('#hTag').onclick = tagSelected; $('#hSync').onclick = e => syncSelected(e.target); }
    loadTemplates();
    GET('/api/regions').then(r => {
      if (!$('#hRegion')) return;
      const regs = r.regions.sort((a, b) => (b.instances || 0) - (a.instances || 0));
      $('#hRegion').innerHTML = regs.map(x => `<option value="${x.region}" ${x.region === ST.region ? 'selected' : ''}>${x.region}（${x.instances ?? '?'} 台）</option>`).join('');
      $('#hSum').textContent = `共 ${regs.filter(x => x.instances).length} 个区域有实例，合计 ${regs.reduce((s, x) => s + (x.instances || 0), 0)} 台`;
    }).catch(e => toast(e.message, true));
    await guard(null, loadHosts);
  }
};

async function loadHosts() {
  const tk = $('#hTk').value.trim(), tv = $('#hTv').value.trim();
  const r = await GET(`/api/hosts?region=${ST.region}${tk ? `&tag_key=${encodeURIComponent(tk)}&tag_value=${encodeURIComponent(tv)}` : ''}`);
  ST.hosts = r.hosts; ST.hints = r.hints;
  if (!$('#hList')) return;  // 已切换页面
  const cnt = k => r.hosts.filter(h => h.ssm === k).length;
  $('#hList').innerHTML = `<div class="row small" style="margin-bottom:8px">${r.hosts.length} 台 · ${badge('SSM 在线 ' + cnt('online'), 'good')} ${badge('未注册 ' + cnt('not_registered'), 'warn')}
    ${badge('无实例角色 ' + cnt('no_profile'), 'crit')} ${badge('离线 ' + cnt('offline'), 'serious')} · 已开启同步 ${r.hosts.filter(h => h.sync_enabled).length} 台</div>` +
    table([{ key: 'sel', label: '' }, { key: 'name', label: '名称' }, { key: 'instance_id', label: '实例 ID' }, { key: 'state', label: '状态' },
      { key: 'platform', label: '系统' }, { key: 'private_ip', label: '私网 IP' }, { key: 'ssm', label: 'SSM' }, { key: 'sync_enabled', label: '日志同步' },
      { key: 'last_sync', label: '最近同步' }, { key: 'op', label: '操作' }], r.hosts, { render: {
      sel: (_, h) => `<input type="checkbox" class="hsel" value="${h.instance_id}">`,
      name: (v, h) => `${esc(v || '-')}<div class="small muted">${esc(Object.entries(h.tags).filter(([k]) => k !== 'Name' && !k.startsWith('aws:')).slice(0, 3).map(([k, v]) => `${k}=${v}`).join(' '))}</div>`,
      state: v => badge(v, v === 'running' ? 'good' : 'warn'),
      ssm: (v, h) => { const [t, k] = SSM_BADGE[v]; return badge(t, k) + (h.agent ? `<div class="small muted">agent ${esc(h.agent)}</div>` : ''); },
      sync_enabled: (v, h) => v ? badge('已开启', 'info') + `<div class="small muted">${esc(h.templates.join(', '))}${h.extra_paths.length ? ` +${h.extra_paths.length} 路径` : ''}</div>` : '<span class="muted">未开启</span>',
      last_sync: v => v ? `${esc(v.at.slice(5, 16).replace('T', ' '))}<div class="small muted">${fmtNum(v.lines)} 行</div>` : '-',
      op: (_, h) => `<button class="sm" onclick="hostConfig('${h.instance_id}')">${IS_ADMIN ? '同步配置' : '查看配置'}</button>
        ${h.ssm === 'online' ? `<button class="sm" onclick="goLive('${ST.region}','${h.instance_id}')">实时查询</button>` : ''}
        ${h.ssm !== 'online' ? `<button class="sm" onclick="ssmHelp('${h.instance_id}')">${h.ssm === 'no_profile' && IS_ADMIN ? '接入' : '原因'}</button>` : ''}`,
    } });
}

function ssmHelp(iid) {
  const h = ST.hosts.find(x => x.instance_id === iid);
  modal(`SSM 未纳管：${h.name || iid}`, `<p>${esc(ST.hints[h.ssm] || '')}</p>
    <p class="small muted">实例角色：${esc(h.instance_profile || '无')}</p>
    ${h.ssm === 'no_profile' && IS_ADMIN ? '<button class="primary" id="obBtn">挂载 LogAuditInstanceProfile</button>' : ''}
    <details style="margin-top:12px"><summary>未装 SSM Agent 时的安装命令</summary><pre>
# Amazon Linux / RHEL
sudo dnf install -y https://s3.${ST.region}.amazonaws.com/amazon-ssm-${ST.region}/latest/linux_$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')/amazon-ssm-agent.rpm
sudo systemctl enable --now amazon-ssm-agent
# Ubuntu
sudo snap install amazon-ssm-agent --classic && sudo snap start amazon-ssm-agent</pre></details>`, (m, close) => {
    const b = $('#obBtn', m); if (b) b.onclick = () => guard(b, async () => { const r = await POST(`/api/hosts/${ST.region}/${iid}/onboard`); toast(r.message); close(); loadHosts(); });
  });
}

async function hostConfig(iid) {
  const h = ST.hosts.find(x => x.instance_id === iid);
  const c = await GET(`/api/hosts/${ST.region}/${iid}/config`);
  if (!ST.templates.length) await loadTemplates();
  const ro = IS_ADMIN ? '' : 'disabled';
  modal(`同步配置：${h.name || iid}`, `
    <label class="row"><input type="checkbox" id="cEn" ${c.enabled ? 'checked' : ''} ${ro}> 开启每日同步</label>
    <h4>采集模板</h4><div class="chips">${ST.templates.map(t => `<label class="chip"><input type="checkbox" class="cTpl" value="${t.id}" ${c.templates.includes(t.id) ? 'checked' : ''} ${ro}> ${esc(t.name)}</label>`).join('')}</div>
    <h4>自定义路径 <span class="small muted">（支持通配符 *，journal:&lt;unit&gt; 表示 journald）</span></h4><div id="cPaths"></div>
    ${IS_ADMIN ? '<button class="sm" id="cAdd">+ 添加路径</button>' : ''}
    <h4>生效路径预览</h4>${table(['路径', '类型', '方式', '二进制处理'], c.effective_paths.map(p => [p.path, p.logtype, p.kind, p.kind === 'file' || p.kind === 'journal' ? '-' : p.binmode]))}
    ${IS_ADMIN ? '<div class="row" style="margin-top:14px"><span class="spacer"></span><button class="primary" id="cSave">保存</button></div>' : ''}`, (m, close) => {
    const paths = c.extra_paths.slice();
    const draw = () => $('#cPaths', m).innerHTML = paths.map((p, i) => pathRow(p, i, ro)).join('') || '<div class="small muted">无</div>';
    draw();
    if (!IS_ADMIN) return;
    $('#cAdd', m).onclick = () => { collectPaths(m, paths); paths.push({ path: '', logtype: 'custom', binmode: 'text' }); draw(); };
    m.addEventListener('click', e => { if (e.target.dataset.rm !== undefined) { collectPaths(m, paths); paths.splice(+e.target.dataset.rm, 1); draw(); } });
    $('#cSave', m).onclick = e => guard(e.target, async () => {
      collectPaths(m, paths);
      await PUT(`/api/hosts/${ST.region}/${iid}/config`, { enabled: $('#cEn', m).checked, templates: $$('.cTpl', m).filter(x => x.checked).map(x => x.value),
        extra_paths: paths.filter(p => p.path), name: h.name });
      toast('已保存'); close(); loadHosts();
    });
  });
}
const LOGTYPES = ['auth', 'audit', 'cron', 'syslog', 'login_wtmp', 'login_btmp', 'lastlog', 'nginx_access', 'nginx_error', 'httpd_access', 'httpd_error', 'custom'];
function pathRow(p, i, ro = '') {
  return `<div class="row prow" style="margin:4px 0"><input class="pp" value="${esc(p.path)}" placeholder="/var/log/app/*.log" size="36" ${ro}>
    <select class="pt" ${ro}>${LOGTYPES.map(t => `<option ${t === p.logtype ? 'selected' : ''}>${t}</option>`).join('')}</select>
    <select class="pb" title="二进制日志(wtmp/btmp/lastlog)处理方式" ${ro}>${[['text', '转文本'], ['raw', '原样'], ['both', '两者']].map(([v, l]) => `<option value="${v}" ${v === (p.binmode || 'text') ? 'selected' : ''}>${l}</option>`).join('')}</select>
    ${ro ? '' : `<button class="sm danger" data-rm="${i}">删除</button>`}</div>`;
}
function collectPaths(m, arr) { $$('.prow', m).forEach((r, i) => { arr[i] = { path: $('.pp', r).value.trim(), logtype: $('.pt', r).value, binmode: $('.pb', r).value }; }); }

function selected() { return $$('.hsel').filter(x => x.checked).map(x => x.value); }
async function syncSelected(btn) {
  const ids = selected(); if (!ids.length) return toast('请先勾选主机', true);
  await guard(btn, async () => { const r = await POST('/api/sync/run', { targets: ids.map(i => ({ region: ST.region, instance_id: i })) });
    toast(`已下发 ${r.results.filter(x => x.status === 'dispatched').length}/${r.results.length}，到"同步记录"查看进度`); });
}
function tagSelected() {
  const ids = selected(); if (!ids.length) return toast('请先勾选主机', true);
  modal('给选中主机打标签', `<div class="row"><input id="tk" placeholder="键" value="LogAudit"><input id="tv" placeholder="值" value="enabled"><button class="primary" id="tGo">确定</button></div>
    <p class="small muted">${ids.length} 台：${ids.join(', ')}</p>`, (m, close) => {
    $('#tGo', m).onclick = e => guard(e.target, async () => { await POST(`/api/hosts/${ST.region}/tags`, { instance_ids: ids, key: $('#tk', m).value, value: $('#tv', m).value }); toast('已打标签'); close(); loadHosts(); });
  });
}
function bulkEnroll() {
  modal('按标签批量纳管', `<p class="small muted">匹配标签的主机将开启同步并套用所选模板。</p>
    <div class="row"><input id="bk" placeholder="标签键" value="${esc($('#hTk').value || 'Project')}"><input id="bv" placeholder="标签值（空=任意）" value="${esc($('#hTv').value)}"></div>
    <h4>模板</h4><div class="chips">${ST.templates.map(t => `<label class="chip"><input type="checkbox" class="bTpl" value="${t.id}"> ${esc(t.name)}</label>`).join('')}</div>
    <div class="row" style="margin-top:12px"><span class="spacer"></span><button class="primary" id="bGo">执行</button></div>`, (m, close) => {
    $('#bGo', m).onclick = e => guard(e.target, async () => {
      const r = await POST(`/api/hosts/${ST.region}/enroll-by-tag`, { tag_key: $('#bk', m).value, tag_value: $('#bv', m).value, templates: $$('.bTpl', m).filter(x => x.checked).map(x => x.value) });
      toast(`已纳管 ${r.matched} 台`); close(); loadHosts();
    });
  });
}

// ---------------------------------------------------------------- 采集模板
PAGES.templates = {
  title: '采集模板', async render(el) {
    el.innerHTML = `<h1>采集模板</h1><p class="sub">主机套用模板即采集其中的路径；内置模板可修改。二进制日志（wtmp/btmp/lastlog）可选"转文本"（last/lastlog 输出，可检索）、"原样"或"两者"。</p>
      <div class="row" style="margin-bottom:12px">${IS_ADMIN ? '<button class="primary" id="tNew">新建模板</button>' : ''}</div><div id="tList"></div>`;
    if (IS_ADMIN) $('#tNew').onclick = () => editTemplate({ id: '', name: '', paths: [{ path: '', logtype: 'custom', binmode: 'text' }] });
    await drawTemplates();
  }
};
async function drawTemplates() {
  const ts = await loadTemplates();
  $('#tList').innerHTML = ts.map(t => `<div class="card"><div class="row"><h3 style="margin:0">${esc(t.name)}</h3><span class="muted small">${esc(t.id)}${t.builtin ? ' · 内置' : ''}</span><span class="spacer"></span>
    ${IS_ADMIN ? `<button class="sm" onclick='editTemplate(${JSON.stringify(t).replace(/'/g, "&#39;")})'>编辑</button><button class="sm danger" onclick="delTemplate('${t.id}')">删除</button>` : ''}</div>
    ${table(['路径', '日志类型', '采集方式', '二进制处理'], t.paths.map(p => [p.path, p.logtype, p.kind, ['wtmp', 'btmp', 'lastlog'].includes(p.kind) ? ({ text: '转文本', raw: '原样', both: '两者' })[p.binmode] : '-']))}</div>`).join('');
}
function editTemplate(t) {
  modal(t.id ? `编辑模板 ${t.id}` : '新建模板', `<div class="row"><label>ID <input id="eId" value="${esc(t.id)}" ${t.id ? 'disabled' : ''} placeholder="app-logs"></label>
    <label>名称 <input id="eName" value="${esc(t.name)}" size="30"></label></div><h4>路径</h4><div id="ePaths"></div><button class="sm" id="eAdd">+ 添加</button>
    <div class="row" style="margin-top:12px"><span class="spacer"></span><button class="primary" id="eSave">保存</button></div>`, (m, close) => {
    const paths = t.paths.slice();
    const draw = () => $('#ePaths', m).innerHTML = paths.map((p, i) => pathRow(p, i)).join('');
    draw();
    $('#eAdd', m).onclick = () => { collectPaths(m, paths); paths.push({ path: '', logtype: 'custom', binmode: 'text' }); draw(); };
    m.addEventListener('click', e => { if (e.target.dataset.rm !== undefined) { collectPaths(m, paths); paths.splice(+e.target.dataset.rm, 1); draw(); } });
    $('#eSave', m).onclick = e => guard(e.target, async () => { collectPaths(m, paths);
      await PUT('/api/templates', { id: $('#eId', m).value.trim(), name: $('#eName', m).value.trim(), paths: paths.filter(p => p.path) }); toast('已保存'); close(); drawTemplates(); });
  });
}
async function delTemplate(id) { if (!confirm(`删除模板 ${id}？已引用该模板的主机将不再采集其路径。`)) return; await guard(null, async () => { await DEL(`/api/templates/${id}`); drawTemplates(); }); }

// ---------------------------------------------------------------- 同步记录
const SYNC_BADGE = { dispatched: ['已下发', 'info'], uploaded: ['已上传', 'info'], ingested: ['已入库', 'good'], host_failed: ['主机执行失败', 'crit'],
  dispatch_failed: ['下发失败', 'crit'], skipped: ['跳过', 'warn'] };
PAGES.sync = {
  title: '同步记录', async render(el) {
    el.innerHTML = `<h1>同步记录</h1><p class="sub">每日 02:00（北京时间）由 EventBridge Scheduler 触发；主机按偏移量增量上传，S3 事件触发入库。</p>
      <div class="card row"><label>日期 <input type="date" id="sDay" value="${today()}"></label><button id="sLoad" class="primary">刷新</button><span class="spacer"></span>
      ${IS_ADMIN ? '<button id="sAll">立即同步全部已开启主机</button>' : ''}</div><div class="card" id="sList"></div>`;
    const load = () => guard($('#sLoad'), async () => {
      const r = await GET(`/api/sync/history?day=${$('#sDay').value}`);
      $('#sList').innerHTML = table([{ key: 'started_at', label: '开始' }, { key: 'name', label: '主机' }, { key: 'region', label: '区域' }, { key: 'trigger', label: '触发' },
        { key: 'status', label: '状态' }, { key: 'ingest', label: '入库' }, { key: 'detail', label: '详情' }], r.items, { render: {
        started_at: v => esc(v.slice(11, 19)), name: (v, x) => `${esc(v || '')}<div class="small muted">${x.instance_id}</div>`,
        trigger: (v, x) => `${v === 'schedule' ? '定时' : '手动'}<div class="small muted">${esc(x.actor)}</div>`,
        status: v => { const [t, k] = SYNC_BADGE[v] || [v, 'warn']; return badge(t, k); },
        ingest: v => v ? `${fmtNum(v.lines)} 行${v.missing && v.missing.length ? `<div class="small muted">缺失：${esc(v.missing.join(', '))}</div>` : ''}${v.notes && v.notes.length ? `<div class="small muted">轮转补采：${esc(v.notes.join('；'))}</div>` : ''}${v.tz ? `<div class="small muted">主机时区 ${esc(v.tz)}</div>` : ''}` : '-',
        detail: (_, x, i) => `${esc(x.message || x.host_error || '')}${x.script ? ` <button class="sm" onclick="syncCmd(${i})">执行的命令</button>` : ''}` }, empty: '当天无同步记录' });
      window._syncItems = r.items;
    });
    $('#sLoad').onclick = load; $('#sDay').onchange = load;
    if (IS_ADMIN) $('#sAll').onclick = e => guard(e.target, async () => { const r = await POST('/api/sync/run', {}); toast(`已下发 ${r.results.length} 台`); load(); });
    load();
  }
};

function syncCmd(i) {
  const x = window._syncItems[i];
  modal(`执行的命令：${x.name || x.instance_id}`, `<p class="small muted">SSM AWS-RunShellScript（root）· 区域 ${esc(x.region)} · 实例 ${esc(x.instance_id)} · CommandId ${esc(x.command_id || '-')} · 同步批次 ${esc(x.sync_id)}</p>
    <h4>采集配置（kind|二进制处理|日志类型|路径）</h4><pre class="cmdline">${esc((x.config_lines || []).join('\n'))}</pre>
    <h4>主机返回</h4><pre class="cmdline">${esc((x.host_output || '') + (x.host_error ? '\n[stderr]\n' + x.host_error : '') || '（尚未返回）')}</pre>
    <h4>下发的完整脚本 <span class="small muted">（预签名 URL 已脱敏）</span></h4><pre class="sql" style="max-height:420px">${esc(x.script)}</pre>`);
}
