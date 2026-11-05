// O Link payment guard. Runs in every page and frame.
//
// While O's debugger is attached to this tab (i.e. while O is working here), it blocks:
//   - typing, pasting or autofilling into payment fields (card number, CVC, expiry, IBAN, ...)
//   - anything inside payment-provider frames and on payment sites (Stripe, PayPal, Klarna, ...)
//   - clicking pay / buy / place order / checkout / subscribe / donate / send money buttons
//   - submitting a form that contains payment fields
// O's clicks and typing arrive as real browser input events, so blocking them here (before
// the page sees them) stops them. When O detaches, the page works normally again.
(() => {
  if (window.__oGuard) return;
  window.__oGuard = true;

  let active = false;

  const PAY_HOSTS = /(^|\.)(paypal\.com|paypalobjects\.com|venmo\.com|cash\.app|pay\.google\.com|payments\.google\.com|checkout\.stripe\.com|js\.stripe\.com|hooks\.stripe\.com|m\.stripe\.network|braintreegateway\.com|braintree-api\.com|adyen\.com|checkout\.com|squareup\.com|square\.link|klarna\.com|afterpay\.com|clearpay\.co\.uk|affirm\.com|zip\.co|sezzle\.com|pay\.amazon\.com|payments\.amazon\.com|wise\.com|zellepay\.com|recurly\.com|chargebee\.com|paddle\.com|2checkout\.com|authorize\.net|worldpay\.com|payoneer\.com|skrill\.com|revolut\.com|coinbase\.com|apple\.com\/apple-pay)$/i;
  const onPaySite = PAY_HOSTS.test(location.hostname) || /\/(checkout|payment|pay|billing|purchase)(\/|$|\?)/i.test(location.pathname);

  const FIELD = /(card.?num|cc.?num|credit.?card|debit.?card|cardnumber|\bcvc\b|\bcvv\d?\b|\bcsc\b|security.?code|card.?code|expir|exp.?(date|month|year)|\bmm.?yy\b|card.?holder|name.?on.?card|\biban\b|\bbic\b|swift|routing.?(number|no)|sort.?code|account.?(number|no)|bank.?account|\bpin\b)/i;
  // Buttons that always mean money (short button/link text only, never quiz options or labels).
  const BUTTON = /\b(pay\s+now|pay\s+[$€£¥₹]|place\s+(your\s+)?order|buy\s+(it\s+)?now|complete\s+(purchase|order|payment|checkout)|confirm\s+(and\s+pay|payment|purchase|order)|submit\s+(order|payment)|proceed\s+to\s+(checkout|payment)|check\s?out|add\s+funds|top\s?up|send\s+money|transfer\s+money|pay\s+with|donate(\s+now)?|add\s+(a\s+)?(card|payment\s+method)|purchase\s+now)\b|^\s*(pay|buy|purchase|order\s+now)\s*$/i;
  // Words that mean money only when a price is shown ("Subscribe" on YouTube is free).
  const PRICED = /\b(subscribe|start\s+(my\s+)?(free\s+)?trial|upgrade|buy|get|rent|order|join|renew|purchase|unlock)\b/i;
  const PRICE = /[$€£¥₹]\s?\d|\d\s?(usd|eur|gbp)\b|\/\s?(mo|month|yr|year)\b|per\s+(month|year)/i;

  function describe(el) {
    if (!el || el.nodeType !== 1) return '';
    const bits = [el.getAttribute('autocomplete'), el.name, el.id, el.getAttribute('aria-label'), el.getAttribute('placeholder'),
      el.getAttribute('data-elements-stable-field-name'), el.getAttribute('title')];
    if (el.labels) for (const l of el.labels) bits.push(l.textContent);
    return bits.filter(Boolean).join(' ');
  }
  function isPayField(el) {
    if (!el || el.nodeType !== 1) return false;
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    if (ac.startsWith('cc-') || ac.includes(' cc-')) return true;
    return FIELD.test(describe(el));
  }
  function formHasPayFields(form) {
    if (!form) return false;
    for (const el of form.querySelectorAll('input, select, textarea')) if (isPayField(el)) return true;
    return !!form.querySelector('iframe[src*="stripe"], iframe[src*="braintree"], iframe[src*="adyen"], iframe[src*="paypal"], iframe[name*="card"], iframe[title*="card" i], iframe[title*="payment" i]');
  }
  function buttonText(el) {
    return [el.innerText || el.textContent || '', el.value, el.getAttribute('aria-label'), el.getAttribute('title')]
      .filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().slice(0, 120);
  }
  function clickTarget(t) {
    return t && t.closest && t.closest('button, a, input[type=submit], input[type=button], input[type=image], [role=button], [role=link]');
  }
  function isMoneyButton(text) {
    if (!text || text.length > 60) return false;
    return BUTTON.test(text) || (PRICED.test(text) && PRICE.test(text));
  }

  function toast(text) {
    try {
      let el = document.getElementById('__o_guard_toast');
      if (!el) {
        el = document.createElement('div');
        el.id = '__o_guard_toast';
        el.setAttribute('role', 'alert');
        el.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;max-width:360px;padding:10px 14px;border-radius:10px;background:#2a1d1a;color:#fff;font:13px/1.4 system-ui,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.35);border:2px solid #D87556';
        (document.body || document.documentElement).appendChild(el);
      }
      el.textContent = text;
      clearTimeout(el.__t);
      el.__t = setTimeout(() => el.remove(), 6000);
    } catch (_) {}
  }

  function block(ev, reason) {
    ev.preventDefault();
    ev.stopImmediatePropagation();
    if (ev.type === 'click' || ev.type === 'submit' || ev.type === 'beforeinput' || ev.type === 'paste') {
      toast('Blocked by O: O never makes payments or purchases (' + reason + ').');
      try { chrome.runtime.sendMessage({ o: 'blocked', reason }); } catch (_) {}
    }
  }

  // Input into payment fields (and anything at all on payment sites / payment frames).
  for (const type of ['beforeinput', 'keydown', 'keypress', 'paste', 'drop']) {
    window.addEventListener(type, (ev) => {
      if (!active) return;
      const t = ev.target;
      if (onPaySite) return block(ev, 'payment page');
      if (isPayField(t) || isPayField(document.activeElement)) return block(ev, 'payment field');
    }, true);
  }
  // Pay / buy / checkout buttons, and anything on payment sites.
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'auxclick', 'dblclick']) {
    window.addEventListener(type, (ev) => {
      if (!active) return;
      if (onPaySite) return block(ev, 'payment page');
      const el = clickTarget(ev.target);
      if (!el) {
        // A label or radio inside a payment form (e.g. choosing a saved card) is still payment.
        const f = ev.target && ev.target.closest && ev.target.closest('form');
        if (f && formHasPayFields(f) && isPayField(ev.target)) return block(ev, 'payment form');
        return;
      }
      const text = buttonText(el);
      if (isMoneyButton(text)) return block(ev, `"${text.slice(0, 40)}" button`);
      const form = el.closest('form');
      const isSubmit = el.matches('button:not([type]), button[type=submit], input[type=submit], input[type=image]');
      if (isSubmit && formHasPayFields(form)) return block(ev, 'payment form');
    }, true);
  }
  // Enter-to-submit and script submits of payment forms.
  window.addEventListener('submit', (ev) => {
    if (!active) return;
    if (onPaySite || formHasPayFields(ev.target)) block(ev, 'payment form');
  }, true);

  // Is O attached to this tab right now?
  try {
    chrome.runtime.onMessage.addListener((msg) => { if (msg && msg.o === 'active') active = !!msg.on; });
    chrome.runtime.sendMessage({ o: 'state' }, (r) => { void chrome.runtime.lastError; if (r) active = !!r.active; });
  } catch (_) {}
})();
