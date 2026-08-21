// Shared chart behaviour: CSS custom-property bar fills, and the SVG line-chart
// crosshair + tooltip used by the spending trend and savings trajectory charts.

function initPctBars() {
  document.querySelectorAll('[data-pct]').forEach(function (el) {
    el.style.setProperty('--pct', el.getAttribute('data-pct') + '%');
  });
}

function fmtRp(n) {
  return 'Rp ' + Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, '.');
}

// config: { svgId, crosshairId, dotId, tooltipId, totalKey='cumulative',
//           labelKey='label', dayKey, daySuffix }
function initLineChart(config) {
  var svg = document.getElementById(config.svgId);
  if (!svg) return;
  var pts = JSON.parse(svg.getAttribute('data-points') || '[]');
  if (!pts.length) return;

  var totalKey = config.totalKey || 'cumulative';
  var labelKey = config.labelKey || 'label';

  var crosshair = document.getElementById(config.crosshairId);
  var dot = document.getElementById(config.dotId);
  var tip = document.getElementById(config.tooltipId);
  var ttDate = tip.querySelector('.tt-date');
  var ttTotal = tip.querySelector('.tt-total');
  var ttDay = tip.querySelector('.tt-day');
  var viewW = svg.viewBox.baseVal.width;

  function nearest(xUser) {
    var best = pts[0], bestDist = Math.abs(pts[0].px - xUser);
    for (var i = 1; i < pts.length; i++) {
      var dist = Math.abs(pts[i].px - xUser);
      if (dist < bestDist) { best = pts[i]; bestDist = dist; }
    }
    return best;
  }

  function show(evt) {
    var rect = svg.getBoundingClientRect();
    var scaleX = viewW / rect.width;
    var xUser = (evt.clientX - rect.left) * scaleX;
    var p = nearest(xUser);
    crosshair.setAttribute('x1', p.px);
    crosshair.setAttribute('x2', p.px);
    crosshair.setAttribute('opacity', 1);
    dot.setAttribute('cx', p.px);
    dot.setAttribute('cy', p.py);
    dot.setAttribute('opacity', 1);
    ttDate.textContent = p[labelKey];
    ttTotal.textContent = fmtRp(p[totalKey]);
    if (config.dayKey) {
      ttDay.textContent = fmtRp(p[config.dayKey]) + (config.daySuffix || '');
      ttDay.style.display = '';
    } else {
      ttDay.style.display = 'none';
    }
    // Clamp so the tooltip never spills past the chart edges on narrow
    // (mobile) viewports, where the SVG is nearly the full screen width.
    var rawLeft = (p.px / viewW) * rect.width;
    var half = tip.offsetWidth / 2;
    var clampedLeft = Math.min(Math.max(rawLeft, half + 4), rect.width - half - 4);
    tip.style.left = clampedLeft + 'px';
    tip.style.opacity = 1;
  }

  function hide() {
    crosshair.setAttribute('opacity', 0);
    dot.setAttribute('opacity', 0);
    tip.style.opacity = 0;
  }

  svg.addEventListener('pointermove', show);
  svg.addEventListener('pointerdown', show);
  svg.addEventListener('pointerleave', hide);
}
