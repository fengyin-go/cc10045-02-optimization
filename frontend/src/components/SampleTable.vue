<template>
  <table class="data-table">
    <thead>
      <tr>
        <th>采样时刻</th>
        <th>值</th>
        <th>口径</th>
        <th>来源</th>
      </tr>
    </thead>
    <tbody>
      <tr v-for="sample in samples" :key="sample.id">
        <td>{{ sample.sample_time }}</td>
        <td>{{ sample.value }}</td>
        <td>v{{ sample.calibration_version }}</td>
        <td>{{ sample.source === 'backfill' ? '补报' : sample.source }}</td>
      </tr>
      <tr v-if="!samples.length">
        <td colspan="4" class="empty-state">区间内没有采样</td>
      </tr>
    </tbody>
  </table>
</template>

<script setup lang="ts">
import type { Sample } from '@/api/alerting'

defineProps<{ samples: Sample[] }>()
</script>
