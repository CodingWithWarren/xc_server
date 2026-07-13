// The dashboard is a pure API client: it only fetches the JSON endpoints,
// never touches the database directly. All data calls carry our Bearer token
// (see auth.js); athletes see their own data, coaches pick from the roster.

const PT = "America/Los_Angeles";  // show all times in Pacific (PST/PDT)

// DB datetime columns come back without a timezone; treat them as UTC so they
// aren't misread as browser-local.
function toDate(iso) {
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
}

const fmtMi = (m) => (m == null ? "—" : (m / 1609.344).toFixed(2) + " mi");
const fmtHr = (bpm) => (bpm == null ? "—" : bpm + " bpm");

function fmtDate(iso) {
  return toDate(iso).toLocaleDateString("en-US", {
    month: "short", day: "numeric", timeZone: PT,
  });
}

function fmtDuration(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

// Totals read better without seconds: "3h 42m" / "47m".
function fmtTotalTime(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${m}m`;
}

// ---- This-week hero -----------------------------------------------------

function setDelta(id, diff, text) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = "delta " + (diff > 0 ? "up" : diff < 0 ? "down" : "flat");
}

function renderSummary(s) {
  const tw = s.this_week, lw = s.last_week;

  const start = toDate(tw.week_start + "T12:00:00Z");
  document.getElementById("weekRange").textContent =
    "week of " + start.toLocaleDateString("en-US", { month: "short", day: "numeric" });

  document.getElementById("wkDistance").textContent =
    (tw.total_distance_meters / 1609.344).toFixed(1) + " mi";
  document.getElementById("wkTime").textContent =
    fmtTotalTime(tw.total_duration_seconds);
  document.getElementById("wkRuns").textContent = tw.run_count;

  if (lw.session_count === 0 && tw.session_count === 0) {
    setDelta("wkDistanceDelta", 0, "no activity yet");
    setDelta("wkTimeDelta", 0, "no activity yet");
    setDelta("wkRunsDelta", 0, "no activity yet");
    return;
  }

  const dDist = tw.total_distance_meters - lw.total_distance_meters;
  setDelta("wkDistanceDelta", dDist,
    `${dDist >= 0 ? "+" : "−"}${Math.abs(dDist / 1609.344).toFixed(1)} mi vs last week`);

  const dTime = tw.total_duration_seconds - lw.total_duration_seconds;
  setDelta("wkTimeDelta", dTime,
    `${dTime >= 0 ? "+" : "−"}${fmtTotalTime(Math.abs(dTime))} vs last week`);

  const dRuns = tw.run_count - lw.run_count;
  setDelta("wkRunsDelta", dRuns,
    `${dRuns >= 0 ? "+" : "−"}${Math.abs(dRuns)} vs last week`);
}

// ---- Recent workouts ----------------------------------------------------

// One list combining detected sessions with any recorded workouts that have no
// matching session (e.g. manual entries with no sensor samples).
function buildRecent(sessions, workouts) {
  const matched = new Set(
    sessions.map((s) => s.matched_workout_uuid).filter(Boolean));

  const items = sessions.map((s) => ({
    href: `/session.html?id=${s.id}`,
    date: s.start_time,
    type: s.matched_activity_type || s.inferred_activity || "—",
    duration: s.duration_seconds,
    distance: s.total_distance_meters,
    avgHr: s.avg_hr,
    badge: s.matched_workout_uuid ? "recorded" : "detected",
  }));

  for (const w of workouts) {
    if (matched.has(w.source_uuid)) continue;
    items.push({
      href: `/workout.html?uuid=${encodeURIComponent(w.source_uuid)}`,
      date: w.start_time,
      type: w.activity_type || "—",
      duration: w.duration_seconds,
      distance: w.total_distance_meters,
      avgHr: w.avg_heart_rate,
      badge: "recorded",
    });
  }

  return items.sort((a, b) => toDate(b.date) - toDate(a.date)).slice(0, 10);
}

function renderRecent(items) {
  const tbody = document.querySelector("#recentTable tbody");
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">No workouts yet — sync from the app.</td></tr>`;
    return;
  }
  tbody.innerHTML = items.map((it) => `
    <tr class="clickable" onclick="location.href='${it.href}'">
      <td>${fmtDate(it.date)}</td>
      <td>${escapeHtml(it.type)}</td>
      <td class="num">${it.duration == null ? "—" : fmtDuration(it.duration)}</td>
      <td class="num">${fmtMi(it.distance)}</td>
      <td class="num">${fmtHr(it.avgHr)}</td>
      <td><span class="badge badge-${it.badge}">${it.badge}</span></td>
    </tr>`).join("");
}

// ---- Weekly chart -------------------------------------------------------

let weeklyChart = null;  // destroyed on re-render when a coach switches athletes

function renderWeeklyChart(weeks) {
  const canvas = document.getElementById("weeklyChart");
  if (weeklyChart) { weeklyChart.destroy(); weeklyChart = null; }
  if (!weeks.length) {
    canvas.hidden = true;
    document.getElementById("chartEmpty").hidden = false;
    return;
  }
  canvas.hidden = false;
  document.getElementById("chartEmpty").hidden = true;
  weeklyChart = new Chart(canvas, {
    type: "line",
    data: {
      labels: weeks.map((w) => w.week_start),
      datasets: [{
        label: "Distance (mi)",
        data: weeks.map((w) => +(w.total_distance_meters / 1609.344).toFixed(2)),
        borderColor: "#2f6fed",
        backgroundColor: "rgba(47, 111, 237, 0.10)",
        fill: true,
        tension: 0.25,
        pointRadius: 3,
      }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, title: { display: true, text: "mi" } } },
    },
  });
}

// ---- Views ----------------------------------------------------------------

// athleteId null = the signed-in athlete (server scopes by token).
async function loadDashboard(athleteId) {
  document.getElementById("rosterView").hidden = true;
  document.getElementById("dashView").hidden = false;
  const err = document.getElementById("dashError");
  err.hidden = true;
  const qs = athleteId != null ? `?athlete_id=${athleteId}` : "";
  try {
    const [summary, sessions, workouts, weeks] = await Promise.all([
      getJSONAuth("/stats/summary" + qs),
      getJSONAuth("/sessions" + qs),
      getJSONAuth("/workouts" + qs),
      getJSONAuth("/stats/weekly" + qs),
    ]);
    renderSummary(summary);
    renderRecent(buildRecent(sessions, workouts));
    renderWeeklyChart(weeks);
  } catch (e) {
    if (e && e.message === "unauthenticated") return;  // 401 already handled
    console.error("Failed to load dashboard:", e);
    // Clear stale (previous-athlete) data so it can't be mistaken for current.
    ["wkDistance", "wkTime", "wkRuns"].forEach(
      (id) => (document.getElementById(id).textContent = "—"));
    ["wkDistanceDelta", "wkTimeDelta", "wkRunsDelta"].forEach(
      (id) => setDelta(id, 0, ""));
    document.querySelector("#recentTable tbody").innerHTML =
      `<tr><td colspan="6" class="muted">—</td></tr>`;
    renderWeeklyChart([]);
    err.textContent = "Couldn't load this athlete's data — please try again.";
    err.hidden = false;
  }
}

// ---- Coach board (Team Week) ---------------------------------------------

let boardWeekStart = null;  // ISO Monday currently shown; null = this week

function fmtSyncAge(iso) {
  if (!iso) return "never";
  const mins = Math.floor((Date.now() - toDate(iso)) / 60000);
  if (mins < 60) return `${Math.max(mins, 1)}m ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)}h ago`;
  if (mins < 14 * 24 * 60) return `${Math.round(mins / 1440)}d ago`;
  return fmtDate(iso);
}

function boardDayCell(day, dateIso, todayIso) {
  const cls = { trained: "rec", none: "none", nosync: "nosync", future: "future" }[day.state];
  const tip = {
    trained: () => `${day.miles} mi · ${day.minutes}m` + (day.runs > 1 ? ` · ${day.runs} workouts` : ""),
    none: () => dateIso === todayIso ? "no activity yet today" : "no activity",
    nosync: () => "no data — hasn't synced",
    future: () => null,
  }[day.state]();
  const today = dateIso === todayIso ? " today-col" : "";
  return `<td class="board-day${today}">` +
    `<span class="mark ${cls}"${tip ? ` data-tip="${tip}"` : ""}></span></td>`;
}

async function showRoster() {
  document.getElementById("dashView").hidden = true;
  document.getElementById("rosterView").hidden = false;
  const qs = boardWeekStart ? `?start=${boardWeekStart}` : "";
  const week = await getJSONAuth("/team/week" + qs);
  boardWeekStart = week.week_start;
  // Nothing exists before the season's first week (server clamps regardless).
  document.getElementById("weekPrev").disabled =
    week.week_start <= week.season_week_start;

  // header: "Jul 6 – 12"
  const first = toDate(week.days[0] + "T12:00:00Z"), last = toDate(week.days[6] + "T12:00:00Z");
  const md = (d) => d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: PT });
  document.getElementById("weekLabel").textContent =
    `${md(first)} – ${last.toLocaleDateString("en-US", { day: "numeric", timeZone: PT })}`;

  // tiles
  const t = week.team;
  const isThisWeek = week.days.includes(week.today);
  document.getElementById("trainedTodayLabel").textContent =
    isThisWeek ? "Trained today" : "Trained · that week";
  document.getElementById("trainedToday").textContent =
    isThisWeek ? `${t.trained_today} / ${t.total}` : "—";
  document.getElementById("trainedTodaySub").textContent = "";
  document.getElementById("teamMiles").textContent = t.week_miles.toFixed(1);
  const dm = t.week_miles - t.prev_week_miles;
  setDelta("teamMilesDelta", dm,
    `${dm >= 0 ? "+" : "−"}${Math.abs(dm).toFixed(1)} mi vs prior week`);
  document.getElementById("staleCount").textContent = t.stale.length;
  document.getElementById("staleNames").textContent =
    t.stale.length ? t.stale.join(" · ") : "everyone reporting";
  document.getElementById("staleCount").classList.toggle("warn-num", t.stale.length > 0);

  // grid
  const dayNames = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  document.querySelector("#boardTable thead").innerHTML = "<tr><th>Athlete</th>" +
    week.days.map((d, i) => {
      const today = d === week.today ? " today-col" : "";
      const label = d === week.today ? " · today" : "";
      return `<th class="board-day${today}">${dayNames[i]}<small>${Number(d.slice(8))}${label}</small></th>`;
    }).join("") +
    `<th class="num">Runs</th><th class="num">Mi</th><th>Synced</th></tr>`;

  const tbody = document.querySelector("#boardTable tbody");
  if (!week.athletes.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="muted">No athletes yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = week.athletes.map((a) => `
    <tr class="clickable" data-id="${a.id}" data-name="${escapeHtml(a.name)}">
      <td class="board-who">${escapeHtml(a.name)}</td>
      ${a.days.map((day, i) => boardDayCell(day, week.days[i], week.today)).join("")}
      <td class="num">${a.week_runs || "–"}</td>
      <td class="num">${a.week_runs ? a.week_miles.toFixed(1) : "–"}</td>
      <td><span class="sync-chip${a.stale ? " stale" : ""}">${fmtSyncAge(a.last_sync)}</span></td>
    </tr>`).join("");
  tbody.querySelectorAll("tr.clickable").forEach((tr) => {
    tr.onclick = () =>
      coachViewAthlete(Number(tr.dataset.id), tr.dataset.name, true)
        .catch(console.error);
  });
}

function shiftBoardWeek(days) {
  const d = toDate(boardWeekStart + "T12:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  boardWeekStart = d.toISOString().slice(0, 10);
  showRoster().catch(console.error);
}
document.getElementById("weekPrev").onclick = () => shiftBoardWeek(-7);
document.getElementById("weekNext").onclick = () => shiftBoardWeek(7);

function renderHeader(me) {
  document.getElementById("who").hidden = false;
  document.getElementById("whoName").textContent = me.name;
  document.getElementById("whoRole").textContent = me.role;
  document.getElementById("signOutBtn").onclick = signOut;
}

// Exposed for auth.js's 401 fallback.
function showSignIn() {
  initAuth().then(({ config }) => renderSignIn(config));
}

// The coach's "which athlete am I viewing" lives in the URL (?athlete_id=N) so
// the browser Back button — and returning from a session/workout detail page —
// restores that athlete's dashboard instead of dumping back to the roster.
let currentMe = null;
let _athleteNames = null;  // id -> name, resolved lazily for the viewing bar

async function athleteName(id) {
  if (!_athleteNames) {
    const list = await getJSONAuth("/athletes");
    _athleteNames = new Map(list.map((a) => [a.id, a.name]));
  }
  return _athleteNames.get(id);
}

async function coachViewAthlete(id, name, push) {
  if (push) history.pushState({ athleteId: id }, "", `?athlete_id=${id}`);
  document.getElementById("viewingBar").hidden = false;
  document.getElementById("viewingWho").textContent =
    "Viewing " + ((name || (await athleteName(id))) ?? "athlete");
  await loadDashboard(id);
}

async function coachShowRoster(push) {
  if (push) history.pushState({}, "", location.pathname);  // drop ?athlete_id
  document.getElementById("viewingBar").hidden = true;
  await showRoster();
}

// Render the view that matches the current URL. Athletes always see their own
// dashboard; coaches see an athlete when ?athlete_id is set, else the roster.
async function route() {
  if (!currentMe) return;
  if (currentMe.role !== "coach") { await loadDashboard(null); return; }
  const id = new URLSearchParams(location.search).get("athlete_id");
  if (id) await coachViewAthlete(Number(id), null, false);
  else await coachShowRoster(false);
}

// Signed-in entry point: used by the boot path below and by auth.js right
// after a sign-in completes (no full page reload — see onSignedIn).
async function enterApp(me) {
  currentMe = me;
  document.getElementById("signin").hidden = true;
  renderHeader(me);
  document.getElementById("appView").hidden = false;
  document.getElementById("backToRoster").onclick = (e) => {
    e.preventDefault();
    coachShowRoster(true).catch(console.error);
  };
  await route();
}

// Back/forward between roster and an athlete (same-document pushState entries).
window.addEventListener("popstate", () => route().catch(console.error));

// Called by auth.js with the athlete returned by the token exchange.
function onSignedIn(athlete) {
  enterApp(athlete).catch((err) => console.error("Failed to enter app:", err));
}

(async () => {
  try {
    const { config, me } = await initAuth();
    if (!me) { renderSignIn(config); return; }
    await enterApp(me);
  } catch (err) {
    console.error("Dashboard failed to load:", err);
  }
})();
