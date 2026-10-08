// Chess Trainer Frontend — SPA на vanilla JS, потребляет /api/* эндпоинты.

const state = {
  user: null,
  currentView: 'dashboard',
  jobPollTimer: null,
  gamesFilter: 'all',
  gameId: null,
  gameData: null,
  gamePly: 0,
  keyHandler: null,
  tool: 'repertoire',
};

// --- API helpers ---

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

async function apiPost(path, formData) {
  const resp = await fetch(path, { method: 'POST', body: formData });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }));
    throw new Error(err.detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

// --- Navigation ---

const views = ['dashboard', 'overview', 'plan', 'progress', 'games', 'drills', 'coach', 'tools', 'users'];

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

// --- User box ---

function renderUserbox() {
  const box = document.getElementById('userbox');
  if (state.user) {
    box.innerHTML = `<span class="user-nick">${esc(state.user.nick)}</span><a href="#" class="user-switch" data-view="users">сменить</a>`;
  } else {
    box.innerHTML = `<a href="#" data-view="users">войти в профиль</a>`;
  }
  box.querySelectorAll('a').forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      navigate(a.dataset.view);
    });
  });
}

// --- Views ---

async function render(view) {
  const app = document.getElementById('app');
  clearGameKeyboard();
  if (state.jobPollTimer) {
    clearInterval(state.jobPollTimer);
    state.jobPollTimer = null;
  }
  app.innerHTML = '<div class="card"><p>Загрузка...</p></div>';

  try {
    switch (view) {
      case 'dashboard': await viewDashboard(app); break;
      case 'overview': await viewOverview(app); break;
      case 'plan': await viewPlan(app); break;
      case 'progress': await viewProgress(app); break;
      case 'games': await viewGames(app); break;
      case 'drills': await viewDrills(app); break;
      case 'coach': await viewCoach(app); break;
      case 'tools': await viewTools(app); break;
      case 'users': await viewUsers(app); break;
      default: app.innerHTML = '<div class="card"><p>Неизвестная страница</p></div>';
    }
  } catch (err) {
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

  const [plan, progress] = await Promise.all([
    api('/api/plan'),
    api('/api/progress?windows=5'),
  ]);

  const scorePct = plan.score_pct != null ? plan.score_pct.toFixed(1) + '%' : '—';
  const avgWinLoss = plan.avg_win_loss != null ? plan.avg_win_loss.toFixed(2) : '—';
  const weakCount = plan.repertoire.weak_openings.length;

  let humanityHtml = '';
  if (plan.humanity) {
    const h = plan.humanity;
    humanityHtml = `
      <div class="grid grid-3">
        <div class="stat"><div class="stat-value">${h.total}</div><div class="stat-label">Всего плохих</div></div>
        <div class="stat"><div class="stat-value">${h.unnatural}</div><div class="stat-label">Неестественных</div></div>
        <div class="stat"><div class="stat-value">${h.unnatural_share_pct.toFixed(1)}%</div><div class="stat-label">Доля неестественных</div></div>
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
              <td class="num">${o.points_pct.toFixed(1)}%</td>
              <td class="num">${o.errors_per_game.toFixed(2)}</td>
              <td class="num">${o.avg_drop.toFixed(1)}%</td>
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
        <td class="num">${v.from.toFixed(2)}</td>
        <td class="num">${v.to.toFixed(2)}</td>
        <td class="num ${deltaClass}">${v.delta >= 0 ? '+' : ''}${v.delta.toFixed(2)}</td>
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
            <tr><td>${esc(m[0])}</td><td class="num">${m[1].count}</td><td class="num">${m[1].avg_drop.toFixed(1)}%</td></tr>`).join('')}
        </tbody>
      </table>`;
  } else {
    motifsHtml = '<p style="color:var(--c-text-muted)">Нет данных.</p>';
  }

  app.innerHTML = `
    <div class="card-header"><h1>Дашборд</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="grid grid-4">
      <div class="card stat"><div class="stat-value">${plan.games}</div><div class="stat-label">Партий</div></div>
      <div class="card stat"><div class="stat-value">${scorePct}</div><div class="stat-label">Счёт</div></div>
      <div class="card stat"><div class="stat-value">${avgWinLoss}</div><div class="stat-label">Ср. потеря win%</div></div>
      <div class="card stat"><div class="stat-value">${weakCount}</div><div class="stat-label">Дебютов в фокусе</div></div>
    </div>
    <div class="grid grid-2">
      <div class="card">
        <div class="card-header"><span class="card-title">Дрели</span></div>
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
  const ov = await api('/api/overview');

  const whiteBranches = ov.branches.white || [];
  const blackBranches = ov.branches.black || [];

  app.innerHTML = `
    <div class="card-header"><h1>Обзор</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="grid grid-4">
      <div class="card stat"><div class="stat-value">${ov.games}</div><div class="stat-label">Партий</div></div>
      <div class="card stat"><div class="stat-value">${whiteBranches.length + blackBranches.length}</div><div class="stat-label">Ветвей репертуара</div></div>
      <div class="card stat"><div class="stat-value">${ov.openings.length}</div><div class="stat-label">Дебютов</div></div>
      <div class="card stat"><div class="stat-value">${ov.drills_total}</div><div class="stat-label">Дрелей</div></div>
    </div>
    <div class="grid grid-2">
      <div class="card">
        <div class="card-header"><span class="card-title">Репертуар белых</span></div>
        ${whiteBranches.length ? `<table class="table-wrap"><thead><tr><th>Ветвь</th><th>Партий</th><th>Прочность</th></tr></thead><tbody>${whiteBranches.map(b => `<tr><td><code>${esc(b.moves.join(' '))}</code></td><td class="num">${b.count}</td><td class="num">${(b.path_ok_rate * 100).toFixed(0)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Репертуар чёрных</span></div>
        ${blackBranches.length ? `<table class="table-wrap"><thead><tr><th>Ветвь</th><th>Партий</th><th>Прочность</th></tr></thead><tbody>${blackBranches.map(b => `<tr><td><code>${esc(b.moves.join(' '))}</code></td><td class="num">${b.count}</td><td class="num">${(b.path_ok_rate * 100).toFixed(0)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Слабые места репертуара</span></div>
      ${ov.openings.length ? `<table class="table-wrap"><thead><tr><th>Дебют</th><th>ECO</th><th>Партий</th><th>Очков %</th><th>Ошибок/партию</th><th>Ср. потеря</th></tr></thead><tbody>${ov.openings.slice(0, 12).map(o => `<tr><td>${esc(o.opening)}</td><td>${esc(o.eco)}</td><td class="num">${o.games}</td><td class="num">${o.points_pct.toFixed(1)}%</td><td class="num">${o.errors_per_game.toFixed(2)}</td><td class="num">${o.avg_drop.toFixed(1)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
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
  const plan = await api('/api/plan');

  const scorePct = plan.score_pct != null ? plan.score_pct.toFixed(1) + '%' : '—';
  const avgWinLoss = plan.avg_win_loss != null ? plan.avg_win_loss.toFixed(2) : '—';

  app.innerHTML = `
    <div class="card-header"><h1>План тренировки</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="grid grid-4">
      <div class="card stat"><div class="stat-value">${plan.games}</div><div class="stat-label">Партий</div></div>
      <div class="card stat"><div class="stat-value">${scorePct}</div><div class="stat-label">Счёт</div></div>
      <div class="card stat"><div class="stat-value">${avgWinLoss}</div><div class="stat-label">Ср. потеря win%</div></div>
      <div class="card stat"><div class="stat-value">${plan.drills.total}/${plan.drills.unnatural}</div><div class="stat-label">Дрели всего/неест.</div></div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Репертуар — частые ветви</span></div>
      <div class="grid grid-2">
        <div><h3>Белые</h3>${plan.repertoire.white.length ? `<table class="table-wrap" style="font-size:12px"><thead><tr><th>Ветвь</th><th>Партий</th><th>OK %</th></tr></thead><tbody>${plan.repertoire.white.map(b => `<tr><td><code>${esc(b.moves.join(' '))}</code></td><td class="num">${b.count}</td><td class="num">${(b.ok_rate * 100).toFixed(0)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}</div>
        <div><h3>Чёрные</h3>${plan.repertoire.black.length ? `<table class="table-wrap" style="font-size:12px"><thead><tr><th>Ветвь</th><th>Партий</th><th>OK %</th></tr></thead><tbody>${plan.repertoire.black.map(b => `<tr><td><code>${esc(b.moves.join(' '))}</code></td><td class="num">${b.count}</td><td class="num">${(b.ok_rate * 100).toFixed(0)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}</div>
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Слабые дебюты</span></div>
      ${plan.repertoire.weak_openings.length ? `<table class="table-wrap"><thead><tr><th>Дебют</th><th>ECO</th><th>Партий</th><th>Очков %</th><th>Ошибок/партию</th><th>Ср. потеря</th></tr></thead><tbody>${plan.repertoire.weak_openings.slice(0, 6).map(o => `<tr><td>${esc(o.opening)}</td><td>${esc(o.eco)}</td><td class="num">${o.games}</td><td class="num">${o.points_pct.toFixed(1)}%</td><td class="num">${o.errors_per_game.toFixed(2)}</td><td class="num">${o.avg_drop.toFixed(1)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет слабых дебютов.</p>'}
    </div>
    <div class="grid grid-2">
      <div class="card">
        <div class="card-header"><span class="card-title">Мотивы ошибок</span></div>
        ${plan.patterns.motifs.length ? `<table class="table-wrap"><thead><tr><th>Мотив</th><th>Частота</th><th>Ср. потеря</th></tr></thead><tbody>${plan.patterns.motifs.map(m => `<tr><td>${esc(m[0])}</td><td class="num">${m[1].count}</td><td class="num">${m[1].avg_drop.toFixed(1)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
      </div>
      <div class="card">
        <div class="card-header"><span class="card-title">Фазы ошибок</span></div>
        ${plan.patterns.phases.length ? `<table class="table-wrap"><thead><tr><th>Фаза</th><th>Ошибок</th><th>Грубых</th><th>Обычных</th><th>Ср. потеря</th></tr></thead><tbody>${plan.patterns.phases.map(p => `<tr><td>${esc(p[0])}</td><td class="num">${p[1].count}</td><td class="num">${p[1].blunders}</td><td class="num">${p[1].mistakes}</td><td class="num">${p[1].avg_drop.toFixed(1)}%</td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted)">Нет данных.</p>'}
      </div>
    </div>
    <div class="card">
      <div class="card-header"><span class="card-title">Человечность ошибок</span></div>
      ${plan.humanity ? `<div class="grid grid-4"><div class="stat"><div class="stat-value">${plan.humanity.total}</div><div class="stat-label">Всего</div></div><div class="stat"><div class="stat-value">${plan.humanity.natural}</div><div class="stat-label">Естественные</div></div><div class="stat"><div class="stat-value">${plan.humanity.borderline}</div><div class="stat-label">Пограничные</div></div><div class="stat"><div class="stat-value">${plan.humanity.unnatural}</div><div class="stat-label">Неестественные</div></div></div><div style="margin-top:8px"><strong>Доля неестественных: </strong>${plan.humanity.unnatural_share_pct.toFixed(1)}%</div>` : '<p style="color:var(--c-text-muted)">Нет данных humanize.</p>'}
    </div>`;
}

async function viewProgress(app) {
  if (!state.user) {
    app.innerHTML = '<div class="onboarding card"><div class="empty-icon">📈</div><h1>Нет данных</h1><p>Профиль не выбран.</p></div>';
    return;
  }
  const pr = await api('/api/progress?windows=5');

  let trendRows = '';
  if (pr.trend) {
    for (const [m, v] of Object.entries(pr.trend)) {
      if (['improving', 'worsening', 'overall'].includes(m)) continue;
      const deltaClass = v.delta > 0 ? 'badge-bad' : 'badge-good';
      trendRows += `<tr><td>${esc(m)}</td><td class="num">${v.from.toFixed(2)}</td><td class="num">${v.to.toFixed(2)}</td><td class="num ${deltaClass}">${v.delta >= 0 ? '+' : ''}${v.delta.toFixed(2)}</td><td>${v.direction}</td><td><span class="badge ${v.verdict === 'better' ? 'badge-good' : v.verdict === 'worse' ? 'badge-bad' : 'badge-neutral'}">${v.verdict}</span></td></tr>`;
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
            <td class="num">${w.score_pct != null ? w.score_pct.toFixed(1) : '—'}</td>
            <td class="num">${w.avg_win_loss != null ? w.avg_win_loss.toFixed(1) : '—'}</td>
            <td class="num">${w.avg_win_before != null ? w.avg_win_before.toFixed(1) : '—'}</td>
            <td class="num">${w.errors_per_game != null ? w.errors_per_game.toFixed(1) : '—'}</td>
            <td class="num">${w.unnatural_share_pct != null ? w.unnatural_share_pct.toFixed(1) : '—'}%</td>
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
        ${boardHtml(fens[ply], lastMove)}
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
            <a class="btn btn-secondary" href="https://lichess.org/${esc(game.id)}" target="_blank" rel="noopener">Открыть на Lichess</a>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><span class="card-title">Анализ</span></div>
          ${analysis ? gameMetricsHtml(analysis) : '<p style="color:var(--c-text-muted)">Не проанализирована.</p>'}
        </div>
      </div>
    </div>`;

  bindLinks(app);
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
  const avgLoss = analysis.avg_win_loss != null ? analysis.avg_win_loss.toFixed(1) : '—';
  const avgBefore = analysis.avg_win_before != null ? analysis.avg_win_before.toFixed(1) : '—';
  return `
    <div class="grid grid-3">
      <div class="stat"><div class="stat-value" style="color:#c5221f">${blunders}</div><div class="stat-label">Зевки</div></div>
      <div class="stat"><div class="stat-value" style="color:#d64f00">${mistakes}</div><div class="stat-label">Ошибки</div></div>
      <div class="stat"><div class="stat-value" style="color:#b8860b">${inaccuracies}</div><div class="stat-label">Неточности</div></div>
      <div class="stat"><div class="stat-value">${missed}</div><div class="stat-label">Упущ. выигрыши</div></div>
      <div class="stat"><div class="stat-value">${avgLoss}</div><div class="stat-label">Ср. потеря win%</div></div>
      <div class="stat"><div class="stat-value">${avgBefore}</div><div class="stat-label">Win% до хода</div></div>
    </div>`;
}

const PIECE_GLYPHS = {
  K: '♔', Q: '♕', R: '♖', B: '♗', N: '♘', P: '♙',
  k: '♚', q: '♛', r: '♜', b: '♝', n: '♞', p: '♟',
};
const FILES = 'abcdefgh';

function fenPieces(fen) {
  const placement = fen.split(' ')[0];
  const pieces = {};
  let rank = 8, file = 0;
  for (const ch of placement) {
    if (ch === '/') { rank--; file = 0; continue; }
    if (ch >= '1' && ch <= '8') { file += +ch; continue; }
    pieces[FILES[file] + rank] = ch;
    file++;
  }
  return pieces;
}

function lastMoveSquares(fenBefore, fenAfter) {
  const before = fenPieces(fenBefore);
  const after = fenPieces(fenAfter);
  const froms = [];
  const tos = [];
  for (let r = 8; r >= 1; r--) {
    for (let f = 0; f < 8; f++) {
      const sq = FILES[f] + r;
      if (before[sq] && !after[sq]) froms.push(sq);
      if (!before[sq] && after[sq]) tos.push(sq);
    }
  }
  if (froms.length === 2 && tos.length === 1) {
    // en passant: снятая пешка стоит на вертикали пункта назначения
    const to = tos[0];
    const captured = froms.find(s => s[0] === to[0]) || froms[1];
    const from = froms.find(s => s !== captured) || froms[0];
    return [from, to, captured];
  }
  return froms.concat(tos);
}

function boardHtml(fen, hlSquares) {
  const pieces = fenPieces(fen);
  const hl = hlSquares || [];
  let html = '';
  for (let rank = 8; rank >= 1; rank--) {
    for (let f = 0; f < 8; f++) {
      const square = FILES[f] + rank;
      const light = ((f + (8 - rank)) % 2 === 0);
      const cls = ['sq', light ? 'light' : 'dark'];
      if (hl.includes(square)) cls.push('lm');
      const piece = pieces[square];
      html += `<div class="${cls.join(' ')}">${piece ? `<span class="piece">${PIECE_GLYPHS[piece]}</span>` : ''}</div>`;
    }
  }
  return `<div class="board">${html}</div>`;
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
    app.innerHTML = '<div class="onboarding card"><div class="empty-icon">🎯</div><h1>Нет дрелей</h1><p>Профиль не выбран.</p></div>';
    return;
  }
  const params = new URLSearchParams({ min_drop: 15, min_win: 50 });
  const dr = await api('/api/drills?' + params);

  const summary = dr.summary;
  const items = dr.items;

  app.innerHTML = `
    <div class="card-header"><h1>Дрели</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="card">
      <form id="drills-filter" class="form-row" style="gap:16px;align-items:flex-end">
        <div class="form-group"><label>Мин. потеря win% <input type="number" name="min_drop" step="0.1" value="${dr.min_drop}"></label></div>
        <div class="form-group"><label>Мин. win% до хода <input type="number" name="min_win" step="0.1" value="${dr.min_win}"></label></div>
        <div class="form-group"><label>Вердикт <select name="verdict"><option value="">все</option><option value="unnatural">только неестественные</option><option value="natural,borderline">естественные + пограничные</option></select></label></div>
        <button class="btn btn-secondary" style="height:38px">Применить</button>
      </form>
    </div>
    ${summary ? `<div class="grid grid-3"><div class="card stat"><div class="stat-value">${summary.found}</div><div class="stat-label">Дрелей найдено</div></div><div class="card stat"><div class="stat-value">${summary.games}</div><div class="stat-label">Партий</div></div><div class="card stat"><div class="stat-value">${summary.avg_drop != null ? summary.avg_drop.toFixed(1) + '%' : '—'}</div><div class="stat-label">Ср. потеря win%</div></div></div>` : ''}
    <div class="card">
      <div class="card-header"><span class="card-title">Список дрелей (${items.length})</span><div><a href="/api/drills.pgn?${params}" class="btn btn-secondary" style="font-size:12px;padding:4px 10px">PGN</a> <a href="/api/drills.json?${params}" class="btn btn-secondary" style="font-size:12px;padding:4px 10px;margin-left:4px">JSON</a></div></div>
      ${items.length ? `<table class="table-wrap"><thead><tr><th>#</th><th>Партия</th><th>Ход</th><th>Сыграно</th><th>Лучше</th><th>Класс</th><th>Потеря</th><th>Win% до/после</th><th>Дебют</th><th>Результат</th></tr></thead><tbody>${items.map((d, i) => `<tr class="drill-row"><td class="num">${i + 1}</td><td>${esc(d.game_id.slice(0, 8))}</td><td class="num">${d.ply}</td><td class="drill-san">${esc(d.san_played)}</td><td class="drill-best">${esc(d.best_move_san)}</td><td><span class="badge ${d.classification === 'blunder' ? 'badge-bad' : 'badge-neutral'}">${d.classification}</span></td><td class="num">${d.drop.toFixed(1)}%</td><td class="num">${d.win_before.toFixed(1)}% → ${d.win_after.toFixed(1)}%</td><td>${esc(d.opening)}</td><td class="center"><span class="badge ${d.result_for_user === 'win' ? 'badge-good' : d.result_for_user === 'loss' ? 'badge-bad' : 'badge-neutral'}">${d.result_for_user}</span></td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted);padding:16px">Дрелей не найдено.</p>'}
    </div>`;

  document.getElementById('drills-filter').addEventListener('submit', e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const p = new URLSearchParams();
    if (fd.get('min_drop')) p.set('min_drop', fd.get('min_drop'));
    if (fd.get('min_win')) p.set('min_win', fd.get('min_win'));
    if (fd.get('verdict')) p.set('verdict', fd.get('verdict'));
    viewDrillsWithParams(app, p);
  });
}

async function viewDrillsWithParams(app, params) {
  const dr = await api('/api/drills?' + params);
  // re-render just the table part — simpler to re-call viewDrills with custom params
  const summary = dr.summary;
  const items = dr.items;
  app.innerHTML = `
    <div class="card-header"><h1>Дрели</h1><span class="badge badge-blue">${esc(state.user.nick)}</span></div>
    <div class="card">
      <form id="drills-filter" class="form-row" style="gap:16px;align-items:flex-end">
        <div class="form-group"><label>Мин. потеря win% <input type="number" name="min_drop" step="0.1" value="${dr.min_drop}"></label></div>
        <div class="form-group"><label>Мин. win% до хода <input type="number" name="min_win" step="0.1" value="${dr.min_win}"></label></div>
        <div class="form-group"><label>Вердикт <select name="verdict"><option value="">все</option><option value="unnatural">только неестественные</option><option value="natural,borderline">естественные + пограничные</option></select></label></div>
        <button class="btn btn-secondary" style="height:38px">Применить</button>
      </form>
    </div>
    ${summary ? `<div class="grid grid-3"><div class="card stat"><div class="stat-value">${summary.found}</div><div class="stat-label">Дрелей найдено</div></div><div class="card stat"><div class="stat-value">${summary.games}</div><div class="stat-label">Партий</div></div><div class="card stat"><div class="stat-value">${summary.avg_drop != null ? summary.avg_drop.toFixed(1) + '%' : '—'}</div><div class="stat-label">Ср. потеря win%</div></div></div>` : ''}
    <div class="card">
      <div class="card-header"><span class="card-title">Список дрелей (${items.length})</span><div><a href="/api/drills.pgn?${params}" class="btn btn-secondary" style="font-size:12px;padding:4px 10px">PGN</a> <a href="/api/drills.json?${params}" class="btn btn-secondary" style="font-size:12px;padding:4px 10px;margin-left:4px">JSON</a></div></div>
      ${items.length ? `<table class="table-wrap"><thead><tr><th>#</th><th>Партия</th><th>Ход</th><th>Сыграно</th><th>Лучше</th><th>Класс</th><th>Потеря</th><th>Win% до/после</th><th>Дебют</th><th>Результат</th></tr></thead><tbody>${items.map((d, i) => `<tr class="drill-row"><td class="num">${i + 1}</td><td>${esc(d.game_id.slice(0, 8))}</td><td class="num">${d.ply}</td><td class="drill-san">${esc(d.san_played)}</td><td class="drill-best">${esc(d.best_move_san)}</td><td><span class="badge ${d.classification === 'blunder' ? 'badge-bad' : 'badge-neutral'}">${d.classification}</span></td><td class="num">${d.drop.toFixed(1)}%</td><td class="num">${d.win_before.toFixed(1)}% → ${d.win_after.toFixed(1)}%</td><td>${esc(d.opening)}</td><td class="center"><span class="badge ${d.result_for_user === 'win' ? 'badge-good' : d.result_for_user === 'loss' ? 'badge-bad' : 'badge-neutral'}">${d.result_for_user}</span></td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted);padding:16px">Дрелей не найдено.</p>'}
    </div>`;
  document.getElementById('drills-filter').addEventListener('submit', e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const p = new URLSearchParams();
    if (fd.get('min_drop')) p.set('min_drop', fd.get('min_drop'));
    if (fd.get('min_win')) p.set('min_win', fd.get('min_win'));
    if (fd.get('verdict')) p.set('verdict', fd.get('verdict'));
    viewDrillsWithParams(app, p);
  });
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
        <div class="card stat"><div class="stat-value">${report.score.score_pct.toFixed(1)}%</div><div class="stat-label">Счёт</div></div>
        <div class="card stat"><div class="stat-value">${report.avg.avg_win_loss.toFixed(2)}</div><div class="stat-label">Ср. потеря win%</div></div>
        <div class="card stat"><div class="stat-value">${report.total.blunders + report.total.mistakes}</div><div class="stat-label">Ошибок всего</div></div>
      </div>
      ${report.worst_games.length ? `<div class="card" style="margin-top:12px"><h3 style="font-size:14px;margin-bottom:8px">Худшие партии</h3><table class="table-wrap" style="font-size:12px"><thead><tr><th>Партия</th><th>Соперник</th><th>Результат</th><th>Ср. потеря</th><th>Win% до</th><th>Грубых</th></tr></thead><tbody>${report.worst_games.slice(0, 5).map(g => `<tr><td>${esc(g.game_id.slice(0, 8))}</td><td>${esc(g.opponent)}</td><td>${esc(g.result)}</td><td class="num">${g.avg_win_loss.toFixed(1)}%</td><td class="num">${g.avg_win_before.toFixed(1)}%</td><td class="num">${g.blunders}</td></tr>`).join('')}</tbody></table></div>` : ''}
      ${report.missed_wins.length ? `<div class="card" style="margin-top:12px"><h3 style="font-size:14px;margin-bottom:8px">Упущенные выигрыши (топ-5)</h3><table class="table-wrap" style="font-size:12px"><thead><tr><th>Партия</th><th>Ход</th><th>SAN</th><th>Win% до</th><th>Win% после</th><th>Лучший ход</th></tr></thead><tbody>${report.missed_wins.slice(0, 5).map(m => `<tr><td>${esc(m.game_id.slice(0, 8))}</td><td>${m.ply}</td><td class="drill-san">${esc(m.san)}</td><td class="num">${m.win_before.toFixed(1)}%</td><td class="num">${m.win_after.toFixed(1)}%</td><td class="drill-best">${esc(m.best_move_san)}</td></tr>`).join('')}</tbody></table></div>` : ''}
      <div class="card" style="margin-top:12px"><h3 style="font-size:14px;margin-bottom:8px">Тайм-тробль</h3><div class="grid grid-3"><div class="stat"><div class="stat-value">${report.time_pressure.moves_count}</div><div class="stat-label">Ходов в ТТ</div></div><div class="stat"><div class="stat-value">${report.time_pressure.blunders_in_tp}</div><div class="stat-label">Грубых в ТТ</div></div><div class="stat"><div class="stat-value">${report.time_pressure.tp_blunder_rate_pct.toFixed(1)}%</div><div class="stat-label">% грубых в ТТ</div></div></div></div>`;
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
          <button type="submit" class="btn btn-primary" style="width:fit-content">Запустить</button>
        </form>
      </div>
      <div class="card"><h3>Последний отчёт</h3>${reportHtml}</div>
    </div>
    <div id="job-container"></div>`;

  document.getElementById('coach-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      const result = await apiPost('/api/coach/run', fd);
      showJob(app, result.job_id);
    } catch (err) {
      document.getElementById('job-container').innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
    }
  });

  // Check for running jobs
  try {
    const jobs = await api('/api/jobs');
    if (jobs && jobs.length > 0) {
      const running = jobs.find(j => j.status === 'running');
      if (running) showJob(app, running.id);
    }
  } catch { /* ignore */ }
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
      <div id="job-error"></div>
      <div id="job-summary"></div>
    </div>`;

  if (state.jobPollTimer) clearInterval(state.jobPollTimer);
  state.jobPollTimer = setInterval(async () => {
    try {
      const j = await api('/api/jobs/' + jobId);
      const bar = document.getElementById('progress-bar');
      const phase = document.getElementById('job-phase');
      const detail = document.getElementById('job-detail');
      const status = document.getElementById('job-status');
      if (!bar || !phase || !detail || !status) { clearInterval(state.jobPollTimer); return; }
      bar.style.width = j.total ? (j.done * 100 / j.total) + '%' : '0%';
      phase.textContent = j.phase;
      detail.textContent = j.done + ' / ' + j.total + (j.current ? ' (' + j.current.slice(0, 8) + ')' : '');
      status.textContent = j.status.toUpperCase();
      if (j.error) {
        document.getElementById('job-error').innerHTML = `<div class="job-error">${esc(j.error)}</div>`;
      }
      if (j.status === 'done') {
        clearInterval(state.jobPollTimer);
        document.getElementById('job-summary').innerHTML = `<div class="alert alert-success" style="margin-top:12px">${esc(j.summary)}</div>`;
        setTimeout(() => navigate('coach'), 1500);
      } else if (j.status === 'error') {
        clearInterval(state.jobPollTimer);
      }
    } catch { clearInterval(state.jobPollTimer); }
  }, 1000);
}

// --- Tools (репертуар / соперник / человечность / турнир / FIDE) ---

const TOOL_LABELS = {
  repertoire: 'Репертуар',
  opponent: 'Соперник',
  humanity: 'Человечность',
  tournament: 'Турнир',
  fide: 'FIDE',
};

const TOOL_COLOR_RU = { white: 'белые', black: 'чёрные' };
const FIDE_CONTROL_RU = { standard: 'Классика', rapid: 'Рапид', blitz: 'Блиц' };

async function viewTools(app) {
  const tool = TOOL_LABELS[state.tool] ? state.tool : 'repertoire';
  state.tool = tool;
  const needsUser = tool === 'repertoire' || tool === 'opponent' || tool === 'humanity';
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
  const prefill = state.user && state.user.fide_id ? state.user.fide_id : '';
  return `
    <form id="tool-form">
      <div class="form-row">
        <div class="form-group"><label>FIDE ID <input type="text" name="id" required placeholder="4130005" value="${esc(prefill)}"></label></div>
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
      }
    } catch (err) {
      toolError(err);
    }
  });
}

function toolError(err) {
  const box = document.getElementById('tool-result');
  if (box) box.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
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

  if (state.jobPollTimer) clearInterval(state.jobPollTimer);
  state.jobPollTimer = setInterval(async () => {
    const bar = document.getElementById('tool-bar');
    if (!bar) { clearInterval(state.jobPollTimer); state.jobPollTimer = null; return; }
    let j;
    try {
      j = await api('/api/jobs/' + jobId);
    } catch {
      clearInterval(state.jobPollTimer);
      state.jobPollTimer = null;
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
      clearInterval(state.jobPollTimer);
      state.jobPollTimer = null;
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
        <tbody>${openings.map(o => `<tr><td>${esc(o.opening)}</td><td>${esc(o.eco || '—')}</td><td class="num">${o.games}</td><td class="num">${o.points_pct.toFixed(1)}%</td><td class="num">${o.errors_per_game.toFixed(2)}</td><td class="num">${o.avg_drop.toFixed(1)}%</td></tr>`).join('')}</tbody></table>` : ''}
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
        <td class="num">${o.games}</td><td class="num">${o.points_pct.toFixed(1)}%</td>
        <td class="num">${o.errors_per_game.toFixed(2)}</td><td class="num">${o.avg_drop.toFixed(1)}%</td>
      </tr>`);
    }
  }

  const weakness = p.weaknesses || {};
  const weakRows = [
    ...(weakness.phases || []).map(w => `<tr><td>фаза</td><td>${esc(w.label)}</td><td class="num">${w.bad_moves}</td><td class="num">${w.avg_drop != null ? w.avg_drop.toFixed(1) + '%' : '—'}</td></tr>`),
    ...(weakness.motifs || []).map(w => `<tr><td>мотив</td><td>${esc(w.motif)}</td><td class="num">${w.bad_moves}</td><td class="num">${w.avg_drop != null ? w.avg_drop.toFixed(1) + '%' : '—'}</td></tr>`),
  ];

  box.innerHTML = `
    <div class="card">
      <div class="card-header">
        <span class="card-title">Подготовка: ${esc(p.opponent || '')}</span>
        <span class="badge badge-blue">${p.games != null ? p.games : 0} партий</span>
      </div>
      ${p.small_sample ? '<div class="alert alert-warn">Меньше 20 партий — выводы о репертуаре соперника шумные.</div>' : ''}
      <div class="grid grid-4">
        <div class="stat"><div class="stat-value">${p.score_pct != null ? p.score_pct.toFixed(1) + '%' : '—'}</div><div class="stat-label">Его очки</div></div>
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
          <td class="num">${c.his_errors_per_game.toFixed(2)}</td>
          <td class="num">${c.your_errors_per_game.toFixed(2)}</td>
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
          <td class="num">${i.drop != null ? i.drop.toFixed(1) + '%' : '—'}</td>
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

async function viewUsers(app) {
  const users = await api('/api/users');
  const list = users.users;

  app.innerHTML = `
    <div class="card-header"><h1>Профили</h1></div>
    ${state.user ? `<div class="card" style="margin-bottom:16px"><div class="card-header"><span class="card-title">Текущий профиль</span></div><div class="grid grid-3"><div class="stat"><div class="stat-value">${esc(state.user.nick)}</div><div class="stat-label">Ник</div></div><div class="stat"><div class="stat-value">${state.user.fide_id || '—'}</div><div class="stat-label">FIDE ID</div></div><div class="stat"><div class="stat-value">${state.user.games || 0}</div><div class="stat-label">Партий</div></div></div></div>` : '<div class="alert alert-info">Профиль не выбран. Добавь профиль или выбери из списка.</div>'}
    <div class="card">
      <div class="card-header"><span class="card-title">Добавить профиль</span></div>
      <form id="add-user-form" class="form-row" style="gap:16px;align-items:flex-end">
        <div class="form-group" style="flex:1;min-width:200px"><label>Ник <input type="text" name="nick" required placeholder="Tleukhanov"></label></div>
        <div class="form-group"><label>FIDE ID (опц.) <input type="text" name="fide" placeholder="4130005"></label></div>
        <button class="btn btn-primary" style="height:38px">Добавить и закрепить</button>
      </form>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="card-header"><span class="card-title">Все профили (${list.length})</span></div>
      ${list.length ? `<table class="table-wrap"><thead><tr><th>Ник</th><th>FIDE</th><th>Партий</th><th>Анализов</th><th>Последний вход</th><th>Действия</th></tr></thead><tbody>${list.map(u => `<tr><td${u.nick === (state.user ? state.user.nick : '') ? ' style="font-weight:600"' : ''}>${esc(u.nick)}${u.nick === (state.user ? state.user.nick : '') ? ' <span class="badge badge-blue">текущий</span>' : ''}</td><td>${esc(u.fide_id || '—')}</td><td class="num">${u.games}</td><td class="num">${u.analyses}</td><td>${u.last_seen_at ? new Date(u.last_seen_at * 1000).toLocaleDateString('ru-RU') : '—'}</td><td><form method="post" action="/api/user/switch" style="display:inline"><input type="hidden" name="nick" value="${esc(u.nick)}"><button class="btn ${u.nick === (state.user ? state.user.nick : '') ? 'btn-secondary' : 'btn-primary'}" style="font-size:12px;padding:4px 10px" ${u.nick === (state.user ? state.user.nick : '') ? 'disabled' : ''}>${u.nick === (state.user ? state.user.nick : '') ? 'Закреплён' : 'Закрепить'}</button></form></td></tr>`).join('')}</tbody></table>` : '<p style="color:var(--c-text-muted);padding:16px">Профилей нет. Добавь первый выше.</p>'}
    </div>`;

  document.getElementById('add-user-form').addEventListener('submit', async e => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      await apiPost('/api/user/add', fd);
      await loadUser();
      navigate('users');
    } catch (err) {
      alert(err.message);
    }
  });
}

// --- Utilities ---

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
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
  try {
    const resp = await api('/api/user/current');
    state.user = resp.user;
  } catch {
    state.user = null;
  }
  renderUserbox();
}

async function init() {
  setupNav();
  await loadUser();
  navigate('dashboard');
}

init();