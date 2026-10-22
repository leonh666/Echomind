<template>
  <main :class="['app-shell', `app-shell-${activeView}`]">
    <header class="topbar">
      <a class="brand" href="#" aria-label="EchoMind 首页" @click.prevent="activeView = 'chat'">
        <span class="brand-mark">E</span>
        <span class="brand-name">EchoMind</span>
      </a>

      <nav class="view-nav" aria-label="工作区">
        <button :class="{ active: activeView === 'chat' }" @click="activeView = 'chat'">对话</button>
        <button v-if="access.authenticated" :class="{ active: activeView === 'profile' }" @click="activeView = 'profile'">我的信息</button>
        <button v-if="access.authenticated && settings.backend === 'python'" :class="{ active: activeView === 'answers' }" @click="activeView = 'answers'">{{ ['admin', 'support'].includes(authSession?.role) ? '咨询与反馈' : '我的咨询' }}</button>
        <button v-if="access.knowledge" :class="{ active: activeView === 'knowledge' }" @click="activeView = 'knowledge'">知识库</button>
        <button v-if="access.authenticated && settings.backend === 'python'" :class="{ active: activeView === 'tickets' }" @click="activeView = 'tickets'">{{ access.admin ? '工单处理' : '我的工单' }}</button>
        <button v-if="access.admin" :class="{ active: activeView === 'admin' }" @click="activeView = 'admin'">管理后台</button>
        <button v-if="access.admin && settings.backend === 'python'" :class="{ active: activeView === 'diagnostics' }" @click="activeView = 'diagnostics'">客服诊断</button>
        <button v-if="access.admin" :class="{ active: activeView === 'evaluation' }" @click="activeView = 'evaluation'">评测</button>
      </nav>

      <div class="topbar-tools">
        <span class="environment-pill">
          <i :class="healthOk ? 'online' : 'offline'"></i>
          {{ currentBackend.label }}
        </span>
        <a v-if="access.admin" class="docs-link" :href="docsUrl" target="_blank" rel="noreferrer">API 文档</a>
        <button class="avatar-button" title="当前用户" @click="activeView = access.authenticated ? 'profile' : 'chat'">{{ userInitial }}</button>
      </div>
    </header>

    <div v-if="toast" class="toast" role="status">{{ toast }}</div>
    <OperationsPanel v-if="settings.backend === 'python'" ref="operationsPanel" :key="currentBackend.baseUrl" :settings="settings"
      :visible="activeView === 'tickets'" :refresh-key="operationsRefresh" @session-change="sessionChanged" @notice="showToast" />
    <AdminDashboard v-if="activeView === 'admin' && access.admin" :settings="settings" :session="authSession"
      @notice="showToast" @session-expired="invalidateSession" />
    <DiagnosticsPanel v-if="activeView === 'diagnostics' && access.admin && settings.backend === 'python'" :settings="settings" :session="authSession"
      @notice="showToast" @session-expired="invalidateSession" />
    <DiagnosticsPanel v-if="activeView === 'answers' && access.authenticated && settings.backend === 'python'" :settings="settings" :session="authSession" history-only
      @notice="showToast" @session-expired="invalidateSession" />
    <section v-if="activeView === 'profile' && access.authenticated" class="page page-profile">
      <div class="page-heading"><div class="heading-copy"><span class="kicker">My account</span><h1>我的信息</h1><p>当前登录账号的个人资料和所属组织。</p></div></div>
      <section class="workspace-card">
        <dl class="detail-list">
          <div><dt>姓名</dt><dd>{{ authSession.user?.display_name || '-' }}</dd></div>
          <div><dt>邮箱</dt><dd>{{ authSession.user?.email || '-' }}</dd></div>
          <div><dt>用户编号</dt><dd>{{ authSession.user_id || authSession.user?.id }}</dd></div>
          <div><dt>所属组织</dt><dd>{{ authSession.organization?.name || authSession.organization_id }}</dd></div>
          <div><dt>账号角色</dt><dd>{{ roleName }}</dd></div>
        </dl>
        <p class="muted">{{ access.admin ? '可进入管理后台查看本组织的业务数据。' : '个人对话与工单按当前账号隔离，后台数据按角色授权。' }}</p>
        <button class="quiet-button" @click="activeView = 'tickets'">查看我的工单</button>
      </section>
    </section>

    <section v-if="activeView === 'chat'" class="page page-chat">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">让每一次咨询，都有回应</span>
          <h1>客服工作台</h1>
          <p>{{ access.admin ? '结合知识来源处理咨询，复核回答并记录反馈。' : '咨询产品与服务问题，查看参考来源，跟进处理进度。' }}</p>
        </div>
        <div class="heading-actions">
          <span class="session-label">{{ settings.conversationId ? '当前会话' : '新会话' }}</span>
          <button class="quiet-button" @click="clearConversation" :disabled="chatPending">清空</button>
        </div>
      </div>

      <div class="chat-layout">
        <section class="chat-stage">
          <div class="stage-bar">
            <div class="stage-context">
              <span class="context-dot"></span>
              <span>{{ access.admin ? '客服协同空间' : '在线咨询' }}</span>
            </div>
            <span>{{ messages.length }} 条消息</span>
          </div>

          <div class="messages" ref="messageList">
            <article v-for="item in messages" :key="item.id" :class="['message', item.role]">
              <div class="message-meta">
                <span>{{ item.role === 'user' ? '你' : currentBackend.label + ' Agent' }}</span>
                <small v-if="item.meta">{{ item.meta }}</small>
              </div>
              <details v-if="access.admin && item.progress?.length" class="message-progress" :open="item.streaming">
                <summary>{{ item.streaming ? '正在处理' : '处理进度' }}<small v-if="item.streaming">{{ item.elapsed || 0 }} 秒</small></summary>
                <ol><li v-for="(step, index) in item.progress" :key="index" :class="step.status">{{ step.message }}</li></ol>
              </details>
              <p v-if="item.content">{{ item.content }}<span v-if="item.streaming" class="stream-cursor" aria-label="正在生成">▍</span></p>
              <p v-else-if="item.streaming" class="reply-waiting" role="status">{{ item.progress?.at(-1)?.message || '请求已发送，等待处理…' }}</p>
              <AnswerEvidence v-if="item.role === 'assistant' && !item.streaming && (item.feedbackAvailable || item.sources?.length)" :sources="item.sources || []" />
              <AnswerFeedback v-if="item.feedbackAvailable && access.authenticated && settings.backend === 'python'" :settings="settings" :session="authSession" :request-id="item.requestId" :answer="item.content" @notice="showToast" @session-expired="invalidateSession" />
              <p v-for="warning in item.warnings || []" :key="warning" class="danger">{{ warning }}</p>
              <button v-if="item.tasks?.length" class="quiet-button" @click="activeView = 'tickets'">核对 {{ item.tasks.length }} 份工单草稿</button>
              <div v-if="access.admin && item.trace" class="message-trace">
                <div class="trace-head">
                  <span>工具调用</span>
                  <small v-if="item.trace.requestId">#{{ item.trace.requestId }}</small>
                </div>
                <div v-if="item.trace.toolCalls?.length" class="trace-calls">
                  <details v-for="(call, index) in item.trace.toolCalls" :key="`${item.id}-${index}`" open>
                    <summary>
                      <strong>{{ call.tool_name || 'unknown_tool' }}</strong>
                      <span>{{ call.success ? '成功' : '失败' }}</span>
                    </summary>
                    <pre>{{ formatJson(call.input || {}) }}</pre>
                  </details>
                </div>
                <div v-else class="trace-empty-block">
                  <p>本次请求已生成 trace，但没有可展示的工具输入。</p>
                  <p v-if="item.trace.toolsUsed?.length" class="trace-note">已调用：{{ item.trace.toolsUsed.join(' · ') }}</p>
                </div>
              </div>
            </article>

            <div v-if="messages.length === 0" class="empty-state">
              <div class="welcome-mark" aria-hidden="true"><svg viewBox="0 0 32 32" fill="none"><path d="M7 7h18v13H15l-6 5v-5H7V7Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M12 12h8M12 16h5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg></div>
              <span class="welcome-eyebrow">{{ authSession?.user?.display_name ? '你好，' + authSession.user.display_name : '欢迎使用 EchoMind' }}</span>
              <h2>今天有什么可以帮你？</h2>
              <p>从一个问题开始，找到资料，也找到下一步。</p>
              <div class="starter-prompts">
                <button @click="usePrompt('我想了解退款政策和申请流程')"><span class="prompt-symbol" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M5 8h10a5 5 0 0 1 0 10h-4M5 8l4-4M5 8l4 4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span><strong>退款与售后</strong><small>了解政策与办理流程</small></span><span class="prompt-arrow" aria-hidden="true">↗</span></button>
                <button @click="usePrompt('登录时提示错误，应该怎么排查？')"><span class="prompt-symbol" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M5 14v-3a7 7 0 0 1 14 0v3M5 12H3v6h4v-6H5Zm14 0h2v6h-4v-6h2Zm0 6c0 3-4 3-6 3" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg></span><span><strong>技术支持</strong><small>一起排查遇到的问题</small></span><span class="prompt-arrow" aria-hidden="true">↗</span></button>
                <button @click="usePrompt('发票多久可以开具？')"><span class="prompt-symbol" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M6 3h12v18l-3-2-3 2-3-2-3 2V3Z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M9 8h6M9 12h4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg></span><span><strong>发票与账单</strong><small>查看开票与费用说明</small></span><span class="prompt-arrow" aria-hidden="true">↗</span></button>
              </div>
            </div>
          </div>

          <form class="composer" @submit.prevent="sendMessage">
            <div class="composer-heading"><span>描述你遇到的问题</span><span>支持多轮对话</span></div>
            <textarea
              v-model="draft"
              rows="3"
              placeholder="例如：退款审核需要多久？我应该准备哪些信息？"
              @keydown.meta.enter.prevent="sendMessage"
              @keydown.ctrl.enter.prevent="sendMessage"
            ></textarea>
            <div class="composer-bottom">
              <span>⌘ / Ctrl + Enter 发送</span>
              <button v-if="chatPending" type="button" class="quiet-button" @click="stopResponse">停止生成</button>
              <button v-else type="submit" :disabled="busy || !draft.trim()">{{ busy ? '处理中' : '发送' }}</button>
            </div>
          </form>
        </section>

        <aside class="chat-sidebar">
          <div class="chat-sidebar-scroll">
            <section v-if="access.admin" class="side-card session-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Session</span>
                  <h2>会话信息</h2>
                </div>
                <span class="status-copy muted">{{ settings.conversationId ? '已启用' : '新会话' }}</span>
              </div>
              <div class="session-grid">
                <div>
                  <span>会话 ID</span>
                  <strong>{{ settings.conversationId || '自动生成' }}</strong>
                </div>
                <div>
                  <span>用户 ID</span>
                  <strong>{{ settings.userId || 'anonymous' }}</strong>
                </div>
              </div>
            </section>

            <section v-else class="side-card guidance-card">
              <div class="card-heading"><div><span class="kicker">咨询小贴士</span><h2>让问题更快解决</h2></div></div>
              <ol><li><span>01</span><div><strong>说清遇到的情况</strong><p>描述发生了什么，以及你希望获得的帮助。</p></div></li><li><span>02</span><div><strong>补充必要的上下文</strong><p>提供商品型号、错误提示或已经尝试的步骤。</p></div></li><li><span>03</span><div><strong>核对回复与参考来源</strong><p>仍有疑问可以继续追问，并记录处理结果。</p></div></li></ol>
            </section>

            <section v-if="access.admin" class="side-card connection-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Connection</span>
                  <h2>连接配置</h2>
                </div>
                <span class="status-copy" :class="healthOk ? 'success' : 'muted'">{{ healthLabel }}</span>
              </div>

              <div v-if="availableBackends.length > 1" class="backend-tabs">
                <button v-for="type in availableBackends" :key="type" :disabled="chatPending" :class="{ active: settings.backend === type }" @click="switchBackend(type)">{{ backendMeta(type, settings).label }}</button>
              </div>

              <label>
                <span>用户 ID</span>
                <input v-model="settings.userId" @change="persist" :disabled="Boolean(authSession)" placeholder="u1001" />
              </label>
              <label>
                <span>会话 ID</span>
                <input v-model="settings.conversationId" @change="persist" placeholder="自动生成" />
              </label>
              <div class="side-actions">
                <button @click="checkHealth">检查连接</button>
                <button class="quiet-button" @click="refreshConsole">刷新</button>
              </div>
            </section>

            <section v-if="access.admin" class="side-card trace-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Last trace</span>
                  <h2>最近一次请求</h2>
                </div>
                <span class="trace-status" :class="lastResponse ? 'has-data' : ''"></span>
              </div>

              <div v-if="lastResponse" class="trace-body">
                <div class="latency">
                  <span>响应耗时</span>
                  <strong>{{ lastResponse.latencyMs || '-' }}<small> ms</small></strong>
                </div>
                <dl class="detail-list">
                  <div><dt>主 Agent</dt><dd>{{ lastResponse.primaryAgent || lastResponse.agentType || '-' }}</dd></div>
                  <div><dt>意图</dt><dd>{{ lastResponse.intent || '-' }}</dd></div>
                  <div><dt>置信度</dt><dd>{{ formatPercent(lastResponse.routingConfidence) }}</dd></div>
                  <div><dt>知识库</dt><dd :class="lastResponse.knowledgeUsed ? 'success' : 'muted'">{{ lastResponse.knowledgeUsed ? '已使用' : '未使用' }}</dd></div>
                  <div><dt>转人工</dt><dd :class="lastResponse.escalated ? 'danger' : 'muted'">{{ lastResponse.escalated ? '是' : '否' }}</dd></div>
                </dl>
                <p v-if="lastResponse.routingReason" class="routing-reason">{{ lastResponse.routingReason }}</p>
                <div v-if="lastTrace?.trace" class="trace-call-list">
                  <div class="trace-call-title">工具调用</div>
                  <div v-for="(call, index) in lastTrace.trace.toolCalls" :key="`${call.tool_use_id || index}`" class="trace-call-item">
                    <div class="trace-call-meta">
                      <strong>{{ call.tool_name || 'unknown_tool' }}</strong>
                      <span>{{ call.latency_ms || 0 }} ms</span>
                    </div>
                    <pre>{{ formatJson(call.input || {}) }}</pre>
                  </div>
                  <div v-if="!lastTrace.trace.toolCalls?.length" class="trace-empty-block">
                    <p>这次 trace 没有记录到工具输入。</p>
                    <p v-if="lastTrace.trace.toolsUsed?.length" class="trace-note">已调用：{{ lastTrace.trace.toolsUsed.join(' · ') }}</p>
                  </div>
                </div>
              </div>
              <p v-else class="side-empty">发送消息后，这里会显示 Agent 路由、意图和耗时。</p>
            </section>

            <section v-if="access.admin" class="side-card monitor-card">
              <div class="card-heading">
                <div>
                  <span class="kicker">Runtime</span>
                  <h2>服务运行状态</h2>
                </div>
                <button class="link-button" @click="loadMonitor">刷新</button>
              </div>
              <p class="muted">当前服务进程的聚合统计</p>
              <div class="mini-stats">
                <div><strong>{{ totalRequests }}</strong><span>请求</span></div>
                <div><strong>{{ agentCount }}</strong><span>Agent</span></div>
                <div><strong>{{ activeAlerts.length }}</strong><span>告警</span></div>
              </div>
              <div v-if="activeAlerts.length" class="alert-note">{{ activeAlerts[0].detail || activeAlerts[0].title }}</div>
              <p v-else class="healthy-note">当前没有活跃告警。</p>
            </section>
          </div>
        </aside>
      </div>
    </section>

    <section v-else-if="activeView === 'knowledge' && access.knowledge" class="page page-knowledge">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Knowledge operations</span>
          <h1>知识库</h1>
          <p>搜索、补充和维护客服 Agent 使用的知识片段。</p>
        </div>
        <div class="count-display"><strong>{{ knowledgeCount }}</strong><span>chunks</span></div>
      </div>

      <div class="knowledge-layout">
        <section class="workspace-card search-workspace">
          <div class="card-heading">
            <div><span class="kicker">Retrieval</span><h2>检索知识</h2></div>
            <code>POST /search</code>
          </div>
          <div class="search-line">
            <input v-model="searchQuery" placeholder="例如：退款多久到账" @keydown.enter="searchKnowledge" />
            <select v-if="settings.backend === 'python'" v-model="searchStrategy" aria-label="检索方式"><option value="vector">向量</option><option value="bm25">BM25</option><option value="hybrid">混合检索</option></select>
            <button @click="searchKnowledge" :disabled="busy || !searchQuery.trim()">搜索</button>
          </div>
          <div v-if="settings.backend === 'python'" class="form-pair search-filters">
            <label><span>限定产品版本</span><input v-model="searchVersion" placeholder="选填，例如 2026.10" /></label>
            <label><span>限定知识库</span><input v-model="searchKnowledgeBase" placeholder="选填，例如 default" /></label>
          </div>
          <div v-if="searchResults.length" class="result-list">
            <article v-for="(item, index) in searchResults" :key="item.id || item.title || index" class="result-item">
              <span class="result-number">{{ String(index + 1).padStart(2, '0') }}</span>
              <div>
                <div class="result-title"><strong>{{ item.title || '未命名文档' }}</strong><small>score {{ item.score ?? '-' }}</small></div>
                <p>{{ item.content }}</p>
              </div>
            </article>
          </div>
          <div v-else class="workspace-empty">输入客户问题开始搜索。</div>
        </section>

        <section v-if="settings.backend !== 'python' || canEditKnowledge" class="workspace-card import-workspace">
          <div class="card-heading">
            <div><span class="kicker">Ingestion</span><h2>添加知识</h2></div>
            <code>ChromaDB</code>
          </div>
          <label><span>标题</span><input v-model="docTitle" placeholder="退款补充政策" /></label>
          <label><span>来源地址</span><input v-model="docSource" type="url" placeholder="https://…" /></label>
          <label><span>产品版本</span><input v-model="docVersion" placeholder="例如 2026.10" /></label>
          <label><span>内容</span><textarea v-model="docContent" rows="7" placeholder="输入客服规范、产品说明或排障流程"></textarea></label>
          <div class="side-actions">
            <button @click="submitKnowledge" :disabled="busy || !docTitle.trim() || !docContent.trim()">添加文档</button>
            <label class="upload-button">上传文件<input type="file" accept=".txt,.md,.json" @change="handleUpload" /></label>
          </div>
        </section>
      </div>
      <KnowledgeDocuments v-if="settings.backend === 'python' && authSession" :key="authSession.organization_id" :settings="settings" :session="authSession" :refresh-key="knowledgeRefresh" @updated="loadStats" />

      <section class="workspace-card skills-workspace">
        <div class="card-heading">
          <div><span class="kicker">Loaded skills</span><h2>已加载能力</h2></div>
          <button v-if="settings.backend !== 'python' || authSession?.role === 'admin'" class="link-button" @click="reloadSkillSet">重新加载</button>
        </div>
        <div class="skill-table">
          <div v-for="skill in skillsData.skills" :key="skill.name" class="skill-item">
            <span class="skill-dot"></span><strong>{{ skill.name }}</strong><span>{{ skill.description || '业务规范能力' }}</span><small>{{ skill.content_chars || 0 }} chars</small>
          </div>
          <div v-if="!skillsData.skills.length" class="workspace-empty">暂无已加载 Skill。</div>
        </div>
      </section>
    </section>

    <section v-else-if="activeView === 'evaluation' && access.admin" class="page page-evaluation">
      <div class="page-heading">
        <div class="heading-copy">
          <span class="kicker">Evaluation lab</span>
          <h1>评测 Agent</h1>
          <p>运行 FastAPI 内置评测，查看意图识别、对话质量和回归结果。</p>
        </div>
        <button @click="runEvaluation" :disabled="busy || (settings.backend === 'python' && authSession?.role !== 'admin')">{{ busy ? '运行中...' : '运行评测' }}</button>
      </div>
      <p v-if="settings.backend === 'python' && authSession?.role !== 'admin'" class="muted">请使用管理员账号运行评测与管理基线。</p>
      <section v-if="settings.backend === 'python' && authSession?.role === 'admin'" class="workspace-card report-history">
        <div class="card-heading"><h2>历史报告</h2><button class="quiet-button" @click="loadReports">刷新</button></div>
        <div v-for="report in reports" :key="report.run_id" class="document-row">
          <button class="quiet-button" @click="evalData = report">{{ report.timestamp }} · {{ formatPercent(report.pass_rate) }}</button>
          <button :disabled="busy" @click="promoteReport(report)">设为回归基线</button>
        </div>
      </section>

      <div v-if="evalData" class="evaluation-content">
        <div class="evaluation-summary">
          <div class="score-hero"><span>Pass rate</span><strong>{{ formatPercent(evalData.pass_rate) }}</strong><small>{{ evalData.passed }} / {{ evalData.total }} cases passed</small></div>
          <div><span>通过</span><strong>{{ evalData.passed }}</strong></div>
          <div><span>总数</span><strong>{{ evalData.total }}</strong></div>
          <div><span>回归</span><strong :class="evalData.regressions?.length ? 'danger' : 'success'">{{ evalData.regressions?.length || 0 }}</strong></div>
        </div>
        <div class="evaluation-layout">
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Scores</span><h2>平均评分</h2></div></div>
            <div class="score-list">
              <div v-for="(value, key) in evalData.avg_scores" :key="key"><span>{{ key }}</span><i><b :style="{ width: `${Math.min(Number(value) * 100, 100)}%` }"></b></i><strong>{{ Number(value).toFixed(2) }}</strong></div>
            </div>
          </section>
          <section class="workspace-card">
            <div class="card-heading"><div><span class="kicker">Recommendations</span><h2>优化建议</h2></div></div>
            <div v-if="evalData.recommendations?.length" class="recommendations"><p v-for="(item, index) in evalData.recommendations" :key="index">{{ item }}</p></div>
            <div v-else class="workspace-empty">本次评测没有返回额外建议。</div>
          </section>
        </div>
      </div>
      <p v-if="evalData?.coverage != null">有效评测覆盖率 {{ formatPercent(evalData.coverage) }} · 失败 {{ evalData.failed }} · 报告 {{ evalData.run_id }}<br />{{ evalData.metadata?.baseline_comparison?.reasons?.join('；') }}</p>
      <div v-if="!evalData" class="evaluation-empty"><div class="empty-symbol">◎</div><h2>还没有评测结果</h2><p>点击右上角运行一次评测。</p></div>
    </section>
  </main>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import OperationsPanel from './components/OperationsPanel.vue'
import KnowledgeDocuments from './components/KnowledgeDocuments.vue'
import AdminDashboard from './components/AdminDashboard.vue'
import AnswerEvidence from './components/AnswerEvidence.vue'
import AnswerFeedback from './components/AnswerFeedback.vue'
import DiagnosticsPanel from './components/DiagnosticsPanel.vue'
import { identityKey, workspaceAccess } from './lib/access.js'
import { evaluationReports, promoteEvaluation } from './lib/operations.js'
import {
  addKnowledge,
  availableBackendIds,
  backendMeta,
  createInitialSettings,
  reloadSkills,
  requestChatStream,
  requestHealth,
  requestKnowledgeStats,
  requestMonitor,
  requestSearch,
  requestToolTrace,
  requestSkills,
  runEvaluation as requestEvaluation,
  saveSettings,
  uploadKnowledge
} from './lib/backends'

const availableBackends = availableBackendIds()
const settings = reactive(createInitialSettings())
const activeView = ref('chat')
const authSession = ref(null), operationsRefresh = ref(0), knowledgeRefresh = ref(0), reports = ref([])
const access = computed(() => workspaceAccess(authSession.value))
const canEditKnowledge = computed(() => access.value.knowledge)
const operationsPanel = ref(null)
const roleName = computed(() => ({ admin: '管理员', customer: '普通用户', support: '客服', kb_admin: '知识库管理员' })[authSession.value?.role] || '-')
let sessionGeneration = 0
const messages = ref([])
const draft = ref('')
const busy = ref(false)
const chatPending = ref(false)
let chatController
let chatTimer
let scrollScheduled = false
const healthOk = ref(false)
const healthLabel = ref('未检查')
const statusText = ref('')
const knowledgeCount = ref('-')
const searchQuery = ref('退款多久能到账')
const searchResults = ref([])
const searchStrategy = ref('vector'), docSource = ref(''), docVersion = ref('')
const searchVersion = ref(''), searchKnowledgeBase = ref('')
const docTitle = ref('退款补充政策')
const docContent = ref('大促期间退款审核时间可能延长到 3-5 个工作日。')
const messageList = ref(null)
const monitorData = ref({ agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] })
const skillsData = ref({ count: 0, skills: [], errors: [] })
const lastResponse = ref(null)
const lastTrace = ref(null)
const evalData = ref(null)
const toast = ref('')
let toastTimer
let messageSequence = 0

const currentBackend = computed(() => backendMeta(settings.backend, settings))
const docsUrl = computed(() => `${currentBackend.value.baseUrl}/docs`)
const userInitial = computed(() => (authSession.value?.user?.display_name || settings.userId || 'U').slice(0, 1).toUpperCase())
const activeAlerts = computed(() => monitorData.value.active_alerts || [])
const agentCount = computed(() => Object.keys(monitorData.value.agent_stats || {}).length)
const totalRequests = computed(() => Object.values(monitorData.value.agent_stats || {}).reduce((sum, item) => sum + Number(item.total || 0), 0))

watch(() => settings.conversationId, persist)
watch(activeView, value => { if (value === 'evaluation' && authSession.value?.role === 'admin') loadReports() })
onMounted(() => {
  refreshConsole()
})

onBeforeUnmount(() => {
  chatController?.abort()
  clearInterval(chatTimer)
})

function persist() { saveSettings(settings) }

function sessionChanged(session) {
  const userId = session?.user_id || session?.user?.id
  if (identityKey(authSession.value) !== identityKey(session) || !session) {
    sessionGeneration += 1
    chatController?.abort()
    clearInterval(chatTimer); chatController = null; chatTimer = null
    busy.value = false; chatPending.value = false; draft.value = ''
    messages.value = []; lastResponse.value = null; lastTrace.value = null
    searchResults.value = []; evalData.value = null; reports.value = []; settings.conversationId = ''
    docTitle.value = ''; docContent.value = ''; docSource.value = ''; docVersion.value = ''
    searchQuery.value = ''; searchVersion.value = ''; searchKnowledgeBase.value = ''
    statusText.value = ''
    knowledgeCount.value = '-'
    monitorData.value = { agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] }
    skillsData.value = { count: 0, skills: [], errors: [] }
  }
  authSession.value = session
  if (userId) settings.userId = userId
  else settings.userId = ''
  if (!access.value.views.includes(activeView.value)) activeView.value = 'chat'
  knowledgeRefresh.value += 1
  persist(); refreshConsole()
}

function invalidateSession() {
  if (operationsPanel.value) operationsPanel.value.invalidateSession()
  else sessionChanged(null)
}

function handleFailure(error, generation, notify = true) {
  if (generation !== sessionGeneration) return
  if ([401, 403].includes(error.status)) {
    invalidateSession()
    showToast('登录已失效或权限发生变化，请重新登录')
  } else if (notify) showToast(error.message)
}

function safeSourceUrl(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : '' } catch { return '' }
}

async function loadReports() {
  if (!access.value.admin) return
  const generation = sessionGeneration
  try { const data = await evaluationReports(settings); if (generation === sessionGeneration) reports.value = data.items } catch (error) { handleFailure(error, generation) }
}

async function promoteReport(report) {
  if (!access.value.admin) return
  const generation = sessionGeneration
  busy.value = true
  try { await promoteEvaluation(settings, report.run_id); if (generation === sessionGeneration) showToast('已设置回归基线，后续评测会检查版本是否可比') }
  catch (error) { handleFailure(error, generation) } finally { if (generation === sessionGeneration) busy.value = false }
}

function switchBackend(type) {
  if (chatPending.value) return
  settings.backend = type
  sessionChanged(null)
  settings.conversationId = ''
  persist()
  healthOk.value = false
  healthLabel.value = '未检查'
  messages.value = []
  searchResults.value = []
  lastResponse.value = null
  lastTrace.value = null
  refreshConsole()
}

async function refreshConsole() {
  const requests = [checkHealth()]
  if (access.value.knowledge) requests.push(loadStats(), loadSkills())
  if (access.value.admin) requests.push(loadMonitor())
  await Promise.allSettled(requests)
}

async function checkHealth() {
  try {
    const data = await requestHealth(settings.backend, settings)
    healthOk.value = data.status === 'ok'
    healthLabel.value = data.status || 'ok'
    statusText.value = JSON.stringify(data, null, 2)
  } catch (error) {
    healthOk.value = false
    healthLabel.value = '不可用'
    statusText.value = error.message

  }
}

async function loadStats() {
  if (!access.value.knowledge) return
  const generation = sessionGeneration
  try {
    const data = await requestKnowledgeStats(settings.backend, settings)
    if (generation === sessionGeneration) knowledgeCount.value = data.total_chunks ?? data.totalChunks ?? '-'
  } catch (error) {
    if (generation === sessionGeneration) knowledgeCount.value = '-'
    handleFailure(error, generation, false)
  }
}

async function loadMonitor() {
  if (!access.value.admin) return
  const generation = sessionGeneration
  try {
    const data = await requestMonitor(settings.backend, settings)
    if (generation === sessionGeneration) monitorData.value = data
  } catch (error) {
    if (generation === sessionGeneration) monitorData.value = { agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] }
    handleFailure(error, generation, false)
  }
}

async function loadSkills() {
  if (!access.value.knowledge) return
  const generation = sessionGeneration
  try {
    const data = await requestSkills(settings.backend, settings)
    if (generation === sessionGeneration) skillsData.value = data
  } catch (error) {
    if (generation === sessionGeneration) skillsData.value = { count: 0, skills: [], errors: [] }
    handleFailure(error, generation, false)
  }
}

async function reloadSkillSet() {
  if (!access.value.admin) return
  const generation = sessionGeneration
  busy.value = true
  try {
    const data = await reloadSkills(settings.backend, settings)
    if (generation !== sessionGeneration) return
    skillsData.value = data
    showToast('Skills 已重新加载')
  } catch (error) {
    handleFailure(error, generation)
  } finally { if (generation === sessionGeneration) busy.value = false }
}

async function sendMessage() {
  const content = draft.value.trim()
  if (!content || busy.value) return
  if (settings.backend === 'python' && !access.value.authenticated) { showToast('请先登录后再发送消息'); return }
  const generation = sessionGeneration
  messages.value.push({ id: createMessageId(), role: 'user', content })
  draft.value = ''
  busy.value = true
  chatPending.value = true
  const controller = new AbortController()
  chatController = controller
  const reply = reactive({ id: createMessageId(), role: 'assistant', content: '', streaming: true,
    elapsed: 0, progress: [{ message: '请求已发送，等待处理', status: 'pending' }], trace: null })
  messages.value.push(reply)
  const startedAt = Date.now()
  const timer = setInterval(() => { reply.elapsed = Math.floor((Date.now() - startedAt) / 1000) }, 1000)
  chatTimer = timer
  scheduleChatScroll()
  try {
    const response = await requestChatStream(settings.backend, settings, content, (event) => {
      if (generation !== sessionGeneration) return
      if (event.type === 'session' && !settings.conversationId) {
        settings.conversationId = event.conv_id
        persist()
      } else if (event.type === 'stage') {
        for (const step of reply.progress) {
          if (step.status === 'pending' && !step.toolId) step.status = 'done'
        }
        reply.progress.push({ message: event.message, status: 'pending' })
      } else if (event.type === 'answer_start') {
        reply.content = ''
      } else if (event.type === 'text_delta') {
        reply.content += event.text
      } else if (event.type === 'tool_start') {
        const labels = { search_knowledge_base: '检索知识库', lookup_error_code: '查询错误码',
          build_diagnostic_plan: '制定排查步骤', check_billing_fields: '检查账单信息',
          compare_amounts: '比较金额', inspect_request_context: '检查请求信息',
          suggest_required_fields: '核对所需信息', create_handoff_summary: '整理人工交接摘要' }
        reply.progress.push({ message: `正在${labels[event.tool_name] || '执行工具'}`,
          toolId: event.tool_use_id, status: 'pending' })
      } else if (event.type === 'tool_end') {
        const step = reply.progress.find((item) => item.toolId === event.tool_use_id && item.status === 'pending')
        const success = event.success && event.result_success !== false
        if (step) {
          step.status = success ? 'done' : 'error'
          step.message = step.message.replace(/^正在/, '') + (success ? '完成' : '失败')
        }
        if (!reply.trace) reply.trace = { toolCalls: [] }
        reply.trace.toolCalls.push(event)
      }
      scheduleChatScroll()
    }, controller.signal)
    if (generation !== sessionGeneration) return
    if (response.conversationId && !settings.conversationId) {
      settings.conversationId = response.conversationId
      persist()
    }
    lastResponse.value = response
    reply.streaming = false
    reply.content = response.response
    reply.sources = response.sources; reply.tasks = response.tasks; reply.warnings = response.warnings
    reply.requestId = response.requestId; reply.feedbackAvailable = response.feedbackAvailable
    if (response.tasks?.length) operationsRefresh.value += 1
    for (const step of reply.progress) { if (step.status === 'pending') step.status = 'done' }
    const trace = await loadToolTrace(response.requestId)
    if (generation !== sessionGeneration) return
    lastTrace.value = trace
    const meta = access.value.admin ? [response.intent, response.primaryAgent || response.agentType, response.knowledgeUsed ? 'RAG' : '', response.escalated ? '转人工' : ''].filter(Boolean).join(' · ') : ''
    reply.meta = meta
    reply.trace = lastTrace.value?.trace || reply.trace
    await loadMonitor()
  } catch (error) {
    if (generation !== sessionGeneration) return
    if ([401, 403].includes(error.status)) { handleFailure(error, generation); return }
    const stopped = error.name === 'AbortError'
    reply.meta = stopped ? '已停止生成' : '请求失败'
    if (!reply.content) reply.content = stopped ? '已停止生成。' : error.message
    else if (!stopped) reply.content += `\n\n${error.message}`
    for (const step of reply.progress) { if (step.status === 'pending') step.status = stopped ? 'stopped' : 'error' }
  } finally {
    reply.streaming = false
    clearInterval(timer)
    if (generation === sessionGeneration) {
      chatController = null; chatTimer = null
      chatPending.value = false; busy.value = false
    }
    await nextTick()
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
  }
}

function stopResponse() { chatController?.abort() }

function scheduleChatScroll() {
  if (scrollScheduled) return
  scrollScheduled = true
  requestAnimationFrame(async () => {
    await nextTick()
    messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'auto' })
    scrollScheduled = false
  })
}

function usePrompt(prompt) { draft.value = prompt }

function clearConversation() {
  if (chatPending.value) return
  messages.value = []
  lastResponse.value = null
  lastTrace.value = null
  settings.conversationId = ''
  persist()
}

async function searchKnowledge() {
  if (!access.value.knowledge) return
  const generation = sessionGeneration
  busy.value = true
  try {
    const data = await requestSearch(settings.backend, settings, searchQuery.value, 5, searchStrategy.value, true,
      { product_version: searchVersion.value, knowledge_base_id: searchKnowledgeBase.value })
    if (generation !== sessionGeneration) return
    searchResults.value = data.results || []
    showToast(`检索完成，返回 ${searchResults.value.length} 条结果`)
  } catch (error) {
    handleFailure(error, generation)
  } finally { if (generation === sessionGeneration) busy.value = false }
}

async function submitKnowledge() {
  if (!access.value.knowledge) return
  const generation = sessionGeneration
  busy.value = true
  try {
    const data = await addKnowledge(settings.backend, settings, [{ title: docTitle.value.trim(), content: docContent.value.trim(), source_url: docSource.value.trim(), product_version: docVersion.value.trim() }])
    if (generation !== sessionGeneration) return
    knowledgeRefresh.value += 1
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
    showToast('文档已添加')
  } catch (error) {
    handleFailure(error, generation)
  } finally { if (generation === sessionGeneration) busy.value = false }
}

async function handleUpload(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file || !access.value.knowledge) return
  const generation = sessionGeneration
  busy.value = true
  try {
    const data = await uploadKnowledge(settings.backend, settings, file)
    if (generation !== sessionGeneration) return
    knowledgeRefresh.value += 1
    statusText.value = JSON.stringify(data, null, 2)
    await loadStats()
    showToast(`${file.name} 导入成功`)
  } catch (error) {
    handleFailure(error, generation)
  } finally { if (generation === sessionGeneration) busy.value = false }
}

async function runEvaluation() {
  if (!access.value.admin) return
  const generation = sessionGeneration
  busy.value = true
  try {
    const data = await requestEvaluation(settings.backend, settings)
    if (generation !== sessionGeneration) return
    evalData.value = data
    if (settings.backend === 'python') await loadReports()
    showToast('评测完成')
  } catch (error) {
    handleFailure(error, generation)
  } finally { if (generation === sessionGeneration) busy.value = false }
}

async function loadToolTrace(requestId) {
  if (!access.value.admin) return null
  const generation = sessionGeneration
  try {
    return await requestToolTrace(settings.backend, settings, requestId)
  } catch (error) {
    handleFailure(error, generation, false)
    return null
  }
}

function formatPercent(value) {
  const number = Number(value || 0)
  return `${(number <= 1 ? number * 100 : number).toFixed(1)}%`
}

function formatJson(value) {
  try {
    return JSON.stringify(value ?? {}, null, 2)
  } catch {
    return String(value ?? '')
  }
}

function createMessageId() {
  messageSequence += 1
  return `message-${Date.now()}-${messageSequence}`
}

function showToast(message) {
  toast.value = message
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => { toast.value = '' }, 2600)
}
</script>
