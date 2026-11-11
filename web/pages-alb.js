// ALB 日志分析 + CloudTrail
const ALB = { src: localStorage.getItem('la_albsrc') || '', from: '', to: '' };

PAGES.alb = {
  title: 'ALB 日志分析', async render(el) {
    const srcs = (await GET('/api/alb/sources')).sources;
    if (!srcs.length) { el.innerHTML = `<h1>ALB 日志分析</h1><div class="card">尚未配置 ALB 数据源。${IS_ADMIN ? '<a href="#settings">去"数据源与设置"添加</a>' : '请联系管理员添加。'}</div>`; return; }
    if (!srcs.find(s => s.id === ALB.src)) ALB.src = srcs[0].id;
    const tab = PAGES.alb.tab || 'dash';
    el.innerHTML = `<h1>ALB 日志分析</h1><p class="sub">每个数据源对应一个 S3 桶/前缀（按天分区投影），日期范围最长 92 天。</p>
      <div class="card"><div class="row"><label>数据源 <select id="aSrc">${srcs.map(s => `<option value="${s.id}" ${s.id === ALB.src ? 'selected' : ''}>${esc(s.name)}（s3://${esc(s.bucket)}/${esc(s.prefix)}）</option>`).join('')}</select></label>
        <label>开始 <input type="date" id="aFrom" value="${ALB.from || today(-6)}"></label><label>结束 <input type="date" id="aTo" value="${ALB.to || today()}"></label>
        <label>负载均衡器 <input id="aElb" placeholder="app/name/id（可空）" size="22"></label><button id="aGo" class="primary">分析</button></div></div>
      <div class="tabs"><a data-t="dash" class="${tab === 'dash' ? 'on' : ''}">分析看板</a><a data-t="search" class="${tab === 'search' ? 'on' : ''}">条件检索</a></div><div id="aBody"></div>`;
    $$('.tabs a', el).forEach(a => a.onclick = () => { PAGES.alb.tab = a.dataset.t; saveAlb(); PAGES.alb.render(el); });
    $('#aSrc').onchange = () => { saveAlb(); };
    const f = () => ({ date_from: $('#aFrom').value, date_to: $('#aTo').value, elb: $('#aElb').value.trim() || undefined });
    if (tab === 'dash') { $('#aGo').onclick = e => guard(e.target, () => { saveAlb(); return albDash(f()); }); albDashSkeleton(); }
    else { $('#aGo').onclick = () => { saveAlb(); albSearchTab(f); }; albSearchTab(f); }
  }
};
function saveAlb() { ALB.src = $('#aSrc').value; ALB.from = $('#aFrom').value; ALB.to = $('#aTo').value; localStorage.setItem('la_albsrc', ALB.src); }

function albDashSkeleton() {
  $('#aBody').innerHTML = `<div id="aKpi" class="kpi"></div>
    <div class="grid g2" style="margin-top:16px"><div class="card"><h3>请求与状态码趋势</h3><div class="chartbox"><canvas id="cTrend"></canvas></div></div>
      <div class="card"><h3>P95 后端延迟（ms）</h3><div class="chartbox"><canvas id="cLat"></canvas></div></div></div>
    <div class="grid g2"><div class="card"><h3>地理分布（Top 500 IP 归属国家）</h3><div class="chartbox"><canvas id="cGeo"></canvas></div><div class="small muted">IP 归属库：DB-IP Lite（CC BY 4.0）</div></div>
      <div class="card"><h3>状态码分布</h3><div id="rStatus"></div></div></div>
    <div class="card"><h3>疑似扫描 / 攻击来源</h3><div id="rScan"></div></div>
    <div class="grid g2"><div class="card"><h3>Top 客户端 IP</h3><div id="rTopIp"></div></div><div class="card"><h3>Top URL</h3><div id="rTopUrl"></div></div></div>
    <div class="grid g2"><div class="card"><h3>5xx 明细</h3><div id="r5xx"></div></div><div class="card"><h3>慢请求（≥ <input id="aSlow" type="number" value="1" step="0.5" style="width:56px"> 秒）</h3><div id="rSlow"></div></div></div>
    <div class="grid g2"><div class="card"><h3>各负载均衡器</h3><div id="rElb"></div></div><div class="card"><h3>User-Agent</h3><div id="rUa"></div></div></div>
    <div class="card"><h3>日志格式与解析率</h3><p class="small muted">ALB 日志格式随时间追加字段；新旧格式可混存，缺失字段为空。出现"解析失败"说明有未知格式。</p><div id="rFmt"></div></div>`;
  $('#aKpi').innerHTML = '<div class="muted">选择范围后点击"分析"</div>';
}

async function albDash(f) {
  const sid = ALB.src; const days = (new Date(f.date_to) - new Date(f.date_from)) / 864e5;
  const bucket = days <= 2 ? 'hour' : 'day';
  const rep = (rid, extra = {}, opts = {}) => runQuery(POST(`/api/alb/${sid}/report/${rid}`, { ...f, bucket, ...extra }), opts);
  $('#aKpi').innerHTML = '<span class="spin"></span> 并行执行 12 个报表…';
  const jobs = {
    overview: rep('overview').then(r => { const o = Object.fromEntries(r.columns.map((c, i) => [c, r.rows[0]?.[i]]));
      $('#aKpi').innerHTML = [['requests', '请求数', fmtNum], ['unique_ips', '独立 IP', fmtNum], ['pct_4xx', '4xx 占比', v => v + '%'], ['pct_5xx', '5xx 占比', v => v + '%'],
        ['avg_ms', '平均后端延迟', v => (v ?? '-') + ' ms'], ['p95_ms', 'P95 延迟', v => (v ?? '-') + ' ms'], ['sent_mb', '下行流量', v => v + ' MB']]
        .map(([k, l, fn]) => `<div><b>${fn(o[k])}</b><span>${l}</span></div>`).join(''); }),
    trend: rep('trend').then(r => { const g = c => r.rows.map(x => +x[r.columns.indexOf(c)]); const labels = r.rows.map(x => x[0].slice(5));
      chart('cTrend', { type: 'bar', data: { labels, datasets: [{ label: '2xx/3xx', data: g('ok'), backgroundColor: SERIES[0] },
        { label: '4xx', data: g('c4xx'), backgroundColor: STATUS.warning }, { label: '5xx', data: g('c5xx'), backgroundColor: STATUS.critical }] },
        options: { scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true, grid: { color: '#ececea' } } },
          datasets: { bar: { borderRadius: 2, borderSkipped: 'bottom', borderColor: '#fcfcfb', borderWidth: { top: 1 } } } } });
      chart('cLat', { type: 'line', data: { labels, datasets: [{ label: 'P95 ms', data: g('p95_ms'), borderColor: SERIES[0], backgroundColor: SERIES[0], borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: .25 }] },
        options: { plugins: { legend: { display: false } } } }); }),
    geo: rep('geo', {}, { geo: true }).then(r => { const c = (r.countries || []).slice(0, 12);
      chart('cGeo', { type: 'bar', data: { labels: c.map(x => x.name), datasets: [{ label: '请求数', data: c.map(x => x.requests), backgroundColor: SERIES[0], borderRadius: 3 }] },
        options: { indexAxis: 'y', plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true, grid: { color: '#ececea' } }, y: { grid: { display: false } } } } }); }),
    status: rep('status').then(r => $('#rStatus').innerHTML = resultTable(r)),
    scan: rep('scan', {}, { geo: true }).then(r => $('#rScan').innerHTML = resultTable(r, { empty: '未发现' })),
    top_ip: rep('top_ip', {}, { geo: true }).then(r => $('#rTopIp').innerHTML = resultTable(r)),
    top_url: rep('top_url').then(r => $('#rTopUrl').innerHTML = resultTable(r)),
    errors_5xx: rep('errors_5xx').then(r => $('#r5xx').innerHTML = resultTable(r, { empty: '无 5xx' })),
    slow: rep('slow', { slow_threshold: +$('#aSlow').value || 1 }).then(r => $('#rSlow').innerHTML = resultTable(r, { empty: '无慢请求' })),
    elb: rep('elb').then(r => $('#rElb').innerHTML = resultTable(r)),
    ua: rep('user_agent').then(r => $('#rUa').innerHTML = resultTable(r)),
    fmt: rep('format').then(r => $('#rFmt').innerHTML = resultTable(r)),
  };
  const res = await Promise.allSettled(Object.values(jobs));
  const bad = res.filter(x => x.status === 'rejected'); if (bad.length) toast(`${bad.length} 个报表失败：${bad[0].reason.message}`, true);
}

function albSearchTab(f) {
  $('#aBody').innerHTML = `<div class="card"><div class="row"><label>客户端 IP <input id="sIp" size="15"></label><label>状态码 <input id="sSt" size="6" placeholder="5xx / 404"></label>
    <label>URL 包含 <input id="sUrl" size="20"></label><label>方法 <select id="sM"><option value="">全部</option><option>GET</option><option>POST</option><option>PUT</option><option>DELETE</option></select></label>
    <label>延迟 ≥ <input id="sLat" size="4" placeholder="秒"></label><label>条数 <input id="sLim" type="number" value="200" style="width:70px"></label>
    <span class="spacer"></span><button id="sGo" class="primary">检索</button></div></div><div class="card" id="sOut"></div>`;
  $('#sGo').onclick = e => guard(e.target, async () => { saveAlb();
    $('#sOut').innerHTML = '<span class="spin"></span> 查询中…';
    const r = await runQuery(POST(`/api/alb/${ALB.src}/search`, { ...f(), client_ip: $('#sIp').value.trim(), status: $('#sSt').value.trim(), url_contains: $('#sUrl').value.trim(),
      method: $('#sM').value, min_latency: $('#sLat').value ? +$('#sLat').value : undefined, limit: +$('#sLim').value }), { geo: true });
    $('#sOut').innerHTML = resultTable(r);
  });
}

// ---------------------------------------------------------------- CloudTrail
PAGES.cloudtrail = {
  title: 'CloudTrail 审计', async render(el) {
    const c = await GET('/api/cloudtrail/config');
    if (!c.config) { el.innerHTML = `<h1>CloudTrail 审计</h1><div class="card">尚未配置 CloudTrail 数据源。${IS_ADMIN ? '<a href="#settings">去"数据源与设置"配置</a>' : ''}</div>`; return; }
    el.innerHTML = `<h1>CloudTrail 审计</h1><p class="sub">数据源 s3://${esc(c.config.bucket)}/${esc(c.config.prefix)}（全局服务事件如 IAM/控制台登录在 us-east-1）</p>
      <div class="card"><div class="row"><label>开始 <input type="date" id="cFrom" value="${today(-2)}"></label><label>结束 <input type="date" id="cTo" value="${today()}"></label>
        <label>区域 <input id="cReg" value="us-east-1,${esc(CFG.region)}" size="28"></label><label>操作者包含 <input id="cWho" size="16"></label>
        <label>事件名 <input id="cEv" size="16"></label><label>源 IP <input id="cIp" size="13"></label></div>
        <div class="chips" style="margin-top:10px">${c.presets.map(p => `<span class="chip" data-p="${p.id}">${esc(p.name)}</span>`).join('')}<span class="chip" data-p="__search">按条件检索</span></div></div>
      <div class="card" id="cOut"><div class="muted small">选择预置查询</div></div>`;
    const f = () => ({ date_from: $('#cFrom').value, date_to: $('#cTo').value, regions: $('#cReg').value.split(',').map(s => s.trim()).filter(Boolean),
      who: $('#cWho').value.trim(), event_name: $('#cEv').value.trim(), source_ip: $('#cIp').value.trim() });
    $$('[data-p]').forEach(ch => ch.onclick = () => guard(null, async () => {
      $('#cOut').innerHTML = '<span class="spin"></span> 查询中（CloudTrail 数据量大，可能需十几秒）…';
      const p = ch.dataset.p;
      const r = await runQuery(p === '__search' ? POST('/api/cloudtrail/search', f()) : POST(`/api/cloudtrail/preset/${p}`, f()));
      $('#cOut').innerHTML = `<h3>${esc(ch.textContent)}</h3>` + resultTable(r, { empty: '无记录' });
    }));
  }
};
