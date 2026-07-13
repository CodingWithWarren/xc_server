// Detected-session detail page: a pure API client. Reads ?id= and fetches
// GET /sessions/{id}, which includes the streams sliced to the session window.

const sessionId = new URLSearchParams(location.search).get("id");

const PT = "America/Los_Angeles";  // show all times in Pacific (PST/PDT)

// DB datetime columns come back without a timezone; treat them as UTC so they
// aren't misread as browser-local. Sample times already carry Z/offset.
function toDate(iso) {
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
}

const fmtMi = (m) => (m == null ? "—" : (m / 1609.344).toFixed(2) + " mi");
const fmtBpm = (b) => (b == null ? "—" : b + " bpm");

function fmtDateTime(iso) {
  return toDate(iso).toLocaleString("en-US", {
    year: "numeric", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
    timeZone: PT, timeZoneName: "short",
  });
}

function fmtTime(iso) {
  return toDate(iso).toLocaleTimeString("en-US", { timeZone: PT });
}
function fmtDuration(sec) {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

const SAMPLE_LABELS = {
  heart_rate_samples: "Heart rate",
  step_samples: "Steps",
  distance_samples: "Distance",
};

const stat = (label, value) =>
  `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div></div>`;

function renderHeader(s) {
  const recorded = !!s.matched_workout_uuid;
  const activity = escapeHtml(s.matched_activity_type || s.inferred_activity || "Session");
  const badge = recorded
    ? `<span class="badge badge-recorded">recorded</span>`
    : `<span class="badge badge-detected">detected</span>`;
  document.getElementById("detail").innerHTML = `
    <h2 class="detail-title">${activity} ${badge}</h2>
    <p class="detail-sub">${fmtDateTime(s.start_time)} · detection ${escapeHtml(s.detection_version)}</p>
    <div class="stats">
      ${stat("Duration", fmtDuration(s.duration_seconds))}
      ${stat("Distance", fmtMi(s.total_distance_meters))}
      ${stat("Avg HR", fmtBpm(s.avg_hr))}
      ${stat("Peak HR", fmtBpm(s.peak_hr))}
      ${stat("Steps", s.total_steps == null ? "—" : s.total_steps.toLocaleString())}
      ${stat("Cadence", s.avg_steps_per_min == null ? "—" : s.avg_steps_per_min + " spm")}
      ${stat("HR coverage", s.hr_coverage_pct == null ? "—" : s.hr_coverage_pct + "%")}
      ${stat("HR sources", s.hr_source_count == null ? "—" : s.hr_source_count)}
    </div>`;
}

function renderHrChart(raw) {
  document.getElementById("hrCard").hidden = false;
  const hr = raw.heart_rate_samples || [];
  if (!hr.length) {
    document.getElementById("hrChart").hidden = true;
    document.getElementById("hrEmpty").hidden = false;
    return;
  }
  new Chart(document.getElementById("hrChart"), {
    type: "line",
    data: {
      labels: hr.map((s) => fmtTime(s.time)),
      datasets: [{
        label: "bpm",
        data: hr.map((s) => s.value),
        borderColor: "#e0245e",
        backgroundColor: "rgba(224, 36, 94, 0.08)",
        fill: true,
        pointRadius: 0,
        borderWidth: 1.5,
        tension: 0.2,
      }],
    },
    options: {
      responsive: true,
      animation: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { autoSkip: true, maxTicksLimit: 8 } },
        y: { title: { display: true, text: "bpm" } },
      },
    },
  });
}

// Moving-average window for smoothing the *displayed* GPS path. Mirrors the
// phone app's smoothPath (lib/main.dart): raw points stay intact in the DB/API;
// only the drawn polyline is smoothed. Higher = smoother but rounds corners; 1
// disables. Keep in sync with _pathSmoothingWindow on the phone.
const PATH_SMOOTHING_WINDOW = 7;

// Centered moving average over lat/lng; endpoints preserved (the window shrinks
// at the edges). A direct port of smoothPath() in the phone app, so the
// dashboard draws the same path the athlete saw while recording.
function smoothPath(latlngs, window = PATH_SMOOTHING_WINDOW) {
  if (latlngs.length <= 2 || window < 2) return latlngs;
  const half = Math.floor(window / 2);
  const out = [];
  for (let i = 0; i < latlngs.length; i++) {
    let lat = 0, lng = 0, n = 0;
    for (let j = i - half; j <= i + half; j++) {
      if (j < 0 || j >= latlngs.length) continue;
      lat += latlngs[j][0];
      lng += latlngs[j][1];
      n++;
    }
    out.push([lat / n, lng / n]);
  }
  return out;
}

// Draw the DIY GPS route (if one overlaps this session) as a polyline with
// start/end markers. Uses Leaflet + OpenStreetMap tiles (no API key). The drawn
// path is smoothed (see smoothPath) to match the phone; raw points are unchanged.
function renderRouteMap(route) {
  if (!route || !route.points || route.points.length === 0) return;

  const latlngs = smoothPath(route.points.map((p) => [p.lat, p.lng]));
  document.getElementById("mapCard").hidden = false;

  const map = L.map("routeMap");
  // Attribution to bottom-left so it doesn't sit under the bottom-right button.
  map.attributionControl.setPosition("bottomleft");
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  L.polyline(latlngs, { color: "#2f6fed", weight: 4 }).addTo(map);

  // Circle markers (no image assets, so no Leaflet default-icon path issues).
  const ends = [[latlngs[0], "#177245", "Start"],
                [latlngs[latlngs.length - 1], "#b3261e", "End"]];
  for (const [pos, color, label] of ends) {
    L.circleMarker(pos, {
      radius: 6, color: "#fff", weight: 2, fillColor: color, fillOpacity: 1,
    }).addTo(map).bindTooltip(label);
  }

  // The initial "show the whole route" view; the Fit-route button replays it.
  const fitWholeRoute = () => {
    if (latlngs.length === 1) map.setView(latlngs[0], 16);
    else map.fitBounds(latlngs, { padding: [24, 24] });
  };
  fitWholeRoute();

  const fitBtn = document.getElementById("fitRouteBtn");
  fitBtn.hidden = false;
  fitBtn.onclick = fitWholeRoute;

  document.getElementById("routeMeta").textContent =
    `${fmtMi(route.distance_meters)} · ${route.point_count.toLocaleString()} GPS points`;
}

function renderSampleChips(raw) {
  const chips = Object.entries(SAMPLE_LABELS)
    .map(([key, label]) => {
      const n = Array.isArray(raw[key]) ? raw[key].length : 0;
      return n > 0 ? `<span class="chip">${label}: ${n.toLocaleString()}</span>` : null;
    })
    .filter(Boolean);
  if (!chips.length) return;
  document.getElementById("samplesCard").hidden = false;
  document.getElementById("chips").innerHTML = chips.join("");
}

async function load() {
  const detail = document.getElementById("detail");
  if (!sessionId) {
    detail.innerHTML = `<p class="muted">No session id in the URL.</p>`;
    return;
  }
  const res = await authFetch(`/sessions/${encodeURIComponent(sessionId)}`);
  if (res.status === 404) {
    detail.innerHTML = `<p class="muted">Session not found.</p>`;
    return;
  }
  if (res.status === 403) {
    detail.innerHTML = `<p class="muted">You don't have access to this session.</p>`;
    return;
  }
  if (!res.ok) throw new Error(`/sessions/${sessionId} -> ${res.status}`);

  const s = await res.json();
  const raw = s.raw_payload || {};
  renderHeader(s);
  renderHrChart(raw);
  renderSampleChips(raw);

  // Route is a separate fetch (reconciled server-side by time overlap). A
  // failure here shouldn't blank the rest of the page.
  try {
    const routeRes = await authFetch(`/sessions/${encodeURIComponent(sessionId)}/route`);
    if (routeRes.ok) renderRouteMap(await routeRes.json());
  } catch (err) {
    console.error("Failed to load route:", err);
  }
}

load().catch((err) => {
  console.error("Failed to load session:", err);
  document.getElementById("detail").innerHTML =
    `<p class="muted">Failed to load — see console.</p>`;
});
