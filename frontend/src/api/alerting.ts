/** 告警判定与归因证据接口：与后端 routers/alerting.py 一一对应。 */
import { request } from './client'

export type Sample = {
  id: number
  sample_time: string
  value: number
  calibration_version: number
  source: string
  ingested_at?: string
}

export type RunSummary = {
  calibration_version: number
  start_time: string
  last_time: string
  sample_count: number
  exceeded: boolean
  closed_by: string | null
  closed_at: string | null
  missing_sample_time: string | null
  break_reason: string | null
  sample_times: string[]
}

export type PendingInfo = {
  sample_time: string
  reason: string
  remaining_samples: number
  grace_deadline: string
  grace_seconds: number
  can_wait: boolean
  wait_state: string
  calibration_version: number
  run: RunSummary
  if_backfilled_exceeded: {
    would_trigger: boolean
    window_start: string | null
    window_end: string | null
    evidence_sample_times: string[]
  }
}

export type AlertCheck = {
  status: string
  triggered: boolean
  already_triggered: boolean
  reason: string
  threshold: number
  sample_interval_seconds: number
  samples_considered: number
  duplicate_samples: number
  evaluated_samples: Sample[]
  latest_sample: Sample | null
  runs: RunSummary[]
  pending: PendingInfo | null
  waiting_runs: PendingInfo[]
  if_backfilled: PendingInfo['if_backfilled_exceeded'] | null
  evidence: Sample[]
  window_start: string | null
  window_end: string | null
  log_id: number | null
  check_id: number
  calibration_note?: string
}

export type AlertLog = {
  id: number
  triggered_at: string
  sample_time: string
  window_start: string
  window_end: string
  consecutive_count: number
  calibration_version: number
  metric_value: number
  threshold_value: number
  evidence: Sample[]
  rule_snapshot: Record<string, unknown>
  resolved: number
}

export type CheckAudit = {
  id: number
  checked_at: string
  status: string
  reason: string
  sample_count: number
  evaluated_samples: Sample[]
  pending: PendingInfo | Record<string, never>
  can_wait: number
}

export type CalibrationGroup = {
  calibration_version: number
  sample_count: number
  exceeded_count: number
  sample_start: string
  sample_end: string
  samples: Sample[]
  runs: RunSummary[]
  alert_logs: AlertLog[]
}

export type AnalysisEvidence = {
  ok: boolean
  entry_id: number
  status: string
  conclusion: string
  alert_log_id: number | null
  alert_evidence: Sample[]
  sample_window_start: string
  sample_window_end: string
  calibration_version: number
  frozen_evidence: Sample[]
  current_evidence: Sample[]
  evidence_changed: boolean
  changed_samples: { sample_id: number; sample_time?: string; frozen_value: number; current_value?: number }[]
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = (payload as { detail?: string }).detail
    throw new Error(detail || `接口返回 ${response.status}`)
  }
  return payload as T
}

export function checkAlert(alertId: number, asOf?: string): Promise<{ check_result: AlertCheck }> {
  return json('/api/alert_policy/' + alertId + '/check', {
    method: 'POST',
    body: JSON.stringify({ as_of: asOf || null }),
  })
}

export function alertHistory(alertId: number): Promise<{ items: AlertLog[] }> {
  return json('/api/alert_policy/' + alertId + '/history')
}

export function alertChecks(alertId: number): Promise<{ items: CheckAudit[] }> {
  return json('/api/alert_policy/' + alertId + '/checks')
}

export function alertCalibrations(alertId: number): Promise<{
  groups: CalibrationGroup[]
  threshold: number
  sample_interval_seconds: number
  metric_name: string
}> {
  return json('/api/alert_policy/' + alertId + '/calibrations')
}

export function startAttribution(logId: number, dimensions = '["dimension_key"]'): Promise<{
  ok: boolean
  message: string
  entry: { id: number }
  sample_window_start: string
  sample_window_end: string
  calibration_version: number
}> {
  return json('/api/anomaly_analysis/from-alert/' + logId, {
    method: 'POST',
    body: JSON.stringify({ attribution_dimensions: dimensions }),
  })
}

export function runAnalysis(entryId: number, recompute = false): Promise<Record<string, unknown>> {
  return json('/api/anomaly_analysis/' + entryId + '/run', {
    method: 'POST',
    body: JSON.stringify({ recompute }),
  })
}

export function analysisEvidence(entryId: number): Promise<AnalysisEvidence> {
  return json('/api/anomaly_analysis/' + entryId + '/evidence')
}

export function recordSample(
  metricId: number,
  payload: { value: number; sample_time: string; calibration_version?: number; source?: string },
  backfill = false,
): Promise<{ ok: boolean; message: string; changed: boolean }> {
  const path = '/api/metric_store/' + metricId + (backfill ? '/backfill' : '/samples')
  return json(path, { method: 'POST', body: JSON.stringify(payload) })
}
