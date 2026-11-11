// 智能问答 / 用户管理 / 操作审计 / 数据源与设置
const CHAT = { msgs: [] };
const SAMPLES = ['昨天有哪些 IP 在暴力破解 SSH？各失败多少次？', '最近 7 天每台主机 root 登录成功的记录', '最近 3 天 ALB 5xx 最多的接口和时段',
  '最近 7 天访问量 Top 10 的客户端 IP', '最近一周有没有新建系统用户或修改 crontab？', '最近 3 天 IAM 有哪些变更操作？'];

PAGES.nlq = {
  title: '智能问答', async render(el) {
    el.innerHTML = `<h1>智能问答</h1><p class="sub">用自然语言提问，助手把问题转成只读 SQL 在 Athena 上执行并解释结果。每次回答附执行轨迹（SQL、耗时、token）。</p>
      <div class="card"><div class="chat" id="chat"></div>
      <div class="chips" style="margin:12px 0" id="samples">${SAMPLES.map(s => `<span class="chip">${esc(s)}</span>`).join('')}</div>
      <div class="row"><textarea id="q" rows="2" placeholder="例如：11月2日哪个 IP 对 app-prod 主机做了 SSH 爆破？之后有没有登录成功？" style="flex:1"></textarea>
        <div style="display:flex;flex-direction:column;gap:6px"><button class="primary" id="ask">发送</button><button class="sm" id="clr">清空对话</button></div></div></div>`;
    drawChat();
    $$('#samples .chip').forEach(c => c.onclick = () => { $('#q').value = c.textContent; $('#q').focus(); });
    $('#q').onkeydown = e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) $('#ask').click(); };
    $('#clr').onclick = () => { CHAT.msgs = []; drawChat(); };
    $('#ask').onclick = e => guard(e.target, async () => {
      const q = $('#q').value.trim(); if (!q) return;
      const history = CHAT.msgs.filter(m => m.text).map(m => ({ role: m.role, text: m.text }));
      CHAT.msgs.push({ role: 'user', text: q }); const a = { role: 'assistant', pending: true, steps: [] }; CHAT.msgs.push(a); $('#q').value = ''; drawChat();
      const { job_id } = await POST('/api/nlq', { question: q, history });
      for (let i = 0; i < 300; i++) {
        await sleep(1500); const j = await GET(`/api/nlq/${job_id}`); a.steps = j.steps || [];
        if (j.status === 'done') { Object.assign(a, { pending: false, text: j.answer, sql_runs: j.sql_runs, usage: j.usage_, latency_ms: j.latency_ms, cost: j.cost_usd, model: j.model }); break; }
        if (j.status === 'error') { Object.assign(a, { pending: false, text: '出错了：' + j.error }); break; }
        drawChat();
      }
      drawChat();
    });
  }
};
function drawChat() {
  const el = $('#chat'); if (!el) return;
  el.innerHTML = CHAT.msgs.map(m => m.role === 'user' ? `<div class="msg u">${esc(m.text)}</div>` :
    `<div class="msg a">${m.pending ? `<span class="spin"></span> 分析中… ${m.steps.length ? `<span class="small muted">（${esc(lastStep(m.steps))}）</span>` : ''}` : md(m.text)}
      ${m.pending ? '' : traceHtml(m)}</div>`).join('') || '<div class="muted small">还没有对话</div>';
}
function lastStep(steps) { const s = steps[steps.length - 1]; return s.type === 'tool' ? `执行 ${s.name}` : '模型推理'; }
function traceHtml(m) {
  if (!m.steps) return '';
  const sqls = (m.sql_runs || []).map((r, i) => `<details open><summary>执行的 SQL #${i + 1} ${r.ok ? badge('成功', 'good') : badge(r.error === 'rejected' ? '守卫拒绝' : '执行失败', 'crit')} ${esc(r.purpose || '')}</summary>
    <pre class="sql">${esc(r.executed_sql || r.sql)}</pre>${r.ok ? `<div class="small muted">${r.result.row_count} 行 · 扫描 ${fmtBytes(r.result.bytes_scanned)} · ${r.result.elapsed_ms} ms</div>` : `<div class="small">${esc(r.message || '')}</div>`}</details>`).join('');
  const steps = m.steps.map(s => s.type === 'llm' ? `模型第 ${s.turn + 1} 轮 ${s.ms}ms（in ${s.tokens_in} / out ${s.tokens_out}）` : `工具 ${s.name} ${s.ms}ms ${s.ok ? '' : '✗'} ${esc(s.summary || '')}`);
  return `<div class="trace">执行轨迹：${fmtNum(m.latency_ms)} ms · ${m.usage ? `${fmtNum(m.usage.input)} / ${fmtNum(m.usage.output)} tokens` : ''} · 估算 $${m.cost ?? '-'} · ${esc(m.model || '')}
    ${sqls}<details><summary>步骤（${m.steps.length}）</summary><div class="small">${steps.join('<br>')}</div></details></div>`;
}

// ---------------------------------------------------------------- 用户管理
PAGES.users = {
  title: '用户管理', admin: true, async render(el) {
    el.innerHTML = `<h1>用户管理</h1><p class="sub">管理员：配置主机/模板/数据源、触发同步、管理用户、查看操作审计与评估控制台；普通用户：查看与查询。</p>
      <div class="card"><div class="row"><input id="uName" placeholder="用户名"><input id="uDisp" placeholder="显示名（可空）"><input id="uMail" placeholder="邮箱（可空）">
        <select id="uRole"><option value="user">普通用户</option><option value="admin">管理员</option></select><button class="primary" id="uAdd">新建用户</button></div></div>
      <div class="card" id="uList"></div>`;
    const load = async () => { const r = await GET('/api/users');
      $('#uList').innerHTML = table([{ key: 'username', label: '用户名' }, { key: 'name', label: '显示名' }, { key: 'role', label: '角色' }, { key: 'status', label: '状态' },
        { key: 'created', label: '创建时间' }, { key: 'op', label: '操作' }], r.users, { render: {
        role: v => v === 'admin' ? badge('管理员', 'serious') : badge('普通用户', 'info'),
        status: (v, u) => `${u.enabled ? '' : badge('已停用', 'crit')} ${v === 'FORCE_CHANGE_PASSWORD' ? '待首次改密' : v === 'CONFIRMED' ? '正常' : esc(v)}`,
        created: v => esc(String(v).slice(0, 16).replace('T', ' ')),
        op: (_, u) => u.username === ME.username ? '<span class="muted small">当前用户</span>' :
          `<button class="sm" onclick="uAct('role','${u.username}','${u.role === 'admin' ? 'user' : 'admin'}')">设为${u.role === 'admin' ? '普通用户' : '管理员'}</button>
           <button class="sm" onclick="uAct('enabled','${u.username}',${!u.enabled})">${u.enabled ? '停用' : '启用'}</button>
           <button class="sm" onclick="uAct('reset','${u.username}')">重置密码</button><button class="sm danger" onclick="uAct('del','${u.username}')">删除</button>` } });
    };
    window.uReload = load;
    $('#uAdd').onclick = e => guard(e.target, async () => {
      const r = await POST('/api/users', { username: $('#uName').value.trim(), name: $('#uDisp').value.trim(), email: $('#uMail').value.trim(), role: $('#uRole').value });
      showPw(r); load(); });
    await guard(null, load);
  }
};
function showPw(r) { modal('临时密码', `<p>用户 <b>${esc(r.username)}</b> 的临时密码（仅显示一次，首次登录需修改）：</p><pre>${esc(r.temp_password)}</pre>`); }
async function uAct(act, name, v) {
  await guard(null, async () => {
    if (act === 'role') await PUT(`/api/users/${name}/role`, { role: v });
    if (act === 'enabled') await PUT(`/api/users/${name}/enabled`, { enabled: v });
    if (act === 'reset') { if (!confirm(`重置 ${name} 的密码？`)) return; showPw(await POST(`/api/users/${name}/reset-password`)); }
    if (act === 'del') { if (!confirm(`删除用户 ${name}？`)) return; await DEL(`/api/users/${name}`); }
    toast('已完成'); uReload();
  });
}

// ---------------------------------------------------------------- 操作审计
PAGES.audit = {
  title: '平台操作审计', admin: true, async render(el) {
    el.innerHTML = `<h1>平台操作审计</h1><p class="sub">记录每一次配置变更、查询（含实时主机查询与智能问答的问题）、用户管理操作：谁、何时、从哪、做了什么、结果。</p>
      <div class="card row"><label>开始 <input type="date" id="auF" value="${today(-6)}"></label><label>结束 <input type="date" id="auT" value="${today()}"></label>
        <label>操作者 <input id="auA" size="12"></label><label>动作包含 <input id="auX" size="14" placeholder="live / user. / nlq"></label><button class="primary" id="auGo">查询</button></div>
      <div class="card" id="auOut"></div>`;
    const load = () => guard($('#auGo'), async () => {
      const r = await GET(`/api/audit?from=${$('#auF').value}&to=${$('#auT').value}&actor=${encodeURIComponent($('#auA').value.trim())}&action=${encodeURIComponent($('#auX').value.trim())}`);
      $('#auOut').innerHTML = `<div class="small muted" style="margin-bottom:6px">${r.items.length} 条</div>` + table([{ key: 'ts', label: '时间(UTC)' }, { key: 'actor', label: '操作者' },
        { key: 'action', label: '动作' }, { key: 'target', label: '对象' }, { key: 'status', label: '结果' }, { key: 'ip', label: '来源 IP' }, { key: 'detail', label: '参数' }], r.items, { render: {
        ts: v => esc(v.replace('T', ' ').replace('Z', '')), actor: (v, x) => `${esc(v)}<div class="small muted">${x.role}</div>`,
        status: v => +v < 400 ? badge(v, 'good') : badge(v, 'crit'), detail: v => `<code class="small">${esc(String(v).slice(0, 300))}</code>` } });
    });
    $('#auGo').onclick = load; load();
  }
};

// ---------------------------------------------------------------- 数据源与设置
PAGES.settings = {
  title: '数据源与设置', admin: true, async render(el) {
    el.innerHTML = `<h1>数据源与设置</h1>
      <div class="card"><h3>ALB 日志数据源</h3><p class="small muted">每个数据源 = 一个桶 + 前缀（ALB 访问日志的 S3 位置，不含 AWSLogs/...），平台自动探测账号与区域并建 Athena 表（按天分区投影）。</p>
        <div id="srcList"></div>
        <div class="row" style="margin-top:10px"><input id="nsId" placeholder="ID（如 shop_web）" size="14"><input id="nsName" placeholder="名称" size="14">
          <input id="nsBucket" placeholder="桶名" size="30"><input id="nsPrefix" placeholder="前缀（可空）" size="12"><button id="nsDetect">探测</button><button class="primary" id="nsAdd">添加</button></div>
        <div id="nsInfo" class="small muted" style="margin-top:6px"></div>
        <details style="margin-top:10px"><summary>本账号已开启访问日志的 ALB（点击带入）</summary><div id="elbList" class="small muted">加载中…</div></details></div>
      <div class="card"><h3>CloudTrail 数据源</h3><div id="ctCur" class="small"></div><div id="ctTrails" class="small muted" style="margin-top:6px">加载中…</div></div>
      <div class="card"><h3>实时查询允许的根目录</h3><p class="small muted">实时查询只允许读取这些目录下的文件（禁止 /、/etc、/root、/proc）。</p>
        <div class="row"><input id="roots" size="60"><button class="primary" id="rootsSave">保存</button></div></div>`;
    const loadSrc = async () => { const r = await GET('/api/alb/sources');
      $('#srcList').innerHTML = table([{ key: 'id', label: 'ID' }, { key: 'name', label: '名称' }, { key: 'loc', label: '位置' }, { key: 'table', label: 'Athena 表' }, { key: 'op', label: '' }], r.sources,
        { render: { loc: (_, s) => `s3://${esc(s.bucket)}/${esc(s.prefix)} <span class="muted">(${s.account} / ${s.region})</span>`,
          op: (_, s) => `<button class="sm danger" onclick="delSrc('${s.id}')">删除</button>` }, empty: '暂无' }); };
    window.reloadSrc = loadSrc; loadSrc();
    $('#nsDetect').onclick = e => guard(e.target, async () => { const r = await POST('/api/alb/detect', { bucket: $('#nsBucket').value.trim(), prefix: $('#nsPrefix').value.trim() });
      $('#nsInfo').textContent = r.found.length ? '发现：' + r.found.map(x => `${x.account}/${x.region}`).join('、') : '该位置下未发现 AWSLogs/<账号>/elasticloadbalancing/ 目录'; });
    $('#nsAdd').onclick = e => guard(e.target, async () => { await POST('/api/alb/sources', { id: $('#nsId').value.trim(), name: $('#nsName').value.trim(),
      bucket: $('#nsBucket').value.trim(), prefix: $('#nsPrefix').value.trim() }); toast('已添加'); loadSrc(); });
    GET('/api/alb/elbs').then(r => { $('#elbList').innerHTML = table([{ key: 'name', label: 'ALB' }, { key: 'region', label: '区域' }, { key: 'scheme', label: '类型' },
      { key: 'logs', label: '访问日志' }], r.elbs, { render: { logs: (_, x) => x.logs_enabled ? `<a href="javascript:void 0" onclick="fillSrc('${x.bucket}','${x.prefix || ''}','${x.name}')">s3://${esc(x.bucket)}/${esc(x.prefix || '')}</a>` : '<span class="muted">未开启</span>' } }); });
    const ct = await GET('/api/cloudtrail/config');
    $('#ctCur').innerHTML = ct.config ? `当前：s3://${esc(ct.config.bucket)}/${esc(ct.config.prefix)}（${ct.config.regions.length} 个区域投影）` : '尚未配置';
    GET('/api/cloudtrail/trails').then(r => $('#ctTrails').innerHTML = table(['Trail', '桶', '前缀', '主区域', '多区域', ''], r.trails.map(t => [t.name, t.bucket, t.prefix, t.home_region, t.multi_region ? '是' : '否', t]),
      { render: { 5: t => `<button class="sm primary" onclick="setCt('${t.bucket}','${t.prefix}')">使用此 Trail</button>` } }));
    const roots = await GET('/api/live/roots'); $('#roots').value = roots.roots.join(', ');
    $('#rootsSave').onclick = e => guard(e.target, async () => { await PUT('/api/live/roots', { roots: $('#roots').value.split(',').map(s => s.trim()).filter(Boolean) }); toast('已保存'); });
  }
};
function fillSrc(b, p, n) { $('#nsBucket').value = b; $('#nsPrefix').value = p; $('#nsName').value = n; $('#nsId').value = n.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^[^a-z]+/, '').slice(0, 30); }
async function delSrc(id) { if (!confirm(`删除数据源 ${id}？（仅删除 Athena 表，不删 S3 日志）`)) return; await guard(null, async () => { await DEL(`/api/alb/sources/${id}`); reloadSrc(); }); }
async function setCt(b, p) { await guard(null, async () => { await PUT('/api/cloudtrail/config', { bucket: b, prefix: p }); toast('已配置 CloudTrail'); PAGES.settings.render($('main')); }); }
