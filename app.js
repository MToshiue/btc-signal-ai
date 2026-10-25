const $ = (id) => document.getElementById(id);
const fmt = (v, digits=2) => v === null || v === undefined || Number.isNaN(Number(v)) ? "—" : Number(v).toLocaleString("en-US",{minimumFractionDigits:digits,maximumFractionDigits:digits});
function renderSignal(s) {
  const n = s.horizon_minutes;
  const d = $("direction-"+n);
  d.textContent = s.direction;
  d.className = "direction " + s.direction.toLowerCase();
  $("strength-"+n).textContent = s.model_strength + "/100";
  $("meter-"+n).style.width = s.model_strength + "%";
  $("meter-"+n).style.background = s.direction === "UP" ? "var(--green)" : s.direction === "DOWN" ? "var(--red)" : "var(--yellow)";
  $("score-"+n).textContent = s.score;
  const list = $("reasons-"+n);
  list.innerHTML = "";
  (s.reasons || []).forEach(reason => {
    const li = document.createElement("li");
    li.textContent = reason;
    list.appendChild(li);
  });
}
async function refresh() {
  const btn = $("refresh");
  btn.disabled = true;
  $("status").textContent = "Получение данных с OKX…";
  try {
    const response = await fetch("/api/analysis", {cache:"no-store"});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Ошибка API");
    $("price").textContent = "$" + fmt(data.price);
    $("updated").textContent = "Свеча: " + new Date(data.candle_time).toLocaleString();
    data.signals.forEach(renderSignal);
    const s = data.signals[0];
    $("rsi").textContent = fmt(s.rsi14);
    $("ema9").textContent = "$" + fmt(s.ema9);
    $("ema21").textContent = "$" + fmt(s.ema21);
    $("macd").textContent = fmt(s.macd,4);
    $("momentum").textContent = fmt(s.momentum3_pct,4) + "%";
    $("atr").textContent = "$" + fmt(s.atr14);
    $("status").textContent = "Обновлено: " + new Date(data.updated_at).toLocaleTimeString() + " · автообновление каждые 15 секунд";
  } catch (err) {
    $("status").textContent = "Ошибка: " + err.message + " · попробуйте обновить страницу";
  } finally {
    btn.disabled = false;
  }
}
$("refresh").addEventListener("click", refresh);
refresh();
setInterval(refresh, 15000);
