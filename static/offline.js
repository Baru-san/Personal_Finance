// Service worker registration + the topbar connection indicator.
// (The offline write queue is appended further down this file — see the
// IndexedDB section below.)

if ('serviceWorker' in navigator) {
  // Registered from the site root (not /static/sw.js) so its scope is '/' —
  // see the /sw.js route in app.py for why.
  navigator.serviceWorker.register('/sw.js').catch(function (err) {
    console.warn('Service worker registration failed:', err);
  });
}

// navigator.onLine only reflects "is a network interface present" — it stays
// true on WiFi with no real route out, which is the actual bad-network case
// this app targets. A same-origin HEAD ping is a much more honest check.
function checkConnectivity() {
  fetch('/', { method: 'HEAD', cache: 'no-store' })
    .then(function () { setConnectionState(true); })
    .catch(function () { setConnectionState(false); });
}

var lastKnownOnline = navigator.onLine;
var pendingQueueCount = 0;

function setConnectionState(isOnline) {
  var wasOffline = !lastKnownOnline;
  lastKnownOnline = isOnline;
  renderConnectionStatus();
  if (isOnline && wasOffline) replayQueue();
}

function renderConnectionStatus() {
  var dot = document.querySelector('.topbar .status .dot');
  var label = document.querySelector('.topbar .status .status-label');
  if (!dot || !label) return;

  if (!lastKnownOnline) {
    dot.classList.remove('blink');
    dot.classList.add('offline');
    label.textContent = 'OFFLINE  | ';
    return;
  }

  dot.classList.remove('offline');
  dot.classList.add('blink');
  label.textContent = (pendingQueueCount > 0 ? pendingQueueCount + ' QUEUED' : 'SYNCED') + '  | ';
}

// The browser's own 'offline' event is trustworthy (no interface -> no
// doubt); 'online' just means a route reappeared, so verify with a real ping
// before flipping the indicator back.
window.addEventListener('offline', function () { setConnectionState(false); });
window.addEventListener('online', checkConnectivity);
document.addEventListener('DOMContentLoaded', function () {
  renderConnectionStatus();
  checkConnectivity();
});

// ============================================================
// Offline write queue — lets the five "add" forms (spend, one-off
// expense/income, scheduled category/income source) keep working with no
// signal: the submission is stored in IndexedDB and replayed automatically
// on reconnect. Deliberately NOT applied to edit_category, edit_journal_entry,
// or close_day: the edits don't need it (they're already idempotent — see
// app.py), and close_day is cron-driven and derives its window from the
// server's current date, so replaying it later would sweep the wrong period.
// ============================================================

var QUEUE_DB_NAME = 'ledger-offline-queue';
var QUEUE_STORE = 'writes';

function uuid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
    var r = (Math.random() * 16) | 0;
    var v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

function openQueueDB() {
  return new Promise(function (resolve, reject) {
    if (!('indexedDB' in window)) { reject(new Error('no indexedDB')); return; }
    var req = indexedDB.open(QUEUE_DB_NAME, 1);
    req.onupgradeneeded = function () {
      req.result.createObjectStore(QUEUE_STORE, { keyPath: 'clientId' });
    };
    req.onsuccess = function () { resolve(req.result); };
    req.onerror = function () { reject(req.error); };
  });
}

function queueAdd(record) {
  return openQueueDB().then(function (db) {
    return new Promise(function (resolve, reject) {
      var tx = db.transaction(QUEUE_STORE, 'readwrite');
      tx.objectStore(QUEUE_STORE).put(record);
      tx.oncomplete = resolve;
      tx.onerror = function () { reject(tx.error); };
    });
  });
}

function queueRemove(clientId) {
  return openQueueDB().then(function (db) {
    return new Promise(function (resolve, reject) {
      var tx = db.transaction(QUEUE_STORE, 'readwrite');
      tx.objectStore(QUEUE_STORE).delete(clientId);
      tx.oncomplete = resolve;
      tx.onerror = function () { reject(tx.error); };
    });
  });
}

function queueAll() {
  return openQueueDB().then(function (db) {
    return new Promise(function (resolve, reject) {
      var tx = db.transaction(QUEUE_STORE, 'readonly');
      var req = tx.objectStore(QUEUE_STORE).getAll();
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  });
}

function refreshQueueBanner(sessionExpired) {
  queueAll().then(function (records) {
    pendingQueueCount = records.length;
    renderConnectionStatus();
    renderQueueBanner(records.length, sessionExpired);
  }).catch(function () { /* IndexedDB unavailable — queue feature simply no-ops */ });
}

function renderQueueBanner(count, sessionExpired) {
  var existing = document.getElementById('queue-banner');
  if (count <= 0) {
    if (existing) existing.remove();
    return;
  }
  if (!existing) {
    existing = document.createElement('div');
    existing.id = 'queue-banner';
    existing.className = 'queue-banner';
    var topbar = document.querySelector('.topbar');
    if (topbar && topbar.parentNode) {
      topbar.parentNode.insertBefore(existing, topbar.nextSibling);
    } else {
      document.body.insertBefore(existing, document.body.firstChild);
    }
  }
  var noun = count === 1 ? 'entry' : 'entries';
  existing.textContent = sessionExpired
    ? '⚠ ' + count + ' ' + noun + ' queued — session expired, log in to sync.'
    : '⚠ ' + count + ' ' + noun + ' queued — will sync when back online.';
}

function isLoginBounce(res) {
  // A redirect on its own does NOT mean the write failed: every add route
  // answers a successful POST with 302 -> "/" or "/income", and fetch follows
  // it, so res.redirected is true for the happy path too. An unauthenticated
  // POST is answered with a bare 401 instead (app.py's require_login), never
  // a redirect — so the only redirect worth distrusting is one that actually
  // lands on the login screen.
  if (!res.redirected) return false;
  try {
    return new URL(res.url, window.location.origin).pathname === '/login';
  } catch (e) {
    return false;
  }
}

function replayQueue() {
  queueAll().then(function (records) {
    var sessionExpired = false;
    var attempts = records.map(function (record) {
      return fetch(record.url, { method: 'POST', body: new URLSearchParams(record.params) })
        .then(function (res) {
          // Only a bounce to /login or a 401 means this write was never
          // saved — do not remove it from the queue then, even though the
          // fetch itself "succeeded". Any other 2xx confirms the server has
          // it, including the 302-to-dashboard that a normal save returns.
          if (res.ok && !isLoginBounce(res)) return queueRemove(record.clientId);
          if (isLoginBounce(res) || res.status === 401) sessionExpired = true;
        })
        .catch(function () { /* still offline — leave it queued, next trigger retries */ });
    });
    Promise.all(attempts).then(function () { refreshQueueBanner(sessionExpired); });
  }).catch(function () {});
}

function formatRpForQueue(n) {
  return 'Rp ' + Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, '.');
}

// Optimistic local update for a category spend: adjusts the figures on that
// category's own card. Deliberately minimal — it does not recompute
// dashboard-wide totals (income/available/funded-count), which stay in sync
// on the next real page load. See build_dashboard_data() in app.py for the
// full computation this intentionally does not duplicate.
function applyOptimisticExpense(categoryId, amount) {
  var card = document.querySelector('.window[data-category-id="' + categoryId + '"]');
  if (!card) return;
  var budget = parseInt(card.getAttribute('data-budget'), 10) || 0;
  var spent = (parseInt(card.getAttribute('data-spent'), 10) || 0) + amount;
  card.setAttribute('data-spent', spent);

  var h3 = card.querySelector('.window-spend-line');
  if (h3) {
    var suffix = h3.querySelector('.per-window');
    h3.firstChild.textContent = formatRpForQueue(spent) + ' / ' + formatRpForQueue(budget) + ' ';
    if (suffix) h3.appendChild(suffix);
  }

  var barFill = card.querySelector('.bar-fill');
  if (barFill && budget > 0) {
    var pct = Math.min(100, Math.round((spent / budget) * 100));
    barFill.setAttribute('data-pct', pct);
    barFill.style.setProperty('--pct', pct + '%');
    barFill.classList.remove('over', 'warn');
    if (spent > budget) barFill.classList.add('over');
  }
}

function insertPendingLogEntry(kind, categoryName, note, amount) {
  var log = document.getElementById('recent-activity-log');
  if (!log) return;
  var empty = log.querySelector('.empty');
  if (empty) empty.remove();

  var isSpend = kind !== 'income';
  var entry = document.createElement('div');
  entry.className = 'log-entry';
  var tagClass = isSpend ? 'spend' : 'income';
  var tagLabel = isSpend ? '[SPEND]' : '[INCOME]';
  var sign = isSpend ? '-' : '+';
  var amtClass = isSpend ? 'neg' : 'pos';
  var today = new Date();
  var ts = String(today.getMonth() + 1).padStart(2, '0') + '-' + String(today.getDate()).padStart(2, '0');

  entry.innerHTML =
    '<div class="left"><div class="ts">' + ts + '</div><div class="msg">' +
    '<span class="tag ' + tagClass + '">' + tagLabel + '</span> ' +
    categoryName + (note ? ' — ' + note : '') +
    '</div></div><div class="amt ' + amtClass + '">' + sign + formatRpForQueue(amount) +
    ' <span class="pending-badge">⏳ PENDING</span></div>';

  log.insertBefore(entry, log.firstChild);
}

document.addEventListener('submit', function (event) {
  var form = event.target;
  var kind = form.getAttribute('data-offline-queue');
  if (!kind) return;

  event.preventDefault();
  var clientId = uuid();
  var formData = new FormData(form);
  formData.set('client_id', clientId);
  var params = new URLSearchParams();
  formData.forEach(function (v, k) { params.append(k, v); });

  fetch(form.action, { method: 'POST', body: params })
    .then(function (res) {
      // isLoginBounce catches the unauthenticated bounce to /login, which
      // fetch follows and reports as a plain 200 — without that check an
      // expired session would look like a successful save. It deliberately
      // does not reject every redirect: a save that worked returns one.
      if (!res.ok || isLoginBounce(res)) throw new Error('bad status ' + res.status);
      window.location.href = res.url || form.action;
    })
    .catch(function () {
      queueAdd({
        clientId: clientId,
        url: form.action,
        params: Array.from(params.entries()),
        kind: kind,
        timestamp: Date.now(),
      }).then(function () {
        if (kind === 'expense') {
          var categoryId = formData.get('category_id');
          var amount = parseInt(formData.get('amount'), 10) || 0;
          var card = document.querySelector('.window[data-category-id="' + categoryId + '"]');
          var categoryName = card ? card.getAttribute('data-category-name') : 'Category';
          applyOptimisticExpense(categoryId, amount);
          insertPendingLogEntry('spend', categoryName, formData.get('note'), amount);
        } else if (kind === 'custom_expense') {
          var amt = parseInt(formData.get('amount'), 10) || 0;
          insertPendingLogEntry('spend', 'Custom Expense', formData.get('label'), amt);
        }
        form.reset();
        refreshQueueBanner();
      });
    });
});

document.addEventListener('DOMContentLoaded', function () {
  refreshQueueBanner();
  replayQueue();
});
