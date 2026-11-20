import {DOMAIN} from '../constants/domain.js';
import {store, load} from '../stores/research.js';
import {exclusions} from '../api/research.js';
import {studySelect} from '../components/common/study-select.js';
import {table} from '../components/common/table.js';
import {status} from '../components/common/status.js';
import {bindForm} from '../hooks/forms.js';
import {esc, fmt} from '../utils/format.js';

const trialState = {plan: null, studyId: 0, error: '', conflict: null};

function parseKeys(value) {
  return [...new Set(String(value || '').split(/[\s,，;；]+/).map(x => x.trim()).filter(Boolean))];
}

function retainedTable(rows) {
  return table(
    ['试件', '孔隙率 (%)', '吸水率 (%)', '必留'],
    rows.map(row => [row.source_key, fmt(row.porosity), fmt(row.absorption), row.forced ? '必留' : '']),
  );
}

function excludedTable(rows, mustKeys) {
  const must = new Set(mustKeys);
  return table(
    ['试件', '孔隙率 (%)', '吸水率 (%)', '性质'],
    rows.map(row => [row.source_key, fmt(row.porosity), fmt(row.absorption), must.has(row.source_key) ? '所有最优解都剔除' : '本方案剔除，存在保留它的最优解']),
  );
}

function trialList(rows) {
  return table(
    ['方案', '状态', '样本 / 剔除', '方案依据 SHA256'],
    rows.map(row => [
      {html: `<a href="#exclusions/${row.id}">#${row.id} ${esc(row.name)}</a>`},
      {html: status(row.status)},
      row.retained_count == null ? '—' : `${row.retained_count} 保留 / ${row.excluded_count} 剔除`,
      row.basis_checksum.slice(0, 16) + '…',
    ]),
  );
}

function planBlock(plan) {
  const must = plan.must_exclude_keys || [];
  return `<section class="result-intro"><strong>最少剔除 ${plan.minimum_excluded} 件，保留 ${plan.retained_count} / ${plan.total} 件。</strong><p>保留样本按孔隙率排序后吸水率不下降；所有最优解共同要求剔除 ${must.length} 件${must.length ? `：${must.map(esc).join('、')}` : '（无强制剔除项）'}。</p></section>
<p class="checksum">方案依据 SHA256 ${plan.basis_checksum || plan.input_sha256} · 算法 ${esc(plan.algorithm)} · 全量 ${plan.total} 件按秩归并，未做逐对比较</p>
<h2>保留样本 <small>${plan.retained_count} 件</small></h2>${retainedTable(plan.retained)}
<h2>剔除样本 <small>${plan.excluded_count} 件，其中 ${must.length} 件无法保留在任何最优解中</small></h2>${excludedTable(plan.excluded, must)}
${plan.flexible?.length ? `<details><summary>可替换剔除项（${plan.flexible.length} 件）</summary><p>这些试件在本方案被剔除，但存在保留它们的同等最优解：${plan.flexible.map(x => esc(x.source_key)).join('、')}。</p></details>` : ''}`;
}

function confirmBlock(plan) {
  const writable = localStorage.getItem('research-role') !== 'viewer';
  if (!writable) return '<p class="method-note">观察员可查看试算结果；分析师与负责人可以确认并冻结方案。</p>';
  return `<form id="confirm-plan" class="worksheet"><label>方案名称<input name="name" minlength="2" maxlength="120" required value="${esc(DOMAIN.names[0])} · 最少剔除"></label><p>确认后冻结保留样本、必留项与全部观测快照（${plan.row_count} 条）并排队计算；来源观测不会被修改。确认期间若测值或必留项被改动，本次确认会被拒绝。</p><button type="submit">确认方案并冻结排队 →</button></form>`;
}

function conflictBlock(conflict) {
  return `<div class="error"><strong>必留项矛盾</strong><p>${esc(conflict.message)}</p><p>请去掉 ${esc(conflict.conflict.first.source_key)} 或 ${esc(conflict.conflict.second.source_key)} 中的一件，或在来源页核对测值后重新试算。</p></div>`;
}

export async function render(id) {
  if (!store.studies.length) await load();
  if (!store.studyId) store.studyId = store.studies[0]?.id || 0;
  if (id) return renderTrial(id);
  return `<div class="eyebrow">02B / EXCLUSION TRIAL</div><h1>最少剔除试算</h1><p class="lead">保留样本中，孔隙率更高的试件吸水率不得更低。试算给出剔除最少的一套方案，并列出所有最优解都必须剔除的试件；计算按秩归并，不做逐对比较。</p>
<div class="method-note">试算只读取当前观测，不改动任何来源记录。试算与确认之间若测值或必留项被修改，旧方案不得生效，必须重新试算。</div>
<form id="trial-form" class="worksheet">${studySelect()}<label>必留试件编号（可空，逗号或换行分隔）<textarea name="forced_keys" placeholder="如 FIELD-001, FIELD-014" style="min-height:70px"></textarea></label><button type="submit">试算 →</button></form>
<div id="trial-output">${trialState.error ? `<div class="error">${esc(trialState.error)}</div>` : ''}${trialState.conflict ? conflictBlock(trialState.conflict) : ''}${trialState.plan ? planBlock(trialState.plan) + confirmBlock(trialState.plan) : ''}</div>
<h2>已冻结的试算方案</h2>${trialList(await exclusions.list(store.studyId))}`;
}

async function renderTrial(id) {
  const trial = await exclusions.get(id);
  store.trialId = trial.id;
  store.trialPending = ['queued', 'running'].includes(trial.status);
  return `<div class="eyebrow">02B / EXCLUSION TRIAL · #${trial.id}</div><h1>${esc(trial.name)} ${status(trial.status)}</h1><p class="checksum">方案依据 SHA256 ${trial.basis_checksum}</p><p>冻结观测 ${trial.basis_row_count} 条 · 必留 ${esc((trial.forced_keys || []).join('、') || '无')} · ${esc(trial.created_at)}</p>
${trial.status === 'failed' ? `<div class="error">试算失败：${esc(trial.error)}</div>` : trial.result ? planBlock(trial.result) : '<p class="method-note">方案已冻结排队，worker 完成后展示保留样本。中断后重启 worker 会自动续跑，同一依据不会重复建方案。</p>'}
<a class="button secondary" href="#exclusions">返回试算 →</a>`;
}

export function bind(refresh) {
  const selector = document.getElementById('study-select');
  if (selector) selector.onchange = () => { store.studyId = +selector.value; trialState.plan = null; trialState.conflict = null; trialState.error = ''; refresh(); };
  bindForm('trial-form', async data => {
    trialState.plan = null; trialState.conflict = null; trialState.error = '';
    const result = await exclusions.preview({study_id: +data.study_id, forced_keys: parseKeys(data.forced_keys)});
    if (result.status === 'conflict') trialState.conflict = result;
    else trialState.plan = result;
    refresh();
  });
  bindForm('confirm-plan', async data => {
    const plan = trialState.plan;
    if (!plan) return;
    try {
      const created = await exclusions.create({study_id: plan.study_id, name: data.name, forced_keys: plan.forced_keys, basis_checksum: plan.basis_checksum});
      location.hash = 'exclusions/' + created.trial.id;
    } catch (error) {
      trialState.plan = null;
      trialState.error = error.message;
      refresh();
    }
  });
}
