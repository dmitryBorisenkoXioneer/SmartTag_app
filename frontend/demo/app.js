const apiBase = ""; // same origin as demo_server

async function health() {
  const r = await fetch(`${apiBase}/api/health`);
  if (!r.ok) throw new Error(`health ${r.status}`);
  return r.json();
}

function setBusy(busy) {
  for (const id of ["btn-assembled", "btn-no-bearing", "btn-train"]) {
    document.getElementById(id).disabled = busy;
  }
}

/** 0..100 from RMS (mg); same bounds as backend RMS_DEV_LOW / RMS_DEV_HIGH. */
function pctFromRmsMg(rms, low, high) {
  if (rms == null || low == null || high == null) return null;
  const span = Math.max(Number(high) - Number(low), 1e-9);
  return Math.min(100, Math.max(0, (100 * (Number(rms) - Number(low))) / span));
}

/**
 * Prefer API stats.avg_deviation_pct / max (exact per-window SQL).
 * Fallback: older demo_server without those fields — approximate from aggregates + IF share.
 */
function deviationDisplay(st, sc) {
  const n = Number(st.n_windows);
  if (!n) return { avg: null, max: null, approx: false };
  const low = sc.rms_low_mg ?? 5;
  const high = sc.rms_high_mg ?? 85;
  const fl = sc.if_outlier_floor_pct ?? 90;
  if (st.avg_deviation_pct != null && st.max_deviation_pct != null) {
    return { avg: st.avg_deviation_pct, max: st.max_deviation_pct, approx: false };
  }
  if (st.avg_rms_mag == null || st.max_rms_mag == null) return { avg: null, max: null, approx: false };
  const avgLin = pctFromRmsMg(st.avg_rms_mag, low, high);
  const maxLin = pctFromRmsMg(st.max_rms_mag, low, high);
  const no = Number(st.n_if_outliers) || 0;
  const devOut = Math.min(100, Math.max(avgLin ?? 0, fl));
  const avgB = no ? ((n - no) * (avgLin ?? 0) + no * devOut) / n : avgLin;
  const maxB = no ? Math.max(maxLin ?? 0, fl) : maxLin;
  return {
    avg: avgB != null ? Math.round(avgB * 10) / 10 : null,
    max: maxB != null ? Math.round(maxB * 10) / 10 : null,
    approx: true,
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

function showResults(data) {
  const st = data.stats || {};
  const sc = data.deviation_scale || {};
  const dev = deviationDisplay(st, sc);
  document.getElementById("r-scenario").textContent = data.scenario_id || data.scenario || "—";
  document.getElementById("r-n").textContent = st.n_windows ?? "—";
  document.getElementById("r-dev-avg").textContent = dev.avg == null ? "—" : `${dev.avg} %`;
  document.getElementById("r-dev-max").textContent = dev.max == null ? "—" : `${dev.max} %`;
  document.getElementById("r-rms").textContent = st.avg_rms_mag ?? "—";
  document.getElementById("r-score").textContent =
    st.avg_anomaly_score == null || st.n_windows === 0 ? "—" : String(st.avg_anomaly_score);
  document.getElementById("r-out").textContent =
    st.n_if_outliers == null ? "—" : `${st.n_if_outliers} / ${st.n_windows || 0}`;
  document.getElementById("r-max").textContent = st.max_rms_mag ?? "—";
  setDeviationMeter(dev.avg);
  const scaleHint =
    sc.rms_low_mg != null
      ? ` Шкала: ${sc.rms_low_mg}–${sc.rms_high_mg} mg → 0–100%, IF ≥ ${sc.if_outlier_floor_pct}%.`
      : "";
  const approxHint = dev.approx
    ? " Проценты отклонения оценены в браузере (в ответе API нет avg_deviation_pct). Перезапустите uvicorn с актуальным demo_server.py — тогда значения считаются в SQL по каждому окну."
    : "";
  document.getElementById("r-hint").textContent = (data.hint || "") + scaleHint + approxHint;
  document.getElementById("raw").textContent = JSON.stringify(data, null, 2);
  document.getElementById("results").hidden = false;
  document.getElementById("log").hidden = false;
}

async function runScenario(scenario) {
  const status = document.getElementById("status-line");
  setBusy(true);
  status.textContent = "Публикация в MQTT…";
  try {
    const r = await fetch(`${apiBase}/api/demo/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario, duration_sec: 8, publish_hz: 20 }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || r.statusText);
    status.textContent = "Готово.";
    showResults(data);
  } catch (e) {
    status.textContent = `Ошибка: ${e.message || e}`;
  } finally {
    setBusy(false);
  }
}

async function runTrain() {
  const status = document.getElementById("status-line");
  setBusy(true);
  status.textContent = "Запуск train_if.py…";
  try {
    const r = await fetch(`${apiBase}/api/demo/train`, { method: "POST" });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || r.statusText);
    status.textContent = data.ok ? "Обучение завершилось." : "Обучение завершилось с ошибкой (см. сырой ответ).";
    document.getElementById("raw").textContent = JSON.stringify(data, null, 2);
    document.getElementById("log").hidden = false;
  } catch (e) {
    status.textContent = `Ошибка: ${e.message || e}`;
  } finally {
    setBusy(false);
  }
}

document.getElementById("btn-assembled").addEventListener("click", () => runScenario("assembled"));
document.getElementById("btn-no-bearing").addEventListener("click", () => runScenario("no_bearing"));
document.getElementById("btn-train").addEventListener("click", () => runTrain());

(async () => {
  const el = document.getElementById("status-line");
  try {
    const h = await health();
    const bits = [`Postgres: ${h.postgres ? "ok" : "нет"}`, `MODEL_PATH (на сервере UI): ${h.model_path_set ? "задан" : "нет"}`];
    const ds = h.deviation_scale;
    const scale =
      ds && ds.rms_low_mg != null
        ? ` Шкала отклонения: RMS ${ds.rms_low_mg}–${ds.rms_high_mg} mg → 0–100%, IF ≥ ${ds.if_outlier_floor_pct}%.`
        : "";
    el.textContent = `${bits.join(" · ")}. Запустите ingest с MODEL_PATH для IF в БД.${scale}`;
  } catch {
    el.textContent = "Нет связи с API. Запустите: uvicorn demo_server:app --host 127.0.0.1 --port 8787 из каталога backend.";
  }
})();
