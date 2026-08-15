<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { apiGet, apiPost, apiUpload, setCsrfToken } from './api'

type Toast = { id: number; level: string; text: string }
type Filters = { market: string; status: string; purchased: string; date: string; q: string }

const bootstrap = ref<any>(null)
const result = ref<any>({ items: [], pagination: { page: 1, pages: 1, total: 0 }, summary: {} })
const settings = ref<any>(null)
const filters = reactive<Filters>({ market: '', status: '', purchased: '', date: '', q: '' })
const page = ref(1)
const perPage = ref(8)
const jumpPage = ref<number | ''>('')
const loading = ref(true)
const busy = ref('')
const calendarOpen = ref(false)
const settingsOpen = ref(false)
const logsOpen = ref(false)
const logsData = ref<any>({ items: [], total: 0 })
const logsCategory = ref('')
const logsLoading = ref(false)
const settingsTab = ref('recommendations')
const calendarDate = ref(new Date())
const calendarStats = ref<Record<string, any>>({})
const toasts = ref<Toast[]>([])
let toastId = 0
let searchTimer = 0

const profileLabels: Record<string, string> = { crs: '比分', had: '胜平负', ttg: '进球数' }
const statusLabels: Record<string, string> = { pending: '待结算', won: '已中奖', lost: '未中奖', void: '已取消' }

const queryString = computed(() => {
  const query = new URLSearchParams({ page: String(page.value), per_page: String(perPage.value) })
  Object.entries(filters).forEach(([key, value]) => value && query.set(key, value))
  return query.toString()
})

const perPageOptions = [8, 16, 32, 50]
const paginationStart = computed(() => {
  const total = result.value.pagination.total || 0
  if (total === 0) return 0
  return (result.value.pagination.page - 1) * (result.value.pagination.per_page || perPage.value) + 1
})
const paginationEnd = computed(() => {
  const total = result.value.pagination.total || 0
  return Math.min(result.value.pagination.page * (result.value.pagination.per_page || perPage.value), total)
})
const visiblePages = computed(() => {
  const current = result.value.pagination.page || 1
  const total = result.value.pagination.pages || 1
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1)
  if (current <= 4) return [1, 2, 3, 4, 5, '…', total]
  if (current >= total - 3) return [1, '…', total - 4, total - 3, total - 2, total - 1, total]
  return [1, '…', current - 1, current, current + 1, '…', total]
})

function goToPage(target: number) {
  const pages = result.value.pagination.pages || 1
  page.value = Math.min(Math.max(1, target), pages)
  loadPlans()
}

function jumpToPage() {
  const target = Number(jumpPage.value)
  if (!Number.isFinite(target) || target < 1) {
    toast('请输入有效页码', 'warn')
    return
  }
  goToPage(target)
  jumpPage.value = ''
}

const summary = computed(() => result.value.summary || {})
const calendarTitle = computed(() => `${calendarDate.value.getFullYear()}年${calendarDate.value.getMonth() + 1}月`)
const calendarCells = computed(() => {
  const year = calendarDate.value.getFullYear()
  const month = calendarDate.value.getMonth()
  const first = new Date(year, month, 1)
  const mondayIndex = (first.getDay() + 6) % 7
  const days = new Date(year, month + 1, 0).getDate()
  const cells: Array<{ day: number; date: string } | null> = Array.from({ length: mondayIndex }, () => null)
  for (let day = 1; day <= days; day += 1) {
    cells.push({ day, date: `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}` })
  }
  while (cells.length % 7) cells.push(null)
  return cells
})

function toast(text: string, level = 'ok') {
  const id = ++toastId
  toasts.value.push({ id, level, text })
  window.setTimeout(() => { toasts.value = toasts.value.filter((item) => item.id !== id) }, 4200)
}

async function loadPlans(reset = false) {
  if (reset) page.value = 1
  loading.value = true
  try {
    result.value = await apiGet(`/api/v1/plans?${queryString.value}`)
    page.value = result.value.pagination.page
    perPage.value = result.value.pagination.per_page || perPage.value
  } catch (error) {
    toast((error as Error).message, 'error')
  } finally {
    loading.value = false
  }
}

async function loadSettings() {
  try { settings.value = await apiGet('/api/v1/settings') }
  catch (error) { toast((error as Error).message, 'error') }
}

async function loadCalendar() {
  const query = new URLSearchParams({
    year: String(calendarDate.value.getFullYear()),
    month: String(calendarDate.value.getMonth() + 1),
  })
  Object.entries(filters).forEach(([key, value]) => key !== 'date' && value && query.set(key, value))
  try { calendarStats.value = (await apiGet<any>(`/api/v1/calendar?${query}`)).days }
  catch (error) { toast((error as Error).message, 'error') }
}

async function openCalendar() {
  calendarOpen.value = true
  await loadCalendar()
}

async function moveCalendar(offset: number) {
  calendarDate.value = new Date(calendarDate.value.getFullYear(), calendarDate.value.getMonth() + offset, 1)
  await loadCalendar()
}

function selectCalendarDate(value: string) {
  filters.date = value
  calendarOpen.value = false
  loadPlans(true)
}

function planMatchesFilters(plan: any) {
  if (filters.market && plan.market !== filters.market) return false
  if (filters.status && plan.status !== filters.status) return false
  if (filters.date && plan.recommendation_date !== filters.date) return false
  if (filters.purchased && plan.purchased !== (filters.purchased === 'true')) return false
  const search = filters.q.trim().toLowerCase()
  if (search) {
    const values = [plan.plan_id, ...plan.legs.flatMap((leg: any) => [leg.match_num, leg.league, leg.home, leg.away])]
    if (!values.some((value: unknown) => String(value || '').toLowerCase().includes(search))) return false
  }
  return true
}

function applyActionData(data: any, fallbackPlanId = '') {
  if (!data) return
  if (data.summary) result.value.summary = data.summary
  const planId = data.plan?.plan_id || fallbackPlanId
  const index = result.value.items.findIndex((item: any) => item.plan_id === planId)
  if (data.deleted || (data.plan && !planMatchesFilters(data.plan))) {
    if (index >= 0) result.value.items.splice(index, 1)
    if (data.deleted) {
      result.value.pagination.total = Math.max(0, result.value.pagination.total - 1)
      result.value.pagination.pages = Math.max(1, Math.ceil(result.value.pagination.total / result.value.pagination.per_page))
    }
  } else if (data.plan && index >= 0) {
    result.value.items[index] = data.plan
  } else if (data.plan && page.value === 1) {
    result.value.items.unshift(data.plan)
  }
}

async function pollPlanTask(kind: 'settle' | 'ai', planId: string) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 1500))
    const query = new URLSearchParams({ kind, plan_id: planId })
    Object.entries(filters).forEach(([key, value]) => value && query.set(key, value))
    try {
      const task = await apiGet<any>(`/api/v1/plan-task?${query}`)
      if (task.status === 'finished') {
        applyActionData(task, planId)
        toast(task.detail, task.level)
        return
      }
    } catch (error) {
      toast((error as Error).message, 'error')
      return
    }
  }
  toast('后台任务仍在执行，稍后可继续查看当前计划', 'warn')
}

async function action(path: string, payload: Record<string, unknown>, key = path) {
  busy.value = key
  try {
    const requestPayload = { ...payload, filters: { ...filters } }
    const response = await apiPost<any>(path, requestPayload)
    toast(response.detail || '操作成功', response.level)
    const planId = String(payload.plan_id || '')
    applyActionData(response.data, planId)
    if (planId && path === '/api/v1/actions/settle-plan' && response.level === 'ok') {
      void pollPlanTask('settle', planId)
    } else if (planId && path === '/api/v1/actions/analyze-plan' && response.level === 'ok') {
      void pollPlanTask('ai', planId)
    }
    return response
  } catch (error) {
    toast((error as Error).message, 'error')
  } finally {
    busy.value = ''
  }
}

async function recommend() {
  if (!bootstrap.value) return
  await action('/api/v1/actions/recommend', bootstrap.value.recommendation_request, 'recommend')
  bootstrap.value = await apiGet('/api/v1/bootstrap')
}

async function logout() {
  try {
    await apiPost('/api/v1/logout', {})
    window.location.assign('/login')
  } catch (error) { toast((error as Error).message, 'error') }
}

async function deletePlan(plan: any) {
  if (!window.confirm(`确定删除计划 ${plan.plan_id}？此操作不能撤销。`)) return
  await action('/api/v1/actions/delete-plan', { plan_id: plan.plan_id }, `delete-${plan.plan_id}`)
}

const ticketFileInput = ref<HTMLInputElement | null>(null)
const pendingUploadPlan = ref<any>(null)
const ticketZoomUrl = ref<string>('')
function pickTicket(plan: any) {
  if (pendingUploadPlan.value === plan) {
    // 已处于等待粘贴状态，再次点击改为选择文件
    pendingUploadPlan.value = null
    ticketFileInput.value?.click()
    return
  }
  pendingUploadPlan.value = plan
  toast(`已就绪：请直接按 Ctrl+V 粘贴 ${plan.plan_id} 的实票截图，或再次点击按钮选择文件`, 'info')
}
async function uploadTicketFile(file: File, plan: any) {
  busy.value = `ticket-${plan.plan_id}`
  try {
    const response = await apiUpload<any>('/api/v1/actions/upload-ticket', file, plan.plan_id)
    toast(response.detail || '实票已上传', response.level)
    if (response.data?.plan) applyActionData(response.data, plan.plan_id)
  } catch (error) {
    toast((error as Error).message, 'error')
  } finally {
    busy.value = ''
  }
}
async function uploadTicket(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  const plan = pendingUploadPlan.value
  input.value = ''
  pendingUploadPlan.value = null
  if (!file || !plan) return
  await uploadTicketFile(file, plan)
}
function handlePaste(event: ClipboardEvent) {
  const plan = pendingUploadPlan.value
  if (!plan) return
  const file = Array.from(event.clipboardData?.items || [])
    .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
    .map((item) => item.getAsFile())
    .find((item): item is File => !!item)
    || event.clipboardData?.files?.[0]
  if (!file) return
  event.preventDefault()
  pendingUploadPlan.value = null
  void uploadTicketFile(file, plan)
}
async function deleteTicket(plan: any) {
  if (!window.confirm(`确定移除计划 ${plan.plan_id} 的实票图片？`)) return
  await action('/api/v1/actions/delete-ticket', { plan_id: plan.plan_id }, `delticket-${plan.plan_id}`)
}

async function deleteLeg(plan: any, leg: any) {
  if (!window.confirm(`确定从计划中删除 ${leg.match_num}？`)) return
  await action('/api/v1/actions/delete-leg', { plan_id: plan.plan_id, match_id: leg.match_id }, `leg-${leg.match_id}`)
}

async function saveSection(section: string, payload: Record<string, unknown>) {
  busy.value = `settings-${section}`
  try {
    const response = await apiPost<any>(`/api/v1/settings/${section}`, payload)
    settings.value = response.data
    toast(response.detail || '设置已保存')
  } catch (error) { toast((error as Error).message, 'error') }
  finally { busy.value = '' }
}

function saveProfiles() {
  saveSection('recommendations', JSON.parse(JSON.stringify(settings.value.profiles)))
}

const recipientText = ref('')
function syncRecipientText() {
  recipientText.value = (settings.value?.recipients || []).map((item: any) => item.email).join('\n')
}
function saveRecipients() {
  const recipients = recipientText.value.split(/[\n,;]+/).map((value) => value.trim()).filter(Boolean)
  saveSection('recipients', { recipients })
}

const authCode = ref('')
function saveMail() {
  saveSection('mail', { ...settings.value.mail, new_auth_code: authCode.value }).then(() => { authCode.value = '' })
}

const recommendationTimesText = ref('')
function syncRuntimeText() { recommendationTimesText.value = (settings.value?.runtime?.recommendation_times || []).join(', ') }
function saveRuntime() {
  saveSection('runtime', {
    ...settings.value.runtime,
    recommendation_times: recommendationTimesText.value.split(/[,，\s]+/).filter(Boolean),
  })
}

const modelForm = reactive({ id: '', provider: 'qwen', display_name: '', base_url: '', model_name: '', api_key: '' })
function selectedProvider() { return settings.value?.providers?.find((item: any) => item.code === modelForm.provider) }
function applyProviderDefaults() {
  const provider = selectedProvider()
  if (!provider) return
  modelForm.base_url = provider.default_base_url
  modelForm.model_name = provider.default_model
  if (!modelForm.display_name) modelForm.display_name = provider.name
}
function editModel(model?: any) {
  Object.assign(modelForm, model ? {
    id: model.id, provider: model.provider, display_name: model.display_name,
    base_url: model.base_url, model_name: model.model_name, api_key: '',
  } : { id: '', provider: 'qwen', display_name: '', base_url: '', model_name: '', api_key: '' })
  if (!model) applyProviderDefaults()
}
async function saveModel() {
  busy.value = 'model-save'
  try {
    const response = await apiPost<any>('/api/v1/settings/models', { ...modelForm })
    settings.value = response.data.settings
    modelForm.id = response.data.model_config_id
    modelForm.api_key = ''
    toast(response.detail || '模型已保存')
  } catch (error) { toast((error as Error).message, 'error') }
  finally { busy.value = '' }
}
async function testModel(id: string) {
  busy.value = `model-test-${id}`
  try {
    const response = await apiPost<any>('/api/v1/settings/model-test', { model_config_id: id })
    toast(response.detail || '正在测试', 'warn')
    const taskId = response.data.task_id
    for (let attempts = 0; attempts < 120; attempts += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000))
      const task = await apiGet<any>(`/api/v1/tasks/${taskId}`)
      if (task.status === 'finished') {
        toast(task.detail, task.level)
        await loadSettings()
        break
      }
    }
  } catch (error) { toast((error as Error).message, 'error') }
  finally { busy.value = '' }
}

async function removeModel(id: string) {
  if (!window.confirm('确定删除这个大模型配置？')) return
  busy.value = `model-delete-${id}`
  try {
    const response = await apiPost<any>('/api/v1/settings/model-delete', { model_config_id: id })
    settings.value = response.data
    toast(response.detail || '模型已删除')
  } catch (error) { toast((error as Error).message, 'error') }
  finally { busy.value = '' }
}

async function activateModel(id: string) {
  if (!window.confirm('确定把这个大模型切换为当前使用？切换后立即影响 AI 分析。')) return
  busy.value = `model-activate-${id}`
  try {
    const response = await apiPost<any>('/api/v1/settings/model-activate', { model_config_id: id })
    settings.value = response.data
    toast(response.detail || '已切换为当前模型')
  } catch (error) { toast((error as Error).message, 'error') }
  finally { busy.value = '' }
}

function money(value: string | number) { return `¥${Number(value || 0).toFixed(2)}` }
function formatTime(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
function shortId(value: string) { return value.length > 20 ? `${value.slice(0, 9)}…${value.slice(-6)}` : value }
function formatResult(market: string, result: any) {
  if (result.status === 'pending') return '待公布'
  if (result.status === 'void') return '比赛无效'
  const score = result.score
  if (market === 'crs') return `比分 ${result.outcome}`
  if (market === 'had') return `${result.outcome}（${score}）`
  if (market === 'ttg') return `${result.outcome}球（${score}）`
  return result.market_result
}
// 只有 TTG（进球数）玩法的取值是纯数字如"2"，需要追加"球"字以便和胜平负的
// "3/1/0" 区分；CRS（比分）的取值已经是"2:0"形式自带冒号分隔，HAD 的"3/1/0"
// 是胜平负代码，二者均不追加。
function pickWithBall(value: string | null | undefined, market: string) {
  if (!value) return value
  if (market !== 'ttg') return value
  if (/球$/.test(value)) return value
  return `${value}球`
}
async function applyAiSuggestion(plan: any, leg: any) {
  if (!leg.ai_suggestion) return
  await action('/api/v1/actions/update-leg', {
    plan_id: plan.plan_id,
    match_id: leg.match_id,
    option_code: leg.ai_suggestion.code
  }, `ai-pick-${leg.match_id}`)
}
const categoryLabels: Record<string, string> = { recommend: '推荐', settle: '结算', ai: 'AI分析', mail: '邮件' }
async function loadLogs() {
  logsLoading.value = true
  try {
    const query = new URLSearchParams({ limit: '200' })
    if (logsCategory.value) query.set('category', logsCategory.value)
    logsData.value = await apiGet(`/api/v1/logs?${query}`)
  } catch (error) { toast((error as Error).message, 'error') }
  logsLoading.value = false
}
function openLogs() {
  logsOpen.value = true
  loadLogs()
}
function formatLogTime(value: string) {
  try { return new Date(value).toLocaleString('zh-CN', { hour12: false }) } catch { return value }
}

watch(() => filters.q, () => {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => loadPlans(true), 350)
})
watch(settings, () => { syncRecipientText(); syncRuntimeText() })

onMounted(async () => {
  document.addEventListener('paste', handlePaste)
  try {
    bootstrap.value = await apiGet('/api/v1/bootstrap')
    setCsrfToken(bootstrap.value.csrf_token)
    await Promise.all([loadPlans(), loadSettings()])
    editModel()
  } catch (error) { toast((error as Error).message, 'error') }
})
onBeforeUnmount(() => {
  document.removeEventListener('paste', handlePaste)
})
</script>

<template>
  <header class="topbar">
    <div class="brand"><h1>个人看板</h1><span>竞彩足球计划管理</span></div>
    <div class="top-actions">
      <span class="online"><i />服务正常</span>
      <button class="ghost" @click="openCalendar">购彩日历</button>
      <button class="ghost" @click="openLogs">运行日志</button>
      <button class="ghost" @click="settingsOpen = true">设置</button>
      <button v-if="bootstrap?.public_mode" class="ghost" @click="logout">退出登录</button>
    </div>
  </header>

  <main class="page-shell">
    <section class="summary surface">
      <div class="metric"><span>筛选计划</span><strong>{{ summary.plans_total || 0 }}</strong><small>待结算 {{ summary.plans_pending || 0 }}</small></div>
      <div class="metric"><span>投入金额</span><strong>{{ money(summary.stake) }}</strong><small>已购买 {{ summary.plans_purchased || 0 }} 张</small></div>
      <div class="metric"><span>实际返还</span><strong>{{ money(summary.return) }}</strong><small>中奖 {{ summary.plans_won || 0 }} 张</small></div>
      <div class="metric"><span>净盈亏</span><strong :class="Number(summary.profit) >= 0 ? 'profit' : 'loss'">{{ money(summary.profit) }}</strong><small>未中 {{ summary.plans_lost || 0 }} 张</small></div>
      <div class="summary-action"><span>汇总随当前筛选实时变化</span><button :disabled="busy === 'recommend'" @click="recommend">{{ busy === 'recommend' ? '提交中…' : '生成今日推荐' }}</button></div>
    </section>

    <section class="toolbar surface">
      <div class="segmented">
        <button v-for="item in [{v:'',l:'全部'},{v:'crs',l:'比分'},{v:'had',l:'胜平负'},{v:'ttg',l:'进球数'}]" :key="item.v" :class="{ active: filters.market === item.v }" @click="filters.market = item.v; loadPlans(true)">{{ item.l }}</button>
      </div>
      <select v-model="filters.status" @change="loadPlans(true)"><option value="">全部状态</option><option v-for="(label,key) in statusLabels" :key="key" :value="key">{{ label }}</option></select>
      <select v-model="filters.purchased" @change="loadPlans(true)"><option value="">购买状态</option><option value="true">已购买</option><option value="false">未购买</option></select>
      <input v-model="filters.date" type="date" @change="loadPlans(true)" />
      <label class="search"><span>⌕</span><input v-model="filters.q" placeholder="搜索计划编号、球队或联赛" /></label>
      <button class="ghost" @click="loadPlans(true)">查询</button>
      <button v-if="Object.values(filters).some(Boolean)" class="link" @click="Object.assign(filters,{market:'',status:'',purchased:'',date:'',q:''}); loadPlans(true)">清空筛选</button>
      <span class="result-count">共 {{ result.pagination.total || 0 }} 张</span>
    </section>

    <section class="content-frame surface">
      <div v-if="loading" class="state-box">正在读取计划…</div>
      <div v-else-if="!result.items.length" class="state-box"><strong>没有符合条件的计划</strong><span>可以调整筛选条件或生成今日推荐。</span></div>
      <div v-else class="plan-scroll">
        <article v-for="plan in result.items" :key="plan.plan_id" class="plan-card">
          <header class="plan-head">
            <div><div class="plan-name"><span class="market-tag" :class="plan.market">{{ plan.market_label }}</span>{{ plan.pass_size }}串1 · {{ plan.recommendation_date }}</div><div class="plan-id" :title="plan.plan_id">{{ shortId(plan.plan_id) }} · {{ formatTime(plan.created_at) }}</div></div>
            <div class="badges"><span :class="['status', plan.status]">{{ statusLabels[plan.status] }}</span><span v-if="plan.purchased" class="status purchased">已购买</span></div>
          </header>
          <div class="money-row"><span>投注 <b>{{ money(plan.stake) }}</b></span><span>联合赔率 <b>{{ plan.combined_odds }}</b></span><span>理论奖金 <b>{{ money(plan.net_prize) }}</b></span><span v-if="plan.net_profit !== ''">净盈亏 <b :class="Number(plan.net_profit) >= 0 ? 'profit' : 'loss'">{{ money(plan.net_profit) }}</b></span></div>
          <div class="legs-wrap">
            <table><thead><tr><th>场次 / 联赛</th><th>比赛</th><th>推荐</th><th>最初推荐</th><th>AI分析</th><th>SP</th><th>赛果</th><th>{{ plan.status === 'pending' && !plan.purchased ? '调整' : '结算' }}</th></tr></thead>
              <tbody><tr v-for="leg in plan.legs" :key="leg.match_id">
                <td><b>{{ leg.match_num }}</b><small>{{ leg.league }} · {{ formatTime(leg.start_at) }}</small></td>
                <td>{{ leg.home }} <em>vs</em> {{ leg.away }}</td>
                <td><span class="pick">{{ pickWithBall(leg.pick_label, plan.market) }}</span></td>
                <td><span v-if="leg.original_pick_label" class="pick original">{{ pickWithBall(leg.original_pick_label, plan.market) }}</span><span v-else class="muted">-</span></td>
                <td><span v-if="leg.ai_suggestion" class="ai-suggestion">{{ pickWithBall(leg.ai_suggestion.label, plan.market) }}</span><span v-else class="muted">-</span></td>
                <td>{{ leg.odds }}</td>
                <td class="result-cell"><span :class="['result-label', leg.result.status]">{{ formatResult(plan.market, leg.result) }}</span></td>
                <td class="settle-cell">
                  <b :class="['verdict', leg.result.hit === true ? 'hit' : leg.result.hit === false ? 'miss' : leg.result.status]">{{ leg.result.verdict }}</b>
                  <div class="settle-actions">
                    <button v-if="leg.ai_suggestion && leg.ai_suggestion.code !== leg.pick_code && leg.result.status === 'pending' && !plan.purchased" class="soft" @click="applyAiSuggestion(plan, leg)">替换为AI推荐</button>
                    <template v-if="leg.result.status === 'pending' && !plan.purchased">
                      <select :value="leg.pick_code" @change="action('/api/v1/actions/update-leg',{plan_id:plan.plan_id,match_id:leg.match_id,option_code:($event.target as HTMLSelectElement).value},`update-${leg.match_id}`)"><option v-for="option in leg.options" :key="option.code" :value="option.code">{{ option.label }} · {{ option.odds }}</option></select>
                      <button class="text-danger" @click="deleteLeg(plan,leg)">删除</button>
                    </template>
                  </div>
                </td>
              </tr></tbody>
            </table>
          </div>
          <footer class="plan-actions">
            <div class="action-bar">
              <button class="soft" :disabled="busy === `settle-${plan.plan_id}` || !['pending','void'].includes(plan.status)" @click="action('/api/v1/actions/settle-plan',{plan_id:plan.plan_id},`settle-${plan.plan_id}`)">{{ plan.status === 'void' ? '重新获取赛果' : '更新本计划赛果' }}</button>
              <button class="soft" @click="action('/api/v1/actions/analyze-plan',{plan_id:plan.plan_id},`ai-${plan.plan_id}`)">AI分析</button>
              <button class="soft" @click="action('/api/v1/actions/mark-purchased',{plan_id:plan.plan_id,purchased:!plan.purchased},`buy-${plan.plan_id}`)">{{ plan.purchased ? '取消购买' : '标记购买' }}</button>
              <button class="soft" :class="{ 'paste-waiting': pendingUploadPlan === plan }" :disabled="busy === `ticket-${plan.plan_id}`" @click="pickTicket(plan)">{{ pendingUploadPlan === plan ? '粘贴截图（Ctrl+V）…' : '上传实票' }}</button>
              <button class="soft" :disabled="busy === `push-${plan.plan_id}`" @click="action('/api/v1/actions/push-mail',{plan_id:plan.plan_id},`push-${plan.plan_id}`)">推送邮件</button>
            </div>
            <button class="text-danger" @click="deletePlan(plan)">删除计划</button>
            <div v-if="plan.ticket_image_url" class="ticket-box">
              <img class="ticket-thumb" :src="plan.ticket_image_url" :alt="`计划 ${plan.plan_id} 的实票图片`" loading="lazy" @click="ticketZoomUrl = plan.ticket_image_url" />
              <div class="ticket-meta">
                <span class="ticket-label">实票凭证</span>
                <div class="ticket-ops">
                  <button class="link" @click="ticketZoomUrl = plan.ticket_image_url">放大查看</button>
                  <button class="text-danger" @click="deleteTicket(plan)">移除实票</button>
                </div>
              </div>
            </div>
          </footer>
          <details v-if="plan.ai_summary" class="ai-summary"><summary>查看 AI 总体分析</summary><p>{{ plan.ai_summary }}</p></details>
        </article>
      </div>
      <nav class="pagination">
        <span class="pagination-info">{{ paginationStart }} - {{ paginationEnd }} 共 {{ result.pagination.total || 0 }} 张</span>
        <button class="page-prev" :disabled="page <= 1" @click="page--; loadPlans()">上一页</button>
        <template v-for="(item, index) in visiblePages" :key="`${item}-${index}`">
          <button v-if="typeof item === 'number'" class="page-number" :class="{ active: item === page }" @click="goToPage(item)">{{ item }}</button>
          <span v-else class="page-ellipsis">{{ item }}</span>
        </template>
        <button class="page-next" :disabled="page >= result.pagination.pages" @click="page++; loadPlans()">下一页</button>
        <select v-model="perPage" class="per-page" @change="loadPlans(true)">
          <option v-for="size in perPageOptions" :key="size" :value="size">{{ size }}条/页</option>
        </select>
        <label class="page-jump">跳至 <input v-model.number="jumpPage" type="number" min="1" @keyup.enter="jumpToPage" /> 页</label>
      </nav>
    </section>
  </main>

  <div v-if="calendarOpen" class="modal-layer" @click.self="calendarOpen = false">
    <section class="calendar-modal surface">
      <header><button @click="moveCalendar(-1)">← 上个月</button><h2>{{ calendarTitle }}</h2><button @click="moveCalendar(1)">下个月 →</button><button class="modal-close" @click="calendarOpen = false">×</button></header>
      <div class="week"><span v-for="day in ['一','二','三','四','五','六','日']" :key="day">{{ day }}</span></div>
      <div class="calendar-grid"><button v-for="(cell,index) in calendarCells" :key="index" :disabled="!cell" :class="{selected:cell?.date===filters.date,active:cell && calendarStats[cell.date]}" @click="cell && selectCalendarDate(cell.date)"><template v-if="cell"><b>{{ cell.day }}</b><span v-if="calendarStats[cell.date]">{{ calendarStats[cell.date].total }}张计划</span><small v-if="calendarStats[cell.date]?.pending" class="pending">待结算{{ calendarStats[cell.date].pending }}</small><small v-else-if="calendarStats[cell.date]?.won" class="profit">中奖{{ calendarStats[cell.date].won }}</small><small v-else-if="calendarStats[cell.date]?.lost" class="loss">未中奖{{ calendarStats[cell.date].lost }}</small><small v-else-if="calendarStats[cell.date]?.void" class="void">已取消{{ calendarStats[cell.date].void }}</small><small v-else>—</small></template></button></div>
    </section>
  </div>

  <div v-if="settingsOpen" class="modal-layer" @click.self="settingsOpen = false">
    <section class="settings-modal surface">
      <header><div><h2>设置</h2><p>现有配置已自动初始化到新设置中，不会覆盖历史计划。</p></div><button class="modal-close" @click="settingsOpen = false">×</button></header>
      <div v-if="settings" class="settings-body">
        <nav class="settings-nav"><button v-for="tab in [{v:'recommendations',l:'推荐计划'},{v:'recipients',l:'推送邮箱'},{v:'models',l:'大模型'},{v:'runtime',l:'时间与运行'}]" :key="tab.v" :class="{active:settingsTab===tab.v}" @click="settingsTab=tab.v">{{ tab.l }}</button></nav>
        <div class="settings-content">
          <section v-if="settingsTab === 'recommendations'">
            <h3>计划生成规则</h3><p class="hint">优先使用最高串关数；比赛不足逐级降低。连最低串关数也不满足时不生成。</p>
            <div class="profile-grid"><article v-for="(profile,key) in settings.profiles" :key="key"><label class="switch-row"><input v-model="profile.enabled" type="checkbox" /><b>{{ profileLabels[key as string] }}</b></label><label>最低串关数<input v-model.number="profile.min_pass_size" type="number" min="2" max="8" /></label><label>最高串关数<input v-model.number="profile.max_pass_size" type="number" min="2" max="8" /></label><label>生成计划数<input v-model.number="profile.plan_count" type="number" min="1" max="20" /></label></article></div>
            <div class="form-actions"><button @click="saveProfiles">保存推荐规则</button></div>
          </section>
          <section v-else-if="settingsTab === 'recipients'">
            <h3>推送邮箱</h3><p class="hint">每行填写一个邮箱，最多 20 个。</p><textarea v-model="recipientText" rows="8" placeholder="name@example.com" /><div class="form-actions"><button @click="saveRecipients">保存收件人</button></div>
            <hr/><h3>SMTP 发件设置</h3><div class="form-grid"><label>服务器<input v-model="settings.mail.smtp_host" /></label><label>端口<input v-model.number="settings.mail.smtp_port" type="number" /></label><label>用户名<input v-model="settings.mail.smtp_username" /></label><label>发件邮箱<input v-model="settings.mail.mail_from" type="email" /></label><label class="wide">新授权码<input v-model="authCode" type="password" :placeholder="settings.mail.smtp_auth_configured ? '已配置；留空表示不修改' : '请输入授权码'" /></label><label class="switch-row wide"><input v-model="settings.mail.mail_dry_run" type="checkbox" />只生成邮件预览，不实际发送</label></div><div class="form-actions"><button @click="saveMail">保存邮件设置</button></div>
          </section>
          <section v-else-if="settingsTab === 'models'">
            <h3>大模型</h3>
            <p class="hint">可添加多个模型，但同一时间只启用一个。点击“测试并启用”会真实调用 API 并核验联网搜索能力，失败不会替换当前模型。已通过测试的模型可随时“切换为当前”。</p>
            <div class="model-list">
              <article v-for="model in settings.ai.models" :key="model.id" :class="{ active: settings.ai.active_model_config_id === model.id }">
                <div>
                  <b>{{ model.display_name }}</b>
                  <span>{{ model.provider }} · {{ model.model_name }}</span>
                  <small :class="model.last_test_status">{{ model.last_test_detail || '尚未测试' }}</small>
                  <span v-if="settings.ai.active_model_config_id === model.id" class="active-badge">当前正在使用</span>
                </div>
                <div>
                  <button class="soft" @click="editModel(model)">编辑</button>
                  <button class="soft" :disabled="busy === `model-test-${model.id}`" @click="testModel(model.id)">
                    {{ settings.ai.active_model_config_id === model.id ? '重新测试' : '测试并启用' }}
                  </button>
                  <button
                    v-if="settings.ai.active_model_config_id !== model.id && model.last_test_status === 'passed'"
                    class="primary"
                    :disabled="busy === `model-activate-${model.id}`"
                    @click="activateModel(model.id)"
                  >切换为当前</button>
                  <button class="text-danger" :disabled="settings.ai.active_model_config_id === model.id" @click="removeModel(model.id)">删除</button>
                </div>
              </article>
            </div>
            <div class="model-form">
              <h4>{{ modelForm.id ? '编辑模型配置' : '添加模型配置' }}</h4>
              <div class="form-grid">
                <label>模型厂商<select v-model="modelForm.provider" @change="applyProviderDefaults"><option v-for="provider in settings.providers" :key="provider.code" :value="provider.code">{{ provider.name }}</option></select></label>
                <label>显示名称<input v-model="modelForm.display_name" /></label>
                <label class="wide">API 地址<input v-model="modelForm.base_url" /></label>
                <label>调用模型<input v-model="modelForm.model_name" /></label>
                <label>API Key<input v-model="modelForm.api_key" type="password" :placeholder="modelForm.id ? '留空表示不修改' : '输入 API Key'" /></label>
              </div>
              <p v-if="selectedProvider() && !selectedProvider().native_web_search" class="warning">该厂商当前标准接口不支持系统强制联网搜索，可保存配置，但“测试并启用”会明确失败。</p>
              <div class="form-actions">
                <button class="soft" @click="editModel()">取消编辑</button>
                <button @click="saveModel">{{ modelForm.id ? '保存修改' : '新增模型' }}</button>
              </div>
            </div>
          </section>
          <section v-else>
            <h3>时间与运行</h3><p class="hint">时间均为北京时间。最迟推送时间之后不会再发送当日推荐邮件。</p><div class="form-grid"><label class="wide">推荐生成时间（逗号分隔）<input v-model="recommendationTimesText" placeholder="14:00, 14:30" /></label><label>首封邮件时间<input v-model="settings.runtime.recommendation_first_mail_time" type="time" /></label><label>最迟生成时间<input v-model="settings.runtime.recommendation_latest_start" type="time" /></label><label>最迟推送时间<input v-model="settings.runtime.recommendation_deadline" type="time" /></label><label>推送安全缓冲（分钟）<input v-model.number="settings.runtime.recommendation_send_buffer_minutes" type="number" /></label><label>轮询间隔（秒）<input v-model.number="settings.runtime.poll_interval_seconds" type="number" min="60" /></label><label>赛果检查延迟（分钟）<input v-model.number="settings.runtime.result_check_delay_minutes" type="number" min="90" /></label><label class="switch-row wide"><input v-model="settings.runtime.send_no_recommendation" type="checkbox" />没有推荐时也发送通知</label></div><div class="form-actions"><button @click="saveRuntime">保存运行设置</button></div>
          </section>
        </div>
      </div>
    </section>
  </div>

  <div v-if="logsOpen" class="modal-layer" @click.self="logsOpen = false">
    <section class="logs-modal surface">
      <header><h2>运行日志</h2><div class="logs-filters"><select v-model="logsCategory" @change="loadLogs"><option value="">全部类别</option><option v-for="(label,key) in categoryLabels" :key="key" :value="key">{{ label }}</option></select><button class="soft" @click="loadLogs">刷新</button></div><button class="modal-close" @click="logsOpen = false">×</button></header>
      <div class="logs-body">
        <div v-if="logsLoading" class="logs-empty">加载中…</div>
        <div v-else-if="!logsData.items?.length" class="logs-empty">暂无日志记录</div>
        <div v-else class="logs-list">
          <div v-for="item in logsData.items" :key="item.id" class="log-entry" :class="item.category">
            <span class="log-time">{{ formatLogTime(item.created_at) }}</span>
            <span class="log-cat">{{ categoryLabels[item.category] || item.category }}</span>
            <span class="log-msg">{{ item.message }}</span>
            <small v-if="item.detail">{{ item.detail }}</small>
          </div>
        </div>
      </div>
    </section>
  </div>

  <div v-if="ticketZoomUrl" class="modal-layer zoom-layer" @click.self="ticketZoomUrl = ''">
    <img class="zoom-img" :src="ticketZoomUrl" alt="实票大图" />
    <button class="modal-close zoom-close" @click="ticketZoomUrl = ''">×</button>
  </div>

  <input ref="ticketFileInput" type="file" accept="image/jpeg,image/png,image/gif,image/webp" hidden @change="uploadTicket" />
  <div class="toasts"><div v-for="item in toasts" :key="item.id" :class="['toast',item.level]">{{ item.text }}</div></div>
</template>
