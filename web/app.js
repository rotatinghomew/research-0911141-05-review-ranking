const state = {context: null, current: null, frozen: false, snapshot: null, exclusions: [], dataset: null};
const element = identifier => document.getElementById(identifier);
const reliabilityText = {sufficient: '证据充分', limited: '证据有限', insufficient: '证据不足'};
const reviewerReliabilityText = {sufficient: '足够', limited: '偏少', insufficient: '不足'};

async function request(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || '请求失败');
  return value;
}

function message(value, error = false) {
  element('status').textContent = value;
  element('status').className = error ? 'error' : '';
}

async function guarded(action) {
  try { await action(); } catch (error) { message(error.message, true); }
}

function displayValue(value) {
  if (value === null || value === undefined) return '未观测';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(4);
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function make(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function readConfig() {
  const weights = {};
  for (const input of document.querySelectorAll('.weight-input')) {
    weights[input.dataset.criterion] = Number(input.value);
  }
  const config = {
    weights,
    exclude_conflicts: element('exclude-conflicts').checked,
    excluded_pairs: (state.exclusions || []).map(pair => ({...pair})),
  };
  const topK = Number(element('top-k').value);
  if (Number.isInteger(topK) && topK >= 1) config.top_k = topK;
  return config;
}

function selected() {
  const selection = {};
  for (const control of state.context.product.options) selection[control[0]] = element('select-' + control[0]).value;
  return {revision: element('revision').value, selection, ranking_config: readConfig()};
}

function renderControls() {
  const revision = element('revision').value || state.context.default_revision;
  element('revision').replaceChildren();
  for (const dataset of state.context.datasets) {
    const option = document.createElement('option');
    option.value = dataset.revision;
    option.textContent = dataset.label + ' · ' + dataset.revision.slice(0, 9);
    element('revision').append(option);
  }
  element('revision').value = revision;
  if (!element('revision').value) element('revision').value = state.context.default_revision;
  if (element('controls').children.length) return;
  for (const [key, labelText, choices] of state.context.product.options) {
    const label = document.createElement('label');
    label.textContent = labelText;
    const select = document.createElement('select');
    select.id = 'select-' + key;
    for (const choice of choices) {
      const option = document.createElement('option');
      option.value = choice; option.textContent = choice;
      select.append(option);
    }
    select.value = state.context.product.selection[key];
    label.append(select);
    element('controls').append(label);
  }
}

function renderRankingControls(result) {
  const data = result.dataset || state.dataset;
  const config = result.ranking_config;
  const weightBox = element('weight-controls');
  if (!weightBox.children.length) {
    for (const rule of data.rubric) {
      const label = make('label', '权重 · ' + rule.id + '（满分' + rule.maximum + '，默认' + rule.weight + '）');
      const input = document.createElement('input');
      input.type = 'number'; input.min = '0'; input.step = '0.05';
      input.className = 'weight-input'; input.dataset.criterion = rule.id;
      label.append(input); weightBox.append(label);
    }
  }
  for (const input of document.querySelectorAll('.weight-input')) {
    input.value = config.weights[input.dataset.criterion];
  }
  element('exclude-conflicts').checked = config.exclude_conflicts;
  element('top-k').max = result.ranking.summary['参评项目'];
  if (document.activeElement !== element('top-k')) element('top-k').value = config.top_k;

  const projectSelect = element('exclude-project');
  if (!projectSelect.children.length) {
    for (const project of data.projects) {
      projectSelect.append(make('option', project.project_id));
    }
    for (const reviewer of data.reviewers) {
      element('exclude-reviewer').append(make('option', reviewer.reviewer_id));
    }
  }
  state.exclusions = config.excluded_pairs.map(pair => ({...pair}));
  renderExclusionList();
}

function renderExclusionList() {
  const list = element('exclusion-list');
  list.replaceChildren();
  if (!state.exclusions.length) {
    list.append(make('p', '暂无额外回避；利益冲突按上方开关自动处理。', 'hint'));
    return;
  }
  for (const [index, pair] of state.exclusions.entries()) {
    const row = make('div', pair.project_id + ' × ' + pair.reviewer_id +
      (pair.reason ? '（' + pair.reason + '）' : ''), 'exclusion-chip');
    const remove = make('button', '移除', 'mini');
    remove.type = 'button';
    remove.onclick = () => { state.exclusions.splice(index, 1); renderExclusionList(); };
    row.append(remove);
    list.append(row);
  }
}

function drawChart(series) {
  const canvas = element('chart');
  const context = canvas.getContext('2d');
  context.clearRect(0, 0, canvas.width, canvas.height);
  const values = series.filter(point => typeof point.y === 'number' && Number.isFinite(point.y));
  context.font = '12px sans-serif';
  if (!values.length) { context.fillText('该范围没有可定量的描述值', 30, 50); return; }
  const low = Math.min(0, ...values.map(point => point.y));
  const high = Math.max(1, ...values.map(point => point.y));
  const scale = value => 250 - (value - low) / (high - low || 1) * 210;
  context.strokeStyle = '#90a5ad';
  context.beginPath(); context.moveTo(45, 20); context.lineTo(45, 250); context.lineTo(940, 250); context.stroke();
  context.fillStyle = '#0a7168';
  values.forEach((point, index) => {
    const position = 55 + index * 875 / values.length;
    context.fillRect(position, Math.min(scale(point.y), scale(0)), Math.max(3, 800 / values.length),
      Math.max(1, Math.abs(scale(point.y) - scale(0))));
    if (values.length < 12) context.fillText(point.label, position, 275);
  });
  context.fillStyle = '#29434e';
  context.fillText(high.toFixed(2), 0, 45); context.fillText(low.toFixed(2), 0, 250);
  context.fillText('原始均分仅用于核对，不是校准后的名次；缺测不绘制为零', 50, 20);
}

function renderOverview(result) {
  const view = result.view;
  element('summary').replaceChildren();
  for (const [label, value] of Object.entries(view.summary)) {
    const card = document.createElement('article');
    card.append(make('span', label), make('strong', displayValue(value)));
    element('summary').append(card);
  }
  element('notes').replaceChildren();
  for (const note of view.notes) element('notes').append(make('p', note));
  const rows = view.rows;
  const fields = rows.length ? Object.keys(rows[0]) : [];
  const header = document.createElement('tr');
  for (const field of fields) header.append(make('th', field));
  element('columns').replaceChildren(header); element('rows').replaceChildren();
  for (const row of rows) {
    const line = document.createElement('tr'); line.tabIndex = 0;
    for (const field of fields) line.append(make('td', displayValue(row[field])));
    line.onclick = () => { element('details').textContent = JSON.stringify(row, null, 2); };
    line.onkeydown = event => { if (event.key === 'Enter') line.click(); };
    element('rows').append(line);
  }
  drawChart(view.series);
}

function renderRanking(result) {
  const ranking = result.ranking;
  element('ranking-notes').replaceChildren();
  for (const note of ranking.notes) element('ranking-notes').append(make('p', note));

  const banner = element('component-banner');
  banner.replaceChildren();
  if (ranking.components.length > 1) {
    const text = '可比性限制：本范围分成 ' + ranking.components.length + ' 个互不连通的评审组（' +
      ranking.components.map(c => c.component_id + '：' + c.projects.length + '项目/' +
        c.reviewers.length + '评委').join('；') +
      '）。组内项目有共同评委或间接桥接，可比较；跨组的 ' + ranking.uncomparable_pairs +
      ' 对项目缺少共同参照，全局名次仅在“各组标尺一致”的假设下成立。';
    banner.append(make('div', text, 'comparability'));
  } else {
    banner.append(make('div', '本范围所有项目处于同一评审连通组，任意两个项目都可经由共同评委比较。',
      'comparability ok'));
  }

  const columns = ['全局假设名次', '组内名次', '重叠带', '项目', '分组', '连通组', '综合分(0-1)',
    '95%区间', '证据', '有效评委', '权重扰动名次', '留一评委名次', '候选名单入选率(扰动/折刀)', '覆盖（弃权/无效/回避/缺席评委）'];
  const header = document.createElement('tr');
  for (const column of columns) header.append(make('th', column));
  element('ranking-columns').replaceChildren(header);
  const body = element('ranking-rows');
  body.replaceChildren();
  for (const row of ranking.rankings) {
    const line = document.createElement('tr');
    line.tabIndex = 0;
    line.className = 'reliability-' + row.reliability;
    const w = row.stability.weight, j = row.stability.jackknife;
    const cells = [
      String(row.global_rank_assumption) + (ranking.components.length > 1 ? '*' : ''),
      row.within_rank, row.band, row.project_id, row.track, row.component_id,
      row.composite_fraction.toFixed(4),
      row.ci_low.toFixed(3) + '–' + row.ci_high.toFixed(3),
      reliabilityText[row.reliability], row.scored_reviewers,
      w.min_rank + '–' + w.max_rank,
      j.min_rank === null ? '无法估计' : j.min_rank + '–' + j.max_rank +
        (j.eliminations ? '（剔除评委后' + j.eliminations + '次无估计）' : ''),
      (w.top_k_frequency * 100).toFixed(0) + '% / ' + (j.top_k_frequency * 100).toFixed(0) + '%',
      row.abstain_cells + '/' + row.invalid_cells + '/' + row.recused_cells + '/' + row.missing_reviewers.length,
    ];
    for (const value of cells) line.append(make('td', String(value)));
    line.onclick = () => showProjectDetail(result, row.project_id);
    line.onkeydown = event => { if (event.key === 'Enter') line.click(); };
    body.append(line);
  }
  if (ranking.unranked.length) {
    for (const row of ranking.unranked) {
      const line = make('tr', null, 'reliability-insufficient');
      const values = ['—', '—', '—', row.project_id, row.track, '无连通组', row.reason,
        '—', '—', '—', '—', '—', '—',
        row.abstain_cells + '/' + row.invalid_cells + '/' + row.recused_cells + '/' +
        row.missing_reviewers.length];
      for (const value of values) line.append(make('td', String(value)));
      body.append(line);
    }
  }

  element('method-notes').replaceChildren();
  const method = [
    '校准模型：评分 = 标准均值 + 项目效应 + 评委严宽效应，每个连通组内两类效应各自均值为零；' +
    '评委效应带岭收缩（先验强度 ' + ranking.config.ridge_prior + ' 个评分格）。',
    '综合分按主席权重对各标准校准均值加权（先折算到 0-1 标度再加权），95% 区间基于残差标准误，' +
    '重叠带表示区间重叠的项目不应被解读为确定的先后。',
    '稳定性：' + ranking.summary['权重扰动试验'] + ' 次固定种子权重扰动 + ' +
      ranking.summary['留一评委折刀'] + ' 位评委的留一折刀；入选率是项目留在前 ' +
      ranking.config.top_k + ' 名候选名单的频率。',
    '带 * 的全局名次跨评审组比较，缺少共同参照，请优先使用组内名次与区间。',
  ];
  for (const text of method) element('method-notes').append(make('p', text));

  renderReviewers(ranking);
  renderCriteria(ranking);
}

function showProjectDetail(result, projectId) {
  const row = result.ranking.rankings.find(item => item.project_id === projectId);
  const links = result.ranking.links[projectId] || [];
  const comparable = links.filter(link => link.shared_reviewers.length || link.chain.length);
  const payload = {
    项目: row.project_id, 连通组: row.component_id, 证据等级: reliabilityText[row.reliability],
    综合分: row.composite_fraction, 标准误: row.composite_se, 置信区间: [row.ci_low, row.ci_high],
    标准贡献: row.contributions,
    各标准校准分: row.adjusted,
    原始评分格: row.cells,
    覆盖: {弃权格: row.abstain_cells, 无效格: row.invalid_cells, 回避格: row.recused_cells,
      缺席评委: row.missing_reviewers},
    可比较项目: comparable.map(link => ({项目: link.project_id,
      共同评委: link.shared_reviewers,
      间接桥接: link.chain.length ? link.chain.join(' → ') : '直接共同评委'})),
    不可比项目数: result.ranking.uncomparable_pairs,
    稳定性: row.stability,
  };
  element('details').textContent = JSON.stringify(payload, null, 2);
}

function renderReviewers(ranking) {
  const columns = ['评委', '有效格', '证据', '原始均分', '综合严宽效应', '占满分比例',
    'rigor效应', 'clarity效应', 'usefulness效应', '收缩程度(按标准)'];
  const header = document.createElement('tr');
  for (const column of columns) header.append(make('th', column));
  element('reviewer-columns').replaceChildren(header);
  const body = element('reviewer-rows');
  body.replaceChildren();
  for (const reviewer of ranking.reviewers) {
    const line = document.createElement('tr');
    line.tabIndex = 0;
    const shrink = Object.entries(reviewer.shrinkage)
      .map(([criterion, value]) => criterion + ' ' + value.toFixed(2)).join('；');
    const cells = [reviewer.reviewer_id, reviewer.scored_cells,
      reviewerReliabilityText[reviewer.reliability], reviewer.raw_mean.toFixed(3),
      reviewer.overall_effect.toFixed(3),
      (reviewer.overall_effect_fraction * 100).toFixed(1) + '%',
      displayValue(reviewer.effects.rigor), displayValue(reviewer.effects.clarity),
      displayValue(reviewer.effects.usefulness), shrink];
    for (const value of cells) line.append(make('td', String(value)));
    line.onclick = () => { element('details').textContent = JSON.stringify(reviewer, null, 2); };
    body.append(line);
  }
}

function renderCriteria(ranking) {
  const columns = ['标准', '满分', '当前权重', '全局均值', '残差标准差', '缺测填补项目数'];
  const header = document.createElement('tr');
  for (const column of columns) header.append(make('th', column));
  element('criteria-columns').replaceChildren(header);
  const body = element('criteria-rows');
  body.replaceChildren();
  for (const criterion of ranking.criteria) {
    const line = document.createElement('tr');
    for (const value of [criterion.id, criterion.maximum, criterion.weight,
      criterion.global_mean, criterion.residual_sd, criterion.imputed_projects]) {
      line.append(make('td', displayValue(value)));
    }
    line.onclick = () => { element('details').textContent = JSON.stringify(criterion, null, 2); };
    body.append(line);
  }
}

function renderScenario() {
  const header = document.createElement('tr');
  for (const column of ['项目', '连通组', '快照全局名次', '当前全局名次', '变化',
    '快照组内名次', '当前组内名次', '快照综合分', '当前综合分', '当前证据']) {
    header.append(make('th', column));
  }
  element('scenario-columns').replaceChildren(header);
  const body = element('scenario-rows');
  body.replaceChildren();
  if (!state.snapshot) {
    const line = document.createElement('tr');
    const cell = make('td', '点击“把当前结果存为对照情景”后，再调整权重或回避重新计算，即可逐行看到名次变化。');
    cell.colSpan = 10;
    line.append(cell);
    body.append(line);
    return;
  }
  const before = Object.fromEntries(state.snapshot.ranking.rankings.map(row => [row.project_id, row]));
  const after = Object.fromEntries(state.current.ranking.rankings.map(row => [row.project_id, row]));
  const ids = new Set([...Object.keys(before), ...Object.keys(after)]);
  const lines = [];
  for (const projectId of ids) {
    const oldRow = before[projectId], newRow = after[projectId];
    const delta = oldRow && newRow ? oldRow.global_rank_assumption - newRow.global_rank_assumption : null;
    const deltaText = delta === null ? '无法比较' : delta === 0 ? '持平' : (delta > 0 ? '上升 ' + delta : '下降 ' + -delta);
    const line = make('tr', null, delta !== null && delta !== 0 ? 'rank-shift' : '');
    for (const value of [projectId,
      newRow ? newRow.component_id : (oldRow ? oldRow.component_id : ''),
      oldRow ? oldRow.global_rank_assumption : '已无估计',
      newRow ? newRow.global_rank_assumption : '已无估计', deltaText,
      oldRow ? oldRow.within_rank : '—', newRow ? newRow.within_rank : '—',
      oldRow ? oldRow.composite_fraction.toFixed(4) : '—',
      newRow ? newRow.composite_fraction.toFixed(4) : '—',
      newRow ? reliabilityText[newRow.reliability] : '—']) {
      line.append(make('td', String(value)));
    }
    lines.push([newRow ? newRow.global_rank_assumption : 9999, line]);
  }
  for (const [, line] of lines.sort((a, b) => a[0] - b[0])) body.append(line);
}

function setFrozen(frozen) {
  state.frozen = frozen;
  element('frozen-banner').hidden = !frozen;
  element('frozen-view').hidden = !frozen;
  element('return-live').hidden = !frozen;
  // 冻结时只锁定会改变计算的控件；查看原始记录等只读核对仍可用
  const keepEnabled = new Set(['raw']);
  for (const control of document.querySelectorAll('aside input, aside select, aside button')) {
    if (control.id === 'return-live' || keepEnabled.has(control.id)) continue;
    control.disabled = frozen;
  }
}

function render(result, heading) {
  // 实时接口返回 {revision, view, ranking, ...}；保存材料返回 {result:{...}, dataset}
  if (result.result) {
    result = {...result.result, dataset: result.dataset, notebook_id: result.id};
  }
  state.current = result;
  renderRankingControls(result);
  renderOverview(result);
  renderRanking(result);
  renderScenario();
  element('details').textContent = JSON.stringify({revision: result.revision, selection: result.selection,
    ranking_config: result.ranking_config, notebook_id: result.notebook_id || null}, null, 2);
  message((heading || '已按当前配置重新计算') + ' · 数据 ' + result.revision.slice(0, 12));
}

async function refresh() {
  state.context = await request('/api/context');
  renderControls();
  element('archive').replaceChildren();
  for (const notebook of state.context.notebooks) {
    const button = document.createElement('button');
    button.textContent = notebook.title + ' #' + notebook.id;
    button.onclick = () => guarded(async () => {
      const saved = await request('/api/notebooks/' + notebook.id);
      element('revision').value = saved.revision;
      for (const [key, value] of Object.entries(saved.selection)) {
        const control = element('select-' + key); if (control) control.value = value;
      }
      state.dataset = saved.dataset;
      render(saved, '已读取冻结保存材料 #' + saved.id);
      setFrozen(true);
    });
    element('archive').append(button);
  }
}

async function recompute(heading) {
  const live = await request('/api/explore', selected());
  state.dataset = await request('/api/data?revision=' + encodeURIComponent(live.revision));
  setFrozen(false);
  render(live, heading);
}
element('inspect').onclick = () => guarded(() => recompute());
element('add-exclusion').onclick = () => guarded(() => {
  element('exclusion-errors').textContent = '';
  const pair = {project_id: element('exclude-project').value, reviewer_id: element('exclude-reviewer').value};
  if (state.exclusions.some(item => item.project_id === pair.project_id &&
    item.reviewer_id === pair.reviewer_id)) {
    element('exclusion-errors').textContent = '该项目—评委对已在回避清单中';
    return;
  }
  const reason = window.prompt('回避原因（可留空，例如：近期合作）', '');
  if (reason === null) return;
  state.exclusions.push({...pair, reason: reason.trim()});
  renderExclusionList();
});
element('snapshot').onclick = () => guarded(() => {
  if (!state.current) throw new Error('请先查看一个范围');
  state.snapshot = JSON.parse(JSON.stringify(state.current));
  element('scenario-caption').textContent = '对照情景已保存（数据 ' +
    state.snapshot.revision.slice(0, 12) + '）。现在调整权重或回避并重新计算。';
  renderScenario();
  message('已保存对照情景，调整配置后重新计算即可比较');
});
element('return-live').onclick = () => guarded(async () => {
  await recompute('已返回实时视图');
});
element('save').onclick = () => guarded(async () => {
  const saved = await request('/api/notebooks', {...selected(), title: element('title').value});
  await refresh();
  state.dataset = saved.dataset;
  render(saved, '已冻结保存材料 #' + saved.id);
  setFrozen(true);
});
element('raw').onclick = () => guarded(async () => {
  const revision = state.current ? state.current.revision : element('revision').value;
  element('details').textContent = JSON.stringify(
    await request('/api/data?revision=' + encodeURIComponent(revision)), null, 2);
});
element('upload').onchange = () => guarded(async () => {
  const file = element('upload').files[0]; if (!file) return;
  const data = JSON.parse(await file.text());
  const uploaded = await request('/api/datasets', {label: file.name, data});
  await refresh(); element('revision').value = uploaded.revision;
  await recompute('已导入独立数据版本');
});
element('export').onclick = () => guarded(async () => {
  if (!state.current) throw new Error('请先查看一个范围');
  const data = await request('/api/data?revision=' + encodeURIComponent(state.current.revision));
  const blob = new Blob([JSON.stringify({description: state.current, data}, null, 2)],
    {type: 'application/json'});
  const url = URL.createObjectURL(blob); const link = document.createElement('a');
  link.href = url; link.download = 'analysis-' + state.current.revision.slice(0, 12) + '.json'; link.click();
  URL.revokeObjectURL(url);
});
guarded(async () => {
  await refresh();
  await recompute();
});
