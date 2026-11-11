// 告警中心：告警记录（所有人）+ 告警设置（管理员）
PAGES.alerts = {
  title: '告警中心', async render(el) {
    el.innerHTML = `<h1>告警中心</h1><p class="sub">每天北京时间 03:00（日志同步完成 1 小时后）按所选审计场景巡检；同一风险只告警一次（按场景的去重键比对），新增命中经邮件 / Webhook 通知。</p>
      ${IS_ADMIN ? '<div class="card" id="alCfg"><span class="spin"></span></div>' : ''}
      <div class="card"><div class="row"><h3 style="margin:0">告警记录（近 14 天）</h3><span class="spacer"></span><button class="sm" id="alReload">刷新</button></div><div id="alHist" style="margin-top:10px"></div></div>`;
    $('#alReload').onclick = () => guard($('#alReload'), loadAlertHist);
    if (IS_ADMIN) loadAlertCfg();
    await guard(null, loadAlertHist);
  }
};
async function loadAlertHist() {
  const r = await GET('/api/alerts/history');
  if (!$('#alHist')) return;
  $('#alHist').innerHTML = r.items.length ? r.items.map(a => `<div class="card" style="margin:8px 0">
    <div class="row small"><b>${esc(a.at.replace('T', ' ').replace('Z', ''))}</b><span class="muted">触发：${esc(a.actor)} · 范围 ${esc(a.range.date_from)} ~ ${esc(a.range.date_to)}</span>
      <span class="spacer"></span>${a.hits.length ? badge(`${a.hits.length} 类新增风险`, 'crit') : badge('无新增', 'good')}
      ${a.notified && a.notified.length ? badge('已通知 ' + a.notified.join('、'), 'info') : ''}${a.notify_error ? badge('通知失败', 'serious') : ''}${a.notify_skipped ? badge('未发送邮件', 'warn') : ''}</div>
    ${a.notify_skipped ? `<div class="small muted">${esc(a.notify_skipped)}</div>` : ''}
    ${a.notify_error ? `<div class="small" style="color:var(--crit)">${esc(a.notify_error)}</div>` : ''}
    ${a.hits.map(h => `<details style="margin-top:6px"><summary>${badge(LEVEL[h.level][0], LEVEL[h.level][1])} ${esc(h.name)}：新增 ${h.new_count} 条（本次命中 ${h.total_hits}）</summary>
      ${table(h.columns, h.rows)}<details class="small"><summary>执行的 SQL</summary><pre class="sql">${esc(h.sql)}</pre></details></details>`).join('')}
    ${(a.errors || []).map(e => `<div class="small" style="color:var(--crit)">${esc(e.name)} 执行失败：${esc(e.error)}</div>`).join('')}</div>`).join('') : '<div class="muted small">暂无告警记录</div>';
}
async function loadAlertCfg() {
  const r = await GET('/api/alerts/settings'); const s = r.settings;
  if (!$('#alCfg')) return;
  $('#alCfg').innerHTML = `<h3>告警设置</h3>
    <div class="row"><label><input type="checkbox" id="alEn" ${s.enabled ? 'checked' : ''}> 启用每日巡检</label>
      <label>检查范围：最近 <input id="alLb" type="number" min="1" max="7" value="${s.lookback_days}" style="width:50px"> 天的同步数据</label></div>
    <h4>巡检场景</h4><div class="grid g3">${r.scenarios.map(x => { const c = s.scenarios[x.id] || {}; return `<label class="row small" style="border:1px solid var(--line);border-radius:6px;padding:6px 8px">
      <input type="checkbox" class="alSc" data-id="${x.id}" ${c.enabled ? 'checked' : ''}> ${badge(...LEVEL[x.level])} ${esc(x.name)}
      ${c.threshold !== undefined ? `<span class="spacer"></span>阈值 <input class="alTh" data-id="${x.id}" type="number" value="${c.threshold}" style="width:60px">` : ''}</label>`; }).join('')}</div>
    <h4>Webhook（IM 机器人）</h4><div class="row"><select id="alWt">${[['none', '不使用'], ['feishu', '飞书'], ['dingtalk', '钉钉'], ['wecom', '企业微信'], ['generic', '通用 JSON']].map(([v, l]) => `<option value="${v}" ${v === s.webhook.type ? 'selected' : ''}>${l}</option>`).join('')}</select>
      <input id="alWu" size="60" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…" value="${esc(s.webhook.url || '')}"></div>
    <p class="small muted">仅允许 https，且不能指向内网地址。</p>
    <div class="row"><button class="primary" id="alSave">保存设置</button><button id="alDry">预演（只看会告警什么，不通知、不记录）</button><button id="alRun">立即检查并通知</button></div>
    <h4>邮件订阅（Amazon SNS）</h4>
    ${table([{ key: 'email', label: '邮箱' }, { key: 'status', label: '状态' }, { key: 'op', label: '' }], r.subscriptions, { empty: '暂无订阅', render: {
      op: (_, x) => x.arn !== 'PendingConfirmation' ? `<button class="sm danger" onclick="alUnsub('${x.arn}')">取消</button>` : '' } })}
    <div class="row" style="margin-top:8px"><input id="alMail" placeholder="ops@anycompany.com" size="30"><button id="alSub">添加订阅</button>
      <span class="small muted">添加后收件人会收到一封确认邮件，点击确认才会开始接收告警。</span></div>
    <div id="alDryOut" style="margin-top:12px"></div>`;
  const collect = () => ({ enabled: $('#alEn').checked, lookback_days: +$('#alLb').value, webhook: { type: $('#alWt').value, url: $('#alWu').value.trim() },
    scenarios: Object.fromEntries($$('.alSc').map(c => { const th = $(`.alTh[data-id="${c.dataset.id}"]`); return [c.dataset.id, { enabled: c.checked, ...(th ? { threshold: +th.value } : {}) }]; })) });
  $('#alSave').onclick = e => guard(e.target, async () => { await PUT('/api/alerts/settings', collect()); toast('已保存'); });
  $('#alDry').onclick = e => guard(e.target, async () => { await PUT('/api/alerts/settings', collect()); const a = await POST('/api/alerts/run', { dry_run: true });
    $('#alDryOut').innerHTML = `<h4>预演结果：${a.hits.length ? `${a.hits.length} 类新增风险` : '无新增风险'}</h4>` + a.hits.map(h => `<div class="small"><b>${esc(h.name)}</b>：新增 ${h.new_count} 条</div>${table(h.columns, h.rows.slice(0, 5))}`).join('') + (a.errors || []).map(x => `<div class="small">${esc(x.name)} 失败：${esc(x.error)}</div>`).join(''); });
  $('#alRun').onclick = e => guard(e.target, async () => { if (!confirm('立即执行巡检并发送通知？')) return; await PUT('/api/alerts/settings', collect()); const r = await POST('/api/alerts/run', {}); toast(r.message); setTimeout(loadAlertHist, 45000); });
  $('#alSub').onclick = e => guard(e.target, async () => { const r = await POST('/api/alerts/subscriptions', { email: $('#alMail').value.trim() }); toast(r.message); loadAlertCfg(); });
}
async function alUnsub(arn) { if (!confirm('取消该邮箱的订阅？')) return; await guard(null, async () => { await POST('/api/alerts/unsubscribe', { arn }); loadAlertCfg(); }); }
