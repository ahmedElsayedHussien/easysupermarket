'use strict';

/* ============================================================
   EasySupermarket POS System
   Complete Point of Sale JavaScript
   ============================================================ */
const POS = {
    sessions: [{id: 1, name: 'فاتورة 1', cart: []}],
    activeSessionId: 1,
    nextSessionId: 2,
    discountMode: 'pct',
    // Max total discount % per line / invoice for this user (set by the
    // template from SystemSetting.max_pos_discount_percentage; 100 for admins).
    maxDiscountPercent: 100,

    get cart() {
        const session = this.sessions.find(s => s.id === this.activeSessionId);
        return session ? session.cart : [];
    },
    
    set cart(newCart) {
        const session = this.sessions.find(s => s.id === this.activeSessionId);
        if (session) {
            session.cart = newCart;
            this.saveSessions();
        }
    },

    saveSessions() {
        try {
            sessionStorage.setItem('pos_sessions', JSON.stringify(this.sessions));
            sessionStorage.setItem('pos_activeSessionId', this.activeSessionId);
            sessionStorage.setItem('pos_nextSessionId', this.nextSessionId);
            // Store today's date to detect stale data next day
            sessionStorage.setItem('pos_sessions_date', new Date().toISOString().slice(0, 10));
        } catch(e) { /* ignore */ }
    },

    restoreSessions() {
        try {
            const saved = sessionStorage.getItem('pos_sessions');
            const savedDate = sessionStorage.getItem('pos_sessions_date');
            const today = new Date().toISOString().slice(0, 10); // YYYY-MM-DD

            // Discard sessions saved on a previous day — prices may be stale
            if (saved && savedDate && savedDate !== today) {
                this.clearPOSCache();
                console.log('[POS] Session data from previous day cleared to refresh prices.');
                return;
            }

            if (saved) {
                this.sessions = JSON.parse(saved);
                if (Array.isArray(this.sessions)) {
                    this.sessions.forEach(s => {
                        if (!Array.isArray(s.cart)) s.cart = [];
                        s.cart = s.cart.filter(item => item && item.id && !isNaN(parseInt(item.id)));
                    });
                }
                this.activeSessionId = parseInt(sessionStorage.getItem('pos_activeSessionId')) || this.sessions[0]?.id || 1;
                this.nextSessionId = parseInt(sessionStorage.getItem('pos_nextSessionId')) || 2;
                if (this.sessions.length === 0) {
                    this.sessions = [{id: 1, name: 'فاتورة 1', cart: [], discountVal: 0, discountPct: 0, discountMode: 'pct', customerId: '', notes: '', applyTax: !!this.applyVatByDefault, applyWht: false}];
                    this.activeSessionId = 1;
                    this.nextSessionId = 2;
                }
                const active = this.sessions.find(s => s.id === this.activeSessionId);
                if (active) {
                    this.discountMode = active.discountMode || 'pct';
                    const valInput = document.getElementById('globalDiscountVal');
                    if (valInput && active.discountVal !== undefined) valInput.value = active.discountVal;
                    const pctInput = document.getElementById('globalDiscountPct');
                    if (pctInput && active.discountPct !== undefined) pctInput.value = active.discountPct;
                    const notesEl = document.getElementById('posInvoiceNotes');
                    if (notesEl && active.notes !== undefined) notesEl.value = active.notes;
                    const custEl = document.getElementById('posCustomer');
                    if (custEl && active.customerId !== undefined) {
                        custEl.value = active.customerId;
                        if (typeof $ !== 'undefined' && $(custEl).hasClass('select2-hidden-accessible')) {
                            $(custEl).val(active.customerId).trigger('change.select2');
                        }
                    }
                    const taxEl = document.getElementById('posApplyTax');
                    if (taxEl && active.applyTax !== undefined) taxEl.checked = !!active.applyTax;
                    const whtEl = document.getElementById('posApplyWht');
                    if (whtEl && active.applyWht !== undefined) whtEl.checked = !!active.applyWht;
                }
            }
        } catch(e) { /* ignore */ }
    },

    clearPOSCache() {
        try {
            sessionStorage.removeItem('pos_sessions');
            sessionStorage.removeItem('pos_activeSessionId');
            sessionStorage.removeItem('pos_nextSessionId');
            this.sessions = [{id: 1, name: 'فاتورة 1', cart: [], discountVal: 0, discountPct: 0, discountMode: 'pct', customerId: '', notes: '', applyTax: !!this.applyVatByDefault, applyWht: false}];
            this.activeSessionId = 1;
            this.nextSessionId = 2;
        } catch(e) { /* ignore */ }
    },
    
    // Set by templates/pos/index.html from SystemSetting
    applyVatByDefault: false,          // apply_vat_by_default
    autoPrintReceipt: false,           // auto_print_receipt
    largeAmountRequiresCustomer: 0,    // large_amount_requires_customer (0 = off)
    cashRounding: 0,                   // pos_cash_rounding step (0 = no rounding)
    receiptPaperWidth: '80mm',         // receipt_paper_width

    // Round a cash amount to the configured step (half-up), like settings_helpers.round_cash.
    // Display only: the server recomputes the rounding.
    roundCash(amount) {
        const step = parseFloat(this.cashRounding) || 0;
        const value = parseFloat(amount) || 0;
        if (step <= 0) return Math.round(value * 100) / 100;
        const units = Math.floor(value / step + 0.5 + 1e-9);
        return Math.round(units * step * 100) / 100;
    },

    // Cash due after rounding when `nonCash` is paid by other means.
    cashDueAfterRounding(total, nonCash = 0) {
        const exact = Math.max(0, (parseFloat(total) || 0) - (parseFloat(nonCash) || 0));
        return exact > 0 ? this.roundCash(exact) : 0;
    },

    // large_amount_requires_customer: block a big sale to the walk-in cash customer.
    checkLargeAmountCustomer(total) {
        const threshold = parseFloat(this.largeAmountRequiresCustomer) || 0;
        if (threshold <= 0 || this.isReturnMode) return true;
        if ((parseFloat(total) || 0) + 1e-9 < threshold) return true;
        const sel = document.getElementById('posCustomer');
        const opt = sel && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex] : null;
        const hasRealCustomer = !!(sel && sel.value) && !(opt && opt.text && opt.text.includes('عميل نقدي'));
        if (hasRealCustomer) return true;
        this.showNotification(`⚠️ إجمالي الفاتورة (${this.formatCurrency(total)}) يساوي أو يتجاوز ${this.formatCurrency(threshold)} — يجب اختيار عميل مسجل قبل إتمام البيع`, 'error');
        if (sel) { try { sel.focus(); } catch (e) { /* ignore */ } }
        return false;
    },

    // Shows "المطلوب نقداً بعد التقريب" under the totals when rounding changes the amount.
    updateCashRoundingRow(total) {
        const row = document.getElementById('cartCashRoundedRow');
        if (!row) return;
        const rounded = this.cashDueAfterRounding(total);
        const diff = Math.round((rounded - (parseFloat(total) || 0)) * 100) / 100;
        if ((parseFloat(this.cashRounding) || 0) > 0 && diff !== 0) {
            const valEl = document.getElementById('cartCashRounded');
            if (valEl) valEl.textContent = `${this.formatCurrency(rounded)} ج.م (تقريب ${diff > 0 ? '+' : ''}${this.formatCurrency(diff)})`;
            row.style.display = '';
        } else {
            row.style.display = 'none';
        }
    },

    currentWarehouseId: null,
    currentBranchId: null,
    currentShiftId: null,
    transactionNumber: '001',
    selectedProductRow: null,
    lastInvoiceData: null,
    isReturnMode: false,
    currentCustomerPrices: {},
    lastSaleCustomerId: null,
    lastSaleCustomerName: null,
    lastSaleInvoiceNumber: null,
    initialProductsHtml: null,
    isServerSearchActive: false,
    searchRequestId: 0,

    // ============================================================
    // INIT
    // ============================================================
    init() {
        // Restore consecutive customer info from sessionStorage if not set by server template
        if (!this.lastSaleCustomerId && typeof sessionStorage !== 'undefined') {
            const savedCustId = sessionStorage.getItem('pos_last_sale_customer_id');
            if (savedCustId) {
                this.lastSaleCustomerId = savedCustId;
                this.lastSaleCustomerName = sessionStorage.getItem('pos_last_sale_customer_name') || '';
                this.lastSaleInvoiceNumber = sessionStorage.getItem('pos_last_sale_invoice_num') || '';
            }
        }

        const tbodyInit = document.getElementById('productsTableBody');
        if (tbodyInit && !this.initialProductsHtml && tbodyInit.children.length > 0) {
            this.initialProductsHtml = tbodyInit.innerHTML;
        }

        this.restoreSessions();
        this.setupBarcodeScanner();
        this.setupProductSearch();
        this.setupKeyboardShortcuts();
        this.setupCartEvents();
        this.setupCashInputCalculator();
        this.updateDateTime();
        setInterval(() => this.updateDateTime(), 1000);
        this.renderTabs();
        this.updateCartDisplay();
        this.updateReceiptPaperSizeUI();
        
        const applyTaxCheckbox = document.getElementById('posApplyTax');
        if (applyTaxCheckbox) {
            applyTaxCheckbox.addEventListener('change', () => {
                const s = this.sessions.find(x => x.id === this.activeSessionId);
                if (s) { s.applyTax = applyTaxCheckbox.checked; this.saveSessions(); }
                this.updateCartDisplay();
            });
        }

        const applyWhtCheckbox = document.getElementById('posApplyWht');
        if (applyWhtCheckbox) {
            applyWhtCheckbox.addEventListener('change', () => {
                const s = this.sessions.find(x => x.id === this.activeSessionId);
                if (s) { s.applyWht = applyWhtCheckbox.checked; this.saveSessions(); }
                this.updateCartDisplay();
            });
        }

        const notesInput = document.getElementById('posInvoiceNotes');
        if (notesInput) {
            notesInput.addEventListener('input', () => {
                const s = this.sessions.find(x => x.id === this.activeSessionId);
                if (s) { s.notes = notesInput.value; this.saveSessions(); }
            });
        }
        
        const posCustomerSelect = document.getElementById('posCustomer');
        if (posCustomerSelect) {
            const handleCustomerChange = async () => {
                const s = this.sessions.find(x => x.id === this.activeSessionId);
                const custId = posCustomerSelect.value;
                if (s) { s.customerId = custId; this.saveSessions(); }
                const whtCheckbox = document.getElementById('posApplyWht');
                const opt = posCustomerSelect.options[posCustomerSelect.selectedIndex];
                if (whtCheckbox && opt) {
                    whtCheckbox.checked = (opt.dataset.whtSubject === 'true');
                    if (s) { s.applyWht = whtCheckbox.checked; this.saveSessions(); }
                }
                await this.applyCustomerPrices(custId);
                this.updateCartDisplay();
                this.checkConsecutiveCustomerWarning(true);
            };
            
            posCustomerSelect.addEventListener('change', handleCustomerChange);
            if (typeof $ !== 'undefined') {
                $(posCustomerSelect).on('change select2:select select2:clear', handleCustomerChange);
            }

            const customerSearchInput = document.getElementById('posCustomerSearch');
            if (customerSearchInput) {
                customerSearchInput.addEventListener('input', () => {
                    const term = customerSearchInput.value.trim().toLowerCase();
                    Array.from(posCustomerSelect.options).forEach((opt, idx) => {
                        if (idx === 0 || opt.selected || !term) {
                            opt.hidden = false;
                            return;
                        }
                        opt.hidden = !opt.textContent.toLowerCase().includes(term);
                    });
                });
                customerSearchInput.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        const match = Array.from(posCustomerSelect.options).find((opt, idx) => idx > 0 && !opt.hidden);
                        if (match) {
                            posCustomerSelect.value = match.value;
                            posCustomerSelect.dispatchEvent(new Event('change'));
                        }
                    }
                });
            }
        }
        
        this.setupPersistedHeaders();

        // Sync active session's fields to UI
        const active = this.sessions.find(s => s.id === this.activeSessionId);
        if (active) {
            if (applyTaxCheckbox && active.applyTax !== undefined) applyTaxCheckbox.checked = !!active.applyTax;
            if (applyWhtCheckbox && active.applyWht !== undefined) applyWhtCheckbox.checked = !!active.applyWht;
            if (notesInput && active.notes !== undefined) notesInput.value = active.notes || '';
            if (posCustomerSelect && active.customerId !== undefined) {
                posCustomerSelect.value = active.customerId || '';
                if (typeof $ !== 'undefined' && $(posCustomerSelect).hasClass('select2-hidden-accessible')) {
                    $(posCustomerSelect).val(active.customerId || '').trigger('change.select2');
                }
            }
        }

        const initialCustId = active?.customerId || (posCustomerSelect ? posCustomerSelect.value : '');
        if (initialCustId) {
            this.applyCustomerPrices(initialCustId);
        }

        this.checkConsecutiveCustomerWarning(false);

        window.addEventListener('pageshow', (event) => {
            if (event.persisted) {
                window.location.reload();
            }
        });

        console.log('[POS] Initialized. Warehouse:', this.currentWarehouseId, 'Branch:', this.currentBranchId);
    },

    // ============================================================
    // PERSISTED HEADERS (Branch, Warehouse, Bank, Safe, Wallet)
    // ============================================================
    setupPersistedHeaders() {
        const urlParams = new URLSearchParams(window.location.search);
        const userSuffix = `_${this.currentUserId || 'default'}`;
        
        // Reset saved shift state if a new shift was opened
        if (this.currentShiftId || urlParams.has('new_shift')) {
            const lastShiftKey = `pos_active_shift_id${userSuffix}`;
            const lastShiftId = localStorage.getItem(lastShiftKey);
            const isNewShift = urlParams.has('new_shift') || (this.currentShiftId && String(this.currentShiftId) !== lastShiftId);
            
            if (isNewShift) {
                if (this.currentShiftId) {
                    localStorage.setItem(lastShiftKey, String(this.currentShiftId));
                }
                localStorage.removeItem(`pos_saved_customer${userSuffix}`);
            }
        }
        
        // 1. Branch & Warehouse Check
        const branchSelect = document.getElementById('posBranchSelect');
        const warehouseSelect = document.getElementById('posWarehouseSelect');
        
        if (branchSelect && warehouseSelect) {
            // The BRANCH comes from the header (server session) — never from localStorage,
            // otherwise the POS would silently sell from a different branch than the header.
            localStorage.removeItem(`pos_saved_branch${userSuffix}`);
            // Only the WAREHOUSE is remembered, and only if it belongs to the current branch.
            const savedWarehouse = localStorage.getItem(`pos_saved_warehouse${userSuffix}`);
            const warehouseInBranch = savedWarehouse &&
                Array.from(warehouseSelect.options).some(o => o.value === savedWarehouse);
            if (!urlParams.has('warehouse_id') && warehouseInBranch && warehouseSelect.value !== savedWarehouse) {
                window.location.href = '?warehouse_id=' + encodeURIComponent(savedWarehouse);
                return; // let the redirect happen
            }
            if (warehouseSelect.value) {
                localStorage.setItem(`pos_saved_warehouse${userSuffix}`, warehouseSelect.value);
            }
            warehouseSelect.addEventListener('change', function() {
                localStorage.setItem(`pos_saved_warehouse${userSuffix}`, this.value);
            });
        }

        // 2. Treasury, Bank, Ewallet
        const selectsToPersist = [
            { id: 'posTreasury', key: `pos_saved_treasury${userSuffix}` },
            { id: 'posVisaBank', key: `pos_saved_visa_bank${userSuffix}` },
            { id: 'posInstaBank', key: `pos_saved_insta_bank${userSuffix}` },
            { id: 'posBank', key: `pos_saved_bank${userSuffix}` },
            { id: 'posEwallet', key: `pos_saved_ewallet${userSuffix}` }
        ];

        selectsToPersist.forEach(item => {
            const selectEl = document.getElementById(item.id);
            // Locked payment accounts (lock_pos_payment_accounts): keep the branch default.
            if (selectEl && selectEl.disabled) return;
            if (selectEl) {
                // Restore saved value
                const savedVal = localStorage.getItem(item.key);
                if (savedVal && savedVal !== "" && Array.from(selectEl.options).some(opt => opt.value === savedVal)) {
                    selectEl.value = savedVal;
                    if (item.isSelect2 && typeof $ !== 'undefined') {
                        $(selectEl).trigger('change');
                    } else {
                        selectEl.dispatchEvent(new Event('change'));
                    }
                } else if ((!selectEl.value || selectEl.value === "") && selectEl.options.length > 1) {
                    // Fallback to Django pre-selected option or first real option
                    const selectedOpt = Array.from(selectEl.options).find(opt => opt.hasAttribute('selected') && opt.value);
                    if (selectedOpt) {
                        selectEl.value = selectedOpt.value;
                    } else if (selectEl.options[1] && selectEl.options[1].value) {
                        selectEl.value = selectEl.options[1].value;
                    }
                }

                // Listen for changes and save
                if (item.isSelect2 && typeof $ !== 'undefined') {
                    $(selectEl).on('change', function() {
                        localStorage.setItem(item.key, this.value);
                    });
                } else {
                    selectEl.addEventListener('change', function() {
                        localStorage.setItem(item.key, this.value);
                    });
                }
            }
        });
    },

    // ============================================================
    // DATE/TIME (POS navbar display)
    // ============================================================
    updateDateTime() {
        const now = new Date();
        const el = document.getElementById('posDateTime');
        const cartEl = document.getElementById('cartDate');
        if (!el && !cartEl) return;

        const pad = (n) => String(n).padStart(2, '0');
        const d = pad(now.getDate());
        const m = pad(now.getMonth() + 1);
        const y = now.getFullYear();
        const hh = pad(now.getHours());
        const mm = pad(now.getMinutes());
        const ss = pad(now.getSeconds());

        const dateStr = `${d}/${m}/${y}`;
        const timeStr = `${hh}:${mm}:${ss}`;

        if (el) el.textContent = `${dateStr}  ${timeStr}`;
        if (cartEl) cartEl.textContent = dateStr;
    },

    // ============================================================
    // BARCODE SCANNER
    // ============================================================
    setupBarcodeScanner() {
        const barcodeInput = document.getElementById('barcodeInput');
        if (!barcodeInput) return;

        // Auto-focus on barcode field at start
        barcodeInput.focus();

        // Re-focus when clicking away (but not on other inputs)
        document.addEventListener('click', (e) => {
            const tag = e.target.tagName.toLowerCase();
            const isInput = ['input', 'textarea', 'select', 'button'].includes(tag);
            if (!isInput) barcodeInput.focus();
        });

        barcodeInput.addEventListener('keypress', async (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                const barcode = barcodeInput.value.trim();
                if (barcode) {
                    await this.addProductByBarcode(barcode);
                    barcodeInput.value = '';
                    barcodeInput.focus();
                }
            }
        });
    },

    async addProductByBarcode(barcode) {
        try {
            const custId = (this.sessions.find(x => x.id === this.activeSessionId)?.customerId) || document.getElementById('posCustomer')?.value || '';
            const resp = await fetch(`/invoicing/api/product/barcode/?barcode=${encodeURIComponent(barcode)}&warehouse_id=${this.currentWarehouseId || ''}&partner_id=${encodeURIComponent(custId)}`);
            if (!resp.ok) throw new Error('Network error');
            const data = await resp.json();
            if (data.found) {
                this.addToCart(data.product, 1, data.product.selected_uom_id);
                const badge = data.product.pricing_badge_text ? ` [${data.product.pricing_badge_text}]` : '';
                this.showNotification(`✓ تمت الإضافة: ${data.product.name}${badge}`, 'success');
            } else {
                this.showNotification('❌ صنف غير موجود: ' + barcode, 'error');
            }
        } catch (err) {
            // If API not available, try to find in the product table (offline)
            let row = document.querySelector(`[data-product-barcode="${barcode}"]`);
            let matchedUomId = null;
            if (!row) {
                const allRows = document.querySelectorAll('#productsTableBody .product-row');
                for (const r of allRows) {
                    try {
                        const uoms = JSON.parse(r.dataset.uoms || '[]');
                        const foundUom = uoms.find(u => u.barcode && String(u.barcode) === barcode);
                        if (foundUom) {
                            row = r;
                            matchedUomId = foundUom.id;
                            break;
                        }
                    } catch(e) {}
                }
            }
            if (row) {
                const product = this.extractProductFromRow(row);
                this.addToCart(product, 1, matchedUomId);
                this.highlightRow(row);
                this.showNotification(`✓ تمت الإضافة: ${product.name}`, 'success');
            } else {
                this.showNotification('⚠ صنف غير موجود أو تعذر الاتصال', 'error');
            }
        }
    },

    applyCurrentCustomerPricesToTable() {
        if (!this.currentCustomerPrices || Object.keys(this.currentCustomerPrices).length === 0) return;
        const rows = document.querySelectorAll('#productsTableBody .product-row');
        rows.forEach(row => {
            const pid = String(row.dataset.productId);
            if (!row.dataset.originalSalePrice) {
                row.dataset.originalSalePrice = row.dataset.salePrice;
            }
            const origPrice = parseFloat(row.dataset.originalSalePrice) || 0;
            const priceCol = row.querySelector('.price-col');

            if (this.currentCustomerPrices[pid]) {
                const pInfo = this.currentCustomerPrices[pid];
                row.dataset.salePrice = String(pInfo.price);
                if (priceCol) {
                    priceCol.innerHTML = `${parseFloat(pInfo.price).toFixed(2)} ج.م <span class="customer-price-badge" style="font-size:0.65rem; background:rgba(0,229,255,0.15); color:${pInfo.badge_color || '#00e5ff'}; border:1px solid ${pInfo.badge_color || '#00e5ff'}; padding:1px 4px; border-radius:3px; margin-right:2px; font-weight:700;">${pInfo.badge_text || ''}</span>`;
                }
            } else {
                row.dataset.salePrice = String(origPrice);
                if (priceCol) {
                    priceCol.textContent = `${origPrice.toFixed(2)} ج.م`;
                }
            }
        });
    },

    /**
     * Quantity-break / unit-specific customer pricing (mirrors server-side
     * PricingEngineService): CUSTOMER tiers first, then SECTOR; within a tier prefer
     * an item priced for the selected unit, else a base-unit item scaled by the unit
     * factor; pick the highest min_quantity <= quantity. The server recalculates
     * the final price on checkout — this only keeps the cart display in sync.
     */
    applyTierPrice(item) {
        const tiers = this.currentCustomerTiers && this.currentCustomerTiers[String(item.id)];
        if (!tiers || !tiers.length || item.is_open_price || item._manualPrice) return;
        const selUom = item.uoms && item.uoms.find(u => String(u.id) === String(item.uom_id));
        const factor = selUom ? parseFloat(selUom.factor) || 1.0 : 1.0;
        const unitId = selUom && selUom.unit_id ? String(selUom.unit_id) : null;
        const qty = parseFloat(item.quantity) || 0;
        const pick = (list) => list
            .filter(t => t.min_quantity <= qty)
            .sort((a, b) => b.min_quantity - a.min_quantity)[0];
        for (const tierName of ['CUSTOMER', 'SECTOR']) {
            const inTier = tiers.filter(t => t.tier === tierName);
            if (!inTier.length) continue;
            const unitMatch = unitId ? pick(inTier.filter(t => t.uom_id !== null && String(t.uom_id) === unitId)) : null;
            if (unitMatch) { item.price = parseFloat(unitMatch.price); return; }
            const baseMatch = pick(inTier.filter(t => t.uom_id === null));
            if (baseMatch) { item.price = parseFloat(baseMatch.price) * factor; return; }
        }
    },

    async applyCustomerPrices(customerId) {
        this.currentCustomerPrices = {};
        this.currentCustomerTiers = {};
        const branchId = this.currentBranchId || '';

        if (customerId) {
            try {
                const resp = await fetch(`/pricing/api/customer-prices/?partner_id=${encodeURIComponent(customerId)}&branch_id=${encodeURIComponent(branchId)}`);
                if (resp.ok) {
                    const data = await resp.json();
                    if (data.success && data.prices) {
                        this.currentCustomerPrices = data.prices;
                    }
                    this.currentCustomerTiers = (data.success && data.tiers) ? data.tiers : {};
                }
            } catch(e) {
                console.warn('[POS] Failed to fetch customer prices:', e);
            }
        }

        // 1. Update Products Table rows with customer pricing
        this.applyCurrentCustomerPricesToTable();

        // 2. Update Cart items in the active session
        if (this.cart && this.cart.length > 0) {
            let cartUpdated = false;
            this.cart.forEach(item => {
                const pid = String(item.id);
                if (item.original_base_price === undefined) {
                    item.original_base_price = item.base_price;
                }

                const selUom = item.uoms && item.uoms.find(u => String(u.id) === String(item.uom_id));
                const uomFactor = selUom ? parseFloat(selUom.factor) || 1.0 : 1.0;

                if (this.currentCustomerPrices[pid]) {
                    const pInfo = this.currentCustomerPrices[pid];
                    item.base_price = parseFloat(pInfo.price);
                    item.price = item.base_price * uomFactor;
                    item.pricing_badge_text = pInfo.badge_text;
                    item.pricing_badge_color = pInfo.badge_color;
                    cartUpdated = true;
                } else if (item.original_base_price !== undefined && item.base_price !== item.original_base_price) {
                    item.base_price = item.original_base_price;
                    item.price = item.base_price * uomFactor;
                    delete item.pricing_badge_text;
                    delete item.pricing_badge_color;
                    cartUpdated = true;
                }
            });

            if (cartUpdated) {
                this.saveSessions();
                this.updateCartDisplay();
                this.showNotification('✓ تم تحديث أسعار الأصناف في السلة تلقائياً وفقاً للعميل المختار', 'info');
            }
        }
    },

    extractProductFromRow(row) {
        if (!row || !row.dataset) return null;
        const cleanNumber = (val) => String(val || '0').replace(/,/g, '');
        const pid = parseInt(row.dataset.productId);
        if (isNaN(pid)) {
            console.error('[POS] Invalid product ID on row:', row);
            return null;
        }
        const pidStr = String(pid);
        const customPriceObj = (this.currentCustomerPrices && this.currentCustomerPrices[pidStr]) ? this.currentCustomerPrices[pidStr] : null;
        const effectiveSalePrice = customPriceObj ? String(customPriceObj.price) : cleanNumber(row.dataset.salePrice);
        return {
            id: pid,
            name: row.dataset.productName,
            product_type: row.dataset.productType || 'PRODUCT',
            is_open_price: row.dataset.isOpenPrice === 'true',
            barcode: (row.dataset.productBarcode === 'None' || !row.dataset.productBarcode) ? '' : row.dataset.productBarcode,
            sale_price: effectiveSalePrice,
            original_sale_price: cleanNumber(row.dataset.originalSalePrice || row.dataset.salePrice),
            pricing_badge_text: customPriceObj ? customPriceObj.badge_text : '',
            pricing_badge_color: customPriceObj ? customPriceObj.badge_color : '',
            last_purchase_price: cleanNumber(row.dataset.lastPurchase),
            last_sale_price: cleanNumber(row.dataset.lastSale),
            available_stock: parseFloat(cleanNumber(row.dataset.availableStock)) || 0,
            allow_negative_stock: row.dataset.allowNegativeStock === 'true',
            tax_rate: parseFloat(cleanNumber(row.dataset.taxRate)) || 0,
            wht_rate: parseFloat(cleanNumber(row.dataset.whtRate)) || 0,
            avg_cost: parseFloat(cleanNumber(row.dataset.avgCost)) || 0,
            uoms: (() => {
                try {
                    return JSON.parse(row.dataset.uoms || '[]');
                } catch(e) {
                    return [{ id: 'base', name: 'قطعة', factor: 1.0 }];
                }
            })()
        };
    },

    highlightRow(row) {
        if (this.selectedProductRow) {
            this.selectedProductRow.classList.remove('selected');
        }
        row.classList.add('selected');
        this.selectedProductRow = row;
        row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    },

    // ============================================================
    // PRODUCT SEARCH (High Performance & Fully Responsive)
    // ============================================================
    setupProductSearch() {
        const searchInput = document.getElementById('productSearch');
        if (!searchInput) return;

        // Cache initial products table HTML on setup
        const tbody = document.getElementById('productsTableBody');
        if (tbody && !this.initialProductsHtml && tbody.children.length > 0) {
            this.initialProductsHtml = tbody.innerHTML;
        }

        let debounceTimer = null;
        const triggerFilter = (immediate = false) => {
            const rawVal = searchInput.value || '';
            const clearBtn = document.getElementById('clearSearchBtn');
            if (clearBtn) {
                clearBtn.style.display = rawVal.trim() ? 'block' : 'none';
            }

            // If empty, trigger immediately without debounce for instant response
            if (!rawVal.trim()) {
                if (debounceTimer) clearTimeout(debounceTimer);
                this.filterProducts('');
                return;
            }

            if (immediate) {
                if (debounceTimer) clearTimeout(debounceTimer);
                this.filterProducts(rawVal);
            } else {
                if (debounceTimer) clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => {
                    this.filterProducts(searchInput.value || '');
                }, 120);
            }
        };

        // Input event (fires on typing, backspace, delete, cut, speech, virtual keyboard)
        searchInput.addEventListener('input', () => triggerFilter(false));

        // Keyup event (instant response for Backspace & Delete when field becomes empty)
        searchInput.addEventListener('keyup', (e) => {
            if (e.key === 'Backspace' || e.key === 'Delete') {
                if (!searchInput.value || !searchInput.value.trim()) {
                    triggerFilter(true);
                }
            }
        });

        // Keydown event (instant Escape to clear)
        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                e.preventDefault();
                this.clearProductSearch();
            }
        });

        // Search event (for HTML5 search clear 'x' button)
        searchInput.addEventListener('search', () => triggerFilter(true));

        // Paste event
        searchInput.addEventListener('paste', () => {
            setTimeout(() => triggerFilter(true), 10);
        });

        // Change event
        searchInput.addEventListener('change', () => triggerFilter(true));
    },

    clearProductSearch() {
        const searchInput = document.getElementById('productSearch');
        if (searchInput) {
            searchInput.value = '';
            const clearBtn = document.getElementById('clearSearchBtn');
            if (clearBtn) clearBtn.style.display = 'none';
            this.filterProducts('');
            searchInput.focus();
        }
    },

    filterProducts(query) {
        const q = (query || '').toLowerCase().trim();
        const tbody = document.getElementById('productsTableBody');
        if (!tbody) return;

        // Ensure initialProductsHtml is captured
        if (!this.initialProductsHtml && tbody.children.length > 0) {
            this.initialProductsHtml = tbody.innerHTML;
        }

        // Increment request ID to cancel/invalidate any in-flight asynchronous search responses
        const currentReqId = ++this.searchRequestId;

        // Update clear button visibility
        const clearBtn = document.getElementById('clearSearchBtn');
        if (clearBtn) {
            clearBtn.style.display = q ? 'block' : 'none';
        }

        // 1. If query is empty: RESTORE ALL PRODUCTS IMMEDIATELY
        if (!q) {
            if (this.initialProductsHtml) {
                tbody.innerHTML = this.initialProductsHtml;
                this.updateGridStockDisplay();
                this.applyCurrentCustomerPricesToTable();
                this.isServerSearchActive = false;
            } else {
                this.loadProducts();
            }
            const rows = tbody.querySelectorAll('.product-row');
            rows.forEach(r => r.style.display = '');
            const emptyMsg = document.getElementById('productsEmptyMsg');
            if (emptyMsg) emptyMsg.style.display = rows.length === 0 ? '' : 'none';
            return;
        }

        // 2. If server search was active and query is now short (1 char), restore full table first
        if (this.isServerSearchActive && this.initialProductsHtml && q.length < 2) {
            tbody.innerHTML = this.initialProductsHtml;
            this.updateGridStockDisplay();
            this.applyCurrentCustomerPricesToTable();
            this.isServerSearchActive = false;
        }

        // 3. Client-side instant filter on current rows
        let rows = tbody.querySelectorAll('.product-row');
        let visibleCount = 0;

        rows.forEach(row => {
            const name = (row.dataset.productName || '').toLowerCase();
            const barcode = (row.dataset.productBarcode || '').toLowerCase();
            const matches = name.includes(q) || barcode.includes(q);
            row.style.display = matches ? '' : 'none';
            if (matches) visibleCount++;
        });

        // 4. Server-side dynamic search (when q.length >= 2)
        if (q.length >= 2) {
            const params = new URLSearchParams({
                q: q,
                warehouse_id: this.currentWarehouseId || '',
                branch_id: this.currentBranchId || ''
            });

            fetch(`/inventory/api/products/?${params}`)
                .then(r => r.json())
                .then(data => {
                    // Check if a newer request was dispatched or if the query was cleared/changed!
                    if (currentReqId !== this.searchRequestId) {
                        return; // Discard stale response
                    }
                    const searchInput = document.getElementById('productSearch');
                    const currentVal = (searchInput?.value || '').toLowerCase().trim();
                    if (currentVal !== q) {
                        return; // Discard because user already changed/cleared input
                    }

                    if (data.products && data.products.length > 0) {
                        this.isServerSearchActive = true;
                        this.renderProductsTable(data.products);
                        this.applyCurrentCustomerPricesToTable();
                        const emptyMsg = document.getElementById('productsEmptyMsg');
                        if (emptyMsg) emptyMsg.style.display = 'none';
                    } else if (visibleCount === 0) {
                        const emptyMsg = document.getElementById('productsEmptyMsg');
                        if (emptyMsg) emptyMsg.style.display = '';
                    }
                })
                .catch(() => {});
        } else {
            const emptyMsg = document.getElementById('productsEmptyMsg');
            if (emptyMsg) emptyMsg.style.display = visibleCount === 0 ? '' : 'none';
        }
    },

    // ============================================================
    // CART MANAGEMENT
    // ============================================================
    addToCart(product, quantity = 1, forceUomId = null) {
        if (!product || !product.id || isNaN(parseInt(product.id))) {
            this.showNotification('بيانات الصنف غير صحيحة أو غير متوفرة', 'error');
            return;
        }

        if (!this.currentWarehouseId) {
            this.showNotification('لا يمكن إضافة أصناف لعدم وجود مستودع نشط مرتبط بهذا الفرع — اختر فرعاً آخر أو أضف مستودعاً أولاً.', 'error');
            return;
        }

        // SERIALIZED: show serial picker instead of direct add
        if (product.product_type === 'SERIALIZED') {
            this.showSerialPickerModal(product);
            return;
        }

        // Validate stock
        const stock = parseFloat(product.available_stock) || 0;
        const isService = product.product_type === 'SERVICE';
        // Master Lock: allowed if service OR global setting OR product-level allow_negative_stock
        const canNegative = isService || (POS.allowNegativeStock === true) || (product.allow_negative_stock === true);

        const targetUomId = (forceUomId !== null && forceUomId !== undefined) ? forceUomId : (product.selected_uom_id !== undefined ? product.selected_uom_id : (product.uom_id || 'base'));
        const cartKey = product._uniqueKey || `${product.id}_${targetUomId}`;

        // Find existing matching item (checking unique key or product ID + UoM ID)
        const existing = this.cart.find(item => 
            item._uniqueKey 
                ? item._uniqueKey === product._uniqueKey 
                : (item._cartKey ? item._cartKey === cartKey : (item.id === parseInt(product.id) && String(item.uom_id) === String(targetUomId)))
        );

        const addQty = parseFloat(quantity) || 1;

        if (existing) {
            const newQty = existing.quantity + addQty;
            // Get the UoM factor for the currently selected unit (to compare base quantities)
            const selUom = existing.uoms && existing.uoms.find(u => String(u.id) === String(existing.uom_id));
            const uomFactor = selUom ? parseFloat(selUom.factor) || 1.0 : 1.0;
            const baseQtyNeeded = newQty * uomFactor;
            if (!canNegative && stock > 0 && baseQtyNeeded > stock) {
                this.showNotification(`⚠ الكمية المطلوبة (${newQty} ${selUom ? selUom.name : ''}) تتجاوز المخزون المتاح (${stock})`, 'warning');
                return;
            }
            if (!canNegative && stock <= 0) {
                this.showNotification(`⚠ الصنف "${product.name}" غير متاح في المخزون`, 'warning');
                return;
            }
            existing.quantity = newQty;
        } else {
            if (!canNegative && stock <= 0) {
                this.showNotification(`⚠ الصنف "${product.name}" غير متاح في المخزون`, 'warning');
                return;
            }
            const uomList = product.uoms || [{id:'base', name:'Unit', factor:1.0}];
            const selUom = uomList.find(u => String(u.id) === String(targetUomId));
            const uomFactor = selUom ? parseFloat(selUom.factor) || 1.0 : 1.0;
            const baseQtyNeeded = addQty * uomFactor;
            if (!canNegative && stock > 0 && baseQtyNeeded > stock) {
                this.showNotification(`⚠ الكمية المطلوبة (${addQty}) تتجاوز المخزون المتاح (${stock})`, 'warning');
                return;
            }
            const pidStr = String(product.id);
            let basePrice = parseFloat(product.sale_price) || 0;
            if (this.currentCustomerPrices && this.currentCustomerPrices[pidStr]) {
                basePrice = parseFloat(this.currentCustomerPrices[pidStr].price);
            }
            const origBasePrice = parseFloat(product.original_sale_price) || parseFloat(product.sale_price) || basePrice;
            const badgeText = product.pricing_badge_text || (this.currentCustomerPrices?.[pidStr]?.badge_text || '');
            const badgeColor = product.pricing_badge_color || (this.currentCustomerPrices?.[pidStr]?.badge_color || '');
            const baseCost = parseFloat(product.avg_cost) || basePrice || 0;
            this.cart.push({
                id: parseInt(product.id),
                _cartKey: cartKey,
                _uniqueKey: product._uniqueKey || null,
                name: product.name,
                product_type: product.product_type || 'PRODUCT',
                barcode: product.barcode || '',
                price: basePrice * uomFactor,
                base_price: basePrice,
                original_base_price: origBasePrice,
                pricing_badge_text: badgeText,
                pricing_badge_color: badgeColor,
                quantity: addQty,
                discount: 0,
                tax_rate: parseFloat(product.tax_rate) || 0,
                wht_rate: parseFloat(product.wht_rate) || 0,
                available_stock: stock,
                allow_negative_stock: product.allow_negative_stock || false,
                uoms: uomList,
                uom_id: targetUomId,
                base_cost: baseCost,
                current_cost: baseCost * uomFactor
            });
        }

        this.saveSessions();
        this.updateCartDisplay();
        this.updateSalePriceDisplay(product);
    },

    findCartItem(keyOrId) {
        if (keyOrId === null || keyOrId === undefined) return null;
        const strKey = String(keyOrId);
        return this.cart.find(i => (i._cartKey && String(i._cartKey) === strKey) || (i._uniqueKey && String(i._uniqueKey) === strKey) || i.id === parseInt(keyOrId));
    },

    removeFromCart(keyOrId) {
        const strKey = String(keyOrId);
        this.cart = this.cart.filter(item => (item._cartKey ? String(item._cartKey) !== strKey : item.id !== parseInt(keyOrId)));
        this.saveSessions();
        this.updateCartDisplay();
        this.clearSalePriceDisplay();
    },

    updateQuantity(keyOrId, newQty) {
        const item = this.findCartItem(keyOrId);
        if (!item) return;

        const qty = parseFloat(newQty);
        if (isNaN(qty) || qty <= 0) {
            if (confirm(`حذف "${item.name}" من السلة؟`)) {
                this.removeFromCart(keyOrId);
            }
            return;
        }
        // Master Lock: If POS.allowNegativeStock is false, negative stock is strictly forbidden for all physical products
        const canNegative = item.product_type === 'SERVICE' || (POS.allowNegativeStock === true);
        // Account for UoM factor: compare base quantity vs available stock (always in base units)
        const selUom = item.uoms && item.uoms.find(u => String(u.id) === String(item.uom_id));
        const uomFactor = selUom ? parseFloat(selUom.factor) || 1.0 : 1.0;
        if (!canNegative && (qty * uomFactor) > item.available_stock) {
            this.showNotification(`⚠ الحد الأقصى للمخزون: ${(item.available_stock / uomFactor).toFixed(2)} ${selUom ? selUom.name : ''}`, 'warning');
            return;
        }
        item.quantity = qty;
        this.saveSessions();
        this.updateCartDisplay();
    },

    updateQuantityForProduct(event, productId, delta) {
        if (event && event.stopPropagation) {
            event.stopPropagation();
        }
        const item = this.cart.find(i => i.id === parseInt(productId));
        if (!item) {
            // Not in cart yet — find in table and add
            const row = document.querySelector(`[data-product-id="${productId}"]`);
            if (row && delta > 0) {
                const product = this.extractProductFromRow(row);
                this.addToCart(product, 1);
            }
            return;
        }
        const newQty = item.quantity + delta;
        const itemKey = item._cartKey || productId;
        if (newQty <= 0) {
            this.removeFromCart(itemKey);
        } else {
            this.updateQuantity(itemKey, newQty);
        }
    },

    setDiscount(keyOrId, discountPct) {
        const item = this.findCartItem(keyOrId);
        if (item) {
            let pct = Math.max(0, Math.min(100, parseFloat(discountPct) || 0));
            const allowed = this.getAllowedLineDiscount(this.getGlobalDiscountPct());
            if (pct > allowed + 0.0001) {
                pct = allowed;
                this.notifyDiscountCap();
            }
            item.discount = pct;
            this.updateCartDisplay();
        }
    },

    // --- Discount cap (SystemSetting.max_pos_discount_percentage) ---
    // The cap applies to the TOTAL effective discount of each line
    // (line discount combined with the invoice discount), exactly as the
    // server validates it.
    getDiscountCap() {
        const cap = parseFloat(this.maxDiscountPercent);
        if (isNaN(cap)) return 100;
        return Math.max(0, Math.min(100, cap));
    },

    getAllowedLineDiscount(globalPct) {
        const cap = this.getDiscountCap();
        if (cap >= 100) return 100;
        const g = Math.max(0, Math.min(100, parseFloat(globalPct) || 0)) / 100;
        if (g >= 1) return 0;
        return Math.max(0, (1 - (1 - cap / 100) / (1 - g)) * 100);
    },

    getAllowedGlobalDiscount() {
        const cap = this.getDiscountCap();
        if (cap >= 100) return 100;
        const maxLine = (this.cart || []).reduce((m, i) => Math.max(m, parseFloat(i.discount) || 0), 0) / 100;
        if (maxLine >= 1) return 0;
        return Math.max(0, Math.min(cap, (1 - (1 - cap / 100) / (1 - maxLine)) * 100));
    },

    notifyDiscountCap() {
        const cap = this.getDiscountCap();
        const now = Date.now();
        if (this._lastCapNotice && now - this._lastCapNotice < 1500) return;
        this._lastCapNotice = now;
        this.showNotification(`⚠ الحد الأقصى المسموح للخصم هو ${cap}% — تم ضبط الخصم تلقائياً`, 'warning');
    },

    updateUom(keyOrId, uomId) {
        const item = this.findCartItem(keyOrId);
        if (item) {
            const newCartKey = item._uniqueKey || `${item.id}_${uomId}`;
            const existingOther = this.cart.find(i => i !== item && ((i._cartKey && String(i._cartKey) === String(newCartKey)) || (i.id === item.id && String(i.uom_id) === String(uomId))));
            if (existingOther) {
                existingOther.quantity += item.quantity;
                this.cart = this.cart.filter(i => i !== item);
            } else {
                item.uom_id = uomId;
                item._cartKey = newCartKey;
                const selectedUom = item.uoms.find(u => String(u.id) === String(uomId));
                const factor = selectedUom ? parseFloat(selectedUom.factor) || 1.0 : 1.0;
                item.price = item.base_price * factor;
                item.current_cost = (item.base_cost || 0) * factor;
            }
            this.saveSessions();
            this.updateCartDisplay();
            // Update cost display in side panel if this product is selected
            const costEl = document.getElementById('displayCost');
            if (costEl) costEl.textContent = (item.current_cost || 0).toFixed(2);
        }
    },

    handlePriceInput(keyOrId, inputEl) {
        const item = this.findCartItem(keyOrId);
        if (!item) return;

        const val = parseFloat(inputEl.value);
        if (!isNaN(val) && val >= 0) {
            item.price = val;
        } else {
            item.price = 0;
        }
        item._manualPrice = true;  // don't let quantity-break pricing overwrite a typed price

        // Update line total element in DOM directly without re-rendering cart
        const cartItemEl = inputEl.closest('.cart-item');
        if (cartItemEl) {
            const lineTotalEl = cartItemEl.querySelector('.cart-item-total');
            if (lineTotalEl) {
                const applyTaxCheckbox = document.getElementById('posApplyTax');
                const applyTax = applyTaxCheckbox ? applyTaxCheckbox.checked : false;

                const applyWhtCheckbox = document.getElementById('posApplyWht');
                const applyWht = applyWhtCheckbox ? applyWhtCheckbox.checked : false;

                const globalDiscountPct = this.getGlobalDiscountPct();

                const lineGross = item.price * item.quantity;
                const itemDiscount = lineGross * (item.discount / 100);
                let lineNet = lineGross - itemDiscount;
                const lineGlobalDiscount = lineNet * (globalDiscountPct / 100);
                lineNet = lineNet - lineGlobalDiscount;

                const effectiveTaxRate = applyTax ? item.tax_rate : 0;
                const effectiveWhtRate = applyWht ? item.wht_rate : 0;
                const lineTotal = lineNet + (lineNet * (effectiveTaxRate / 100)) - (lineNet * (effectiveWhtRate / 100));

                lineTotalEl.textContent = this.formatCurrency(lineTotal);
            }
        }

        // Update overall invoice totals in DOM directly
        const totals = this.calculateTotals();
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };

        set('cartSubtotal', this.formatCurrency(totals.subtotal));
        set('cartDiscount', '(' + this.formatCurrency(totals.discount) + ')');
        set('cartTax', this.formatCurrency(totals.tax));
        set('cartWht', '(' + this.formatCurrency(totals.wht) + ')');
        set('cartTotal', this.formatCurrency(totals.total) + ' ج.م');
        set('modalTotal', this.formatCurrency(this.cashDueAfterRounding(totals.total)) + ' ج.م');
        this.updateCashRoundingRow(totals.total);
    },

    updateManualPrice(keyOrId, newPrice) {
        const item = this.findCartItem(keyOrId);
        if (!item) return;

        const isEditable = POS.allowPriceEdit || item.product_type === 'SERVICE';
        if (!isEditable) {
            alert('تعديل الأسعار غير مسموح به! يرجى تفعيله من الإعدادات أولاً.');
            this.updateCartDisplay();
            return;
        }
        
        if (item) {
            const price = parseFloat(newPrice);
            if (!isNaN(price) && price >= 0) {
                const selectedUom = item.uoms && item.uoms.find(u => String(u.id) === String(item.uom_id));
                const factor = selectedUom ? parseFloat(selectedUom.factor) || 1.0 : 1.0;
                const originalPrice = item.base_price * factor;
                
                let isAllowed = true;
                if (item.product_type !== 'SERVICE' && POS.priceMarginPercent > 0 && originalPrice > 0) {
                    const marginValue = originalPrice * (POS.priceMarginPercent / 100);
                    const minAllowed = originalPrice - marginValue;
                    const maxAllowed = originalPrice + marginValue;
                    
                    if (price < minAllowed || price > maxAllowed) {
                        isAllowed = false;
                        alert(`تجاوز السعر الحد المسموح به! السعر المسموح بين ${minAllowed.toFixed(2)} و ${maxAllowed.toFixed(2)}`);
                    }
                }
                
                if (isAllowed) {
                    item.price = price;
                    this.saveSessions();
                }
            }
            this.updateCartDisplay();
        }
    },

    // ============================================================
    // DISCOUNT HELPERS & CALCULATIONS
    // ============================================================
    getCartGrossSubtotal() {
        if (!this.cart || this.cart.length === 0) return 0;
        return this.cart.reduce((sum, item) => sum + ((parseFloat(item.price) || 0) * (parseFloat(item.quantity) || 0)), 0);
    },

    getGlobalDiscountPct() {
        const gross = this.getCartGrossSubtotal();
        const valInput = document.getElementById('globalDiscountVal');
        const pctInput = document.getElementById('globalDiscountPct');

        if (this.discountMode === 'val' && valInput) {
            const val = Math.max(0, parseFloat(valInput.value) || 0);
            if (gross > 0) {
                return Math.min(this.getAllowedGlobalDiscount(), (val / gross) * 100);
            }
            return 0;
        } else if (pctInput) {
            return Math.max(0, Math.min(this.getAllowedGlobalDiscount(), parseFloat(pctInput.value) || 0));
        }
        return 0;
    },

    onDiscountValChange() {
        this.discountMode = 'val';
        const valInput = document.getElementById('globalDiscountVal');
        const pctInput = document.getElementById('globalDiscountPct');
        let val = Math.max(0, parseFloat(valInput ? valInput.value : 0) || 0);
        const gross = this.getCartGrossSubtotal();

        // Enforce the discount cap (value mode is converted to a percentage).
        if (gross > 0 && valInput) {
            const allowedPct = this.getAllowedGlobalDiscount();
            if ((val / gross) * 100 > allowedPct + 0.0001) {
                val = Math.floor(gross * allowedPct) / 100;
                valInput.value = val;
                this.notifyDiscountCap();
            }
        }

        if (pctInput) {
            if (gross > 0) {
                const pct = Math.min(100, (val / gross) * 100);
                pctInput.value = pct > 0 ? (Math.round(pct * 100) / 100) : '0';
            } else {
                pctInput.value = '0';
            }
        }

        const session = this.sessions.find(s => s.id === this.activeSessionId);
        if (session) {
            session.discountVal = val;
            session.discountPct = pctInput ? parseFloat(pctInput.value) || 0 : 0;
            session.discountMode = 'val';
            this.saveSessions();
        }

        this.updateCartDisplay(false);
    },

    onDiscountPctChange() {
        this.discountMode = 'pct';
        const valInput = document.getElementById('globalDiscountVal');
        const pctInput = document.getElementById('globalDiscountPct');
        let pct = Math.max(0, Math.min(100, parseFloat(pctInput ? pctInput.value : 0) || 0));
        const allowedPct = this.getAllowedGlobalDiscount();
        if (pct > allowedPct + 0.0001) {
            pct = Math.floor(allowedPct * 100) / 100;
            if (pctInput) pctInput.value = pct;
            this.notifyDiscountCap();
        }
        const gross = this.getCartGrossSubtotal();

        if (valInput) {
            const val = gross * (pct / 100);
            valInput.value = val > 0 ? (Math.round(val * 100) / 100) : '0';
        }

        const session = this.sessions.find(s => s.id === this.activeSessionId);
        if (session) {
            session.discountPct = pct;
            session.discountVal = valInput ? parseFloat(valInput.value) || 0 : 0;
            session.discountMode = 'pct';
            this.saveSessions();
        }

        this.updateCartDisplay(false);
    },

    syncDiscountInputs() {
        const gross = this.getCartGrossSubtotal();
        const valInput = document.getElementById('globalDiscountVal');
        const pctInput = document.getElementById('globalDiscountPct');

        if (gross === 0) {
            if (valInput) valInput.value = '0';
            if (pctInput) pctInput.value = '0';
            return;
        }

        if (this.discountMode === 'val' && valInput && pctInput) {
            let val = Math.max(0, parseFloat(valInput.value) || 0);
            if (val > gross) {
                val = gross;
                valInput.value = val.toFixed(2);
            }
            // Cart shrank: keep the fixed amount within the discount cap.
            const allowedPct = this.getAllowedGlobalDiscount();
            if ((val / gross) * 100 > allowedPct + 0.0001) {
                val = Math.floor(gross * allowedPct) / 100;
                valInput.value = val.toFixed(2);
            }
            const pct = (val / gross) * 100;
            pctInput.value = pct > 0 ? (Math.round(pct * 100) / 100) : '0';
        } else if (pctInput && valInput) {
            let pct = Math.max(0, Math.min(100, parseFloat(pctInput.value) || 0));
            const allowedPct = this.getAllowedGlobalDiscount();
            if (pct > allowedPct + 0.0001) {
                pct = Math.floor(allowedPct * 100) / 100;
                pctInput.value = pct;
            }
            const val = gross * (pct / 100);
            valInput.value = val > 0 ? (Math.round(val * 100) / 100) : '0';
        }
    },

    // ============================================================
    // TOTALS CALCULATION
    // ============================================================
    calculateTotals() {
        this.cart.forEach(item => this.applyTierPrice(item));
        let grossTotal = 0;
        let totalDiscount = 0;
        let totalTax = 0;
        let totalWht = 0;

        const applyTaxCheckbox = document.getElementById('posApplyTax');
        const applyTax = applyTaxCheckbox ? applyTaxCheckbox.checked : false;
        
        const applyWhtCheckbox = document.getElementById('posApplyWht');
        const applyWht = applyWhtCheckbox ? applyWhtCheckbox.checked : false;
        
        const globalDiscountPct = this.getGlobalDiscountPct();

        this.cart.forEach(item => {
            const lineGross = item.price * item.quantity;
            // First apply item specific discount if any
            const itemDiscount = lineGross * (item.discount / 100);
            let lineNet = lineGross - itemDiscount;
            
            // Then apply global discount pct
            const lineGlobalDiscount = lineNet * (globalDiscountPct / 100);
            lineNet = lineNet - lineGlobalDiscount;
            
            totalDiscount += (itemDiscount + lineGlobalDiscount);

            const effectiveTaxRate = applyTax ? item.tax_rate : 0;
            const lineTax = lineNet * (effectiveTaxRate / 100);
            
            const effectiveWhtRate = applyWht ? item.wht_rate : 0;
            const lineWht = lineNet * (effectiveWhtRate / 100);

            grossTotal += lineGross; // or track net? usually subtotal is gross
            totalTax += lineTax;
            totalWht += lineWht;
        });

        return {
            subtotal: grossTotal,
            discount: totalDiscount,
            tax: totalTax,
            wht: totalWht,
            total: grossTotal - totalDiscount + totalTax - totalWht
        };
    },

    formatCurrency(amount) {
        return parseFloat(amount || 0).toFixed(2);
    },

    // ============================================================
    // CART DISPLAY UPDATE
    // ============================================================
    updateCartDisplay(syncDiscount = true) {
        const cartItems = document.getElementById('cartItems');
        if (!cartItems) return;

        if (syncDiscount) {
            this.syncDiscountInputs();
        }

        const applyTaxCheckbox = document.getElementById('posApplyTax');
        const applyTax = applyTaxCheckbox ? applyTaxCheckbox.checked : false;

        const applyWhtCheckbox = document.getElementById('posApplyWht');
        const applyWht = applyWhtCheckbox ? applyWhtCheckbox.checked : false;

        const globalDiscountPct = this.getGlobalDiscountPct();

        if (this.cart.length === 0) {
            cartItems.innerHTML = '<div class="cart-empty-msg">أضف منتجات للسلة</div>';
        } else {
            cartItems.innerHTML = this.cart.map(item => {
                const itemKey = item._cartKey || item.id;
                const lineGross = item.price * item.quantity;
                const itemDiscount = lineGross * (item.discount / 100);
                let lineNet = lineGross - itemDiscount;
                const lineGlobalDiscount = lineNet * (globalDiscountPct / 100);
                lineNet = lineNet - lineGlobalDiscount;

                const effectiveTaxRate = applyTax ? item.tax_rate : 0;
                const effectiveWhtRate = applyWht ? item.wht_rate : 0;
                const lineTotal = lineNet + (lineNet * (effectiveTaxRate / 100)) - (lineNet * (effectiveWhtRate / 100));
                const selectedUom = item.uoms && item.uoms.find(u => String(u.id) == String(item.uom_id));
                const uomFactor = selectedUom ? parseFloat(selectedUom.factor) || 1.0 : 1.0;
                const displayCost = ((item.base_cost || 0) * uomFactor).toFixed(2);
                const showCost = this.showCost !== false;
                const costLabel = (showCost && item.product_type !== 'SERVICE' && item.base_cost > 0)
                    ? `<div class="cart-item-cost">💰 تكلفة/وحدة: ${displayCost}</div>`
                    : '';
                const canEditPrice = POS.allowPriceEdit || item.product_type === 'SERVICE';
                return `
                <div class="cart-item" data-cart-id="${itemKey}">
                    <div>
                        <div class="cart-item-name">${item.name} ${item.pricing_badge_text ? `<span style="font-size:0.7em; background:rgba(0,229,255,0.15); color:${item.pricing_badge_color || '#00e5ff'}; border:1px solid ${item.pricing_badge_color || '#00e5ff'}; padding:1px 4px; border-radius:3px; margin-right:4px; font-weight:700;">${item.pricing_badge_text}</span>` : ''}</div>
                        <div class="cart-item-barcode">${item.barcode}</div>
                        ${costLabel}
                    </div>
                    <div class="cart-item-qty">
                        <button class="qty-btn" onclick="POS.updateQuantity('${itemKey}', ${item.quantity - 1})">−</button>
                        <input type="number" class="qty-value cart-item-qty-input" value="${item.quantity}" min="0.001" step="any"
                               onchange="POS.updateQuantity('${itemKey}', this.value)">
                        <button class="qty-btn" onclick="POS.updateQuantity('${itemKey}', ${item.quantity + 1})">+</button>
                    </div>
                    <div style="text-align:center;">
                        <input type="number"
                               value="${item.price}"
                               class="cart-item-price-input ${canEditPrice ? '' : 'is-readonly'}"
                               onchange="POS.updateManualPrice('${itemKey}', this.value)"
                               oninput="POS.handlePriceInput('${itemKey}', this)"
                               ${canEditPrice ? '' : 'readonly'}
                               step="0.01" min="0" title="${canEditPrice ? 'تعديل السعر' : 'تعديل السعر مغلق من الإعدادات'}">
                        <div class="cart-item-tax-meta">ض: ${effectiveTaxRate}% | خ: ${effectiveWhtRate}%</div>
                    </div>
                    <div style="text-align:center;">
                        ${item.uoms && item.uoms.length > 1 ? `
                        <select class="cart-item-uom-select" onchange="POS.updateUom('${itemKey}', this.value)">
                            ${item.uoms.map(u => `<option value="${u.id}" ${String(item.uom_id) == String(u.id) ? 'selected' : ''}>${u.name}</option>`).join('')}
                        </select>
                        ` : `<span class="cart-item-uom-static">${item.uoms && item.uoms[0] ? item.uoms[0].name : 'Unit'}</span>`}
                    </div>
                    <div class="cart-item-total-cell">
                        <div class="cart-item-total">${this.formatCurrency(lineTotal)}</div>
                        <button class="cart-item-remove" onclick="POS.removeFromCart('${itemKey}')" title="حذف">✕</button>
                    </div>
                </div>`;
            }).join('');
        }

        // Update totals
        const totals = this.calculateTotals();
        const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        
        set('cartSubtotal', this.formatCurrency(totals.subtotal));
        set('cartDiscount', '(' + this.formatCurrency(totals.discount) + ')');
        set('cartTax', this.formatCurrency(totals.tax));
        set('cartWht', '(' + this.formatCurrency(totals.wht) + ')');
        set('cartTotal', this.formatCurrency(totals.total) + ' ج.م');
        set('modalTotal', this.formatCurrency(this.cashDueAfterRounding(totals.total)) + ' ج.م');
        this.updateCashRoundingRow(totals.total);

        // Cart item count badge
        const countEl = document.getElementById('cartCount');
        if (countEl) {
            const total = this.cart.reduce((s, i) => s + i.quantity, 0);
            countEl.textContent = total;
            countEl.style.display = total > 0 ? '' : 'none';
        }

        // Update the main product grid stock display dynamically
        this.updateGridStockDisplay();
    },

    // ============================================================
    // SALE PRICE DISPLAY (top info boxes)
    // ============================================================
    updateSalePriceDisplay(product) {
        const priceEl = document.getElementById('displayPrice');
        const qtyEl = document.getElementById('displayQty');
        const stockEl = document.getElementById('displayStock');
        const costEl = document.getElementById('displayCost');

        if (priceEl) {
            const pidStr = String(product.id);
            const customPriceObj = (this.currentCustomerPrices && this.currentCustomerPrices[pidStr]) ? this.currentCustomerPrices[pidStr] : null;
            const effectivePrice = customPriceObj ? customPriceObj.price : product.sale_price;
            const badge = customPriceObj ? ` (${customPriceObj.badge_text})` : (product.pricing_badge_text ? ` (${product.pricing_badge_text})` : '');
            priceEl.textContent = `${parseFloat(effectivePrice).toFixed(2)}${badge}`;
        }
        if (qtyEl) {
            const cartItem = this.cart.find(i => i.id === parseInt(product.id));
            qtyEl.textContent = cartItem ? cartItem.quantity : 1;
            if (costEl && cartItem) {
                costEl.textContent = (cartItem.current_cost || cartItem.base_cost || 0).toFixed(2);
            } else if (costEl) {
                costEl.textContent = parseFloat(product.avg_cost || 0).toFixed(2);
            }
        }
        if (stockEl) {
            const sVal = parseFloat(product.available_stock) || 0;
            stockEl.textContent = product.available_stock || 0;
            if (sVal < 0) {
                stockEl.style.color = '#ff4d4f';
                stockEl.style.fontWeight = 'bold';
            } else {
                stockEl.style.color = '';
                stockEl.style.fontWeight = '';
            }
        }
    },

    updateGridStockDisplay() {
        // Build a map of total reserved quantities across ALL open sessions
        const totalReserved = {};
        this.sessions.forEach(session => {
            (session.cart || []).forEach(item => {
                totalReserved[item.id] = (totalReserved[item.id] || 0) + item.quantity;
            });
        });

        const rows = document.querySelectorAll('#productsTableBody .product-row');
        rows.forEach(row => {
            const productId = parseInt(row.dataset.productId);
            const originalStock = parseFloat(row.dataset.availableStock) || 0;
            const stockCell = row.querySelector('.stock-cell');
            const qtyValue = row.querySelector('.qty-value');
            if (stockCell) {
                const reserved = totalReserved[productId] || 0;
                const remaining = originalStock - reserved;
                stockCell.textContent = remaining;
                if (remaining < 0) {
                    stockCell.style.color = '#ff4d4f';
                    stockCell.style.fontWeight = 'bold';
                } else if (remaining === 0) {
                    stockCell.style.color = '#94a3b8';
                    stockCell.style.fontWeight = '';
                } else {
                    stockCell.style.color = '';
                    stockCell.style.fontWeight = '';
                }
                // Show active session's cart qty in the grid row
                if (qtyValue) {
                    const activeCartItem = this.cart.find(i => i.id === productId);
                    qtyValue.textContent = activeCartItem ? activeCartItem.quantity : 1;
                }
            }
        });
    },

    clearSalePriceDisplay() {
        ['displayPrice', 'displayQty', 'displayStock', 'displayCost'].forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                el.textContent = '-';
                el.style.color = '';
                el.style.fontWeight = '';
            }
        });
    },

    // ============================================================
    // CART EVENTS (row click & long-press delegation)
    // ============================================================
    setupCartEvents() {
        const tbody = document.getElementById('productsTableBody');
        if (!tbody) return;

        // Prevent duplicate listeners
        if (tbody._posEventsAttached) return;
        tbody._posEventsAttached = true;

        let longPressTimer = null;
        let isLongPress = false;
        let startX = 0;
        let startY = 0;

        const cancelLongPress = () => {
            if (longPressTimer) {
                clearTimeout(longPressTimer);
                longPressTimer = null;
            }
        };

        const handleStart = (e) => {
            if (e.target.closest('.qty-btn') || e.target.closest('.qty-control')) return;
            const row = e.target.closest('.product-row');
            if (!row) return;

            isLongPress = false;
            cancelLongPress();

            if (e.type === 'touchstart' && e.touches && e.touches[0]) {
                startX = e.touches[0].clientX;
                startY = e.touches[0].clientY;
            }

            longPressTimer = setTimeout(() => {
                isLongPress = true;
                row._isLongPress = true;
                if (navigator.vibrate) {
                    try { navigator.vibrate(50); } catch(err) {}
                }
                const productId = parseInt(row.dataset.productId);
                POS.showLastPricesModal(productId, row.dataset.productName);
            }, 500);
        };

        const handleMove = (e) => {
            if (e.type === 'touchmove' && e.touches && e.touches[0]) {
                const diffX = Math.abs(e.touches[0].clientX - startX);
                const diffY = Math.abs(e.touches[0].clientY - startY);
                if (diffX > 10 || diffY > 10) {
                    cancelLongPress();
                }
            }
        };

        tbody.addEventListener('mousedown', handleStart);
        tbody.addEventListener('touchstart', handleStart, { passive: true });

        tbody.addEventListener('mouseup', cancelLongPress);
        tbody.addEventListener('mouseleave', cancelLongPress);
        tbody.addEventListener('touchend', cancelLongPress);
        tbody.addEventListener('touchcancel', cancelLongPress);
        tbody.addEventListener('touchmove', handleMove, { passive: true });

        tbody.addEventListener('contextmenu', (e) => {
            const row = e.target.closest('.product-row');
            if (row && (isLongPress || row._isLongPress)) {
                e.preventDefault();
            }
        });

        tbody.addEventListener('click', (e) => {
            // Don't trigger on quantity buttons
            if (e.target.closest('.qty-btn')) return;
            if (e.target.closest('.qty-control')) return;

            const row = e.target.closest('.product-row');
            if (!row) return;

            if (isLongPress || row._isLongPress) {
                isLongPress = false;
                row._isLongPress = false;
                e.preventDefault();
                e.stopPropagation();
                return;
            }

            const product = this.extractProductFromRow(row);
            if (product) {
                this.addToCart(product, 1);
                this.highlightRow(row);
                this.updateSalePriceDisplay(product);
            }
        });
    },

    // ============================================================
    // LAST SELLING PRICES MODAL (Long-Press Handler)
    // ============================================================
    async showLastPricesModal(productId, fallbackName = '') {
        const modal = document.getElementById('lastPricesModal');
        const pNameEl = document.getElementById('lpModalProductName');
        const pBarcodeEl = document.getElementById('lpModalProductBarcode');
        const cNameEl = document.getElementById('lpModalCustomerName');
        const bodyEl = document.getElementById('lpModalBody');
        const movementBtn = document.getElementById('lpModalMovementBtn');

        if (!modal || !bodyEl) return;

        if (movementBtn) {
            movementBtn.href = `/reports/inventory/movement/?product_id=${productId}`;
        }

        // Get currently selected customer from select element
        const customerSelect = document.getElementById('posCustomer');
        let customerId = '';
        let customerNameText = 'عميل نقدي';

        if (customerSelect && customerSelect.value) {
            customerId = customerSelect.value;
            const opt = customerSelect.options[customerSelect.selectedIndex];
            if (opt) customerNameText = opt.textContent.trim();
        }

        if (pNameEl) pNameEl.textContent = fallbackName || 'جاري التحميل...';
        if (pBarcodeEl) pBarcodeEl.textContent = '';
        if (cNameEl) cNameEl.textContent = customerNameText;
        if (bodyEl) {
            bodyEl.innerHTML = `
                <div style="text-align:center; padding:2rem; color:var(--text-muted);">
                    <div style="font-size:1rem; margin-bottom:8px;">⏳</div>
                    <div>جاري جلب آخر 5 أسعار بيع للعميل...</div>
                </div>`;
        }

        modal.style.display = 'flex';

        try {
            const url = `/invoicing/api/product/last-prices/?product_id=${productId}&partner_id=${customerId}`;
            const resp = await fetch(url);
            if (!resp.ok) throw new Error('Failed to fetch sales history');
            const data = await resp.json();

            if (!data.success) {
                bodyEl.innerHTML = `<div style="text-align:center; padding:2rem; color:var(--danger);">❌ ${data.error || 'حدث خطأ أثناء جلب البيانات'}</div>`;
                return;
            }

            if (pNameEl) pNameEl.textContent = data.product_name;
            if (pBarcodeEl) pBarcodeEl.textContent = data.product_barcode ? `باركود: ${data.product_barcode}` : '';
            if (cNameEl) cNameEl.textContent = data.customer_name;

            if (!data.history || data.history.length === 0) {
                bodyEl.innerHTML = `
                    <div style="text-align:center; padding:2.5rem 1.5rem; color:#e2e8f0; background:rgba(255,255,255,0.03); border-radius:10px; border:1px dashed rgba(255,255,255,0.2); font-size:1.05rem; line-height:1.6;">
                        ℹ️ لا توجد فواتير مبيعات سابقة لهذه المادة مع <strong style="color:#ffffff;">${data.customer_name}</strong>.
                    </div>`;
                return;
            }

            let html = `
                <div style="overflow-x: auto; border:1px solid rgba(255,255,255,0.15); border-radius:8px; background: #0f172a;">
                    <table style="width:100%; border-collapse:collapse; font-size:0.95rem; min-width: 540px;">
                        <thead>
                            <tr style="background:#1e293b; border-bottom:2px solid var(--neon-blue); text-align:right;">
                                <th style="padding:12px 14px; font-size:1rem; font-weight:700; color:#ffffff;">الفاتورة والتاريخ</th>
                                <th style="padding:12px 14px; font-size:1rem; font-weight:700; color:#ffffff;">سعر البيع</th>
                                <th style="padding:12px 14px; font-size:1rem; font-weight:700; color:#ffffff;">الكمية والوحدة</th>
                                <th style="padding:12px 14px; font-size:1rem; font-weight:700; color:#ffffff;">الخصم</th>
                                <th style="padding:12px 14px; text-align:left; font-size:1rem; font-weight:700; color:#ffffff;">طريقة السداد</th>
                            </tr>
                        </thead>
                        <tbody>`;

            data.history.forEach((line, idx) => {
                const discountText = line.discount_pct > 0 ? `${line.discount_pct}%` : '-';
                html += `
                    <tr style="border-bottom:1px solid rgba(255,255,255,0.08); ${idx % 2 === 0 ? 'background:rgba(255,255,255,0.04);' : 'background:transparent;'}">
                        <td style="padding:12px 14px;">
                            <div style="font-weight:700; color:#38bdf8; font-size:1.05rem;">#${line.invoice_number}</div>
                            <div style="font-size:0.85rem; color:#cbd5e1; margin-top:3px;">${line.date}</div>
                        </td>
                        <td style="padding:12px 14px; font-weight:800; color:#10b981; font-size:1.25rem;">
                            ${parseFloat(line.unit_price).toFixed(2)} <span style="font-size:0.85rem; font-weight:600; color:#e2e8f0;">ج.م</span>
                        </td>
                        <td style="padding:12px 14px; font-size:1.05rem; font-weight:600; color:#ffffff;">
                            ${line.quantity} <span style="font-size:0.9rem; color:#cbd5e1; font-weight:normal;">${line.uom_name || ''}</span>
                        </td>
                        <td style="padding:12px 14px; color:#fbbf24; font-weight:700; font-size:1.05rem;">
                            ${discountText}
                        </td>
                        <td style="padding:12px 14px; text-align:left;">
                            <span class="badge" style="background:rgba(255,255,255,0.15); color:#ffffff; font-size:0.88rem; font-weight:600; padding:5px 12px; border-radius:6px; border:1px solid rgba(255,255,255,0.2); display:inline-block;">${line.payment_type_display}</span>
                        </td>
                    </tr>`;
            });

            html += `</tbody></table></div>`;
            bodyEl.innerHTML = html;

        } catch (err) {
            console.error('[POS] Last prices error:', err);
            if (bodyEl) {
                bodyEl.innerHTML = `<div style="text-align:center; padding:1.5rem; color:var(--danger);">⚠️ تعذر جلب سجل الأسعار. تحقق من الاتصال بالشبكة.</div>`;
            }
        }
    },

    // ============================================================
    // CASH INPUT CALCULATOR (live change display)
    // ============================================================
    setupCashInputCalculator() {
        const cashInput = document.getElementById('cashReceived');
        if (!cashInput) return;

        cashInput.addEventListener('input', () => {
            const received = parseFloat(cashInput.value) || 0;
            const totals = this.calculateTotals();
            const change = received - this.cashDueAfterRounding(totals.total);
            const changeEl = document.getElementById('changeAmount');
            if (changeEl) {
                changeEl.textContent = `${this.formatCurrency(Math.max(0, change))} ج.م`;
                changeEl.style.color = change >= 0 ? '#059669' : '#e53e3e';
            }
        });

        cashInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') this.confirmCashPayment();
        });
    },

    // ============================================================
    // PAYMENT — CASH
    // ============================================================
    async processCashPayment() {
        if (this.cart.length === 0) {
            this.showNotification('⚠ السلة فارغة — أضف منتجات أولاً', 'warning');
            return;
        }
        if (!this.checkLargeAmountCustomer(this.calculateTotals().total)) return;
        const modal = document.getElementById('cashModal');
        if (modal) {
            const totals = this.calculateTotals();
            const cashDue = this.cashDueAfterRounding(totals.total);
            const totalEl = document.getElementById('modalTotal');
            if (totalEl) totalEl.textContent = `${this.formatCurrency(cashDue)} ج.م`;
            const cashInput = document.getElementById('cashReceived');
            if (cashInput) {
                cashInput.value = cashDue;
                cashInput.focus();
                cashInput.select();
            }
            const changeEl = document.getElementById('changeAmount');
            if (changeEl) changeEl.textContent = '0.00 ج.م';
            modal.style.display = 'flex';
        } else {
            // No modal — proceed directly
            await this.completeSale('CASH');
        }
    },

    async confirmCashPayment() {
        const cashInput = document.getElementById('cashReceived');
        const received = parseFloat(cashInput?.value) || 0;
        const totals = this.calculateTotals();
        const cashDue = this.cashDueAfterRounding(totals.total);

        if (received + 1e-9 < cashDue) {
            this.showNotification(`⚠ المبلغ المدفوع (${this.formatCurrency(received)}) أقل من المطلوب (${this.formatCurrency(cashDue)})`, 'error');
            return;
        }

        const modal = document.getElementById('cashModal');
        if (modal) modal.style.display = 'none';

        await this.completeSale('CASH', received);
    },

    // ============================================================
    // PAYMENT — CARD
    // ============================================================
    async processCardPayment() {
        if (this.cart.length === 0) {
            this.showNotification('⚠ السلة فارغة', 'warning');
            return;
        }

        const visaSelect = document.getElementById('posVisaBank') || document.getElementById('posBank');
        const bankId = visaSelect ? visaSelect.value : null;

        if (!bankId) {
            this.showNotification('⚠️ لا توجد ماكينة فيزا مسجلة أو مختارة! يرجى اختيار الماكينة من القائمة العلوية أو إنشاؤها من شاشة الحسابات البنكية.', 'error');
            return;
        }

        if (confirm('تأكيد الدفع بالبطاقة؟')) {
            await this.completeSale('CARD', null, null, bankId);
        }
    },

    // ============================================================
    // PAYMENT — CREDIT
    // ============================================================
    async processCreditPayment() {
        if (this.cart.length === 0) {
            this.showNotification('⚠ السلة فارغة', 'warning');
            return;
        }
        
        const customerSelect = document.getElementById('posCustomer');
        if (!customerSelect || !customerSelect.value) {
            this.showNotification('⚠ يجب اختيار عميل للدفع الآجل', 'warning');
            return;
        }
        
        if (confirm('تأكيد الدفع الآجل؟')) {
            await this.completeSale('CREDIT');
        }
    },

    // ============================================================
    // PAYMENT — EWALLET
    // ============================================================
    async processEWalletPayment() {
        if (this.cart.length === 0) {
            this.showNotification('⚠️ السلة فارغة', 'warning');
            return;
        }
        
        const ewalletSelect = document.getElementById('posEwallet');
        const ewalletId = ewalletSelect ? ewalletSelect.value : null;

        if (!ewalletId) {
            this.showNotification('⚠️ الرجاء اختيار المحفظة من القائمة العلوية أولاً', 'error');
            return;
        }

        if (confirm('تأكيد الدفع عن طريق المحفظة الإلكترونية؟')) {
            await this.completeSale('EWALLET', null, ewalletId, null);
        }
    },

    // ============================================================
    // PAYMENT — BANK
    // ============================================================
    async processBankPayment() {
        if (this.cart.length === 0) {
            this.showNotification('⚠️ السلة فارغة', 'warning');
            return;
        }
        
        const instaSelect = document.getElementById('posInstaBank') || document.getElementById('posBank');
        const bankId = instaSelect ? instaSelect.value : null;

        if (!bankId) {
            this.showNotification('⚠️ لا يوجد حساب إنستا مسجل أو مختار! يرجى اختيار الحساب من القائمة العلوية أو إنشاؤه من شاشة الحسابات البنكية.', 'error');
            return;
        }

        if (confirm('تأكيد الدفع عن طريق إنستا (تحويل بنكي لحظي)؟')) {
            await this.completeSale('BANK_TRANSFER', null, null, bankId);
        }
    },

    // ============================================================
    // PAYMENT — SPLIT / MULTI-PAYMENT
    // ============================================================
    processSplitPayment() {
        if (this.cart.length === 0) {
            this.showNotification('⚠️ السلة فارغة', 'warning');
            return;
        }
        if (!this.checkLargeAmountCustomer(this.calculateTotals().total)) return;
        const modal = document.getElementById('splitModal');
        if (modal) {
            const totals = this.calculateTotals();
            const totalEl = document.getElementById('splitModalTotal');
            if (totalEl) totalEl.textContent = `${this.formatCurrency(totals.total)} ج.م`;

            // Reset inputs
            ['splitCashInput', 'splitCardInput', 'splitEwalletInput', 'splitBankInput', 'splitCreditInput'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.value = '0';
            });
            // Default first input (cash) with full total
            const cashInp = document.getElementById('splitCashInput');
            if (cashInp) cashInp.value = this.cashDueAfterRounding(totals.total);

            this.updateSplitCalculations();
            modal.style.display = 'flex';
        }
    },

    updateSplitCalculations() {
        const totals = this.calculateTotals();
        const total = totals.total;

        const cash = parseFloat(document.getElementById('splitCashInput')?.value) || 0;
        const card = parseFloat(document.getElementById('splitCardInput')?.value) || 0;
        const ewallet = parseFloat(document.getElementById('splitEwalletInput')?.value) || 0;
        const bank = parseFloat(document.getElementById('splitBankInput')?.value) || 0;
        const credit = parseFloat(document.getElementById('splitCreditInput')?.value) || 0;

        const totalPaid = cash + card + ewallet + bank + credit;
        // Expected amount: non-cash parts + the cash part rounded (pos_cash_rounding).
        const nonCash = card + ewallet + bank + credit;
        const expected = cash > 0 ? nonCash + this.cashDueAfterRounding(total, nonCash) : total;
        const remaining = Math.round((expected - totalPaid) * 100) / 100;

        const remEl = document.getElementById('splitRemainingAmount');
        if (remEl) {
            remEl.textContent = `${this.formatCurrency(remaining)} ج.م`;
            remEl.style.color = Math.abs(remaining) < 0.01 ? 'var(--neon-green)' : (remaining > 0 ? 'var(--neon-gold)' : 'var(--danger)');
        }
    },

    fillSplitRemaining(targetType) {
        const totals = this.calculateTotals();
        const total = totals.total;

        const inputs = {
            cash: document.getElementById('splitCashInput'),
            card: document.getElementById('splitCardInput'),
            ewallet: document.getElementById('splitEwalletInput'),
            bank: document.getElementById('splitBankInput'),
            credit: document.getElementById('splitCreditInput'),
        };

        let otherTotal = 0;
        Object.keys(inputs).forEach(key => {
            if (key !== targetType && inputs[key]) {
                otherTotal += parseFloat(inputs[key].value) || 0;
            }
        });

        let remaining = Math.max(0, total - otherTotal);
        if (targetType === 'cash') remaining = this.cashDueAfterRounding(total, otherTotal);
        if (inputs[targetType]) {
            inputs[targetType].value = remaining.toFixed(2);
        }
        this.updateSplitCalculations();
    },

    async confirmSplitPayment() {
        const totals = this.calculateTotals();
        const total = totals.total;

        const cash = parseFloat(document.getElementById('splitCashInput')?.value) || 0;
        const card = parseFloat(document.getElementById('splitCardInput')?.value) || 0;
        const ewallet = parseFloat(document.getElementById('splitEwalletInput')?.value) || 0;
        const bank = parseFloat(document.getElementById('splitBankInput')?.value) || 0;
        const credit = parseFloat(document.getElementById('splitCreditInput')?.value) || 0;

        const sumPaid = cash + card + ewallet + bank + credit;

        if (credit > 0) {
            const customerSelect = document.getElementById('posCustomer');
            if (!customerSelect || !customerSelect.value) {
                this.showNotification('⚠️ يجب اختيار عميل لتسجيل جزء آجل', 'warning');
                return;
            }
        }

        // The cash part is rounded (pos_cash_rounding); the server recomputes it.
        const nonCashPaid = card + ewallet + bank + credit;
        const expectedPaid = cash > 0 ? nonCashPaid + this.cashDueAfterRounding(total, nonCashPaid) : total;
        if (Math.abs(sumPaid - expectedPaid) > 0.05) {
            this.showNotification(`⚠️ إجمالي المبالغ المدخلة (${this.formatCurrency(sumPaid)}) لا يساوي المطلوب (${this.formatCurrency(expectedPaid)})`, 'error');
            return;
        }

        const ewalletSelect = document.getElementById('posEwallet');
        const ewalletId = ewalletSelect ? ewalletSelect.value : null;

        const visaSelect = document.getElementById('posVisaBank') || document.getElementById('posBank');
        const visaBankId = visaSelect ? visaSelect.value : null;

        const instaSelect = document.getElementById('posInstaBank') || document.getElementById('posBank');
        const instaBankId = instaSelect ? instaSelect.value : null;

        if (card > 0 && !visaBankId) {
            this.showNotification('⚠️ الرجاء اختيار ماكينة الفيزا من القائمة العلوية أولاً', 'error');
            return;
        }

        if (bank > 0 && !instaBankId) {
            this.showNotification('⚠️ الرجاء اختيار حساب إنستا من القائمة العلوية أولاً', 'error');
            return;
        }

        if (ewallet > 0 && !ewalletId) {
            this.showNotification('⚠️ الرجاء اختيار المحفظة الإلكترونية من القائمة العلوية أولاً', 'error');
            return;
        }

        const bankId = visaBankId || instaBankId;
        const splitPayments = { cash, card, ewallet, bank, credit, visa_bank_id: visaBankId || null, insta_bank_id: instaBankId || null };

        const modal = document.getElementById('splitModal');
        if (modal) modal.style.display = 'none';

        await this.completeSale('SPLIT', cash, ewalletId, bankId, splitPayments);
    },

    // ============================================================
    // COMPLETE SALE
    // ============================================================
    async completeSale(paymentType, cashReceived = null, ewalletId = null, bankAccountId = null, splitPayments = null) {
        if (this.cart.length === 0) return;

        const totals = this.calculateTotals();
        if (!this.checkLargeAmountCustomer(totals.total)) return;
        const customerSelect = document.getElementById('posCustomer');
        const partner_id = customerSelect ? customerSelect.value : null;
        const customerOption = customerSelect ? customerSelect.options[customerSelect.selectedIndex] : null;

        const treasurySelect = document.getElementById('posTreasury');
        const treasury_id = treasurySelect ? treasurySelect.value : null;
        const invoice_type = this.isReturnMode ? 'RETURN_SALE' : 'SALE';

        const applyTaxCheckbox = document.getElementById('posApplyTax');
        const applyTax = applyTaxCheckbox ? applyTaxCheckbox.checked : false;

        const applyWhtCheckbox = document.getElementById('posApplyWht');
        const applyWht = applyWhtCheckbox ? applyWhtCheckbox.checked : false;

        const globalDiscountPct = this.getGlobalDiscountPct();

        const notesEl = document.getElementById('posInvoiceNotes');
        const invoiceNotes = notesEl ? notesEl.value.trim() : '';

        const payload = {
            cart: this.cart.map(item => {
                const lineGross = item.price * item.quantity;
                const itemDiscAmt = lineGross * ((item.discount || 0) / 100);
                const lineNet = lineGross - itemDiscAmt;
                const globalDiscAmt = lineNet * (globalDiscountPct / 100);
                const totalLineDiscAmt = itemDiscAmt + globalDiscAmt;
                const effectiveDiscPct = lineGross > 0 ? (totalLineDiscAmt / lineGross) * 100 : 0;

                return {
                    product_id: item.id,
                    quantity: item.quantity,
                    unit_price: item.price,
                    discount_percent: parseFloat(effectiveDiscPct.toFixed(4)),
                    tax_rate: applyTax ? item.tax_rate : 0,
                    wht_rate: applyWht ? item.wht_rate : 0,
                    uom_id: item.uom_id,
                    serial_item_id: item.serial_item_id || null
                };
            }),
            payment_type: paymentType,
            warehouse_id: this.currentWarehouseId,
            branch_id: this.currentBranchId,
            partner_id: partner_id,
            treasury_id: treasury_id,
            ewallet_id: ewalletId,
            bank_account_id: bankAccountId,
            invoice_type: invoice_type,
            discount_percentage: globalDiscountPct,
            subtotal: totals.subtotal,
            discount_amount: totals.discount,
            tax_amount: totals.tax,
            wht_amount: totals.wht,
            total_amount: totals.total,
            cash_received: cashReceived,
            payments: splitPayments || {},
            notes: invoiceNotes
        };

        try {
            const resp = await fetch('/invoicing/api/sale/complete/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken()
                },
                body: JSON.stringify(payload)
            });

            if (!resp.ok) {
                const err = await resp.json().catch(() => ({ error: 'Server error' }));
                throw new Error(err.error || 'خطأ في السيرفر');
            }

            const data = await resp.json();
            if (data.success) {
                this.lastInvoiceData = data;
                if (notesEl) notesEl.value = '';

                // Record consecutive customer info
                const completedCustId = data.partner_id ? String(data.partner_id) : (partner_id ? String(partner_id) : '');
                const completedCustName = data.partner_name || (customerOption ? customerOption.text.trim() : '') || '';
                const isCashCust = !completedCustId || completedCustName.includes('نقدي') || (customerOption && customerOption.text.includes('نقدي'));

                if (!isCashCust && completedCustId) {
                    this.lastSaleCustomerId = completedCustId;
                    this.lastSaleCustomerName = completedCustName;
                    this.lastSaleInvoiceNumber = data.invoice_number || '';
                    try {
                        sessionStorage.setItem('pos_last_sale_customer_id', this.lastSaleCustomerId);
                        sessionStorage.setItem('pos_last_sale_customer_name', this.lastSaleCustomerName);
                        sessionStorage.setItem('pos_last_sale_invoice_num', this.lastSaleInvoiceNumber);
                    } catch(e) {}
                } else {
                    this.lastSaleCustomerId = null;
                    this.lastSaleCustomerName = null;
                    this.lastSaleInvoiceNumber = null;
                    try {
                        sessionStorage.removeItem('pos_last_sale_customer_id');
                        sessionStorage.removeItem('pos_last_sale_customer_name');
                        sessionStorage.removeItem('pos_last_sale_invoice_num');
                    } catch(e) {}
                }

                // Update stock locally: subtract sold quantities or add returned quantities to grid (factored)
                this.cart.forEach(item => {
                    const row = document.querySelector(`[data-product-id="${item.id}"]`);
                    if (row) {
                        const oldStock = parseFloat(row.dataset.availableStock) || 0;
                        const selUom = item.uoms && item.uoms.find(u => String(u.id) === String(item.uom_id));
                        const uomFactor = selUom ? parseFloat(selUom.factor) || 1.0 : 1.0;
                        const deltaQty = item.quantity * uomFactor;
                        if (invoice_type === 'RETURN_SALE') {
                            row.dataset.availableStock = oldStock + deltaQty;
                        } else {
                            row.dataset.availableStock = oldStock - deltaQty;
                        }
                    }
                });

                // Auto reset return mode if active so subsequent sales are normal
                if (this.isReturnMode) {
                    this.toggleReturnMode(false);
                }

                this.showReceiptModal(data, totals, cashReceived);
                // auto_print_receipt: open the receipt document, which prints itself once on load.
                if (this.autoPrintReceipt && data.invoice_id) {
                    this.printReceipt(data.invoice_id);
                }
                this.closeTab(null, this.activeSessionId);
                this.checkConsecutiveCustomerWarning(false);
                this.updateGridStockDisplay();
                this.showNotification(`تم حفظ الفاتورة: ${data.invoice_number}`, 'success');
                this.refreshShiftStats();
            } else {
                throw new Error(data.error || 'فشل في إتمام البيع');
            }
        } catch (err) {
            this.showNotification(`❌ خطأ: ${err.message}`, 'error');
            if (err.message && err.message.includes('وردية')) {
                setTimeout(() => {
                    window.location.href = '/invoicing/pos/open-shift/';
                }, 1500);
            }
        }
    },

    // ============================================================
    // SERIAL PICKER MODAL (for SERIALIZED products)
    // ============================================================
    async showSerialPickerModal(product) {
        // Remove old modal if exists
        const oldModal = document.getElementById('serialPickerModal');
        if (oldModal) oldModal.remove();

        // Fetch available serials from server
        let serials = [];
        try {
            const resp = await fetch(`/invoicing/api/product/serials/?product_id=${product.id}&warehouse_id=${this.currentWarehouseId || ''}`);
            const data = await resp.json();
            serials = data.serials || [];
        } catch(e) {
            this.showNotification('❌ خطأ في جلب السيريالات', 'error');
            return;
        }

        if (serials.length === 0) {
            this.showNotification(`⚠ لا يوجد مخزون متاح من "${product.name}"`, 'warning');
            return;
        }

        const rowsHtml = serials.map(s => `
            <tr style="cursor:pointer; color:#fff; border-bottom:1px solid rgba(255,255,255,0.05);" onmouseover="this.style.background='rgba(255,255,255,0.1)'" onmouseout="this.style.background='transparent'" onclick="POS._selectSerial(${product.id}, ${s.id}, '${s.display_label}', ${product.sale_price || 0}, '${product.name}', '${product.barcode || ''}', ${product.tax_rate || 0}); document.getElementById('serialPickerModal').remove();">
                <td style="padding:10px;">${s.display_label}</td>
                <td style="padding:10px;">${s.condition}</td>
                <td style="padding:10px;">${s.storage || '-'}</td>
                <td style="padding:10px;">${s.ram || '-'}</td>
            </tr>
        `).join('');

        const modal = document.createElement('div');
        modal.id = 'serialPickerModal';
        modal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;';
        modal.innerHTML = `
            <div style="background:var(--surface,#1e2a3a);border-radius:12px;padding:24px;min-width:520px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,0.5);">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
                    <h3 style="margin:0;color:var(--text,#fff);font-size:1.1rem;">اختر السيريال — ${product.name}</h3>
                    <button onclick="document.getElementById('serialPickerModal').remove()" style="background:none;border:none;color:var(--text-muted,#94a3b8);font-size:1.4rem;cursor:pointer;">✕</button>
                </div>
                <div style="max-height:340px;overflow-y:auto;">
                    <table style="width:100%;border-collapse:collapse;">
                        <thead>
                            <tr style="background:rgba(255,255,255,0.05);">
                                <th style="padding:10px;text-align:right;color:var(--text-muted,#94a3b8);font-size:0.85rem;">السيريال/IMEI</th>
                                <th style="padding:10px;text-align:right;color:var(--text-muted,#94a3b8);font-size:0.85rem;">الحالة</th>
                                <th style="padding:10px;text-align:right;color:var(--text-muted,#94a3b8);font-size:0.85rem;">المساحة</th>
                                <th style="padding:10px;text-align:right;color:var(--text-muted,#94a3b8);font-size:0.85rem;">الرام</th>
                            </tr>
                        </thead>
                        <tbody>${rowsHtml}</tbody>
                    </table>
                </div>
            </div>`;
        document.body.appendChild(modal);
    },

    _selectSerial(productId, serialId, displayLabel, price, name, barcode, taxRate) {
        this.cart.push({
            id: productId,
            name: `${name} (${displayLabel})`,
            product_type: 'SERIALIZED',
            barcode: barcode,
            price: parseFloat(price) || 0,
            base_price: parseFloat(price) || 0,
            quantity: 1,
            discount: 0,
            tax_rate: parseFloat(taxRate) || 0,
            wht_rate: 0,
            available_stock: 1,
            uoms: [{id: 'base', name: 'قطعة'}],
            uom_id: 'base',
            serial_item_id: serialId,
            serial_number: displayLabel,
            _uniqueKey: `${productId}_${serialId}` // prevent merging
        });
        this.saveSessions();
        this.updateCartDisplay();
        this.showNotification(`✓ تمت الإضافة: ${name} (${displayLabel})`, 'success');
    },

    // ============================================================
    // RECEIPT MODAL
    // ============================================================
    showReceiptModal(data, totals, cashReceived) {
        const modal = document.getElementById('receiptModal');
        const content = document.getElementById('receiptContent');
        if (!modal || !content) return;

        // Change is computed against the cash actually due (after pos_cash_rounding).
        const roundingAmount = parseFloat(data.rounding_amount) || 0;
        const cashDue = (data.paid_cash !== undefined && data.paid_cash !== null)
            ? (parseFloat(data.paid_cash) || 0)
            : (totals?.total || 0);
        const change = cashReceived ? Math.max(0, cashReceived - cashDue) : 0;
        const itemsHtml = (data.items || this.cart).map(item => `
            <div class="receipt-item">
                <span>${item.name || item.product_name}</span>
                <span>${this.formatCurrency(item.total || (item.price * item.quantity))}</span>
            </div>`).join('');

        content.innerHTML = `
            <div class="receipt-header" style="text-align:center;margin-bottom:0.75rem;">
                <img src="/static/img/logo.png" alt="مسار" style="height:45px;margin-bottom:4px;border-radius:6px;object-fit:contain;">
                <div style="font-size:1.1rem;font-weight:800;color:#1e293b;">مسار Masar</div>
                <div style="font-size:0.75rem;color:#64748b;">${data.branch_name || ''}</div>
                <div style="font-size:0.72rem;color:#94a3b8;">${new Date().toLocaleString('en-US')}</div>
            </div>
            <div style="font-size:0.78rem;margin-bottom:0.4rem;color:#374151;">
                <strong>فاتورة #${data.invoice_number}</strong>
            </div>
            <div class="receipt-items" style="font-size:0.78rem;">${itemsHtml}</div>
            <div class="receipt-total">
                <span>الإجمالي</span>
                <span>${this.formatCurrency(data.total_amount || totals?.total)} ج.م</span>
            </div>
            ${roundingAmount !== 0 ? `
            <div class="receipt-item" style="font-size:0.8rem;">
                <span>تقريب</span><span>${roundingAmount > 0 ? '+' : ''}${this.formatCurrency(roundingAmount)} ج.م</span>
            </div>
            <div class="receipt-item" style="font-size:0.8rem;font-weight:700;">
                <span>المطلوب نقداً بعد التقريب</span><span>${this.formatCurrency(cashDue)} ج.م</span>
            </div>` : ''}
            ${cashReceived ? `
            <div class="receipt-item" style="font-size:0.8rem;margin-top:0.4rem;">
                <span>المبلغ المدفوع</span><span>${this.formatCurrency(cashReceived)} ج.م</span>
            </div>
            <div class="receipt-item" style="font-weight:700;color:#059669;">
                <span>الباقي</span><span>${this.formatCurrency(change)} ج.م</span>
            </div>` : ''}
            <div style="text-align:center;margin-top:1rem;font-size:0.7rem;color:#94a3b8;">
                شكراً لتسوقكم معنا • Thank you for shopping
            </div>`;

        // Store last invoice data for modal buttons
        this.lastInvoiceId = data.invoice_id;

        // Sync paper size selector UI with saved preference
        this.updateReceiptPaperSizeUI();

        modal.style.display = 'flex';
    },


    // ============================================================
    clearCart() {
        this.cart = [];
        this.discountMode = 'pct';
        const valInput = document.getElementById('globalDiscountVal');
        if (valInput) valInput.value = '0';
        const pctInput = document.getElementById('globalDiscountPct');
        if (pctInput) pctInput.value = '0';
        const notesInput = document.getElementById('posInvoiceNotes');
        if (notesInput) notesInput.value = '';

        const custEl = document.getElementById('posCustomer');
        if (custEl) {
            custEl.value = '';
            if (typeof $ !== 'undefined' && $(custEl).hasClass('select2-hidden-accessible')) {
                $(custEl).val('').trigger('change.select2');
            }
        }

        const session = this.sessions.find(s => s.id === this.activeSessionId);
        if (session) {
            session.discountVal = 0;
            session.discountPct = 0;
            session.discountMode = 'pct';
            session.notes = '';
            session.customerId = '';
            this.saveSessions();
        }

        this.updateCartDisplay(false);
        this.clearSalePriceDisplay();
        // Clear product row selections
        document.querySelectorAll('.product-row.selected').forEach(r => r.classList.remove('selected'));
        this.selectedProductRow = null;
        this.checkConsecutiveCustomerWarning(false);
    },

    // ============================================================
    // TABS MANAGEMENT
    // ============================================================
    createTab() {
        const current = this.sessions.find(s => s.id === this.activeSessionId);
        if (current) {
            const custEl = document.getElementById('posCustomer');
            if (custEl) current.customerId = custEl.value;
            const notesEl = document.getElementById('posInvoiceNotes');
            if (notesEl) current.notes = notesEl.value;
            const taxEl = document.getElementById('posApplyTax');
            if (taxEl) current.applyTax = taxEl.checked;
            const whtEl = document.getElementById('posApplyWht');
            if (whtEl) current.applyWht = whtEl.checked;
        }

        const id = this.nextSessionId++;
        this.sessions.push({
            id: id,
            name: `فاتورة ${id}`,
            cart: [],
            discountVal: 0,
            discountPct: 0,
            discountMode: 'pct',
            customerId: '',
            notes: '',
            applyTax: !!this.applyVatByDefault,
            applyWht: false
        });
        this.saveSessions();
        this.switchTab(id);
    },

    switchTab(id) {
        const current = this.sessions.find(s => s.id === this.activeSessionId);
        if (current && current.id !== id) {
            const custEl = document.getElementById('posCustomer');
            if (custEl) current.customerId = custEl.value;
            const notesEl = document.getElementById('posInvoiceNotes');
            if (notesEl) current.notes = notesEl.value;
            const taxEl = document.getElementById('posApplyTax');
            if (taxEl) current.applyTax = taxEl.checked;
            const whtEl = document.getElementById('posApplyWht');
            if (whtEl) current.applyWht = whtEl.checked;
        }

        const session = this.sessions.find(s => s.id === id);
        if (session) {
            this.activeSessionId = id;
            this.discountMode = session.discountMode || 'pct';
            const valInput = document.getElementById('globalDiscountVal');
            if (valInput) valInput.value = session.discountVal !== undefined ? session.discountVal : '0';
            const pctInput = document.getElementById('globalDiscountPct');
            if (pctInput) pctInput.value = session.discountPct !== undefined ? session.discountPct : '0';

            const custEl = document.getElementById('posCustomer');
            if (custEl) {
                custEl.value = session.customerId || '';
                if (typeof $ !== 'undefined' && $(custEl).hasClass('select2-hidden-accessible')) {
                    $(custEl).val(session.customerId || '').trigger('change.select2');
                }
            }

            const notesEl = document.getElementById('posInvoiceNotes');
            if (notesEl) notesEl.value = session.notes || '';

            const taxEl = document.getElementById('posApplyTax');
            if (taxEl) taxEl.checked = !!session.applyTax;

            const whtEl = document.getElementById('posApplyWht');
            if (whtEl) whtEl.checked = !!session.applyWht;

            this.saveSessions();
            this.renderTabs();
            this.applyCustomerPrices(session.customerId || '');
            this.updateCartDisplay(false);
            this.clearSalePriceDisplay();
            this.checkConsecutiveCustomerWarning(false);
            const txEl = document.getElementById('txNumber');
            if (txEl) txEl.textContent = id.toString().padStart(3, '0');
        }
    },

    closeTab(event, id) {
        if (event && event.stopPropagation) {
            event.stopPropagation();
        }
        if (this.sessions.length <= 1) {
            this.clearCart();
            return;
        }
        this.sessions = this.sessions.filter(s => s.id !== id);
        let nextId = this.activeSessionId;
        if (this.activeSessionId === id) {
            nextId = this.sessions[this.sessions.length - 1].id;
        }
        this.saveSessions();
        this.switchTab(nextId);
    },

    renderTabs() {
        const tabsContainer = document.getElementById('posTabs');
        if (!tabsContainer) return;
        
        tabsContainer.innerHTML = this.sessions.map(s => `
            <div class="pos-tab ${s.id === this.activeSessionId ? 'active' : ''}" onclick="POS.switchTab(${s.id})">
                ${s.name}
                ${this.sessions.length > 1 ? `<button type="button" class="pos-tab-close" onclick="POS.closeTab(event, ${s.id})">❌</button>` : ''}
            </div>
        `).join('') + `<button type="button" class="pos-tab-add" onclick="POS.createTab()" title="New Tab (F4)">+</button>`;
    },

    // ============================================================
    // KEYBOARD SHORTCUTS
    // ============================================================
    setupKeyboardShortcuts() {
        document.addEventListener('keydown', (e) => {
            // Skip if user is typing in a text input (except barcodeInput)
            const activeTag = document.activeElement?.tagName?.toLowerCase();
            const isBarcode = document.activeElement?.id === 'barcodeInput';
            const isTyping = ['textarea'].includes(activeTag) ||
                             (activeTag === 'input' && !isBarcode);

            switch (e.key) {
                case 'Enter':
                    if (e.ctrlKey) {
                        e.preventDefault();
                        this.processCashPayment();
                    }
                    break;
                case 'F1':
                    e.preventDefault();
                    this.voidTransaction();
                    break;
                case 'F2':
                    e.preventDefault();
                    this.holdTransaction();
                    break;
                case 'F3':
                    e.preventDefault();
                    this.reprintLast();
                    break;
                case 'F4':
                    e.preventDefault();
                    this.createTab();
                    break;
                case 'F7':
                    e.preventDefault();
                    this.processBankPayment();
                    break;
                case 'F8':
                    e.preventDefault();
                    this.processEWalletPayment();
                    break;
                case 'F9':
                    e.preventDefault();
                    this.processSplitPayment();
                    break;
                case 'F10':
                    e.preventDefault();
                    this.processCreditPayment();
                    break;
                case 'F11':
                    e.preventDefault();
                    this.processCashPayment();
                    break;
                case 'F12':
                    e.preventDefault();
                    this.processCardPayment();
                    break;
                case 'Escape':
                    e.preventDefault();
                    // Close any open modals
                    document.querySelectorAll('.modal').forEach(m => {
                        if (m.style.display !== 'none') m.style.display = 'none';
                    });
                    // Clear barcode input
                    const bi = document.getElementById('barcodeInput');
                    if (bi) { bi.value = ''; bi.focus(); }
                    break;
                case 'Delete':
                    if (!isTyping) {
                        e.preventDefault();
                        if (this.cart.length > 0) {
                            // Remove the selected product's cart item first (if any)
                            if (this.selectedProductRow) {
                                const selId = parseInt(this.selectedProductRow.dataset.productId);
                                const inCart = this.cart.find(i => i.id === selId);
                                if (inCart) {
                                    this.removeFromCart(selId);
                                    break;
                                }
                            }
                            // Fallback: remove last cart item
                            this.removeFromCart(this.cart[this.cart.length - 1].id);
                        }
                    }
                    break;
            }
        });
    },

    // ============================================================
    // RETURN MODE
    // ============================================================
    toggleReturnMode(forceState = null) {
        // POS checkout only creates SALE invoices now (server rejects RETURN_SALE for safety:
        // returns must be priced/validated server-side). Send the cashier to the returns screen.
        if (forceState === null && !this.isReturnMode) {
            if (confirm('المرتجعات تتم الآن من شاشة المرتجعات (مرتبطة بفاتورة أو مرتجع مستقل).\n\nهل تريد فتح شاشة المرتجعات؟')) {
                window.open('/invoicing/sales-returns/', '_blank');
            }
            return;
        }
        if (forceState !== null) {
            this.isReturnMode = forceState;
        } else {
            // Guard: if switching ON, require explicit confirmation
            if (!this.isReturnMode) {
                const confirmed = confirm(
                    '⚠️ تحذير: وضع المرتجع\n\n' +
                    'هذا الوضع يُنشئ فاتورة مرتجع مستقلة وتُعاد البضاعة للمخزون فوراً.\n\n' +
                    'للمرتجعات المرتبطة بفاتورة محددة، استخدم زر "إرجاع" داخل تفاصيل الفاتورة.\n\n' +
                    'هل تريد المتابعة في وضع المرتجع؟'
                );
                if (!confirmed) return;
            }
            this.isReturnMode = !this.isReturnMode;
        }

        const toggleBtn = document.getElementById('returnModeToggle');
        const navbar = document.querySelector('.pos-navbar');
        if (this.isReturnMode) {
            if (toggleBtn) toggleBtn.style.backgroundColor = 'var(--danger)';
            if (toggleBtn) toggleBtn.style.color = '#fff';
            if (navbar) navbar.style.borderBottom = '3px solid var(--danger)';
            this.showNotification('⚠️ وضع المرتجع مفعّل — البضاعة ستُعاد للمخزون', 'error');
        } else {
            if (toggleBtn) toggleBtn.style.backgroundColor = 'transparent';
            if (toggleBtn) toggleBtn.style.color = 'var(--danger)';
            if (navbar) navbar.style.borderBottom = '';
            if (forceState === null) {
                this.showNotification('تم إيقاف وضع المرتجع', 'info');
            }
        }
    },

    // ============================================================
    // VOID TRANSACTION
    // ============================================================
    voidTransaction() {
        if (this.cart.length === 0) {
            this.showNotification('السلة فارغة', 'info');
            return;
        }
        if (confirm('⚠ تأكيد إلغاء المعاملة الحالية؟\nسيتم حذف جميع الأصناف من السلة.')) {
            this.clearCart();
            this.showNotification('✓ تم إلغاء المعاملة', 'info');
        }
    },

    // ============================================================
    // HOLD TRANSACTION
    // ============================================================
    holdTransaction() {
        if (this.cart.length === 0) {
            this.showNotification('السلة فارغة', 'warning');
            return;
        }
        const name = prompt('اسم أو رقم المعاملة المحتجزة:') || `Hold-${Date.now()}`;
        this.heldTransactions.push({
            id: Date.now(),
            name: name,
            cart: JSON.parse(JSON.stringify(this.cart)),
            time: new Date().toISOString()
        });
        this.clearCart();
        this.showNotification(`✓ تم حفظ المعاملة: ${name}`, 'success');
    },

    // ============================================================
    // RECEIPT PRINTING & PAPER SIZE MANAGEMENT
    // ============================================================
    getReceiptPaperSize() {
        let saved = null;
        try { saved = localStorage.getItem('pos_receipt_paper_size'); } catch (e) { /* ignore */ }
        return saved || this.receiptPaperWidth || '80mm';
    },

    setReceiptPaperSize(size) {
        localStorage.setItem('pos_receipt_paper_size', size);
        this.updateReceiptPaperSizeUI(size);
    },

    updateReceiptPaperSizeUI(size) {
        const activeSize = size || this.getReceiptPaperSize();
        const btns = document.querySelectorAll('#posReceiptSizeButtons .btn-paper-size');
        btns.forEach(btn => {
            if (btn.dataset.size === activeSize) {
                btn.classList.add('active');
            } else {
                btn.classList.remove('active');
            }
        });
    },

    reprintLast() {
        if (!this.lastInvoiceData || !this.lastInvoiceData.invoice_id) {
            this.showNotification('لا يوجد إيصال سابق للطباعة', 'warning');
            return;
        }
        this.printReceipt(this.lastInvoiceData.invoice_id);
    },

    printReceipt(invoiceId, size) {
        const paperSize = size || this.getReceiptPaperSize();
        let winWidth = 380, winHeight = 650;
        if (paperSize === 'a4') {
            winWidth = 980;
            winHeight = 900;
        } else if (paperSize === 'a5') {
            winWidth = 720;
            winHeight = 820;
        } else if (paperSize === '58mm') {
            winWidth = 320;
            winHeight = 600;
        }
        const win = window.open(`/invoicing/receipt/${invoiceId}/?size=${paperSize}`, 'receipt', `width=${winWidth},height=${winHeight},scrollbars=yes`);
        if (win) {
            win.focus();
        } else {
            this.showNotification('⚠️ المتصفح منع نافذة الطباعة — اسمح بالنوافذ المنبثقة لهذا الموقع', 'warning');
        }
    },


    // ============================================================
    // CONSECUTIVE CUSTOMER WARNING
    // ============================================================
    checkConsecutiveCustomerWarning(triggerToast = false) {
        const custSelect = document.getElementById('posCustomer');
        const custId = custSelect ? custSelect.value : '';
        const badge = document.getElementById('posSameCustomerBadge');
        const notice = document.getElementById('posSameCustomerNotice');
        const badgeText = document.getElementById('posSameCustomerText');
        const noticeText = document.getElementById('posSameCustomerNoticeText');

        if (!custId || !this.lastSaleCustomerId) {
            if (badge) badge.style.display = 'none';
            if (notice) notice.style.display = 'none';
            return;
        }

        const isSameCustomer = (String(custId) === String(this.lastSaleCustomerId));

        if (isSameCustomer) {
            const custName = this.lastSaleCustomerName || (custSelect.options[custSelect.selectedIndex]?.text?.trim()) || 'نفس العميل';
            const invNum = this.lastSaleInvoiceNumber ? `(#${this.lastSaleInvoiceNumber})` : '';
            const msg = `تنبيه: آخر فاتورة بيع مسجلة ${invNum} كانت لنفس العميل (${custName})`;

            if (badge) {
                if (badgeText) badgeText.textContent = `آخر فاتورة لنفس العميل ${invNum}`;
                badge.style.display = 'inline-flex';
            }
            if (notice) {
                if (noticeText) noticeText.textContent = msg;
                notice.style.display = 'block';
            }

            if (triggerToast) {
                this.showNotification(`⚠️ ${msg}`, 'warning');
            }
        } else {
            if (badge) badge.style.display = 'none';
            if (notice) notice.style.display = 'none';
        }
    },

    // ============================================================
    // NOTIFICATIONS (TOASTS)
    // ============================================================
    showNotification(msg, type = 'info') {
        let container = document.getElementById('posToastContainer');
        if (!container) {
            container = document.createElement('div');
            container.id = 'posToastContainer';
            container.className = 'pos-toast-container';
            document.body.appendChild(container);
        }

        const toast = document.createElement('div');
        toast.className = `pos-toast ${type}`;
        toast.textContent = msg;
        container.appendChild(toast);

        // Auto remove after 3 seconds
        setTimeout(() => {
            toast.style.transition = 'all 0.3s ease';
            toast.style.opacity = '0';
            toast.style.transform = 'translateY(-10px)';
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    },

    // ============================================================
    // LOAD PRODUCTS (initial or refresh)
    // ============================================================
    loadProducts() {
        // Products are loaded via Django template context
        // This method is available for AJAX refresh if needed
        const params = new URLSearchParams({
            warehouse_id: this.currentWarehouseId || '',
            branch_id: this.currentBranchId || ''
        });

        fetch(`/inventory/api/products/?${params}`)
            .then(r => r.json())
            .then(data => {
                if (data.products) {
                    this.renderProductsTable(data.products);
                    const tbody = document.getElementById('productsTableBody');
                    if (tbody && !this.initialProductsHtml && data.products.length > 0) {
                        this.initialProductsHtml = tbody.innerHTML;
                    }
                }
            })
            .catch(() => {
                // Silently fail - Django template already rendered products
            });
    },

    renderProductsTable(products) {
        const tbody = document.getElementById('productsTableBody');
        if (!tbody) return;
        const showCost = this.showCost !== false;

        if (products.length === 0) {
            tbody.innerHTML = `<tr><td colspan="${showCost ? 7 : 6}" style="text-align:center;padding:2rem;color:#94a3b8;">لا توجد منتجات</td></tr>`;
            return;
        }

        tbody.innerHTML = products.map(p => {
            const pidStr = String(p.id);
            const customPriceObj = (this.currentCustomerPrices && this.currentCustomerPrices[pidStr]) ? this.currentCustomerPrices[pidStr] : null;
            const effectivePrice = customPriceObj ? customPriceObj.price : p.sale_price;
            const badgeHtml = customPriceObj ? `<span class="customer-price-badge" style="font-size:0.65rem; background:rgba(0,229,255,0.15); color:${customPriceObj.badge_color || '#00e5ff'}; border:1px solid ${customPriceObj.badge_color || '#00e5ff'}; padding:1px 4px; border-radius:3px; margin-right:2px; font-weight:700;">${customPriceObj.badge_text || ''}</span>` : '';
            const safeName = (p.name || '').replace(/"/g, '&quot;');
            return `
            <tr class="product-row"
                data-product-id="${p.id}"
                data-product-name="${safeName}"
                data-product-type="${p.product_type || 'PRODUCT'}"
                data-is-open-price="${p.is_open_price ? 'true' : 'false'}"
                data-product-barcode="${p.barcode || ''}"
                data-sale-price="${effectivePrice}"
                data-original-sale-price="${p.sale_price}"
                data-last-purchase="${p.last_purchase_price || 0}"
                data-last-sale="${p.last_sale_price || p.sale_price || 0}"
                data-available-stock="${p.available_stock || 0}"
                data-allow-negative-stock="${p.allow_negative_stock ? 'true' : 'false'}"
                data-tax-rate="${p.tax_rate || 0}"
                data-wht-rate="${p.wht_rate || p.withholding_tax_rate || 0}"
                data-uoms='${JSON.stringify(p.uoms || [])}'
                data-avg-cost="${p.avg_cost || p.last_purchase_price || 0}">
                <td class="td-product">
                    <div style="display:inline-flex; align-items:center; gap:10px;">
                        ${p.image ? `<img src="${p.image}" class="product-thumb" alt="${p.name}">` :
                          `<div class="product-thumb-placeholder">📦</div>`}
                        <div>
                            <div class="product-name">${p.name}</div>
                            ${p.barcode ? `<span class="product-barcode-tag en">${p.barcode}</span>` : ''}
                        </div>
                    </div>
                </td>
                ${showCost ? `<td class="cost-col en">${parseFloat(p.last_purchase_price || 0).toFixed(2)} ج.م</td>` : ''}
                <td class="last-sale-col en">${parseFloat(p.last_sale_price || p.sale_price || 0).toFixed(2)} ج.م</td>
                <td class="price-col en">${parseFloat(effectivePrice).toFixed(2)} ج.م ${badgeHtml}</td>
                <td class="stock-cell">${p.available_stock || 0}</td>
                <td>${p.unit_display || p.unit || '-'}</td>
                <td>
                    <div class="qty-control" style="justify-content:center;">
                        <button class="qty-btn" onclick="event.stopPropagation(); POS.updateQuantityForProduct(event, ${p.id}, -1)">−</button>
                        <span class="qty-value">1</span>
                        <button class="qty-btn" onclick="event.stopPropagation(); POS.updateQuantityForProduct(event, ${p.id}, 1)">+</button>
                    </div>
                </td>
            </tr>`;
        }).join('');

        this.updateGridStockDisplay();
    },

    async refreshShiftStats() {
        try {
            const resp = await fetch('/invoicing/pos/api/shift-stats/');
            if (!resp.ok) return;
            const data = await resp.json();
            if (!data.has_open_shift) return;

            const salesEl = document.getElementById('statTotalSales');
            if (salesEl) salesEl.textContent = `${parseFloat(data.total_sales).toFixed(2)} ج.م`;

            const countEl = document.getElementById('statInvoiceCount');
            if (countEl) countEl.textContent = data.invoices_count;

            const cashEl = document.getElementById('statCashInDrawer');
            if (cashEl) cashEl.textContent = `${parseFloat(data.cash_in_drawer).toFixed(2)} ج.م`;

            const cardEl = document.getElementById('statCardSales');
            if (cardEl) cardEl.textContent = `${parseFloat(data.card_sales).toFixed(2)} ج.م`;

            const feedEl = document.getElementById('recentInvoicesFeed');
            if (feedEl && Array.isArray(data.recent_invoices)) {
                if (data.recent_invoices.length === 0) {
                    feedEl.innerHTML = '<span id="noRecentInvoicesMsg" style="font-size:0.72rem; color:#64748b;">لا توجد فواتير بعد في هذه الوردية</span>';
                } else {
                    feedEl.innerHTML = data.recent_invoices.map(inv => `
                        <div class="recent-inv-chip" style="background:#1e293b; border:1px solid #334155; border-radius:6px; padding:3px 8px; display:flex; align-items:center; gap:6px; white-space:nowrap; font-size:0.75rem;">
                            <span style="font-weight:700; color:#f1f5f9;">${inv.number}</span>
                            <span style="color:#10b981; font-weight:700;">${parseFloat(inv.total).toFixed(2)}</span>
                            <span style="color:#94a3af; font-size:0.68rem;">(${inv.payment_type})</span>
                            <button type="button" onclick="POS.printReceipt(${inv.id})" style="background:transparent; border:none; color:#00e5ff; cursor:pointer; padding:0 2px;" title="إعادة طباعة">🖨️</button>
                        </div>
                    `).join('');
                }
            }
        } catch (err) {
            console.error('Failed to refresh shift stats:', err);
        }
    },

    toggleShiftBar() {
        const content = document.getElementById('shiftBarContent');
        const btn = document.getElementById('toggleShiftBarBtn');
        if (!content || !btn) return;
        if (content.style.display === 'none') {
            content.style.display = 'flex';
            btn.textContent = '▼';
        } else {
            content.style.display = 'none';
            btn.textContent = '▲';
        }
    }
};

// ============================================================
// CSRF TOKEN HELPER
// ============================================================
function getCsrfToken() {
    // Try meta tag
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.content;

    // Try hidden input
    const input = document.querySelector('[name=csrfmiddlewaretoken]');
    if (input) return input.value;

    // Try cookie
    const cookie = document.cookie
        .split(';')
        .find(c => c.trim().startsWith('csrftoken='));
    return cookie ? cookie.split('=')[1].trim() : '';
}

// ============================================================
// DOM READY
// ============================================================
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        POS.init();
    });
} else {
    POS.init();
}

// Expose globally
window.POS = POS;
window.getCsrfToken = getCsrfToken;
