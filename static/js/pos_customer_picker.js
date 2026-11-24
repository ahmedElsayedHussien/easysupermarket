/*
 * POS customer picker: one type-ahead box instead of the long <select>.
 *
 * The original <select id="posCustomer"> stays in the page (hidden) and remains the single
 * source of truth: every POS function keeps reading / setting it. The picker only
 *   - searches its options (name tokens, phone / mobile, Arabic letter variants),
 *   - sets select.value + dispatches 'change' (exactly what a manual pick did),
 *   - mirrors the select when code changes it (tab switch, reset after a sale...).
 *
 * Keys: F6 / Alt+C focus the box · ↑ ↓ move · Enter pick · Esc close & back to barcode.
 */
(function () {
    'use strict';

    var MAX_RESULTS = 8;
    var RECENT_MAX = 5;
    var CASH_LABEL = 'عميل نقدي';

    function norm(s) {
        return String(s || '').toLowerCase()
            .replace(/[ً-ْـ]/g, '')          // tashkeel, tatweel
            .replace(/[أإآٱ]/g, 'ا').replace(/ة/g, 'ه').replace(/ى/g, 'ي')
            .replace(/[ؤ]/g, 'و').replace(/[ئ]/g, 'ي')
            .replace(/[٠-٩]/g, function (d) { return String(d.charCodeAt(0) - 0x660); })
            .replace(/\s+/g, ' ').trim();
    }
    function digits(s) { return norm(s).replace(/\D/g, ''); }
    function esc(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function init() {
        var sel = document.getElementById('posCustomer');
        var box = document.getElementById('posCustomerPicker');
        if (!sel || !box) return;
        var input = box.querySelector('.pcp-input');
        var list = box.querySelector('.pcp-list');
        // The list lives on <body> (fixed, top layer): inside the sub-bar it was painted
        // under the cart / tabs panel and couldn't be clicked.
        document.body.appendChild(list);
        function placeList() {
            var r = input.getBoundingClientRect();
            var w = Math.max(300, r.width);
            var rtl = getComputedStyle(box).direction === 'rtl';
            var left = rtl ? r.right - w : r.left;
            left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
            list.style.width = w + 'px';
            list.style.left = left + 'px';
            list.style.top = (r.bottom + 6) + 'px';
            list.style.maxHeight = Math.min(340, Math.max(160, window.innerHeight - r.bottom - 20)) + 'px';
        }
        window.addEventListener('resize', function () { if (open) placeList(); });
        window.addEventListener('scroll', function () { if (open) placeList(); }, true);
        var clearBtn = box.querySelector('.pcp-clear');
        var userKey = 'pos_recent_customers_' + (window.POS && POS.currentUserId || 'default');

        // ---- index of the select's options (built once; cheap even for thousands) ----
        var items = [];
        Array.prototype.forEach.call(sel.options, function (o) {
            if (!o.value) return;                                      // "عميل نقدي" (empty)
            if (norm(o.text) === norm(CASH_LABEL)) return;             // duplicate cash partner
            var phones = [o.dataset.phone, o.dataset.mobile].filter(Boolean);
            items.push({ id: o.value, name: o.text.trim(), n: norm(o.text), phones: phones,
                         d: phones.map(digits).join(' ') });
        });
        var byId = {};
        items.forEach(function (it) { byId[it.id] = it; });

        function recent() {
            try { return (JSON.parse(localStorage.getItem(userKey)) || []).filter(function (id) { return byId[id]; }); }
            catch (e) { return []; }
        }
        function pushRecent(id) {
            if (!id) return;
            try {
                var r = recent().filter(function (x) { return x !== id; });
                r.unshift(id);
                localStorage.setItem(userKey, JSON.stringify(r.slice(0, RECENT_MAX)));
            } catch (e) { /* storage blocked: ignore */ }
        }

        function search(q) {
            var nq = norm(q), dq = digits(q);
            if (!nq) {
                return [{ id: '', name: CASH_LABEL, hint: 'بدون عميل' }]
                    .concat(recent().map(function (id) { return Object.assign({ hint: 'مؤخراً' }, byId[id]); }));
            }
            var tokens = nq.split(' ');
            var scored = [];
            for (var i = 0; i < items.length; i++) {
                var it = items[i], score = -1;
                if (tokens.every(function (t) { return it.n.indexOf(t) !== -1; })) {
                    score = it.n.indexOf(nq) === 0 ? 3 : (it.n.indexOf(nq) !== -1 ? 2 : 1);
                } else if (dq.length >= 3 && it.d.indexOf(dq) !== -1) {
                    score = 2;
                }
                if (score > 0) scored.push([score, it]);
            }
            scored.sort(function (a, b) { return b[0] - a[0] || a[1].name.length - b[1].name.length; });
            return scored.slice(0, MAX_RESULTS).map(function (x) { return x[1]; });
        }

        var results = [], active = 0, open = false;

        function render() {
            if (!results.length) {
                list.innerHTML = '<li class="pcp-empty">لا يوجد عميل بهذا الاسم / الرقم</li>';
            } else {
                list.innerHTML = results.map(function (r, i) {
                    var sub = r.hint ? r.hint : (r.phones && r.phones.length ? r.phones.join(' · ') : '');
                    return '<li class="pcp-item' + (i === active ? ' active' : '') + (r.id === sel.value ? ' current' : '') +
                        '" data-i="' + i + '"><span class="pcp-name">' + esc(r.name) + '</span>' +
                        (sub ? '<span class="pcp-sub" dir="auto">' + esc(sub) + '</span>' : '') + '</li>';
                }).join('');
            }
            list.hidden = false;
            open = true;
            placeList();
            var a = list.querySelector('.pcp-item.active');
            if (a) a.scrollIntoView({ block: 'nearest' });
        }
        function close() { list.hidden = true; open = false; }

        function labelOf(id) {
            if (!id) return CASH_LABEL;
            if (byId[id]) return byId[id].name;
            var o = sel.querySelector('option[value="' + CSS.escape(String(id)) + '"]');
            return o ? o.text.trim() : CASH_LABEL;
        }
        function showCurrent() {
            if (document.activeElement === input) return;          // don't overwrite typing
            input.value = sel.value ? labelOf(sel.value) : '';
            box.classList.toggle('has-customer', !!sel.value);
        }

        function pick(r) {
            if (!r) return;
            if (sel.value !== r.id) {
                sel.value = r.id;
                sel.dispatchEvent(new Event('change', { bubbles: true }));   // POS handlers run as before
            }
            pushRecent(r.id);
            close();
            input.blur();
            showCurrent();
            var bi = document.getElementById('barcodeInput');
            if (bi) bi.focus();
        }

        // ---- mirror programmatic changes of the select (value / selectedIndex setters) ----
        ['value', 'selectedIndex'].forEach(function (prop) {
            var d = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, prop);
            if (!d || !d.set) return;
            Object.defineProperty(sel, prop, {
                configurable: true,
                get: function () { return d.get.call(this); },
                set: function (v) { d.set.call(this, v); setTimeout(showCurrent, 0); }
            });
        });
        sel.addEventListener('change', function () { setTimeout(showCurrent, 0); });
        sel.focus = function () { input.focus(); };      // "choose a customer" prompts land here

        // ---- input behaviour ----
        input.addEventListener('focus', function () {
            input.select();
            results = search(sel.value ? '' : input.value);
            active = 0;
            render();
        });
        input.addEventListener('input', function () {
            results = search(input.value);
            active = 0;
            render();
        });
        input.addEventListener('blur', function () {
            setTimeout(function () { close(); showCurrent(); }, 150);   // let a click land first
        });
        input.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                if (!open) { results = search(input.value); active = 0; render(); return; }
                if (!results.length) return;
                active = (active + (e.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length;
                render();
            } else if (e.key === 'Enter') {
                e.preventDefault();
                e.stopPropagation();
                if (e.ctrlKey) return;                         // Ctrl+Enter = cash payment (global)
                if (open && results[active]) pick(results[active]);
            } else if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                close();
                input.blur();
                showCurrent();
                var bi = document.getElementById('barcodeInput');
                if (bi) bi.focus();
            } else if (e.key === 'Tab' && open && results[active] && input.value.trim()) {
                pick(results[active]);
            }
        });
        list.addEventListener('mousedown', function (e) {
            var li = e.target.closest('.pcp-item');
            if (!li) return;
            e.preventDefault();
            pick(results[+li.dataset.i]);
        });
        if (clearBtn) clearBtn.addEventListener('click', function () {
            pick({ id: '', name: CASH_LABEL });
        });

        // ---- full customer list popup (click the "العميل:" label) ----
        var MODAL_MAX_ROWS = 300;
        var modal = null, mInput, mBody, mCount, mRows = [], mActive = 0;

        function buildModal() {
            modal = document.createElement('div');
            modal.className = 'pcp-modal-overlay';
            modal.hidden = true;
            modal.innerHTML =
                '<div class="pcp-modal" role="dialog" aria-modal="true" aria-label="اختيار عميل">' +
                '  <div class="pcp-modal-head">' +
                '    <h3>👤 اختيار عميل</h3>' +
                '    <button type="button" class="pcp-modal-x" title="إغلاق (Esc)">✕</button>' +
                '  </div>' +
                '  <input type="text" class="pcp-modal-search" placeholder="ابحث بالاسم أو رقم التليفون..." autocomplete="off" spellcheck="false">' +
                '  <div class="pcp-modal-count"></div>' +
                '  <div class="pcp-modal-scroll"><table class="pcp-modal-table">' +
                '    <thead><tr><th>العميل</th><th>التليفون / الموبايل</th></tr></thead><tbody></tbody>' +
                '  </table></div>' +
                '  <div class="pcp-modal-foot">↑ ↓ للتنقل · Enter للاختيار · Esc للإغلاق</div>' +
                '</div>';
            document.body.appendChild(modal);
            mInput = modal.querySelector('.pcp-modal-search');
            mBody = modal.querySelector('tbody');
            mCount = modal.querySelector('.pcp-modal-count');

            modal.addEventListener('mousedown', function (e) { if (e.target === modal) closeModal(); });
            modal.querySelector('.pcp-modal-x').addEventListener('click', closeModal);
            mInput.addEventListener('input', function () { fillModal(); });
            mBody.addEventListener('click', function (e) {
                var tr = e.target.closest('tr[data-i]');
                if (tr) pickFromModal(+tr.dataset.i);
            });
            mInput.addEventListener('keydown', function (e) {
                if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                    e.preventDefault();
                    if (!mRows.length) return;
                    mActive = (mActive + (e.key === 'ArrowDown' ? 1 : -1) + mRows.length) % mRows.length;
                    paintActive();
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    e.stopPropagation();
                    if (!e.ctrlKey && mRows[mActive]) pickFromModal(mActive);
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    e.stopPropagation();       // don't trigger the POS-wide Escape handler
                    closeModal();
                }
            });
        }

        function fillModal() {
            var q = mInput.value;
            var matches;
            if (!norm(q)) {
                matches = items.slice().sort(function (a, b) { return a.name.localeCompare(b.name, 'ar', { numeric: true }); });
            } else {
                var nq = norm(q), dq = digits(q), tokens = nq.split(' ');
                matches = items.filter(function (it) {
                    return tokens.every(function (t) { return it.n.indexOf(t) !== -1; }) ||
                        (dq.length >= 3 && it.d.indexOf(dq) !== -1);
                }).sort(function (a, b) {
                    return (b.n.indexOf(nq) === 0) - (a.n.indexOf(nq) === 0) || a.name.localeCompare(b.name, 'ar', { numeric: true });
                });
            }
            var total = matches.length;
            mRows = (norm(q) ? [] : [{ id: '', name: CASH_LABEL, phones: [], cash: true }])
                .concat(matches.slice(0, MODAL_MAX_ROWS));
            mActive = 0;
            mBody.innerHTML = mRows.length ? mRows.map(function (r, i) {
                return '<tr data-i="' + i + '" class="' + (r.id === sel.value ? 'current' : '') + (r.cash ? ' cash' : '') + '">' +
                    '<td>' + esc(r.name) + (r.id === sel.value ? ' <span class="pcp-check">✓</span>' : '') + '</td>' +
                    '<td dir="ltr">' + esc((r.phones || []).join(' · ') || (r.cash ? 'بدون عميل' : '—')) + '</td></tr>';
            }).join('') : '<tr><td colspan="2" class="pcp-modal-empty">لا يوجد عميل بهذا الاسم / الرقم</td></tr>';
            mCount.textContent = total > MODAL_MAX_ROWS
                ? 'عرض ' + MODAL_MAX_ROWS + ' من ' + total.toLocaleString('en') + ' عميل — اكتب للبحث لتضييق النتائج'
                : total.toLocaleString('en') + ' عميل';
            paintActive();
        }

        function paintActive() {
            Array.prototype.forEach.call(mBody.querySelectorAll('tr[data-i]'), function (tr) {
                var on = +tr.dataset.i === mActive;
                tr.classList.toggle('active', on);
                if (on) tr.scrollIntoView({ block: 'nearest' });
            });
        }

        function openModal() {
            if (!modal) buildModal();
            close();
            mInput.value = '';
            fillModal();
            modal.hidden = false;
            setTimeout(function () { mInput.focus(); }, 0);
        }
        function closeModal() {
            if (!modal || modal.hidden) return;
            modal.hidden = true;
            var bi = document.getElementById('barcodeInput');
            if (bi) bi.focus();
        }
        function pickFromModal(i) {
            var r = mRows[i];
            if (!r) return;
            modal.hidden = true;
            pick(r);                            // same path as the type-ahead (change event, tabs, prices)
        }

        var label = box.parentElement && box.parentElement.querySelector('.pos-field-label');
        if (label) {
            label.classList.add('pcp-open-list');
            label.setAttribute('role', 'button');
            label.setAttribute('tabindex', '0');
            label.title = 'اضغط لعرض قائمة كل العملاء والاختيار منها';
            label.addEventListener('click', openModal);
            label.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openModal(); }
            });
        }

        // ---- global shortcut ----
        document.addEventListener('keydown', function (e) {
            if (e.key === 'F6' || (e.altKey && e.code === 'KeyC')) {
                e.preventDefault();
                if (modal && !modal.hidden) return;
                input.focus();
            }
        }, true);

        showCurrent();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
