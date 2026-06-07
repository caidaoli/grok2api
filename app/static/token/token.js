let apiKey = '';
let allTokens = {};
let flatTokens = [];
let tokenIndex = new Map();
let isBatchProcessing = false;
let isBatchPaused = false;
let batchQueue = [];
let batchTotal = 0;
let batchProcessed = 0;
let batchSuccess = 0;
let batchFailed = 0;
let batchAbortController = null;
let currentBatchAction = null;
let isWorkersRuntime = false;
let isNsfwRefreshAllRunning = false;

let displayTokens = [];
const filterState = {
  typeSso: false,
  typeSuperSso: false,
  statusActive: false,
  statusInvalid: false,
  statusExhausted: false,
  statusDisabled: false,
};

function normalizeSsoToken(token) {
  const v = String(token || '').trim();
  return v.startsWith('sso=') ? v.slice(4).trim() : v;
}

function poolToType(pool) {
  return String(pool || '').trim() === 'ssoSuper' ? 'ssoSuper' : 'sso';
}

function normalizeStatus(rawStatus) {
  const status = String(rawStatus || 'active').trim().toLowerCase();
  if (status === 'expired') return 'invalid';
  if (status === 'active' || status === 'cooling' || status === 'invalid' || status === 'disabled') return status;
  return 'active';
}

function parseQuotaValue(v) {
  if (v === null || v === undefined || v === '') return { value: -1, known: false };
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) return { value: -1, known: false };
  return { value: Math.floor(n), known: true };
}

function extractApiErrorMessage(payload, fallback = '请求失败') {
  if (!payload) return fallback;
  if (typeof payload === 'string' && payload.trim()) return payload.trim();
  if (typeof payload.detail === 'string' && payload.detail.trim()) return payload.detail.trim();
  if (typeof payload.error === 'string' && payload.error.trim()) return payload.error.trim();
  if (typeof payload.message === 'string' && payload.message.trim()) return payload.message.trim();
  if (payload.error && typeof payload.error.message === 'string' && payload.error.message.trim()) {
    return payload.error.message.trim();
  }
  return fallback;
}

async function parseJsonSafely(response) {
  try {
    return await response.json();
  } catch (e) {
    return null;
  }
}

function normalizeTokenRecord(pool, raw) {
  const tokenType = poolToType(pool);
  const isString = typeof raw === 'string';
  const source = isString ? { token: raw } : (raw || {});
  const token = normalizeSsoToken(source.token);
  if (!token) return null;

  const status = normalizeStatus(source.status);
  const quotaParsed = parseQuotaValue(source.quota);
  const heavyParsed = parseQuotaValue(source.heavy_quota);

  return {
    token,
    status,
    quota: quotaParsed.known ? quotaParsed.value : 0,
    quota_known: quotaParsed.known,
    heavy_quota: heavyParsed.known ? heavyParsed.value : -1,
    heavy_quota_known: heavyParsed.known,
    token_type: source.token_type || tokenType,
    note: source.note || '',
    fail_count: source.fail_count || 0,
    use_count: source.use_count || 0,
    pool: pool,
    _selected: false,
  };
}

function isTokenDisabled(item) {
  return String(item.status || '').toLowerCase() === 'disabled';
}

function isTokenInvalid(item) {
  return ['invalid', 'expired'].includes(String(item.status || '').toLowerCase());
}

function isTokenExhausted(item) {
  const status = String(item.status || '').toLowerCase();
  if (status === 'cooling') return true;
  if (Boolean(item.quota_known) && Number(item.quota) <= 0) return true;
  const tokenType = String(item.token_type || poolToType(item.pool));
  if (tokenType === 'ssoSuper' && Boolean(item.heavy_quota_known) && Number(item.heavy_quota) <= 0) return true;
  return false;
}

function isTokenActive(item) {
  return !isTokenInvalid(item) && !isTokenExhausted(item) && !isTokenDisabled(item);
}

function getTokenKey(token) {
  return normalizeSsoToken(token);
}

function rebuildTokenIndex() {
  tokenIndex = new Map();
  flatTokens.forEach((item, index) => {
    tokenIndex.set(getTokenKey(item.token), index);
  });
}

function findTokenIndexByKey(tokenKey) {
  const key = getTokenKey(tokenKey);
  return tokenIndex.has(key) ? tokenIndex.get(key) : -1;
}

function hasTokenKey(tokenKey) {
  return tokenIndex.has(getTokenKey(tokenKey));
}

function refreshFilterStateFromDom() {
  const getChecked = (id) => {
    const el = document.getElementById(id);
    return Boolean(el && el.checked);
  };
  filterState.typeSso = getChecked('filter-type-sso');
  filterState.typeSuperSso = getChecked('filter-type-supersso');
  filterState.statusActive = getChecked('filter-status-active');
  filterState.statusInvalid = getChecked('filter-status-invalid');
  filterState.statusExhausted = getChecked('filter-status-exhausted');
  filterState.statusDisabled = getChecked('filter-status-disabled');
}

function applyFilters() {
  refreshFilterStateFromDom();

  const hasTypeFilter = filterState.typeSso || filterState.typeSuperSso;
  const hasStatusFilter = filterState.statusActive || filterState.statusInvalid || filterState.statusExhausted || filterState.statusDisabled;

  displayTokens = flatTokens.filter((item) => {
    const tokenType = String(item.token_type || poolToType(item.pool));
    const matchesType = !hasTypeFilter
      || (filterState.typeSso && tokenType === 'sso')
      || (filterState.typeSuperSso && tokenType === 'ssoSuper');

    if (!matchesType) return false;
    if (!hasStatusFilter) return true;

    const active = isTokenActive(item);
    const invalid = isTokenInvalid(item);
    const exhausted = isTokenExhausted(item);
    const disabled = isTokenDisabled(item);
    return (filterState.statusActive && active)
      || (filterState.statusInvalid && invalid)
      || (filterState.statusExhausted && exhausted)
      || (filterState.statusDisabled && disabled);
  });

  const resultEl = document.getElementById('filter-result-count');
  if (resultEl) {
    resultEl.textContent = String(displayTokens.length);
  }
}

function applyLocalView() {
  updateStats();
  applyFilters();
  renderTable();
}

function onFilterChange() {
  applyLocalView();
}

function resetFilters() {
  ['filter-type-sso', 'filter-type-supersso', 'filter-status-active', 'filter-status-invalid', 'filter-status-exhausted', 'filter-status-disabled']
    .forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.checked = false;
    });
  applyLocalView();
}

function setNsfwRefreshUiEnabled(enabled) {
  const btn = document.getElementById('btn-refresh-nsfw-all');
  if (!btn) return;
  if (enabled) {
    btn.classList.remove('hidden');
  } else {
    btn.classList.add('hidden');
  }
}

async function detectWorkersRuntime() {
  try {
    const res = await fetch('/health', { cache: 'no-store' });
    if (!res.ok) return false;
    const text = await res.text();
    try {
      const data = JSON.parse(text);
      const runtime = (data && data.runtime) ? String(data.runtime) : '';
      return runtime.toLowerCase() === 'cloudflare-workers';
    } catch (e) {
      return /cloudflare-workers/i.test(text);
    }
  } catch (e) {
    return false;
  }
}

async function applyRuntimeUiFlags() {
  // Default hide first; show back for local/docker after detection.
  setNsfwRefreshUiEnabled(false);
  isWorkersRuntime = await detectWorkersRuntime();
  if (!isWorkersRuntime) {
    setNsfwRefreshUiEnabled(true);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', applyRuntimeUiFlags);
} else {
  applyRuntimeUiFlags();
}

async function init() {
  apiKey = await ensureApiKey();
  if (apiKey === null) return;
  setupConfirmDialog();
  loadData();
}

async function loadData() {
  try {
    const res = await fetch('/api/v1/admin/tokens', {
      headers: buildAuthHeaders(apiKey)
    });
    if (res.ok) {
      const data = await parseJsonSafely(res);
      allTokens = data;
      processTokens(data);
      applyLocalView();
    } else if (res.status === 401) {
      logout();
    } else {
      const payload = await parseJsonSafely(res);
      throw new Error(extractApiErrorMessage(payload, `HTTP ${res.status}`));
    }
  } catch (e) {
    showToast('加载失败: ' + e.message, 'error');
  }
}

// Convert pool dict to flattened array
function processTokens(data) {
  const prevSelected = new Set(flatTokens.filter(t => t._selected).map(t => getTokenKey(t.token)));
  flatTokens = [];
  const seen = new Set();

  Object.keys(data || {}).forEach(pool => {
    const tokens = data[pool];
    if (!Array.isArray(tokens)) return;

    tokens.forEach(t => {
      const row = normalizeTokenRecord(pool, t);
      if (!row) return;
      const dedupeKey = `${pool}:${getTokenKey(row.token)}`;
      if (seen.has(dedupeKey)) return;
      seen.add(dedupeKey);
      row._selected = prevSelected.has(getTokenKey(row.token));
      flatTokens.push(row);
    });
  });
  rebuildTokenIndex();
}

function updateStats() {
  let totalTokens = flatTokens.length;
  let activeTokens = 0;
  let coolingTokens = 0;
  let invalidTokens = 0;
  let disabledTokens = 0;
  let chatQuota = 0;
  let totalCalls = 0;

  flatTokens.forEach(t => {
    if (isTokenDisabled(t)) {
      disabledTokens++;
    } else if (isTokenInvalid(t)) {
      invalidTokens++;
    } else if (isTokenExhausted(t)) {
      coolingTokens++;
    } else {
      activeTokens++;
      if (Boolean(t.quota_known) && Number(t.quota) > 0) {
        chatQuota += Number(t.quota);
      }
    }
    totalCalls += Number(t.use_count || 0);
  });

  const imageQuota = Math.floor(chatQuota / 2);

  const setText = (id, text) => {
    const el = document.getElementById(id);
    if (el) el.innerText = text;
  };

  setText('stat-total', totalTokens.toLocaleString());
  setText('stat-active', activeTokens.toLocaleString());
  setText('stat-cooling', coolingTokens.toLocaleString());
  setText('stat-invalid', invalidTokens.toLocaleString());
  setText('stat-disabled', disabledTokens.toLocaleString());

  setText('stat-chat-quota', chatQuota.toLocaleString());
  setText('stat-image-quota', imageQuota.toLocaleString());
  setText('stat-total-calls', totalCalls.toLocaleString());
}

function renderTable() {
  const tbody = document.getElementById('token-table-body');
  const loading = document.getElementById('loading');
  const emptyState = document.getElementById('empty-state');

  tbody.innerHTML = '';
  loading.classList.add('hidden');

  if (flatTokens.length === 0) {
    emptyState.innerText = '暂无 Token，请点击右上角导入或添加。';
    emptyState.classList.remove('hidden');
    return;
  }
  if (displayTokens.length === 0) {
    emptyState.innerText = '当前筛选无结果。';
    emptyState.classList.remove('hidden');
    updateSelectionState();
    return;
  }
  emptyState.innerText = '暂无 Token，请点击右上角导入或添加。';
  emptyState.classList.add('hidden');

  const fragment = document.createDocumentFragment();
  displayTokens.forEach((item) => {
    const tr = document.createElement('tr');
    const tokenKey = getTokenKey(item.token);
    const tokenEncoded = encodeURIComponent(item.token);
    const tokenKeyEncoded = encodeURIComponent(tokenKey);

    // Checkbox (Center)
    const tdCheck = document.createElement('td');
    tdCheck.className = 'text-center';
    tdCheck.innerHTML = `<input type="checkbox" class="checkbox" ${item._selected ? 'checked' : ''} onchange="toggleSelectByKey(decodeURIComponent('${tokenKeyEncoded}'))">`;

    // Token (Left)
    const tdToken = document.createElement('td');
    tdToken.className = 'text-left';
    const tokenShort = item.token.length > 24
      ? item.token.substring(0, 8) + '...' + item.token.substring(item.token.length - 16)
      : item.token;
    tdToken.innerHTML = `
                <div class="flex items-center gap-2">
                    <span class="font-mono text-xs text-gray-500" title="${item.token}">${tokenShort}</span>
                    <button class="text-gray-400 hover:text-black transition-colors" onclick="copyToClipboard(decodeURIComponent('${tokenEncoded}'), this)">
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
                    </button>
                </div>
             `;

    // Type (Center)
    const tdType = document.createElement('td');
    tdType.className = 'text-center';
    tdType.innerHTML = `<span class="badge badge-gray">${escapeHtml(item.pool)}</span>`;

    // Status (Center)
    const tdStatus = document.createElement('td');
    let statusClass = 'badge-gray';
    let statusText = 'active';
    if (isTokenDisabled(item)) {
      statusClass = 'badge-gray';
      statusText = 'disabled';
    } else if (isTokenActive(item)) {
      statusClass = 'badge-green';
      statusText = 'active';
    } else if (isTokenExhausted(item)) {
      statusClass = 'badge-orange';
      statusText = 'exhausted';
    } else {
      statusClass = 'badge-red';
      statusText = 'invalid';
    }
    tdStatus.className = 'text-center';
    tdStatus.innerHTML = `<span class="badge ${statusClass}">${statusText}</span>`;

    // Quota (Center)
    const tdQuota = document.createElement('td');
    tdQuota.className = 'text-center font-mono text-xs';
    tdQuota.innerText = item.quota_known ? String(item.quota) : '-';

    // Note (Left)
    const tdNote = document.createElement('td');
    tdNote.className = 'text-left text-gray-500 text-xs truncate max-w-[150px]';
    tdNote.innerText = item.note || '-';

    // Actions (Center)
    const tdActions = document.createElement('td');
    tdActions.className = 'text-center';
    const resetBtn = isTokenDisabled(item) ? `
                     <button onclick="resetToken(decodeURIComponent('${tokenEncoded}'), this)" class="p-1 text-gray-400 hover:text-green-600 rounded" title="恢复">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"></path><path d="M3 3v5h5"></path></svg>
                     </button>` : '';
    tdActions.innerHTML = `
                <div class="flex items-center justify-center gap-2">
                     <button onclick="refreshStatus(decodeURIComponent('${tokenEncoded}'), this)" class="p-1 text-gray-400 hover:text-black rounded" title="刷新状态">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>
                     </button>${resetBtn}
                     <button onclick="openEditModalByKey(decodeURIComponent('${tokenKeyEncoded}'))" class="p-1 text-gray-400 hover:text-black rounded" title="编辑">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
                     </button>
                     <button onclick="deleteTokenByKey(decodeURIComponent('${tokenKeyEncoded}'))" class="p-1 text-gray-400 hover:text-red-600 rounded" title="删除">
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                     </button>
                </div>
             `;

    tr.appendChild(tdCheck);
    tr.appendChild(tdToken);
    tr.appendChild(tdType);
    tr.appendChild(tdStatus);
    tr.appendChild(tdQuota);
    tr.appendChild(tdNote);
    tr.appendChild(tdActions);

    fragment.appendChild(tr);
  });
  tbody.appendChild(fragment);

  updateSelectionState();
}

// Selection Logic
function toggleSelectAll() {
  const checkbox = document.getElementById('select-all');
  const checked = checkbox.checked;
  const visibleKeys = new Set(displayTokens.map((t) => getTokenKey(t.token)));
  flatTokens.forEach((t) => {
    if (visibleKeys.has(getTokenKey(t.token))) {
      t._selected = checked;
    }
  });
  renderTable();
}

function toggleSelectByKey(tokenKey) {
  const idx = findTokenIndexByKey(tokenKey);
  if (idx < 0) return;
  flatTokens[idx]._selected = !flatTokens[idx]._selected;
  updateSelectionState();
}

function updateSelectionState() {
  const selectedCount = flatTokens.filter(t => t._selected).length;
  const allSelected = displayTokens.length > 0 && displayTokens.every((t) => t._selected);

  const selectAll = document.getElementById('select-all');
  if (selectAll) selectAll.checked = allSelected;
  document.getElementById('selected-count').innerText = selectedCount;
  setActionButtonsState();
}

function buildTokenRecord(token, pool, options = {}) {
  return {
    token: normalizeSsoToken(token),
    pool: pool || 'ssoBasic',
    status: options.status || 'active',
    quota: Number.isFinite(Number(options.quota)) ? Number(options.quota) : 80,
    quota_known: true,
    heavy_quota: Number.isFinite(Number(options.heavy_quota)) ? Number(options.heavy_quota) : -1,
    heavy_quota_known: Number.isFinite(Number(options.heavy_quota)) && Number(options.heavy_quota) >= 0,
    token_type: poolToType(pool),
    note: options.note || '',
    fail_count: Number(options.fail_count || 0),
    use_count: Number(options.use_count || 0),
    _selected: false
  };
}

function mergeTokenRecords(records) {
  if (!Array.isArray(records) || records.length === 0) return false;
  let changed = false;
  records.forEach((record) => {
    const pool = record.pool || record.pool_name || 'ssoBasic';
    const row = normalizeTokenRecord(pool, record);
    if (!row) return;
    const key = getTokenKey(row.token);
    const idx = findTokenIndexByKey(key);
    if (idx >= 0) {
      row._selected = Boolean(flatTokens[idx]._selected);
      flatTokens[idx] = row;
    } else {
      flatTokens.push(row);
    }
    changed = true;
  });
  if (changed) {
    rebuildTokenIndex();
    applyLocalView();
  }
  return changed;
}

async function addTokensToServer(pool, records) {
  if (!Array.isArray(records) || records.length === 0) {
    return { status: 'success', added: 0, skipped: 0, tokens: [] };
  }
  try {
    const res = await fetch('/api/v1/admin/tokens/import', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...buildAuthHeaders(apiKey)
      },
      body: JSON.stringify({ pool, tokens: records })
    });
    const payload = await parseJsonSafely(res);
    if (!res.ok) {
      showToast(extractApiErrorMessage(payload, '导入失败'), 'error');
      return null;
    }
    const triggered = Number(payload?.nsfw_refresh?.triggered || 0);
    if (triggered > 0) {
      showToast(`已后台触发 ${triggered} 个 Token 的协议/年龄/NSFW 刷新`, 'info');
    }
    return payload || { status: 'success', added: 0, skipped: 0, tokens: [] };
  } catch (e) {
    showToast('导入错误: ' + e.message, 'error');
    return null;
  }
}

// Actions
function addToken() {
  openAddModal();
}

// Batch export (Selected only)
function batchExport() {
  const selected = flatTokens.filter(t => t._selected);
  if (selected.length === 0) return showToast('未选择 Token', 'error');
  let content = "";
  selected.forEach(t => content += t.token + "\n");
  const blob = new Blob([content], { type: 'text/plain' });
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `tokens_export_selected_${new Date().toISOString().slice(0, 10)}.txt`;
  document.body.appendChild(a);
  a.click();
  window.URL.revokeObjectURL(url);
  document.body.removeChild(a);
}


// Add Modal
function openAddModal() {
  const modal = document.getElementById('add-modal');
  if (!modal) return;
  modal.classList.remove('hidden');
  requestAnimationFrame(() => {
    modal.classList.add('is-open');
  });
}

function closeAddModal() {
  const modal = document.getElementById('add-modal');
  if (!modal) return;
  modal.classList.remove('is-open');
  setTimeout(() => {
    modal.classList.add('hidden');
    resetAddModal();
  }, 200);
}

function resetAddModal() {
  const tokenInput = document.getElementById('add-token-input');
  const noteInput = document.getElementById('add-token-note');
  const quotaInput = document.getElementById('add-token-quota');
  if (tokenInput) tokenInput.value = '';
  if (noteInput) noteInput.value = '';
  if (quotaInput) quotaInput.value = 80;
}

async function submitManualAdd() {
  const tokenInput = document.getElementById('add-token-input');
  const poolSelect = document.getElementById('add-token-pool');
  const quotaInput = document.getElementById('add-token-quota');
  const noteInput = document.getElementById('add-token-note');

  if (!tokenInput) return;
  let token = normalizeSsoToken(tokenInput.value.trim());
  if (!token) return showToast('Token 不能为空', 'error');

  if (hasTokenKey(token)) {
    return showToast('Token 已存在', 'error');
  }

  const pool = poolSelect ? (poolSelect.value.trim() || 'ssoBasic') : 'ssoBasic';
  let quota = quotaInput ? parseInt(quotaInput.value, 10) : 80;
  if (!quota || Number.isNaN(quota)) quota = 80;
  const note = noteInput ? noteInput.value.trim().slice(0, 50) : '';

  const payload = await addTokensToServer(pool, [buildTokenRecord(token, pool, { quota, note })]);
  if (!payload) return;
  mergeTokenRecords(payload.tokens || []);
  closeAddModal();
  showToast(Number(payload.added || 0) > 0 ? '添加成功' : 'Token 已存在', Number(payload.added || 0) > 0 ? 'success' : 'info');
}


// Modal Logic
let currentEditIndex = -1;

function openEditModalByKey(tokenKey) {
  const idx = findTokenIndexByKey(tokenKey);
  if (idx < 0) return;
  openEditModal(idx);
}

function deleteTokenByKey(tokenKey) {
  const idx = findTokenIndexByKey(tokenKey);
  if (idx < 0) return;
  deleteToken(idx);
}

function openEditModal(index) {
  const modal = document.getElementById('edit-modal');
  if (!modal) return;

  currentEditIndex = index;

  if (index >= 0) {
    // Edit existing
    const item = flatTokens[index];
    document.getElementById('edit-token-display').value = item.token;
    document.getElementById('edit-original-token').value = item.token;
    document.getElementById('edit-original-pool').value = item.pool;
    document.getElementById('edit-pool').value = item.pool;
    document.getElementById('edit-quota').value = item.quota;
    document.getElementById('edit-note').value = item.note;
    document.querySelector('#edit-modal h3').innerText = '编辑 Token';
  } else {
    // New Token
    document.getElementById('edit-token-display').value = '';
    document.getElementById('edit-token-display').disabled = false;
    document.getElementById('edit-token-display').placeholder = 'sk-...';
    document.getElementById('edit-token-display').classList.remove('bg-gray-50', 'text-gray-500');

    document.getElementById('edit-original-token').value = '';
    document.getElementById('edit-original-pool').value = '';
    document.getElementById('edit-pool').value = 'ssoBasic';
    document.getElementById('edit-quota').value = 80;
    document.getElementById('edit-note').value = '';
    document.querySelector('#edit-modal h3').innerText = '添加 Token';
  }

  modal.classList.remove('hidden');
  requestAnimationFrame(() => {
    modal.classList.add('is-open');
  });
}

function closeEditModal() {
  const modal = document.getElementById('edit-modal');
  if (!modal) return;
  modal.classList.remove('is-open');
  setTimeout(() => {
    modal.classList.add('hidden');
    // reset styles for token input
    const input = document.getElementById('edit-token-display');
    if (input) {
      input.disabled = true;
      input.classList.add('bg-gray-50', 'text-gray-500');
    }
  }, 200);
}

async function saveEdit() {
  // Collect data
  let token;
  const newPool = document.getElementById('edit-pool').value.trim();
  const newQuota = parseInt(document.getElementById('edit-quota').value) || 0;
  const newNote = document.getElementById('edit-note').value.trim().slice(0, 50);

  if (currentEditIndex >= 0) {
    // Updating existing
    const item = flatTokens[currentEditIndex];
    token = item.token;

    // Update flatTokens first to reflect UI
    item.pool = newPool || 'ssoBasic';
    item.quota = newQuota;
    item.quota_known = true;
    item.token_type = poolToType(item.pool);
    item.note = newNote;
  } else {
    // Creating new
    token = normalizeSsoToken(document.getElementById('edit-token-display').value.trim());
    if (!token) return showToast('Token 不能为空', 'error');

    // Check if exists
    if (hasTokenKey(token)) {
      return showToast('Token 已存在', 'error');
    }

    flatTokens.push({
      token: token,
      pool: newPool || 'ssoBasic',
      quota: newQuota,
      quota_known: true,
      heavy_quota: -1,
      heavy_quota_known: false,
      token_type: poolToType(newPool || 'ssoBasic'),
      note: newNote,
      status: 'active', // default
      use_count: 0,
      _selected: false
    });
  }

  const payload = await syncToServer();
  if (!payload) return;
  rebuildTokenIndex();
  closeEditModal();
  applyLocalView();
}

async function deleteToken(index) {
  const ok = await confirmAction('确定要删除此 Token 吗？', { okText: '删除' });
  if (!ok) return;
  const item = flatTokens[index];
  if (!item) return;
  const payload = await deleteTokensFromServer([item.token]);
  if (!payload) return;
  const tokenKey = normalizeSsoToken(item.token);
  flatTokens = flatTokens.filter(t => normalizeSsoToken(t.token) !== tokenKey);
  rebuildTokenIndex();
  applyLocalView();
}

function batchDelete() {
  startBatchDelete();
}

// Reconstruct object structure and save
async function syncToServer() {
  const newTokens = {};
  flatTokens.forEach(t => {
    if (!newTokens[t.pool]) newTokens[t.pool] = [];
    newTokens[t.pool].push({
      token: normalizeSsoToken(t.token),
      status: t.status,
      quota: t.quota,
      heavy_quota: t.heavy_quota,
      note: t.note,
      fail_count: t.fail_count,
      use_count: t.use_count || 0
    });
  });

  try {
    const res = await fetch('/api/v1/admin/tokens', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...buildAuthHeaders(apiKey)
      },
      body: JSON.stringify(newTokens)
    });
    const payload = await parseJsonSafely(res);
    if (!res.ok) {
      showToast(extractApiErrorMessage(payload, '保存失败'), 'error');
      return null;
    }

    const triggered = Number(payload?.nsfw_refresh?.triggered || 0);
    if (triggered > 0) {
      showToast(`已后台触发 ${triggered} 个 Token 的协议/年龄/NSFW 刷新`, 'info');
    }
    return payload;
  } catch (e) {
    showToast('保存错误: ' + e.message, 'error');
    return null;
  }
}

async function deleteTokensFromServer(tokens) {
  const normalized = Array.from(new Set(
    (tokens || []).map(t => normalizeSsoToken(t)).filter(t => t)
  ));
  if (normalized.length === 0) return { status: 'success', deleted: 0 };

  try {
    const res = await fetch('/api/v1/admin/tokens/delete', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...buildAuthHeaders(apiKey)
      },
      body: JSON.stringify({ tokens: normalized })
    });
    const payload = await parseJsonSafely(res);
    if (!res.ok) {
      showToast(extractApiErrorMessage(payload, '删除失败'), 'error');
      return null;
    }
    return payload || { status: 'success', deleted: 0 };
  } catch (e) {
    showToast('删除错误: ' + e.message, 'error');
    return null;
  }
}

// Import Logic
function openImportModal() {
  const modal = document.getElementById('import-modal');
  if (!modal) return;
  modal.classList.remove('hidden');
  requestAnimationFrame(() => {
    modal.classList.add('is-open');
  });
}

function closeImportModal() {
  const modal = document.getElementById('import-modal');
  if (!modal) return;
  modal.classList.remove('is-open');
  setTimeout(() => {
    modal.classList.add('hidden');
    const input = document.getElementById('import-text');
    if (input) input.value = '';
  }, 200);
}

async function submitImport() {
  const pool = document.getElementById('import-pool').value.trim() || 'ssoBasic';
  const text = document.getElementById('import-text').value;
  const lines = text.split('\n');
  const seen = new Set(tokenIndex.keys());
  const records = [];

  lines.forEach(line => {
    const t = normalizeSsoToken(line.trim());
    if (t && !seen.has(t)) {
      seen.add(t);
      records.push(buildTokenRecord(t, pool));
    }
  });

  if (records.length === 0) {
    showToast('没有可导入的新 Token', 'info');
    return;
  }

  const payload = await addTokensToServer(pool, records);
  if (!payload) return;
  mergeTokenRecords(payload.tokens || []);
  closeImportModal();
  showToast(`导入完成：新增 ${Number(payload.added || 0)} 个，跳过 ${Number(payload.skipped || 0)} 个`, 'success');
}

// Export Logic
function exportTokens() {
  let content = "";
  flatTokens.forEach(t => content += t.token + "\n");
  if (!content) return showToast('列表为空', 'error');

  const blob = new Blob([content], { type: 'text/plain' });
  const url = window.URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `tokens_export_${new Date().toISOString().slice(0, 10)}.txt`;
  document.body.appendChild(a);
  a.click();
  window.URL.revokeObjectURL(url);
  document.body.removeChild(a);
}

async function copyToClipboard(text, btn) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    const originalHtml = btn.innerHTML;
    btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
    btn.classList.remove('text-gray-400');
    btn.classList.add('text-green-500');
    setTimeout(() => {
      btn.innerHTML = originalHtml;
      btn.classList.add('text-gray-400');
      btn.classList.remove('text-green-500');
    }, 2000);
  } catch (err) {
    console.error('Copy failed', err);
  }
}

async function refreshStatus(token, btnEl) {
  try {
    const btn = btnEl || null;
    if (btn) {
      btn.innerHTML = `<svg class="animate-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.219-8.56"></path></svg>`;
    }

    const normalized = normalizeSsoToken(token);
    const res = await fetch('/api/v1/admin/tokens/refresh', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...buildAuthHeaders(apiKey)
      },
      body: JSON.stringify({ token: normalized })
    });

    const data = await parseJsonSafely(res);

    if (res.ok && data && data.status === 'success') {
      const results = data.results || {};
      const isSuccess = Boolean(results[normalized] ?? results[`sso=${normalized}`]);
      mergeTokenRecords(data.tokens || []);

      if (isSuccess) {
        showToast('刷新成功', 'success');
      } else {
        showToast('刷新失败', 'error');
      }
    } else {
      showToast(extractApiErrorMessage(data, '刷新失败'), 'error');
    }
  } catch (e) {
    console.error(e);
    showToast(e?.message ? `请求错误: ${e.message}` : '请求错误', 'error');
  }
}

async function resetToken(token, btnEl) {
  try {
    const btn = btnEl || null;
    if (btn) {
      btn.innerHTML = `<svg class="animate-spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.219-8.56"></path></svg>`;
    }

    const normalized = normalizeSsoToken(token);
    const res = await fetch('/api/v1/admin/tokens/reset', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...buildAuthHeaders(apiKey)
      },
      body: JSON.stringify({ token: normalized })
    });

    const data = await parseJsonSafely(res);

    if (res.ok && data && data.status === 'success') {
      const isSuccess = Boolean(data.results[normalized] ?? data.results[`sso=${normalized}`]);
      mergeTokenRecords(data.tokens || []);

      if (isSuccess) {
        showToast('恢复成功', 'success');
      } else {
        showToast('恢复失败：Token 未找到', 'error');
      }
    } else {
      showToast(extractApiErrorMessage(data, '恢复失败'), 'error');
    }
  } catch (e) {
    console.error(e);
    showToast(e?.message ? `请求错误: ${e.message}` : '请求错误', 'error');
  }
}

async function refreshAllNsfw() {
  if (isNsfwRefreshAllRunning) {
    showToast('NSFW 刷新任务进行中', 'info');
    return;
  }

  const ok = await confirmAction(
    '将对全部 Token 执行：同意用户协议 + 设置年龄 + 开启 NSFW。未成功的 Token 会自动标记为失效，是否继续？',
    { okText: '开始刷新' }
  );
  if (!ok) return;

  const btn = document.getElementById('btn-refresh-nsfw-all');
  const originalText = btn ? btn.innerHTML : '';
  isNsfwRefreshAllRunning = true;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '刷新中...';
  }

  try {
    const res = await fetch('/api/v1/admin/tokens/nsfw/refresh', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...buildAuthHeaders(apiKey)
      },
      body: JSON.stringify({ all: true })
    });

    const payload = await parseJsonSafely(res);
    if (!res.ok) {
      showToast(extractApiErrorMessage(payload, 'NSFW 刷新失败'), 'error');
      return;
    }

    const summary = payload?.summary || {};
    const total = Number(summary.total || 0);
    const success = Number(summary.success || 0);
    const failed = Number(summary.failed || 0);
    const invalidated = Number(summary.invalidated || 0);
    showToast(
      `NSFW 刷新完成：总计 ${total}，成功 ${success}，失败 ${failed}，失效 ${invalidated}`,
      failed > 0 ? 'info' : 'success'
    );
    loadData();
  } catch (e) {
    showToast(e?.message ? `NSFW 刷新失败: ${e.message}` : 'NSFW 刷新失败', 'error');
  } finally {
    isNsfwRefreshAllRunning = false;
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = originalText || '一键刷新 NSFW';
    }
  }
}

async function startBatchRefresh() {
  if (isBatchProcessing) {
    showToast('当前有任务进行中', 'info');
    return;
  }

  const selected = flatTokens.filter(t => t._selected);
  if (selected.length === 0) return showToast('未选择 Token', 'error');

  // Init state
  isBatchProcessing = true;
  isBatchPaused = false;
  currentBatchAction = 'refresh';
  batchQueue = Array.from(new Set(selected.map(t => normalizeSsoToken(t.token)).filter(t => t)));
  batchTotal = batchQueue.length;
  batchProcessed = 0;
  batchSuccess = 0;
  batchFailed = 0;

  updateBatchProgress();
  setActionButtonsState();
  processBatchQueue();
}

async function processBatchQueue() {
  if (!isBatchProcessing || isBatchPaused || currentBatchAction !== 'refresh') return;

  if (batchQueue.length === 0) {
    // Done
    finishBatchProcess();
    return;
  }

  const chunk = batchQueue.splice(0, batchQueue.length);
  batchAbortController = new AbortController();

  try {
    const response = await fetch('/api/v1/admin/tokens/refresh/stream', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...buildAuthHeaders(apiKey)
      },
      body: JSON.stringify({ tokens: chunk }),
      signal: batchAbortController.signal
    });

    if (!response.ok) {
      const payload = await parseJsonSafely(response);
      markRemainingBatchRefreshFailed();
      updateBatchProgress();
      showToast(`刷新失败: ${extractApiErrorMessage(payload, '请求失败')}`, 'error');
      finishBatchProcess(true);
      return;
    }

    if (!response.body) {
      markRemainingBatchRefreshFailed();
      updateBatchProgress();
      showToast('刷新失败: 浏览器不支持流式进度', 'error');
      finishBatchProcess(true);
      return;
    }

    await readBatchRefreshStream(response);
  } catch (e) {
    if (e && e.name === 'AbortError') {
      if (isBatchProcessing) finishBatchProcess(true);
      return;
    }
    showToast(e?.message ? `刷新失败: ${e.message}` : '网络请求错误', 'error');
    markRemainingBatchRefreshFailed();
    updateBatchProgress();
    finishBatchProcess(true);
    return;
  } finally {
    batchAbortController = null;
  }

  if (!isBatchProcessing || isBatchPaused) return;
  finishBatchProcess();
}

async function readBatchRefreshStream(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = consumeBatchRefreshSseBuffer(buffer);
  }

  buffer += decoder.decode();
  if (buffer.trim()) {
    consumeBatchRefreshSseBuffer(`${buffer}\n\n`);
  }
}

function consumeBatchRefreshSseBuffer(buffer) {
  let rest = buffer;
  let splitAt = rest.indexOf('\n\n');
  while (splitAt >= 0) {
    const block = rest.slice(0, splitAt);
    rest = rest.slice(splitAt + 2);
    handleBatchRefreshSseBlock(block);
    splitAt = rest.indexOf('\n\n');
  }
  return rest;
}

function handleBatchRefreshSseBlock(block) {
  const data = String(block || '')
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => line.startsWith('data:'))
    .map(line => line.slice(5).trim())
    .join('\n');
  if (!data) return;

  try {
    applyBatchRefreshProgress(JSON.parse(data));
  } catch (e) {
    console.error('Invalid refresh progress event', e);
  }
}

function applyBatchRefreshProgress(event) {
  if (!event || typeof event !== 'object') return;

  if (Number.isFinite(Number(event.total))) batchTotal = Number(event.total);
  if (Number.isFinite(Number(event.current))) batchProcessed = Number(event.current);
  if (Number.isFinite(Number(event.success))) batchSuccess = Number(event.success);
  if (Number.isFinite(Number(event.failed))) batchFailed = Number(event.failed);

  if (event.record) {
    mergeTokenRecords([event.record]);
  } else if (Array.isArray(event.tokens) && event.tokens.length > 0) {
    mergeTokenRecords(event.tokens);
  }

  updateBatchProgress();
}

function markRemainingBatchRefreshFailed() {
  const remaining = Math.max(0, batchTotal - batchProcessed);
  batchProcessed += remaining;
  batchFailed += remaining;
}

function toggleBatchPause() {
  if (!isBatchProcessing) return;
  if (currentBatchAction === 'refresh') {
    showToast('流式刷新不支持暂停，可直接终止任务', 'info');
    return;
  }
  isBatchPaused = !isBatchPaused;
  updateBatchProgress();
  if (!isBatchPaused && currentBatchAction === 'refresh') {
    processBatchQueue();
  }
}

function stopBatchRefresh() {
  if (!isBatchProcessing) return;
  if (batchAbortController) {
    batchAbortController.abort();
  }
  finishBatchProcess(true);
}

function finishBatchProcess(aborted = false) {
  const action = currentBatchAction;
  isBatchProcessing = false;
  isBatchPaused = false;
  batchQueue = [];
  batchAbortController = null;
  currentBatchAction = null;

  updateBatchProgress();
  setActionButtonsState();
  updateSelectionState();

  if (aborted) {
    showToast(action === 'delete' ? '已终止删除' : '已终止刷新', 'info');
  } else {
    if (action === 'refresh') {
      showToast(`刷新完成：成功 ${batchSuccess}，失败 ${batchFailed}`, batchFailed > 0 ? 'info' : 'success');
    } else {
      showToast('删除完成', 'success');
    }
  }
}

async function batchUpdate() {
  startBatchRefresh();
}

function updateBatchProgress() {
  const container = document.getElementById('batch-progress');
  const text = document.getElementById('batch-progress-text');
  const fill = document.getElementById('batch-progress-bar-fill');
  const pauseBtn = document.getElementById('btn-pause-action');
  const stopBtn = document.getElementById('btn-stop-action');
  if (!container || !text) return;
  if (!isBatchProcessing) {
    container.classList.add('hidden');
    if (fill) fill.style.width = '0%';
    if (pauseBtn) pauseBtn.classList.add('hidden');
    if (stopBtn) stopBtn.classList.add('hidden');
    return;
  }
  const pct = batchTotal ? Math.floor((batchProcessed / batchTotal) * 100) : 0;
  if (fill) fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  if (currentBatchAction === 'refresh') {
    text.textContent = `${batchProcessed}/${batchTotal} (${pct}%) · 成功 ${batchSuccess} · 失败 ${batchFailed}`;
  } else {
    text.textContent = `${batchProcessed}/${batchTotal} (${pct}%)`;
  }
  container.classList.remove('hidden');
  if (pauseBtn) {
    if (currentBatchAction === 'refresh') {
      pauseBtn.classList.add('hidden');
    } else {
      pauseBtn.textContent = isBatchPaused ? '继续' : '暂停';
      pauseBtn.classList.remove('hidden');
    }
  }
  if (stopBtn) stopBtn.classList.remove('hidden');
}

function setActionButtonsState() {
  const selectedCount = flatTokens.filter(t => t._selected).length;
  const disabled = isBatchProcessing;
  const exportBtn = document.getElementById('btn-batch-export');
  const updateBtn = document.getElementById('btn-batch-update');
  const deleteBtn = document.getElementById('btn-batch-delete');
  if (exportBtn) exportBtn.disabled = disabled || selectedCount === 0;
  if (updateBtn) updateBtn.disabled = disabled || selectedCount === 0;
  if (deleteBtn) deleteBtn.disabled = disabled || selectedCount === 0;
}

async function startBatchDelete() {
  if (isBatchProcessing) {
    showToast('当前有任务进行中', 'info');
    return;
  }
  const selected = flatTokens.filter(t => t._selected);
  if (selected.length === 0) return showToast('未选择 Token', 'error');
  const ok = await confirmAction(`确定要删除选中的 ${selected.length} 个 Token 吗？`, { okText: '删除' });
  if (!ok) return;

  isBatchProcessing = true;
  isBatchPaused = false;
  currentBatchAction = 'delete';
  batchTotal = selected.length;
  batchProcessed = 0;
  batchSuccess = 0;
  batchFailed = 0;

  updateBatchProgress();
  setActionButtonsState();

  const toRemove = new Set(selected.map(t => normalizeSsoToken(t.token)));

  const payload = await deleteTokensFromServer(Array.from(toRemove));
  if (!payload) {
    finishBatchProcess(true);
    return;
  }

  flatTokens = flatTokens.filter(t => !toRemove.has(normalizeSsoToken(t.token)));
  rebuildTokenIndex();
  applyLocalView();
  batchProcessed = batchTotal;
  finishBatchProcess();
}

let confirmResolver = null;

function setupConfirmDialog() {
  const dialog = document.getElementById('confirm-dialog');
  if (!dialog) return;
  const okBtn = document.getElementById('confirm-ok');
  const cancelBtn = document.getElementById('confirm-cancel');
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog) {
      closeConfirm(false);
    }
  });
  if (okBtn) okBtn.addEventListener('click', () => closeConfirm(true));
  if (cancelBtn) cancelBtn.addEventListener('click', () => closeConfirm(false));
}

function confirmAction(message, options = {}) {
  const dialog = document.getElementById('confirm-dialog');
  if (!dialog) {
    return Promise.resolve(false);
  }
  const messageEl = document.getElementById('confirm-message');
  const okBtn = document.getElementById('confirm-ok');
  const cancelBtn = document.getElementById('confirm-cancel');
  if (messageEl) messageEl.textContent = message;
  if (okBtn) okBtn.textContent = options.okText || '确定';
  if (cancelBtn) cancelBtn.textContent = options.cancelText || '取消';
  return new Promise(resolve => {
    confirmResolver = resolve;
    dialog.classList.remove('hidden');
    requestAnimationFrame(() => {
      dialog.classList.add('is-open');
    });
  });
}

function closeConfirm(ok) {
  const dialog = document.getElementById('confirm-dialog');
  if (!dialog) return;
  dialog.classList.remove('is-open');
  setTimeout(() => {
    dialog.classList.add('hidden');
    if (confirmResolver) {
      confirmResolver(ok);
      confirmResolver = null;
    }
  }, 200);
}


function escapeHtml(text) {
  if (!text) return '';
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}



window.onload = init;
