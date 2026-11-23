<template>
  <section class="page" data-module="alert_console">
    <header class="page-head">
      <div>
        <h2>告警值班台</h2>
        <p class="page-desc">
          按采样记录核对连续超限：同一采样只算一次，缺口未补齐不接续，不同口径不拼接。
        </p>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="loadAll">
      <label class="filter-item">
        <span>告警规则</span>
        <select v-model.number="alertId" data-testid="rule-select" @change="loadAll">
          <option v-for="rule in rules" :key="rule.id" :value="rule.id">
            {{ rule.rule_name }}（{{ rule.monitor_metric }} {{ rule.alert_condition }}，连续
            {{ rule.consecutive_threshold }} 次）
          </option>
        </select>
      </label>
      <label class="filter-item">
        <span>判定截止采样时刻（可空）</span>
        <input v-model="asOf" data-testid="as-of" placeholder="2026-11-23T10:03:30" />
      </label>
      <button class="btn primary" data-testid="check-now" type="submit">检查一次</button>
      <button class="btn" data-testid="refresh" type="button" @click="loadAll">刷新</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <template v-if="check">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">本次判定</span>
          <strong class="stat-value" :data-testid="'status-' + check.status">{{ check.status }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">连续阈值 / 采样间隔</span>
          <strong class="stat-value">{{ check.threshold }} 次 / {{ check.sample_interval_seconds }}s</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">参与判定采样</span>
          <strong class="stat-value">{{ check.samples_considered }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">告警记录</span>
          <strong class="stat-value">{{ check.log_id ?? '—' }}</strong>
        </article>
      </div>

      <p class="check-reason" data-testid="check-reason">{{ check.reason }}</p>

      <article v-if="check.pending" class="panel" data-testid="pending-panel">
        <h3>还能不能等</h3>
        <dl class="kv">
          <dt>待补报采样</dt><dd>{{ check.pending.sample_time }}</dd>
          <dt>等待结论</dt><dd :data-testid="'wait-' + check.pending.can_wait">
            {{ check.pending.wait_state }}（截止 {{ check.pending.grace_deadline }}）
          </dd>
          <dt>还差</dt><dd>{{ check.pending.remaining_samples }} 个连续样本</dd>
          <dt>补报超限后</dt>
          <dd data-testid="if-backfilled">
            <template v-if="check.pending.if_backfilled_exceeded.would_trigger">
              会触发告警，命中区间
              {{ check.pending.if_backfilled_exceeded.window_start }} ~
              {{ check.pending.if_backfilled_exceeded.window_end }}
            </template>
            <template v-else>仍不足以触发，需继续等待</template>
          </dd>
        </dl>
      </article>

      <article v-if="check.triggered" class="panel" data-testid="triggered-panel">
        <h3>告警记录与超限证据</h3>
        <dl class="kv">
          <dt>判定采样时刻</dt><dd>{{ check.evidence[check.evidence.length - 1]?.sample_time }}</dd>
          <dt>异常区间</dt><dd>{{ check.window_start }} ~ {{ check.window_end }}</dd>
          <dt>口径</dt><dd>v{{ check.evidence[0]?.calibration_version }}</dd>
          <dt>是否新触发</dt><dd>{{ check.already_triggered ? '复用原记录' : '新触发' }}</dd>
        </dl>
        <sample-table :samples="check.evidence" />
        <div class="page-actions" v-if="check.log_id">
          <button class="btn primary" type="button" @click="attribute(check.log_id)">从该告警发起归因</button>
        </div>
      </article>

      <article class="panel">
        <h3>连续段（按口径分别计算）</h3>
        <table class="data-table" data-testid="runs-table">
          <thead>
            <tr>
              <th>口径</th><th>起点</th><th>终点</th><th>采样数</th><th>是否达到阈值</th><th>断开原因</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="run in check.runs" :key="run.calibration_version + run.start_time">
              <td>v{{ run.calibration_version }}</td>
              <td>{{ run.start_time }}</td>
              <td>{{ run.last_time }}</td>
              <td>{{ run.sample_count }}</td>
              <td>{{ run.exceeded ? '是' : '否' }}</td>
              <td>{{ run.break_reason || '—' }}</td>
            </tr>
          </tbody>
        </table>
        <template v-if="check.triggered && check.waiting_runs.length">
          <h4>其他仍在等待的连续段（已触发的段不会变）</h4>
          <ul class="waiting-list">
            <li v-for="wait in check.waiting_runs" :key="wait.calibration_version + wait.sample_time">
              v{{ wait.calibration_version }} 段待补 {{ wait.sample_time }}，还差
              {{ wait.remaining_samples }} 个；{{ wait.wait_state }}
              <template v-if="wait.if_backfilled_exceeded.would_trigger">
                ，补报超限将命中 {{ wait.if_backfilled_exceeded.window_start }} ~
                {{ wait.if_backfilled_exceeded.window_end }}
              </template>
            </li>
          </ul>
        </template>
      </article>

      <article class="panel">
        <h3>本次判定看到的采样</h3>
        <sample-table :samples="check.evaluated_samples" />
      </article>
    </template>

    <article class="panel">
      <h3>告警记录</h3>
      <p v-if="!logs.length" class="empty-state">暂无告警记录</p>
      <table v-else class="data-table" data-testid="logs-table">
        <thead>
          <tr>
            <th>记录</th><th>触发时刻</th><th>判定采样</th><th>异常区间</th><th>口径</th><th>证据</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="log in logs" :key="log.id">
            <td>{{ log.id }}</td>
            <td>{{ log.triggered_at }}</td>
            <td>{{ log.sample_time }}</td>
            <td>{{ log.window_start }} ~ {{ log.window_end }}</td>
            <td>v{{ log.calibration_version }}</td>
            <td>
              <span v-for="sample in log.evidence" :key="sample.id" class="chip">
                {{ sample.sample_time.slice(11) }}={{ sample.value }}
              </span>
            </td>
            <td>
              <button class="link" type="button" @click="attribute(log.id)">发起归因</button>
            </td>
          </tr>
        </tbody>
      </table>
    </article>

    <article v-if="analysis" class="panel" data-testid="analysis-panel">
      <h3>归因结论与证据回查</h3>
      <dl class="kv">
        <dt>归因记录</dt><dd>{{ analysis.entry_id }}</dd>
        <dt>状态</dt><dd>{{ analysis.status }}</dd>
        <dt>样本区间</dt><dd>{{ analysis.sample_window_start }} ~ {{ analysis.sample_window_end }}</dd>
        <dt>口径</dt><dd>v{{ analysis.calibration_version }}</dd>
        <dt>结论</dt><dd data-testid="analysis-conclusion">{{ analysis.conclusion }}</dd>
        <dt>证据是否被改写</dt>
        <dd :data-testid="'evidence-changed-' + analysis.evidence_changed">
          {{ analysis.evidence_changed ? '是（原结论保留，需显式重算）' : '否' }}
        </dd>
      </dl>
      <h4>冻结证据</h4>
      <sample-table :samples="analysis.frozen_evidence" />
      <h4 v-if="analysis.evidence_changed">当前采样</h4>
      <sample-table v-if="analysis.evidence_changed" :samples="analysis.current_evidence" />
      <div class="page-actions">
        <button class="btn" type="button" data-testid="rerun" @click="recompute">按当前采样重算</button>
        <button class="btn ghost" type="button" @click="reloadAnalysis">刷新证据</button>
      </div>
    </article>

    <article class="panel">
      <h3>判定审计（为什么当时没触发）</h3>
      <p v-if="!checks.length" class="empty-state">还没有检查记录</p>
      <table v-else class="data-table" data-testid="checks-table">
        <thead>
          <tr><th>检查时刻</th><th>结论</th><th>采样</th><th>原因</th><th>可等待</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in checks" :key="item.id">
            <td>{{ item.checked_at }}</td>
            <td>{{ item.status }}</td>
            <td>{{ item.sample_count }}</td>
            <td>{{ item.reason }}</td>
            <td>{{ item.can_wait ? '是' : '否' }}</td>
          </tr>
        </tbody>
      </table>
    </article>

    <article v-if="groups.length" class="panel">
      <h3>口径对照</h3>
      <div v-for="group in groups" :key="group.calibration_version" class="calibration-group">
        <h4>
          v{{ group.calibration_version }} · {{ group.sample_count }} 个采样 ·
          超限 {{ group.exceeded_count }} · {{ group.sample_start }} ~ {{ group.sample_end }}
        </h4>
        <sample-table :samples="group.samples" />
        <p class="page-desc">
          告警记录：{{ group.alert_logs.length }} 条<template v-if="group.alert_logs.length">
            （{{ group.alert_logs.map((log) => `#${log.id} ${log.window_start}~${log.window_end}`).join('，') }}）
          </template>
        </p>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  alertCalibrations,
  alertChecks,
  alertHistory,
  analysisEvidence,
  checkAlert,
  runAnalysis,
  startAttribution,
  type AlertCheck,
  type AlertLog,
  type AnalysisEvidence,
  type CalibrationGroup,
  type CheckAudit,
} from '@/api/alerting'
import { listEntries } from '@/api/backend-service'
import SampleTable from '@/components/SampleTable.vue'

type Rule = { id: number; rule_name: string; monitor_metric: string; alert_condition: string; consecutive_threshold: number }

const rules = ref<Rule[]>([])
const alertId = ref<number | null>(null)
const asOf = ref('')
const check = ref<AlertCheck | null>(null)
const logs = ref<AlertLog[]>([])
const checks = ref<CheckAudit[]>([])
const groups = ref<CalibrationGroup[]>([])
const analysis = ref<AnalysisEvidence | null>(null)
const lastEntryId = ref<number | null>(null)
const errorMessage = ref('')

async function loadRules() {
  const payload = await listEntries('alert_policy', {})
  rules.value = payload.items as unknown as Rule[]
  if (alertId.value === null && rules.value.length) alertId.value = rules.value[0].id
}

async function loadAll() {
  errorMessage.value = ''
  try {
    await loadRules()
    if (alertId.value === null) return
    const id = alertId.value
    const [checkResult, history, audits, calibration] = await Promise.all([
      checkAlert(id, asOf.value.trim() || undefined),
      alertHistory(id),
      alertChecks(id),
      alertCalibrations(id),
    ])
    check.value = checkResult.check_result
    logs.value = history.items
    checks.value = audits.items
    groups.value = calibration.groups
    if (lastEntryId.value !== null) await reloadAnalysis()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '加载失败'
  }
}

async function attribute(logId: number) {
  errorMessage.value = ''
  try {
    const created = await startAttribution(logId)
    lastEntryId.value = created.entry.id
    await runAnalysis(created.entry.id)
    await reloadAnalysis()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '发起归因失败'
  }
}

async function recompute() {
  if (lastEntryId.value === null) return
  await runAnalysis(lastEntryId.value, true)
  await reloadAnalysis()
}

async function reloadAnalysis() {
  if (lastEntryId.value === null) return
  analysis.value = await analysisEvidence(lastEntryId.value)
}

onMounted(loadAll)
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 12px; }
.panel h3 { margin: 0 0 8px; font-size: 14px; }
.panel h4 { margin: 10px 0 6px; font-size: 13px; color: var(--muted); }
.kv { display: grid; grid-template-columns: 130px 1fr; gap: 4px 10px; margin: 0 0 8px; font-size: 13px; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; }
.check-reason { background: #fff7e6; border: 1px solid #f5d6a6; border-radius: 6px; padding: 8px 10px; font-size: 13px; }
.chip { display: inline-block; background: #eef2ff; border-radius: 4px; padding: 1px 6px; margin-right: 4px; font-size: 12px; }
.calibration-group { border-top: 1px dashed var(--border); padding-top: 8px; margin-top: 8px; }
.waiting-list { margin: 6px 0 0; padding-left: 18px; font-size: 13px; }
</style>
