/* Multi-hazard DSS viewer. Works against the Flask API, or offline from window.STANDALONE data. */
(function () {
  'use strict';

  var SA = window.STANDALONE || null;
  var COLORS = ['#2e9e4f', '#a6d24b', '#f2c230', '#ef7b22', '#c0252d'];
  var LABELS = ['Very Low', 'Low', 'Moderate', 'High', 'Very High'];

  var state = { runId: null, summary: null, overlays: {}, layers: {}, settlements: [], originLL: null,
                originUid: null, pick: false, routeGroup: null, originMarker: null, layerCtl: null, routeTimer: null };
  var map, canvas;

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'text') n.textContent = attrs[k];
      else if (k === 'class') n.className = attrs[k];
      else n.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (c) { n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
  function fmt(v, d) { return (v === null || v === undefined || isNaN(v)) ? '-' : Number(v).toFixed(d === undefined ? 1 : d); }
  function setStatus(id, msg, isErr) { var n = $(id); n.textContent = msg || ''; n.classList.toggle('err', !!isErr); }

  /* ------------------------------------------------------------------ map */
  function initMap() {
    map = L.map('map', { preferCanvas: true, zoomControl: true, zoomSnap: 0.25 }).setView([20, 78], 5);
    canvas = L.canvas({ padding: 0.5 });
    L.control.scale({ imperial: false }).addTo(map);
    state.base = {
      'OpenStreetMap': L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }),
      'Satellite (Esri)': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', { maxZoom: 19, attribution: 'Tiles &copy; Esri' }),
      'No basemap': L.layerGroup()
    };
    state.base['OpenStreetMap'].addTo(map);
    state.layerCtl = L.control.layers(state.base, {}, { collapsed: true }).addTo(map);
    map.on('click', function (e) {
      if (!state.pick) return;
      setPick(false);
      setOrigin(e.latlng, null);
      $('origin-select').value = '';
    });
    addLegend();
  }

  function addLegend() {
    var ctl = L.control({ position: 'bottomright' });
    ctl.onAdd = function () {
      var d = el('div', { class: 'legend' });
      d.appendChild(el('b', { text: 'Risk class' }));
      LABELS.forEach(function (l, i) {
        var row = el('div');
        var sw = el('i'); sw.style.background = COLORS[i];
        row.appendChild(sw); row.appendChild(document.createTextNode(l)); d.appendChild(row);
      });
      var r1 = el('div'); r1.appendChild(el('span', { class: 'ln' })); r1.appendChild(document.createTextNode('Least-risk route')); d.appendChild(r1);
      var r2 = el('div'); r2.appendChild(el('span', { class: 'ln dash' })); r2.appendChild(document.createTextNode('Shortest route')); d.appendChild(r2);
      return d;
    };
    ctl.addTo(map);
  }

  /* ------------------------------------------------------------------ data access */
  function getJSON(name) {
    if (SA) {
      if (name === 'summary') return Promise.resolve(SA.summary);
      if (name === 'overlays') return Promise.resolve(SA.overlays);
      return Promise.resolve(SA.geojson[name]);
    }
    var path = name === 'summary' ? 'web/summary.json' : name === 'overlays' ? 'web/overlays.json' : 'vectors/' + name + '.geojson';
    return fetch('/runs/' + encodeURIComponent(state.runId) + '/' + path).then(function (r) {
      if (!r.ok) throw new Error('Could not load ' + path + ' (' + r.status + ')');
      return r.json();
    });
  }
  function overlayUrl(key, o) { return SA ? o.url : '/runs/' + encodeURIComponent(state.runId) + '/web/' + o.file; }

  /* ------------------------------------------------------------------ run loading */
  function loadRun(runId) {
    state.runId = runId;
    setStatus('run-status', 'Loading results...');
    var names = ['summary', 'overlays', 'high_risk_zones', 'settlements_risk', 'hospitals', 'shelters', 'roads_risk'];
    return Promise.all(names.map(getJSON)).then(function (r) {
      var d = { summary: r[0], overlays: r[1], zones: r[2], sett: r[3], hosp: r[4], shel: r[5], roads: r[6] };
      state.summary = d.summary;
      buildLayers(d);
      fillSummary(d.summary);
      fillRouting(d.sett);
      $('sec-results').hidden = false;
      $('sec-route').hidden = false;
      setStatus('run-status', 'Loaded ' + (runId || 'results') + '.');
    }).catch(function (e) { setStatus('run-status', e.message, true); });
  }

  function removeLayers() {
    Object.keys(state.layers).forEach(function (k) {
      map.removeLayer(state.layers[k]);
      state.layerCtl.removeLayer(state.layers[k]);
    });
    state.layers = {};
    clearRoute();
  }

  function popup(title, rows) {
    var d = el('div');
    d.appendChild(el('b', { text: title }));
    rows.forEach(function (r) { if (r[1] !== null && r[1] !== undefined && r[1] !== '') d.appendChild(el('div', { text: r[0] + ': ' + r[1] })); });
    return d;
  }

  function buildLayers(d) {
    removeLayers();
    var L_ = state.layers, add = function (key, label, layer, on) {
      L_[key] = layer; state.layerCtl.addOverlay(layer, label); if (on) layer.addTo(map);
    };
    var bounds = null;
    var order = ['risk', 'risk_index', 'flood', 'landslide', 'slope'];
    order.forEach(function (k) {
      var o = d.overlays[k]; if (!o) return;
      var layer = L.imageOverlay(overlayUrl(k, o), o.bounds, { opacity: Number($('opacity').value), interactive: false });
      layer._isHazard = true;
      add('ov_' + k, o.label, layer, k === 'risk');
      var b = L.latLngBounds(o.bounds); bounds = bounds ? bounds.extend(b) : b;
    });
    add('zones', 'High-risk zones', L.geoJSON(d.zones, {
      style: { color: '#111', weight: 1.4, fillColor: '#c0252d', fillOpacity: 0.06 },
      onEachFeature: function (f, l) {
        var p = f.properties;
        l.bindPopup(popup('High-risk zone ' + p.zone_id + ' (rank ' + p.rank + ')', [
          ['Area', fmt(p.area_km2, 2) + ' km2'], ['Mean risk', fmt(p.risk_mean, 3)], ['Main driver', p.driver],
          ['Settlements inside', p.n_settlements], ['Population inside', p.population ? Math.round(p.population) : '']]));
      }
    }), true);
    add('roads', 'Roads (coloured by risk)', L.geoJSON(d.roads, {
      renderer: canvas,
      style: function (f) { var c = f.properties.risk_class || 1; return { color: COLORS[c - 1], weight: c >= 4 ? 3 : 2, opacity: 0.9 }; },
      onEachFeature: function (f, l) {
        var p = f.properties;
        l.bindPopup(popup('Road segment', [['Length', fmt(p.length_m / 1000, 2) + ' km'], ['Mean risk', fmt(p.risk_mean, 3)],
          ['Peak risk', fmt(p.risk_max, 3)], ['Class', LABELS[(p.risk_class || 1) - 1]], ['Type', p.highway]]));
      }
    }), true);

    var pops = d.sett.features.map(function (f) { return f.properties.population || 0; });
    var maxPop = Math.max.apply(null, pops.concat([1]));
    state.settlements = [];
    var settLayer = L.geoJSON(d.sett, {
      pointToLayer: function (f, ll) {
        var p = f.properties, c = p.risk_class || 1;
        var r = 6 + 6 * Math.sqrt((p.population || 0) / maxPop);
        return L.circleMarker(ll, { radius: r, color: '#111', weight: 1, fillColor: COLORS[c - 1], fillOpacity: 0.95 });
      },
      onEachFeature: function (f, l) {
        var p = f.properties;
        var ll = l.getLatLng ? l.getLatLng() : l.getBounds().getCenter();
        state.settlements.push({ uid: p.uid, name: p.name, ll: ll, risk: p.risk_mean, label: p.risk_label });
        l.bindPopup(function () {
          var box = popup(p.name, [['Risk class', p.risk_label], ['Mean risk', fmt(p.risk_mean, 3)], ['Main driver', p.driver],
            ['Flood / landslide / slope index', fmt(p.flood_idx, 2) + ' / ' + fmt(p.landslide_idx, 2) + ' / ' + fmt(p.slope_idx, 2)],
            ['Population', p.population ? Math.round(p.population) : '']]);
          var b = el('button', { type: 'button', text: 'Route from here' });
          b.addEventListener('click', function () { selectOrigin(p.uid); map.closePopup(); runRoute(); });
          box.appendChild(b);
          return box;
        });
      }
    });
    add('settlements', 'Settlements', settLayer, true);

    function facLayer(fc, cls, letter) {
      return L.geoJSON(fc, {
        pointToLayer: function (f, ll) {
          var bad = f.properties.suitable === false || f.properties.suitable === 'False';
          return L.marker(ll, { icon: L.divIcon({ className: '', html: '<div class="mk ' + cls + (bad ? ' bad' : '') + '">' + letter + '</div>', iconSize: [24, 24], iconAnchor: [12, 12] }) });
        },
        onEachFeature: function (f, l) {
          var p = f.properties;
          var bad = p.suitable === false || p.suitable === 'False';
          l.bindPopup(popup(p.name, [['Risk class', p.risk_label], ['Mean risk', fmt(p.risk_mean, 3)],
            ['Status', bad ? 'Not suitable: ' + (p.note || '') : 'Suitable'], ['Capacity', p.capacity]]));
        }
      });
    }
    add('hospitals', 'Hospitals', facLayer(d.hosp, 'hosp', 'H'), true);
    add('shelters', 'Shelters', facLayer(d.shel, 'shel', 'S'), true);

    state.routeGroup = L.layerGroup().addTo(map);
    if (bounds) map.fitBounds(bounds, { padding: [20, 20] });
  }

  /* ------------------------------------------------------------------ summary panel */
  function fillSummary(s) {
    var tiles = $('tiles'); clear(tiles);
    var st = s.settlements;
    var items = [
      [s.n_zones, 'high-risk zones'],
      [fmt(s.high_risk_area_km2, 1) + ' km²', 'high-risk area'],
      [st.high_risk + ' / ' + st.total, 'high-risk settlements'],
      [st.population_in_high_risk !== null && st.population_in_high_risk !== undefined ? Math.round(st.population_in_high_risk).toLocaleString() : '-', 'people in high-risk settlements']
    ];
    items.forEach(function (it) { tiles.appendChild(el('div', { class: 'tile' }, [el('b', { text: String(it[0]) }), el('span', { text: it[1] })])); });

    var bar = $('classbar'); clear(bar);
    var lg = $('classlegend'); clear(lg);
    s.class_stats.forEach(function (c, i) {
      var seg = el('div', { title: c.label + ': ' + c.percent + '% (' + c.area_km2 + ' km2)' });
      seg.style.width = c.percent + '%'; seg.style.background = COLORS[i]; bar.appendChild(seg);
      var li = el('li'); var sw = el('i'); sw.style.background = COLORS[i];
      li.appendChild(sw); li.appendChild(document.createTextNode(c.label + ' ' + fmt(c.percent, 1) + '%')); lg.appendChild(li);
    });

    var w = $('weights'); clear(w);
    Object.keys(s.weights).forEach(function (k) {
      var bar2 = el('div', { class: 'wbar' }, [el('div')]);
      bar2.firstChild.style.width = (s.weights[k] * 100) + '%';
      w.appendChild(el('div', { class: 'wrow' }, [el('span', { text: k }), bar2, el('span', { text: fmt(s.weights[k], 3) })]));
    });
    var info = s.weight_info || {};
    var line = s.aggregation.toUpperCase() + ' + ' + String(info.method || '').toUpperCase() + ' weights';
    if (info.consistency_ratio !== undefined) line += ' (AHP consistency ratio ' + fmt(info.consistency_ratio, 3) + (info.consistent ? ', acceptable' : ', NOT acceptable') + ')';
    w.appendChild(el('p', { class: 'hint', text: line }));

    var tl = $('toplist'); clear(tl);
    st.top.slice(0, 8).forEach(function (t) {
      var b = el('button', { type: 'button', text: t.name });
      b.addEventListener('click', function () { selectOrigin(t.uid); var s2 = state.settlements.filter(function (x) { return x.uid === t.uid; })[0]; if (s2) map.flyTo(s2.ll, Math.max(map.getZoom(), 12)); });
      tl.appendChild(el('li', {}, [b, el('span', { class: 'sub', text: ' - ' + t.risk_label + ', ' + fmt(t.risk_mean, 2) + ' (' + t.driver + ')' })]));
    });

    var dl = $('downloads'); clear(dl);
    if (!SA && state.runId) {
      var base = '/runs/' + encodeURIComponent(state.runId) + '/';
      [['Report', 'report.md'], ['Risk map (PNG)', 'maps/risk_map.png'], ['Routes map (PNG)', 'maps/routes_map.png'],
       ['Risk index (GeoTIFF)', 'risk/risk_index.tif'], ['High-risk zones', 'vectors/high_risk_zones.geojson'],
       ['Routes', 'vectors/emergency_routes.geojson'], ['Settlements CSV', 'tables/settlements_risk.csv'],
       ['Standalone map', 'viewer_standalone.html']].forEach(function (d) {
        dl.appendChild(el('a', { href: base + d[1], target: '_blank', rel: 'noopener', text: d[0] }));
      });
      dl.appendChild(el('a', { href: '/api/runs/' + encodeURIComponent(state.runId) + '/download', text: 'Everything (ZIP)' }));
    }
    var wl = $('warnings'); clear(wl);
    (s.warnings || []).forEach(function (m) { wl.appendChild(el('li', { text: m })); });
  }

  /* ------------------------------------------------------------------ routing */
  function fillRouting(sett) {
    var sel = $('origin-select'); clear(sel);
    if (!SA) sel.appendChild(el('option', { value: '', text: '- point picked on the map -' }));
    var list = state.settlements.slice().sort(function (a, b) { return (b.risk || 0) - (a.risk || 0); });
    list.forEach(function (s) { sel.appendChild(el('option', { value: s.uid, text: s.name + ' (' + s.label + ')' })); });
    if (list.length) selectOrigin(list[0].uid);
    var K = state.summary.routing.risk_aversion;
    $('aversion').value = K; $('aversion-out').textContent = K;
    if (SA) { $('aversion-wrap').hidden = true; $('fixed-k').hidden = false; $('fixed-k').textContent = 'This offline file shows routes precomputed with risk aversion ' + K + '. Use the web app for a slider and custom start points.'; }
    clear($('route-result')); setStatus('route-status', '');
  }

  function selectOrigin(uid) {
    var s = state.settlements.filter(function (x) { return x.uid === uid; })[0];
    if (!s) return;
    $('origin-select').value = uid;
    setOrigin(s.ll, uid);
  }
  function setOrigin(ll, uid) {
    state.originLL = ll; state.originUid = uid;
    if (state.originMarker) map.removeLayer(state.originMarker);
    state.originMarker = L.marker(ll, { icon: L.divIcon({ className: '', html: '<div class="mk origin">A</div>', iconSize: [24, 24], iconAnchor: [12, 12] }), keyboard: false }).addTo(map);
  }
  function setPick(on) { state.pick = on; document.body.classList.toggle('pick', on); $('btn-pick').textContent = on ? 'Click the map...' : 'Pick start on map'; }

  function clearRoute() {
    if (state.routeGroup) state.routeGroup.clearLayers();
    clear($('route-result'));
  }

  function fetchRoute(ftype, K) {
    if (SA) {
      var r = SA.routes[state.originUid] && SA.routes[state.originUid][ftype];
      return r ? Promise.resolve(r) : Promise.reject(new Error('No precomputed route for this start.'));
    }
    return fetch('/api/route', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ run_id: state.runId, lat: state.originLL.lat, lon: state.originLL.lng, facility_type: ftype, risk_aversion: K }) })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || 'Routing failed'); return j; }); });
  }

  function runRoute() {
    if (!state.originLL) { setStatus('route-status', 'Choose a start point first.', true); return; }
    var ftype = document.querySelector('input[name=ftype]:checked').value;
    var K = Number($('aversion').value);
    setStatus('route-status', 'Routing...');
    fetchRoute(ftype, K).then(function (res) { drawRoute(res); setStatus('route-status', ''); })
      .catch(function (e) { clearRoute(); setStatus('route-status', e.message, true); });
  }

  function drawRoute(res) {
    state.routeGroup.clearLayers();
    var fs = res.features;
    function pick(kind) { return fs.filter(function (f) { return f.properties.kind === kind; }); }
    function line(f) { return f.geometry.coordinates.map(function (c) { return [c[1], c[0]]; }); }
    pick('access_origin').concat(pick('access_facility')).forEach(function (f) {
      L.polyline(line(f), { color: '#555', weight: 2, dashArray: '2 6' }).addTo(state.routeGroup);
    });
    pick('shortest').forEach(function (f) { L.polyline(line(f), { color: '#111', weight: 3, dashArray: '8 7', opacity: 0.85 }).addTo(state.routeGroup); });
    var lrBounds = null;
    pick('least_risk').forEach(function (f) {
      var pts = line(f);
      L.polyline(pts, { color: '#fff', weight: 9, opacity: 0.9 }).addTo(state.routeGroup);
      var pl = L.polyline(pts, { color: '#0b6bcb', weight: 5 }).addTo(state.routeGroup);
      lrBounds = pl.getBounds();
    });
    var dest = res.facility;
    var ends = pick('least_risk')[0];
    if (ends) {
      var last = line(ends).slice(-1)[0];
      L.circleMarker(last, { radius: 11, color: '#0b6bcb', weight: 3, fill: false }).bindTooltip('Destination: ' + dest.name, { permanent: false }).addTo(state.routeGroup);
    }
    var all = lrBounds;
    pick('shortest').forEach(function (f) { var b = L.polyline(line(f)).getBounds(); all = all ? all.extend(b) : b; });
    if (all) map.fitBounds(all, { padding: [60, 60], maxZoom: 15 });

    var a = res.least_risk, b = res.shortest, c = res.comparison;
    var box = $('route-result'); clear(box);
    var rows = [
      ['Destination', res.facility.name, res.shortest_facility.name],
      ['Distance (km)', fmt(a.length_km, 2), fmt(b.length_km, 2)],
      ['Est. time (min)', fmt(a.est_minutes, 0), fmt(b.est_minutes, 0)],
      ['Mean risk (0-1)', fmt(a.mean_risk, 3), fmt(b.mean_risk, 3)],
      ['Peak risk (0-1)', fmt(a.max_risk, 3), fmt(b.max_risk, 3)],
      ['km in High+ zones', fmt(a.high_risk_km, 2), fmt(b.high_risk_km, 2)],
      ['Road segments in Very High', String(a.segments_in_very_high), String(b.segments_in_very_high)]
    ];
    var t = el('table');
    t.appendChild(el('tr', {}, [el('th', { text: '' }), el('th', { class: 'lr', text: 'Least-risk' }), el('th', { text: 'Shortest' })]));
    rows.forEach(function (r) { t.appendChild(el('tr', {}, [el('td', { text: r[0] }), el('td', { class: 'lr', text: r[1] }), el('td', { text: r[2] })])); });
    box.appendChild(t);
    var msg;
    if (c.same_route) msg = 'The shortest route is already the least-risk route here.';
    else msg = 'The least-risk route is ' + fmt(Math.abs(c.extra_km), 2) + ' km ' + (c.extra_km >= 0 ? 'longer' : 'shorter') + ' (' + fmt(Math.abs(c.extra_pct), 0) + '%) and cuts mean route risk by ' + fmt(c.risk_reduction_pct, 0) + '% (risk-km by ' + fmt(c.exposure_reduction_pct, 0) + '%).';
    box.appendChild(el('p', { class: 'note', text: msg }));
  }

  /* ------------------------------------------------------------------ run form (server mode) */
  function buildOptions(form) {
    var fd = new FormData(form);
    // drop empty file inputs so the server does not receive 0-byte parts
    ['roads', 'settlements', 'hospitals', 'shelters', 'flood', 'landslide', 'slope', 'dem', 'rainfall', 'ndvi'].forEach(function (k) {
      var f = fd.get(k); if (f && f.size === 0) fd.delete(k);
    });
    return fd;
  }
  function submitRun(e) {
    e.preventDefault();
    var form = $('run-form');
    $('btn-run').disabled = true; $('btn-demo').disabled = true;
    setStatus('run-status', 'Running analysis - this can take a minute for large areas...');
    fetch('/api/run', { method: 'POST', body: buildOptions(form) }).then(handleRun).catch(failRun);
  }
  function demoRun() {
    $('btn-run').disabled = true; $('btn-demo').disabled = true;
    setStatus('run-status', 'Generating demo data and running...');
    fetch('/api/demo', { method: 'POST' }).then(handleRun).catch(failRun);
  }
  function handleRun(r) {
    return r.json().then(function (j) {
      $('btn-run').disabled = false; $('btn-demo').disabled = false;
      if (!r.ok) { setStatus('run-status', j.error || 'The analysis failed.', true); return; }
      return loadRun(j.run_id).then(listRuns);
    });
  }
  function failRun(e) { $('btn-run').disabled = false; $('btn-demo').disabled = false; setStatus('run-status', 'Request failed: ' + e.message, true); }

  function listRuns() {
    return fetch('/api/runs').then(function (r) { return r.json(); }).then(function (j) {
      var sel = $('prev-runs'); clear(sel);
      sel.appendChild(el('option', { value: '', text: '- choose -' }));
      (j.runs || []).forEach(function (id) { sel.appendChild(el('option', { value: id, text: id })); });
      $('prev-wrap').hidden = !(j.runs && j.runs.length);
    }).catch(function () {});
  }

  /* ------------------------------------------------------------------ boot */
  function boot() {
    initMap();
    $('opacity').addEventListener('input', function () {
      var v = Number(this.value);
      Object.keys(state.layers).forEach(function (k) { if (state.layers[k]._isHazard) state.layers[k].setOpacity(v); });
    });
    $('aversion').addEventListener('input', function () {
      $('aversion-out').textContent = this.value;
      clearTimeout(state.routeTimer);
      state.routeTimer = setTimeout(function () { if (state.originLL && !SA) runRoute(); }, 250);
    });
    $('btn-route').addEventListener('click', runRoute);
    $('origin-select').addEventListener('change', function () { if (this.value) { selectOrigin(this.value); runRoute(); } });
    document.querySelectorAll('input[name=ftype]').forEach(function (r) { r.addEventListener('change', function () { if (state.originLL) runRoute(); }); });
    $('btn-pick').addEventListener('click', function () { setPick(!state.pick); });

    if (SA) {
      document.body.classList.add('standalone');
      loadRun(null);
      return;
    }
    $('run-form').addEventListener('submit', submitRun);
    $('btn-demo').addEventListener('click', demoRun);
    $('weights-method').addEventListener('change', function () {
      $('opt-manual').hidden = this.value !== 'manual';
      $('opt-ahp').hidden = this.value !== 'ahp';
    });
    $('prev-runs').addEventListener('change', function () { if (this.value) loadRun(this.value); });
    listRuns();
    var q = new URLSearchParams(window.location.search).get('run');
    if (q) loadRun(q);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
