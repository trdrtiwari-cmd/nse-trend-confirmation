
const labels = {
  advance_decline: 'Advance–Decline Ratio',
  sector_heatmap: 'Sector Heat Map',
  option_premium_change: 'Option Premium % Change',
  call_put_graph: 'CE/PE Premium Graph',
  index_direction: 'Index Price Direction'
};

const symbol = document.getElementById('symbol');
const signal = document.getElementById('signal');
const message = document.getElementById('message');
const updated = document.getElementById('updated');
const sourceStatus = document.getElementById('sourceStatus');
const cards = [...document.querySelectorAll('.check:not(.note)')];
const livePill = document.querySelector('header .pill');
const refreshButton = document.getElementById('refresh');

const directions = {
  1: 'Bullish',
  0: 'Neutral',
  '-1': 'Bearish'
};

function formatTime(value) {
  if (!value) return 'समय उपलब्ध नहीं';

  const date = new Date(value);

  if (Number.isNaN(date.getTime())) return String(value);

  return date.toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata',
    dateStyle: 'medium',
    timeStyle: 'medium'
  }) + ' IST';
}

function describe(key, value) {
  if (value === null || value === undefined) {
    return 'डेटा उपलब्ध नहीं';
  }

  if (key === 'advance_decline') {
    return 'A/D: ' + (value.ratio ?? '—');
  }

  if (key === 'sector_heatmap') {
    return 'Bias: ' + (directions[value.bias] ?? '—');
  }

  if (key === 'option_premium_change') {
    return `CE ${value.ce_pct ?? '—'}% · PE ${value.pe_pct ?? '—'}%`;
  }

  if (key === 'call_put_graph') {
    return 'Bias: ' + (directions[value.bias] ?? '—');
  }

  if (key === 'index_direction') {
    const price = Number.isFinite(Number(value.price))
      ? Number(value.price).toLocaleString('en-IN', {
          maximumFractionDigits: 2
        })
      : '—';

    if (
      value.fresh !== true ||
      value.bias === null ||
      value.bias === undefined
    ) {
      return `STALE / UNVERIFIED · Price: ${price}`;
    }

    return `${directions[value.bias] ?? '—'} · Price: ${price}`;
  }

  return 'डेटा उपलब्ध';
}

function setSignalWait(text) {
  signal.textContent = 'WAIT';
  signal.style.color = '#ffd58a';

  if (text) message.textContent = text;
}

function updateCard(card, key, value) {
  const strong = card.querySelector('strong');
  const small = card.querySelector('small');

  if (!strong || !small) return;

  strong.textContent = describe(key, value);

  if (key === 'index_direction' && value) {
    small.textContent =
      'Candle: ' + formatTime(value.candle_time) +
      ' · ' + (
        value.fresh === true &&
        value.bias !== null &&
        value.bias !== undefined
          ? 'Fresh direction'
          : 'पुरानी/अमान्य candle; signal में उपयोग नहीं'
      );

    return;
  }

  small.textContent = value
    ? 'Data received; freshness and source validation required'
    : 'डेटा अनुपलब्ध — इस check से signal नहीं बनेगा';
}

function updateStatus(result) {
  const direction = result.data?.index_direction;
  const status = result.market_data_status || 'UNAVAILABLE';

  if (livePill) {
    if (status === 'STALE') {
      livePill.textContent = 'INDEX DATA: STALE';
    } else if (status === 'API_ERROR') {
      livePill.textContent = 'UPSTOX API: ERROR';
    } else if (status === 'CONNECTION_ERROR') {
      livePill.textContent = 'API: CONNECTION ERROR';
    } else if (status === 'INVALID_RESPONSE') {
      livePill.textContent = 'API: INVALID RESPONSE';
    } else if (
      direction &&
      direction.fresh === true &&
      direction.bias !== null &&
      direction.bias !== undefined
    ) {
      livePill.textContent = 'INDEX DATA: RECEIVED';
    } else {
      livePill.textContent = 'INDEX DATA: UNAVAILABLE';
    }
  }

  sourceStatus.textContent =
    'Source: ' + (result.source || 'Unknown') +
    ' · Status: ' + status +
    ' · ' + (
      result.ready
        ? 'All required checks validated'
        : 'Required data incomplete or not fresh'
    );
}

async function refresh() {
  setSignalWait('डेटा स्रोत की जाँच हो रही है…');
  updated.textContent = 'जाँच जारी…';

  if (livePill) livePill.textContent = 'DATA STATUS: CHECKING';
  if (refreshButton) refreshButton.disabled = true;

  try {
    const response = await fetch(
      '/api/snapshot/' + encodeURIComponent(symbol.value),
      { cache: 'no-store' }
    );

    if (!response.ok) {
      throw new Error('HTTP ' + response.status);
    }

    const result = await response.json();

    // The backend's signal is displayed only when all checks are ready.
    // This integration version intentionally keeps WAIT as the fallback.
    const safeSignal =
      result.ready === true &&
      ['BUY CE', 'BUY PE', 'WAIT'].includes(result.signal)
        ? result.signal
        : 'WAIT';

    signal.textContent = safeSignal;

    if (safeSignal === 'BUY CE') {
      signal.style.color = '#61e3b6';
    } else if (safeSignal === 'BUY PE') {
      signal.style.color = '#ff7d8a';
    } else {
      signal.style.color = '#ffd58a';
    }

    message.textContent =
      result.message ||
      (result.reasons || []).join(' ') ||
      'सभी आवश्यक डेटा की पुष्टि करें।';

    updated.textContent = result.as_of
      ? 'Candle time: ' + formatTime(result.as_of) +
        (result.checked_at
          ? ' · Checked: ' + formatTime(result.checked_at)
          : '')
      : 'ताज़ा candle timestamp उपलब्ध नहीं';

    updateStatus(result);

    cards.forEach(card => {
      const heading = card.querySelector('h3')?.textContent;
      const key = Object.keys(labels).find(
        item => labels[item] === heading
      );

      if (!key) return;

      updateCard(card, key, result.data?.[key]);
    });

  } catch (error) {
    setSignalWait(
      'सर्वर से डेटा नहीं मिला। अनुमानित trading signal रोक दिया गया है।'
    );

    updated.textContent = 'कनेक्शन त्रुटि';
    sourceStatus.textContent = 'API अनुपलब्ध';

    if (livePill) livePill.textContent = 'DATA STATUS: ERROR';

    console.error('Snapshot request failed:', error);
  } finally {
    if (refreshButton) refreshButton.disabled = false;
  }
}

if (refreshButton) {
  refreshButton.addEventListener('click', refresh);
}

if (symbol) {
  symbol.addEventListener('change', refresh);
}

refresh();
