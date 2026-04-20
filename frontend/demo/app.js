const apiBase = "";
let pollTimer = null;
let previousStatus = null;
let apiErrorActive = false;
const eventLog = [];
const maxEventLogItems = 25;

async function requestJson(url, options) {
  const r = await fetch(url, options);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `${r.status}`);
  return data;
}

async function health() {
  return requestJson(`${apiBase}/api/health`);
}

async function liveStatus() {
  return requestJson(`${apiBase}/api/live/status`);
}

function setButtons(status) {
  const start = document.getElementById("btn-train-start");
  const stop = document.getElementById("btn-train-stop");
  const saveTarget = document.getElementById("btn-train-target-save");
  const targetInput = document.getElementById("train-target-windows");
  const mode = status?.mode;
  const training = status?.training || {};
  const isOnline = Boolean(status?.device_status?.online);
  start.disabled = !isOnline || training.in_progress || training.enabled;
  stop.disabled = training.in_progress || !training.enabled;
  saveTarget.disabled = training.in_progress || training.enabled;
  targetInput.disabled = training.in_progress || training.enabled;
  if (mode === "training") stop.disabled = true;
}

function modeLabel(mode) {
  if (mode === "detecting") return "Детектирование";
  if (mode === "training") return "Обучение модели";
  return "Нет обученной модели";
}

function formatPrettyDateTime(value) {
  if (!value) return "—";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(dt);
}

function pushEvent(type, message, at = new Date().toISOString()) {
  eventLog.unshift({ type, message, at });
  if (eventLog.length > maxEventLogItems) {
    eventLog.length = maxEventLogItems;
  }
}

function renderEventLog() {
  const list = document.getElementById("event-log");
  const empty = document.getElementById("event-log-empty");
  if (!eventLog.length) {
    list.hidden = true;
    empty.hidden = false;
    list.innerHTML = "";
    return;
  }

  empty.hidden = true;
  list.hidden = false;
  list.innerHTML = eventLog
    .map(
      (entry) => `
        <li class="event-log__item">
          <div class="event-log__meta">
            <span class="event-log__type">${entry.type}</span>
            <span>${formatPrettyDateTime(entry.at)}</span>
          </div>
          <div class="event-log__message">${entry.message}</div>
        </li>
      `,
    )
    .join("");
}

function collectEvents(data) {
  const tr = data.training || {};
  const latest = data.latest_window || {};
  const deviceStatus = data.device_status || {};

  if (!previousStatus) {
    pushEvent("Система", `Панель подключена. Текущий режим: ${modeLabel(data.mode)}.`);
    if (deviceStatus.online) {
      pushEvent("Устройство", "Устройство онлайн.");
    }
    previousStatus = {
      mode: data.mode,
      online: Boolean(deviceStatus.online),
      trainingEnabled: Boolean(tr.enabled),
      trainingInProgress: Boolean(tr.in_progress),
      trainingFinishedAt: tr.finished_at || null,
      targetWindows: tr.target_windows ?? null,
      latestWindowAt: latest.received_at || null,
    };
    return;
  }

  const prev = previousStatus;
  const online = Boolean(deviceStatus.online);
  if (prev.online !== online) {
    pushEvent("Устройство", online ? "Устройство снова онлайн." : "Устройство перешло в оффлайн.");
  }

  if (prev.mode !== data.mode) {
    pushEvent("Режим", `Режим изменён: ${modeLabel(prev.mode)} -> ${modeLabel(data.mode)}.`);
  }

  if (!prev.trainingEnabled && tr.enabled) {
    pushEvent("Обучение", `Сбор данных начат. Цель: ${tr.target_windows || "—"} окон.`);
  } else if (prev.trainingEnabled && !tr.enabled && !tr.in_progress) {
    pushEvent("Обучение", "Сбор данных остановлен.");
  }

  if (!prev.trainingInProgress && tr.in_progress) {
    pushEvent("Обучение", "Запущено обучение модели.");
  } else if (prev.trainingInProgress && !tr.in_progress) {
    pushEvent("Обучение", data.last_training_result?.ok ? "Обучение модели завершено успешно." : "Обучение модели завершилось с ошибкой.");
  }

  if (prev.targetWindows !== tr.target_windows && tr.target_windows != null) {
    pushEvent("Настройки", `Размер тренировочного набора изменён на ${tr.target_windows} окон.`);
  }

  if (
    latest.received_at &&
    latest.received_at !== prev.latestWindowAt &&
    latest.if_outlier === true
  ) {
    pushEvent(
      "Детект",
      `Обнаружен выброс: RMS ${Number(latest.rms_mag).toFixed(3)} mg, score ${Number(latest.anomaly_score).toFixed(6)}.`,
      latest.received_at,
    );
  }

  previousStatus = {
    mode: data.mode,
    online,
    trainingEnabled: Boolean(tr.enabled),
    trainingInProgress: Boolean(tr.in_progress),
    trainingFinishedAt: tr.finished_at || null,
    targetWindows: tr.target_windows ?? null,
    latestWindowAt: latest.received_at || null,
  };
}

function setDeviationMeter(avg) {
  const meter = document.getElementById("deviation-meter");
  const value = document.getElementById("deviation-meter-value");
  const mask = document.getElementById("deviation-meter-mask");
  const thumb = document.getElementById("deviation-meter-thumb");
  if (avg == null) {
    meter.hidden = true;
    value.textContent = "—";
    mask.style.width = "100%";
    thumb.style.left = "0%";
    return;
  }
  const pct = Math.max(0, Math.min(100, Number(avg)));
  meter.hidden = false;
  value.textContent = `${pct.toFixed(1)} %`;
  mask.style.width = `${100 - pct}%`;
  thumb.style.left = `${pct}%`;
}

function renderStatus(data, healthData) {
  const tr = data.training || {};
  const latest = data.latest_window || {};
  const deviceStatus = data.device_status || {};
  const scale = data.deviation_scale || healthData?.deviation_scale || {};
  const isOnline = Boolean(deviceStatus.online);

  document.getElementById("status-line").textContent =
    `Postgres: ${healthData?.postgres ? "ok" : "нет"} · ${data.transition_reason || "—"}`;
  document.getElementById("mode-value").textContent = modeLabel(data.mode);
  document.getElementById("mode-pill").textContent = tr.enabled
    ? "Сбор данных"
    : tr.in_progress
      ? "Идёт fit"
      : data.mode === "detecting"
        ? "Модель активна"
        : "Нет модели";
  const onlinePill = document.getElementById("device-online-pill");
  onlinePill.textContent = deviceStatus.label || "Оффлайн";
  onlinePill.classList.toggle("pill--online", Boolean(deviceStatus.online));
  onlinePill.classList.toggle("pill--offline", !deviceStatus.online);
  document.getElementById("device-value").textContent = data.device_id || "—";
  document.getElementById("device-last-seen").textContent =
    deviceStatus.last_seen_ago_sec == null
      ? "Данных ещё нет"
      : `Последний пакет ${deviceStatus.last_seen_ago_sec} с назад`;

  const progress = tr.progress_pct == null ? 0 : Number(tr.progress_pct);
  const progressCard = document.getElementById("train-progress-card");
  const showProgress = Boolean(tr.enabled || tr.in_progress || data.mode === "training");
  progressCard.hidden = !showProgress;
  document.getElementById("train-progress-fill").style.width = `${Math.max(0, Math.min(100, progress))}%`;
  document.getElementById("train-progress-value").textContent =
    tr.progress_pct == null ? "—" : `${Number(tr.progress_pct).toFixed(1)} %`;
  document.getElementById("train-progress-count").textContent =
    tr.target_windows != null ? `${tr.windows_collected || 0} / ${tr.target_windows} окон` : "—";
  document.getElementById("train-progress-reason").textContent =
    data.transition_reason || "—";
  const targetInput = document.getElementById("train-target-windows");
  if (document.activeElement !== targetInput && tr.target_windows != null) {
    targetInput.value = String(tr.target_windows);
  }
  document.getElementById("offline-sensitive-info").hidden = !isOnline;

  document.getElementById("r-scenario").textContent = latest.scenario_id || "—";
  document.getElementById("r-window-start").textContent = formatPrettyDateTime(latest.window_start);
  document.getElementById("r-rms").textContent = latest.rms_mag ?? "—";
  document.getElementById("r-score").textContent = latest.anomaly_score ?? "—";
  document.getElementById("r-out").textContent =
    latest.if_outlier == null ? "—" : latest.if_outlier ? "Да" : "Нет";
  document.getElementById("r-dev-avg").textContent =
    latest.deviation_pct == null ? "—" : `${latest.deviation_pct} %`;

  const hint = [
    scale.if_score_bad_threshold != null
      ? `Шкала отклонения: 0% при положительном score, 100% при score <= -${scale.if_score_bad_threshold}.`
      : "",
    latest.status_label ? `Последнее решение: ${latest.status_label}.` : "Ждём первое окно от ESP32.",
    data.last_training_result?.stderr ? `Последняя ошибка обучения: ${data.last_training_result.stderr}` : "",
  ]
    .filter(Boolean)
    .join(" ");
  document.getElementById("r-hint").textContent = hint || "—";

  setDeviationMeter(latest.deviation_pct);
  collectEvents(data);
  renderEventLog();
  document.getElementById("results").hidden = false;
  document.getElementById("log").hidden = false;
  setButtons(data);
}

async function refresh() {
  try {
    const [h, status] = await Promise.all([health(), liveStatus()]);
    if (apiErrorActive) {
      pushEvent("API", "Связь с backend восстановлена.");
      apiErrorActive = false;
    }
    renderStatus(status, h);
  } catch (e) {
    if (!apiErrorActive) {
      pushEvent("API", `Ошибка связи с backend: ${e.message || e}.`);
      apiErrorActive = true;
      renderEventLog();
      document.getElementById("log").hidden = false;
    }
    document.getElementById("status-line").textContent =
      `Ошибка API: ${e.message || e}. Запустите uvicorn demo_server:app --host 127.0.0.1 --port 8787`;
  }
}

async function startTraining() {
  try {
    await requestJson(`${apiBase}/api/live/training/start`, { method: "POST" });
    await refresh();
  } catch (e) {
    document.getElementById("status-line").textContent = `Ошибка запуска обучения: ${e.message || e}`;
  }
}

async function stopTraining() {
  try {
    await requestJson(`${apiBase}/api/live/training/stop`, { method: "POST" });
    await refresh();
  } catch (e) {
    document.getElementById("status-line").textContent = `Ошибка остановки обучения: ${e.message || e}`;
  }
}

async function saveTrainingTarget() {
  const input = document.getElementById("train-target-windows");
  const value = Number(input.value);
  if (!Number.isInteger(value) || value < 10) {
    document.getElementById("status-line").textContent = "Количество окон должно быть целым числом >= 10";
    return;
  }
  try {
    await requestJson(`${apiBase}/api/live/training/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_windows: value }),
    });
    await refresh();
  } catch (e) {
    document.getElementById("status-line").textContent = `Ошибка сохранения параметра: ${e.message || e}`;
  }
}

document.getElementById("btn-train-start").addEventListener("click", startTraining);
document.getElementById("btn-train-stop").addEventListener("click", stopTraining);
document.getElementById("btn-train-target-save").addEventListener("click", saveTrainingTarget);

(async () => {
  await refresh();
  pollTimer = window.setInterval(refresh, 1500);
})();
