const form = document.querySelector('#check-form');
const input = document.querySelector('#symbol');
const loading = document.querySelector('#loading');
const dashboard = document.querySelector('#dashboard');
const empty = document.querySelector('#empty');
const agentForm = document.querySelector('#agent-form');
const agentInput = document.querySelector('#agent-input');
const agentMessages = document.querySelector('#agent-messages');
const agentStatus = document.querySelector('#agent-status');
const cronForm = document.querySelector('#cron-form');
const cronJobs = document.querySelector('#cron-jobs');
let chart;
let minuteChart;
let activeSymbol = '';
let decisionSymbol = '';

cronForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const payload = { id: crypto.randomUUID().replaceAll('-', '').slice(0, 12), name: document.querySelector('#cron-name').value, task: document.querySelector('#cron-task').value, schedule_kind: document.querySelector('#cron-kind').value, schedule_value: document.querySelector('#cron-value').value };
  const response = await fetch('/api/cron/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  if (response.ok) { cronForm.reset(); await loadCronJobs(); }
});
loadCronJobs();

async function loadCronJobs() {
  const response = await fetch('/api/cron/jobs');
  const jobs = await response.json();
  cronJobs.innerHTML = jobs.length ? jobs.map(job => `<div class="cron-job"><strong>${job.name}</strong><span>${job.task} · ${job.schedule_kind}:${job.schedule_value}</span><small>${job.enabled ? `下次：${job.next_run_at || '--'}` : '已停用'}</small></div>`).join('') : '<p class="muted">尚未创建定时任务</p>';
}

document.querySelector('#confirm-decision').addEventListener('click', () => saveDecision('confirm'));
document.querySelector('#reject-decision').addEventListener('click', () => saveDecision('reject'));

agentForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const message = agentInput.value.trim();
  if (!message) return;
  addAgentMessage(message, 'user');
  agentInput.value = '';
  const answer = addAgentMessage('', 'assistant');
  agentStatus.textContent = '正在连接';
  try {
    const response = await fetch('/api/agent/chat/stream', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message }) });
    if (!response.ok) throw new Error('Agent 请求失败');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split('\n\n');
      buffer = events.pop();
      events.forEach(raw => {
        const line = raw.split('\n').find(item => item.startsWith('data:'));
        if (!line) return;
        const payload = JSON.parse(line.slice(5));
        if (payload.type === 'status') agentStatus.textContent = payload.status;
        if (payload.type === 'delta') { answer.dataset.raw += payload.content; answer.innerHTML = renderMarkdown(answer.dataset.raw); agentMessages.scrollTop = agentMessages.scrollHeight; }
        if (payload.type === 'done') { if (payload.result) render(payload.result); if (payload.results) payload.results.forEach(render); }
        if (payload.type === 'error') throw new Error(payload.message);
      });
    }
    agentStatus.textContent = '就绪';
  } catch (error) {
    answer.className = 'agent-message error';
    answer.textContent = error.message;
    agentStatus.textContent = '请求失败';
  }
});

function addAgentMessage(message, role) {
  const item = document.createElement('div');
  item.className = `agent-message ${role}`;
  item.dataset.raw = message;
  item.innerHTML = role === 'assistant' ? renderMarkdown(message) : escapeHtml(message);
  agentMessages.appendChild(item);
  agentMessages.scrollTop = agentMessages.scrollHeight;
  return item;
}

function renderMarkdown(markdown) {
  if (!markdown) return '<span class="stream-cursor">▌</span>';
  if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') return escapeHtml(markdown).replaceAll('\n', '<br>');
  const html = marked.parse(markdown, { breaks: true, gfm: true });
  return DOMPurify.sanitize(html);
}

function escapeHtml(value) {
  const element = document.createElement('div');
  element.textContent = value;
  return element.innerHTML;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const symbol = input.value.trim();
  if (!/^\d{6}$/.test(symbol)) {
    input.setCustomValidity('请输入 6 位数字股票代码');
    input.reportValidity();
    return;
  }
  input.setCustomValidity('');
  loading.classList.remove('hidden');
  dashboard.classList.add('hidden');
  empty.classList.add('hidden');
  activeSymbol = symbol;
  try {
    const response = await fetch('/api/risk/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ symbol }) });
    if (!response.ok) throw new Error((await response.json()).detail || '检查失败');
    render(await response.json());
  } catch (error) {
    renderRiskUnavailable(symbol, error.message);
  } finally {
    loading.classList.add('hidden');
  }
});

function renderRiskUnavailable(symbol, message) {
  dashboard.classList.remove('hidden');
  empty.classList.add('hidden');
  document.querySelector('#stock-symbol').textContent = symbol;
  document.querySelector('#stock-name').textContent = '风险数据暂不可用';
  document.querySelector('#price').textContent = '--';
  document.querySelector('#change').textContent = '--';
  document.querySelector('#change').className = '';
  const verdict = document.querySelector('#verdict');
  verdict.textContent = 'ERROR';
  verdict.className = 'verdict danger';
  document.querySelector('#action').textContent = message;
  document.querySelector('#score').textContent = '等待数据';
  document.querySelector('#rsi').textContent = '--';
  document.querySelector('#atr').textContent = '--';
  document.querySelector('#macd').textContent = '--';
  document.querySelector('#bollinger').textContent = '--';
  document.querySelector('#micro-factor-list').innerHTML = '<span><b>微观因子</b><em>数据暂不可用</em></span>';
  document.querySelector('#data-time').textContent = '--';
  document.querySelector('#source').textContent = 'tickdb';
  document.querySelector('#reasons').innerHTML = '<li>80 天历史数据未加载，暂不生成风控结论</li>';
  document.querySelector('#chart').innerHTML = '<div class="chart-error">80 天价格走势暂不可用</div>';
}

function render(result) {
  dashboard.classList.remove('hidden');
  document.querySelector('#stock-symbol').textContent = result.symbol;
  document.querySelector('#stock-name').textContent = result.name;
  document.querySelector('#price').textContent = `¥${result.quote.price.toFixed(2)}`;
  const change = document.querySelector('#change');
  change.textContent = `${result.quote.change_percent >= 0 ? '+' : ''}${result.quote.change_percent.toFixed(2)}%`;
  change.className = result.quote.change_percent >= 0 ? 'up' : 'down';
  const verdict = document.querySelector('#verdict');
  verdict.textContent = result.verdict;
  verdict.className = `verdict ${result.verdict.toLowerCase()}`;
  document.querySelector('#action').textContent = result.action;
  document.querySelector('#score').textContent = `风险分数 ${result.score}`;
  document.querySelector('#rsi').textContent = result.indicators.rsi ?? '--';
  document.querySelector('#atr').textContent = result.indicators.atr ?? '--';
  document.querySelector('#macd').textContent = result.indicators.macd ?? '--';
  document.querySelector('#bollinger').textContent = result.indicators.bollinger_upper ?? '--';
  renderMicroFactors(result.microstructure_factors);
  document.querySelector('#data-time').textContent = result.data_timestamp;
  document.querySelector('#source').textContent = result.quote.source;
  document.querySelector('#reasons').innerHTML = result.reasons.map(reason => `<li>${reason}</li>`).join('');
  renderChart(result.bars);
  renderMinuteChart(result.minute_bars);
  decisionSymbol = result.symbol;
}

function renderMicroFactors(factors) {
  const list = document.querySelector('#micro-factor-list');
  list.innerHTML = Object.entries(factors || {}).map(([name, value]) => `<span><b>${name.replaceAll('_', ' ')}</b><em>${value == null ? '数据不足' : Number(value).toFixed(4)}</em></span>`).join('');
}

async function saveDecision(decision) {
  if (!decisionSymbol) return;
  const response = await fetch('/api/decisions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ symbol: decisionSymbol, decision }) });
  if (response.ok) document.querySelector('#action').textContent = decision === 'confirm' ? '已记录确认决策' : '已记录暂不操作';
}

function renderChart(bars) {
  if (!chart) chart = echarts.init(document.querySelector('#chart'));
  chart.setOption({ animationDuration: 500, grid: { left: 12, right: 12, top: 16, bottom: 24 }, xAxis: { type: 'category', data: bars.map(bar => bar.date), boundaryGap: false, axisLabel: { color: '#82909a', fontSize: 10, interval: 9 }, axisLine: { lineStyle: { color: '#28353b' } } }, yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#1d292f' } }, axisLabel: { color: '#82909a' } }, series: [{ data: bars.map(bar => bar.close), type: 'line', smooth: true, showSymbol: false, lineStyle: { color: '#f3b562', width: 2 }, areaStyle: { color: 'rgba(243,181,98,0.08)' } }] });
}

function renderMinuteChart(bars) {
  if (!minuteChart) minuteChart = echarts.init(document.querySelector('#minute-chart'));
  minuteChart.setOption({ animationDuration: 400, grid: { left: 12, right: 12, top: 12, bottom: 24 }, tooltip: { trigger: 'axis' }, xAxis: { type: 'category', data: bars.map(bar => bar.date.slice(11) || bar.date), axisLabel: { color: '#82909a', fontSize: 10, interval: Math.max(1, Math.floor(bars.length / 8)) }, axisLine: { lineStyle: { color: '#28353b' } } }, yAxis: [{ type: 'value', scale: true, splitLine: { lineStyle: { color: '#1d292f' } }, axisLabel: { color: '#82909a' } }, { type: 'value', show: false }], series: [{ name: '价格', type: 'candlestick', data: bars.map(bar => [bar.open, bar.close, bar.low, bar.high]), itemStyle: { color: '#69c5a1', color0: '#ef7664', borderColor: '#69c5a1', borderColor0: '#ef7664' } }, { name: '成交量', type: 'bar', yAxisIndex: 1, data: bars.map(bar => bar.volume), itemStyle: { color: 'rgba(243,181,98,0.25)' }, barMaxWidth: 5 }] });
}

window.addEventListener('resize', () => { chart?.resize(); minuteChart?.resize(); });
