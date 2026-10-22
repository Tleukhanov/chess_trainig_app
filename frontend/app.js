// Chess Trainer Frontend — SPA на vanilla JS, потребляет /api/* эндпоинты.

import { boardHtml, bindBoard, lastMoveSquares } from './board.js?v=0.5.4';

const state = {
  user: null,
  currentView: 'dashboard',
  timers: { coach: null, task: null, llm: null, tool: null, opening: null, middlegame: null, quote: null },
  gamesFilter: 'all',
  gameId: null,
  gameData: null,
  gamePly: 0,
  keyHandler: null,
  tool: 'repertoire',
  task: null,
  taskKey: null,
  taskResult: null,
  taskAnswerText: '',
  taskRevealed: false,
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
  middlegameSession: null,
  middlegameMeta: null,
  middlegameStrength: 'equal',
  middlegamePlanDraft: '',
  middlegameThinking: false,
  middlegameAnalyzing: false,
  middlegameJobId: null,
  middlegameError: null,
  middlegameOptimistic: null,
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
      <table class="table-wrap">
        <thead><tr><th>Дебют</th><th>ECO</th><th>Партий</th><th>Очков %</th><th>Ошибок/партию</th><th>Ср. потеря</th></tr></thead>
        <tbody>
          ${plan.repertoire.weak_openings.slice(0, 6).map(o => `
            <tr>
              <td>${esc(o.opening)}</td><td>${esc(o.eco)}</td>
              <td class="num">${o.games}</td>
              <td class="num">${fmt(o.points_pct)}%</td>
              <td class="num">${fmt(o.errors_per_game, 2)}</td>
              <td class="num">${fmt(o.avg_drop)}%</td>
            </tr>`).join('')}
        </tbody>
      </table>`;
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
      <table class="table-wrap" style="font-size:12px">
        <thead><tr><th>Метрика</th><th>Было</th><th>Стало</th><th>Δ</th><th>Вердикт</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } else {
    trendHtml = '<p style="color:var(--c-text-muted)">Нет данных для тренда.</p>';
  }

  let motifsHtml = '';
  if (plan.patterns.motifs.length > 0) {
    motifsHtml = `
      <table class="table-wrap">
        <thead><tr><th>Мотив</th><th>Частота</th><th>Ср. потеря win%</th></tr></thead>
        <tbody>
          ${plan.patterns.motifs.slice(0, 5).map(m => `
            <tr><td>${esc(m[0])}</td><td class="num">${m[1].count}</td><td class="num">${fmt(m[1].avg_drop)}%</td></tr>`).join('')}
        </tbody>
      </table>`;
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
      <div class="card stat"><div class="stat-value">${avgWinLoss}</div><div class="stat-label">Ср. потеря win%</div></div>
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
        ${whiteBranches.length ? `<table class="table-wrap"><thead><tr><th>Ветвь</th><th>Партий</th><th>Прочность</th></tr></thead><tbody>${whiteBranches.map(b => `<tr><td><code>${esc(b.moves.join(' '))}</code></td><td class="num">${b.count}</td><td class="num">${fmt(b.path_ok_rate * 100, 0)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Репертуар чёрных</span></div>
        ${blackBranches.length ? `<table class="table-wrap"><thead><tr><th>Ветвь</th><th>Партий</th><th>Прочность</th></tr></thead><tbody>${blackBranches.map(b => `<tr><td><code>${esc(b.moves.join(' '))}</code></td><td class="num">${b.count}</td><td class="num">${fmt(b.path_ok_rate * 100, 0)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Слабые места репертуара</span></div>
      ${ov.openings.length ? `<table class="table-wrap"><thead><tr><th>Дебют</th><th>ECO</th><th>Партий</th><th>Очков %</th><th>Ошибок/партию</th><th>Ср. потеря</th></tr></thead><tbody>${ov.openings.slice(0, 12).map(o => `<tr><td>${esc(o.opening)}</td><td>${esc(o.eco)}</td><td class="num">${o.games}</td><td class="num">${fmt(o.points_pct)}%</td><td class="num">${fmt(o.errors_per_game, 2)}</td><td class="num">${fmt(o.avg_drop)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
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
      <div class="card stat"><div class="stat-value">${avgWinLoss}</div><div class="stat-label">Ср. потеря win%</div></div>
      <div class="card stat"><div class="stat-value">${plan.drills.total}/${plan.drills.unnatural}</div><div class="stat-label">Ошибки всего/неест.</div></div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Репертуар — частые ветви</span></div>
      <div class="grid grid-2">
        <div><h3>Белые</h3>${plan.repertoire.white.length ? `<table class="table-wrap" style="font-size:12px"><thead><tr><th>Ветвь</th><th>Партий</th><th>OK %</th></tr></thead><tbody>${plan.repertoire.white.map(b => `<tr><td><code>${esc(b.moves.join(' '))}</code></td><td class="num">${b.count}</td><td class="num">${fmt(b.ok_rate * 100, 0)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}</div>
        <div><h3>Чёрные</h3>${plan.repertoire.black.length ? `<table class="table-wrap" style="font-size:12px"><thead><tr><th>Ветвь</th><th>Партий</th><th>OK %</th></tr></thead><tbody>${plan.repertoire.black.map(b => `<tr><td><code>${esc(b.moves.join(' '))}</code></td><td class="num">${b.count}</td><td class="num">${fmt(b.ok_rate * 100, 0)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}</div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Слабые дебюты</span></div>
      ${plan.repertoire.weak_openings.length ? `<table class="table-wrap"><thead><tr><th>Дебют</th><th>ECO</th><th>Партий</th><th>Очков %</th><th>Ошибок/партию</th><th>Ср. потеря</th></tr></thead><tbody>${plan.repertoire.weak_openings.slice(0, 6).map(o => `<tr><td>${esc(o.opening)}</td><td>${esc(o.eco)}</td><td class="num">${o.games}</td><td class="num">${fmt(o.points_pct)}%</td><td class="num">${fmt(o.errors_per_game, 2)}</td><td class="num">${fmt(o.avg_drop)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет слабых дебютов.</p>'}
    </div>
    <div class="grid grid-2">
      <div class="card">
        <div class="card-header"><span class="card-title">Мотивы ошибок</span></div>
        ${plan.patterns.motifs.length ? `<table class="table-wrap"><thead><tr><th>Мотив</th><th>Частота</th><th>Ср. потеря</th></tr></thead><tbody>${plan.patterns.motifs.map(m => `<tr><td>${esc(m[0])}</td><td class="num">${m[1].count}</td><td class="num">${fmt(m[1].avg_drop)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Фазы ошибок</span></div>
        ${plan.patterns.phases.length ? `<table class="table-wrap"><thead><tr><th>Фаза</th><th>Ошибок</th><th>Грубых</th><th>Обычных</th><th>Ср. потеря</th></tr></thead><tbody>${plan.patterns.phases.map(p => `<tr><td>${esc(p[0])}</td><td class="num">${p[1].count}</td><td class="num">${p[1].blunders}</td><td class="num">${p[1].mistakes}</td><td class="num">${fmt(p[1].avg_drop)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
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
      trendRows += `<tr><td>${esc(m)}</td><td class="num">${fmt(v.from, 2)}</td><td class="num">${fmt(v.to, 2)}</td><td class="num ${deltaClass}">${v.delta >= 0 ? '+' : ''}${fmt(v.delta, 2)}</td><td>${v.direction}</td><td><span class="badge ${v.verdict === 'better' ? 'badge-good' : v.verdict === 'worse' ? 'badge-bad' : 'badge-neutral'}">${v.verdict}</span></td></tr>`;
    }
  }

  const overall = pr.trend ? pr.trend.overall : '—';
  const badgeClass = overall === 'improving' ? 'badge-good' : overall === 'worsening' ? 'badge-bad' : 'badge-neutral';

  app.innerHTML = `
    <div class="card-header"><h1>Прогресс</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="card">
      <div class="card-header"><span class="card-title">Таблица окон (${pr.windows} окон, ${pr.games} партий)</span></div>
      <table class="table-wrap">
        <thead><tr><th>№</th><th>Период</th><th>Партий</th><th>Счёт %</th><th>Ср. потеря win%</th><th>Win% до хода</th><th>Ошибок/партию</th><th>Неест. %</th><th>Ср. рейтинг</th></tr></thead>
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
      </table>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Тренд метрик</span></div>
      ${pr.trend ? `<div style="margin-bottom:8px"><span class="badge ${badgeClass}">${overall.toUpperCase()}</span></div>
      <table class="table-wrap"><thead><tr><th>Метрика</th><th>Было</th><th>Стало</th><th>Δ</th><th>Направление</th><th>Вердикт</th></tr></thead><tbody>${trendRows}</tbody></table>
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
      <td class="center">${g.analyzed ? '<span class="badge badge-good">анализ</span>' : '<span class="badge badge-neutral">нет</span>'}</td>
    </tr>`;
  }).join('');

  app.innerHTML = `
    <div class="card-header"><h1>Партии</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="card">
      <div class="games-filter">
        <button class="btn btn-secondary ${state.gamesFilter === 'all' ? 'btn-active' : ''}" data-gfilter="all">Все</button>
        <button class="btn btn-secondary ${state.gamesFilter === 'analyzed' ? 'btn-active' : ''}" data-gfilter="analyzed">Только проанализированные</button>
      </div>
      ${filtered.length ? `<table class="table-wrap"><thead><tr><th>Дата</th><th>Соперник</th><th>Цвет</th><th>Результат</th><th>Дебют</th><th class="num">Ходов</th><th>Анализ</th></tr></thead><tbody>${rows}</tbody></table>` : '<p style="color:var(--c-text-muted);padding:16px">Партий нет. Запусти анализ на странице «Анализ».</p>'}
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

  const lastMove = ply > 0 ? lastMoveSquares(fens[ply - 1], fens[ply]) : [];

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

  const moveCellHtml = m => m ? `<button class="move-cell${clsForPly(m.ply)}${ply === m.ply ? ' active' : ''}" data-ply="${m.ply}">${esc(m.san)}</button>` : '';

  const moveRowsHtml = moves.length
    ? Object.values(moveRows).map(r => `
        <div class="move-row">
          <span class="move-num">${r.num}.</span>
          <span class="move-w">${moveCellHtml(r.w)}</span>
          <span class="move-b">${moveCellHtml(r.b)}</span>
        </div>`).join('')
    : '<p style="color:var(--c-text-muted)">Ходов нет.</p>';

  app.innerHTML = `
    <div class="card-header">
      <h1 style="margin:0">Партия <code>${esc(game.id)}</code></h1>
      <span class="badge ${resBadge}">${resLabel}</span>
    </div>
    <div style="margin-bottom:16px"><a href="#" class="btn btn-secondary" data-view="games">← К списку партий</a></div>
    <div class="grid game-layout">
      <div class="card">
        ${boardHtml(fens[ply], { lastMove, flip: game.user_color === 'black' })}
        <div class="board-meta">
          <span class="badge badge-neutral">Ходят: ${turnLabel}</span>
          <span class="badge badge-blue">${esc(posLabel)}</span>
        </div>
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
        <div class="card">
          <div class="card-header"><span class="card-title">Ходы</span></div>
          <div class="moves-list">${moveRowsHtml}</div>
          <div class="game-links">
            ${game.link ? '<a class="btn btn-secondary" href="' + safeHttp(game.link) + '" target="_blank" rel="noopener">Открыть партию</a>' : ''}
          </div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">Анализ</span></div>
          ${analysis ? gameMetricsHtml(analysis) : '<p style="color:var(--c-text-muted)">Не проанализирована.</p>'}
        </div>
      </div>
    </div>`;

  bindLinks(app);
  bindBoard(app.querySelector('.board'));
  app.querySelectorAll('.move-cell').forEach(cell => {
    cell.addEventListener('click', () => {
      state.gamePly = +cell.dataset.ply;
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
      <div class="stat"><div class="stat-value">${avgLoss}</div><div class="stat-label">Ср. потеря win%</div></div>
      <div class="stat"><div class="stat-value">${avgBefore}</div><div class="stat-label">Win% до хода</div></div>
    </div>`;
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
    ${summary ? `<div class="grid grid-3"><div class="card stat"><div class="stat-value">${summary.found}</div><div class="stat-label">Ошибок найдено</div></div><div class="card stat"><div class="stat-value">${summary.games}</div><div class="stat-label">Партий</div></div><div class="card stat"><div class="stat-value">${summary.avg_drop != null ? fmt(summary.avg_drop) + '%' : '—'}</div><div class="stat-label">Ср. потеря win%</div></div></div>` : ''}
    <div class="card">
      <div class="card-header"><span class="card-title">Список ошибок (${items.length})</span><div><a href="/api/drills.pgn?${params}" class="btn btn-secondary" style="font-size:12px;padding:4px 10px">PGN</a> <a href="/api/drills.json?${params}" class="btn btn-secondary" style="font-size:12px;padding:4px 10px;margin-left:4px">JSON</a></div></div>
      ${items.length ? `<table class="table-wrap"><thead><tr><th>#</th><th>Партия</th><th>Ход</th><th>Сыграно</th><th>Лучше</th><th>Класс</th><th>Потеря</th><th>Win% до/после</th><th>Дебют</th><th>Результат</th></tr></thead><tbody>${items.map((d, i) => `<tr class="drill-row"><td class="num">${i + 1}</td><td>${d.link ? '<a href="' + safeHttp(d.link) + '" target="_blank" rel="noopener" title="Открыть партию">' + esc(d.game_id.slice(0, 8)) + '</a>' : esc(d.game_id.slice(0, 8))}</td><td class="num">${d.ply}</td><td class="drill-san">${esc(d.san_played)}</td><td class="drill-best">${esc(d.best_move_san)}</td><td><span class="badge ${d.classification === 'blunder' ? 'badge-bad' : 'badge-neutral'}">${d.classification}</span></td><td class="num">${fmt(d.drop)}%</td><td class="num">${fmt(d.win_before)}% → ${fmt(d.win_after)}%</td><td>${esc(d.opening)}</td><td class="center"><span class="badge ${d.result_for_user === 'win' ? 'badge-good' : d.result_for_user === 'loss' ? 'badge-bad' : 'badge-neutral'}">${d.result_for_user}</span></td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted);padding:16px">Ошибок не найдено.</p>'}
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
    ${summary ? `<div class="grid grid-3"><div class="card stat"><div class="stat-value">${summary.found}</div><div class="stat-label">Ошибок найдено</div></div><div class="card stat"><div class="stat-value">${summary.games}</div><div class="stat-label">Партий</div></div><div class="card stat"><div class="stat-value">${summary.avg_drop != null ? fmt(summary.avg_drop) + '%' : '—'}</div><div class="stat-label">Ср. потеря win%</div></div></div>` : ''}
    <div class="card">
      <div class="card-header"><span class="card-title">Список ошибок (${items.length})</span><div><a href="/api/drills.pgn?${params}" class="btn btn-secondary" style="font-size:12px;padding:4px 10px">PGN</a> <a href="/api/drills.json?${params}" class="btn btn-secondary" style="font-size:12px;padding:4px 10px;margin-left:4px">JSON</a></div></div>
      ${items.length ? `<table class="table-wrap"><thead><tr><th>#</th><th>Партия</th><th>Ход</th><th>Сыграно</th><th>Лучше</th><th>Класс</th><th>Потеря</th><th>Win% до/после</th><th>Дебют</th><th>Результат</th></tr></thead><tbody>${items.map((d, i) => `<tr class="drill-row"><td class="num">${i + 1}</td><td>${d.link ? '<a href="' + safeHttp(d.link) + '" target="_blank" rel="noopener" title="Открыть партию">' + esc(d.game_id.slice(0, 8)) + '</a>' : esc(d.game_id.slice(0, 8))}</td><td class="num">${d.ply}</td><td class="drill-san">${esc(d.san_played)}</td><td class="drill-best">${esc(d.best_move_san)}</td><td><span class="badge ${d.classification === 'blunder' ? 'badge-bad' : 'badge-neutral'}">${d.classification}</span></td><td class="num">${fmt(d.drop)}%</td><td class="num">${fmt(d.win_before)}% → ${fmt(d.win_after)}%</td><td>${esc(d.opening)}</td><td class="center"><span class="badge ${d.result_for_user === 'win' ? 'badge-good' : d.result_for_user === 'loss' ? 'badge-bad' : 'badge-neutral'}">${d.result_for_user}</span></td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted);padding:16px">Ошибок не найдено.</p>'}
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

function drawTask(app) {
  const task = state.task;
  if (!task) {
    app.innerHTML = '<div class="card"><p>Загрузка...</p></div>';
    return;
  }

  const answered = !!state.taskResult || !!task.done;
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
        ${boardHtml(task.fen, { flip: task.user_color === 'black' })}
        <div class="board-meta">
          <span class="badge badge-blue">${esc(task.date)}</span>
          <span class="badge badge-neutral">${esc(task.opening || '—')}${task.eco ? ` ${esc(task.eco)}` : ''}</span>
          <span class="badge badge-neutral">ты: ${colorLabel}</span>
          ${clsBadge}
        </div>
        ${answered ? '' : '<div id="task-board-hint" style="color:var(--c-text-muted);font-size:13px;margin-top:8px">кликни фигуру, затем клетку — или введи ход текстом</div>'}
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
  if (prevId !== s.id) state.openingFens = null;
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
            <button type="button" class="btn btn-secondary" data-opp="plus100" title="Соперник примерно на 100 пунктов сильнее тебя">+100</button>
            <button type="button" class="btn btn-secondary" data-opp="stronger" title="Соперник примерно на 200 пунктов сильнее тебя">сильнее (+200)</button>
          </div>
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
  app.querySelectorAll('#opening-form [data-opp]').forEach(btn => {
    btn.addEventListener('click', () => {
      app.querySelectorAll('#opening-form [data-opp]').forEach(b => b.classList.toggle('btn-active', b === btn));
      const my = document.getElementById('opening-my-elo');
      const opp = document.getElementById('opening-opp-elo');
      const base = Number(my && my.value) || 1500;
      const mode = btn.dataset.opp;
      if (mode === 'equal') opp.value = my && my.value ? my.value : '';
      else if (mode === 'plus100') opp.value = String(base + 100);
      else opp.value = String(base + 200);
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
      ${sessions.length ? `<table class="table-wrap"><thead><tr><th>Статус</th><th>Стиль</th><th>Цвет</th><th class="num">Ходы</th><th class="num">Рейтинги</th></tr></thead><tbody>${rows}</tbody></table>` : '<p style="color:var(--c-text-muted);padding:8px">Пока нет партий — начни первую.</p>'}
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
        ${boardHtml(s.fen, { lastMove: openingHighlight(), flip, animate: yourTurn && !state.openingThinking && moves.length > 0 })}
        <div class="board-meta">
          <span class="badge badge-neutral">${esc(openingStyleLabel(s.style))}</span>
          <span class="badge badge-blue">ты: ${colorLabel}</span>
          <span class="badge ${finished ? 'badge-good' : 'badge-neutral'}">${moves.length} / ${s.plies}</span>
          ${engineNote}
          ${state.openingThinking ? '<span class="badge badge-neutral">соперник думает…</span>' : ''}
        </div>
        ${replyHtml}
        <div style="margin-top:12px"><button class="btn btn-secondary" id="opening-new">Новая партия</button></div>
      </div>
      <div class="game-side">
        <div class="card">
          <div class="card-header"><span class="card-title">Ходы</span></div>
          <div class="moves-list">${openingMoveRowsHtml(moves)}</div>
        </div>
        ${panel}
      </div>
    </div>`;

  bindBoard(app.querySelector('.board'));
  const newBtn = document.getElementById('opening-new');
  if (newBtn) newBtn.addEventListener('click', () => {
    if (state.timers.opening) { clearInterval(state.timers.opening); state.timers.opening = null; }
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
  if (!finished && yourTurn && !state.openingThinking) {
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
    if (s.finished || s.turn !== s.color || state.openingThinking) { bindBoard(board); return; }
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
      body: JSON.stringify({ session_id: s.id }),
    });
    if (state.openingFens) state.openingFens.push(replyState.fen);
    state.openingSession = replyState;
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
      body: JSON.stringify({ session_id: s.id }),
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
const MIDDLEGAME_STRENGTH_LABELS = { equal: 'равный', plus100: '+100', plus200: '+200' };
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
                ${Object.keys(MIDDLEGAME_STRENGTH_LABELS).map(key => `<button type="button" class="btn btn-secondary ${key === strength ? 'btn-active' : ''}" data-strength="${esc(key)}">${esc(MIDDLEGAME_STRENGTH_LABELS[key])}</button>`).join('')}
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
      state.middlegameStrength = btn.dataset.strength;
      app.querySelectorAll('#middlegame-strength [data-strength]').forEach(b => b.classList.toggle('btn-active', b === btn));
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
    result = await api('/api/middlegame/session/' + encodeURIComponent(session.id) + '/reply', { method: 'POST' });
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
        <div class="card stat"><div class="stat-value">${fmt(report.avg.avg_win_loss, 2)}</div><div class="stat-label">Ср. потеря win%</div></div>
        <div class="card stat"><div class="stat-value">${report.total.blunders + report.total.mistakes}</div><div class="stat-label">Ошибок всего</div></div>
      </div>
      ${report.worst_games.length ? `<div class="card" style="margin-top:12px"><h3 style="font-size:14px;margin-bottom:8px">Худшие партии</h3><table class="table-wrap" style="font-size:12px"><thead><tr><th>Партия</th><th>Соперник</th><th>Результат</th><th>Ср. потеря</th><th>Win% до</th><th>Грубых</th></tr></thead><tbody>${report.worst_games.slice(0, 5).map(g => `<tr><td>${esc(g.game_id.slice(0, 8))}</td><td>${esc(g.opponent)}</td><td>${esc(g.result)}</td><td class="num">${fmt(g.avg_win_loss)}%</td><td class="num">${fmt(g.avg_win_before)}%</td><td class="num">${g.blunders}</td></tr>`).join('')}</tbody></table></div>` : ''}
      ${report.missed_wins.length ? `<div class="card" style="margin-top:12px"><h3 style="font-size:14px;margin-bottom:8px">Упущенные выигрыши (топ-5)</h3><table class="table-wrap" style="font-size:12px"><thead><tr><th>Партия</th><th>Ход</th><th>SAN</th><th>Win% до</th><th>Win% после</th><th>Лучший ход</th></tr></thead><tbody>${report.missed_wins.slice(0, 5).map(m => `<tr><td>${esc(m.game_id.slice(0, 8))}</td><td>${m.ply}</td><td class="drill-san">${esc(m.san)}</td><td class="num">${fmt(m.win_before)}%</td><td class="num">${fmt(m.win_after)}%</td><td class="drill-best">${esc(m.best_move_san)}</td></tr>`).join('')}</tbody></table></div>` : ''}
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
            <div class="form-group"><label>Глубина <input type="number" name="depth" value="14" min="1" max="30"></label></div>
            <div class="form-group"><label>Multipv <input type="number" name="multipv" value="3" min="1" max="10"></label></div>
          </div>
          <div class="checkbox"><input type="checkbox" name="cached_only" id="cached_only"><label for="cached_only">Только из кеша (не ходить в сеть)</label></div>
          <div class="checkbox"><input type="checkbox" name="refresh" id="refresh"><label for="refresh">Переанализировать всё заново</label></div>
          <div class="card" style="background:var(--c-bg-soft);font-size:13px;line-height:1.6">
            <b>💡 Какую глубину выбрать?</b><br>
            До 1500 — хватит глубины 8: ищи зевки и висячие фигуры.<br>
            1500–2000 — ставь 10–12: уже видна тактика и слабые поля.<br>
            2000+ — бери 12–14 и Multipv 2: сравнивай свою идею с первой линией.<br>
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
  container.innerHTML = `
    <div class="card job-card" id="job-card" style="margin-top:16px">
      <h3>Задача <code>${jobId.slice(0, 8)}</code> — <span id="job-status">RUNNING</span></h3>
      <div class="job-progress"><div class="job-progress-bar" id="progress-bar" style="width:0%"></div></div>
      <div class="job-phase" id="job-phase">Загрузка...</div>
      <div class="job-detail" id="job-detail">0 / 0</div>
      <div id="job-eta" style="color:var(--c-text-muted);font-size:13px"></div>
      <div id="job-quote" style="margin-top:10px;font-style:italic;color:var(--c-text-muted);font-size:13px;line-height:1.6"></div>
      <div id="job-error"></div>
      <div id="job-summary"></div>
    </div>`;

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
          const elapsed = (Date.now() - startedAt) / 1000;
          const remain = Math.round(elapsed / j.done * (j.total - j.done));
          const mm = Math.floor(remain / 60), ss = remain % 60;
          etaBox.textContent = `⏳ Прошло ${Math.floor(elapsed / 60)}:${String(Math.floor(elapsed % 60)).padStart(2, '0')} · осталось примерно ${mm}:${String(ss).padStart(2, '0')}`;
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
        document.getElementById('job-summary').innerHTML = `<div class="alert alert-success" style="margin-top:12px">${esc(j.summary)}</div>`;
        if (state.currentView === 'coach') setTimeout(() => navigate('coach'), 1500);
      } else if (j.status === 'error') {
        clearInterval(state.timers.coach);
        state.timers.coach = null;
        stopQuote();
        unlockCoachButton();
      }
    } catch (pollErr) {
      const msg = String((pollErr && pollErr.message) || '');
      if (/404/.test(msg)) {
        // Задача мертва (рестарт сервера): честный LOST + разблок.
        clearInterval(state.timers.coach);
        state.timers.coach = null;
        stopQuote();
        unlockCoachButton();
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
      ${lines.length ? `<table class="table-wrap">
        <thead><tr>${both ? '<th>Цвет</th>' : ''}<th>Ходы</th><th class="num">Партий</th><th class="num">Хорошо</th><th class="num">Плохо</th><th class="num">Прочность</th></tr></thead>
        <tbody>${lines.map(l => `<tr>
          ${both ? `<td>${TOOL_COLOR_RU[l.color] || esc(l.color)}</td>` : ''}
          <td><code>${esc((l.moves || []).join(' '))}</code></td>
          <td class="num">${l.count}</td><td class="num">${l.ok}</td><td class="num">${l.bad}</td>
          <td class="num">${l.path_ok_rate != null ? Math.round(l.path_ok_rate * 100) + '%' : '—'}</td>
        </tr>`).join('')}</tbody></table>`
        : '<p style="color:var(--c-text-muted);padding:8px">Нет данных: запусти анализ партий.</p>'}
      ${openings.length ? `
        <h3 style="font-size:14px;margin:16px 0 8px">Дебюты — ошибки по названиям</h3>
        <table class="table-wrap"><thead><tr><th>Дебют</th><th>ECO</th><th class="num">Партий</th><th class="num">Очков %</th><th class="num">Ошибок/партию</th><th class="num">Ср. потеря</th></tr></thead>
        <tbody>${openings.map(o => `<tr><td>${esc(o.opening)}</td><td>${esc(o.eco || '—')}</td><td class="num">${o.games}</td><td class="num">${fmt(o.points_pct)}%</td><td class="num">${fmt(o.errors_per_game, 2)}</td><td class="num">${fmt(o.avg_drop)}%</td></tr>`).join('')}</tbody></table>` : ''}
    </div>`;
}

function renderOpponent(d) {
  const box = document.getElementById('tool-result');
  const p = d.profile || {};
  const conf = d.confrontations || [];
  const rating = p.rating || {};
  const ratingText = rating.first != null ? `${rating.first} → ${rating.last} (${rating.delta >= 0 ? '+' : ''}${rating.delta})` : '—';
  const speed = Object.entries(p.speed || {}).map(([k, v]) => `${esc(k)}: ${v}`).join(', ') || '—';

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
      ${openRows.length ? `<table class="table-wrap"><thead><tr><th>Цвет</th><th>Дебют</th><th class="num">Партий</th><th class="num">Очков %</th><th class="num">Ошибок/партию</th><th class="num">Ср. потеря</th></tr></thead><tbody>${openRows.join('')}</tbody></table>`
        : '<p style="color:var(--c-text-muted)">Нет данных о его дебютах.</p>'}
      <h3 style="font-size:14px;margin:16px 0 8px">Где он ошибается</h3>
      ${weakRows.length ? `<table class="table-wrap"><thead><tr><th>Тип</th><th>Что</th><th class="num">Плохих ходов</th><th class="num">Ср. потеря</th></tr></thead><tbody>${weakRows.join('')}</tbody></table>`
        : '<p style="color:var(--c-text-muted)">Ошибок не найдено.</p>'}
      <h3 style="font-size:14px;margin:16px 0 8px">Точки встречи (его дебют → твой ответ)</h3>
      ${conf.length ? `<table class="table-wrap"><thead><tr><th>Дебют</th><th>Он</th><th>Твой ответ</th><th class="num">Его ошибок/парт.</th><th class="num">Твоих ошибок/парт.</th></tr></thead>
        <tbody>${conf.map(c => `<tr>
          <td>${esc(c.opening)}</td>
          <td>${TOOL_COLOR_RU[c.his_color]}: ${c.his_games} парт.</td>
          <td><code>${esc(c.your_reply)}</code> (${c.your_reply_games} парт.)</td>
           <td class="num">${fmt(c.his_errors_per_game, 2)}</td>
           <td class="num">${fmt(c.your_errors_per_game, 2)}</td>
        </tr>`).join('')}</tbody></table>`
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
      ${items.length ? `<table class="table-wrap">
        <thead><tr><th>Партия</th><th class="num">П.</th><th>Ход</th><th>Класс</th><th class="num">Потеря</th><th class="num">β</th><th>Вердикт</th></tr></thead>
        <tbody>${items.map(i => `<tr>
          <td>${esc(String(i.game_id || '').slice(0, 8))}</td>
          <td class="num">${i.ply}</td>
          <td class="drill-san">${esc(i.san || '')}</td>
          <td>${esc(i.classification || '—')}</td>
          <td class="num">${i.drop != null ? fmt(i.drop) + '%' : '—'}</td>
          <td class="num">${i.beta != null ? i.beta : '—'}</td>
          <td><span class="badge ${verdictBadge(i.verdict)}">${esc(i.verdict || '—')}</span></td>
        </tr>`).join('')}</tbody></table>`
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
      ${rows.length ? `<table class="table-wrap">
        <thead><tr><th>Соперник</th><th>Цвет</th><th class="num">Рейтинг</th><th>Результат</th><th class="num">Очки</th></tr></thead>
        <tbody>${rows.map(row => `<tr>
          <td>${esc(row.opponent)}</td>
          <td>${esc(row.color)}</td>
          <td class="num">${row.opponent_rating != null ? row.opponent_rating : '—'}</td>
          <td><span class="badge ${row.result === 'Победа' ? 'badge-good' : row.result === 'Поражение' ? 'badge-bad' : 'badge-neutral'}">${esc(row.result)}</span></td>
          <td class="num">${row.points}</td>
        </tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Партий нет.</p>'}
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
        <table class="table-wrap"><thead><tr><th>Контроль</th><th class="num">Сейчас</th><th class="num">Δ за окна</th><th class="center">Направление</th><th class="num">Периодов</th></tr></thead>
        <tbody>${summaryRows}</tbody></table>` : ''}
      ${winRows.length ? `<h3 style="font-size:14px;margin:16px 0 8px">Окна истории</h3>
        <table class="table-wrap"><thead><tr><th>Контроль</th><th class="num">№</th><th class="num">Периодов</th><th>Период</th><th class="num">Было → стало</th><th class="num">Δ</th></tr></thead>
        <tbody>${winRows.join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">История рейтингов пуста.</p>'}
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
      ${list.length ? `<table class="table-wrap"><thead><tr><th>Ник</th><th>Lichess</th><th>FIDE</th><th>Партий</th><th>Анализов</th><th>Последний вход</th></tr></thead><tbody>${list.map(u => {
        const isCurrent = String(u.nick || '').toLowerCase() === want;
        return `<tr>
          <td${isCurrent ? ' style="font-weight:600"' : ''}>${esc(u.nick)}${isCurrent ? ' <span class="badge badge-blue">это ты</span>' : ''}</td>
          <td>${esc(u.lichess || '—')}</td>
          <td>${esc(u.fide_id || '—')}</td>
          <td class="num">${u.games}</td>
          <td class="num">${u.analyses}</td>
          <td>${u.last_seen_at ? new Date(u.last_seen_at * 1000).toLocaleDateString('ru-RU') : '—'}</td>
        </tr>`;
      }).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted);padding:16px">Пользователей нет.</p>'}
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
}

init();