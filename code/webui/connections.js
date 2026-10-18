import './connections.css';

export function openConnections(ui) {
  const { openModal, icon, paintIcons, esc, toast, back } = ui;
  const modal = document.getElementById('modal');
  let model;
  const button = (symbol, label, attrs = '') => `<button type="button" class="icon-button" aria-label="${label}" data-tooltip="${label}" ${attrs}>${icon(symbol)}</button>`;
  const date = (value) => value ? new Date(value).toLocaleDateString('zh-CN') : '尚未使用';
  const labels = { active: '有效', expired: '已到期', revoked: '已撤销' };
  const header = (extra = '') => `<div class="connections-nav">${button('arrow-left', '返回设置', 'id="connections-back"')}<span></span>${extra}</div>`;

  function show(title, html) {
    openModal(title, html);
    modal.classList.add('connections-modal');
    modal.dataset.view = 'connections';
    document.getElementById('connections-back')?.addEventListener('click', back);
    paintIcons();
  }
  function active() { return modal.open && modal.dataset.view === 'connections'; }
  async function request(path, options = {}) {
    const response = await fetch('/api/connections/' + path, {
      credentials: 'same-origin', ...options,
      headers: { 'Content-Type': 'application/json', 'X-Mindbox-Request': '1' },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(body.error?.message || (response.status === 404 ? '连接管理暂不可用' : '请求失败，请重试'));
      error.status = response.status;
      throw error;
    }
    return body;
  }
  function errorText(error) {
    const target = document.getElementById('connections-error');
    if (target) { target.textContent = error.message; target.hidden = false; }
  }
  async function copy(value, target) {
    try { await navigator.clipboard.writeText(value); toast('已复制'); }
    catch { target?.focus(); target?.select(); toast('无法自动复制，请选择后复制'); }
  }
  function pairing() {
    show('个人 Agent', `${header()}<form id="agent-pair-form" class="form-grid connections-pair"><span class="connections-symbol">${icon('shield-check')}</span><p class="field-note">仅允许本机管理。生成一次性配对码后，在这里完成管理连接。</p><div class="connections-pair-actions"><button class="button" type="button" id="agent-generate-code">${icon('key-round')}生成本机配对码</button><span class="field-note">10 分钟有效</span></div><label class="field">本机配对码<input name="code" type="password" autocomplete="off" required minlength="40" maxlength="100" spellcheck="false"></label><p class="connections-error" id="connections-error" role="alert" hidden></p><div class="connections-submit"><span class="field-note">管理会话 · 8 小时</span><button class="button primary" type="submit">配对</button></div></form>`);
    document.getElementById('agent-generate-code').addEventListener('click', async (event) => {
      event.currentTarget.disabled = true;
      try {
        const result = await request('pairing-code', { method: 'POST' });
        const input = document.querySelector('#agent-pair-form input[name="code"]');
        input.type = 'text'; input.value = result.code; input.focus(); input.select();
        toast('配对码已生成，仅在本机显示');
      } catch (error) { errorText(error); event.currentTarget.disabled = false; }
    });
    document.getElementById('agent-pair-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const submit = form.querySelector('button[type="submit"]');
      submit.disabled = true;
      try {
        await request('pair', { method: 'POST', body: JSON.stringify({ code: form.elements.code.value.trim() }) });
        form.reset();
        if (active()) await load();
      } catch (error) { if (active()) errorText(error); }
      finally { submit.disabled = false; }
    });
  }
  function list() {
    const entries = model.connections;
    show('个人 Agent', `${header(button('unplug', '结束本机管理', 'id="agent-logout"'))}
      <div class="connections-endpoint"><span>本机接口</span><code>${esc(model.api_url)}</code>${button('copy', '复制接口地址', 'id="agent-copy-url"')}</div>
      <div class="connections-section-head"><h3>连接<span>${entries.filter((entry) => entry.status === 'active').length}</span></h3>${button('plus', '新建连接', 'id="agent-add"')}</div>
      <div class="connections-list">${entries.length ? entries.map((entry) => `<div class="connection-row ${entry.status !== 'active' ? 'is-inactive' : ''}"><span class="connection-icon">${icon('key-round')}</span><div class="connection-copy"><div class="connection-title"><strong>${esc(entry.name)}</strong><span class="connection-status ${entry.status === 'active' ? 'is-active' : ''}">${labels[entry.status]}</span></div><code>${esc(entry.prefix)}••••</code><small>${entry.scopes.map((scope) => ({ 'inventory:read': '库存', 'history:read': '记录', 'images:read': '照片' })[scope]).join(' · ')} · ${date(entry.expires_at)} 到期</small><small>最近使用 · ${date(entry.last_used_at)}</small></div>${entry.status === 'active' ? `<div class="connection-actions">${button('rotate-cw', '更新密钥', `data-rotate="${esc(entry.id)}"`)}${button('unplug', '撤销连接', `data-revoke="${esc(entry.id)}"`)}</div>` : ''}</div>`).join('') : `<div class="connections-empty">${icon('link-2')}<span>暂无连接</span></div>`}</div><p class="connections-error" id="connections-error" role="alert" hidden></p>`);
    document.getElementById('agent-add').addEventListener('click', create);
    document.getElementById('agent-copy-url').addEventListener('click', () => copy(model.api_url));
    document.getElementById('agent-logout').addEventListener('click', async () => {
      try { await request('logout', { method: 'POST' }); if (active()) pairing(); }
      catch (error) { errorText(error); }
    });
    modal.querySelectorAll('[data-revoke]').forEach((target) => target.addEventListener('click', () => revoke(target.dataset.revoke)));
    modal.querySelectorAll('[data-rotate]').forEach((target) => target.addEventListener('click', async () => {
      target.disabled = true;
      try {
        const result = await request(`keys/${target.dataset.rotate}/rotate`, { method: 'POST' });
        if (active()) secret(result, true);
      } catch (error) { errorText(error); }
      finally { target.disabled = false; }
    }));
  }
  function create() {
    show('新建连接', `${header()}<form class="form-grid" id="agent-create-form"><label class="field">连接名称<input name="name" placeholder="Muse" autocomplete="off" maxlength="80" required></label>
      <fieldset class="connections-fields"><legend>可访问设备</legend>${model.cabinets.map((cabinet, index) => `<label class="connection-option"><span>${esc(cabinet.name)}</span><input type="checkbox" name="cabinet" value="${esc(cabinet.id)}" ${index === 0 ? 'checked' : ''}></label>`).join('')}</fieldset>
      <fieldset class="connections-fields"><legend>读取权限</legend><label class="connection-option"><span>库存与位置</span><input type="checkbox" checked disabled></label><label class="connection-option"><span>取放记录</span><input type="checkbox" name="history"></label><label class="connection-option"><span>物品照片</span><input type="checkbox" name="images"></label></fieldset>
      <label class="field">有效期<select name="days"><option value="30">30 天</option><option value="90" selected>90 天</option><option value="180">180 天</option><option value="365">1 年</option></select></label><p class="connections-error" id="connections-error" role="alert" hidden></p><div class="modal-actions"><button class="button" type="button" id="agent-cancel">取消</button><button class="button primary" type="submit">生成密钥</button></div></form>`);
    document.getElementById('agent-cancel').addEventListener('click', list);
    document.getElementById('agent-create-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const submit = form.querySelector('button[type="submit"]');
      const values = new FormData(form);
      if (!values.getAll('cabinet').length) return errorText(new Error('请至少选择一台设备'));
      if (!values.get('name').trim()) return errorText(new Error('请输入连接名称'));
      submit.disabled = true;
      try {
        const result = await request('keys', { method: 'POST', body: JSON.stringify({
          name: values.get('name').trim(), cabinet_ids: values.getAll('cabinet'), days: Number(values.get('days')),
          scopes: ['inventory:read', ...(values.has('history') ? ['history:read'] : []), ...(values.has('images') ? ['images:read'] : [])],
        }) });
        if (active()) secret(result);
      } catch (error) { if (active()) errorText(error); }
      finally { submit.disabled = false; }
    });
  }
  function secret(result, rotated = false) {
    show('连接已创建', `${header()}<div class="connections-secret"><div class="connections-created">${icon('check')}<strong>${esc(result.connection.name)}</strong></div><label class="field">API Key<div class="connections-key"><textarea id="agent-secret" readonly rows="3" spellcheck="false" aria-label="API Key">${esc(result.key)}</textarea>${button('copy', '复制密钥', 'id="agent-copy-key"')}</div></label><p class="field-note">密钥仅显示这一次。${rotated ? '旧密钥仍有效，更新 Agent 后请撤销旧连接。' : ''}</p><div class="connections-endpoint"><span>接口地址</span><code>${esc(model.api_url)}</code>${button('copy', '复制接口地址', 'id="agent-copy-url"')}</div><div class="modal-actions"><button class="button primary" id="agent-done">完成</button></div></div>`);
    document.getElementById('agent-copy-key').addEventListener('click', () => copy(document.getElementById('agent-secret').value, document.getElementById('agent-secret')));
    document.getElementById('agent-copy-url').addEventListener('click', () => copy(model.api_url));
    document.getElementById('agent-done').addEventListener('click', load);
  }
  function revoke(identity) {
    const entry = model.connections.find((connection) => connection.id === identity);
    show('撤销连接', `${header()}<div class="connections-revoke"><strong>${esc(entry.name)}</strong><p class="field-note">此连接将无法继续读取物品信息。</p><p class="connections-error" id="connections-error" role="alert" hidden></p><div class="modal-actions"><button class="button" id="agent-cancel-revoke">取消</button><button class="button primary" id="agent-confirm-revoke">撤销连接</button></div></div>`);
    document.getElementById('agent-cancel-revoke').addEventListener('click', list);
    document.getElementById('agent-confirm-revoke').addEventListener('click', async (event) => {
      event.currentTarget.disabled = true;
      try { await request(`keys/${identity}/revoke`, { method: 'POST' }); if (active()) await load(); }
      catch (error) { errorText(error); event.target.disabled = false; }
    });
  }
  async function load() {
    show('个人 Agent', `${header()}<div class="connections-empty" role="status">正在读取连接…</div>`);
    try { model = await request('state'); if (active()) list(); }
    catch (error) {
      if (!active()) return;
      if (error.status === 401) return pairing();
      show('个人 Agent', `${header()}<p class="connections-error" role="alert">${esc(error.message)}</p><div class="modal-actions"><button class="button" id="agent-retry">重试</button></div>`);
      document.getElementById('agent-retry').addEventListener('click', load);
    }
  }
  load();
}
