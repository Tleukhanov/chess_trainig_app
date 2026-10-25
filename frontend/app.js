// Chess Trainer Frontend — SPA на vanilla JS, потребляет /api/* эндпоинты.

import { boardHtml, bindBoard, lastMoveSquares } from './board.js?v=0.7.1';

const state = {
  user: null,
  currentView: 'dashboard',
  timers: { coach: null, task: null, llm: null, tool: null, opening: null, middlegame: null, quote: null, theory: null },
  gamesFilter: 'all',
  gameId: null,
  gameData: null,
  gamePly: 0,
  gameExtras: null,
  gameReview: null,
  gameRepeats: null,
  gameReviewRefresh: false,
  keyHandler: null,
  tool: 'repertoire',
  task: null,
  taskKey: null,
  taskResult: null,
  taskAnswerText: '',
  taskRevealed: false,
  taskLine: null,
  taskPlayTimer: null,
  // Свободный разбор решения: своя позиция, ходы и оценка Stockfish.
  taskExplore: false,
  taskExploreFen: null,
  taskExploreMoves: [],
  taskExploreEval: null,
  taskExploreResult: null,
  taskExploreLast: [],
  taskExploreBusy: false,
  taskExploreError: null,
  taskExploreToken: 0,
  taskExplanation: null,
  taskExplaining: false,
  taskJobId: null,
  taskError: null,
  planLlmGenerating: false,
  planLlmJobId: null,
  planLlmError: null,
  openingSession: null,
  openingStyles: null,
  openingStyle: null,
  openingColor: 'white',
  openingFens: null,
  openingThinking: false,
  openingAnalyzing: false,
  openingJobId: null,
  openingError: null,
  openingLastReply: null,
  // Панель «Теория» в тренере дебютов: линия Lichess/ECO и идеи LLM.
  openingTheory: null,
  openingTheoryRequestFen: null,
  openingTheoryLoading: false,
  openingTheoryError: null,
  openingTheoryLine: null,
  openingTheorySetup: false,
  openingTheoryIdeas: null,
  openingTheoryIdeasLoading: false,
  openingTheoryIdeasDone: false,
  openingTheoryIdeasCached: false,
  openingTheoryIdeasError: null,
  middlegameSession: null,
  middlegameMeta: null,
  middlegameStrength: 'equal',
  middlegamePlanDraft: '',
  middlegameThinking: false,
  middlegameAnalyzing: false,
  middlegameJobId: null,
  middlegameError: null,
  middlegameOptimistic: null,
  middlegameHuman: true,
  openingHuman: true,
  authMode: 'login',
  trackStarted: false,
};

// --- API helpers ---

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    const error = new Error(err.detail || `HTTP ${resp.status}`);
    error.status = resp.status;
    throw error;
  }
  return resp.json();
}

async function apiPost(path, formData) {
  const resp = await fetch(path, { method: 'POST', body: formData });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    const error = new Error(err.detail || `HTTP ${resp.status}`);
    error.status = resp.status;
    throw error;
  }
  return resp.json();
}

function showToast(msg, kind) {
  const toast = document.createElement('div');
  toast.className = kind === 'error' ? 'alert alert-error' : 'alert alert-info';
  toast.textContent = msg;
  Object.assign(toast.style, {
    position: 'fixed', bottom: '16px', right: '16px', zIndex: '9999',
    maxWidth: '360px', boxShadow: '0 4px 12px rgba(0,0,0,0.25)',
  });
  document.body.appendChild(toast);
  setTimeout(() => { if (toast.parentNode) toast.parentNode.removeChild(toast); }, kind === 'error' ? 4000 : 2500);
}

// --- Тема (светлая/тёмная) ---

function readStoredTheme() {
  try { return localStorage.getItem('theme'); } catch { return null; }
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const btn = document.querySelector('[data-theme-toggle]');
  if (btn) {
    const dark = theme === 'dark';
    btn.textContent = dark ? '☀' : '☾';
    btn.title = dark ? 'Светлая тема' : 'Тёмная тема';
  }
}

function setupTheme() {
  applyTheme(readStoredTheme() === 'dark' ? 'dark' : 'light');
  const btn = document.querySelector('[data-theme-toggle]');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    try { localStorage.setItem('theme', next); } catch { /* приватный режим */ }
  });
}

// --- Navigation ---

const views = ['dashboard', 'overview', 'plan', 'progress', 'games', 'drills', 'task', 'opening', 'middlegame', 'coach', 'tools', 'users'];

function clearAllTimers() {
  if (state.taskPlayTimer) { clearInterval(state.taskPlayTimer); state.taskPlayTimer = null; }
  Object.keys(state.timers).forEach(key => {
    if (state.timers[key]) {
      clearInterval(state.timers[key]);
      state.timers[key] = null;
    }
  });
}

function navigate(view) {
  if (!views.includes(view)) view = 'dashboard';
  state.currentView = view;
  if (view === 'games') {
    state.gameId = null;
    state.gameData = null;
  }
  document.querySelectorAll('#nav a').forEach(a => {
    a.classList.toggle('active', a.dataset.view === view);
  });
  document.querySelectorAll('#nav .nav-group').forEach(g => {
    const groupViews = Array.from(g.querySelectorAll('a')).map(a => a.dataset.view);
    g.classList.toggle('active', groupViews.includes(view));
  });
  render(view);
}

function setupNav() {
  document.querySelectorAll('#nav a').forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      navigate(a.dataset.view);
    });
  });
  document.querySelector('.brand').addEventListener('click', e => {
    e.preventDefault();
    navigate('dashboard');
  });
}

// --- Auth gate ---

async function authStatus() {
  try {
    return await api('/api/auth/status');
  } catch {
    return { logged_in: false, nick: null };
  }
}

function hideNav() {
  const nav = document.getElementById('nav');
  if (nav) nav.style.display = 'none';
}

function showNav() {
  const nav = document.getElementById('nav');
  if (nav) nav.style.display = '';
}

function viewAuth(app) {
  const mode = state.authMode === 'register' ? 'register' : 'login';
  const loginForm = `
    <form id="auth-login-form">
      <div class="form-group"><label>Логин <input type="text" name="login" required autocomplete="username"></label></div>
      <div class="form-group"><label>Пароль <input type="password" name="password" required autocomplete="current-password"></label></div>
      <button class="btn btn-primary" style="width:100%">Войти</button>
    </form>`;
  const regForm = `
    <form id="auth-register-form">
      <div class="form-group"><label>Логин <input type="text" name="login" required autocomplete="username"></label></div>
      <div class="form-group"><label>Пароль (мин. 4 символа) <input type="password" name="password" required autocomplete="new-password"></label></div>
      <div class="form-group"><label>Ник на Lichess (обяз.) <input type="text" name="lichess" required placeholder="GarryKasparov" autocomplete="username"></label></div>
      <div class="form-group"><label>FIDE ID (необяз.) <input type="text" name="fide" placeholder="4130005"></label></div>
      <button class="btn btn-primary" style="width:100%">Зарегистрироваться</button>
    </form>
    <p style="color:var(--c-text-muted);font-size:12px;margin-top:8px">Если твой ник уже есть в базе без пароля (старые данные и партии), регистрация закрепит его за тобой — партии сохранятся.</p>`;
  app.innerHTML = `
    <div class="card" style="max-width:420px;margin:40px auto">
      <div class="card-header"><span class="card-title">Вход / Регистрация</span></div>
      <div class="games-filter" style="margin-bottom:16px">
        <button class="btn btn-secondary ${mode === 'login' ? 'btn-active' : ''}" data-authmode="login">Вход</button>
        <button class="btn btn-secondary ${mode === 'register' ? 'btn-active' : ''}" data-authmode="register">Регистрация</button>
      </div>
      <div class="alert alert-error" id="auth-err" style="display:none"></div>
      ${mode === 'login' ? loginForm : regForm}
    </div>`;
  app.querySelectorAll('[data-authmode]').forEach(btn => {
    btn.addEventListener('click', () => {
      state.authMode = btn.dataset.authmode;
      viewAuth(app);
    });
  });
  const fail = msg => {
    const box = document.getElementById('auth-err');
    if (box) {
      box.textContent = msg;
      box.style.display = 'block';
    }
  };
  const loginF = document.getElementById('auth-login-form');
  if (loginF) loginF.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;
    try {
      await apiPost('/api/auth/login', new FormData(loginF));
      showNav();
      await loadUser();
      navigate('dashboard');
      startGlobalJobPolling();
    } catch (err) {
      fail(err.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  });
  const regF = document.getElementById('auth-register-form');
  if (regF) regF.addEventListener('submit', async e => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;
    try {
      await apiPost('/api/auth/register', new FormData(regF));
      showNav();
      await loadUser();
      navigate('dashboard');
      startGlobalJobPolling();
    } catch (err) {
      fail(err.message);
    } finally {
      if (btn) btn.disabled = false;
    }
  });
}

// --- User box ---

function renderUserbox() {
  const box = document.getElementById('userbox');
  if (!box) return;
  if (state.user) {
    box.innerHTML = `<span class="user-nick">${esc(state.user.nick)}</span><a href="#" id="logout-link">выйти</a>`;
    document.getElementById('logout-link').addEventListener('click', async e => {
      e.preventDefault();
      try {
        await apiPost('/api/auth/logout', new FormData());
      } catch { /* всё равно выходим */ }
      state.user = null;
      state.authMode = 'login';
      stopGlobalJobPolling();
      hideGlobalJobIndicator();
      Object.assign(state, {
        gameId: null, gameData: null, task: null, taskKey: null, taskResult: null,
        taskRevealed: false, taskExplanation: null, planLlmJobId: null,
        planLlmGenerating: false, tool: 'repertoire', gamesFilter: 'all',
        openingSession: null, openingFens: null, openingLastReply: null, openingJobId: null,
        middlegameSession: null, middlegameMeta: null, middlegameJobId: null,
        middlegameThinking: false, middlegameAnalyzing: false, middlegameError: null,
        middlegamePlanDraft: '', middlegameStrength: 'equal',
      });
      clearMiddlegameSessionId();
      hideNav();
      renderUserbox();
      render(state.currentView);
    });
  } else {
    box.innerHTML = '';
  }
}

// --- Глобальный индикатор анализа (в шапке, рядом с никнеймом) ---

let globalJobTimer = null;
let globalJobId = null;
let globalJobSeenAt = 0;
let globalJobToken = 0;
let cancelledJobId = null;

function formatEta(seconds) {
  if (seconds == null || !isFinite(seconds) || seconds < 0) return '';
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  return `${m}:${String(s).padStart(2, '0')}`;
}

// Elapsed по серверному старту задачи; при рассинхроне часов — локальный таймер.
function jobElapsedSeconds(job, fallbackStart) {
  const started = job && Number(job.started);
  if (started && started > 0) {
    const sec = Date.now() / 1000 - started;
    if (sec >= 0 && sec < 7 * 24 * 3600) return sec;
  }
  return Math.max(0, (Date.now() - fallbackStart) / 1000);
}

// ETA по текущему темпу: прошло / сделано * осталось.
function jobEtaSeconds(done, total, elapsedSeconds) {
  const d = Number(done) || 0;
  const t = Number(total) || 0;
  if (d <= 0 || t <= d) return null;
  return Math.max(0, Math.round((elapsedSeconds / d) * (t - d)));
}

function ensureGlobalJobIndicator() {
  let el = document.getElementById('global-job-indicator');
  if (el) return el;
  const right = document.querySelector('.topbar-right');
  if (!right) return null;
  el = document.createElement('a');
  el.id = 'global-job-indicator';
  el.className = 'global-job';
  el.href = '#';
  el.hidden = true;
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.title = 'Открыть экран анализа';
  el.addEventListener('click', e => {
    e.preventDefault();
    navigate('coach');
  });
  const userbox = document.getElementById('userbox');
  if (userbox && userbox.parentNode === right) right.insertBefore(el, userbox);
  else right.appendChild(el);
  return el;
}

function hideGlobalJobIndicator() {
  const el = document.getElementById('global-job-indicator');
  if (el) {
    el.hidden = true;
    el.textContent = '';
  }
  globalJobId = null;
  globalJobSeenAt = 0;
}

function paintGlobalJobIndicator(job) {
  const el = ensureGlobalJobIndicator();
  if (!el) return;
  if (job.id !== globalJobId) {
    globalJobId = job.id;
    globalJobSeenAt = Date.now();
  }
  const done = Number(job.done) || 0;
  const total = Number(job.total) || 0;
  const parts = [`Анализ: ${done}/${total}`];
  if (job.phase) parts.push(job.phase);
  const eta = jobEtaSeconds(done, total, jobElapsedSeconds(job, globalJobSeenAt));
  if (eta != null) parts.push('~' + formatEta(eta));
  el.textContent = parts.join(' · ');
  el.hidden = false;
}

async function pollGlobalJob() {
  if (!state.user) {
    stopGlobalJobPolling();
    hideGlobalJobIndicator();
    return;
  }
  const token = globalJobToken;
  let job;
  try {
    job = await api('/api/jobs/active');
  } catch (err) {
    // 404 = активной задачи нет (или сессия кончилась) — гасим индикатор.
    if (token !== globalJobToken) return;
    if (err && (err.status === 404 || isSessionError(err))) {
      stopGlobalJobPolling();
      hideGlobalJobIndicator();
    }
    return;
  }
  // Пока шёл запрос, индикатор могли погасить (задача завершилась/отменена).
  if (token !== globalJobToken) return;
  if (!job || !job.id || job.status !== 'running') {
    stopGlobalJobPolling();
    hideGlobalJobIndicator();
    return;
  }
  paintGlobalJobIndicator(job);
}

function startGlobalJobPolling() {
  if (!state.user) return;
  ensureGlobalJobIndicator();
  if (globalJobTimer) return;
  pollGlobalJob();
  globalJobTimer = setInterval(pollGlobalJob, 3000);
}

function stopGlobalJobPolling() {
  globalJobToken++;
  if (globalJobTimer) {
    clearInterval(globalJobTimer);
    globalJobTimer = null;
  }
}

// --- Views ---

async function render(view) {
  const app = document.getElementById('app');
  clearGameKeyboard();
  clearAllTimers();
  const auth = await authStatus();
  if (!auth.logged_in) {
    state.user = null;
    hideNav();
    renderUserbox();
    viewAuth(app);
    return;
  }
  showNav();
  app.innerHTML = '<div class="card"><p>Загрузка...</p></div>';

  try {
    switch (view) {
      case 'dashboard': await viewDashboard(app); break;
      case 'overview': await viewOverview(app); break;
      case 'plan': await viewPlan(app); break;
      case 'progress': await viewProgress(app); break;
      case 'games': await viewGames(app); break;
      case 'drills': await viewDrills(app); break;
      case 'task': await viewTask(app); break;
      case 'opening': await viewOpening(app); break;
      case 'middlegame': await viewMiddlegame(app); break;
      case 'coach': await viewCoach(app); break;
      case 'tools': await viewTools(app); break;
      case 'users': await viewUsers(app); break;
      default: app.innerHTML = '<div class="card"><p>Неизвестная страница</p></div>';
    }
  } catch (err) {
    if (isSessionError(err)) {
      handleSessionExpired();
      return;
    }
    app.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
  }
}

async function viewDashboard(app) {
  if (!state.user) {
    app.innerHTML = `
      <div class="onboarding card">
        <div class="empty-icon">🏁</div>
        <h1>Добро пожаловать в Chess Trainer</h1>
        <p>Проанализированных партий пока нет.<br>Нажми «Запустить анализ», чтобы загрузить партии с Lichess и пройти их Stockfish.</p>
        <a href="#" class="btn btn-primary" data-view="coach">Запустить анализ</a>
      </div>`;
    bindLinks(app);
    return;
  }

  const [plan, progress, task] = await Promise.all([
    api('/api/plan').catch(() => null),
    api('/api/progress?windows=5').catch(() => null),
    api('/api/task/today').catch(() => null),
  ]);

  if (!plan) {
    app.innerHTML = `
      <div class="onboarding card">
        <div class="empty-icon">🏁</div>
        <h1>Пока нет данных</h1>
        <p>Зарегистрировался — запусти анализ: партии с Lichess пройдут через Stockfish, и здесь появится план тренировки.</p>
        <a href="#" class="btn btn-primary" data-view="coach">Запустить анализ</a>
      </div>`;
    bindLinks(app);
    return;
  }

  const scorePct = plan.score_pct != null ? fmt(plan.score_pct) + '%' : '—';
  const avgWinLoss = plan.avg_win_loss != null ? fmt(plan.avg_win_loss, 2) : '—';
  const weakCount = plan.repertoire.weak_openings.length;

  let humanityHtml = '';
  if (plan.humanity) {
    const h = plan.humanity;
    humanityHtml = `
      <div class="grid grid-3">
        <div class="stat"><div class="stat-value">${h.total}</div><div class="stat-label">Всего плохих</div></div>
        <div class="stat"><div class="stat-value">${h.unnatural}</div><div class="stat-label">Неестественных</div></div>
        <div class="stat"><div class="stat-value">${fmt(h.unnatural_share_pct)}%</div><div class="stat-label">Доля неестественных</div></div>
      </div>`;
  } else {
    humanityHtml = '<p style="color:var(--c-text-muted)">Нет данных humanize.</p>';
  }

  let weakOpeningsHtml = '';
  if (plan.repertoire.weak_openings.length > 0) {
    weakOpeningsHtml = `
      <div class="table-wrap"><table>
        <thead><tr><th>Дебют</th><th>ECO</th><th>Партий</th><th>Очков %</th><th>Ошибок/партию</th><th>Ср. потеря</th><th></th></tr></thead>
        <tbody>
          ${plan.repertoire.weak_openings.slice(0, 6).map(o => `
            <tr>
              <td>${esc(o.opening)}</td><td>${esc(o.eco)}</td>
              <td class="num">${o.games}</td>
              <td class="num">${fmt(o.points_pct)}%</td>
              <td class="num">${fmt(o.errors_per_game, 2)}</td>
              <td class="num">${fmt(o.avg_drop)}%</td>
              <td class="center"><button type="button" class="btn btn-secondary" data-train-opening="${esc(o.eco || '')}" data-train-color="${esc(o.color || 'white')}" data-train-name="${esc(o.opening || '')}" style="font-size:12px;padding:4px 10px">Тренировать</button></td>
            </tr>`).join('')}
        </tbody>
      </table></div>`;
  } else {
    weakOpeningsHtml = '<p style="color:var(--c-text-muted)">Дебютов нет — запусти анализ.</p>';
  }

  let trendHtml = '';
  if (progress && progress.trend) {
    const t = progress.trend;
    const badgeClass = t.overall === 'improving' ? 'badge-good' : t.overall === 'worsening' ? 'badge-bad' : 'badge-neutral';
    let rows = '';
    for (const [m, v] of Object.entries(t)) {
      if (['improving', 'worsening', 'overall'].includes(m)) continue;
      const deltaClass = v.delta > 0 ? 'badge-bad' : 'badge-good';
      rows += `<tr>
        <td>${esc(m)}</td>
        <td class="num">${fmt(v.from, 2)}</td>
        <td class="num">${fmt(v.to, 2)}</td>
        <td class="num ${deltaClass}">${v.delta >= 0 ? '+' : ''}${fmt(v.delta, 2)}</td>
        <td>${esc(v.verdict)}</td>
      </tr>`;
    }
    trendHtml = `
      <div style="margin-bottom:8px"><span class="badge ${badgeClass}">${t.overall.toUpperCase()}</span></div>
      <div class="table-wrap"><table style="font-size:12px">
        <thead><tr><th>Метрика</th><th>Было</th><th>Стало</th><th>Δ</th><th>Вердикт</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>`;
  } else {
    trendHtml = '<p style="color:var(--c-text-muted)">Нет данных для тренда.</p>';
  }

  let motifsHtml = '';
  if (plan.patterns.motifs.length > 0) {
    motifsHtml = `
      <div class="table-wrap"><table>
        <thead><tr><th>Мотив</th><th>Частота</th><th title="Средняя потеря win% на ход игрока">Ср. потеря win%</th></tr></thead>
        <tbody>
          ${plan.patterns.motifs.slice(0, 5).map(m => `
            <tr><td>${esc(m[0])}</td><td class="num">${m[1].count}</td><td class="num">${fmt(m[1].avg_drop)}%</td></tr>`).join('')}
        </tbody>
      </table></div>`;
  } else {
    motifsHtml = '<p style="color:var(--c-text-muted)">Нет данных.</p>';
  }

  let taskCardHtml = '';
  if (task) {
    const taskBadge = task.done
      ? '<span class="badge badge-good">решено сегодня ✓</span>'
      : '<span class="badge badge-neutral">не решено</span>';
    const taskCls = task.done && task.classification
      ? `<span class="badge ${task.classification === 'blunder' ? 'badge-bad' : 'badge-neutral'}">${esc(task.classification)}</span>`
      : '';
    taskCardHtml = `
      <div class="card">
        <div class="card-header"><span class="card-title">Задача дня</span>${taskBadge}</div>
        <div style="color:var(--c-text-muted);font-size:13px;margin-bottom:10px">${esc(task.date)} · ${esc(task.opening || '—')}${task.eco ? ` (${esc(task.eco)})` : ''} ${taskCls}</div>
        <a href="#" class="btn btn-primary" data-view="task">Открыть</a>
      </div>`;
  }

  app.innerHTML = `
    <div class="card-header"><h1>Дашборд</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    ${taskCardHtml}
    <div class="grid grid-4">
      <div class="card stat"><div class="stat-value">${plan.games}</div><div class="stat-label">Партий</div></div>
      <div class="card stat"><div class="stat-value">${scorePct}</div><div class="stat-label">Счёт</div></div>
      <div class="card stat"><div class="stat-value">${avgWinLoss}</div><div class="stat-label" title="Средняя потеря win% (процентов) на ход игрока (метрика avg_win_loss из анализа)">Ср. потеря win%</div></div>
      <div class="card stat"><div class="stat-value">${weakCount}</div><div class="stat-label">Дебютов в фокусе</div></div>
    </div>
    <div class="grid grid-2">
      <div class="card">
        <div class="card-header"><span class="card-title">Ошибки</span></div>
        <div class="grid grid-2">
          <div class="stat"><div class="stat-value">${plan.drills.total}</div><div class="stat-label">Всего</div></div>
          <div class="stat"><div class="stat-value">${plan.drills.unnatural}</div><div class="stat-label">Неестественные</div></div>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Человечность ошибок</span></div>
        ${humanityHtml}
      </div>
    </div>
    <div class="grid grid-2">
      <div class="card">
        <div class="card-header"><span class="card-title">Слабые дебюты (топ-6)</span></div>
        ${weakOpeningsHtml}
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Тренд</span></div>
        ${trendHtml}
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Топ-мотивы ошибок</span></div>
      ${motifsHtml}
    </div>
    <div style="text-align:center;margin-top:16px">
      <a href="#" class="btn btn-secondary" data-view="plan">Полный план →</a>
      <a href="#" class="btn btn-secondary" data-view="progress">Подробный прогресс →</a>
      <a href="#" class="btn btn-primary" data-view="coach">Запустить анализ →</a>
    </div>`;
  bindLinks(app);

  // Кнопка «Тренировать» у слабого дебюта: стартует тренажёр дебютов
  // с нужным цветом, а не перекидывает автоматически.
  app.querySelectorAll('[data-train-opening]').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const res = await api('/api/opening/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            style: 'universal',
            color: btn.dataset.trainColor || 'white',
            human: true,
          }),
        });
        startOpeningSession(res);
        navigate('opening');
      } catch (err) {
        if (isSessionError(err)) { handleSessionExpired(); return; }
        showToast(err.message, 'error');
        btn.disabled = false;
      }
    });
  });
}

async function viewOverview(app) {
  if (!state.user) {
    app.innerHTML = '<div class="onboarding card"><div class="empty-icon">📋</div><h1>Нет данных</h1><p>Профиль не выбран.</p></div>';
    return;
  }
  const ov = await api('/api/overview').catch(() => null);
  if (!ov) {
    app.innerHTML = `
      <div class="onboarding card">
        <div class="empty-icon">📋</div>
        <h1>Пока нет данных</h1>
        <p>Зарегистрировался — запусти анализ, чтобы собрать репертуар и обзор ошибок.</p>
        <a href="#" class="btn btn-primary" data-view="coach">Запустить анализ</a>
      </div>`;
    bindLinks(app);
    return;
  }

  const whiteBranches = ov.branches.white || [];
  const blackBranches = ov.branches.black || [];

  app.innerHTML = `
    <div class="card-header"><h1>Обзор</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="grid grid-4">
      <div class="card stat"><div class="stat-value">${ov.games}</div><div class="stat-label">Партий</div></div>
      <div class="card stat"><div class="stat-value">${whiteBranches.length + blackBranches.length}</div><div class="stat-label">Ветвей репертуара</div></div>
      <div class="card stat"><div class="stat-value">${ov.openings.length}</div><div class="stat-label">Дебютов</div></div>
      <div class="card stat"><div class="stat-value">${ov.drills_total}</div><div class="stat-label">Ошибок</div></div>
    </div>
    <div class="grid grid-2">
      <div class="card">
        <div class="card-header"><span class="card-title">Репертуар белых</span></div>
        ${whiteBranches.length ? `<div class="table-wrap"><table><thead><tr><th>Ветвь</th><th>Партий</th><th>Прочность</th></tr></thead><tbody>${whiteBranches.map(b => `<tr><td><code>${esc(b.moves.join(' '))}</code></td><td class="num">${b.count}</td><td class="num">${fmt(b.path_ok_rate * 100, 0)}%</td></tr>`).join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Репертуар чёрных</span></div>
        ${blackBranches.length ? `<div class="table-wrap"><table><thead><tr><th>Ветвь</th><th>Партий</th><th>Прочность</th></tr></thead><tbody>${blackBranches.map(b => `<tr><td><code>${esc(b.moves.join(' '))}</code></td><td class="num">${b.count}</td><td class="num">${fmt(b.path_ok_rate * 100, 0)}%</td></tr>`).join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Слабые места репертуара</span></div>
      ${ov.openings.length ? `<div class="table-wrap"><table><thead><tr><th>Дебют</th><th>ECO</th><th>Партий</th><th>Очков %</th><th>Ошибок/партию</th><th>Ср. потеря</th></tr></thead><tbody>${ov.openings.slice(0, 12).map(o => `<tr><td>${esc(o.opening)}</td><td>${esc(o.eco)}</td><td class="num">${o.games}</td><td class="num">${fmt(o.points_pct)}%</td><td class="num">${fmt(o.errors_per_game, 2)}</td><td class="num">${fmt(o.avg_drop)}%</td></tr>`).join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Человечность ошибок</span></div>
      ${ov.humanity ? `<div class="grid grid-4"><div class="stat"><div class="stat-value">${ov.humanity.total}</div><div class="stat-label">Всего плохих</div></div><div class="stat"><div class="stat-value">${ov.humanity.natural}</div><div class="stat-label">Естественные</div></div><div class="stat"><div class="stat-value">${ov.humanity.borderline}</div><div class="stat-label">Пограничные</div></div><div class="stat"><div class="stat-value">${ov.humanity.unnatural}</div><div class="stat-label">Неестественные</div></div></div>` : '<p style="color:var(--c-text-muted)">Нет файла humanity.json.</p>'}
    </div>`;
}

async function viewPlan(app) {
  if (!state.user) {
    app.innerHTML = '<div class="onboarding card"><div class="empty-icon">📋</div><h1>Нет плана</h1><p>Профиль не выбран.</p></div>';
    return;
  }
  const plan = await api('/api/plan').catch(() => null);
  if (!plan) {
    app.innerHTML = `
      <div class="onboarding card">
        <div class="empty-icon">📋</div>
        <h1>Пока нет плана</h1>
        <p>Зарегистрировался — запусти анализ, и тренер соберёт план тренировки.</p>
        <a href="#" class="btn btn-primary" data-view="coach">Запустить анализ</a>
      </div>`;
    bindLinks(app);
    return;
  }
  const [taskToday, track, report, goalData, weekData, llmData] = await Promise.all([
    api('/api/task/today').catch(() => null),
    api('/api/track/today').catch(() => null),
    api('/api/report').catch(() => null),
    api('/api/plan/goal').catch(() => null),
    api('/api/plan/week').catch(() => null),
    api('/api/plan/llm').catch(() => null),
  ]);

  const focusRu = { auto: 'авто', opening: 'дебют', middlegame: 'миттельшпиль', endgame: 'эндшпиль' };
  const goalTypeRu = { rating: 'Поднять рейтинг', tournament: 'Подготовка к турниру', fix_weak: 'Исправить слабые места' };
  const goalFormHtml = (goal, minutes, focus, goalType, tournamentDate, targetRating) => {
    const gt = goalTypeRu[goalType] ? goalType : 'fix_weak';
    return `
    <form id="plan-goal-form" class="form-row" style="gap:12px;align-items:flex-end;flex-wrap:wrap">
      <div class="form-group" style="flex:2;min-width:180px;margin-bottom:0">
        <label for="plan-goal-text">Твоя цель</label>
        <input type="text" id="plan-goal-text" name="goal" maxlength="200" placeholder="например, 2000 на Lichess к лету"
               value="${esc(goal || '')}" autocomplete="off" required>
      </div>
      <div class="form-group" style="flex:0;min-width:180px;margin-bottom:0">
        <label for="plan-goal-type">Тип цели</label>
        <select id="plan-goal-type" name="goal_type">
          ${Object.entries(goalTypeRu).map(([v, label]) => `<option value="${v}"${gt === v ? ' selected' : ''}>${label}</option>`).join('')}
        </select>
      </div>
      <div class="form-group" id="plan-goal-date-wrap" style="flex:0;min-width:150px;margin-bottom:0;${gt === 'tournament' ? '' : 'display:none'}">
        <label for="plan-goal-date">Дата турнира</label>
        <input type="date" id="plan-goal-date" name="tournament_date" value="${esc(tournamentDate || '')}">
      </div>
      <div class="form-group" id="plan-goal-rating-wrap" style="flex:0;min-width:130px;margin-bottom:0;${gt === 'rating' ? '' : 'display:none'}">
        <label for="plan-goal-rating">Целевой рейтинг</label>
        <input type="number" id="plan-goal-rating" name="target_rating" min="800" max="3200" step="1" value="${esc(targetRating || '')}" placeholder="2000">
      </div>
      <div class="form-group" style="flex:0;min-width:110px;margin-bottom:0">
        <label for="plan-goal-minutes">Минут в день</label>
        <input type="number" id="plan-goal-minutes" name="minutes" min="5" max="480" step="1" value="${minutes || 30}" required>
      </div>
      <div class="form-group" style="flex:0;min-width:140px;margin-bottom:0">
        <label for="plan-goal-focus">Фокус</label>
        <select id="plan-goal-focus" name="focus">
          ${Object.entries(focusRu).map(([v, label]) => `<option value="${v}"${(focus || 'auto') === v ? ' selected' : ''}>${label}</option>`).join('')}
        </select>
      </div>
      <button class="btn btn-primary" style="height:38px">Сохранить</button>
    </form>
    <div class="alert alert-error" id="plan-goal-err" style="display:none;margin-top:8px"></div>`;
  };

  let goalHtml = '';
  if (goalData) {
    const gExtra = goalData.goal_type === 'tournament' && goalData.tournament_date
      ? ` · турнир ${esc(goalData.tournament_date)}${goalData.weeks_left != null ? ` (осталось ${goalData.weeks_left} нед.)` : ''}`
      : goalData.goal_type === 'rating' && goalData.target_rating
        ? ` · цель ${goalData.target_rating}`
        : '';
    if (goalData.goal) {
      goalHtml = `<div class="card"><div class="card-header"><span class="card-title">Твоя цель</span></div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <span class="badge badge-good">цель</span>
          <span style="flex:1;min-width:180px">«${esc(goalData.goal)}» · ${esc(goalTypeRu[goalData.goal_type] || goalData.goal_type || '')}${gExtra} · ${goalData.minutes} мин/день · фокус: ${esc(focusRu[goalData.focus] || goalData.focus)}</span>
          <a href="#" class="btn btn-link" id="plan-goal-edit">изменить</a>
        </div>
        <div id="plan-goal-slot" style="margin-top:8px"></div></div>`;
    } else {
      goalHtml = `<div class="card"><div class="card-header"><span class="card-title">Твоя цель</span><span class="badge badge-blue">шаг 1 из 2</span></div>
        <p style="color:var(--c-text-muted);font-size:13px;margin-top:0">Скажи, к чему идём — и неделя посчитается под твою цель.</p>
        ${goalFormHtml('', 30, 'auto', 'fix_weak', '', '')}</div>`;
    }
  } else {
    // Цели ещё нет вообще: показываем опросник сразу, а не пустое место.
    goalHtml = `<div class="card"><div class="card-header"><span class="card-title">Твоя цель</span><span class="badge badge-blue">шаг 1 из 2</span></div>
      <p style="color:var(--c-text-muted);font-size:13px;margin-top:0">С чего начнём? Укажи цель, время в день и ближайший турнир — дальше тренер соберёт неделю под тебя.</p>
      ${goalFormHtml('', 30, 'auto', 'fix_weak', '', '')}</div>`;
  }

  let llmHtml = '';
  if (state.planLlmGenerating) {
    llmHtml = `<div class="card"><div class="card-header"><span class="card-title">Индивидуальный план</span></div>
      <p style="color:var(--c-text-muted);font-size:13px;margin:0">генерирую…</p></div>`;
  } else if (llmData && llmData.text) {
    llmHtml = `<div class="card"><div class="card-header"><span class="card-title">Индивидуальный план</span>${llmData.created_at ? `<span class="badge badge-neutral">${esc(llmData.created_at)}</span>` : ''}</div>
      <div class="mentor-reply">${esc(llmData.text)}</div>
      ${state.planLlmError ? `<div class="alert alert-error" style="margin-top:8px">${esc(state.planLlmError)}</div>` : ''}
      <div style="margin-top:8px"><button class="btn btn-secondary" id="plan-llm-refresh" style="font-size:12px;padding:4px 10px">Обновить</button></div></div>`;
  } else {
    llmHtml = `<div class="card"><div class="card-header"><span class="card-title">Индивидуальный план</span><span class="badge badge-blue">шаг 2 из 2</span></div>
      <p style="color:var(--c-text-muted);font-size:13px;margin-top:0">Пройди опросник выше — и тренер соберёт неделю под твою цель, время и слабые места.</p>
      ${state.planLlmError ? `<div class="alert alert-error" style="margin-bottom:8px">${esc(state.planLlmError)}</div>` : ''}
      <button class="btn btn-primary" id="plan-llm-create" style="width:fit-content">Создать план на неделю (LLM)</button></div>`;
  }

  let weekHtml = '';
  if (weekData && weekData.focus && Array.isArray(weekData.week_tasks)) {
    const wPhase = esc(focusRu[weekData.focus.phase] || weekData.focus.phase);
    weekHtml = `<div class="card"><div class="card-header"><span class="card-title">План на неделю</span>
        <span class="badge badge-blue">фокус: ${wPhase}</span></div>
      <div style="color:var(--c-text-muted);font-size:13px;margin-bottom:8px">${esc(weekData.focus.reason || '')}</div>
      ${weekData.week_tasks.map(t => `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:6px 0"><span class="badge badge-neutral">${esc(t.key)}</span><span style="flex:1;min-width:180px"><strong>${esc(t.text)}</strong><br><span style="color:var(--c-text-muted);font-size:12px">${esc(t.detail || '')}</span></span></div>`).join('')}
      ${weekData.human_summary ? `<div class="mentor-reply" style="margin-top:8px">${esc(weekData.human_summary)}</div>` : ''}</div>`;
  }

  let todayHtml = '';
  if (taskToday || track || report) {
    const rows = [];
    if (taskToday) {
      const done = !!taskToday.done;
      rows.push(`<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:6px 0"><span class="badge ${done ? 'badge-good' : 'badge-neutral'}">${done ? 'выполнено' : 'осталось'}</span><span style="flex:1;min-width:180px">Задача дня${done ? ' — решена' : ' — не решена'}</span><button class="btn btn-secondary" data-plan-act="task" style="font-size:12px;padding:4px 10px">Открыть</button></div>`);
    }
    if (track) {
      rows.push(`<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:6px 0"><span class="badge badge-blue">время</span><span style="flex:1;min-width:180px">Время сегодня: ${_fmtDur(track.total_sec)}</span><button class="btn btn-secondary" data-plan-act="users" style="font-size:12px;padding:4px 10px">Профили</button></div>`);
    }
    if (plan && plan.drills) {
      const un = Number(plan.drills.unnatural) || 0;
      const total = Number(plan.drills.total) || 0;
      const clean = un === 0;
      rows.push(`<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:6px 0"><span class="badge ${clean ? 'badge-good' : 'badge-neutral'}">${clean ? 'чисто' : 'повторить'}</span><span style="flex:1;min-width:180px">${clean ? 'Ошибки: неестественных нет' : `Повторить ${un} неестественных из ${total}`}</span><button class="btn btn-secondary" data-plan-act="drills" style="font-size:12px;padding:4px 10px">Ошибки</button></div>`);
    }
    const worst = report && report.worst_games && report.worst_games[0];
    if (worst) {
      rows.push(`<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:6px 0"><span class="badge badge-bad">разобрать</span><span style="flex:1;min-width:180px">Разобрать: ${esc(worst.opponent || '—')} (${esc(String(worst.game_id).slice(0, 8))}…)</span><button class="btn btn-secondary" data-plan-act="worst" data-game-id="${esc(worst.game_id)}" style="font-size:12px;padding:4px 10px">Открыть</button></div>`);
    }
    if (rows.length) {
      todayHtml = `<div class="card"><div class="card-header"><span class="card-title">План на сегодня</span></div>${rows.join('')}</div>`;
    }
  }

  const scorePct = plan.score_pct != null ? fmt(plan.score_pct) + '%' : '—';
  const avgWinLoss = plan.avg_win_loss != null ? fmt(plan.avg_win_loss, 2) : '—';

  app.innerHTML = `
    <div class="card-header"><h1>План тренировки</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    ${goalHtml}
    ${llmHtml}
    ${weekHtml}
    ${todayHtml}
    <div class="grid grid-4">
      <div class="card stat"><div class="stat-value">${plan.games}</div><div class="stat-label">Партий</div></div>
      <div class="card stat"><div class="stat-value">${scorePct}</div><div class="stat-label">Счёт</div></div>
      <div class="card stat"><div class="stat-value">${avgWinLoss}</div><div class="stat-label" title="Средняя потеря win% (процентов) на ход игрока (метрика avg_win_loss из анализа)">Ср. потеря win%</div></div>
      <div class="card stat"><div class="stat-value">${plan.drills.total}/${plan.drills.unnatural}</div><div class="stat-label">Ошибки всего/неест.</div></div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Репертуар — частые ветви</span></div>
      <div class="grid grid-2">
        <div><h3>Белые</h3>${plan.repertoire.white.length ? `<div class="table-wrap"><table style="font-size:12px"><thead><tr><th>Ветвь</th><th>Партий</th><th>OK %</th></tr></thead><tbody>${plan.repertoire.white.map(b => `<tr><td><code>${esc(b.moves.join(' '))}</code></td><td class="num">${b.count}</td><td class="num">${fmt(b.ok_rate * 100, 0)}%</td></tr>`).join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}</div>
        <div><h3>Чёрные</h3>${plan.repertoire.black.length ? `<div class="table-wrap"><table style="font-size:12px"><thead><tr><th>Ветвь</th><th>Партий</th><th>OK %</th></tr></thead><tbody>${plan.repertoire.black.map(b => `<tr><td><code>${esc(b.moves.join(' '))}</code></td><td class="num">${b.count}</td><td class="num">${fmt(b.ok_rate * 100, 0)}%</td></tr>`).join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}</div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Слабые дебюты</span></div>
      ${plan.repertoire.weak_openings.length ? `<div class="table-wrap"><table><thead><tr><th>Дебют</th><th>ECO</th><th>Партий</th><th>Очков %</th><th>Ошибок/партию</th><th>Ср. потеря</th></tr></thead><tbody>${plan.repertoire.weak_openings.slice(0, 6).map(o => `<tr><td>${esc(o.opening)}</td><td>${esc(o.eco)}</td><td class="num">${o.games}</td><td class="num">${fmt(o.points_pct)}%</td><td class="num">${fmt(o.errors_per_game, 2)}</td><td class="num">${fmt(o.avg_drop)}%</td></tr>`).join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted)">Нет слабых дебютов.</p>'}
    </div>
    <div class="grid grid-2">
      <div class="card">
        <div class="card-header"><span class="card-title">Мотивы ошибок</span></div>
        ${plan.patterns.motifs.length ? `<div class="table-wrap"><table><thead><tr><th>Мотив</th><th>Частота</th><th>Ср. потеря</th></tr></thead><tbody>${plan.patterns.motifs.map(m => `<tr><td>${esc(m[0])}</td><td class="num">${m[1].count}</td><td class="num">${fmt(m[1].avg_drop)}%</td></tr>`).join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Фазы ошибок</span></div>
        ${plan.patterns.phases.length ? `<div class="table-wrap"><table><thead><tr><th>Фаза</th><th>Ошибок</th><th>Грубых</th><th>Обычных</th><th>Ср. потеря</th></tr></thead><tbody>${plan.patterns.phases.map(p => `<tr><td>${esc(p[0])}</td><td class="num">${p[1].count}</td><td class="num">${p[1].blunders}</td><td class="num">${p[1].mistakes}</td><td class="num">${fmt(p[1].avg_drop)}%</td></tr>`).join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Человечность ошибок</span></div>
      ${plan.humanity ? `<div class="grid grid-4"><div class="stat"><div class="stat-value">${plan.humanity.total}</div><div class="stat-label">Всего</div></div><div class="stat"><div class="stat-value">${plan.humanity.natural}</div><div class="stat-label">Естественные</div></div><div class="stat"><div class="stat-value">${plan.humanity.borderline}</div><div class="stat-label">Пограничные</div></div><div class="stat"><div class="stat-value">${plan.humanity.unnatural}</div><div class="stat-label">Неестественные</div></div></div><div style="margin-top:8px"><strong>Доля неестественных: </strong>${fmt(plan.humanity.unnatural_share_pct)}%</div>` : '<p style="color:var(--c-text-muted)">Нет данных humanize.</p>'}
    </div>`;

  app.querySelectorAll('[data-plan-act]').forEach(btn => {
    btn.addEventListener('click', () => {
      const act = btn.dataset.planAct;
      if (act === 'task') navigate('task');
      else if (act === 'users') navigate('users');
      else if (act === 'drills') navigate('drills');
      else if (act === 'worst' && btn.dataset.gameId) {
        state.gameId = btn.dataset.gameId;
        state.currentView = 'games';
        document.querySelectorAll('#nav a').forEach(a => {
          a.classList.toggle('active', a.dataset.view === 'games');
        });
        viewGames(app);
      }
    });
  });

  const bindGoalForm = () => {
    const form = document.getElementById('plan-goal-form');
    if (!form) return;
    const typeSel = document.getElementById('plan-goal-type');
    const dateWrap = document.getElementById('plan-goal-date-wrap');
    const ratingWrap = document.getElementById('plan-goal-rating-wrap');
    const toggleGoalType = () => {
      const v = typeSel ? typeSel.value : 'fix_weak';
      if (dateWrap) dateWrap.style.display = v === 'tournament' ? '' : 'none';
      if (ratingWrap) ratingWrap.style.display = v === 'rating' ? '' : 'none';
    };
    if (typeSel) {
      typeSel.addEventListener('change', toggleGoalType);
      toggleGoalType();
    }
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const err = document.getElementById('plan-goal-err');
      const fd = new FormData(form);
      try {
        await apiPost('/api/plan/goal', fd);
        await viewPlan(app);
      } catch (apiErr) {
        if (err) {
          err.textContent = apiErr.message;
          err.style.display = 'block';
        }
      }
    });
  };
  bindGoalForm();
  const goalEdit = document.getElementById('plan-goal-edit');
  if (goalEdit) {
    goalEdit.addEventListener('click', e => {
      e.preventDefault();
      const slot = document.getElementById('plan-goal-slot');
      if (!slot) return;
      if (slot.innerHTML) {
        slot.innerHTML = '';
        return;
      }
      slot.innerHTML = goalFormHtml(goalData.goal, goalData.minutes, goalData.focus, goalData.goal_type, goalData.tournament_date, goalData.target_rating);
      bindGoalForm();
    });
  }
  const llmCreate = document.getElementById('plan-llm-create');
  if (llmCreate) llmCreate.addEventListener('click', () => planLlmGenerate(app, false));
  const llmRefresh = document.getElementById('plan-llm-refresh');
  if (llmRefresh) llmRefresh.addEventListener('click', () => planLlmGenerate(app, true));
  // вернулись на страницу, пока шёл план, — возобновляем поллинг джобы
  if (state.planLlmGenerating && !state.timers.llm && state.planLlmJobId) {
    planLlmPoll(app, state.planLlmJobId);
  }
}

async function planLlmGenerate(app, regenerate) {
  state.planLlmGenerating = true;
  state.planLlmError = null;
  try {
    const fd = new FormData();
    if (regenerate) fd.set('regenerate', 'on');
    const res = await apiPost('/api/plan/generate', fd);
    if (res.cached) {
      state.planLlmGenerating = false;
      state.planLlmJobId = null;
      await viewPlan(app);
      return;
    }
    state.planLlmJobId = res.job_id;
    await viewPlan(app);
    planLlmPoll(app, res.job_id);
  } catch (err) {
    state.planLlmGenerating = false;
    state.planLlmJobId = null;
    state.planLlmError = err.message;
    showToast(err.message, 'error');
    await viewPlan(app);
  }
}

function planLlmPoll(app, jobId) {
  if (state.timers.llm) clearInterval(state.timers.llm);
  state.timers.llm = setInterval(async () => {
    if (state.currentView !== 'plan') { clearInterval(state.timers.llm); state.timers.llm = null; return; }
    let job;
    try {
      job = await api('/api/jobs/' + jobId);
    } catch {
      clearInterval(state.timers.llm);
      state.timers.llm = null;
      state.planLlmGenerating = false;
      state.planLlmJobId = null;
      state.planLlmError = 'Не удалось получить план';
      viewPlan(app);
      return;
    }
    if (job.status === 'done') {
      clearInterval(state.timers.llm);
      state.timers.llm = null;
      state.planLlmGenerating = false;
      state.planLlmJobId = null;
      viewPlan(app);
    } else if (job.status === 'error') {
      clearInterval(state.timers.llm);
      state.timers.llm = null;
      state.planLlmGenerating = false;
      state.planLlmJobId = null;
      state.planLlmError = job.error || 'Не удалось получить план';
      viewPlan(app);
    }
  }, 700);
}

async function viewProgress(app) {
  if (!state.user) {
    app.innerHTML = '<div class="onboarding card"><div class="empty-icon">📈</div><h1>Нет данных</h1><p>Профиль не выбран.</p></div>';
    return;
  }
  const pr = await api('/api/progress?windows=5').catch(() => null);
  if (!pr) {
    app.innerHTML = `
      <div class="onboarding card">
        <div class="empty-icon">📈</div>
        <h1>Пока нет данных</h1>
        <p>Зарегистрировался — запусти анализ, чтобы увидеть прогресс по окнам.</p>
        <a href="#" class="btn btn-primary" data-view="coach">Запустить анализ</a>
      </div>`;
    bindLinks(app);
    return;
  }

  let trendRows = '';
  if (pr.trend) {
    for (const [m, v] of Object.entries(pr.trend)) {
      if (['improving', 'worsening', 'overall'].includes(m)) continue;
      const deltaClass = v.delta > 0 ? 'badge-bad' : 'badge-good';
      trendRows += `<tr><td>${esc(m)}</td><td class="num">${fmt(v.from, 2)}</td><td class="num">${fmt(v.to, 2)}</td><td class="num ${deltaClass}">${v.delta >= 0 ? '+' : ''}${fmt(v.delta, 2)}</td><td>${esc(v.direction)}</td><td><span class="badge ${v.verdict === 'better' ? 'badge-good' : v.verdict === 'worse' ? 'badge-bad' : 'badge-neutral'}">${esc(v.verdict)}</span></td></tr>`;
    }
  }

  const overall = pr.trend ? pr.trend.overall : '—';
  const badgeClass = overall === 'improving' ? 'badge-good' : overall === 'worsening' ? 'badge-bad' : 'badge-neutral';

  app.innerHTML = `
    <div class="card-header"><h1>Прогресс</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="card">
      <div class="card-header"><span class="card-title">Таблица окон (${pr.windows} окон, ${pr.games} партий)</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>№</th><th>Период</th><th>Партий</th><th>Счёт %</th><th title="Средняя потеря win% на ход игрока">Ср. потеря win%</th><th title="Средний win% перед ходом игрока">Win% до хода</th><th>Ошибок/партию</th><th>Неест. %</th><th>Ср. рейтинг</th></tr></thead>
        <tbody>
          ${pr.windows_rows.map(w => `<tr>
            <td class="num">${w.index}</td><td>${esc(w.dates)}</td><td class="num">${w.games}</td>
            <td class="num">${fmt(w.score_pct)}</td>
            <td class="num">${fmt(w.avg_win_loss)}</td>
            <td class="num">${fmt(w.avg_win_before)}</td>
            <td class="num">${fmt(w.errors_per_game)}</td>
            <td class="num">${fmt(w.unnatural_share_pct)}%</td>
            <td class="num">${w.avg_rating != null ? w.avg_rating : '—'}</td>
          </tr>`).join('')}
        </tbody>
      </table></div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Тренд метрик</span></div>
      ${pr.trend ? `<div style="margin-bottom:8px"><span class="badge ${badgeClass}">${overall.toUpperCase()}</span></div>
      <div class="table-wrap"><table><thead><tr><th>Метрика</th><th>Было</th><th>Стало</th><th>Δ</th><th>Направление</th><th>Вердикт</th></tr></thead><tbody>${trendRows}</tbody></table></div>
      <div style="margin-top:12px;font-size:13px"><strong>Улучшается: </strong>${pr.trend.improving.length ? pr.trend.improving.join(', ') : '—'}<br><strong>Ухудшается: </strong>${pr.trend.worsening.length ? pr.trend.worsening.join(', ') : '—'}</div>` : '<p style="color:var(--c-text-muted)">Недостаточно данных для тренда.</p>'}
    </div>`;
}

async function viewGames(app) {
  if (!state.user) {
    app.innerHTML = '<div class="onboarding card"><div class="empty-icon">♟</div><h1>Нет профиля</h1><p>Выбери профиль на странице Профили.</p></div>';
    return;
  }
  if (state.gameId) {
    await viewGameDetail(app);
  } else {
    await viewGameList(app);
  }
}

async function viewGameList(app) {
  state.gameId = null;
  state.gameData = null;
  state.gameReview = null;
  clearGameKeyboard();
  app.innerHTML = '<div class="card"><p>Загрузка...</p></div>';
  const data = await api('/api/games?limit=100');
  const games = data.games;
  const filtered = state.gamesFilter === 'analyzed' ? games.filter(g => g.analyzed) : games;

  const rows = filtered.map(g => {
    const res = g.result_for_user || 'draw';
    const resBadge = res === 'win' ? 'badge-good' : res === 'loss' ? 'badge-bad' : 'badge-neutral';
    const resLabel = res === 'win' ? 'W' : res === 'loss' ? 'L' : 'D';
    const opening = (g.opening ? esc(g.opening) : '—') + (g.eco ? ` <span style="color:var(--c-text-muted)">${esc(g.eco)}</span>` : '');
    return `<tr class="game-row" data-id="${esc(g.id)}">
      <td>${new Date(g.created_at).toLocaleDateString('ru-RU')}</td>
      <td>${esc(g.opponent || '—')}</td>
      <td class="center">${g.user_color === 'white' ? '♔' : '♚'}</td>
      <td class="center"><span class="badge ${resBadge}">${resLabel}</span></td>
      <td>${opening}</td>
      <td class="num">${g.moves}</td>
      <td class="center">${g.analyzed ? '<span class="badge badge-good">анализ</span>' : `<button type="button" class="btn btn-secondary analyze-game" data-id="${esc(g.id)}" style="font-size:12px;padding:3px 8px">Анализ</button>`}</td>
    </tr>`;
  }).join('');

  app.innerHTML = `
    <div class="card-header"><h1>Партии</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="card">
      <div class="card-header"><span class="card-title">Импорт PGN</span><span class="badge badge-neutral">1–20 партий</span></div>
      <form id="pgn-import-form">
        <div class="form-group">
          <label for="pgn-text">PGN</label>
          <textarea id="pgn-text" name="pgn" rows="7" placeholder="[Event ...]&#10;[White ...]&#10;1. e4 e5 2. Nf3 ..." style="min-height:140px;font-family:var(--font-mono,monospace);font-size:12px"></textarea>
        </div>
        <div class="form-row" style="align-items:flex-end">
          <div class="form-group"><label for="pgn-file">Или файл .pgn</label><input type="file" id="pgn-file" accept=".pgn,text/plain"></div>
          <div class="form-group"><label for="pgn-color">Твой цвет</label>
            <select id="pgn-color"><option value="">определить по нику</option><option value="white">белые</option><option value="black">чёрные</option></select>
          </div>
          <div class="form-group" style="display:flex;align-items:center;gap:6px;min-height:38px">
            <input type="checkbox" id="pgn-analyze"><label for="pgn-analyze" style="margin:0;white-space:nowrap">сразу проанализировать</label>
          </div>
          <button type="submit" class="btn btn-primary" style="height:38px">Импортировать</button>
        </div>
        <div id="pgn-import-status" class="alert alert-error" style="display:none;margin-top:8px"></div>
        <p style="color:var(--c-text-muted);font-size:12px;margin:8px 0 0">Вставь одну или несколько партий. Если ника нет в заголовках — выбери свой цвет. Анализ сразу — для небольших импортов (до 3 партий).</p>
      </form>
    </div>
    <div class="card">
      <div class="games-filter">
        <button class="btn btn-secondary ${state.gamesFilter === 'all' ? 'btn-active' : ''}" data-gfilter="all">Все</button>
        <button class="btn btn-secondary ${state.gamesFilter === 'analyzed' ? 'btn-active' : ''}" data-gfilter="analyzed">Только проанализированные</button>
      </div>
      ${filtered.length ? `<div class="table-wrap"><table><thead><tr><th>Дата</th><th>Соперник</th><th>Цвет</th><th>Результат</th><th>Дебют</th><th class="num">Ходов</th><th>Анализ</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p style="color:var(--c-text-muted);padding:16px">Партий нет. Запусти анализ на странице «Анализ».</p>'}
    </div>`;

  app.querySelectorAll('[data-gfilter]').forEach(btn => {
    btn.addEventListener('click', () => {
      state.gamesFilter = btn.dataset.gfilter;
      viewGameList(app);
    });
  });
  app.querySelectorAll('.game-row').forEach(row => {
    row.addEventListener('click', () => {
      state.gameId = row.dataset.id;
      viewGames(app);
    });
  });
  const importForm = document.getElementById('pgn-import-form');
  if (importForm) {
    importForm.addEventListener('submit', async e => {
      e.preventDefault();
      const btn = importForm.querySelector('button[type="submit"]');
      const status = document.getElementById('pgn-import-status');
      const text = document.getElementById('pgn-text').value.trim();
      const fileInput = document.getElementById('pgn-file');
      const file = fileInput && fileInput.files && fileInput.files[0];
      if (!text && !file) {
        if (status) { status.textContent = 'Вставь PGN или выбери файл'; status.style.display = 'block'; }
        return;
      }
      const fd = new FormData();
      if (file) fd.append('file', file);
      else fd.append('pgn', text);
      const color = document.getElementById('pgn-color').value;
      if (color) fd.append('user_color', color);
      if (document.getElementById('pgn-analyze').checked) fd.append('analyze', 'on');
      btn.disabled = true;
      btn.textContent = 'Импортирую…';
      if (status) { status.style.display = 'none'; status.textContent = ''; }
      try {
        const res = await apiPost('/api/games/import', fd);
        showToast(
          'Импортировано: ' + res.imported + (res.analyzed ? ', проанализировано: ' + res.analyzed : ''),
          'info',
        );
        viewGameList(app);
      } catch (err) {
        if (isSessionError(err)) { handleSessionExpired(); return; }
        if (status) { status.textContent = err.message; status.style.display = 'block'; }
        showToast(err.message, 'error');
        btn.disabled = false;
        btn.textContent = 'Импортировать';
      }
    });
  }
  app.querySelectorAll('.analyze-game').forEach(btn => {
    btn.addEventListener('click', async e => {
      e.stopPropagation();
      btn.disabled = true;
      btn.textContent = '…';
      try {
        await api('/api/games/' + encodeURIComponent(btn.dataset.id) + '/analyze', { method: 'POST' });
        viewGameList(app);
      } catch (err) {
        showToast(err.message, 'error');
        btn.disabled = false;
        btn.textContent = 'Анализ';
      }
    });
  });
}

async function viewGameDetail(app) {
  clearGameKeyboard();
  app.innerHTML = '<div class="card"><p>Загрузка...</p></div>';
  let data;
  try {
    data = await api('/api/games/' + encodeURIComponent(state.gameId));
  } catch (err) {
    state.gameId = null;
    app.innerHTML = `<div class="card"><div class="alert alert-error">${esc(err.message)}</div><a href="#" class="btn btn-secondary" data-view="games">К списку партий</a></div>`;
    bindLinks(app);
    return;
  }
  state.gameData = data;
  state.gamePly = 0;
  state.gameExtras = null;
  state.gameReview = null;
  state.gameRepeats = null;
  state.gameReviewRefresh = false;
  drawGameDetail(app);
  bindGameKeyboard();
}

function drawGameDetail(app) {
  const data = state.gameData;
  const game = data.game;
  const analysis = data.analysis;
  const fens = data.fens;
  const ply = Math.max(0, Math.min(state.gamePly, fens.length - 1));
  state.gamePly = ply;
  const moves = game.moves || [];
  if (analysis) ensureGameExtras(game.id);
  const reviewState = ensureGameReview(game.id);
  const review = reviewState.status === 'ready' ? reviewState.data : null;
  const reviewMoves = review && Array.isArray(review.moves) ? review.moves : [];

  const lastMove = ply > 0 ? lastMoveSquares(fens[ply - 1], fens[ply]) : [];
  const animFrom = lastMove[0] || '';
  const animTo = lastMove[1] || '';

  const res = game.result_for_user || 'draw';
  const resBadge = res === 'win' ? 'badge-good' : res === 'loss' ? 'badge-bad' : 'badge-neutral';
  const resLabel = res === 'win' ? 'Победа' : res === 'loss' ? 'Поражение' : 'Ничья';

  const turnChar = fens[ply].split(' ')[1] === 'w' ? 'white' : 'black';
  const turnLabel = turnChar === 'white' ? 'Белые' : 'Чёрные';
  const posLabel = ply === 0 ? 'Начальная позиция' : `После хода ${ply}`;

  const moveRows = {};
  moves.forEach((san, i) => {
    const num = Math.floor(i / 2) + 1;
    if (!moveRows[num]) moveRows[num] = { num, w: null, b: null };
    moveRows[num][i % 2 === 0 ? 'w' : 'b'] = { san, ply: i };
  });

  const userIsWhite = game.user_color === 'white';
  const clsForPly = i => {
    if (!analysis) return '';
    if ((i % 2 === 0) !== userIsWhite) return '';
    const entry = analysis.moves && analysis.moves[i];
    const c = entry && entry.classification;
    if (c === 'blunder') return ' move-blunder';
    if (c === 'mistake') return ' move-mistake';
    if (c === 'inaccuracy') return ' move-inaccuracy';
    return '';
  };

  const moveClassForBadge = badge => {
    if (badge === 'blunder') return ' move-blunder';
    if (badge === 'mistake') return ' move-mistake';
    if (badge === 'inaccuracy') return ' move-inaccuracy';
    return '';
  };
  const moveCellHtml = m => {
    if (!m) return '';
    const entry = reviewMoves[m.ply];
    const badge = entry ? reviewBadge(entry.badge) : '';
    const isCurrent = ply === m.ply + 1;
    const badgeHtml = badge ? reviewBadgeIcon(badge, isCurrent ? 'pop' : '') : '';
    // Цвет клетки — по бейджу из разбора (единый источник правды), иначе по анализу.
    const cellCls = badge ? moveClassForBadge(badge) : clsForPly(m.ply);
    return `<button class="move-cell${cellCls}${isCurrent ? ' active' : ''}" data-ply="${m.ply}">${esc(m.san)}${badgeHtml}</button>`;
  };

  const moveRowsHtml = moves.length
    ? Object.values(moveRows).map(r => `
        <div class="move-row">
          <span class="move-num">${r.num}.</span>
          <span class="move-w">${moveCellHtml(r.w)}</span>
          <span class="move-b">${moveCellHtml(r.b)}</span>
        </div>`).join('')
    : '<p style="color:var(--c-text-muted)">Ходов нет.</p>';

  // Перерисовка (стрелки/слайдер) убирает textarea из DOM без blur —
  // сохраняем заметку заранее, чтобы не потерять введённый текст.
  const activeNote = document.activeElement;
  if (activeNote && activeNote.classList && activeNote.classList.contains('err-note')) {
    saveGameNote(activeNote);
  }

  app.innerHTML = `
    <div class="card-header">
      <h1 style="margin:0">Партия <code>${esc(game.id)}</code></h1>
      <span class="badge ${resBadge}">${resLabel}</span>
    </div>
    <div style="margin-bottom:16px"><a href="#" class="btn btn-secondary" data-view="games">← К списку партий</a></div>
    <div class="grid game-layout">
      <div class="card">
        ${boardHtml(fens[ply], { lastMove, animFrom, animTo, flip: game.user_color === 'black' })}
        <div class="board-meta">
          <span class="badge badge-neutral">Ходят: ${turnLabel}</span>
          <span class="badge badge-blue">${esc(posLabel)}</span>
        </div>
        ${reviewGraphHtml(review, reviewState.status, ply)}
        <div class="board-controls">
          <button class="btn btn-secondary" id="mv-first" ${ply === 0 ? 'disabled' : ''}>⏮</button>
          <button class="btn btn-secondary" id="mv-prev" ${ply === 0 ? 'disabled' : ''}>◀</button>
          <input type="range" id="mv-slider" min="0" max="${fens.length - 1}" value="${ply}">
          <button class="btn btn-secondary" id="mv-next" ${ply >= fens.length - 1 ? 'disabled' : ''}>▶</button>
          <button class="btn btn-secondary" id="mv-last" ${ply >= fens.length - 1 ? 'disabled' : ''}>⏭</button>
        </div>
        <div class="game-meta">
          <div>Соперник: <strong>${esc(game.opponent || '—')}</strong> · <strong>${game.user_color === 'white' ? 'белые' : 'чёрные'}</strong> · ${game.rated ? 'рейтинговая' : 'нерейтинговая'} · <strong>${esc(game.speed || '—')}</strong></div>
          <div>Дебют: <strong>${esc(game.opening || '—')}</strong>${game.eco ? ` <strong>${esc(game.eco)}</strong>` : ''} · ${new Date(game.created_at).toLocaleDateString('ru-RU')}</div>
        </div>
      </div>
      <div class="game-side">
        ${reviewReportCardHtml(review, reviewState.status)}
        <div class="card">
          <div class="card-header"><span class="card-title">Ходы</span></div>
          ${reviewCoachHtml(review, ply)}
          <div class="moves-list">${moveRowsHtml}</div>
          <div class="game-links">
            ${game.link ? '<a class="btn btn-secondary" href="' + safeHttp(game.link) + '" target="_blank" rel="noopener">Открыть партию</a>' : ''}
          </div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">Анализ</span></div>
          ${analysis ? gameMetricsHtml(analysis) : '<p style="color:var(--c-text-muted)">Не проанализирована.</p>'}
          <div style="margin-top:10px">
            <button type="button" class="btn btn-primary" id="analyze-game-btn">${analysis ? 'Переанализировать' : 'Проанализировать'}</button>
          </div>
          ${analysis ? '<div id="game-errors"></div>' : ''}
          <div id="game-repeats"></div>
          ${analysis ? `<div style="margin-top:10px"><button class="btn btn-primary" id="train-from-game" data-color="${esc(game.user_color || 'white')}">Тренировать этот дебют</button></div>` : ''}
        </div>
      </div>
    </div>`;

  bindLinks(app);
  bindBoard(app.querySelector('.board'));
  app.querySelectorAll('.eval-marker').forEach(marker => {
    const jump = () => {
      state.gamePly = +marker.dataset.ply;
      drawGameDetail(app);
    };
    marker.addEventListener('click', jump);
    marker.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        jump();
      }
    });
  });
  renderGameErrors(app);
  const repeatsBox = document.getElementById('game-repeats');
  if (repeatsBox && analysis) {
    const repeats = ensureGameRepeats(game.id).data;
    if (repeats.length) {
      repeatsBox.innerHTML = `
        <div class="alert alert-error" style="margin-top:10px">
          <strong>Повтор ошибки:</strong> ${repeats.length} ход(ов) уже были в прошлых партиях.
          <ul style="margin:6px 0 0 18px">
            ${repeats.map(r => `<li>${esc(r.san)} (полуход ${r.ply + 1}) — как в партии от ${new Date(r.created_at).toLocaleDateString('ru-RU')}${r.opening ? ' · ' + esc(r.opening) : ''}</li>`).join('')}
          </ul>
        </div>`;
    }
  }
  const analyzeGameBtn = document.getElementById('analyze-game-btn');
  if (analyzeGameBtn) {
    analyzeGameBtn.addEventListener('click', async () => {
      const oldText = analyzeGameBtn.textContent;
      analyzeGameBtn.disabled = true;
      analyzeGameBtn.textContent = 'Анализирую…';
      try {
        await api(
          '/api/games/' + encodeURIComponent(game.id) + '/analyze' + (analysis ? '?refresh=1' : ''),
          { method: 'POST' },
        );
        const fresh = await api('/api/games/' + encodeURIComponent(game.id));
        state.gameData = fresh;
        state.gameReview = null;
        state.gameExtras = null;
        state.gameRepeats = null;
        drawGameDetail(document.getElementById('app'));
      } catch (err) {
        showToast(err.message, 'error');
        analyzeGameBtn.disabled = false;
        analyzeGameBtn.textContent = oldText;
      }
    });
  }
  const trainFromGame = document.getElementById('train-from-game');
  if (trainFromGame) {
    trainFromGame.addEventListener('click', async () => {
      trainFromGame.disabled = true;
      try {
        const res = await api('/api/opening/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ style: 'universal', color: trainFromGame.dataset.color || 'white', human: true }),
        });
        startOpeningSession(res);
        navigate('opening');
      } catch (err) {
        if (isSessionError(err)) { handleSessionExpired(); return; }
        showToast(err.message, 'error');
        trainFromGame.disabled = false;
      }
    });
  }
  app.querySelectorAll('.move-cell').forEach(cell => {
    cell.addEventListener('click', () => {
      // data-ply — индекс хода, а gamePly — индекс позиции: показываем позицию ПОСЛЕ хода.
      state.gamePly = +cell.dataset.ply + 1;
      drawGameDetail(app);
    });
  });
  document.getElementById('mv-slider').addEventListener('input', e => {
    state.gamePly = +e.target.value;
    drawGameDetail(app);
  });
  document.getElementById('mv-first').addEventListener('click', () => { state.gamePly = 0; drawGameDetail(app); });
  document.getElementById('mv-prev').addEventListener('click', () => { state.gamePly = Math.max(0, state.gamePly - 1); drawGameDetail(app); });
  document.getElementById('mv-next').addEventListener('click', () => { state.gamePly = Math.min(fens.length - 1, state.gamePly + 1); drawGameDetail(app); });
  document.getElementById('mv-last').addEventListener('click', () => { state.gamePly = fens.length - 1; drawGameDetail(app); });
}

// --- Разбор партии (/review): бейджи, график оценки, отчёт ---

// Порядок бейджей в отчёте — как в задании. book идёт последним.
const REVIEW_BADGES = [
  'brilliant', 'great', 'best', 'excellent', 'good',
  'inaccuracy', 'mistake', 'blunder', 'missed_win', 'book',
];

const REVIEW_BADGE_LABELS = {
  book: 'Теория',
  brilliant: 'Блестящий ход',
  great: 'Отличный ход',
  best: 'Лучший ход',
  excellent: 'Очень хороший',
  good: 'Хороший ход',
  inaccuracy: 'Неточность',
  mistake: 'Ошибка',
  blunder: 'Зевок',
  missed_win: 'Упущенный выигрыш',
};

const REVIEW_BADGE_SHORT = {
  brilliant: 'Блестящие',
  great: 'Отличные',
  best: 'Лучшие',
  excellent: 'Оч. хорошие',
  good: 'Хорошие',
  inaccuracy: 'Неточности',
  mistake: 'Ошибки',
  blunder: 'Зевки',
  missed_win: 'Упущ. выигрыши',
  book: 'Теория',
};

// Цвета маркеров графика согласованы с .mb-* из style.css.
const REVIEW_BADGE_COLORS = {
  book: '#8a6f4d',
  brilliant: '#14a37f',
  great: '#2b7fd4',
  best: '#2e9e4a',
  excellent: '#3f9e57',
  good: '#639f40',
  inaccuracy: '#e09b1a',
  mistake: '#d64f00',
  blunder: '#c5221f',
  missed_win: '#b3261e',
};

const REVIEW_CATEGORIES = [
  ['opening', 'Дебют'],
  ['middlegame', 'Миттельшпиль'],
  ['endgame', 'Эндшпиль'],
  ['tactics', 'Тактика'],
  ['strategy', 'Стратегия'],
  ['positional', 'Позиционка'],
];

const REVIEW_BADGE_SET = new Set(REVIEW_BADGES);

function reviewBadge(badge) {
  return typeof badge === 'string' && REVIEW_BADGE_SET.has(badge) ? badge : '';
}

function reviewBadgeIcon(badge, extraClass) {
  const b = reviewBadge(badge);
  if (!b) return '';
  // Файл иконки — missed_win.svg, а CSS-класс — mb-missed-win.
  const cls = 'move-badge mb-' + b.replace(/_/g, '-') + (extraClass ? ' ' + extraClass : '');
  return `<span class="${cls}"><img src="assets/badges/${b}.svg" alt=""></span>`;
}

function reviewLimit(value) {
  const num = value != null && value !== '' ? Number(value) : NaN;
  return Number.isFinite(num) ? Math.max(0, Math.min(100, num)) : null;
}

// Повторы ошибок грузим один раз на партию, а не на каждую перерисовку.
function ensureGameRepeats(gameId) {
  if (state.gameRepeats && state.gameRepeats.gameId === gameId) return state.gameRepeats;
  const entry = { gameId, data: [] };
  state.gameRepeats = entry;
  api('/api/games/' + encodeURIComponent(gameId) + '/repeats').then(res => {
    if (state.gameRepeats !== entry) return;
    entry.data = (res && res.repeats) || [];
  }).catch(() => {});
  return entry;
}

// /review грузится один раз на партию; результат живёт в state.gameReview.
function ensureGameReview(gameId) {
  if (state.gameReview && state.gameReview.gameId === gameId) return state.gameReview;
  const review = { gameId, status: 'loading', data: null };
  state.gameReview = review;
  api('/api/games/' + encodeURIComponent(gameId) + '/review').then(res => {
    if (state.gameReview !== review) return;
    review.data = res && res.review ? res.review : null;
    review.status = review.data ? 'ready' : 'none';
    refreshGameReviewView(gameId);
  }).catch(() => {
    if (state.gameReview !== review) return;
    // Ошибка разбора не должна ломать экран партии: просто не рисуем блоки.
    review.status = 'error';
    refreshGameReviewView(gameId);
  });
  return review;
}

function refreshGameReviewView(gameId) {
  if (state.currentView !== 'games' || state.gameId !== gameId) return;
  if (!state.gameData || state.gameData.game.id !== gameId) return;
  if (state.gameReviewRefresh) return;
  state.gameReviewRefresh = true;
  setTimeout(() => {
    state.gameReviewRefresh = false;
    if (state.currentView !== 'games' || state.gameId !== gameId) return;
    const app = document.getElementById('app');
    if (app) drawGameDetail(app);
  }, 0);
}

function reviewGraphHtml(review, status, ply) {
  if (status === 'loading') {
    return `<div class="review-graph review-graph-loading" aria-busy="true" style="margin-top:12px">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
        <span class="card-title">График оценки</span>
        <span class="badge badge-neutral">…</span>
      </div>
      <div style="height:78px;margin-top:6px;border-radius:8px;background:var(--c-bg-hover);opacity:.55"></div>
    </div>`;
  }
  if (!review) return '';
  const moves = Array.isArray(review.moves) ? review.moves : [];
  if (!moves.length) return '';

  const W = 420;
  const H = 116;
  const padX = 14;
  const padTop = 8;
  const padBottom = 16;
  const innerW = W - padX * 2;
  const innerH = H - padTop - padBottom;

  // win_before может отсутствовать — ведём линию от предыдущего значения.
  let prev = 50;
  const points = moves.map((m, i) => {
    const raw = reviewLimit(m && m.win_before);
    // win_before в анализе — для стороны, чей ход; график — от белых.
    const value = raw == null ? null : (m && m.color === 'w' ? raw : 100 - raw);
    const v = value != null ? value : prev;
    prev = v;
    const x = moves.length === 1 ? W / 2 : padX + i * innerW / (moves.length - 1);
    const y = padTop + (1 - v / 100) * innerH;
    return { x, y, v, i, san: (m && m.san) || '', badge: reviewBadge(m && m.badge) };
  });

  const line = points.map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ');
  const bottom = (H - padBottom).toFixed(1);
  const last = points[points.length - 1];
  const area = `M${points[0].x.toFixed(1)} ${bottom} ` +
    points.map(p => `L${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ') +
    ` L${last.x.toFixed(1)} ${bottom} Z`;

  const grid = [0, 25, 50, 75, 100].map(v => {
    const y = padTop + (1 - v / 100) * innerH;
    const dash = v === 50 ? '' : ' stroke-dasharray="3 4"';
    return `<line x1="${padX}" y1="${y.toFixed(1)}" x2="${W - padX}" y2="${y.toFixed(1)}" stroke="var(--c-border)" stroke-width="0.7"${dash}></line>`;
  }).join('');
  const yLabels = [0, 50, 100].map(v => {
    const y = padTop + (1 - v / 100) * innerH;
    return `<text x="1" y="${(y + 2.5).toFixed(1)}" font-size="7" fill="var(--c-text-muted)">${v}</text>`;
  }).join('');

  const markers = points.map(p => {
    const active = ply === p.i + 1;
    const color = REVIEW_BADGE_COLORS[p.badge] || '#6d6d6d';
    const title = `${p.i + 1}. ${p.san || '—'} · win ${Math.round(p.v)}%${p.badge ? ' · ' + REVIEW_BADGE_LABELS[p.badge] : ''}`;
    const cls = 'eval-marker' + (active ? ' marker-pulse' : '');
    return `<g class="${cls}" data-ply="${p.i + 1}" role="button" tabindex="0" aria-label="${esc(title)}">` +
      `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${active ? 4.6 : 3.2}" fill="${color}" stroke="var(--c-bg-box)" stroke-width="1.2"></circle>` +
      `<title>${esc(title)}</title></g>`;
  }).join('');

  const current = ply > 0 && points[ply - 1] ? points[ply - 1] : null;
  const currentLabel = current ? `win ${Math.round(current.v)}%` : 'старт';

  return `<div class="review-graph" style="margin-top:12px">
    <div class="review-graph-head" style="display:flex;align-items:center;justify-content:space-between;gap:8px">
      <span class="card-title">График оценки</span>
      <span class="badge badge-neutral">${esc(currentLabel)}</span>
    </div>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="График оценки по полуходам" style="display:block;width:100%;height:auto;margin-top:6px">
      ${grid}
      <path d="${area}" fill="var(--c-link)" opacity="0.12"></path>
      <path d="${line}" fill="none" stroke="var(--c-link)" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"></path>
      ${markers}
      ${yLabels}
    </svg>
    <div style="font-size:11px;color:var(--c-text-muted);margin-top:2px">Клик по точке — позиция после хода; цвет точки — бейдж хода.</div>
  </div>`;
}

function reviewCoachHtml(review, ply) {
  if (!review) return '';
  const moves = Array.isArray(review.moves) ? review.moves : [];
  let badge = '';
  let label = 'Начальная позиция';
  let comment = 'Выбери ход в списке, стрелками или точкой на графике — здесь появится комментарий тренера.';
  let score = 'старт';

  if (ply > 0) {
    const entry = moves[ply - 1];
    if (!entry) return '';
    badge = reviewBadge(entry.badge);
    label = entry.label || REVIEW_BADGE_LABELS[badge] || 'Ход';
    comment = entry.comment || '';
    const win = reviewLimit(entry.win_before);
    const whiteWin = win == null ? null : (entry.color === 'w' ? win : 100 - win);
    score = whiteWin != null ? `win белых ${Math.round(whiteWin)}%` : '';
  }

  return `<div class="coach-bubble enter" style="margin:10px 0 12px;padding:10px 12px;border:1px solid var(--c-border);border-radius:10px;background:var(--c-bg-box-alt,var(--c-bg-hover))">
    <div class="coach-bubble-head" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      ${badge ? reviewBadgeIcon(badge) : ''}
      <strong class="coach-bubble-label">${esc(label)}</strong>
      <span class="coach-bubble-score" style="margin-left:auto;font-size:12px;color:var(--c-text-muted)">${esc(score)}</span>
    </div>
    <div class="coach-bubble-text" style="margin-top:6px;font-size:13px;line-height:1.45">${esc(comment)}</div>
  </div>`;
}

function reviewReportCardHtml(review, status) {
  if (status === 'loading') {
    return `<div class="card review-report review-report-loading" aria-busy="true">
      <div class="card-header"><span class="card-title">Отчёт</span><span class="badge badge-neutral">…</span></div>
      <p style="color:var(--c-text-muted);margin:0">Загрузка разбора…</p>
    </div>`;
  }
  if (!review) return '';

  const accuracy = review.accuracy || {};
  const moves = Array.isArray(review.moves) ? review.moves : [];
  const counts = {};
  moves.forEach(m => {
    const b = reviewBadge(m && m.badge);
    if (b) counts[b] = (counts[b] || 0) + 1;
  });

  const badgeCells = REVIEW_BADGES.map(b => {
    const count = counts[b] || 0;
    const title = REVIEW_BADGE_LABELS[b] + ': ' + count;
    return `<div class="review-badge-stat${count ? '' : ' is-empty'}" title="${esc(title)}" style="display:flex;align-items:center;gap:4px;font-size:12px;${count ? '' : 'opacity:.55'}">
      ${reviewBadgeIcon(b)}<span>${count}</span><span style="color:var(--c-text-muted)">${esc(REVIEW_BADGE_SHORT[b])}</span>
    </div>`;
  }).join('');

  const categories = review.categories || {};
  const catRows = REVIEW_CATEGORIES.map(([key, label]) => {
    const raw = categories[key];
    const has = typeof raw === 'number' && Number.isFinite(raw);
    const value = has ? Math.max(0, Math.min(100, raw)) : 0;
    return `<div class="review-cat" style="margin-top:8px">
      <div style="display:flex;justify-content:space-between;gap:8px;font-size:12px"><span>${label}</span><span>${has ? Math.round(value) : '—'}</span></div>
      <div class="review-cat-bar" style="height:6px;margin-top:3px;border-radius:3px;overflow:hidden;background:var(--c-border)" title="${has ? '' : 'нет ходов в этой фазе'}">
        <div style="width:${has ? value.toFixed(1) : 0}%;height:100%;border-radius:3px;background:var(--c-btn-primary)"></div>
      </div>
    </div>`;
  }).join('');

  const rating = review.rating_estimate != null && Number.isFinite(Number(review.rating_estimate))
    ? String(Math.round(Number(review.rating_estimate)))
    : '—';
  const summary = review.summary
    ? `<p class="review-summary" style="font-size:13px;line-height:1.5;margin:10px 0 0">${esc(review.summary)}</p>`
    : '';

  return `<div class="card review-report">
    <div class="card-header"><span class="card-title">Отчёт</span><span class="badge badge-blue">разбор</span></div>
    <div style="display:flex;gap:8px;margin-top:4px">
      <div class="stat" style="flex:1 1 0"><div class="stat-value">${accuracy.user != null ? fmt(accuracy.user, 1) + '%' : '—'}</div><div class="stat-label">Точность игрока</div></div>
      <div class="stat" style="flex:1 1 0"><div class="stat-value">${accuracy.opponent != null ? fmt(accuracy.opponent, 1) + '%' : '—'}</div><div class="stat-label">Точность соперника</div></div>
    </div>
    <div style="display:flex;align-items:baseline;gap:8px;margin-top:8px">
      <span style="font-size:24px;font-weight:700;line-height:1.1">${esc(rating)}</span>
      <span style="font-size:12px;color:var(--c-text-muted)">оценка по точности</span>
    </div>
    <div class="review-badge-grid" style="display:flex;flex-wrap:wrap;gap:6px 12px;margin-top:10px">${badgeCells}</div>
    <div class="review-categories" style="margin-top:12px">${catRows}</div>
    ${summary}
  </div>`;
}

function gameMetricsHtml(analysis) {
  const blunders = (analysis.blunders || []).length;
  const mistakes = (analysis.mistakes || []).length;
  const inaccuracies = (analysis.inaccuracies || []).length;
  const missed = (analysis.missed_wins || []).length;
  const avgLoss = fmt(analysis.avg_win_loss);
  const avgBefore = fmt(analysis.avg_win_before);
  return `
    <div class="grid grid-3">
      <div class="stat"><div class="stat-value" style="color:var(--c-err-blunder)">${blunders}</div><div class="stat-label">Зевки</div></div>
      <div class="stat"><div class="stat-value" style="color:var(--c-err-mistake)">${mistakes}</div><div class="stat-label">Ошибки</div></div>
      <div class="stat"><div class="stat-value" style="color:var(--c-err-inaccuracy)">${inaccuracies}</div><div class="stat-label">Неточности</div></div>
      <div class="stat"><div class="stat-value">${missed}</div><div class="stat-label">Упущ. выигрыши</div></div>
      <div class="stat"><div class="stat-value">${avgLoss}</div><div class="stat-label" title="Средняя потеря win% (процентов) на ход игрока (метрика avg_win_loss из анализа)">Ср. потеря win%</div></div>
      <div class="stat"><div class="stat-value">${avgBefore}</div><div class="stat-label" title="Средний win% перед ходом игрока (метрика avg_win_before)">Win% до хода</div></div>
    </div>`;
}

// --- Ошибки партии: причина (/causes) и «твоя идея» (/notes) ---

// Кэш ответов /causes и /notes на текущую партию. drawGameDetail()
// перерисовывает весь экран на каждый полуход, поэтому данные держим
// в state и не дёргаем API повторно.
function ensureGameExtras(gameId) {
  if (state.gameExtras && state.gameExtras.gameId === gameId) return state.gameExtras;
  const extras = {
    gameId,
    causes: {},
    notes: {},
    drafts: {},
    saving: {},
    causesLoading: true,
    causesError: false,
  };
  state.gameExtras = extras;
  loadGameExtras(extras);
  return extras;
}

function loadGameExtras(extras) {
  const base = '/api/games/' + encodeURIComponent(extras.gameId);
  api(base + '/causes').then(data => {
    if (state.gameExtras !== extras) return;
    extras.causes = (data && data.causes) || {};
    extras.causesLoading = false;
    renderGameErrors(document.getElementById('app'));
  }).catch(() => {
    if (state.gameExtras !== extras) return;
    extras.causesLoading = false;
    extras.causesError = true;
    renderGameErrors(document.getElementById('app'));
  });
  api(base + '/notes').then(data => {
    if (state.gameExtras !== extras) return;
    extras.notes = (data && data.notes) || {};
    renderGameErrors(document.getElementById('app'));
  }).catch(() => {
    if (state.gameExtras !== extras) return;
    // Блок ошибок остаётся на месте: заметки просто будут пустыми.
    renderGameErrors(document.getElementById('app'));
  });
}

// Ошибки игрока: зевки и ошибки, до 10 штук. Ключ — индекс полухода
// в analysis.moves (0-based), как в /causes и /notes.
function gameErrorEntries(analysis, game) {
  const moves = (analysis && analysis.moves) || [];
  const userColor = game && game.user_color;
  const out = [];
  moves.forEach((m, i) => {
    if (!m) return;
    const c = m.classification;
    if (c !== 'blunder' && c !== 'mistake') return;
    if (userColor && ((i % 2 === 0) !== (userColor === 'white'))) return;
    out.push({ ply: i, san: m.san, drop: m.drop, classification: c });
  });
  return out;
}

function gameErrorsInnerHtml(analysis, game, extras) {
  const errors = gameErrorEntries(analysis, game);
  if (!errors.length) {
    return `<div class="card-header" style="margin-top:12px"><span class="card-title">Ошибки: причина и твоя идея</span></div>
      <p style="color:var(--c-text-muted)">Зевков и ошибок не найдено.</p>`;
  }
  const shown = errors.slice(0, 10);
  const rows = shown.map(e => {
    const cause = extras && extras.causes ? extras.causes[e.ply] : null;
    let causeHtml;
    if (extras && extras.causesError) {
      causeHtml = '<span class="badge badge-neutral" title="Не удалось загрузить причину">—</span>';
    } else if (!cause) {
      const pending = !extras || extras.causesLoading;
      causeHtml = `<span class="badge badge-neutral" title="${pending ? 'Причина загружается' : 'Причина не определена'}">${pending ? '…' : '—'}</span>`;
    } else {
      const label = cause.label || cause.cause || '—';
      const explain = cause.explanation ? ` title="${esc(cause.explanation)}"` : '';
      causeHtml = `<span class="badge badge-neutral"${explain}>${esc(label)}</span>`;
    }
    const explanation = cause && cause.explanation
      ? `<div style="color:var(--c-text-muted);font-size:12px">${esc(cause.explanation)}</div>`
      : '';
    const saved = extras && extras.notes ? (extras.notes[e.ply] || '') : '';
    const draft = extras && extras.drafts ? extras.drafts[e.ply] : undefined;
    const value = draft !== undefined ? draft : saved;
    const status = value === saved && saved ? 'сохранено' : '';
    const clsBadge = e.classification === 'blunder'
      ? '<span class="badge badge-bad" style="color:var(--c-err-blunder)">Зевок</span>'
      : '<span class="badge badge-neutral" style="color:var(--c-err-mistake)">Ошибка</span>';
    const loss = e.drop != null
      ? `${e.drop >= 0 ? '−' : '+'}${fmt(Math.abs(e.drop))} win%`
      : '—';
    return `
      <div class="game-error" data-ply="${e.ply}" style="padding:10px 0;border-top:1px solid var(--c-border);display:flex;flex-direction:column;gap:6px">
        <div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px">
          <button type="button" class="btn btn-secondary err-jump" data-ply="${e.ply}" style="padding:2px 8px;font-size:13px">${esc(e.san || '—')}</button>
          ${clsBadge}
          <span style="color:var(--c-text-muted);font-size:13px">${loss}</span>
          ${causeHtml}
        </div>
        ${explanation}
        <div class="form-group" style="margin:0">
          <textarea class="err-note" data-ply="${e.ply}" rows="2" maxlength="500" placeholder="Твоя идея: что ты хотел сделать этим ходом?">${esc(value)}</textarea>
          <div class="err-save-status" style="font-size:12px;color:var(--c-text-muted);min-height:14px">${esc(status)}</div>
        </div>
      </div>`;
  }).join('');
  const more = errors.length > shown.length
    ? `<p style="color:var(--c-text-muted);font-size:12px">Показаны первые ${shown.length} из ${errors.length}.</p>`
    : '';
  return `<div class="card-header" style="margin-top:12px"><span class="card-title">Ошибки: причина и твоя идея</span></div>
    <div class="game-errors-list">${rows}</div>${more}`;
}

function renderGameErrors(root) {
  if (!root) return;
  const box = root.querySelector('#game-errors');
  if (!box) return;
  const data = state.gameData;
  if (!data || !data.analysis) return;
  const extras = state.gameExtras && state.gameExtras.gameId === data.game.id
    ? state.gameExtras
    : null;
  // Перерисовка приходит после ответа /causes или /notes — не теряем
  // фокус и позицию курсора в textarea, которую пользователь заполняет.
  const active = document.activeElement;
  const focus = active && box.contains(active) && active.classList.contains('err-note')
    ? { ply: active.dataset.ply, start: active.selectionStart, end: active.selectionEnd }
    : null;
  box.innerHTML = gameErrorsInnerHtml(data.analysis, data.game, extras);
  bindGameErrors(box);
  if (focus) {
    const ta = box.querySelector('.err-note[data-ply="' + focus.ply + '"]');
    if (ta) {
      ta.focus();
      try { ta.setSelectionRange(focus.start, focus.end); } catch { /* не критично */ }
    }
  }
}

function bindGameErrors(box) {
  box.querySelectorAll('.err-note').forEach(ta => {
    ta.addEventListener('input', () => {
      if (state.gameExtras) state.gameExtras.drafts[ta.dataset.ply] = ta.value;
    });
    ta.addEventListener('blur', () => { saveGameNote(ta); });
    ta.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        saveGameNote(ta);
      }
    });
  });
  box.querySelectorAll('.err-jump').forEach(btn => {
    btn.addEventListener('click', () => {
      state.gamePly = +btn.dataset.ply + 1;
      drawGameDetail(document.getElementById('app'));
    });
  });
}

function setNoteStatus(ta, text, isError) {
  if (!ta || !ta.isConnected) return;
  const el = ta.parentNode && ta.parentNode.querySelector('.err-save-status');
  if (!el) return;
  el.textContent = text;
  el.style.color = isError ? 'var(--c-err-blunder)' : 'var(--c-text-muted)';
}

async function saveGameNote(ta) {
  const extras = state.gameExtras;
  if (!extras || !ta || !ta.isConnected) return;
  const ply = +ta.dataset.ply;
  const intent = ta.value.trim();
  const saved = extras.notes[ply] || '';
  if (intent === saved) {
    setNoteStatus(ta, saved ? 'сохранено' : '');
    return;
  }
  if (extras.saving[ply]) return;
  extras.saving[ply] = true;
  setNoteStatus(ta, 'сохранение…');
  try {
    await api('/api/games/' + encodeURIComponent(extras.gameId) + '/notes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ply, intent }),
    });
    if (state.gameExtras !== extras) return;
    extras.notes[ply] = intent;
    extras.drafts[ply] = intent;
    if (ta.isConnected) {
      setNoteStatus(ta, 'сохранено');
    } else {
      // Экран успел перерисоваться, пока шёл запрос.
      const cur = document.querySelector('#game-errors .err-note[data-ply="' + ply + '"]');
      if (cur && cur.value.trim() === intent) setNoteStatus(cur, 'сохранено');
    }
  } catch (err) {
    if (isSessionError(err)) { handleSessionExpired(); return; }
    setNoteStatus(ta, 'не сохранено', true);
    showToast(err.message, 'error');
  } finally {
    extras.saving[ply] = false;
  }
}


function bindGameKeyboard() {
  clearGameKeyboard();
  const handler = e => {
    const fens = state.gameData && state.gameData.fens;
    if (!fens) return;
    if (e.key === 'ArrowLeft') {
      e.preventDefault();
      state.gamePly = Math.max(0, state.gamePly - 1);
      drawGameDetail(document.getElementById('app'));
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      state.gamePly = Math.min(fens.length - 1, state.gamePly + 1);
      drawGameDetail(document.getElementById('app'));
    }
  };
  state.keyHandler = handler;
  document.addEventListener('keydown', handler);
}

function clearGameKeyboard() {
  if (state.keyHandler) {
    document.removeEventListener('keydown', state.keyHandler);
    state.keyHandler = null;
  }
}

async function viewDrills(app) {
  if (!state.user) {
    app.innerHTML = '<div class="onboarding card"><div class="empty-icon">🎯</div><h1>Нет ошибок</h1><p>Профиль не выбран.</p></div>';
    return;
  }
  const params = new URLSearchParams({ min_drop: 15, min_win: 50 });
  const dr = await api('/api/drills?' + params);

  const summary = dr.summary;
  const items = dr.items;

  app.innerHTML = `
    <div class="card-header"><h1>Ваши ошибки</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="card">
      <form id="drills-filter" class="form-row" style="gap:16px;align-items:flex-end">
        <div class="form-group"><label>Мин. потеря win% <input type="number" name="min_drop" step="0.1" value="${dr.min_drop}"></label></div>
        <div class="form-group"><label>Мин. win% до хода <input type="number" name="min_win" step="0.1" value="${dr.min_win}"></label></div>
        <div class="form-group"><label>Вердикт <select name="verdict"><option value="">все</option><option value="unnatural">только неестественные</option><option value="natural,borderline">естественные + пограничные</option></select></label></div>
        <button class="btn btn-secondary" style="height:38px">Применить</button>
      </form>
    </div>
    ${summary ? `<div class="grid grid-3"><div class="card stat"><div class="stat-value">${summary.found}</div><div class="stat-label">Ошибок найдено</div></div><div class="card stat"><div class="stat-value">${summary.games}</div><div class="stat-label">Партий</div></div><div class="card stat"><div class="stat-value">${summary.avg_drop != null ? fmt(summary.avg_drop) + '%' : '—'}</div><div class="stat-label" title="Средняя потеря win% (процентов) на ход игрока (метрика avg_win_loss из анализа)">Ср. потеря win%</div></div></div>` : ''}
    <div class="card">
      <div class="card-header"><span class="card-title">Список ошибок (${items.length})</span><div><a href="/api/drills.pgn?${params}" class="btn btn-secondary" style="font-size:12px;padding:4px 10px">PGN</a> <a href="/api/drills.json?${params}" class="btn btn-secondary" style="font-size:12px;padding:4px 10px;margin-left:4px">JSON</a></div></div>
      ${items.length ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>Партия</th><th>Ход</th><th>Сыграно</th><th>Лучше</th><th>Класс</th><th>Потеря</th><th>Win% до/после</th><th>Дебют</th><th>Результат</th></tr></thead><tbody>${items.map((d, i) => `<tr class="drill-row"><td class="num">${i + 1}</td><td>${d.link ? '<a href="' + safeHttp(d.link) + '" target="_blank" rel="noopener" title="Открыть партию">' + esc(d.game_id.slice(0, 8)) + '</a>' : esc(d.game_id.slice(0, 8))}</td><td class="num">${d.ply}</td><td class="drill-san">${esc(d.san_played)}</td><td class="drill-best">${esc(d.best_move_san)}</td><td><span class="badge ${d.classification === 'blunder' ? 'badge-bad' : 'badge-neutral'}">${esc(d.classification)}</span></td><td class="num">${fmt(d.drop)}%</td><td class="num">${fmt(d.win_before)}% → ${fmt(d.win_after)}%</td><td>${esc(d.opening)}</td><td class="center"><span class="badge ${d.result_for_user === 'win' ? 'badge-good' : d.result_for_user === 'loss' ? 'badge-bad' : 'badge-neutral'}">${esc(d.result_for_user)}</span></td></tr>`).join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted);padding:16px">Ошибок не найдено.</p>'}
    </div>`;

  document.getElementById('drills-filter').addEventListener('submit', e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const p = new URLSearchParams();
    if (fd.get('min_drop')) p.set('min_drop', fd.get('min_drop'));
    if (fd.get('min_win')) p.set('min_win', fd.get('min_win'));
    if (fd.get('verdict')) p.set('verdict', fd.get('verdict'));
    viewDrillsWithParams(app, p).catch(err => {
      if (isSessionError(err)) {
        handleSessionExpired();
        return;
      }
      app.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
    });
  });
}

async function viewDrillsWithParams(app, params) {
  const dr = await api('/api/drills?' + params);
  // re-render just the table part — simpler to re-call viewDrills with custom params
  const summary = dr.summary;
  const items = dr.items;
  app.innerHTML = `
    <div class="card-header"><h1>Ваши ошибки</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="card">
      <form id="drills-filter" class="form-row" style="gap:16px;align-items:flex-end">
        <div class="form-group"><label>Мин. потеря win% <input type="number" name="min_drop" step="0.1" value="${dr.min_drop}"></label></div>
        <div class="form-group"><label>Мин. win% до хода <input type="number" name="min_win" step="0.1" value="${dr.min_win}"></label></div>
        <div class="form-group"><label>Вердикт <select name="verdict"><option value="">все</option><option value="unnatural">только неестественные</option><option value="natural,borderline">естественные + пограничные</option></select></label></div>
        <button class="btn btn-secondary" style="height:38px">Применить</button>
      </form>
    </div>
    ${summary ? `<div class="grid grid-3"><div class="card stat"><div class="stat-value">${summary.found}</div><div class="stat-label">Ошибок найдено</div></div><div class="card stat"><div class="stat-value">${summary.games}</div><div class="stat-label">Партий</div></div><div class="card stat"><div class="stat-value">${summary.avg_drop != null ? fmt(summary.avg_drop) + '%' : '—'}</div><div class="stat-label" title="Средняя потеря win% (процентов) на ход игрока (метрика avg_win_loss из анализа)">Ср. потеря win%</div></div></div>` : ''}
    <div class="card">
      <div class="card-header"><span class="card-title">Список ошибок (${items.length})</span><div><a href="/api/drills.pgn?${params}" class="btn btn-secondary" style="font-size:12px;padding:4px 10px">PGN</a> <a href="/api/drills.json?${params}" class="btn btn-secondary" style="font-size:12px;padding:4px 10px;margin-left:4px">JSON</a></div></div>
      ${items.length ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>Партия</th><th>Ход</th><th>Сыграно</th><th>Лучше</th><th>Класс</th><th>Потеря</th><th>Win% до/после</th><th>Дебют</th><th>Результат</th></tr></thead><tbody>${items.map((d, i) => `<tr class="drill-row"><td class="num">${i + 1}</td><td>${d.link ? '<a href="' + safeHttp(d.link) + '" target="_blank" rel="noopener" title="Открыть партию">' + esc(d.game_id.slice(0, 8)) + '</a>' : esc(d.game_id.slice(0, 8))}</td><td class="num">${d.ply}</td><td class="drill-san">${esc(d.san_played)}</td><td class="drill-best">${esc(d.best_move_san)}</td><td><span class="badge ${d.classification === 'blunder' ? 'badge-bad' : 'badge-neutral'}">${esc(d.classification)}</span></td><td class="num">${fmt(d.drop)}%</td><td class="num">${fmt(d.win_before)}% → ${fmt(d.win_after)}%</td><td>${esc(d.opening)}</td><td class="center"><span class="badge ${d.result_for_user === 'win' ? 'badge-good' : d.result_for_user === 'loss' ? 'badge-bad' : 'badge-neutral'}">${esc(d.result_for_user)}</span></td></tr>`).join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted);padding:16px">Ошибок не найдено.</p>'}
    </div>`;
  document.getElementById('drills-filter').addEventListener('submit', e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const p = new URLSearchParams();
    if (fd.get('min_drop')) p.set('min_drop', fd.get('min_drop'));
    if (fd.get('min_win')) p.set('min_win', fd.get('min_win'));
    if (fd.get('verdict')) p.set('verdict', fd.get('verdict'));
    viewDrillsWithParams(app, p).catch(err => {
      if (isSessionError(err)) {
        handleSessionExpired();
        return;
      }
      app.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
    });
  });
}

// --- Задача дня ---

async function viewTask(app) {
  if (!state.user) {
    app.innerHTML = '<div class="onboarding card"><div class="empty-icon">♟</div><h1>Задача дня</h1><p>Профиль не выбран — выбери его на странице Профили.</p><a href="#" class="btn btn-primary" data-view="users">Профили</a></div>';
    bindLinks(app);
    return;
  }

  let task;
  try {
    task = await api('/api/task/today');
  } catch (err) {
    app.innerHTML = `
      <div class="card-header"><h1>Задача дня</h1></div>
      <div class="onboarding card"><div class="empty-icon">♟</div><h1>Задач нет</h1><p>${esc(err.message)}</p><a href="#" class="btn btn-primary" data-view="coach">Запустить анализ</a></div>`;
    bindLinks(app);
    return;
  }

  if (state.taskKey !== task.task_key) {
    state.taskKey = task.task_key;
    state.taskResult = null;
    state.taskAnswerText = '';
    state.taskError = null;
    state.taskExplanation = task.explanation || null;
    state.taskExplaining = false;
    state.taskJobId = null;
    state.taskRevealed = !!task.done;
    state.taskLine = null;
    if (state.taskPlayTimer) { clearInterval(state.taskPlayTimer); state.taskPlayTimer = null; }
    resetTaskExplore();
  } else if (!state.taskExplanation && task.explanation) {
    state.taskExplanation = task.explanation;
  }
  state.task = task;
  drawTask(app);
  // вернулись на страницу, пока шло объяснение — возобновляем поллинг джобы
  if (state.taskExplaining && !state.timers.task && state.taskJobId) {
    taskPollExplanation(app, state.taskJobId);
  }
}

function taskExplainControlsHtml() {
  if (state.taskExplanation) return '';
  if (state.taskExplaining) {
    return '<p style="color:var(--c-text-muted);font-size:13px;margin:0">Объясняю…</p>';
  }
  return '<button type="button" class="btn btn-secondary" id="task-explain">Объяснить</button>';
}

function taskSolutionHtml(task) {
  const drop = typeof task.drop === 'number' ? task.drop : null;
  const dropBadge = drop != null
    ? `<span class="badge ${drop >= 20 ? 'badge-bad' : 'badge-neutral'}">потеря ${drop >= 0 ? '−' : '+'}${Math.abs(drop).toFixed(1)}%</span>`
    : '';
  const before = fmt(task.win_before);
  const after = fmt(task.win_after);
  return `
    <div class="card">
      <div class="card-header"><span class="card-title">Решение</span>${dropBadge}</div>
      <div>Твой ход: <span class="drill-san" style="color:var(--c-err-blunder)">${esc(task.san_played)}</span></div>
      <div>Лучший: <span class="drill-best">${esc(task.best_san)}</span></div>
      <div style="color:var(--c-text-muted);font-size:13px;margin-top:6px">Win%: ${before} → ${after}</div>
    </div>`;
}

function uciToFromTo(uci) {
  const s = String(uci || '');
  return /^[a-h][1-8][a-h][1-8]/.test(s) ? [s.slice(0, 2), s.slice(2, 4)] : null;
}

function taskLineControlsHtml(lineFens, lineIdx) {
  const total = Math.max(0, lineFens.length - 1);
  const atStart = lineIdx == null;
  const value = atStart ? 0 : lineIdx;
  return `
    <div class="task-line-controls">
      <button type="button" class="btn btn-secondary" id="task-line-played" ${atStart ? 'disabled' : ''} title="Показать сыгранный ход (красный крестик)">Твой ход</button>
      <button type="button" class="btn btn-secondary" id="task-line-best" ${total < 1 ? 'disabled' : ''} title="Показать лучший ход (зелёная галочка)">Лучший ход</button>
      <button type="button" class="btn btn-secondary" id="task-line-play" ${total < 1 ? 'disabled' : ''}>▶ Показать линию</button>
      <button type="button" class="btn btn-secondary" id="task-line-explore" title="Ходить любыми фигурами и смотреть оценку Stockfish">Свободный разбор</button>
      <span class="task-line-pos">${atStart ? 'до хода' : `${Math.min(value, total)} / ${total}`}</span>
      <div class="task-line-nav">
        <button type="button" class="btn btn-secondary" id="task-line-prev" ${atStart || value <= 0 ? 'disabled' : ''}>◀</button>
        <input type="range" id="task-line-range" min="0" max="${Math.max(0, total)}" value="${Math.min(value, total)}" ${total < 1 ? 'disabled' : ''}>
        <button type="button" class="btn btn-secondary" id="task-line-next" ${atStart || value >= total ? 'disabled' : ''}>▶</button>
      </div>
    </div>`;
}

function taskEvalBarsHtml(task) {
  const before = Number(task.win_before);
  const after = Number(task.win_after);
  if (!Number.isFinite(before) || !Number.isFinite(after)) return '';
  const white = v => (task.user_color === 'white' ? v : 100 - v);
  const items = [
    ['до', white(before)],
    ['после твоего хода', white(after)],
  ];
  return `<div class="eval-bars" aria-label="Оценка Stockfish до и после хода">
    ${items.map(([label, v]) => {
      const pct = Math.max(0, Math.min(100, v));
      return `<div class="eval-bar-item">
        <div class="eval-bar" title="${esc(label)}: win белых ${Math.round(pct)}%">
          <div class="eval-bar-white" style="height:${pct.toFixed(1)}%"></div>
        </div>
        <span>${esc(label)}</span>
      </div>`;
    }).join('')}
  </div>`;
}

function drawTask(app) {
  const task = state.task;
  if (!task) {
    app.innerHTML = '<div class="card"><p>Загрузка...</p></div>';
    return;
  }

  const answered = !!state.taskResult || !!task.done;
  const lineFens = [task.fen, ...(Array.isArray(task.best_line_fens) ? task.best_line_fens : [])];
  const lineIdx = typeof state.taskLine === 'number' ? state.taskLine : null;
  const inLine = lineIdx != null && lineIdx >= 0 && lineIdx < lineFens.length;
  const explore = !!state.taskExplore && answered;
  const bestMove = uciToFromTo(task.best_uci);
  const playedMove = uciToFromTo(task.played_uci);
  const currentFen = explore && state.taskExploreFen
    ? state.taskExploreFen
    : (inLine ? lineFens[lineIdx] : task.fen);
  const exploreLast = explore && Array.isArray(state.taskExploreLast) ? state.taskExploreLast : [];
  const lineLast = explore
    ? exploreLast
    : (inLine && lineIdx > 0 ? lastMoveSquares(lineFens[lineIdx - 1], lineFens[lineIdx]) : []);
  const arrows = [];
  const marks = [];
  if (!explore) {
    if (answered && !inLine) {
      if (bestMove) {
        arrows.push({ from: bestMove[0], to: bestMove[1], color: 'good' });
        marks.push({ square: bestMove[1], kind: 'good' });
      }
      if (playedMove && !(bestMove && playedMove[0] === bestMove[0] && playedMove[1] === bestMove[1])) {
        arrows.push({ from: playedMove[0], to: playedMove[1], color: 'bad' });
        marks.push({ square: playedMove[1], kind: 'bad' });
      }
    } else if (inLine && lineLast.length >= 2) {
      arrows.push({ from: lineLast[0], to: lineLast[1], color: 'good' });
      marks.push({ square: lineLast[1], kind: 'good' });
    }
  }
  const colorLabel = task.user_color === 'white' ? 'белые' : 'чёрные';
  const clsBadge = answered && task.classification
    ? `<span class="badge ${task.classification === 'blunder' ? 'badge-bad' : 'badge-neutral'}">${esc(task.classification)}</span>`
    : '';

  let panel = '';
  if (state.taskResult) {
    const ok = state.taskResult.correct;
    panel = `
      <div class="card">
        <div class="card-header"><span class="card-title">Ответ</span>
          <span class="badge ${ok ? 'badge-good' : 'badge-bad'}">${ok ? 'верно ✓' : 'неверно'}</span></div>
        ${taskExplainControlsHtml()}
      </div>`;
  } else if (task.done) {
    panel = `
      <div class="card">
        <div class="card-header"><span class="card-title">Ответ</span>
          <span class="badge badge-good">решено сегодня ✓</span></div>
        ${taskExplainControlsHtml()}
      </div>`;
  } else {
    panel = `
      <div class="card">
        <div class="card-header"><span class="card-title">Твой ход</span></div>
        <form id="task-answer-form" class="form-row" style="gap:8px;align-items:flex-end">
          <div class="form-group" style="flex:1;min-width:160px;margin-bottom:0">
            <label for="task-answer">Ход (SAN)</label>
            <input type="text" id="task-answer" name="answer" placeholder="например, Nf3"
                   value="${esc(state.taskAnswerText)}" autocomplete="off" spellcheck="false" required>
          </div>
          <button class="btn btn-primary" style="height:38px">Ответить</button>
        </form>
        <div style="margin-top:8px"><a href="#" id="task-show-solution" class="btn btn-link">Показать решение</a></div>
      </div>`;
  }

  const explanationHtml = state.taskExplanation
    ? `<div class="card"><div class="card-header"><span class="card-title">Почему так</span></div><div class="mentor-reply">${esc(state.taskExplanation)}</div></div>`
    : '';

  const revealedNoAnswer = state.taskRevealed && !state.taskResult && !task.done;
  const revealedExplainHtml = (revealedNoAnswer && !state.taskExplanation)
    ? `<div class="card"><div class="card-header"><span class="card-title">Объяснение LLM</span></div>${taskExplainControlsHtml()}</div>`
    : '';

  app.innerHTML = `
    <div class="card-header"><h1>Задача дня</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="grid game-layout">
      <div class="card">
        ${boardHtml(currentFen, {
          flip: task.user_color === 'black',
          lastMove: lineLast,
          animFrom: lineLast[0],
          animTo: lineLast[1],
          arrows,
          marks,
        })}
        <div class="board-meta">
          <span class="badge badge-blue">${esc(task.date)}</span>
          <span class="badge badge-neutral">${esc(task.opening || '—')}${task.eco ? ` ${esc(task.eco)}` : ''}</span>
          <span class="badge badge-neutral">ты: ${colorLabel}</span>
          ${explore ? '<span class="badge badge-blue">свободный разбор</span>' : ''}
          ${clsBadge}
        </div>
        ${answered ? '' : '<div id="task-board-hint" style="color:var(--c-text-muted);font-size:13px;margin-top:8px">кликни фигуру, затем клетку — или введи ход текстом</div>'}
        ${explore ? '<div class="task-explore-hint">Ходи любой фигурой — оценка Stockfish обновится после хода.</div>' : ''}
        ${answered ? (explore ? taskExplorePanelHtml() : taskLineControlsHtml(lineFens, lineIdx)) : ''}
        ${answered && !explore ? taskEvalBarsHtml(task) : ''}
      </div>
      <div class="game-side">
        ${state.taskError ? `<div class="alert alert-error">${esc(state.taskError)}</div>` : ''}
        ${panel}
        ${state.taskRevealed ? taskSolutionHtml(task) : ''}
        ${revealedExplainHtml}
        ${explanationHtml}
      </div>
    </div>`;

  bindLinks(app);
  const form = document.getElementById('task-answer-form');
  if (form) {
    form.addEventListener('submit', onTaskAnswer);
    const input = document.getElementById('task-answer');
    input.addEventListener('input', () => { state.taskAnswerText = input.value; });
    if (matchMedia('(pointer:fine)').matches) input.focus();
  }
  const showSolution = document.getElementById('task-show-solution');
  if (showSolution) {
    showSolution.addEventListener('click', e => {
      e.preventDefault();
      state.taskRevealed = true;
      drawTask(app);
    });
  }
  const explainBtn = document.getElementById('task-explain');
  if (explainBtn) explainBtn.addEventListener('click', () => taskExplain(app));
  bindBoard(app.querySelector('.board'));

  // Свободный разбор: вход, выход и доска с любыми легальными ходами.
  const exploreBtn = document.getElementById('task-line-explore');
  if (exploreBtn) exploreBtn.addEventListener('click', () => startTaskExplore(app));
  const exploreBack = document.getElementById('task-explore-back');
  if (exploreBack) {
    exploreBack.addEventListener('click', () => {
      resetTaskExplore();
      drawTask(app);
    });
  }
  if (explore) {
    const exploreBoard = app.querySelector('.board');
    if (exploreBoard) {
      bindBoard(exploreBoard, {
        moves: Array.isArray(state.taskExploreMoves) ? state.taskExploreMoves : [],
        onMove: move => playTaskExploreMove(app, move),
      });
    }
  }

  // Просмотр решения: позиция до хода, лучший ход, вся линия, шаги.
  const totalLine = Math.max(0, lineFens.length - 1);
  const setLine = value => {
    if (state.taskPlayTimer) { clearInterval(state.taskPlayTimer); state.taskPlayTimer = null; }
    state.taskLine = value == null ? null : Math.max(0, Math.min(totalLine, value));
    drawTask(app);
  };
  const linePlayed = document.getElementById('task-line-played');
  if (linePlayed) linePlayed.addEventListener('click', () => setLine(null));
  const lineBest = document.getElementById('task-line-best');
  if (lineBest) lineBest.addEventListener('click', () => setLine(0));
  const linePrev = document.getElementById('task-line-prev');
  if (linePrev) linePrev.addEventListener('click', () => setLine((lineIdx == null ? 0 : lineIdx) - 1));
  const lineNext = document.getElementById('task-line-next');
  if (lineNext) lineNext.addEventListener('click', () => setLine((lineIdx == null ? -1 : lineIdx) + 1));
  const lineRange = document.getElementById('task-line-range');
  if (lineRange) lineRange.addEventListener('input', e => setLine(Number(e.target.value)));
  const linePlay = document.getElementById('task-line-play');
  if (linePlay) {
    linePlay.addEventListener('click', () => {
      if (totalLine < 1) return;
      if (state.taskPlayTimer) { clearInterval(state.taskPlayTimer); state.taskPlayTimer = null; }
      state.taskLine = 0;
      drawTask(app);
      state.taskPlayTimer = setInterval(() => {
        const current = typeof state.taskLine === 'number' ? state.taskLine : 0;
        if (current >= totalLine) {
          clearInterval(state.taskPlayTimer);
          state.taskPlayTimer = null;
          return;
        }
        state.taskLine = current + 1;
        drawTask(app);
      }, 700);
    });
  }
  if (!state.taskResult && !task.done) {
    api('/api/task/legal').then(legal => {
      if (!legal || !state.task || legal.task_key !== state.task.task_key) return;
      if (state.taskResult || state.task.done) return;
      // Доска могла перерисоваться, пока шёл запрос: берём актуальную ноду.
      const board = app.querySelector('.board');
      if (!board) return;
      const moves = (legal.moves || []).filter(m => m && m.from && m.to && m.san);
      if (!moves.length) { bindBoard(board); return; }
      bindBoard(board, {
        moves,
        onMove: move => submitTaskAnswer(app, move.san),
      });
      const hint = document.getElementById('task-board-hint');
      if (hint) hint.textContent = 'кликни фигуру, затем клетку (или перетащи её) — либо введи ход текстом';
    }).catch(() => {
      bindBoard(app.querySelector('.board'));
      const hint = document.getElementById('task-board-hint');
      if (hint) hint.textContent = 'введи ход текстом (доска-интерактив недоступен)';
    });
  }
}

// --- Свободный разбор задачи ---

const TASK_EXPLORE_DEPTH = 12;

function resetTaskExplore() {
  state.taskExploreToken = (state.taskExploreToken || 0) + 1;
  state.taskExplore = false;
  state.taskExploreFen = null;
  state.taskExploreMoves = [];
  state.taskExploreEval = null;
  state.taskExploreResult = null;
  state.taskExploreLast = [];
  state.taskExploreBusy = false;
  state.taskExploreError = null;
}

// Позиция, которую сейчас показывает просмотр линии (до хода или шаг линии).
function taskExploreSourceFen() {
  const task = state.task;
  if (!task) return null;
  const lineFens = [task.fen, ...(Array.isArray(task.best_line_fens) ? task.best_line_fens : [])];
  const lineIdx = typeof state.taskLine === 'number' ? state.taskLine : null;
  if (lineIdx != null && lineIdx >= 0 && lineFens[lineIdx]) return lineFens[lineIdx];
  return task.fen || null;
}

function chessPost(path, payload) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

function taskExplorePanelHtml() {
  const ev = state.taskExploreEval;
  const result = state.taskExploreResult;
  let evalText;
  if (state.taskExploreBusy) {
    evalText = 'Stockfish думает…';
  } else if (result && result.game_over) {
    evalText = 'Позиция завершена (мат или пат)';
  } else if (ev) {
    const win = ev.win == null ? NaN : Number(ev.win);
    const winTxt = Number.isFinite(win) ? Math.round(Math.max(0, Math.min(100, win))) + '%' : '—';
    const depth = ev.depth != null ? ev.depth : TASK_EXPLORE_DEPTH;
    const best = ev.best_san ? String(ev.best_san) : '—';
    evalText = `Stockfish (depth ${depth}): win белых ${winTxt} · лучший ход ${best}`;
  } else {
    evalText = 'Stockfish: оценка недоступна';
  }
  const status = result && !result.game_over && result.check
    ? '<span class="badge badge-bad">шах</span>'
    : '';
  return `
    <div class="task-explore-panel" aria-live="polite">
      <div class="task-explore-eval">${esc(evalText)}${status ? ' ' + status : ''}</div>
      ${state.taskExploreError ? `<div class="task-explore-error">${esc(state.taskExploreError)}</div>` : ''}
      <button type="button" class="btn btn-secondary" id="task-explore-back">Вернуться к решению</button>
    </div>`;
}

function startTaskExplore(app) {
  const fen = taskExploreSourceFen();
  if (!fen) return;
  if (state.taskPlayTimer) { clearInterval(state.taskPlayTimer); state.taskPlayTimer = null; }
  resetTaskExplore();
  state.taskExplore = true;
  state.taskExploreFen = fen;
  // Сохраняем подсветку последнего хода линии, если разбор начат с шага линии.
  const task = state.task;
  if (task && typeof state.taskLine === 'number' && state.taskLine > 0) {
    const lineFens = [task.fen, ...(Array.isArray(task.best_line_fens) ? task.best_line_fens : [])];
    if (lineFens[state.taskLine - 1] && lineFens[state.taskLine]) {
      state.taskExploreLast = lastMoveSquares(lineFens[state.taskLine - 1], lineFens[state.taskLine]);
    }
  }
  drawTask(app);
  taskExploreRefresh(app);
}

// Догружает легальные ходы и оценку для текущей позиции разбора.
async function taskExploreRefresh(app) {
  if (!state.taskExplore || !state.taskExploreFen) return;
  const token = state.taskExploreToken;
  const fen = state.taskExploreFen;
  state.taskExploreBusy = true;
  state.taskExploreError = null;
  if (state.currentView === 'task') drawTask(app);
  const [legalRes, evalRes] = await Promise.allSettled([
    chessPost('/api/chess/legal', { fen }),
    chessPost('/api/chess/eval', { fen, depth: TASK_EXPLORE_DEPTH }),
  ]);
  if (token !== state.taskExploreToken || !state.taskExplore) return;
  const errors = [];
  if (legalRes.status === 'fulfilled') {
    const moves = (legalRes.value && legalRes.value.moves) || [];
    state.taskExploreMoves = moves.filter(m => m && m.from && m.to);
  } else {
    state.taskExploreMoves = [];
    errors.push('ходы: ' + ((legalRes.reason && legalRes.reason.message) || 'недоступны'));
  }
  if (evalRes.status === 'fulfilled') {
    state.taskExploreEval = evalRes.value || null;
  } else {
    state.taskExploreEval = null;
    errors.push('оценка: ' + ((evalRes.reason && evalRes.reason.message) || 'недоступна'));
  }
  state.taskExploreBusy = false;
  state.taskExploreError = errors.length ? errors.join(' · ') : null;
  if (errors.length) showToast(errors[0], 'error');
  if (state.currentView === 'task') drawTask(app);
}

async function playTaskExploreMove(app, move) {
  if (!state.taskExplore || state.taskExploreBusy) return;
  const fen = state.taskExploreFen;
  const from = move && move.from;
  const to = move && move.to;
  if (!fen || !from || !to) return;
  const token = state.taskExploreToken;
  state.taskExploreBusy = true;
  state.taskExploreError = null;
  let res;
  try {
    res = await chessPost('/api/chess/move', {
      fen, from, to, promotion: move.promotion || undefined,
    });
  } catch (err) {
    if (token !== state.taskExploreToken || !state.taskExplore) return;
    state.taskExploreBusy = false;
    state.taskExploreError = err.message;
    if (state.currentView !== 'task') return;
    showToast(err.message, 'error');
    drawTask(app);
    return;
  }
  if (token !== state.taskExploreToken || !state.taskExplore) return;
  if (!res || !res.fen) {
    state.taskExploreBusy = false;
    state.taskExploreError = 'Сервер не вернул позицию';
    if (state.currentView !== 'task') return;
    showToast('Сервер не вернул позицию', 'error');
    drawTask(app);
    return;
  }
  state.taskExploreFen = res.fen;
  state.taskExploreResult = res;
  state.taskExploreLast = [from, to];
  state.taskExploreMoves = [];
  state.taskExploreEval = null;
  if (res.game_over) {
    // Терминальная позиция: ходов и оценки нет, доска остаётся как есть.
    state.taskExploreBusy = false;
    if (state.currentView === 'task') drawTask(app);
    return;
  }
  if (state.currentView === 'task') drawTask(app);
  await taskExploreRefresh(app);
}

async function submitTaskAnswer(app, san) {
  const answer = String(san == null ? '' : san).trim();
  if (!answer) return;
  state.taskAnswerText = answer;
  state.taskError = null;
  try {
    const fd = new FormData();
    fd.set('answer', answer);
    const res = await apiPost('/api/task/answer', fd);
    state.taskResult = res;
    state.taskRevealed = true;
    state.taskLine = null;
    if (state.taskPlayTimer) { clearInterval(state.taskPlayTimer); state.taskPlayTimer = null; }
    resetTaskExplore();
    state.task.done = !!res.correct;
  } catch (err) {
    state.taskError = err.message;
    showToast(err.message, 'error');
  }
  drawTask(app);
}

async function onTaskAnswer(e) {
  e.preventDefault();
  const app = document.getElementById('app');
  const input = document.getElementById('task-answer');
  await submitTaskAnswer(app, input ? input.value : state.taskAnswerText);
}

async function taskExplain(app) {
  state.taskExplaining = true;
  state.taskError = null;
  drawTask(app);
  let res;
  try {
    res = await apiPost('/api/task/explain', new FormData());
  } catch (err) {
    state.taskExplaining = false;
    state.taskJobId = null;
    state.taskError = err.message;
    showToast(err.message, 'error');
    drawTask(app);
    return;
  }
  if (res.cached) {
    state.taskExplaining = false;
    state.taskJobId = null;
    state.taskExplanation = res.explanation;
    drawTask(app);
    return;
  }
  state.taskJobId = res.job_id;
  taskPollExplanation(app, res.job_id);
}

function taskPollExplanation(app, jobId) {
  if (state.timers.task) clearInterval(state.timers.task);
  state.timers.task = setInterval(async () => {
    if (state.currentView !== 'task') { clearInterval(state.timers.task); state.timers.task = null; return; }
    let job;
    try {
      job = await api('/api/jobs/' + jobId);
    } catch {
      clearInterval(state.timers.task);
      state.timers.task = null;
      state.taskExplaining = false;
      state.taskJobId = null;
      state.taskError = 'Не удалось получить объяснение';
      drawTask(app);
      return;
    }
    if (job.status === 'done') {
      clearInterval(state.timers.task);
      state.timers.task = null;
      state.taskExplaining = false;
      state.taskJobId = null;
      state.taskExplanation = (job.result && job.result.explanation) || '';
      drawTask(app);
    } else if (job.status === 'error') {
      clearInterval(state.timers.task);
      state.timers.task = null;
      state.taskExplaining = false;
      state.taskJobId = null;
      state.taskError = job.error || 'Не удалось получить объяснение';
      drawTask(app);
    }
  }, 700);
}

// --- Тренер дебютов ---

const OPENING_START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const OPENING_KIND_RU = { good: 'хорошо', better: 'можно лучше', bad: 'плохо' };

function openingStyleLabel(key) {
  const s = (state.openingStyles || []).find(x => x.key === key);
  return s ? s.label : (key || '—');
}

function openingHighlight() {
  const fens = state.openingFens;
  if (fens && fens.length >= 2) {
    try { return lastMoveSquares(fens[fens.length - 2], fens[fens.length - 1]); } catch { return []; }
  }
  return [];
}

function openingMoveRowsHtml(moves) {
  if (!moves.length) return '<p style="color:var(--c-text-muted)">Ходов пока нет.</p>';
  const rows = {};
  moves.forEach((san, i) => {
    const num = Math.floor(i / 2) + 1;
    if (!rows[num]) rows[num] = { num, w: null, b: null };
    rows[num][i % 2 === 0 ? 'w' : 'b'] = san;
  });
  return Object.values(rows).map(r => `
    <div class="move-row">
      <span class="move-num">${r.num}.</span>
      <span class="move-cell" style="cursor:default">${esc(r.w || '')}</span>
      <span class="move-cell" style="cursor:default">${esc(r.b || '')}</span>
    </div>`).join('');
}

// --- Теория для тренера дебютов ---

function resetOpeningTheory() {
  stopOpeningTheoryPlay();
  state.openingTheory = null;
  state.openingTheoryRequestFen = null;
  state.openingTheoryLoading = false;
  state.openingTheoryError = null;
  state.openingTheoryLine = null;
  state.openingTheorySetup = false;
  state.openingTheoryIdeas = null;
  state.openingTheoryIdeasLoading = false;
  state.openingTheoryIdeasDone = false;
  state.openingTheoryIdeasCached = false;
  state.openingTheoryIdeasError = null;
}

function stopOpeningTheoryPlay() {
  if (state.timers.theory) {
    clearInterval(state.timers.theory);
    state.timers.theory = null;
  }
}

function openingTheorySourceBadge(source) {
  if (source === 'lichess-masters' || source === 'lichess') return '<span class="badge badge-blue">Lichess masters</span>';
  if (source === 'eco') return '<span class="badge badge-neutral">ECO (офлайн)</span>';
  if (source === 'none' || source === 'off') return '<span class="badge badge-neutral">теория не найдена</span>';
  return '<span class="badge badge-neutral">источник неизвестен</span>';
}

function openingTheoryGamesLabel(games) {
  const v = Number(games);
  if (!Number.isFinite(v) || v <= 0) return '';
  const trim = s => s.replace(/,0$/, '');
  if (v >= 1e6) return trim((v / 1e6).toFixed(v >= 1e7 ? 0 : 1).replace('.', ',')) + 'M';
  if (v >= 1e3) return trim((v / 1e3).toFixed(v >= 1e4 ? 0 : 1).replace('.', ',')) + 'k';
  return String(Math.round(v));
}

function openingTheoryGamesWord(games) {
  const n = Math.round(Math.abs(Number(games) || 0));
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 14) return 'партий';
  const mod10 = mod100 % 10;
  if (mod10 === 1) return 'партия';
  if (mod10 >= 2 && mod10 <= 4) return 'партии';
  return 'партий';
}

function openingTheoryStatsLabel(move) {
  const games = Number(move && move.games);
  const total = Number.isFinite(games) && games > 0 ? games : 0;
  const pct = value => {
    const n = Number(value);
    if (!total || !Number.isFinite(n)) return null;
    return Math.round((n / total) * 100);
  };
  const parts = [];
  const titleParts = [];
  if (total) {
    const w = pct(move.white);
    const d = pct(move.draws);
    const b = pct(move.black);
    if (w != null && d != null && b != null) {
      parts.push(`${w}/${d}/${b}%`);
      titleParts.push(`белые ${w}% · ничьи ${d}% · чёрные ${b}%`);
    }
  }
  const gamesLabel = openingTheoryGamesLabel(total);
  if (gamesLabel) {
    parts.push(`${gamesLabel} ${total >= 1000 ? 'партий' : openingTheoryGamesWord(total)}`);
    titleParts.push(`${Math.round(total)} ${openingTheoryGamesWord(total)}`);
  }
  return { text: parts.join(' · '), title: titleParts.join(' · ') };
}

function openingTheorySetupFen() {
  const t = state.openingTheory;
  if (!t) return null;
  const line = Array.isArray(t.main_line) ? t.main_line : [];
  if (typeof t.setup_fen === 'string' && t.setup_fen) return t.setup_fen;
  const last = line.length ? line[line.length - 1] : null;
  return last && last.fen ? last.fen : null;
}

// Что показать на доске вместо текущей позиции партии: шаг линии или расстановку.
function openingTheoryPreview() {
  const t = state.openingTheory;
  if (!t) return null;
  const line = Array.isArray(t.main_line) ? t.main_line : [];
  const base = state.openingTheoryRequestFen || t.fen || '';
  let fen = null;
  let prev = null;
  let label = '';
  if (state.openingTheorySetup) {
    fen = openingTheorySetupFen();
    if (!fen) return null;
    const last = line.length ? line[line.length - 1] : null;
    if (last && last.fen === fen) {
      prev = line.length > 1 && line[line.length - 2] && line[line.length - 2].fen
        ? line[line.length - 2].fen
        : base;
    } else {
      prev = last && last.fen ? last.fen : base;
    }
    label = 'setup';
  } else if (typeof state.openingTheoryLine === 'number') {
    const step = line[state.openingTheoryLine];
    if (!step || !step.fen) return null;
    fen = step.fen;
    const before = state.openingTheoryLine > 0 ? line[state.openingTheoryLine - 1] : null;
    prev = before && before.fen ? before.fen : base;
    label = 'line';
  } else {
    return null;
  }
  let lastMove = [];
  try {
    lastMove = prev && fen && prev !== fen ? lastMoveSquares(prev, fen) : [];
  } catch {
    lastMove = [];
  }
  const arrows = lastMove.length >= 2 ? [{ from: lastMove[0], to: lastMove[1], color: 'good' }] : [];
  return { fen, lastMove, arrows, label };
}

function openingTheoryIdeasNote() {
  const t = state.openingTheory;
  const base = t && (t.source === 'lichess-masters' || t.source === 'lichess') ? 'по данным Lichess masters' : 'объяснение LLM';
  return base + (state.openingTheoryIdeasCached ? ' · из кеша' : '');
}

function openingTheoryNavHtml(total, idx) {
  const last = Math.max(0, total - 1);
  const pos = Math.max(0, Math.min(last, idx == null ? 0 : idx));
  return `
    <div class="theory-nav">
      <button type="button" class="btn btn-secondary" id="theory-prev" ${pos <= 0 ? 'disabled' : ''}>◀</button>
      <input type="range" id="theory-range" min="0" max="${last}" value="${pos}" ${total < 2 ? 'disabled' : ''}>
      <button type="button" class="btn btn-secondary" id="theory-next" ${pos >= last ? 'disabled' : ''}>▶</button>
      <span class="theory-pos">${pos + 1} / ${total}</span>
    </div>`;
}

function openingTheoryCardHtml() {
  const t = state.openingTheory;
  const loading = !!state.openingTheoryLoading;
  const line = t && Array.isArray(t.main_line) ? t.main_line : [];
  const activeIdx = typeof state.openingTheoryLine === 'number' ? state.openingTheoryLine : null;
  const setupActive = !!state.openingTheorySetup;
  const canSetup = !!openingTheorySetupFen();
  const source = t && t.source;
  const badge = t ? openingTheorySourceBadge(source) : '';

  let body = '';
  if (!t) {
    body = `
      <p class="theory-card-note">Статистика ходов из базы Lichess masters и офлайн-дебютов ECO для текущей позиции.</p>
      <button type="button" class="btn btn-primary" id="theory-load" ${loading ? 'disabled' : ''}>${loading ? 'Загружаю…' : 'Показать теорию'}</button>
      ${state.openingTheoryError ? `<div class="alert alert-error" style="margin-top:8px">${esc(state.openingTheoryError)}</div>` : ''}`;
  } else {
    const chips = line.map((m, i) => {
      if (!m) return '';
      const stats = openingTheoryStatsLabel(m);
      const title = `Показать позицию после ${m.san || m.uci || '—'}${stats.title ? ' · ' + stats.title : ''}`;
      return `<button type="button" class="theory-chip ${i === activeIdx && !setupActive ? 'theory-chip-active' : ''}"
        data-theory-ply="${i}" title="${esc(title)}">
        <span class="theory-chip-san">${esc(m.san || m.uci || '—')}</span>
        ${stats.text ? `<span class="theory-chip-stats">${esc(stats.text)}</span>` : ''}
      </button>`;
    }).join('');

    let ideasHtml = '';
    if (state.openingTheoryIdeasError) {
      ideasHtml += `<div class="alert alert-error" style="margin-top:8px">${esc(state.openingTheoryIdeasError)}</div>`;
    }
    if (state.openingTheoryIdeas) {
      ideasHtml += `<div class="theory-ideas">
        <div class="theory-ideas-source">${esc(openingTheoryIdeasNote())}</div>
        <div class="mentor-reply">${esc(state.openingTheoryIdeas)}</div>
      </div>`;
    } else if (state.openingTheoryIdeasDone && !state.openingTheoryIdeasError) {
      ideasHtml += '<p class="theory-card-note" style="margin-top:8px">Идеи для этой позиции недоступны.</p>';
    } else if (!state.openingTheoryIdeasLoading && !state.openingTheoryIdeasError) {
      ideasHtml += '<p class="theory-card-note" style="margin-top:8px">«Идеи вкратце» — короткое объяснение плана (LLM или база).</p>';
    }

    body = `
      ${line.length
        ? `<div class="theory-chips">${chips}</div>`
        : `<p class="theory-empty">${source === 'none' || source === 'off' ? 'Для этой позиции теории не найдено.' : 'Главная линия пуста.'}</p>`}
      <div class="theory-controls">
        <button type="button" class="btn btn-secondary" id="theory-play" ${line.length < 1 || loading ? 'disabled' : ''}>▶ Показать линию</button>
        <button type="button" class="btn btn-secondary" id="theory-setup" ${canSetup && !loading ? '' : 'disabled'}>Расстановка</button>
        <button type="button" class="btn btn-secondary" id="theory-ideas" ${state.openingTheoryIdeasLoading ? 'disabled' : ''}>${state.openingTheoryIdeasLoading ? 'Идеи…' : 'Идеи вкратце'}</button>
        <button type="button" class="btn btn-secondary" id="theory-back" ${activeIdx != null || setupActive ? '' : 'disabled'}>Вернуться</button>
        <button type="button" class="btn btn-link" id="theory-load" ${loading ? 'disabled' : ''}>${loading ? 'Обновляю…' : 'Обновить'}</button>
      </div>
      ${activeIdx != null && !setupActive ? openingTheoryNavHtml(line.length, activeIdx) : ''}
      ${setupActive ? '<p class="theory-card-note" style="margin-top:8px">Расстановка — позиция в конце линии; последний ход подсвечен.</p>' : ''}
      ${ideasHtml}`;
  }

  return `
    <div class="card theory-card">
      <div class="card-header"><span class="card-title">Теория</span>${badge}</div>
      ${body}
    </div>`;
}

async function loadOpeningTheory(app) {
  const s = state.openingSession;
  if (!s || !s.fen || state.openingTheoryLoading) return;
  const fen = s.fen;
  stopOpeningTheoryPlay();
  state.openingTheoryLoading = true;
  state.openingTheoryError = null;
  drawOpening(app);
  try {
    const moves = Array.isArray(s.moves) ? s.moves.join(',') : '';
    const data = await api(
      '/api/theory?fen=' + encodeURIComponent(fen) + '&plies=12' +
      (moves ? '&moves=' + encodeURIComponent(moves) : ''),
    );
    if (!state.openingSession || state.openingSession.fen !== fen) return;
    state.openingTheory = data || null;
    state.openingTheoryRequestFen = fen;
    state.openingTheoryLine = null;
    state.openingTheorySetup = false;
    state.openingTheoryIdeas = null;
    state.openingTheoryIdeasDone = false;
    state.openingTheoryIdeasCached = false;
    state.openingTheoryIdeasError = null;
  } catch (err) {
    if (isSessionError(err)) { handleSessionExpired(); return; }
    state.openingTheory = null;
    state.openingTheoryError = err.message;
    showToast(err.message, 'error');
  } finally {
    state.openingTheoryLoading = false;
    if (state.user && state.currentView === 'opening' && state.openingSession && state.openingSession.fen === fen) {
      drawOpening(app);
    }
  }
}

async function loadOpeningTheoryIdeas(app) {
  const t = state.openingTheory;
  if (!t || state.openingTheoryIdeasLoading) return;
  const fen = state.openingTheoryRequestFen || t.fen;
  const line = (Array.isArray(t.main_line) ? t.main_line : [])
    .filter(m => m && m.san && m.fen);
  state.openingTheoryIdeasLoading = true;
  state.openingTheoryIdeasError = null;
  drawOpening(app);
  try {
    const data = await api('/api/theory/ideas', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ fen, line }),
    });
    if (state.openingTheory !== t) return;
    state.openingTheoryIdeas = data && typeof data.ideas === 'string' && data.ideas ? data.ideas : null;
    state.openingTheoryIdeasCached = !!(data && data.cached);
    state.openingTheoryIdeasDone = true;
  } catch (err) {
    if (isSessionError(err)) { handleSessionExpired(); return; }
    state.openingTheoryIdeasError = err.message;
    showToast(err.message, 'error');
  } finally {
    state.openingTheoryIdeasLoading = false;
    if (state.user && state.currentView === 'opening' && state.openingTheory === t) drawOpening(app);
  }
}

function startOpeningTheoryPlay(app) {
  const t = state.openingTheory;
  const total = t && Array.isArray(t.main_line) ? t.main_line.length : 0;
  if (!total) return;
  stopOpeningTheoryPlay();
  state.openingTheorySetup = false;
  state.openingTheoryLine = 0;
  drawOpening(app);
  if (total < 2) return;
  state.timers.theory = setInterval(() => {
    if (!state.user || state.currentView !== 'opening') { stopOpeningTheoryPlay(); return; }
    const current = typeof state.openingTheoryLine === 'number' ? state.openingTheoryLine : 0;
    if (current >= total - 1) { stopOpeningTheoryPlay(); return; }
    state.openingTheoryLine = current + 1;
    drawOpening(app);
  }, 700);
}

function openingTheoryStep(app, delta) {
  const t = state.openingTheory;
  const total = t && Array.isArray(t.main_line) ? t.main_line.length : 0;
  if (!total) return;
  stopOpeningTheoryPlay();
  const current = typeof state.openingTheoryLine === 'number' ? state.openingTheoryLine : (delta > 0 ? -1 : 0);
  state.openingTheorySetup = false;
  state.openingTheoryLine = Math.max(0, Math.min(total - 1, current + delta));
  drawOpening(app);
}

function bindOpeningTheory(app) {
  const loadBtn = document.getElementById('theory-load');
  if (loadBtn) loadBtn.addEventListener('click', () => loadOpeningTheory(app));

  app.querySelectorAll('[data-theory-ply]').forEach(btn => {
    btn.addEventListener('click', () => {
      const t = state.openingTheory;
      const line = t && Array.isArray(t.main_line) ? t.main_line : [];
      const idx = Number(btn.dataset.theoryPly);
      if (!Number.isInteger(idx) || idx < 0 || idx >= line.length) return;
      stopOpeningTheoryPlay();
      state.openingTheorySetup = false;
      state.openingTheoryLine = idx;
      drawOpening(app);
    });
  });

  const backBtn = document.getElementById('theory-back');
  if (backBtn) backBtn.addEventListener('click', () => {
    stopOpeningTheoryPlay();
    state.openingTheoryLine = null;
    state.openingTheorySetup = false;
    drawOpening(app);
  });
  const playBtn = document.getElementById('theory-play');
  if (playBtn) playBtn.addEventListener('click', () => startOpeningTheoryPlay(app));
  const setupBtn = document.getElementById('theory-setup');
  if (setupBtn) setupBtn.addEventListener('click', () => {
    if (!openingTheorySetupFen()) return;
    stopOpeningTheoryPlay();
    state.openingTheoryLine = null;
    state.openingTheorySetup = true;
    drawOpening(app);
  });
  const ideasBtn = document.getElementById('theory-ideas');
  if (ideasBtn) ideasBtn.addEventListener('click', () => loadOpeningTheoryIdeas(app));
  const prevBtn = document.getElementById('theory-prev');
  if (prevBtn) prevBtn.addEventListener('click', () => openingTheoryStep(app, -1));
  const nextBtn = document.getElementById('theory-next');
  if (nextBtn) nextBtn.addEventListener('click', () => openingTheoryStep(app, 1));
  const range = document.getElementById('theory-range');
  if (range) range.addEventListener('input', e => {
    const t = state.openingTheory;
    const total = t && Array.isArray(t.main_line) ? t.main_line.length : 0;
    if (!total) return;
    stopOpeningTheoryPlay();
    state.openingTheorySetup = false;
    state.openingTheoryLine = Math.max(0, Math.min(total - 1, Number(e.target.value) || 0));
    drawOpening(app);
  });
}

async function viewOpening(app) {
  if (!state.user) {
    app.innerHTML = '<div class="onboarding card"><div class="empty-icon">♟</div><h1>Тренер дебютов</h1><p>Профиль не выбран — выбери его на странице Профили.</p><a href="#" class="btn btn-primary" data-view="users">Профили</a></div>';
    bindLinks(app);
    return;
  }
  if (state.openingSession && state.openingSession.id) {
    await loadOpeningSession(app, state.openingSession.id);
    return;
  }
  await drawOpeningSetup(app);
}

async function loadOpeningSession(app, id) {
  const prevId = state.openingSession && state.openingSession.id;
  let s;
  try {
    s = await api('/api/opening/' + encodeURIComponent(id));
  } catch (err) {
    state.openingSession = null;
    state.openingFens = null;
    state.openingLastReply = null;
    if (isSessionError(err)) { handleSessionExpired(); return; }
    await drawOpeningSetup(app, err.message);
    return;
  }
  state.openingSession = s;
  if (prevId !== s.id) {
    state.openingFens = null;
    resetOpeningTheory();
  }
  drawOpening(app);
  if (state.openingAnalyzing && !state.timers.opening && state.openingJobId) {
    openingPoll(app, state.openingJobId);
  }
}

async function drawOpeningSetup(app, errorMsg) {
  if (state.timers.opening) { clearInterval(state.timers.opening); state.timers.opening = null; }
  state.openingThinking = false;
  if (!state.openingStyles) {
    try {
      const data = await api('/api/opening/styles');
      state.openingStyles = data.styles || [];
    } catch (err) {
      if (isSessionError(err)) { handleSessionExpired(); return; }
      state.openingStyles = [];
    }
  }
  const styles = state.openingStyles;
  if (!state.openingStyle && styles.length) state.openingStyle = styles[0].key;
  const style = state.openingStyle;
  const color = state.openingColor === 'black' ? 'black' : 'white';
  const selected = styles.find(s => s.key === style) || styles[0] || {};

  app.innerHTML = `
    <div class="card-header"><h1>Тренер дебютов</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    ${errorMsg ? `<div class="alert alert-error">${esc(errorMsg)}</div>` : ''}
    <div class="card">
      <div class="card-header"><span class="card-title">Новая партия</span><span class="badge badge-neutral">${40} полуходов</span></div>
      <form id="opening-form">
        <div class="form-group">
          <label>Стиль соперника</label>
          <div class="games-filter" id="opening-styles">
            ${styles.map(s => `<button type="button" class="btn btn-secondary ${s.key === style ? 'btn-active' : ''}" data-style="${esc(s.key)}">${esc(s.label)}</button>`).join('')}
          </div>
          <div class="opening-style-desc" id="opening-style-desc">${esc(selected.description || '')}</div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label>Твой рейтинг (необяз.)</label>
            <input type="number" name="my_elo" id="opening-my-elo" min="100" max="3500" step="1" placeholder="по умолчанию">
          </div>
          <div class="form-group">
            <label>Рейтинг соперника (необяз.)</label>
            <input type="number" name="opp_elo" id="opening-opp-elo" min="100" max="3500" step="1" placeholder="по умолчанию">
          </div>
        </div>
        <div class="form-group">
          <label>Соперник</label>
          <div class="games-filter">
            <button type="button" class="btn btn-secondary" data-opp="equal">равный</button>
            <button type="button" class="btn btn-secondary" data-opp="plus100" title="Соперник на 100 Elo выше твоего рейтинга (база: твой рейтинг или 1500 по умолчанию); Maia играет сильнее">+100</button>
            <button type="button" class="btn btn-secondary" data-opp="stronger" title="Соперник на 200 Elo выше твоего рейтинга (база: твой рейтинг или 1500 по умолчанию); Maia играет сильнее">сильнее (+200)</button>
            <button type="button" class="btn btn-secondary" data-opp="hybrid" title="Гибрид: Maia предлагает человеческие ходы, Stockfish отсекает зевки — сильнее ~2200-2400, но играет по-человечески">Maia+ (фильтр)</button>
          </div>
          <div style="font-size:12px;color:var(--c-text-muted);margin-top:4px">Число — разница Elo соперника относительно твоего рейтинга (база из профиля или 1500): +100/+200 означает соперника сильнее на 100/200 пунктов. Используется для выбора силы Maia.</div>
          <div style="font-size:12px;color:var(--c-text-muted);margin-top:4px">«Maia+ (фильтр)» — гибрид: Maia предлагает человеческие ходы, Stockfish отсекает зевки. Сильнее ~2200-2400, но играет по-человечески. Работает на сервере с достаточной RAM.</div>
          <div class="form-group" style="margin-top:8px">
            <label>Скорость соперника</label>
            <div class="games-filter">
              <button type="button" class="btn btn-secondary ${state.openingHuman ? '' : 'btn-active'}" data-speed="fast">Быстро</button>
              <button type="button" class="btn btn-secondary ${state.openingHuman ? 'btn-active' : ''}" data-speed="human">Как человек</button>
            </div>
          </div>
          <div id="opening-strength-note" style="font-size:12px;color:var(--c-text-muted);margin-top:4px;display:none">Твой рейтинг ≥ 2000: Maia не различает силы выше 2000 Elo, доступен только равный уровень.</div>
        </div>
        <div class="form-group">
          <label>Твой цвет</label>
          <div class="games-filter">
            <button type="button" class="btn btn-secondary ${color === 'white' ? 'btn-active' : ''}" data-color="white">белые</button>
            <button type="button" class="btn btn-secondary ${color === 'black' ? 'btn-active' : ''}" data-color="black">чёрные</button>
          </div>
        </div>
        <div class="alert alert-error" id="opening-setup-err" style="display:none"></div>
        <button type="submit" class="btn btn-primary" style="width:fit-content">Начать</button>
      </form>
    </div>
    <div id="opening-recent"></div>`;

  app.querySelectorAll('#opening-styles [data-style]').forEach(btn => {
    btn.addEventListener('click', () => {
      state.openingStyle = btn.dataset.style;
      app.querySelectorAll('#opening-styles [data-style]').forEach(b => b.classList.toggle('btn-active', b === btn));
      const sel = (state.openingStyles || []).find(s => s.key === state.openingStyle);
      const desc = document.getElementById('opening-style-desc');
      if (desc) desc.textContent = (sel && sel.description) || '';
    });
  });
  app.querySelectorAll('#opening-form [data-color]').forEach(btn => {
    btn.addEventListener('click', () => {
      state.openingColor = btn.dataset.color;
      app.querySelectorAll('#opening-form [data-color]').forEach(b => b.classList.toggle('btn-active', b === btn));
    });
  });
  const updateOpeningStrengthAvailability = () => {
    const my = document.getElementById('opening-my-elo');
    const base = Number(my && my.value) || 1500;
    const high = base >= 2000;
    const note = document.getElementById('opening-strength-note');
    if (note) note.style.display = high ? 'block' : 'none';
    app.querySelectorAll('#opening-form [data-opp="plus100"], #opening-form [data-opp="stronger"]').forEach(btn => {
      btn.disabled = high;
      btn.style.opacity = high ? '0.45' : '';
      btn.style.cursor = high ? 'not-allowed' : '';
      btn.title = high
        ? 'Maia не различает силы выше 2000 Elo: для твоего рейтинга +100/+200 недоступны'
        : (btn.dataset.opp === 'plus100'
            ? 'Соперник на 100 Elo выше твоего рейтинга (база: твой рейтинг или 1500 по умолчанию); Maia играет сильнее'
            : 'Соперник на 200 Elo выше твоего рейтинга (база: твой рейтинг или 1500 по умолчанию); Maia играет сильнее');
    });
  };
  app.querySelectorAll('#opening-form [data-opp]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      app.querySelectorAll('#opening-form [data-opp]').forEach(b => b.classList.toggle('btn-active', b === btn));
      const my = document.getElementById('opening-my-elo');
      const opp = document.getElementById('opening-opp-elo');
      const base = Number(my && my.value) || 1500;
      const mode = btn.dataset.opp;
      if (mode === 'equal') opp.value = my && my.value ? my.value : '';
      else if (mode === 'plus100') opp.value = String(base + 100);
      else if (mode === 'stronger') opp.value = String(base + 200);
      else if (mode === 'hybrid') opp.value = '-1';
    });
  });
  const myEloInput = document.getElementById('opening-my-elo');
  if (myEloInput) myEloInput.addEventListener('input', updateOpeningStrengthAvailability);
  updateOpeningStrengthAvailability();
  app.querySelectorAll('#opening-form [data-speed]').forEach(btn => {
    btn.addEventListener('click', () => {
      state.openingHuman = btn.dataset.speed === 'human';
      app.querySelectorAll('#opening-form [data-speed]').forEach(b => b.classList.toggle('btn-active', b === btn));
    });
  });

  const form = document.getElementById('opening-form');
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const err = document.getElementById('opening-setup-err');
    if (err) err.style.display = 'none';
    const fd = new FormData(form);
    const body = { style: state.openingStyle, color: state.openingColor };
    const my = String(fd.get('my_elo') || '').trim();
    const opp = String(fd.get('opp_elo') || '').trim();
    if (my) body.my_elo = Number(my);
    if (opp) body.opp_elo = Number(opp);
    body.human = !!state.openingHuman;
    const btn = form.querySelector('button[type="submit"]');
    if (btn) btn.disabled = true;
    try {
      const res = await api('/api/opening/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      startOpeningSession(res);
      drawOpening(app);
    } catch (apiErr) {
      if (isSessionError(apiErr)) { handleSessionExpired(); return; }
      if (btn) btn.disabled = false;
      if (err) { err.textContent = apiErr.message; err.style.display = 'block'; }
    }
  });

  loadOpeningRecent(app);
}

function startOpeningSession(res) {
  if (state.timers.opening) { clearInterval(state.timers.opening); state.timers.opening = null; }
  resetOpeningTheory();
  state.openingSession = res;
  state.openingLastReply = res.last_reply || null;
  state.openingThinking = false;
  state.openingAnalyzing = false;
  state.openingJobId = null;
  state.openingError = null;
  state.openingFens = [OPENING_START_FEN];
  if ((res.moves || []).length) state.openingFens.push(res.fen);
}

async function loadOpeningRecent(app) {
  const box = document.getElementById('opening-recent');
  if (!box) return;
  let data;
  try { data = await api('/api/opening?limit=10'); } catch { return; }
  const sessions = (data && data.sessions) || [];
  const rows = sessions.map(s => {
    const st = s.status === 'analyzed' ? '<span class="badge badge-good">разобран</span>'
      : s.status === 'done' ? '<span class="badge badge-blue">доигран</span>'
        : '<span class="badge badge-neutral">в игре</span>';
    return `<tr class="game-row" data-session="${esc(s.id)}">
      <td>${st}</td>
      <td>${esc(openingStyleLabel(s.style))}</td>
      <td class="center">${s.color === 'black' ? '♚' : '♔'}</td>
      <td class="num">${(s.moves || []).length} / ${s.plies}</td>
      <td class="num">${s.my_elo != null ? s.my_elo : '—'} vs ${s.opp_elo != null ? s.opp_elo : '—'}</td>
    </tr>`;
  }).join('');
  box.innerHTML = `
    <div class="card">
      <div class="card-header"><span class="card-title">Недавние партии</span></div>
      ${sessions.length ? `<div class="table-wrap"><table><thead><tr><th>Статус</th><th>Стиль</th><th>Цвет</th><th class="num">Ходы</th><th class="num">Рейтинги</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p style="color:var(--c-text-muted);padding:8px">Пока нет партий — начни первую.</p>'}
    </div>`;
  box.querySelectorAll('.game-row').forEach(row => {
    row.addEventListener('click', () => {
      loadOpeningSession(app, row.dataset.session);
    });
  });
}

function drawOpening(app) {
  const s = state.openingSession;
  if (!s) { app.innerHTML = '<div class="card"><p>Загрузка...</p></div>'; return; }
  const moves = s.moves || [];
  const flip = s.color === 'black';
  const finished = !!s.finished;
  const yourTurn = !finished && s.turn === s.color;
  const colorLabel = s.color === 'black' ? 'чёрные' : 'белые';
  const report = s.report;
  // Просмотр теории (шаг линии или расстановка) заменяет позицию на доске,
  // но не трогает саму партию: ходы и ответы Maia продолжают работать.
  const theoryPreview = openingTheoryPreview();

  const engineNote = s.engine
    ? `<span class="badge badge-neutral" title="движок соперника">движок: ${s.engine === 'maia2' ? 'Maia-2' : 'MaiaLite'}</span>`
    : '';

  let replyHtml = '';
  const lr = state.openingLastReply;
  if (lr && lr.san) {
    replyHtml = `<div class="opening-reply">соперник: <span class="drill-san">${esc(lr.san)}</span>${lr.reason ? ` — <span style="color:var(--c-text-muted)">${esc(lr.reason)}</span>` : ''}</div>`;
  }

  let panel = '';
  if (finished) {
    if (s.status === 'analyzed' && report) {
      panel = openingReportHtml(report);
    } else {
      const analyzing = state.openingAnalyzing;
      panel = `
        <div class="card">
          <div class="card-header"><span class="card-title">Партия завершена</span><span class="badge badge-blue">${moves.length} / ${s.plies}</span></div>
          <p style="color:var(--c-text-muted);font-size:13px;margin-top:0">Пройдено ${moves.length} полуходов. Запусти разбор — Stockfish + LLM соберут отчёт по дебюту.</p>
          <button class="btn btn-primary" id="opening-analyze" ${analyzing ? 'disabled' : ''}>${analyzing ? 'Разбираю…' : 'Разобрать дебют'}</button>
          <div id="opening-analysis-status" style="color:var(--c-text-muted);font-size:13px;margin-top:8px"></div>
        </div>`;
    }
  } else {
    panel = `
      <div class="card">
        <div class="card-header"><span class="card-title">Твой ход</span></div>
        ${state.openingThinking
          ? '<p style="color:var(--c-text-muted);font-size:13px;margin-top:0">соперник думает…</p>'
          : theoryPreview
            ? '<p style="color:var(--c-text-muted);font-size:13px;margin-top:0">просмотр теории: доска только для просмотра — нажми «Вернуться», чтобы вернуться к партии</p>'
            : yourTurn
              ? '<p style="color:var(--c-text-muted);font-size:13px;margin-top:0">кликни или перетащи фигуру — точки показывают доступные поля; ПКМ-протяжка рисует стрелку</p>'
              : '<p style="color:var(--c-text-muted);font-size:13px;margin-top:0">ждём соперника…</p>'}
      </div>`;
  }

  app.innerHTML = `
    <div class="card-header"><h1>Тренер дебютов</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    ${state.openingError ? `<div class="alert alert-error">${esc(state.openingError)}</div>` : ''}
    <div class="grid game-layout">
      <div class="card">
        ${boardHtml(theoryPreview ? theoryPreview.fen : s.fen, {
          lastMove: theoryPreview ? theoryPreview.lastMove : openingHighlight(),
          flip,
          animate: theoryPreview ? true : (yourTurn && !state.openingThinking && moves.length > 0),
          arrows: theoryPreview ? theoryPreview.arrows : [],
        })}
        <div class="board-meta">
          <span class="badge badge-neutral">${esc(openingStyleLabel(s.style))}</span>
          <span class="badge badge-blue">ты: ${colorLabel}</span>
          <span class="badge ${finished ? 'badge-good' : 'badge-neutral'}">${moves.length} / ${s.plies}</span>
          ${engineNote}
          ${state.openingThinking ? '<span class="badge badge-neutral">соперник думает…</span>' : ''}
          ${theoryPreview ? `<span class="badge badge-blue">${theoryPreview.label === 'setup' ? 'расстановка' : 'просмотр линии'}</span>` : ''}
        </div>
        ${replyHtml}
        <div style="margin-top:12px"><button class="btn btn-secondary" id="opening-new">Новая партия</button></div>
      </div>
      <div class="game-side">
        <div class="card">
          <div class="card-header"><span class="card-title">Ходы</span></div>
          <div class="moves-list">${openingMoveRowsHtml(moves)}</div>
        </div>
        ${openingTheoryCardHtml()}
        ${panel}
      </div>
    </div>`;

  bindBoard(app.querySelector('.board'));
  bindOpeningTheory(app);
  const newBtn = document.getElementById('opening-new');
  if (newBtn) newBtn.addEventListener('click', () => {
    if (state.timers.opening) { clearInterval(state.timers.opening); state.timers.opening = null; }
    resetOpeningTheory();
    state.openingSession = null;
    state.openingFens = null;
    state.openingLastReply = null;
    state.openingError = null;
    state.openingAnalyzing = false;
    state.openingJobId = null;
    viewOpening(app);
  });
  const analyzeBtn = document.getElementById('opening-analyze');
  if (analyzeBtn) analyzeBtn.addEventListener('click', () => openingAnalyze(app));
  if (state.openingAnalyzing && !state.timers.opening && state.openingJobId) {
    openingPoll(app, state.openingJobId);
  }
  if (!finished && yourTurn && !state.openingThinking && !theoryPreview) {
    bindOpeningBoard(app, s.id);
  }
}

function bindOpeningBoard(app, sessionId) {
  api('/api/opening/' + encodeURIComponent(sessionId) + '/legal').then(legal => {
    const s = state.openingSession;
    if (!s || s.id !== sessionId) return;
    // Доска могла перерисоваться, пока шёл запрос: берём актуальную ноду.
    const board = app.querySelector('.board');
    if (!board) return;
    if (s.finished || s.turn !== s.color || state.openingThinking || openingTheoryPreview()) { bindBoard(board); return; }
    const moves = ((legal && legal.moves) || []).filter(m => m && m.from && m.to && m.san);
    if (!moves.length) { bindBoard(board); return; }
    bindBoard(board, {
      moves,
      canMove: () => {
        const cur = state.openingSession;
        return !!cur && cur.id === sessionId && !cur.finished && !state.openingThinking;
      },
      onMove: move => submitOpeningMove(app, move.san),
    });
  }).catch(() => {
    /* доска недоступна для ходов — стрелки и подсветка всё равно нужны */
    bindBoard(app.querySelector('.board'));
  });
}

async function submitOpeningMove(app, san) {
  const s = state.openingSession;
  if (!s) return;
  state.openingThinking = true;
  state.openingError = null;
  drawOpening(app);
  let moveState;
  try {
    moveState = await api('/api/opening/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: s.id, san }),
    });
  } catch (err) {
    if (isSessionError(err)) { handleSessionExpired(); return; }
    state.openingError = err.message;
    showToast(err.message, 'error');
    state.openingThinking = false;
    drawOpening(app);
    return;
  }
  // Ход игрока показываем сразу, не дожидаясь ответа соперника.
  if (state.openingFens) state.openingFens.push(moveState.fen);
  state.openingSession = moveState;
  resetOpeningTheory();
  state.openingLastReply = null;
  state.openingThinking = false;
  drawOpening(app);
  if (!moveState.need_reply) return;

  // Соперник отвечает отдельным запросом — доска уже показывает ход игрока.
  state.openingThinking = true;
  drawOpening(app);
  try {
    const replyState = await api('/api/opening/reply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: s.id, human: !!state.openingHuman }),
    });
    if (state.openingFens) state.openingFens.push(replyState.fen);
    state.openingSession = replyState;
    resetOpeningTheory();
    state.openingLastReply = replyState.last_reply || null;
  } catch (err) {
    if (isSessionError(err)) { handleSessionExpired(); return; }
    state.openingError = err.message;
    showToast(err.message, 'error');
  } finally {
    state.openingThinking = false;
    drawOpening(app);
  }
}

async function openingAnalyze(app) {
  const s = state.openingSession;
  if (!s) return;
  state.openingAnalyzing = true;
  state.openingError = null;
  drawOpening(app);
  try {
    const res = await api('/api/opening/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: s.id, human: !!state.openingHuman }),
    });
    state.openingJobId = res.job_id;
    openingPoll(app, res.job_id);
  } catch (err) {
    if (isSessionError(err)) { handleSessionExpired(); return; }
    state.openingAnalyzing = false;
    state.openingJobId = null;
    state.openingError = err.message;
    showToast(err.message, 'error');
    drawOpening(app);
  }
}

async function openingRecover(app, fallbackMessage) {
  if (state.timers.opening) { clearInterval(state.timers.opening); state.timers.opening = null; }
  state.openingAnalyzing = false;
  state.openingJobId = null;
  const s = state.openingSession;
  if (s && s.id) {
    try {
      const fresh = await api('/api/opening/' + encodeURIComponent(s.id));
      state.openingSession = fresh;
      drawOpening(app);
      if (fresh.report) return;
      state.openingError = fallbackMessage;
      drawOpening(app);
      return;
    } catch (err) {
      if (isSessionError(err)) { handleSessionExpired(); return; }
    }
  }
  state.openingError = 'Не удалось получить отчёт';
  drawOpening(app);
}

function openingPoll(app, jobId) {
  if (state.timers.opening) clearInterval(state.timers.opening);
  state.timers.opening = setInterval(async () => {
    if (state.currentView !== 'opening') {
      clearInterval(state.timers.opening);
      state.timers.opening = null;
      return;
    }
    let job;
    try {
      job = await api('/api/jobs/' + jobId);
    } catch {
      await openingRecover(app, 'Анализ прерван — попробуй запустить разбор ещё раз');
      return;
    }
    const status = document.getElementById('opening-analysis-status');
    if (status) status.textContent = (job.phase || 'Анализ') + (job.total ? ` · ${job.done}/${job.total}` : '');
    if (job.status === 'done') {
      clearInterval(state.timers.opening);
      state.timers.opening = null;
      state.openingAnalyzing = false;
      state.openingJobId = null;
      await loadOpeningSession(app, state.openingSession.id);
    } else if (job.status === 'error') {
      await openingRecover(app, job.error || 'Анализ прерван — попробуй запустить разбор ещё раз');
    }
  }, 800);
}

function openingReportHtml(report) {
  const opening = report.opening || {};
  const name = opening.name || '—';
  const eco = opening.eco || '';
  const moments = report.moments || [];
  const kindBadge = k => k === 'good' ? 'badge-good' : k === 'bad' ? 'badge-bad' : 'badge-neutral';
  const kindLabel = k => OPENING_KIND_RU[k] || (k ? esc(k) : '—');
  const momentsHtml = moments.length ? moments.map(m => `
    <div class="opening-report-moment">
      <div>
        <span class="drill-san">${esc(m.san || '—')}</span>
        <span class="badge ${kindBadge(m.kind)}">${kindLabel(m.kind)}</span>
        ${m.ply ? `<span style="color:var(--c-text-muted);font-size:12px">полуход ${esc(String(m.ply))}</span>` : ''}
      </div>
      ${m.text ? `<div style="margin-top:4px">${esc(m.text)}</div>` : ''}
      ${m.better && m.better !== '—' ? `<div style="margin-top:2px;font-size:13px">лучше: <span class="drill-best">${esc(m.better)}</span></div>` : ''}
    </div>`).join('') : '<p style="color:var(--c-text-muted)">Моментов нет.</p>';
  return `
    <div class="card">
      <div class="card-header"><span class="card-title">Отчёт тренера</span><span class="badge badge-good">Stockfish + LLM</span></div>
      <div style="font-size:15px;font-weight:600;margin-bottom:6px">${esc(name)}${eco ? ` <span class="badge badge-neutral">${esc(eco)}</span>` : ''}</div>
      ${report.summary ? `<div class="mentor-reply">${esc(report.summary)}</div>` : ''}
      <h3 style="font-size:14px;margin:12px 0 8px">Ключевые моменты</h3>
      ${momentsHtml}
      ${report.opponent ? `<h3 style="font-size:14px;margin:12px 0 6px">Соперник</h3><p style="margin:0">${esc(report.opponent)}</p>` : ''}
      ${report.recommendation ? `<h3 style="font-size:14px;margin:12px 0 6px">Рекомендация</h3><p style="margin:0">${esc(report.recommendation)}</p>` : ''}
    </div>`;
}

const MIDDLEGAME_STORAGE_KEY = 'middlegameSessionId';
const MIDDLEGAME_JOB_KEY = 'middlegameAnalysisJob';
const MIDDLEGAME_MIN_PLAN = 20;
const MIDDLEGAME_MAX_PLIES = 60;
const MIDDLEGAME_STRENGTH_LABELS = { equal: 'равный', plus100: '+100', plus200: '+200', hybrid: 'Maia+' };
const MIDDLEGAME_VERDICTS = {
  good: { label: 'План доказан', badge: 'badge-good' },
  partial: { label: 'Частично', badge: 'badge-neutral' },
  wrong: { label: 'План не сработал', badge: 'badge-bad' },
};
const MIDDLEGAME_FILES = 'abcdefgh';
const MIDDLEGAME_KNIGHT_DELTAS = [[1, 2], [2, 1], [2, -1], [1, -2], [-1, -2], [-2, -1], [-2, 1], [-1, 2]];
const MIDDLEGAME_KING_DELTAS = [[1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0], [-1, -1], [0, -1], [1, -1]];
const MIDDLEGAME_ROOK_DIRS = [[1, 0], [-1, 0], [0, 1], [0, -1]];
const MIDDLEGAME_BISHOP_DIRS = [[1, 1], [1, -1], [-1, 1], [-1, -1]];

function mgSquare(index) {
  return MIDDLEGAME_FILES[index % 8] + (8 - Math.floor(index / 8));
}

function mgIndex(square) {
  if (!square || square.length < 2) return -1;
  const file = MIDDLEGAME_FILES.indexOf(square[0]);
  const rank = Number(square[1]);
  if (file < 0 || !rank || rank < 1 || rank > 8) return -1;
  return (8 - rank) * 8 + file;
}

function mgColor(piece) {
  if (!piece) return null;
  return piece === piece.toUpperCase() ? 'w' : 'b';
}

function mgParseFen(fen) {
  const parts = String(fen || '').trim().split(/\s+/);
  const board = new Array(64).fill('');
  let index = 0;
  for (const ch of (parts[0] || '')) {
    if (ch === '/') continue;
    if (ch >= '1' && ch <= '8') { index += Number(ch); continue; }
    if (index < 64) board[index++] = ch;
  }
  return {
    board,
    turn: parts[1] === 'b' ? 'b' : 'w',
    castling: parts[2] && parts[2] !== '-' ? parts[2] : '',
    ep: parts[3] && parts[3] !== '-' ? parts[3] : '',
    half: Number(parts[4]) || 0,
    full: Number(parts[5]) || 1,
  };
}

function mgFen(state) {
  let placement = '';
  for (let row = 0; row < 8; row++) {
    let empty = 0;
    for (let file = 0; file < 8; file++) {
      const piece = state.board[row * 8 + file];
      if (!piece) { empty++; continue; }
      if (empty) { placement += String(empty); empty = 0; }
      placement += piece;
    }
    if (empty) placement += String(empty);
    if (row < 7) placement += '/';
  }
  return `${placement} ${state.turn} ${state.castling || '-'} ${state.ep || '-'} ${state.half || 0} ${state.full || 1}`;
}

function mgAttacked(state, target, by) {
  const board = state.board;
  const targetFile = target % 8;
  const targetRow = Math.floor(target / 8);
  const pawnRow = targetRow + (by === 'w' ? 1 : -1);
  if (pawnRow >= 0 && pawnRow < 8) {
    const pawn = by === 'w' ? 'P' : 'p';
    for (const delta of [-1, 1]) {
      const file = targetFile + delta;
      if (file < 0 || file > 7) continue;
      if (board[pawnRow * 8 + file] === pawn) return true;
    }
  }
  const knight = by === 'w' ? 'N' : 'n';
  for (const [df, dr] of MIDDLEGAME_KNIGHT_DELTAS) {
    const file = targetFile + df;
    const row = targetRow + dr;
    if (file < 0 || file > 7 || row < 0 || row > 7) continue;
    if (board[row * 8 + file] === knight) return true;
  }
  const king = by === 'w' ? 'K' : 'k';
  for (const [df, dr] of MIDDLEGAME_KING_DELTAS) {
    const file = targetFile + df;
    const row = targetRow + dr;
    if (file < 0 || file > 7 || row < 0 || row > 7) continue;
    if (board[row * 8 + file] === king) return true;
  }
  const ray = (directions, pieces) => {
    for (const [df, dr] of directions) {
      let file = targetFile + df;
      let row = targetRow + dr;
      while (file >= 0 && file <= 7 && row >= 0 && row <= 7) {
        const piece = board[row * 8 + file];
        if (piece) {
          if (mgColor(piece) === by && pieces.includes(piece.toLowerCase())) return true;
          break;
        }
        file += df;
        row += dr;
      }
    }
    return false;
  };
  if (ray(MIDDLEGAME_ROOK_DIRS, 'rq')) return true;
  return ray(MIDDLEGAME_BISHOP_DIRS, 'bq');
}

function mgInCheck(state, color) {
  const king = state.board.indexOf(color === 'w' ? 'K' : 'k');
  if (king < 0) return false;
  return mgAttacked(state, king, color === 'w' ? 'b' : 'w');
}

function mgKingSquare(state, color) {
  const king = state.board.indexOf(color === 'w' ? 'K' : 'k');
  return king >= 0 ? mgSquare(king) : null;
}

function mgPseudoMoves(state) {
  const moves = [];
  const add = (from, to, extra) => {
    moves.push(Object.assign({ from, to, promotion: null, castle: null, ep: false }, extra || {}));
  };
  for (let from = 0; from < 64; from++) {
    const piece = state.board[from];
    if (!piece || mgColor(piece) !== state.turn) continue;
    const kind = piece.toLowerCase();
    const file = from % 8;
    const row = Math.floor(from / 8);
    if (kind === 'p') {
      const direction = state.turn === 'w' ? -8 : 8;
      const startRow = state.turn === 'w' ? 6 : 1;
      const promoRow = state.turn === 'w' ? 0 : 7;
      const one = from + direction;
      if (one >= 0 && one < 64 && !state.board[one]) {
        if (Math.floor(one / 8) === promoRow) {
          for (const promotion of ['q', 'r', 'b', 'n']) add(from, one, { promotion });
        } else {
          add(from, one);
        }
        const two = from + direction * 2;
        if (row === startRow && !state.board[two]) add(from, two);
      }
      for (const delta of [-1, 1]) {
        const targetFile = file + delta;
        if (targetFile < 0 || targetFile > 7) continue;
        const to = from + direction + delta;
        if (to < 0 || to >= 64) continue;
        const target = state.board[to];
        if (target && mgColor(target) !== state.turn) {
          if (Math.floor(to / 8) === promoRow) {
            for (const promotion of ['q', 'r', 'b', 'n']) add(from, to, { promotion });
          } else {
            add(from, to);
          }
        } else if (!target && state.ep && mgSquare(to) === state.ep) {
          add(from, to, { ep: true });
        }
      }
    } else if (kind === 'n' || kind === 'k') {
      const deltas = kind === 'n' ? MIDDLEGAME_KNIGHT_DELTAS : MIDDLEGAME_KING_DELTAS;
      for (const [df, dr] of deltas) {
        const targetFile = file + df;
        const targetRow = row + dr;
        if (targetFile < 0 || targetFile > 7 || targetRow < 0 || targetRow > 7) continue;
        const to = targetRow * 8 + targetFile;
        const target = state.board[to];
        if (!target || mgColor(target) !== state.turn) add(from, to);
      }
    } else {
      const directions = kind === 'r'
        ? MIDDLEGAME_ROOK_DIRS
        : kind === 'b'
          ? MIDDLEGAME_BISHOP_DIRS
          : MIDDLEGAME_ROOK_DIRS.concat(MIDDLEGAME_BISHOP_DIRS);
      for (const [df, dr] of directions) {
        let targetFile = file + df;
        let targetRow = row + dr;
        while (targetFile >= 0 && targetFile <= 7 && targetRow >= 0 && targetRow <= 7) {
          const to = targetRow * 8 + targetFile;
          const target = state.board[to];
          if (!target) {
            add(from, to);
          } else {
            if (mgColor(target) !== state.turn) add(from, to);
            break;
          }
          targetFile += df;
          targetRow += dr;
        }
      }
    }
  }
  const homeRow = state.turn === 'w' ? 7 : 0;
  const kingFrom = homeRow * 8 + 4;
  const kingPiece = state.turn === 'w' ? 'K' : 'k';
  if (state.board[kingFrom] === kingPiece) {
    const enemy = state.turn === 'w' ? 'b' : 'w';
    const kingSide = state.turn === 'w' ? 'K' : 'k';
    const queenSide = state.turn === 'w' ? 'Q' : 'q';
    const rookPiece = state.turn === 'w' ? 'R' : 'r';
    if (state.castling.includes(kingSide) && state.board[homeRow * 8 + 7] === rookPiece &&
        !state.board[homeRow * 8 + 5] && !state.board[homeRow * 8 + 6] &&
        !mgAttacked(state, kingFrom, enemy) && !mgAttacked(state, homeRow * 8 + 5, enemy) &&
        !mgAttacked(state, homeRow * 8 + 6, enemy)) {
      add(kingFrom, homeRow * 8 + 6, { castle: 'K' });
    }
    if (state.castling.includes(queenSide) && state.board[homeRow * 8] === rookPiece &&
        !state.board[homeRow * 8 + 1] && !state.board[homeRow * 8 + 2] && !state.board[homeRow * 8 + 3] &&
        !mgAttacked(state, kingFrom, enemy) && !mgAttacked(state, homeRow * 8 + 3, enemy) &&
        !mgAttacked(state, homeRow * 8 + 2, enemy)) {
      add(kingFrom, homeRow * 8 + 2, { castle: 'Q' });
    }
  }
  return moves;
}

function mgApply(state, move) {
  const next = {
    board: state.board.slice(),
    turn: state.turn === 'w' ? 'b' : 'w',
    castling: state.castling || '',
    ep: '',
    half: state.half || 0,
    full: state.full || 1,
  };
  const piece = next.board[move.from];
  const color = mgColor(piece);
  const kind = piece.toLowerCase();
  const captured = !!(next.board[move.to] || move.ep);
  next.board[move.from] = '';
  if (move.ep) {
    const capturedSquare = move.to + (color === 'w' ? 8 : -8);
    next.board[capturedSquare] = '';
  }
  if (move.castle) {
    const row = color === 'w' ? 7 : 0;
    if (move.castle === 'K') {
      next.board[row * 8 + 5] = next.board[row * 8 + 7];
      next.board[row * 8 + 7] = '';
    } else {
      next.board[row * 8 + 3] = next.board[row * 8];
      next.board[row * 8] = '';
    }
  }
  next.board[move.to] = move.promotion
    ? (color === 'w' ? move.promotion.toUpperCase() : move.promotion)
    : piece;
  let rights = next.castling;
  for (const square of [mgSquare(move.from), mgSquare(move.to)]) {
    if (square === 'e1') rights = rights.replace(/[KQ]/g, '');
    else if (square === 'e8') rights = rights.replace(/[kq]/g, '');
    else if (square === 'a1') rights = rights.replace(/Q/g, '');
    else if (square === 'h1') rights = rights.replace(/K/g, '');
    else if (square === 'a8') rights = rights.replace(/q/g, '');
    else if (square === 'h8') rights = rights.replace(/k/g, '');
  }
  next.castling = rights;
  if (kind === 'p' && Math.abs(move.to - move.from) === 16) {
    next.ep = mgSquare((move.from + move.to) / 2);
  }
  next.half = (kind === 'p' || captured) ? 0 : next.half + 1;
  next.full = state.turn === 'b' ? (state.full || 1) + 1 : (state.full || 1);
  return next;
}

function mgLegalMoves(state) {
  const legal = [];
  for (const move of mgPseudoMoves(state)) {
    const next = mgApply(state, move);
    if (!mgInCheck(next, state.turn)) legal.push(move);
  }
  return legal;
}

function mgFindMove(legal, uci) {
  if (!uci || uci.length < 4) return null;
  const from = mgIndex(uci.slice(0, 2));
  const to = mgIndex(uci.slice(2, 4));
  const promotion = uci.length > 4 ? uci[4].toLowerCase() : null;
  return legal.find(m => m.from === from && m.to === to && (m.promotion || null) === promotion) ||
    legal.find(m => m.from === from && m.to === to) || null;
}

function mgMoveUci(move) {
  return mgSquare(move.from) + mgSquare(move.to) + (move.promotion || '');
}

function mgSan(state, move, legal) {
  const piece = state.board[move.from];
  const kind = piece.toLowerCase();
  let san;
  if (move.castle) {
    san = move.castle === 'K' ? 'O-O' : 'O-O-O';
  } else if (kind === 'p') {
    san = '';
    if (state.board[move.to] || move.ep) san += MIDDLEGAME_FILES[move.from % 8] + 'x';
    san += mgSquare(move.to);
    if (move.promotion) san += '=' + move.promotion.toUpperCase();
  } else {
    san = kind.toUpperCase();
    const otherFroms = legal.filter(m =>
      m.to === move.to && m.from !== move.from &&
      state.board[m.from] && state.board[m.from].toLowerCase() === kind &&
      mgColor(state.board[m.from]) === state.turn
    ).map(m => m.from);
    if (otherFroms.length) {
      const sameFile = otherFroms.some(from => from % 8 === move.from % 8);
      const sameRow = otherFroms.some(from => Math.floor(from / 8) === Math.floor(move.from / 8));
      if (!sameFile) san += MIDDLEGAME_FILES[move.from % 8];
      else if (!sameRow) san += String(8 - Math.floor(move.from / 8));
      else san += mgSquare(move.from);
    }
    if (state.board[move.to]) san += 'x';
    san += mgSquare(move.to);
  }
  const after = mgApply(state, move);
  if (mgInCheck(after, after.turn)) {
    san += mgLegalMoves(after).length ? '+' : '#';
  }
  return san;
}

function mgReplayMoves(fen, moves) {
  let state = mgParseFen(fen);
  const fens = [mgFen(state)];
  const sans = [];
  for (const uci of moves || []) {
    const legal = mgLegalMoves(state);
    const move = mgFindMove(legal, uci);
    if (!move) break;
    sans.push(mgSan(state, move, legal));
    state = mgApply(state, move);
    fens.push(mgFen(state));
  }
  const legal = mgLegalMoves(state);
  return { state, fens, sans, legal, fen: mgFen(state) };
}

function middlegameMoveRowsHtml(moves, sans) {
  return openingMoveRowsHtml((moves || []).map((uci, index) => (sans && sans[index]) || uci));
}

function readMiddlegameSessionId() {
  try { return localStorage.getItem(MIDDLEGAME_STORAGE_KEY); } catch { return null; }
}

function storeMiddlegameSessionId(id) {
  if (!id) return;
  try { localStorage.setItem(MIDDLEGAME_STORAGE_KEY, id); } catch {}
}

function clearMiddlegameSessionId() {
  try { localStorage.removeItem(MIDDLEGAME_STORAGE_KEY); } catch {}
}

function readMiddlegameJob() {
  try {
    const saved = JSON.parse(localStorage.getItem(MIDDLEGAME_JOB_KEY) || 'null');
    if (saved && saved.sessionId && saved.jobId) return saved;
  } catch {}
  return null;
}

function storeMiddlegameJob(sessionId, jobId) {
  if (!sessionId || !jobId) return;
  try { localStorage.setItem(MIDDLEGAME_JOB_KEY, JSON.stringify({ sessionId, jobId })); } catch {}
}

function clearMiddlegameJob() {
  try { localStorage.removeItem(MIDDLEGAME_JOB_KEY); } catch {}
}

function middlegameTotal() {
  return (state.middlegameMeta && state.middlegameMeta.total) || 64;
}

function middlegameSourceTitle(session) {
  return (session && session.source && session.source.title) || 'Curacao Candidates 1962';
}

function middlegameResultLabel(result) {
  if (result === '1-0') return '1-0 — победа белых';
  if (result === '0-1') return '0-1 — победа чёрных';
  if (result === '1/2-1/2') return '1/2-1/2 — ничья';
  if (result === '*') return '* — лимит полуходов';
  return result || '';
}

function middlegamePlanHtml(plan) {
  if (!plan) return '';
  return `<details class="middlegame-plan"><summary>Мой план</summary><div class="middlegame-plan-text">${esc(plan)}</div></details>`;
}

async function viewMiddlegame(app) {
  if (!state.user) {
    app.innerHTML = '<div class="onboarding card"><div class="empty-icon">♞</div><h1>Миттельшпиль</h1><p>Профиль не выбран — выбери его на странице Профили.</p><a href="#" class="btn btn-primary" data-view="users">Профили</a></div>';
    bindLinks(app);
    return;
  }
  if (state.middlegameSession && state.middlegameSession.id) {
    await loadMiddlegameSession(app, state.middlegameSession.id);
    return;
  }
  const stored = readMiddlegameSessionId();
  if (stored) {
    await loadMiddlegameSession(app, stored);
    return;
  }
  await createMiddlegameSession(app);
}

async function createMiddlegameSession(app) {
  if (state.timers.middlegame) { clearInterval(state.timers.middlegame); state.timers.middlegame = null; }
  clearMiddlegameJob();
  state.middlegameSession = null;
  state.middlegameThinking = false;
  state.middlegameAnalyzing = false;
  state.middlegameJobId = null;
  state.middlegameError = null;
  state.middlegameOptimistic = null;
  state.middlegamePlanDraft = '';
  state.middlegameStrength = 'equal';
  app.innerHTML = '<div class="card"><p>Выбираю позицию миттельшпиля...</p></div>';
  try {
    const [res, meta] = await Promise.all([
      api('/api/middlegame/session', { method: 'POST' }),
      api('/api/middlegame/meta').catch(() => null),
    ]);
    if (!res || !res.session) throw new Error('Сервер не вернул позицию');
    state.middlegameSession = res.session;
    state.middlegameMeta = meta;
    storeMiddlegameSessionId(res.session.id);
    drawMiddlegame(app);
  } catch (err) {
    if (isSessionError(err)) { handleSessionExpired(); return; }
    if (state.currentView !== 'middlegame') return;
    app.innerHTML = `
      <div class="card-header"><h1>Миттельшпиль</h1></div>
      <div class="alert alert-error">${esc(err.message)}</div>
      <button class="btn btn-primary" id="middlegame-retry">Попробовать ещё раз</button>`;
    const retry = document.getElementById('middlegame-retry');
    if (retry) retry.addEventListener('click', () => createMiddlegameSession(app));
  }
}

async function loadMiddlegameSession(app, id) {
  let session;
  try {
    const res = await api('/api/middlegame/session/' + encodeURIComponent(id));
    session = res && res.session;
    if (!session || !session.id) throw new Error('Сервер не вернул сессию');
  } catch (err) {
    if (isSessionError(err)) { handleSessionExpired(); return; }
    if (err.status === 404) {
      clearMiddlegameSessionId();
      state.middlegameSession = null;
      await createMiddlegameSession(app);
      return;
    }
    if (state.currentView !== 'middlegame') return;
    app.innerHTML = `
      <div class="card-header"><h1>Миттельшпиль</h1></div>
      <div class="alert alert-error">${esc(err.message)}</div>
      <div class="games-filter">
        <button class="btn btn-primary" id="middlegame-retry-load">Повторить загрузку</button>
        <button class="btn btn-secondary" id="middlegame-new-from-error">Новая позиция</button>
      </div>`;
    const retry = document.getElementById('middlegame-retry-load');
    if (retry) retry.addEventListener('click', () => loadMiddlegameSession(app, id));
    const fresh = document.getElementById('middlegame-new-from-error');
    if (fresh) fresh.addEventListener('click', () => {
      clearMiddlegameSessionId();
      state.middlegameSession = null;
      createMiddlegameSession(app);
    });
    return;
  }
  state.middlegameSession = session;
  state.middlegameError = null;
  state.middlegameOptimistic = null;
  storeMiddlegameSessionId(session.id);
  const savedJob = readMiddlegameJob();
  if (savedJob && savedJob.sessionId !== session.id) clearMiddlegameJob();
  if (savedJob && savedJob.sessionId === session.id && !session.report && session.status !== 'plan') {
    state.middlegameAnalyzing = true;
    state.middlegameJobId = savedJob.jobId;
  } else {
    if (session.report || session.status === 'plan') clearMiddlegameJob();
    state.middlegameAnalyzing = false;
    state.middlegameJobId = null;
  }
  drawMiddlegame(app);
  if (state.middlegameAnalyzing && !state.timers.middlegame && state.middlegameJobId) {
    middlegamePoll(app, state.middlegameJobId);
    return;
  }
  if (state.currentView === 'middlegame' && !session.report && session.status === 'playing' && !session.result) {
    const replay = mgReplayMoves(session.fen, session.moves || []);
    if (replay.state.turn === 'b' && replay.legal.length && !state.middlegameThinking) {
      middlegameReply(app);
    }
  }
}

function drawMiddlegame(app) {
  if (state.currentView !== 'middlegame') return;
  const session = state.middlegameSession;
  if (!session) { app.innerHTML = '<div class="card"><p>Загрузка...</p></div>'; return; }
  if (session.status === 'plan') { drawMiddlegameSetup(app); return; }
  drawMiddlegameGame(app);
}

function drawMiddlegameSetup(app) {
  if (state.timers.middlegame) { clearInterval(state.timers.middlegame); state.timers.middlegame = null; }
  state.middlegameThinking = false;
  const session = state.middlegameSession;
  if (!session) return;
  const replay = mgReplayMoves(session.fen, session.moves || []);
  const plan = state.middlegamePlanDraft || session.plan || '';
  const strength = MIDDLEGAME_STRENGTH_LABELS[state.middlegameStrength] ? state.middlegameStrength : 'equal';
  const total = middlegameTotal();
  const title = middlegameSourceTitle(session);
  const ready = plan.trim().length >= MIDDLEGAME_MIN_PLAN;
  const played = state.middlegameMeta && state.middlegameMeta.played;

  app.innerHTML = `
    <div class="card-header"><h1>Миттельшпиль</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    ${state.middlegameError ? `<div class="alert alert-error">${esc(state.middlegameError)}</div>` : ''}
    <div class="grid game-layout">
      <div class="card">
        ${boardHtml(replay.fen, [], false)}
        <div class="middlegame-position-line">${esc(title)} • позиция ${esc(String(session.n || 0))} из ${total}</div>
        <div class="board-meta">
          <span class="badge badge-blue">ты: белые</span>
          <span class="badge badge-neutral">соперник: Maia</span>
          ${played != null ? `<span class="badge badge-neutral">пройдено: ${esc(String(played))} / ${total}</span>` : ''}
        </div>
      </div>
      <div class="game-side">
        <div class="card">
          <div class="card-header"><span class="card-title">План на позицию</span><span class="badge badge-neutral" id="middlegame-plan-count">${plan.trim().length} / ${MIDDLEGAME_MIN_PLAN}+</span></div>
          <form id="middlegame-plan-form">
            <div class="form-group">
              <label>Твой план</label>
              <textarea class="middlegame-plan-input" id="middlegame-plan-input" name="plan" maxlength="4000" placeholder="Опиши план: за счёт чего позиция лучше/хуже и конкретные ходы-идеи (напр. Ne5, Qh3)">${esc(plan)}</textarea>
            </div>
            <div class="form-group">
              <label>Соперник</label>
              <div class="games-filter" id="middlegame-strength">
                ${Object.keys(MIDDLEGAME_STRENGTH_LABELS).map(key => {
                  const disabled = (key !== 'equal' && key !== 'hybrid') && ((state.middlegameMeta && state.middlegameMeta.base_elo >= 2000) || false);
                  const title = key === 'equal'
                    ? 'Соперник того же уровня: рейтинг равен твоему'
                    : key === 'hybrid'
                      ? 'Гибрид: Maia предлагает человеческие ходы, Stockfish отсекает зевки (сильнее ~2200-2400)'
                      : (disabled
                          ? 'Maia не различает силы выше 2000 Elo: для твоего рейтинга +100/+200 недоступны'
                          : (key === 'plus100' ? 'Соперник на 100 Elo выше твоего рейтинга' : 'Соперник на 200 Elo выше твоего рейтинга'));
                  return `<button type="button" class="btn btn-secondary ${key === strength ? 'btn-active' : ''}" data-strength="${esc(key)}" title="${esc(title)}" ${disabled ? 'disabled style="opacity:0.45;cursor:not-allowed"' : ''}>${esc(MIDDLEGAME_STRENGTH_LABELS[key])}</button>`;
                }).join('')}
              </div>
              <div style="font-size:12px;color:var(--c-text-muted);margin-top:4px">Число — разница Elo соперника относительно твоего рейтинга (база из профиля или значение по умолчанию): +100/+200 = соперник сильнее на 100/200 пунктов. Используется для выбора силы Maia.</div>
              ${state.middlegameMeta && state.middlegameMeta.base_elo >= 2000 ? '<div style="font-size:12px;color:var(--c-text-muted);margin-top:4px">Твой рейтинг ≥ 2000: Maia не различает силы выше 2000 Elo, доступен только равный уровень.</div>' : ''}
              <div class="form-group" style="margin-top:8px">
                <label>Скорость соперника</label>
                <div class="games-filter">
                  <button type="button" class="btn btn-secondary ${state.middlegameHuman ? '' : 'btn-active'}" data-speed="fast">Быстро</button>
                  <button type="button" class="btn btn-secondary ${state.middlegameHuman ? 'btn-active' : ''}" data-speed="human">Как человек</button>
                </div>
              </div>
            </div>
            <div class="alert alert-error" id="middlegame-setup-err" style="display:none"></div>
            <button type="submit" class="btn btn-primary" id="middlegame-start" ${ready ? '' : 'disabled'}>Начать партию</button>
          </form>
        </div>
      </div>
    </div>`;

  bindBoard(app.querySelector('.board'));
  const input = document.getElementById('middlegame-plan-input');
  const start = document.getElementById('middlegame-start');
  const counter = document.getElementById('middlegame-plan-count');
  if (input) {
    input.addEventListener('input', () => {
      state.middlegamePlanDraft = input.value;
      const length = input.value.trim().length;
      if (counter) counter.textContent = `${length} / ${MIDDLEGAME_MIN_PLAN}+`;
      if (start) start.disabled = length < MIDDLEGAME_MIN_PLAN;
    });
  }
  app.querySelectorAll('#middlegame-strength [data-strength]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      state.middlegameStrength = btn.dataset.strength;
      app.querySelectorAll('#middlegame-strength [data-strength]').forEach(b => b.classList.toggle('btn-active', b === btn));
    });
  });
  app.querySelectorAll('[data-speed]').forEach(btn => {
    if (btn.closest('#middlegame-strength')) return;
    btn.addEventListener('click', () => {
      state.middlegameHuman = btn.dataset.speed === 'human';
      app.querySelectorAll('[data-speed]').forEach(b => { if (!b.closest('#middlegame-strength')) b.classList.toggle('btn-active', b === btn); });
    });
  });

  const form = document.getElementById('middlegame-plan-form');
  if (form) form.addEventListener('submit', async e => {
    e.preventDefault();
    const errBox = document.getElementById('middlegame-setup-err');
    if (errBox) errBox.style.display = 'none';
    const text = input ? input.value.trim() : '';
    if (text.length < MIDDLEGAME_MIN_PLAN) {
      if (input) state.middlegamePlanDraft = input.value;
      return;
    }
    if (start) start.disabled = true;
    try {
      const res = await api('/api/middlegame/session/' + encodeURIComponent(session.id) + '/plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan: text, strength: state.middlegameStrength }),
      });
      state.middlegameSession = res.session;
      state.middlegamePlanDraft = text;
      state.middlegameError = null;
      state.middlegameThinking = false;
      storeMiddlegameSessionId(res.session.id);
      drawMiddlegame(app);
    } catch (err) {
      if (isSessionError(err)) { handleSessionExpired(); return; }
      if (start) start.disabled = false;
      if (errBox) {
        errBox.textContent = err.message;
        errBox.style.display = 'block';
      }
    }
  });
}

function drawMiddlegameGame(app) {
  const session = state.middlegameSession;
  if (!session) { app.innerHTML = '<div class="card"><p>Загрузка...</p></div>'; return; }
  const moves = session.moves || [];
  const replay = mgReplayMoves(session.fen, moves);
  const total = middlegameTotal();
  const title = middlegameSourceTitle(session);
  const report = session.report;
  const finished = !!session.result || replay.legal.length === 0 || moves.length >= MIDDLEGAME_MAX_PLIES;
  const yourTurn = !finished && session.status === 'playing' && replay.state.turn === 'w';
  // Последний ход берём из UCI: по разнице FEN не видно взятия на занятом поле
  // (фигура на клетке назначения просто меняется), а анимация нужна и там.
  const lastUci = moves.length ? String(moves[moves.length - 1]) : '';
  const lastMove = /^[a-h][1-8][a-h][1-8]/.test(lastUci)
    ? [lastUci.slice(0, 2), lastUci.slice(2, 4)]
    : (replay.fens.length > 1
      ? lastMoveSquares(replay.fens[replay.fens.length - 2], replay.fens[replay.fens.length - 1])
      : []);
  // Пока сервер не подтвердил ход, рисуем его локально — без «мигания» фигуры назад.
  const optimistic = state.middlegameOptimistic;
  const boardFen = optimistic ? optimistic.fen : replay.fen;
  const boardLastMove = optimistic ? optimistic.lastMove : lastMove;
  // Анимируем только чужой ход: свой уже проехал под курсором или в оптимистичной отрисовке.
  const animateBoard = optimistic
    ? optimistic.animate !== false
    : (!state.middlegameThinking && moves.length % 2 === 0);
  const checkState = mgParseFen(boardFen);
  const checkSquare = mgInCheck(checkState, checkState.turn) ? mgKingSquare(checkState, checkState.turn) : null;
  const errorMsg = state.middlegameError || (session.status === 'error' ? session.error : null);
  const resultBadge = session.result === '1-0' ? 'badge-good' : session.result === '0-1' ? 'badge-bad' : 'badge-neutral';

  let panel;
  if (report) {
    panel = middlegameReportHtml(report, session.source || {}, session.plan);
  } else if (finished) {
    panel = `
      <div class="card">
        <div class="card-header"><span class="card-title">Партия завершена</span><span class="badge badge-neutral">${moves.length} / ${MIDDLEGAME_MAX_PLIES}</span></div>
        <p style="color:var(--c-text-muted);font-size:13px;margin-top:0">${esc(middlegameResultLabel(session.result) || 'Позиция доиграна.')} Запусти разбор — Stockfish + LLM проверят твой план.</p>
        <button class="btn btn-primary" id="middlegame-analyze" ${state.middlegameAnalyzing ? 'disabled' : ''}>${state.middlegameAnalyzing ? 'Анализирую…' : 'Анализировать'}</button>
        <div id="middlegame-analysis-status" style="color:var(--c-text-muted);font-size:13px;margin-top:8px">${state.middlegameAnalyzing ? 'Анализ…' : ''}</div>
      </div>`;
  } else {
    panel = `
      <div class="card">
        <div class="card-header"><span class="card-title">Твой ход</span></div>
        ${state.middlegameThinking
          ? '<p style="color:var(--c-text-muted);font-size:13px;margin-top:0">соперник думает…</p>'
          : yourTurn
            ? '<p style="color:var(--c-text-muted);font-size:13px;margin-top:0">кликни или перетащи фигуру — точки показывают доступные поля; ПКМ-протяжка рисует стрелку</p>'
            : '<p style="color:var(--c-text-muted);font-size:13px;margin-top:0">ждём соперника…</p>'}
        ${!yourTurn && !state.middlegameThinking ? '<button class="btn btn-primary" id="middlegame-resume">Продолжить</button>' : ''}
      </div>`;
  }

  app.innerHTML = `
    <div class="card-header"><h1>Миттельшпиль</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    ${errorMsg ? `<div class="alert alert-error">${esc(errorMsg)}</div>` : ''}
    <div class="grid game-layout">
      <div class="card">
        ${boardHtml(boardFen, { lastMove: boardLastMove, animate: animateBoard, check: checkSquare })}
        <div class="board-meta">
          <span class="badge badge-neutral">${esc(title)}</span>
          <span class="badge badge-blue">позиция ${esc(String(session.n || 0))} из ${total}</span>
          <span class="badge badge-blue">ты: белые</span>
          <span class="badge badge-neutral">${moves.length} / ${MIDDLEGAME_MAX_PLIES}</span>
          ${session.result ? `<span class="badge ${resultBadge}">${esc(middlegameResultLabel(session.result))}</span>` : ''}
          ${state.middlegameThinking ? '<span class="badge badge-neutral">соперник думает…</span>' : ''}
        </div>
        ${middlegamePlanHtml(session.plan)}
        <div style="margin-top:12px"><button class="btn btn-secondary" id="middlegame-new">Новая позиция</button></div>
      </div>
      <div class="game-side">
        <div class="card">
          <div class="card-header"><span class="card-title">Ходы</span></div>
          <div class="moves-list">${middlegameMoveRowsHtml(moves, replay.sans)}</div>
        </div>
        ${panel}
      </div>
    </div>`;

  bindBoard(app.querySelector('.board'));
  const newBtn = document.getElementById('middlegame-new');
  if (newBtn) newBtn.addEventListener('click', () => {
    if (state.timers.middlegame) { clearInterval(state.timers.middlegame); state.timers.middlegame = null; }
    state.middlegameSession = null;
    state.middlegameThinking = false;
    state.middlegameAnalyzing = false;
    state.middlegameJobId = null;
    state.middlegameError = null;
    state.middlegameOptimistic = null;
    state.middlegamePlanDraft = '';
    state.middlegameStrength = 'equal';
    clearMiddlegameSessionId();
    createMiddlegameSession(app);
  });
  const analyzeBtn = document.getElementById('middlegame-analyze');
  if (analyzeBtn) analyzeBtn.addEventListener('click', () => middlegameAnalyze(app));
  const resumeBtn = document.getElementById('middlegame-resume');
  if (resumeBtn) resumeBtn.addEventListener('click', () => middlegameReply(app));
  if (state.middlegameAnalyzing && !state.timers.middlegame && state.middlegameJobId) {
    middlegamePoll(app, state.middlegameJobId);
  }
  if (!finished && !report && yourTurn && !state.middlegameThinking) {
    bindMiddlegameBoard(app, session.id, replay.legal);
  }
}

function bindMiddlegameBoard(app, sessionId, legalMoves) {
  const session = state.middlegameSession;
  if (!session || session.id !== sessionId || !legalMoves.length) return;
  const board = app.querySelector('.board');
  if (!board) return;
  bindBoard(board, {
    moves: legalMoves.map(m => ({
      from: mgSquare(m.from),
      to: mgSquare(m.to),
      promotion: m.promotion,
      uci: mgMoveUci(m),
    })),
    canMove: () => {
      const current = state.middlegameSession;
      return !!current && current.id === sessionId && !current.result && !state.middlegameThinking;
    },
    onMove: (move, meta) => submitMiddlegameMove(app, move.uci, { animate: !meta.dragged }),
  });
}

async function submitMiddlegameMove(app, uci, opts = {}) {
  const session = state.middlegameSession;
  if (!session) return;
  const replay = mgReplayMoves(session.fen, session.moves || []);
  const move = mgFindMove(replay.legal, uci);
  state.middlegameThinking = true;
  state.middlegameError = null;
  state.middlegameOptimistic = move ? {
    fen: mgFen(mgApply(replay.state, move)),
    lastMove: [mgSquare(move.from), mgSquare(move.to)],
    animate: opts.animate !== false,
  } : null;
  drawMiddlegame(app);
  let result;
  try {
    result = await api('/api/middlegame/session/' + encodeURIComponent(session.id) + '/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ move: uci }),
    });
  } catch (err) {
    state.middlegameThinking = false;
    state.middlegameOptimistic = null;
    if (isSessionError(err)) { handleSessionExpired(); return; }
    state.middlegameError = err.message;
    showToast(err.message, 'error');
    drawMiddlegame(app);
    return;
  }
  state.middlegameSession = result.session;
  state.middlegameThinking = false;
  state.middlegameOptimistic = null;
  storeMiddlegameSessionId(result.session.id);
  drawMiddlegame(app);
  if (result.need_reply) await middlegameReply(app);
}

async function middlegameReply(app) {
  const session = state.middlegameSession;
  if (!session) return;
  state.middlegameThinking = true;
  state.middlegameError = null;
  drawMiddlegame(app);
  let result;
  try {
    result = await api('/api/middlegame/session/' + encodeURIComponent(session.id) + '/reply' + (state.middlegameHuman ? '?human=1' : '') , { method: 'POST' });
  } catch (err) {
    state.middlegameThinking = false;
    if (isSessionError(err)) { handleSessionExpired(); return; }
    state.middlegameError = err.message;
    showToast(err.message, 'error');
    try {
      const fresh = await api('/api/middlegame/session/' + encodeURIComponent(session.id));
      state.middlegameSession = fresh;
      storeMiddlegameSessionId(fresh.id);
    } catch {}
    drawMiddlegame(app);
    return;
  }
  state.middlegameSession = result.session;
  state.middlegameThinking = false;
  storeMiddlegameSessionId(result.session.id);
  drawMiddlegame(app);
}

async function middlegameAnalyze(app) {
  const session = state.middlegameSession;
  if (!session) return;
  state.middlegameAnalyzing = true;
  state.middlegameError = null;
  drawMiddlegame(app);
  try {
    const res = await api('/api/middlegame/session/' + encodeURIComponent(session.id) + '/analyze', { method: 'POST' });
    if (!res || !res.job_id) throw new Error('Сервер не вернул задачу анализа');
    state.middlegameJobId = res.job_id;
    storeMiddlegameJob(session.id, res.job_id);
    middlegamePoll(app, res.job_id);
  } catch (err) {
    if (isSessionError(err)) { handleSessionExpired(); return; }
    state.middlegameAnalyzing = false;
    state.middlegameJobId = null;
    clearMiddlegameJob();
    state.middlegameError = err.status === 503 ? 'Сервер занят, попробуйте ещё раз' : err.message;
    showToast(state.middlegameError, 'error');
    drawMiddlegame(app);
  }
}

async function middlegameRecover(app, fallbackMessage) {
  if (state.timers.middlegame) { clearInterval(state.timers.middlegame); state.timers.middlegame = null; }
  state.middlegameAnalyzing = false;
  state.middlegameJobId = null;
  const session = state.middlegameSession;
  if (session && session.id) {
    try {
      const res = await api('/api/middlegame/session/' + encodeURIComponent(session.id));
      const fresh = res && res.session;
      if (!fresh || !fresh.id) throw new Error('Сервер не вернул сессию');
      state.middlegameSession = fresh;
      storeMiddlegameSessionId(fresh.id);
      drawMiddlegame(app);
      if (fresh.report) return;
      state.middlegameError = fallbackMessage;
      drawMiddlegame(app);
      return;
    } catch (err) {
      if (isSessionError(err)) { handleSessionExpired(); return; }
    }
  }
  state.middlegameError = 'Не удалось получить отчёт';
  drawMiddlegame(app);
}

function middlegamePoll(app, jobId) {
  if (state.timers.middlegame) clearInterval(state.timers.middlegame);
  state.timers.middlegame = setInterval(async () => {
    if (state.currentView !== 'middlegame') {
      clearInterval(state.timers.middlegame);
      state.timers.middlegame = null;
      return;
    }
    let job;
    try {
      job = await api('/api/jobs/' + jobId);
    } catch {
      await middlegameRecover(app, 'Анализ прерван — попробуй запустить разбор ещё раз');
      return;
    }
    const status = document.getElementById('middlegame-analysis-status');
    if (status) status.textContent = (job.phase || 'Анализ') + (job.total ? ` · ${job.done}/${job.total}` : '');
    if (job.status === 'done') {
      clearInterval(state.timers.middlegame);
      state.timers.middlegame = null;
      state.middlegameAnalyzing = false;
      state.middlegameJobId = null;
      const current = state.middlegameSession;
      if (current && current.id) await loadMiddlegameSession(app, current.id);
    } else if (job.status === 'error') {
      await middlegameRecover(app, job.error || 'Анализ прерван — попробуй запустить разбор ещё раз');
    }
  }, 800);
}

function middlegameReportHtml(report, source, plan) {
  const verdict = MIDDLEGAME_VERDICTS[report.verdict] || MIDDLEGAME_VERDICTS.partial;
  const moments = report.moments || [];
  const players = [source.white, source.black].filter(Boolean).join(' vs ');
  const sourceTitle = source.title || 'Curacao Candidates 1962';
  const sourceLine = players ? `${sourceTitle}: ${players}` : sourceTitle;
  const momentsHtml = moments.length ? moments.map(m => {
    const evalLine = (m.eval_before || m.eval_after)
      ? `<span>оценка: ${esc(m.eval_before || '—')} → ${esc(m.eval_after || '—')}</span>`
      : '';
    return `
      <div class="middlegame-moment">
        <div class="middlegame-moment-head">
          ${m.ply ? `<span class="badge badge-neutral">полуход ${esc(String(m.ply))}</span>` : ''}
          ${m.move_san ? `<span class="drill-san">${esc(m.move_san)}</span>` : ''}
          ${m.title ? `<span class="middlegame-moment-title">${esc(m.title)}</span>` : ''}
        </div>
        ${m.comment ? `<div>${esc(m.comment)}</div>` : ''}
        <div class="middlegame-moment-extra">
          ${m.alt_san ? `<span>лучше: <span class="drill-best">${esc(m.alt_san)}</span></span>` : ''}
          ${evalLine}
        </div>
      </div>`;
  }).join('') : '<p style="color:var(--c-text-muted)">Моментов нет.</p>';
  return `
    <div class="card">
      <div class="card-header"><span class="card-title">Отчёт тренера</span><span class="badge ${verdict.badge}">${esc(verdict.label)}</span></div>
      ${report.summary ? `<div class="mentor-reply">${esc(report.summary)}</div>` : ''}
      ${report.plan_comment ? `<h3 style="font-size:14px;margin:12px 0 6px">Разбор плана</h3><div class="mentor-reply">${esc(report.plan_comment)}</div>` : ''}
      <h3 style="font-size:14px;margin:12px 0 8px">Ключевые моменты</h3>
      ${momentsHtml}
      ${report.lesson ? `<h3 style="font-size:14px;margin:12px 0 6px">Урок</h3><div class="mentor-reply">${esc(report.lesson)}</div>` : ''}
      <div class="middlegame-report-source">${esc(sourceLine)}${source.chapter_url ? ` · <a href="${esc(safeHttp(source.chapter_url))}" target="_blank" rel="noopener">открыть главу источника</a>` : ''}</div>
      ${middlegamePlanHtml(plan)}
    </div>`;
}

async function viewCoach(app) {
  if (!state.user) {
    app.innerHTML = '<div class="onboarding card"><div class="empty-icon">🔬</div><h1>Нет профиля</h1><p>Выбери профиль на странице Профили.</p></div>';
    return;
  }

  let report = null;
  try { report = await api('/api/report'); } catch { /* no report yet */ }

  let reportHtml = '';
  if (report) {
    reportHtml = `
      <div class="grid grid-4" style="margin-bottom:12px">
        <div class="card stat"><div class="stat-value">${report.games_analyzed}</div><div class="stat-label">Проанализировано</div></div>
        <div class="card stat"><div class="stat-value">${fmt(report.score.score_pct)}%</div><div class="stat-label">Счёт</div></div>
        <div class="card stat"><div class="stat-value">${fmt(report.avg.avg_win_loss, 2)}</div><div class="stat-label" title="Средняя потеря win% (процентов) на ход игрока (метрика avg_win_loss из анализа)">Ср. потеря win%</div></div>
        <div class="card stat"><div class="stat-value">${report.total.blunders + report.total.mistakes}</div><div class="stat-label">Ошибок всего</div></div>
      </div>
      ${report.worst_games.length ? `<div class="card" style="margin-top:12px"><h3 style="font-size:14px;margin-bottom:8px">Худшие партии</h3><div class="table-wrap"><table style="font-size:12px"><thead><tr><th>Партия</th><th>Соперник</th><th>Результат</th><th>Ср. потеря</th><th>Win% до</th><th>Грубых</th></tr></thead><tbody>${report.worst_games.slice(0, 5).map(g => `<tr><td>${esc(g.game_id.slice(0, 8))}</td><td>${esc(g.opponent)}</td><td>${esc(g.result)}</td><td class="num">${fmt(g.avg_win_loss)}%</td><td class="num">${fmt(g.avg_win_before)}%</td><td class="num">${g.blunders}</td></tr>`).join('')}</tbody></table></div></div>` : ''}
      ${report.missed_wins.length ? `<div class="card" style="margin-top:12px"><h3 style="font-size:14px;margin-bottom:8px">Упущенные выигрыши (топ-5)</h3><div class="table-wrap"><table style="font-size:12px"><thead><tr><th>Партия</th><th>Ход</th><th>SAN</th><th>Win% до</th><th>Win% после</th><th>Лучший ход</th></tr></thead><tbody>${report.missed_wins.slice(0, 5).map(m => `<tr><td>${esc(m.game_id.slice(0, 8))}</td><td>${m.ply}</td><td class="drill-san">${esc(m.san)}</td><td class="num">${fmt(m.win_before)}%</td><td class="num">${fmt(m.win_after)}%</td><td class="drill-best">${esc(m.best_move_san)}</td></tr>`).join('')}</tbody></table></div></div>` : ''}
      <div class="card" style="margin-top:12px"><h3 style="font-size:14px;margin-bottom:8px">Тайм-тробль</h3><div class="grid grid-3"><div class="stat"><div class="stat-value">${report.time_pressure.moves_count}</div><div class="stat-label">Ходов в ТТ</div></div><div class="stat"><div class="stat-value">${report.time_pressure.blunders_in_tp}</div><div class="stat-label">Грубых в ТТ</div></div><div class="stat"><div class="stat-value">${fmt(report.time_pressure.tp_blunder_rate_pct)}%</div><div class="stat-label">% грубых в ТТ</div></div></div></div>`;
  } else {
    reportHtml = '<p style="color:var(--c-text-muted)">Пока нет отчёта. Запусти анализ выше.</p>';
  }

  app.innerHTML = `
    <div class="card-header"><h1>Анализ (Coach)</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="grid grid-2">
      <div class="card">
        <h3>Запустить анализ</h3>
        <form id="coach-form" class="form-row" style="flex-direction:column;gap:12px">
          <div class="form-row">
            <div class="form-group"><label>Макс. партий <input type="number" name="max" value="50" min="1" max="200"></label></div>
            <div class="form-group"><label>Тип <select name="perf"><option value="rapid" selected>rapid</option><option value="blitz">blitz</option><option value="classical">classical</option><option value="bullet">bullet</option></select></label></div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label for="coach-profile">Профиль анализа</label>
              <select name="profile" id="coach-profile">
                <option value="quick" selected>Быстро (10 / 1)</option>
                <option value="standard">Стандарт (12 / 2)</option>
                <option value="deep">Глубоко (14 / 3)</option>
              </select>
              <div class="form-hint">Быстро — рекомендуем для массового анализа; Глубоко — для разбора одной партии.</div>
            </div>
          </div>
          <div class="checkbox"><input type="checkbox" name="cached_only" id="cached_only"><label for="cached_only">Только из кеша (не ходить в сеть)</label></div>
          <div class="checkbox"><input type="checkbox" name="refresh" id="refresh"><label for="refresh">Переанализировать всё заново</label></div>
          <div class="card" style="background:var(--c-bg-soft);font-size:13px;line-height:1.6">
            <b>💡 Какой профиль выбрать?</b><br>
            <b>Быстро</b> (глубина 10, 1 линия) — 50 партий примерно за 2–5 минут.<br>
            <b>Стандарт</b> (глубина 12, 2 линии) — баланс скорости и точности.<br>
            <b>Глубоко</b> (глубина 14, 3 линии) — для разбора одной партии.<br>
            <span style="color:var(--c-text-muted)">Глубже 14 на облаке не уедет — для этого будет воркер на VPS.</span>
          </div>
          <button type="submit" class="btn btn-primary" style="width:fit-content">Запустить</button>
        </form>
      </div>
      <div class="card"><h3>Последний отчёт</h3>${reportHtml}</div>
    </div>
    <div id="job-container"></div>`;

  document.getElementById('coach-form').addEventListener('submit', async e => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type="submit"]');
    if (btn) {
      btn.disabled = true;
      btn.textContent = 'Запущено…';
    }
    const fd = new FormData(e.target);
    try {
      const result = await apiPost('/api/coach/run', fd);
      startGlobalJobPolling();
      showJob(app, result.job_id);
      document.getElementById('job-container').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = 'Запустить';
      }
      document.getElementById('job-container').innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
       showToast(err.message, 'error');
    }
  });

  // Если задача уже бежит (обновили страницу) — блокируем кнопку и цепляем polling.
  // Ретраим 3 раза: одиночный сетевой чих не должен разблокировать кнопку.
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const active = await api('/api/jobs/active');
      if (active && active.id) {
        const btn = document.querySelector('#coach-form button[type="submit"]');
        if (btn) {
          btn.disabled = true;
          btn.textContent = 'Идёт анализ…';
        }
        startGlobalJobPolling();
        showJob(app, active.id);
      }
      break;
    } catch {
      if (attempt < 2) await new Promise(r => setTimeout(r, 1500));
      /* после 3 неудач — кнопка свободна (сервер недоступен или задач нет) */
    }
  }

}

function unlockCoachButton() {
  try {
    const form = document.getElementById('coach-form');
    const btn = form && form.querySelector('button[type="submit"]');
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Запустить';
    }
  } catch { /* форма уже не на странице */ }
}

function showJob(app, jobId) {
  const container = document.getElementById('job-container');
  if (!container) return;
  if (cancelledJobId !== jobId) cancelledJobId = null;
  container.innerHTML = `
    <div class="card job-card" id="job-card" style="margin-top:16px">
      <h3>Задача <code>${jobId.slice(0, 8)}</code> — <span id="job-status">RUNNING</span></h3>
      <div class="job-progress-row" id="job-progress-row">
        <div class="job-progress"><div class="job-progress-bar" id="progress-bar" style="width:0%"></div></div>
        <button type="button" class="btn btn-secondary job-cancel-btn" id="job-cancel">Отменить</button>
      </div>
      <div class="job-phase" id="job-phase">Загрузка...</div>
      <div class="job-detail" id="job-detail">0 / 0</div>
      <div id="job-eta" style="color:var(--c-text-muted);font-size:13px"></div>
      <div id="job-quote" style="margin-top:10px;font-style:italic;color:var(--c-text-muted);font-size:13px;line-height:1.6"></div>
      <div id="job-error"></div>
      <div id="job-summary"></div>
    </div>
    <div class="card job-training" id="job-training" style="margin-top:12px">
      <h3>Пока идёт анализ, можно тренироваться</h3>
      <p>План и слабые дебюты строятся по уже проанализированным партиям — результаты сохраняются после каждой партии, так что можно не ждать конца анализа.</p>
      <div class="job-training-links">
        <a href="#" class="btn btn-secondary" data-view="opening">Дебюты</a>
        <a href="#" class="btn btn-secondary" data-view="middlegame">Миттельшпиль</a>
        <a href="#" class="btn btn-secondary" data-view="task">Задача</a>
      </div>
    </div>`;
  bindLinks(container);

  const cancelBtn = document.getElementById('job-cancel');
  if (cancelBtn) cancelBtn.addEventListener('click', () => cancelCoachJob(jobId));

  // Цитаты, пока движок считает — чтобы не было скучно.
  const QUOTES = [
    ["Когда видишь хороший ход, ищи ход получше.", "Эмануил Ласкер"],
    ["Шахматы — это борьба, главным образом со своими ошибками.", "Сергей Прокофьев"],
    ["Лучший ход — тот, который ставит соперника в тупик.", "Михаил Таль"],
    ["Я не верю в психологию. Я верю в сильные ходы.", "Бобби Фишер"],
    ["Тактика — это знание того, что делать, когда есть что делать.", "Савелий Тартаковер"],
    ["Слабые поля — это дыры в позиции. Занимай их конями.", "Арон Нимцович"],
    ["Проигранных партий нет — есть недоигранные.", "Михаил Ботвинник"],
    ["Конь на краю доски — позор.", "Зигберт Тарраш"],
    ["Пешка — душа шахмат.", "Франсуа Филидор"],
    ["Атака — лучшая защита, если она корректна.", "Гарри Каспаров"],
    ["Сначала стратегия, потом тактика: без плана ходы слепы.", "Макс Эйве"],
    ["Время на часах — тоже материал. Не отдавай его даром.", "Народное"],
  ];
  let quoteIdx = Math.floor(Math.random() * QUOTES.length);
  const paintQuote = () => {
    const box = document.getElementById('job-quote');
    if (!box) return;
    const [text, author] = QUOTES[quoteIdx % QUOTES.length];
    box.innerHTML = `♟ «${esc(text)}» — <b>${esc(author)}</b>`;
    quoteIdx++;
  };
  paintQuote();
  if (state.timers.quote) clearInterval(state.timers.quote);
  state.timers.quote = setInterval(paintQuote, 8000);

  const startedAt = Date.now();
  const stopQuote = () => { if (state.timers.quote) { clearInterval(state.timers.quote); state.timers.quote = null; } };

  if (state.timers.coach) clearInterval(state.timers.coach);
  let pollFails = 0;
  state.timers.coach = setInterval(async () => {
    if (state.currentView !== 'coach') { clearInterval(state.timers.coach); state.timers.coach = null; stopQuote(); return; }
    try {
      const j = await api('/api/jobs/' + jobId);
      if (cancelledJobId === jobId) {
        clearInterval(state.timers.coach);
        state.timers.coach = null;
        stopQuote();
        return;
      }
      pollFails = 0;
      const bar = document.getElementById('progress-bar');
      const phase = document.getElementById('job-phase');
      const detail = document.getElementById('job-detail');
      const status = document.getElementById('job-status');
      if (!bar || !phase || !detail || !status) { clearInterval(state.timers.coach); state.timers.coach = null; stopQuote(); return; }
      bar.style.width = j.total ? (j.done * 100 / j.total) + '%' : '0%';
      phase.textContent = j.phase;
      detail.textContent = j.done + ' / ' + j.total + (j.current ? ' (' + j.current.slice(0, 8) + ')' : '');
      status.textContent = j.status.toUpperCase();
      // ETA по текущему темпу: прошло / сделано * осталось.
      const etaBox = document.getElementById('job-eta');
      if (etaBox) {
        if (j.done > 0 && j.total > j.done && j.status === 'running') {
          const elapsed = jobElapsedSeconds(j, startedAt);
          const remain = jobEtaSeconds(j.done, j.total, elapsed);
          etaBox.textContent = `⏳ Прошло ${formatEta(elapsed)} · осталось примерно ${formatEta(remain)}`;
        } else if (j.total > 0 && j.done === 0) {
          etaBox.textContent = `⏳ Греем движок, первая партия почти готова…`;
        } else {
          etaBox.textContent = '';
        }
      }
      if (j.error) {
        document.getElementById('job-error').innerHTML = `<div class="job-error">${esc(j.error)}</div>`;
      }
      if (j.status === 'done') {
        clearInterval(state.timers.coach);
        state.timers.coach = null;
        stopQuote();
        unlockCoachButton();
        hideJobTrainingHint();
        stopGlobalJobPolling();
        hideGlobalJobIndicator();
        document.getElementById('job-summary').innerHTML = `<div class="alert alert-success" style="margin-top:12px">${esc(j.summary)}</div>`;
        if (state.currentView === 'coach') setTimeout(() => navigate('coach'), 1500);
      } else if (j.status === 'cancelled') {
        clearInterval(state.timers.coach);
        state.timers.coach = null;
        stopQuote();
        unlockCoachButton();
        markJobCancelled();
        stopGlobalJobPolling();
        hideGlobalJobIndicator();
      } else if (j.status === 'error') {
        clearInterval(state.timers.coach);
        state.timers.coach = null;
        stopQuote();
        unlockCoachButton();
        hideJobTrainingHint();
        stopGlobalJobPolling();
        hideGlobalJobIndicator();
      }
    } catch (pollErr) {
      const msg = String((pollErr && pollErr.message) || '');
      if (/404/.test(msg)) {
        // Задача мертва (рестарт сервера): честный LOST + разблок.
        clearInterval(state.timers.coach);
        state.timers.coach = null;
        stopQuote();
        unlockCoachButton();
        hideJobTrainingHint();
        stopGlobalJobPolling();
        hideGlobalJobIndicator();
        const errBox = document.getElementById('job-error');
        if (errBox) errBox.innerHTML = `<div class="job-error">Задача потеряна (сервер перезапустился). Можно запускать заново.</div>`;
        const st = document.getElementById('job-status');
        if (st) st.textContent = 'LOST';
        return;
      }
      // Сетевой чих (обрыв polling-запроса): НЕ разблокируем, ждём следующий тик.
      pollFails += 1;
      const phase = document.getElementById('job-phase');
      if (phase) phase.textContent = `Переподключение… (${pollFails})`;
      if (pollFails >= 10) {
        clearInterval(state.timers.coach);
        state.timers.coach = null;
        stopQuote();
        unlockCoachButton();
        const errBox = document.getElementById('job-error');
        if (errBox) errBox.innerHTML = `<div class="job-error">Сервер не отвечает (10 попыток). Проверьте соединение и обновите страницу.</div>`;
      }
    }
  }, 1000);
}

// Отмена задачи анализа из карточки: сервер помечает job cancelled,
// рабочий поток останавливается после текущей партии.
async function cancelCoachJob(jobId) {
  const btn = document.getElementById('job-cancel');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Отменяем…';
  }
  try {
    await apiPost('/api/jobs/' + jobId + '/cancel', new FormData());
  } catch (err) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Отменить';
    }
    showToast((err && err.message) || 'Не удалось отменить анализ', 'error');
    return;
  }
  cancelledJobId = jobId;
  if (state.timers.coach) {
    clearInterval(state.timers.coach);
    state.timers.coach = null;
  }
  if (state.timers.quote) {
    clearInterval(state.timers.quote);
    state.timers.quote = null;
  }
  unlockCoachButton();
  markJobCancelled();
  stopGlobalJobPolling();
  hideGlobalJobIndicator();
  showToast('Анализ отменён');
}

function markJobCancelled() {
  const row = document.getElementById('job-progress-row');
  if (row) row.style.display = 'none';
  const status = document.getElementById('job-status');
  if (status) status.textContent = 'CANCELLED';
  const phase = document.getElementById('job-phase');
  if (phase) phase.textContent = 'Анализ отменён';
  const detail = document.getElementById('job-detail');
  if (detail) detail.textContent = '';
  const eta = document.getElementById('job-eta');
  if (eta) eta.textContent = '';
  const errBox = document.getElementById('job-error');
  if (errBox) errBox.innerHTML = '';
  hideJobTrainingHint();
  const summary = document.getElementById('job-summary');
  if (summary) {
    summary.innerHTML = '<div class="alert alert-info" style="margin-top:12px">Анализ отменён. Уже проанализированные партии сохранены.</div>';
  }
}

function hideJobTrainingHint() {
  const hint = document.getElementById('job-training');
  if (hint) hint.remove();
}

// --- Tools (репертуар / соперник / человечность / турнир / FIDE) ---

const TOOL_LABELS = {
  repertoire: 'Репертуар',
  opponent: 'Соперник',
  humanity: 'Человечность',
  tournament: 'Турнир',
  fide: 'FIDE',
  review: 'Разбор',
  mentor: 'Тренер',
};

const TOOL_COLOR_RU = { white: 'белые', black: 'чёрные' };
const FIDE_CONTROL_RU = { standard: 'Классика', rapid: 'Рапид', blitz: 'Блиц' };

async function viewTools(app) {
  const tool = TOOL_LABELS[state.tool] ? state.tool : 'repertoire';
  state.tool = tool;
  const needsUser = tool === 'repertoire' || tool === 'opponent' || tool === 'humanity'
    || tool === 'review' || tool === 'mentor';
  if (needsUser && !state.user) {
    app.innerHTML = '<div class="onboarding card"><div class="empty-icon">🛠</div><h1>Нет профиля</h1><p>Выбери профиль на странице Профили.</p></div>';
    return;
  }

  const tabs = Object.entries(TOOL_LABELS).map(([key, label]) =>
    `<button type="button" class="btn btn-secondary ${key === tool ? 'btn-active' : ''}" data-tool="${key}">${label}</button>`
  ).join('');

  app.innerHTML = `
    <div class="card-header"><h1>Инструменты</h1>${state.user ? `<span class="badge badge-blue">${esc(state.user.nick)}</span>` : ''}</div>
    <div class="card">
      <div class="games-filter" style="margin-bottom:16px">${tabs}</div>
      ${toolFormHtml(tool)}
    </div>
    <div id="tool-result"></div>`;

  app.querySelectorAll('[data-tool]').forEach(btn => {
    btn.addEventListener('click', () => {
      state.tool = btn.dataset.tool;
      viewTools(app);
    });
  });
  bindToolForm(tool);
}

function toolFormHtml(tool) {
  if (tool === 'repertoire') {
    return `
      <form id="tool-form">
        <div class="form-row">
          <div class="form-group"><label>Цвет <select name="color"><option value="white">белые</option><option value="black">чёрные</option><option value="both" selected>оба</option></select></label></div>
          <div class="form-group"><label>Глубина линии (ходов) <input type="number" name="max_depth" value="16" min="1" max="64"></label></div>
        </div>
        <button type="submit" class="btn btn-primary" style="width:fit-content">Показать</button>
      </form>`;
  }
  if (tool === 'opponent') {
    return `
      <form id="tool-form">
        <div class="form-row">
          <div class="form-group" style="flex:2;min-width:200px"><label>Ник соперника <input type="text" name="opponent" required placeholder="GarryKasparov"></label></div>
          <div class="form-group"><label>Макс. партий <input type="number" name="max" value="30" min="1" max="200"></label></div>
          <div class="form-group"><label>Тип <select name="perf"><option value="rapid" selected>rapid</option><option value="blitz">blitz</option><option value="classical">classical</option><option value="bullet">bullet</option></select></label></div>
        </div>
        <button type="submit" class="btn btn-primary" style="width:fit-content">Собрать</button>
      </form>
      <p style="color:var(--c-text-muted);font-size:13px;margin-top:12px">Загружает его партии с Lichess, прогоняет через Stockfish и строит лист подготовки: дебюты, слабости, точки встречи с твоим репертуаром. Долго.</p>`;
  }
  if (tool === 'humanity') {
    return `
      <form id="tool-form">
        <p style="color:var(--c-text-muted);font-size:13px;margin:0 0 12px">Кеш — файл humanity.json. «Посчитать» заново оценивает ошибки через MaiaLite (долго, движок).</p>
        <div class="form-row" style="gap:8px">
          <button type="submit" class="btn btn-primary">Посчитать</button>
          <button type="button" class="btn btn-secondary" id="tool-cache">Показать кеш</button>
        </div>
      </form>`;
  }
  if (tool === 'tournament') {
    return `
      <form id="tool-form">
        <div class="form-group"><label>Партии — по одной на строку: Имя:white:win:2100<textarea name="games" rows="5" placeholder="Иванов:white:win:2100&#10;Петров:black:draw:1980" required></textarea></label></div>
        <div class="form-group"><label>Твой рейтинг до турнира (для прироста, опц.) <input type="number" name="initial" placeholder="1850"></label></div>
        <button type="submit" class="btn btn-primary" style="width:fit-content">Посчитать</button>
      </form>`;
  }
  if (tool === 'review') {
    return `
      <form id="tool-form">
        <div class="form-row">
          <div class="form-group"><label>Макс. партий <input type="number" name="max" value="3" min="1" max="50"></label></div>
          <div class="form-group" style="flex:2;min-width:200px"><label>Конкретная партия (опц.) <input type="text" name="game_id" placeholder="2SXfzXV2"></label></div>
          <div class="form-group"><label>Ключевых моментов <input type="number" name="moments" value="6" min="1" max="20"></label></div>
        </div>
        <button type="submit" class="btn btn-primary" style="width:fit-content">Разобрать</button>
      </form>
      <p style="color:var(--c-text-muted);font-size:13px;margin-top:12px">LLM разбирает ключевые моменты (blunder/mistake) твоих проанализированных партий — что ты терял и что надо было сыграть. Требует LLM_API_KEY (см. README).</p>`;
  }
  if (tool === 'mentor') {
    return `
      <form id="tool-form">
        <div class="form-group"><label>Заметки для тренера (опц.)<textarea name="notes" rows="3" placeholder="Хочу сильнее играть эндшпиль, особенно ладейные..."></textarea></label></div>
        <div class="form-row">
          <div class="form-group" style="flex:2;min-width:200px"><label>Конкретная партия (опц.) <input type="text" name="game_id" placeholder="2SXfzXV2"></label></div>
          <div class="form-group"><label>Глубина анализа <input type="number" name="max_depth" value="16" min="1" max="30"></label></div>
        </div>
        <button type="submit" class="btn btn-primary" style="width:fit-content">Спросить тренера</button>
      </form>
      <p style="color:var(--c-text-muted);font-size:13px;margin-top:12px">Тренер строит план по твоим партиям, дебютам, ошибкам и прогрессу — и выдаёт личные задания. Требует LLM_API_KEY (см. README).</p>`;
  }
  const prefill = state.user && state.user.fide_id ? state.user.fide_id : '';
  return `
    <form id="tool-form">
      <div class="form-row">
        <div class="form-group"><label>FIDE ID <input type="text" name="id" required placeholder="4130005" value="${esc(prefill)}"></label></div>
        <div class="form-group"><label>Окна истории <input type="number" name="windows" value="4" min="1" max="52"></label></div>
      </div>
      <button type="submit" class="btn btn-primary" style="width:fit-content">Показать</button>
    </form>`;
}

function bindToolForm(tool) {
  const form = document.getElementById('tool-form');
  if (!form) return;
  const cacheBtn = document.getElementById('tool-cache');
  if (cacheBtn) {
    cacheBtn.addEventListener('click', async () => {
      try {
        renderHumanity(await api('/api/humanize'));
      } catch (err) {
        toolError(err);
      }
    });
  }
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(form);
    try {
      if (tool === 'repertoire') {
        const params = new URLSearchParams({ color: fd.get('color'), max_depth: fd.get('max_depth') || '16' });
        renderRepertoire(await api('/api/repertoire?' + params));
      } else if (tool === 'opponent') {
        startToolJob((await apiPost('/api/prepare/run', fd)).job_id, renderOpponent);
      } else if (tool === 'tournament') {
        renderTournament(await apiPost('/api/tournament/run', fd));
      } else if (tool === 'fide') {
        startToolJob((await apiPost('/api/fide/run', fd)).job_id, renderFide);
      } else if (tool === 'humanity') {
        startToolJob((await api('/api/humanize?refresh=1')).job_id, renderHumanity);
      } else if (tool === 'review') {
        startToolJob((await apiPost('/api/review/run', fd)).job_id, renderReview);
      } else if (tool === 'mentor') {
        startToolJob((await apiPost('/api/mentor/run', fd)).job_id, renderMentor);
      }
    } catch (err) {
      toolError(err);
    }
  });
}

function toolError(err) {
  if (isSessionError(err)) {
    handleSessionExpired();
    return;
  }
  const box = document.getElementById('tool-result');
  if (box) box.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
  showToast(err.message, 'error');
}

function startToolJob(jobId, onDone) {
  const box = document.getElementById('tool-result');
  box.innerHTML = `
    <div class="card job-card">
      <div class="card-header"><span class="card-title">Задача <code>${jobId.slice(0, 8)}</code></span><span class="badge badge-blue" id="tool-status">RUNNING</span></div>
      <div class="job-progress"><div class="job-progress-bar" id="tool-bar" style="width:0%"></div></div>
      <div class="job-phase" id="tool-phase">Запуск...</div>
      <div class="job-detail" id="tool-detail">0 / 0</div>
      <div id="tool-error"></div>
    </div>`;

  if (state.timers.tool) clearInterval(state.timers.tool);
  state.timers.tool = setInterval(async () => {
    const bar = document.getElementById('tool-bar');
    if (!bar) { clearInterval(state.timers.tool); state.timers.tool = null; return; }
    let j;
    try {
      j = await api('/api/jobs/' + jobId);
    } catch {
      clearInterval(state.timers.tool);
      state.timers.tool = null;
      return;
    }
    bar.style.width = j.total ? (j.done * 100 / j.total) + '%' : '0%';
    document.getElementById('tool-phase').textContent = j.phase;
    document.getElementById('tool-detail').textContent = j.done + ' / ' + j.total;
    document.getElementById('tool-status').textContent = j.status.toUpperCase();
    if (j.error) {
      document.getElementById('tool-error').innerHTML = `<div class="job-error">${esc(j.error)}</div>`;
    }
    if (j.status === 'done' || j.status === 'error') {
      clearInterval(state.timers.tool);
      state.timers.tool = null;
      if (j.status === 'done') onDone(j.result || {});
    }
  }, 700);
}

function renderRepertoire(d) {
  const box = document.getElementById('tool-result');
  const lines = d.lines || [];
  const weakTotal = Object.values(d.weak || {}).reduce((a, b) => a + b, 0);
  const both = d.color === 'both';
  const openings = (d.openings || []).slice(0, 8);
  box.innerHTML = `
    <div class="card">
      <div class="card-header">
        <span class="card-title">Линии репертуара (${lines.length}) · партий ${d.games}</span>
        <span class="badge ${weakTotal ? 'badge-bad' : 'badge-good'}">слабых: ${weakTotal}</span>
      </div>
      ${lines.length ? `<div class="table-wrap"><table>
        <thead><tr>${both ? '<th>Цвет</th>' : ''}<th>Ходы</th><th class="num">Партий</th><th class="num">Хорошо</th><th class="num">Плохо</th><th class="num">Прочность</th></tr></thead>
        <tbody>${lines.map(l => `<tr>
          ${both ? `<td>${TOOL_COLOR_RU[l.color] || esc(l.color)}</td>` : ''}
          <td><code>${esc((l.moves || []).join(' '))}</code></td>
          <td class="num">${l.count}</td><td class="num">${l.ok}</td><td class="num">${l.bad}</td>
          <td class="num">${l.path_ok_rate != null ? Math.round(l.path_ok_rate * 100) + '%' : '—'}</td>
        </tr>`).join('')}</tbody></table></div>`
        : '<p style="color:var(--c-text-muted);padding:8px">Нет данных: запусти анализ партий.</p>'}
      ${openings.length ? `
        <h3 style="font-size:14px;margin:16px 0 8px">Дебюты — ошибки по названиям</h3>
        <div class="table-wrap"><table><thead><tr><th>Дебют</th><th>ECO</th><th class="num">Партий</th><th class="num">Очков %</th><th class="num">Ошибок/партию</th><th class="num">Ср. потеря</th></tr></thead>
        <tbody>${openings.map(o => `<tr><td>${esc(o.opening)}</td><td>${esc(o.eco || '—')}</td><td class="num">${o.games}</td><td class="num">${fmt(o.points_pct)}%</td><td class="num">${fmt(o.errors_per_game, 2)}</td><td class="num">${fmt(o.avg_drop)}%</td></tr>`).join('')}</tbody></table></div>` : ''}
    </div>`;
}

function renderOpponent(d) {
  const box = document.getElementById('tool-result');
  const p = d.profile || {};
  const conf = d.confrontations || [];
  const rating = p.rating || {};
  const ratingText = rating.first != null ? `${rating.first} → ${rating.last} (${rating.delta >= 0 ? '+' : ''}${rating.delta})` : '—';
  const speed = Object.entries(p.speed || {}).map(([k, v]) => `${esc(k)}: ${esc(String(v))}`).join(', ') || '—';

  const openRows = [];
  for (const color of ['white', 'black']) {
    for (const o of ((p.openings || {})[color] || [])) {
      openRows.push(`<tr>
        <td>${TOOL_COLOR_RU[color]}</td>
        <td>${esc(o.opening)}${o.variant !== o.opening ? ` <span style="color:var(--c-text-muted)">(${esc(o.variant)})</span>` : ''}</td>
        <td class="num">${o.games}</td><td class="num">${fmt(o.points_pct)}%</td>
        <td class="num">${fmt(o.errors_per_game, 2)}</td><td class="num">${fmt(o.avg_drop)}%</td>
      </tr>`);
    }
  }

  const weakness = p.weaknesses || {};
  const weakRows = [
    ...(weakness.phases || []).map(w => `<tr><td>фаза</td><td>${esc(w.label)}</td><td class="num">${w.bad_moves}</td><td class="num">${w.avg_drop != null ? fmt(w.avg_drop) + '%' : '—'}</td></tr>`),
    ...(weakness.motifs || []).map(w => `<tr><td>мотив</td><td>${esc(w.motif)}</td><td class="num">${w.bad_moves}</td><td class="num">${w.avg_drop != null ? fmt(w.avg_drop) + '%' : '—'}</td></tr>`),
  ];

  box.innerHTML = `
    <div class="card">
      <div class="card-header">
        <span class="card-title">Подготовка: ${esc(p.opponent || '')}</span>
        <span class="badge badge-blue">${p.games != null ? p.games : 0} партий</span>
      </div>
      ${p.small_sample ? '<div class="alert alert-warn">Меньше 20 партий — выводы о репертуаре соперника шумные.</div>' : ''}
      <div class="grid grid-4">
        <div class="stat"><div class="stat-value">${p.score_pct != null ? fmt(p.score_pct) + '%' : '—'}</div><div class="stat-label">Его очки</div></div>
        <div class="stat"><div class="stat-value">${ratingText}</div><div class="stat-label">Его рейтинг</div></div>
        <div class="stat"><div class="stat-value" style="font-size:15px">${esc(p.trend || '—')}</div><div class="stat-label">Качество игры</div></div>
        <div class="stat"><div class="stat-value" style="font-size:15px">${speed}</div><div class="stat-label">Контроль времени</div></div>
      </div>
      <h3 style="font-size:14px;margin:8px 0">Его дебюты</h3>
      ${openRows.length ? `<div class="table-wrap"><table><thead><tr><th>Цвет</th><th>Дебют</th><th class="num">Партий</th><th class="num">Очков %</th><th class="num">Ошибок/партию</th><th class="num">Ср. потеря</th></tr></thead><tbody>${openRows.join('')}</tbody></table></div>`
        : '<p style="color:var(--c-text-muted)">Нет данных о его дебютах.</p>'}
      <h3 style="font-size:14px;margin:16px 0 8px">Где он ошибается</h3>
      ${weakRows.length ? `<div class="table-wrap"><table><thead><tr><th>Тип</th><th>Что</th><th class="num">Плохих ходов</th><th class="num">Ср. потеря</th></tr></thead><tbody>${weakRows.join('')}</tbody></table></div>`
        : '<p style="color:var(--c-text-muted)">Ошибок не найдено.</p>'}
      <h3 style="font-size:14px;margin:16px 0 8px">Точки встречи (его дебют → твой ответ)</h3>
      ${conf.length ? `<div class="table-wrap"><table><thead><tr><th>Дебют</th><th>Он</th><th>Твой ответ</th><th class="num">Его ошибок/парт.</th><th class="num">Твоих ошибок/парт.</th></tr></thead>
        <tbody>${conf.map(c => `<tr>
          <td>${esc(c.opening)}</td>
          <td>${TOOL_COLOR_RU[c.his_color]}: ${c.his_games} парт.</td>
          <td><code>${esc(c.your_reply)}</code> (${c.your_reply_games} парт.)</td>
           <td class="num">${fmt(c.his_errors_per_game, 2)}</td>
           <td class="num">${fmt(c.your_errors_per_game, 2)}</td>
        </tr>`).join('')}</tbody></table></div>`
        : '<p style="color:var(--c-text-muted)">Пересечений с твоим репертуаром не нашлось: готовиться пока не к чему.</p>'}
    </div>`;
}

function renderHumanity(d) {
  const box = document.getElementById('tool-result');
  const s = d.summary || {};
  const items = (d.items || []).slice(0, 60);
  const verdictBadge = v => v === 'natural' ? 'badge-good' : v === 'unnatural' ? 'badge-bad' : 'badge-neutral';
  box.innerHTML = `
    <div class="card">
      <div class="card-header">
        <span class="card-title">Человечность ошибок</span>
        ${d.avg_beta != null ? `<span class="badge badge-blue">ср. β ${d.avg_beta}</span>` : ''}
      </div>
      <div class="grid grid-4">
        <div class="stat"><div class="stat-value">${s.total || 0}</div><div class="stat-label">Всего</div></div>
        <div class="stat"><div class="stat-value">${s.natural || 0}</div><div class="stat-label">Естественные</div></div>
        <div class="stat"><div class="stat-value">${s.borderline || 0}</div><div class="stat-label">Пограничные</div></div>
        <div class="stat"><div class="stat-value">${s.unnatural || 0}</div><div class="stat-label">Неестественные</div></div>
      </div>
      ${items.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Партия</th><th class="num">П.</th><th>Ход</th><th>Класс</th><th class="num">Потеря</th><th class="num">β</th><th>Вердикт</th></tr></thead>
        <tbody>${items.map(i => `<tr>
          <td>${esc(String(i.game_id || '').slice(0, 8))}</td>
          <td class="num">${i.ply}</td>
          <td class="drill-san">${esc(i.san || '')}</td>
          <td>${esc(i.classification || '—')}</td>
          <td class="num">${i.drop != null ? fmt(i.drop) + '%' : '—'}</td>
          <td class="num">${i.beta != null ? i.beta : '—'}</td>
          <td><span class="badge ${verdictBadge(i.verdict)}">${esc(i.verdict || '—')}</span></td>
        </tr>`).join('')}</tbody></table></div>`
        : '<p style="color:var(--c-text-muted);padding:8px">Ошибок нет.</p>'}
    </div>`;
}

function renderTournament(r) {
  const box = document.getElementById('tool-result');
  const rows = r.rows || [];
  const deltaText = r.delta != null ? (r.delta >= 0 ? '+' : '') + r.delta : '—';
  box.innerHTML = `
    <div class="card">
      <div class="card-header"><span class="card-title">Итоги турнира</span></div>
      <div class="grid grid-4">
        <div class="stat"><div class="stat-value">${r.games}</div><div class="stat-label">Партий</div></div>
        <div class="stat"><div class="stat-value">${r.points}</div><div class="stat-label">Очков</div></div>
        <div class="stat"><div class="stat-value">${r.performance != null ? r.performance : '—'}</div><div class="stat-label">Перформанс</div></div>
        <div class="stat"><div class="stat-value">${deltaText}</div><div class="stat-label">Прирост рейтинга</div></div>
      </div>
      <p style="color:var(--c-text-muted);font-size:13px">Средний рейтинг соперника: ${r.avg_opponent != null ? r.avg_opponent : '—'}</p>
      ${rows.length ? `<div class="table-wrap"><table>
        <thead><tr><th>Соперник</th><th>Цвет</th><th class="num">Рейтинг</th><th>Результат</th><th class="num">Очки</th></tr></thead>
        <tbody>${rows.map(row => `<tr>
          <td>${esc(row.opponent)}</td>
          <td>${esc(row.color)}</td>
          <td class="num">${row.opponent_rating != null ? row.opponent_rating : '—'}</td>
          <td><span class="badge ${row.result === 'Победа' ? 'badge-good' : row.result === 'Поражение' ? 'badge-bad' : 'badge-neutral'}">${esc(row.result)}</span></td>
          <td class="num">${row.points}</td>
        </tr>`).join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted)">Партий нет.</p>'}
    </div>`;
}

function renderFide(d) {
  const box = document.getElementById('tool-result');
  const p = d.player || {};
  const trend = d.trend || {};
  const controls = trend.controls || [];
  const ratingOrDash = v => v != null ? v : '—';

  const winRows = [];
  const summaryRows = controls.map(c => {
    for (const row of (c.rows || [])) {
      winRows.push(`<tr>
        <td>${FIDE_CONTROL_RU[c.key] || esc(c.key)}</td>
        <td class="num">${row.index}</td>
        <td class="num">${row.periods}</td>
        <td>${esc(row.first_period)} → ${esc(row.last_period)}</td>
        <td class="num">${row.first_rating} → ${row.last_rating}</td>
        <td class="num">${row.delta >= 0 ? '+' : ''}${row.delta}</td>
      </tr>`);
    }
    const dir = c.direction === 'up' ? '↑' : c.direction === 'down' ? '↓' : '→';
    return `<tr>
      <td>${FIDE_CONTROL_RU[c.key] || esc(c.key)}</td>
      <td class="num">${ratingOrDash(c.current)}</td>
      <td class="num">${c.delta != null ? (c.delta >= 0 ? '+' : '') + c.delta : '—'}</td>
      <td class="center">${dir}</td>
      <td class="num">${c.points}</td>
    </tr>`;
  }).join('');

  box.innerHTML = `
    <div class="card">
      <div class="card-header">
        <span class="card-title">${esc(p.name || 'FIDE ' + (d.id || ''))}</span>
        <span class="badge badge-blue">FIDE ${esc(d.id || '')}</span>
      </div>
      <p style="color:var(--c-text-muted);font-size:13px">Федерация: ${esc(p.federation || '—')} · Год рождения: ${ratingOrDash(p.birth_year)} · Лучший текущий: ${ratingOrDash(trend.best_current)}</p>
      <div class="grid grid-3">
        <div class="stat"><div class="stat-value">${ratingOrDash(p.standard)}</div><div class="stat-label">Классика</div></div>
        <div class="stat"><div class="stat-value">${ratingOrDash(p.rapid)}</div><div class="stat-label">Рапид</div></div>
        <div class="stat"><div class="stat-value">${ratingOrDash(p.blitz)}</div><div class="stat-label">Блиц</div></div>
      </div>
      ${summaryRows ? `<h3 style="font-size:14px;margin:8px 0">Тренд по контролем</h3>
        <div class="table-wrap"><table><thead><tr><th>Контроль</th><th class="num">Сейчас</th><th class="num">Δ за окна</th><th class="center">Направление</th><th class="num">Периодов</th></tr></thead>
        <tbody>${summaryRows}</tbody></table></div>` : ''}
      ${winRows.length ? `<h3 style="font-size:14px;margin:16px 0 8px">Окна истории</h3>
        <div class="table-wrap"><table><thead><tr><th>Контроль</th><th class="num">№</th><th class="num">Периодов</th><th>Период</th><th class="num">Было → стало</th><th class="num">Δ</th></tr></thead>
        <tbody>${winRows.join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted)">История рейтингов пуста.</p>'}
    </div>`;
}

function renderReview(d) {
  const box = document.getElementById('tool-result');
  const games = d.games || [];
  const resBadge = r => r === 'win' ? 'badge-good' : r === 'loss' ? 'badge-bad' : 'badge-neutral';
  const resLabel = r => r === 'win' ? 'Победа' : r === 'loss' ? 'Поражение' : 'Ничья';
  const promptBlocks = (d.prompts || []).map(p => `
    <div style="margin-top:10px"><strong style="font-size:12px">${esc(p.game_id || '')} · ${esc(p.role)}</strong>
    <div class="prompt-block">${esc(p.content)}</div></div>`).join('');

  const gameCards = games.map((g, idx) => {
    const moments = (g.moments || []).map(m => {
      const cls = m.classification || '—';
      return `
        <div style="margin-top:8px">
          <code>${esc(m.san || '—')}</code>
          <span class="badge ${cls === 'blunder' ? 'badge-bad' : cls === 'mistake' ? 'badge-neutral' : 'badge-good'}">${esc(cls)}</span>
          <span style="color:var(--c-text-muted);font-size:12px">полуход ${m.ply || '—'}${m.drop != null ? ` · потеря win% ${fmt(m.drop)}` : ''}</span>
          <div style="margin-top:2px">${m.text ? esc(m.text) : '<span style="color:var(--c-text-muted)">(нет объяснения)</span>'}</div>
        </div>`;
    }).join('');
    return `
      <div class="card"${idx ? ' style="margin-top:12px"' : ''}>
        <div class="card-header">
          <span class="card-title">${esc(g.game_id)} · ${esc(g.white)} — ${esc(g.black)}</span>
          <span class="badge ${resBadge(g.result)}">${resLabel(g.result)}</span>
        </div>
        <p style="color:var(--c-text-muted);font-size:13px;margin:0 0 8px">${esc(g.opening || '—')}${g.avg_win_loss != null ? ` · ср. потеря win% ${fmt(g.avg_win_loss)}` : ''}</p>
        ${moments || '<p style="color:var(--c-text-muted)">Ключевых моментов нет.</p>'}
        ${g.summary ? `<p style="margin-top:8px"><strong>Итог:</strong> ${esc(g.summary)}</p>` : ''}
      </div>`;
  }).join('');

  box.innerHTML = `
    ${d.dry_run ? '<div class="alert alert-info">Режим dry-run: LLM не вызывался — ниже готовые промпты.</div>' : ''}
    ${games.length ? gameCards : '<div class="card"><p style="color:var(--c-text-muted)">Разбора нет: в отобранных партиях не нашлось ключевых моментов (blunder/mistake).</p></div>'}
    ${d.dry_run && promptBlocks ? `<div class="card" style="margin-top:12px"><div class="card-header"><span class="card-title">Готовые промпты</span></div>${promptBlocks}</div>` : ''}`;
}

function renderMentor(d) {
  const box = document.getElementById('tool-result');
  if (d.dry_run) {
    const prompts = (d.prompts || []).map(p => `
      <div style="margin-top:10px"><strong style="font-size:12px">${esc(p.role)}</strong>
      <div class="prompt-block">${esc(p.content)}</div></div>`).join('');
    box.innerHTML = `
      <div class="alert alert-info">Режим dry-run: тренер не вызывался — промпты готовы.</div>
      <div class="card"><div class="card-header"><span class="card-title">Промпты тренера</span></div>${prompts || '<p style="color:var(--c-text-muted)">Промптов нет.</p>'}</div>`;
    return;
  }
  box.innerHTML = `
    <div class="card">
      <div class="card-header">
        <span class="card-title">План тренера${d.user ? ` · ${esc(d.user)}` : ''}</span>
        ${d.model ? `<span class="badge badge-blue">${esc(d.model)}</span>` : ''}
      </div>
      ${d.notes ? `<div class="alert alert-warn">Учтены заметки: ${esc(d.notes)}</div>` : ''}
      <div class="mentor-reply">${esc(d.reply || '(пустой ответ тренера)')}</div>
    </div>`;
}

function _fmtDur(sec) {
  const total = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return h > 0 ? `${h} ч ${m} мин` : `${m} мин`;
}

function _trackLineHtml(track) {
  const lichess = (Number(track.lichess_sec) || 0) + (Number(track.manual_sec) || 0);
  return `Сегодня: <strong style="color:var(--c-text)">${_fmtDur(track.total_sec)}</strong> · Lichess ${_fmtDur(lichess)} · тренер ${_fmtDur(track.site_sec)}`;
}

async function refreshTrackLine() {
  if (!state.user) return;
  const track = await api('/api/track/today').catch(() => null);
  const line = document.getElementById('track-line');
  if (track && line) line.innerHTML = _trackLineHtml(track);
}

function closeLichessManual() {
  const link = document.querySelector('.track-manual-add');
  const slot = document.getElementById('track-manual-slot');
  if (slot) slot.innerHTML = '';
  if (link) link.style.display = '';
}

function openLichessManual() {
  const link = document.querySelector('.track-manual-add');
  const slot = document.getElementById('track-manual-slot');
  if (!link || !slot) return;
  link.style.display = 'none';
  slot.innerHTML = `<form class="track-manual-form">
      <input type="number" min="1" max="600" step="1" placeholder="минут">
      <button class="btn btn-primary" style="font-size:12px;padding:4px 10px">Добавить</button>
      <button type="button" class="btn btn-link track-manual-cancel">✕</button>
    </form><div class="alert alert-error track-manual-err"></div>`;
  const form = slot.querySelector('.track-manual-form');
  const input = form.querySelector('input');
  const err = slot.querySelector('.track-manual-err');
  const fail = msg => {
    err.textContent = msg;
    err.style.display = 'inline-block';
    if (matchMedia('(pointer:fine)').matches) input.focus();
  };
  if (matchMedia('(pointer:fine)').matches) input.focus();
  form.addEventListener('submit', async e => {
    e.preventDefault();
    err.style.display = 'none';
    const raw = input.value.trim();
    const n = Number(raw);
    if (raw === '' || !Number.isInteger(n) || n < 1 || n > 600) {
      fail('От 1 до 600 минут');
      return;
    }
    const fd = new FormData();
    fd.set('minutes', String(n));
    try {
      await apiPost('/api/track/lichess_manual', fd);
      await refreshTrackLine();
      closeLichessManual();
    } catch (apiErr) {
      fail(apiErr.message);
    }
  });
  form.querySelector('.track-manual-cancel').addEventListener('click', closeLichessManual);
}

async function viewUsers(app) {
  const status = await authStatus();
  if (!status.logged_in) {
    viewAuth(app);
    return;
  }
  const users = await api('/api/users');
  const list = users.users || [];
  const want = String(status.nick || '').toLowerCase();
  const me = list.find(u => String(u.nick || '').toLowerCase() === want) || { nick: status.nick };
  if (me.nick) {
    state.user = me;
    renderUserbox();
  }
  const track = state.user ? await api('/api/track/today').catch(() => null) : null;
  const trackHtml = track
    ? `<div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--c-border);font-size:13px;color:var(--c-text-muted)">
        <div id="track-line">${_trackLineHtml(track)}</div>
        <div style="margin-top:2px"><a href="#" class="btn btn-link track-manual-add">+ время на Lichess</a><span id="track-manual-slot"></span></div>
      </div>`
    : '';

  app.innerHTML = `
    <div class="card-header"><h1>Аккаунт</h1></div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-header"><span class="card-title">Мой аккаунт</span></div>
      <div class="grid grid-3">
        <div class="stat"><div class="stat-value">${esc(me.nick || '—')}</div><div class="stat-label">Логин</div></div>
        <div class="stat"><div class="stat-value">${esc(me.lichess || '—')}</div><div class="stat-label">Lichess · <a href="#" class="btn btn-link" id="lichess-edit" style="font-size:11px">изменить</a></div></div>
        <div class="stat"><div class="stat-value">${esc(me.fide_id || '—')}</div><div class="stat-label">FIDE ID · <a href="#" class="btn btn-link" id="fide-edit" style="font-size:11px">изменить</a></div></div>
      </div>
      <div id="lichess-slot"></div>
      <div id="fide-slot"></div>
      ${trackHtml}
      <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--c-border)">
        <form id="pw-form" class="form-row" style="gap:8px;align-items:flex-end">
          <div class="form-group" style="flex:1;min-width:140px;margin-bottom:0"><label>Старый пароль <input type="password" name="old" required autocomplete="current-password"></label></div>
          <div class="form-group" style="flex:1;min-width:140px;margin-bottom:0"><label>Новый пароль <input type="password" name="new" required autocomplete="new-password"></label></div>
          <button class="btn btn-secondary" style="height:38px">Сменить пароль</button>
        </form>
        <div class="alert alert-error" id="pw-err" style="display:none;margin-top:8px"></div>
        <div class="alert alert-success" id="pw-ok" style="display:none;margin-top:8px">Пароль сменён</div>
        <div style="margin-top:8px"><button class="btn btn-secondary" id="logout-btn">Выйти</button></div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Все пользователи (${list.length})</span></div>
      ${list.length ? `<div class="table-wrap"><table><thead><tr><th>Ник</th><th>Lichess</th><th>FIDE</th><th>Партий</th><th>Анализов</th><th>Последний вход</th></tr></thead><tbody>${list.map(u => {
        const isCurrent = String(u.nick || '').toLowerCase() === want;
        return `<tr>
          <td${isCurrent ? ' style="font-weight:600"' : ''}>${esc(u.nick)}${isCurrent ? ' <span class="badge badge-blue">это ты</span>' : ''}</td>
          <td>${esc(u.lichess || '—')}</td>
          <td>${esc(u.fide_id || '—')}</td>
          <td class="num">${u.games}</td>
          <td class="num">${u.analyses}</td>
          <td>${u.last_seen_at ? new Date(u.last_seen_at * 1000).toLocaleDateString('ru-RU') : '—'}</td>
        </tr>`;
      }).join('')}</tbody></table></div>` : '<p style="color:var(--c-text-muted);padding:16px">Пользователей нет.</p>'}
    </div>`;

  const rerender = async () => {
    await loadUser();
    navigate('users');
  };

  const lichEdit = document.getElementById('lichess-edit');
  if (lichEdit) lichEdit.addEventListener('click', e => {
    e.preventDefault();
    const slot = document.getElementById('lichess-slot');
    if (!slot || slot.innerHTML) return;
    slot.innerHTML = `<form id="lichess-form" class="form-row" style="gap:8px;align-items:flex-end;margin-top:8px">
        <div class="form-group" style="flex:1;min-width:160px;margin-bottom:0"><label>Ник на Lichess <input type="text" name="lichess" value="${esc(me.lichess || '')}" required></label></div>
        <button class="btn btn-primary" style="height:38px">Сохранить</button>
      </form><div class="alert alert-error" id="lichess-err" style="display:none;margin-top:8px"></div>`;
    document.getElementById('lichess-form').addEventListener('submit', async ev => {
      ev.preventDefault();
      try {
        await apiPost('/api/auth/lichess', new FormData(ev.target));
        await rerender();
      } catch (err) {
        const box = document.getElementById('lichess-err');
        if (box) {
          box.textContent = err.message;
          box.style.display = 'block';
        }
      }
    });
  });

  const fideEdit = document.getElementById('fide-edit');
  if (fideEdit) fideEdit.addEventListener('click', e => {
    e.preventDefault();
    const slot = document.getElementById('fide-slot');
    if (!slot || slot.innerHTML) return;
    slot.innerHTML = `<form id="account-fide-form" class="form-row" style="gap:8px;align-items:flex-end;margin-top:8px">
        <div class="form-group" style="flex:1;min-width:160px;margin-bottom:0"><label>FIDE ID <input type="text" name="fide_id" value="${esc(me.fide_id || '')}" placeholder="4130005"></label></div>
        <button class="btn btn-primary" style="height:38px">Сохранить</button>
      </form><div class="alert alert-error" id="account-fide-err" style="display:none;margin-top:8px"></div>`;
    document.getElementById('account-fide-form').addEventListener('submit', async ev => {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      fd.set('nick', me.nick);
      try {
        await apiPost('/api/user/fide', fd);
        await rerender();
      } catch (err) {
        const box = document.getElementById('account-fide-err');
        if (box) {
          box.textContent = err.message;
          box.style.display = 'block';
        }
      }
    });
  });

  document.getElementById('pw-form').addEventListener('submit', async e => {
    e.preventDefault();
    const errBox = document.getElementById('pw-err');
    const okBox = document.getElementById('pw-ok');
    errBox.style.display = 'none';
    okBox.style.display = 'none';
    try {
      await apiPost('/api/auth/password', new FormData(e.target));
      e.target.reset();
      okBox.style.display = 'block';
    } catch (err) {
      errBox.textContent = err.message;
      errBox.style.display = 'block';
    }
  });

  document.getElementById('logout-btn').addEventListener('click', async () => {
    try {
      await apiPost('/api/auth/logout', new FormData());
    } catch { /* всё равно выходим */ }
    state.user = null;
    state.authMode = 'login';
    Object.assign(state, {
      gameId: null, gameData: null, task: null, taskKey: null, taskResult: null,
      taskRevealed: false, taskExplanation: null, planLlmJobId: null,
      planLlmGenerating: false, tool: 'repertoire', gamesFilter: 'all',
      openingSession: null, openingFens: null, openingLastReply: null, openingJobId: null,
      middlegameSession: null, middlegameMeta: null, middlegameJobId: null,
      middlegameThinking: false, middlegameAnalyzing: false, middlegameError: null,
      middlegamePlanDraft: '', middlegameStrength: 'equal',
    });
    clearMiddlegameSessionId();
    hideNav();
    renderUserbox();
    render(state.currentView);
  });

  const manualLink = app.querySelector('.track-manual-add');
  if (manualLink) {
    manualLink.addEventListener('click', e => {
      e.preventDefault();
      openLichessManual();
    });
  }
}

// --- Utilities ---

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function safeHttp(u) {
  return /^https?:\/\//i.test(u || '') ? u : '#';
}

const fmt = (v, d = 1) => (v != null ? Number(v).toFixed(d) : '—');

function isSessionError(err) {
  return /No current user|Not authenticated|401/.test((err && err.message) || '');
}

function handleSessionExpired() {
  state.user = null;
  hideNav();
  renderUserbox();
  viewAuth(document.getElementById('app'));
}

function bindLinks(root) {
  root.querySelectorAll('a[data-view]').forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      navigate(a.dataset.view);
    });
  });
}

// --- Init ---

async function loadUser() {
  let status = null;
  try {
    status = await api('/api/auth/status');
  } catch {
    status = { logged_in: false, nick: null };
  }
  if (!status || !status.logged_in) {
    state.user = null;
    renderUserbox();
    return;
  }
  try {
    const data = await api('/api/users');
    const want = String(status.nick || '').toLowerCase();
    const found = (data.users || []).find(
      u => String(u.nick || '').toLowerCase() === want
    );
    state.user = found || { nick: status.nick };
  } catch {
    state.user = { nick: status.nick };
  }
  renderUserbox();
}

function startTrackPing() {
  if (state.trackStarted) return;
  state.trackStarted = true;
  const ping = () => {
    if (state.user) fetch('/api/track/ping', { method: 'POST' }).catch(() => {});
  };
  ping();
  setInterval(ping, 60000);
}

async function init() {
  setupTheme();
  setupNav();
  await loadUser();
  startTrackPing();
  navigate('dashboard');
  startGlobalJobPolling();
}

init();