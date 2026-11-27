document.addEventListener('click', e => {
  if (e.target.closest('[data-toggle-nav]')) document.body.classList.toggle('nav-open');
  const add = e.target.closest('[data-add-row]');
  if (add) { const box = document.querySelector(add.dataset.addRow), n = box.querySelector('.line').cloneNode(true);
    n.querySelectorAll('input').forEach(i => i.value = ''); n.querySelectorAll('select').forEach(s => s.selectedIndex = 0); box.appendChild(n); calc(); }
  if (e.target.closest('[data-del-row]')) { const r = e.target.closest('.line'); if (r.parentNode.querySelectorAll('.line').length > 1) { r.remove(); calc(); } }
});
// Order preview uses integer paise only (no floating point).
function inr(p) { const s = String(Math.floor(p / 100)); let h = s.slice(0, -3), t = s.slice(-3); if (s.length <= 3) h = '', t = s;
  h = h.replace(/\B(?=(\d{2})+(?!\d))/g, ','); return '₹' + (h ? h + ',' : '') + t + '.' + String(p % 100).padStart(2, '0'); }
function calc() {
  const out = document.getElementById('est'); if (!out) return;
  const c = document.querySelector('[name=customer_id]'), dist = c && c.selectedOptions[0] && c.selectedOptions[0].dataset.type === 'distributor';
  let tot = 0;
  document.querySelectorAll('.line').forEach(r => { const o = r.querySelector('[name=product_id]').selectedOptions[0], q = parseInt(r.querySelector('[name=qty]').value || '0', 10);
    if (o && o.value && q > 0) tot += q * parseInt((dist && o.dataset.d) ? o.dataset.d : o.dataset.p, 10); });
  out.textContent = inr(tot);
}
document.addEventListener('input', calc); document.addEventListener('change', e => {
  calc(); if (e.target.matches('[name=material_id]')) { const r = e.target.closest('.line').querySelector('[name=rate]'); if (r && !r.value) r.value = e.target.selectedOptions[0].dataset.cost || ''; }
});
