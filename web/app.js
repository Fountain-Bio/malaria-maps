/* Malaria Region Tracker — choropleth + city/country lookup, one unified detail panel. */

const COLORS = { whole_country: "#cb181d", partial: "#fd8d3c", none: "#dfe3e8" };
const CITATION = "FDA 12/2022; endemic = where CDC recommends chemoprophylaxis. " +
  "A Jan-2025 FDA draft would move to selective testing.";

let countryRows = [];   // /malaria/country_current rows (iso3, iso2, display_name, screening_class, ...)
let geoFeatures = [];   // Natural Earth features (for country fly-to)
let cityMarker = null;

const map = new maplibregl.Map({
  container: "map",
  style: { version: 8, sources: {}, glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
           layers: [{ id: "bg", type: "background", paint: { "background-color": "#bcd5ea" } }] },
  center: [12, 18], zoom: 1.3, attributionControl: false,
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-left");

function isoOf(p) {
  const eh = p.ISO_A3_EH;
  return eh && eh !== "-99" ? eh : p.ISO_A3;
}

function fold(s) {
  return (s || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
    .replace(/[^a-z0-9 ]/g, " ").replace(/\s+/g, " ").trim();
}

map.on("load", async () => {
  const [geo, rows] = await Promise.all([
    fetch("world.geojson").then((r) => r.json()),
    fetch("/malaria/country_current.json?_shape=array&_size=max").then((r) => r.json()),
  ]);
  countryRows = rows;
  geoFeatures = geo.features;

  const byIso = {};
  for (const r of rows) if (r.iso3) byIso[r.iso3] = r;
  for (const f of geo.features) {
    const rec = byIso[isoOf(f.properties)];
    f.properties._class = rec ? rec.screening_class : "none";
    f.properties._name = rec ? rec.display_name : (f.properties.ADMIN || f.properties.NAME);
    f.properties._iso = rec ? rec.iso3 : isoOf(f.properties);
  }

  map.addSource("countries", { type: "geojson", data: geo });
  map.addLayer({
    id: "fill", type: "fill", source: "countries",
    paint: {
      "fill-color": ["match", ["get", "_class"],
        "whole_country", COLORS.whole_country, "partial", COLORS.partial, COLORS.none],
      "fill-opacity": 0.85,
    },
  });
  map.addLayer({ id: "line", type: "line", source: "countries",
    paint: { "line-color": "#8794a0", "line-width": 0.4 } });

  const counts = rows.reduce((a, r) => { a[r.screening_class] = (a[r.screening_class] || 0) + 1; return a; }, {});
  document.getElementById("counts").textContent =
    `${(counts.whole_country || 0) + (counts.partial || 0)} endemic ` +
    `(${counts.whole_country || 0} whole, ${counts.partial || 0} partial)`;

  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
  map.on("mousemove", "fill", (e) => {
    map.getCanvas().style.cursor = "pointer";
    const p = e.features[0].properties;
    popup.setLngLat(e.lngLat).setHTML(`<strong>${p._name}</strong><br>${labelFor(p._class)}`).addTo(map);
  });
  map.on("mouseleave", "fill", () => { map.getCanvas().style.cursor = ""; popup.remove(); });
  map.on("click", "fill", (e) => openCountry(e.features[0].properties));
});

function labelFor(c) {
  return c === "whole_country" ? "Endemic — whole country"
    : c === "partial" ? "Endemic — specific areas" : "Not endemic for deferral";
}

// ----------------------------------------------------------------- unified panel
function residenceBadge(defer) {
  return defer ? ["v-defer", "Residence: defer"] : ["v-ok", "Residence: eligible"];
}
function travelBadge(t) {
  if (t === "yes") return ["v-defer", "Travel: defer"];
  if (t === "areas") return ["v-review", "Travel: defer in listed areas"];
  if (t === "uncertain") return ["v-review", "Travel: review needed"];
  return ["v-ok", "Travel: not deferred"];
}
function field(k, html) {
  return `<div class="field"><div class="k">${k}</div><div class="v">${html}</div></div>`;
}

// PR is a per-verdict modifier, not a standalone rule. A travel deferral (3-month, §IV.B.1)
// is PR-eligible for platelets/plasma; residence (>5 yr) and prior malaria are the 3-year
// categories with no PR alternative. Both lines can apply at once (e.g. an endemic country).
function prLines(d) {
  const out = [];
  if (d.travel === "yes" || d.travel === "areas") {
    out.push(["yes", "<b>Travel:</b> pathogen-reduced platelets/plasma may be collected without " +
      "the 3-month deferral (FDA §IV.B.1). No PR device exists for whole blood / red cells."]);
  }
  if (d.residence) {
    out.push(["no", "<b>Residence (&gt;5 yr):</b> no pathogen-reduction alternative (FDA §IV.B.2)."]);
  }
  return out;
}

function renderPanel(d) {
  const body = document.getElementById("panel-body");
  document.getElementById("panel").classList.add("open");
  const [rcls, rlabel] = residenceBadge(d.residence);
  const [tcls, tlabel] = travelBadge(d.travel);

  let h = `<h2>${d.title}</h2>`;
  h += `<div class="verdict-row"><span class="vbadge ${rcls}">${rlabel}</span>` +
    `<span class="vbadge ${tcls}">${tlabel}</span></div>`;
  const prs = prLines(d);
  if (prs.length) h += `<div class="pr-row">` + prs.map(([k, t]) =>
    `<div class="pr-line pr-line-${k}"><span class="dot"></span><span>${t}</span></div>`).join("") + `</div>`;
  const meta = [];
  if (d.confidence) meta.push(`confidence: ${d.confidence}`);
  if (d.elevation != null) meta.push(`${d.elevation} m`);
  if (meta.length) h += `<div class="conf">${meta.join(" · ")}</div>`;
  if (d.why) h += field(d.scope === "city" ? "Why" : "Deferral implication", d.why);
  if (d.season) h += field("Seasonal", d.season);
  if (d.area_html) h += field("Risk areas (CDC, verbatim)", d.area_html);
  if (d.prophylaxis_html) h += field("Recommended chemoprophylaxis", d.prophylaxis_html);
  if (d.species_html) h += field("Species", d.species_html);
  if (d.chloroquine != null) h += field("Chloroquine resistance", d.chloroquine ? "Yes" : "None reported");
  h += field("Basis", CITATION);
  if (d.alternates && d.alternates.length) {
    h += `<div class="field"><div class="k">Did you mean</div><div class="chips">` +
      d.alternates.map((a) =>
        `<span class="chip" data-gid="${a.geoname_id}">${a.name}, ${a.admin1 || a.country_name}</span>`
      ).join("") + `</div></div>`;
  }
  const links = [];
  if (d.iso2) links.push(`<a href="/malaria/v_malaria_current?iso2=${d.iso2}">Full record</a>`);
  if (d.display_name) links.push(`<a href="/malaria/country_history?country=${encodeURIComponent(d.display_name)}">History</a>`);
  if (links.length) h += `<div class="links">${links.join("")}</div>`;
  if (d.updated) h += `<div class="updated">CDC updated ${d.updated}</div>`;
  if (d.scope === "city") h += `<div class="geo-credit">Geocoding: GeoNames (CC BY)</div>`;

  body.innerHTML = h;
  body.querySelectorAll(".chip").forEach((c) =>
    c.addEventListener("click", () => locateCity({ geonameId: c.dataset.gid })));
}

function implicationText(cls) {
  if (cls === "whole_country") {
    return "<ul><li><b>Residence</b> &gt;5 yr: defer (whole country).</li>" +
      "<li><b>Travel</b> &gt;24 h anywhere in country: defer (3-month window).</li></ul>";
  }
  if (cls === "partial") {
    return "<ul><li><b>Residence</b> &gt;5 yr: defer (whole country — it contains a malaria-endemic area).</li>" +
      "<li><b>Travel</b>: defer only for the chemoprophylaxis-recommended areas listed below (3-month window).</li></ul>";
  }
  return "<ul><li>Not endemic for deferral: CDC recommends no chemoprophylaxis here, " +
    "so neither residence nor travel triggers malaria deferral.</li></ul>";
}

// ----------------------------------------------------------------- country panel
async function openCountry(p) {
  const iso = p._iso;
  document.getElementById("panel").classList.add("open");
  document.getElementById("panel-body").innerHTML = `<div class="field"><div class="v">Loading…</div></div>`;
  let rec = null;
  if (iso) {
    const rows = await fetch(`/malaria/v_malaria_current.json?_shape=array&iso3=${encodeURIComponent(iso)}`)
      .then((r) => r.json()).catch(() => []);
    rec = rows && rows[0];
  }
  if (!rec) {
    renderPanel({ scope: "country", title: p._name || "Country", residence: false, travel: "no",
                  why: "No CDC malaria record for this area (not a CDC travel destination)." });
    return;
  }
  renderPanel({
    scope: "country", title: rec.display_name, residence: !!rec.is_endemic,
    travel: rec.screening_class === "whole_country" ? "yes"
          : rec.screening_class === "partial" ? "areas" : "no",
    why: implicationText(rec.screening_class),
    area_html: rec.area_of_risk_html, prophylaxis_html: rec.recommended_prophylaxis_html,
    species_html: rec.species_html, chloroquine: rec.chloroquine_resistant,
    updated: rec.cdc_updated_date, iso2: rec.iso2, display_name: rec.display_name,
  });
}

// ----------------------------------------------------------------- search (country or city)
document.getElementById("search").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = document.getElementById("city-input").value.trim();
  if (!q) return;
  const country = findCountry(q);
  if (country) {
    if (cityMarker) { cityMarker.remove(); cityMarker = null; }
    openCountry({ _iso: country.iso3, _name: country.display_name, _class: country.screening_class });
    flyToIso3(country.iso3);
  } else {
    locateCity({ q });
  }
});

function findCountry(q) {
  const f = fold(q);
  if (f.length < 3) return null;
  const exact = countryRows.find((r) => fold(r.display_name) === f);
  if (exact) return exact;
  const pre = countryRows.filter((r) => fold(r.display_name).startsWith(f));
  if (pre.length === 1) return pre[0];
  const con = countryRows.filter((r) => fold(r.display_name).includes(f));
  if (con.length === 1) return con[0];
  return null;
}

function countryCentroid(iso3) {
  const f = geoFeatures.find((x) => isoOf(x.properties) === iso3);
  if (!f) return null;
  let minx = 180, miny = 90, maxx = -180, maxy = -90;
  const walk = (c) => {
    if (typeof c[0] === "number") {
      minx = Math.min(minx, c[0]); maxx = Math.max(maxx, c[0]);
      miny = Math.min(miny, c[1]); maxy = Math.max(maxy, c[1]);
    } else { c.forEach(walk); }
  };
  walk(f.geometry.coordinates);
  return [(minx + maxx) / 2, (miny + maxy) / 2];
}
function flyToIso3(iso3) {
  const c = countryCentroid(iso3);
  if (c) map.flyTo({ center: c, zoom: 3.2, speed: 1.2 });
}

async function locateCity(params) {
  document.getElementById("panel").classList.add("open");
  document.getElementById("panel-body").innerHTML = `<div class="field"><div class="v">Searching…</div></div>`;
  const qs = params.geonameId
    ? `geonameId=${encodeURIComponent(params.geonameId)}`
    : `q=${encodeURIComponent(params.q)}`;
  let v;
  try {
    v = await fetch(`/-/locate?${qs}`).then((r) => r.json());
  } catch (_e) {
    document.getElementById("panel-body").innerHTML = `<div class="field"><div class="v">Lookup failed.</div></div>`;
    return;
  }
  if (v.error && !v.resolved) {
    document.getElementById("panel-body").innerHTML = `<div class="field"><div class="v">${v.error}</div></div>`;
    return;
  }
  renderVerdict(v);
}

function renderVerdict(v) {
  const r = v.resolved || {};
  const where = [r.name, r.admin1, r.country_name].filter(Boolean).join(", ");
  renderPanel({
    scope: "city", title: where || "Location",
    residence: v.residence_deferral, travel: v.travel_deferral,
    confidence: v.confidence, elevation: r.elevation_m,
    why: v.travel_reason, season: v.season_note,
    area_html: v.verbatim_area_html, prophylaxis_html: v.prophylaxis_html,
    species_html: v.species_html, chloroquine: v.chloroquine_resistant,
    updated: v.cdc_updated_date, iso2: r.country_iso2, display_name: v.display_name,
    alternates: v.alternates,
  });
  if (r.lat != null && r.lng != null) {
    if (cityMarker) cityMarker.remove();
    cityMarker = new maplibregl.Marker({ color: "#1b6ec2" }).setLngLat([r.lng, r.lat]).addTo(map);
    map.flyTo({ center: [r.lng, r.lat], zoom: 5, speed: 1.2 });
  }
}

// ----------------------------------------------------------------- screening rules drawer
const RULE_TITLES = {
  endemic_definition: "Endemic definition",
  residence_over_5yr: "Residence (>5 years)",
  residence_reeligibility: "Re-eligibility after residence",
  travel_to_endemic_area: "Travel to an endemic area",
  history_of_malaria: "History of malaria",
  pathogen_reduction_alternative: "Pathogen reduction (alternative to deferral)",
  testing_transition: "Testing transition",
};
const PR_PILL = {
  authorized: ["pr-yes", "PR alternative"],
  not_authorized: ["pr-no", "No PR alternative"],
};
function humanize(code) {
  const s = (code || "").replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}
function deferLabel(win) {
  if (!win) return "";
  if (/^none/i.test(win)) return "No deferral";
  const m = win.match(/(\d+)\s*(month|year)/i);
  return m ? `Defer ${m[1]} ${m[2][0] === "m" || m[2][0] === "M" ? "mo" : "yr"}` : win;
}
let rulesLoaded = false;

async function openRules() {
  document.getElementById("rules").classList.add("open");
  if (rulesLoaded) return;
  const body = document.getElementById("rules-body");
  const rules = await fetch("/malaria/deferral_rule.json?_shape=array")
    .then((r) => r.json()).catch(() => []);
  if (!rules.length) { body.textContent = "Could not load rules."; return; }

  const byCode = Object.fromEntries(rules.map((r) => [r.code, r]));
  const MATRIX_CODES = ["travel_to_endemic_area", "residence_over_5yr", "history_of_malaria"];
  const META_CODES = ["endemic_definition", "residence_reeligibility", "testing_transition"];

  // Deferral categories as a scannable matrix: exposure × deferral window × PR alternative.
  const mxRows = MATRIX_CODES.map((code) => {
    const r = byCode[code];
    if (!r) return "";
    const pr = PR_PILL[r.pathogen_reduction];
    const prCell = pr
      ? `<span class="pill ${pr[0]}"><span class="dot"></span>${pr[1]}</span>`
      : `<span class="mx-na">—</span>`;
    return `<div class="mx-row" role="row">` +
      `<span class="mx-cat" role="cell">${RULE_TITLES[code] || humanize(code)}</span>` +
      `<span class="mx-win" role="cell">${deferLabel(r.deferral_window) || "—"}</span>` +
      `<span class="mx-pr" role="cell">${prCell}</span></div>`;
  }).join("");
  const matrix = `<div class="matrix" role="table" aria-label="Deferral categories">` +
    `<div class="mx-head" role="row"><span role="columnheader">Exposure</span>` +
    `<span role="columnheader">Deferral</span><span role="columnheader">PR alternative</span></div>` +
    mxRows + `</div>`;

  // Short PR explainer; the canonical full regulatory text is disclosed on demand.
  const prRule = byCode["pathogen_reduction_alternative"];
  const prExplain = prRule ? `<div class="pr-explain">` +
    `<p class="pr-sum">Pathogen reduction substitutes for the <b>travel</b> deferral only — ` +
    `platelets/plasma, never whole blood or red cells.</p>` +
    `<details class="pr-details"><summary>Full regulatory text</summary>` +
    `<p class="rule-d">${prRule.description}</p>` +
    (prRule.threshold ? `<p class="rule-m"><b>Threshold:</b> ${prRule.threshold}</p>` : "") +
    (prRule.citation ? `<p class="rule-c">${prRule.citation}</p>` : "") +
    `</details></div>` : "";

  const metaCards = META_CODES.map((code) => {
    const r = byCode[code];
    if (!r) return "";
    return `<article class="def"><h4 class="def-h">${RULE_TITLES[code] || humanize(code)}</h4>` +
      `<p class="def-d">${r.description}</p>` +
      (r.citation ? `<p class="rule-c">${r.citation}</p>` : "") + `</article>`;
  }).join("");

  body.innerHTML =
    `<h3 class="sec-h">Deferral categories</h3>${matrix}${prExplain}` +
    `<h3 class="sec-h">Definitions &amp; status</h3>${metaCards}`;
  rulesLoaded = true;
}

document.getElementById("rules-btn").addEventListener("click", openRules);
