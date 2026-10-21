<template>
  <section class="workspace-card document-management">
    <div class="card-heading"><h2>文档与版本</h2><button class="quiet-button" @click="load">刷新列表</button></div>
    <p v-if="error" class="danger" role="alert">{{ error }}</p>
    <div v-for="document in documents" :key="document.document_id" class="document-row">
      <div><strong>{{ document.title }}</strong><small>版本 {{ document.product_version || document.document_version }} · {{ document.chunk_count }} 个片段</small></div>
      <div v-if="canEdit(document)" class="side-actions">
        <button class="quiet-button" @click="edit(document)">修改</button>
        <button class="quiet-button" @click="deleteTarget = document">删除</button>
      </div>
    </div>
    <div v-if="deleteTarget" class="confirmation-box">
      <p>确认删除“{{ deleteTarget.title }}”？它将从活动检索与缓存中退出。</p>
      <button @click="remove" :disabled="pending">确认删除</button>
      <button class="quiet-button" @click="deleteTarget = null">返回</button>
    </div>
    <form v-if="selected" @submit.prevent="save" class="document-editor">
      <h3>更新文档</h3>
      <label><span>标题</span><input v-model="form.title" required /></label>
      <label><span>产品版本</span><input v-model="form.product_version" /></label>
      <label><span>来源地址</span><input v-model="form.source_url" type="url" /></label>
      <label><span>正文</span><textarea v-model="form.content" rows="8" required /></label>
      <div class="side-actions"><button :disabled="pending">保存并替换旧片段</button><button type="button" class="quiet-button" @click="selected = null">返回</button></div>
    </form>
    <p v-if="!documents.length" class="workspace-empty">当前范围内暂无已发布文档。</p>
  </section>
</template>
<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { knowledgeDocuments, replaceDocument, deleteDocument } from '../lib/operations.js'
const props = defineProps({ settings: Object, session: Object, refreshKey: Number })
const emit = defineEmits(['updated'])
const documents = ref([]), selected = ref(null), deleteTarget = ref(null), error = ref(''), pending = ref(false)
const form = reactive({ title: '', content: '', source_url: '', product_version: '', knowledge_base_id: 'default' })
function canEdit(document) { return ['admin', 'kb_admin'].includes(props.session?.role) && document.organization_id === props.session?.organization_id }
async function load() { try { documents.value = (await knowledgeDocuments(props.settings)).items; error.value = '' } catch (failure) { documents.value = []; error.value = failure.message } }
function edit(document) { selected.value = document; for (const key of Object.keys(form)) form[key] = document[key] || (key === 'knowledge_base_id' ? 'default' : '') }
async function save() {
  pending.value = true
  try { await replaceDocument(props.settings, selected.value.document_id, { ...form }); selected.value = null; await load(); emit('updated') } catch (failure) { error.value = failure.message } finally { pending.value = false }
}
async function remove() {
  pending.value = true
  try { await deleteDocument(props.settings, deleteTarget.value.document_id); deleteTarget.value = null; await load(); emit('updated') } catch (failure) { error.value = failure.message } finally { pending.value = false }
}
onMounted(load)
watch(() => props.refreshKey, load)
</script>
