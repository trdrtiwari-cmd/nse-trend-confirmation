
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

function describe(key, value) {
  if (value === null || value === undefined) return 'डेटा उपलब्ध नहीं';

  const directions = {
    1: 'Bullish',
    0: 'Neutral',
    '-1': 'Bearish'
  };

  if (key === 'advance_decline') {
    return 'A/D: ' + (value.ratio ?? '—');
  }

  if (key === 'sector_heatmap') {
    return 'Bias: ' + (directions[value.bias] ?? '—');
  }

  if (key === 'option_premium_change') {
    return `CE ${value.ce_pct ?? '—'}% · PE ${value.pe_pct ?? '—'}%`;
  }

  if (key === 'index_direction') {
    if (value.bias === null || value.bias === undefined) {
      return `पुराना डेटा · Price: ${value.price ?? '—'}`;
    }

    return `${directions[value.bias] ?? '—'} · Price: ${value.price ?? '—'}`;
  }

  return 'डेटा उपलब्ध';
}

async function refresh() {
  signal.textContent = 'WAIT';
  signal.style.color = '#ffd58a';
  message.textContent = 'डेटा स्रोत की जाँच हो रही है…';
  updated.textContent = 'जाँच जारी…';

  if (livePill) livePill.textContent = 'DATA STATUS: CHECKING';

  try {
    const response = await fetch(
      '/api/snapshot/' + encodeURIComponent(symbol.value),
      { cache: 'no-store' }
    );

    if (!response.ok) throw new Error('HTTP ' + response.status);

    const result = await response.json();

    // This version intentionally suppresses all BUY signals.
    signal.textContent = 'WAIT';
    message.textContent = result.message || 'डेटा सत्यापन आवश्यक है।';

    updated.textContent = result.as_of
      ? 'Candle time: ' + result.as_of
      : 'ताज़ा timestamp उपलब्ध नहीं';

    sourceStatus.textContent =
      'Source: ' + (result.source || 'Unknown') +
      ' · ' + (result.ready ? 'All checks present' : 'कुछ डेटा अभी उपलब्ध नहीं');

    if (livePill) {
      livePill.textContent = result.data?.index_direction
        ? 'INDEX DATA: RECEIVED'
        : 'INDEX DATA: UNAVAILABLE';
    }

    cards.forEach(card => {
      const heading = card.querySelector('h3')?.textContent;
      const key = Object.keys(labels).find(k => labels[k] === heading);
      if (!key) return;

      const value = result.data?.[key];
      card.querySelector('strong').textContent = describe(key, value);

      if (key === 'index_direction' && value) {
        card.querySelector('small').textContent =
          value.bias === null || value.bias === undefined
            ? 'Candle पुरानी है; direction उपयोग नहीं होगा'
            : 'Upstox 5-minute candle · ' + (value.candle_time || 'समय अनुपलब्ध');
      } else {
        card.querySelector('small').textContent = value
          ? 'डेटा मिला; source और timestamp सत्यापित करें'
          : 'डेटा अनुपलब्ध — इस चेक से सिग्नल नहीं बनेगा';
      }
    });
  } catch (error) {
    signal.textContent = 'WAIT';
    message.textContent =
      'सर्वर से डेटा नहीं मिला। अनुमानित सिग्नल रोक दिया गया है।';
    updated.textContent = 'कनेक्शन त्रुटि';
    sourceStatus.textContent = 'API अनुपलब्ध';

    if (livePill) livePill.textContent = 'DATA STATUS: ERROR';
  }
}

document.getElementById('refresh').addEventListener('click', refresh);
symbol.addEventListener('change', refresh);
refresh();
