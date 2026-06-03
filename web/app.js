/* Malaria Region Tracker — choropleth. Joins Natural Earth polygons to the
   /malaria/country_current feed; click a country for full detail from v_malaria_current. */

const COLORS = { whole_country: "#cb181d", partial: "#fd8d3c", none: "#dfe3e8" };

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

map.on("load", async () => {
  const [geo, rows] = await Promise.all([
    fetch("world.geojson").then((r) => r.json()),
    fetch("/malaria/country_current.json?_shape=array&_size=max").then((r) => r.json()),
  ]);

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

async function openCountry(p) {
  const panel = document.getElementById("panel");
  const body = document.getElementById("panel-body");
  const iso = p._iso;
  body.innerHTML = `<h2>${p._name}</h2> <span class="badge b-${badge(p._class)}">${labelFor(p._class)}</span>`;
  body.innerHTML += implicationHtml(p._class);
  panel.classList.add("open");

  let rec = null;
  if (iso) {
    const rows = await fetch(`/malaria/v_malaria_current.json?_shape=array&iso3=${encodeURIComponent(iso)}`)
      .then((r) => r.json()).catch(() => []);
    rec = rows && rows[0];
  }
  if (!rec) {
    body.innerHTML += `<div class="field"><div class="v">No CDC malaria record for this area
      (not a CDC travel destination, or no transmission).</div></div>`;
    return;
  }

  body.innerHTML += [
    field("Risk areas (CDC, verbatim)", rec.area_of_risk_html || "—"),
    field("Recommended chemoprophylaxis", rec.recommended_prophylaxis_html || "—"),
    field("Species", rec.species_html || "—"),
    field("Chloroquine resistance",
      rec.chloroquine_resistant === 1 ? "Yes" : rec.chloroquine_resistant === 0 ? "None reported" : "—"),
    `<div class="links">
       <a href="/malaria/v_malaria_current?iso3=${encodeURIComponent(iso)}">Full record</a>
       <a href="/malaria/country_history?country=${encodeURIComponent(rec.display_name)}">History</a>
     </div>`,
    rec.cdc_updated_date ? `<div class="updated">CDC updated ${rec.cdc_updated_date}</div>` : "",
  ].join("");
}

function field(k, html) {
  return `<div class="field"><div class="k">${k}</div><div class="v">${html}</div></div>`;
}
function badge(c) { return c === "whole_country" ? "whole" : c === "partial" ? "partial" : "none"; }

// --- FDA deferral implication, derived from screening_class ---
function implicationHtml(cls) {
  let lines;
  if (cls === "whole_country") {
    lines = [
      "<b>Residence</b> &gt;5 yr in this country: defer &mdash; whole country.",
      "<b>Travel</b> &gt;24 h to anywhere in this country: defer (3-month window).",
    ];
  } else if (cls === "partial") {
    lines = [
      "<b>Residence</b> &gt;5 yr in this country: defer &mdash; whole country (it contains a malaria-endemic area).",
      "<b>Travel</b> &gt;24 h to the chemoprophylaxis-recommended areas listed below: defer (3-month window).",
    ];
  } else {
    lines = [
      "Not endemic for deferral: CDC recommends no chemoprophylaxis here, so malaria residence/travel deferral does not apply under current criteria.",
    ];
  }
  return `<div class="field implic"><div class="k">Deferral implication (FDA 12/2022)</div>
    <div class="v"><ul>${lines.map((l) => `<li>${l}</li>`).join("")}</ul>
      <div class="implic-note">Pending FDA Jan-2025 draft would move to selective donor testing.</div>
    </div></div>`;
}

// --- Global "Screening rules" drawer ---
const RULE_TITLES = {
  endemic_definition: "Endemic definition",
  residence_over_5yr: "Residence (>5 years)",
  residence_reeligibility: "Re-eligibility after residence",
  travel_to_endemic_area: "Travel to an endemic area",
  history_of_malaria: "History of malaria",
  testing_transition: "Testing transition",
};
let rulesLoaded = false;

async function openRules() {
  const drawer = document.getElementById("rules");
  drawer.classList.add("open");
  if (rulesLoaded) return;
  const body = document.getElementById("rules-body");
  const rules = await fetch("/malaria/deferral_rule.json?_shape=array")
    .then((r) => r.json()).catch(() => []);
  if (!rules.length) { body.textContent = "Could not load rules."; return; }
  body.innerHTML = rules.map((r) => `
    <div class="rule">
      <div class="rule-h">${RULE_TITLES[r.code] || r.code}</div>
      <div class="rule-d">${r.description}</div>
      ${r.threshold ? `<div class="rule-m"><b>Threshold:</b> ${r.threshold}</div>` : ""}
      ${r.deferral_window ? `<div class="rule-m"><b>Window:</b> ${r.deferral_window}</div>` : ""}
      ${r.citation ? `<div class="rule-c">${r.citation}</div>` : ""}
    </div>`).join("");
  rulesLoaded = true;
}

document.getElementById("rules-btn").addEventListener("click", openRules);
