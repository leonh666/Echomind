<template>
  <details v-if="sources.length" class="message-sources evidence-panel">
    <summary>检索来源 · {{ sources.length }} 个片段</summary>
    <p class="muted">以下是本次实际检索的资料快照，供核对适用条件；命中资料不代表回答已经核验正确。</p>
    <article v-for="(source, index) in sources" :key="`${source.chunk_id || source.document_id || 'source'}-${index}`">
      <strong>{{ index + 1 }}. {{ source.title || '未命名资料' }}</strong>
      <dl class="evidence-details">
        <div v-if="source.product_name || source.product_id"><dt>商品</dt><dd>{{ source.product_name || source.product_id }}</dd></div>
        <div><dt>产品版本</dt><dd>{{ source.product_version || '未提供' }}</dd></div>
        <div><dt>文档版本</dt><dd>{{ source.document_version || '未提供' }}</dd></div>
        <div><dt>知识库</dt><dd>{{ source.knowledge_base_id || '未提供' }}</dd></div>
        <div><dt>资料更新时间</dt><dd>{{ source.updated_at || '未提供' }}</dd></div>
        <div v-if="source.valid_from || source.valid_until"><dt>有效期</dt><dd>{{ source.valid_from || '未提供' }} 至 {{ source.valid_until || '未提供' }}</dd></div>
        <div><dt>片段位置</dt><dd>{{ source.locator || source.chunk_id || '未提供' }}</dd></div>
      </dl>
      <a v-if="safeSourceUrl(source.source_url)" :href="safeSourceUrl(source.source_url)" target="_blank" rel="noopener noreferrer">查看原始资料</a>
      <p class="evidence-content">{{ source.content }}</p>
      <small v-if="source.content_truncated">片段超过展示长度，当前仅保留前 6000 字符，请查看原始资料。</small>
      <details v-if="source.score != null || source.distance != null" class="evidence-ranking"><summary>检索排序信息</summary><small>{{ source.score_type || '未标注类型' }}<template v-if="source.score != null"> · 分数 {{ source.score }}</template><template v-if="source.distance != null"> · 距离 {{ source.distance }}</template>。分值用于排序，不是正确率。</small></details>
    </article>
  </details>
  <p v-else class="evidence-empty muted">本次没有可展示的检索来源，重要事实与政策请另行核实。</p>
</template>

<script setup>
import { safeSourceUrl } from '../lib/insights.js'
defineProps({ sources: { type: Array, default: () => [] } })
</script>
