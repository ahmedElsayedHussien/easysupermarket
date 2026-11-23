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
        } catch(e) { /* ignore */ }
    },

    restoreSessions() {
        try {
            const saved = sessionStorage.getItem('pos_sessions');
            if (saved) {
                this.sessions = JSON.parse(saved);
                this.activeSessionId = parseInt(sessionStorage.getItem('pos_activeSessionId')) || this.sessions[0]?.id || 1;
                this.nextSessionId = parseInt(sessionStorage.getItem('pos_nextSessionId')) || 2;
                if (this.sessions.length === 0) {
                    this.sessions = [{id: 1, name: 'فاتورة 1', cart: []}];
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
                }
            }
        } catch(e) { /* ignore */ }
    },

    clearPOSCache() {
        try {
            sessionStorage.removeItem('pos_sessions');
            sessionStorage.removeItem('pos_activeSessionId');
            sessionStorage.removeItem('pos_nextSessionId');
            this.sessions = [{id: 1, name: 'فاتورة 1', cart: []}];
            this.activeSessionId = 1;
            this.nextSessionId = 2;
        } catch(e) { /* ignore */ }
    },
    
    currentWarehouseId: null,
    currentBranchId: null,
    currentShiftId: null,
    transactionNumber: '001',
    selectedProductRow: null,
    heldTransactions: [],
    lastInvoiceData: null,
    isReturnMode: false,

    // ============================================================
    // INIT
    // ============================================================
    init() {
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
        
        const applyTaxCheckbox = document.getElementById('posApplyTax');
        if (applyTaxCheckbox) {
            applyTaxCheckbox.addEventListener('change', () => {
                this.updateCartDisplay();
            });
        }
        
        const posCustomerSelect = document.getElementById('posCustomer');
        if (posCustomerSelect) {
            const handleCustomerChange = () => {
                const whtCheckbox = document.getElementById('posApplyWht');
                const opt = posCustomerSelect.options[posCustomerSelect.selectedIndex];
                if (whtCheckbox && opt) {
                    whtCheckbox.checked = (opt.dataset.whtSubject === 'true');
                }
                this.updateCartDisplay();
            };
            
            // using jQuery if Select2 is used, otherwise native event
            if (typeof $ !== 'undefined' && $(posCustomerSelect).hasClass('select2-hidden-accessible')) {
                $(posCustomerSelect).on('change', handleCustomerChange);
            } else {
                posCustomerSelect.addEventListener('change', handleCustomerChange);
            }
        }
        
        this.setupPersistedHeaders();

        window.addEventListener('pageshow', (event) => {
            if (event.persisted) {
                window.location.reload();
            }
        });

        console.log('[POS] Initialized. Warehouse:', this.currentWarehouseId, 'Branch:', this.currentBranchId);
    },

    // ============================================================
    // PERSISTED HEADERS (Branch, Warehouse, Bank, Safe, Wallet, Customer)
    // ============================================================
    setupPersistedHeaders() {
        const urlParams = new URLSearchParams(window.location.search);
        const userSuffix = `_${this.currentUserId || 'default'}`;
        
        // Reset saved customer if a new shift was opened
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
            const hasUrlParams = urlParams.has('branch_id') || urlParams.has('warehouse_id');
            const savedBranch = localStorage.getItem(`pos_saved_branch${userSuffix}`);
            const savedWarehouse = localStorage.getItem(`pos_saved_warehouse${userSuffix}`);
            
            if (!hasUrlParams && savedBranch) {
                // If no URL params but we have saved config, redirect immediately to apply them
                let redirectUrl = '?branch_id=' + savedBranch;
                if (savedWarehouse) redirectUrl += '&warehouse_id=' + savedWarehouse;
                window.location.href = redirectUrl;
                return; // Stop execution to let redirect happen
            } else {
                // Save current URL state to localStorage
                localStorage.setItem(`pos_saved_branch${userSuffix}`, branchSelect.value);
                localStorage.setItem(`pos_saved_warehouse${userSuffix}`, warehouseSelect.value);
                
                branchSelect.addEventListener('change', function() {
                    localStorage.setItem(`pos_saved_branch${userSuffix}`, this.value);
                    localStorage.removeItem(`pos_saved_warehouse${userSuffix}`); // Reset warehouse on branch change
                });
                warehouseSelect.addEventListener('change', function() {
                    localStorage.setItem(`pos_saved_warehouse${userSuffix}`, this.value);
                });
            }
        }

        // 2. Treasury, Bank, Ewallet, Customer
        const selectsToPersist = [
            { id: 'posTreasury', key: `pos_saved_treasury${userSuffix}` },
            { id: 'posBank', key: `pos_saved_bank${userSuffix}` },
            { id: 'posEwallet', key: `pos_saved_ewallet${userSuffix}` },
            { id: 'posCustomer', key: `pos_saved_customer${userSuffix}`, isSelect2: true }
        ];

        selectsToPersist.forEach(item => {
            const selectEl = document.getElementById(item.id);
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
                } else if ((!selectEl.value || selectEl.value === "") && selectEl.options.length > 1 && item.id !== 'posCustomer') {
                    // Fallback to Django pre-selected option or first real option
                    const selectedOpt = Array.from(selectEl.options).find(opt => opt.hasAttribute('selected') && opt.value);
                    if (selectedOpt) {
                        selectEl.value = selectedOpt.value;
                    } else if (selectEl.options[1] && selectEl.options[1].value) {
                        selectEl.value = selectEl.options[1].value;
                    }
                } else if (item.id === 'posCustomer') {
                    // Default to cash customer ("") if no saved value
                    selectEl.value = "";
                    if (item.isSelect2 && typeof $ !== 'undefined') {
                        $(selectEl).trigger('change');
                    } else {
                        selectEl.dispatchEvent(new Event('change'));
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
            const resp = await fetch(`/invoicing/api/product/barcode/?barcode=${encodeURIComponent(barcode)}&warehouse_id=${this.currentWarehouseId || ''}`);
            if (!resp.ok) throw new Error('Network error');
            const data = await resp.json();
            if (data.found) {
                this.addToCart(data.product, 1);
                this.showNotification(`✓ تمت الإضافة: ${data.product.name}`, 'success');
            } else {
                this.showNotification('❌ صنف غير موجود: ' + barcode, 'error');
            }
        } catch (err) {
            // If API not available, try to find in the product table (offline)
            const row = document.querySelector(`[data-product-barcode="${barcode}"]`);
            if (row) {
                const product = this.extractProductFromRow(row);
                this.addToCart(product, 1);
                this.highlightRow(row);
                this.showNotification(`✓ تمت الإضافة: ${product.name}`, 'success');
            } else {
                this.showNotification('⚠ خطأ في الاتصال - تحقق من الإنترنت', 'error');
            }
        }
    },

    extractProductFromRow(row) {
        const cleanNumber = (val) => String(val || '0').replace(/,/g, '');
        return {
            id: parseInt(row.dataset.productId),
            name: row.dataset.productName,
            product_type: row.dataset.productType || 'PRODUCT',
            is_open_price: row.dataset.isOpenPrice === 'true',
            barcode: row.dataset.productBarcode,
            sale_price: cleanNumber(row.dataset.salePrice),
            last_purchase_price: cleanNumber(row.dataset.lastPurchase),
            last_sale_price: cleanNumber(row.dataset.lastSale),
            available_stock: parseFloat(cleanNumber(row.dataset.availableStock)) || 0,
            allow_negative_stock: row.dataset.allowNegativeStock === 'true',
            tax_rate: parseFloat(cleanNumber(row.dataset.taxRate)) || 0,
            wht_rate: parseFloat(cleanNumber(row.dataset.whtRate)) || 0,
            avg_cost: parseFloat(cleanNumber(row.dataset.avgCost)) || 0,
            uoms: JSON.parse(row.dataset.uoms || '[{"id":"base", "name":"Unit"}]')
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
    // PRODUCT SEARCH
    // ============================================================
    setupProductSearch() {
        const searchInput = document.getElementById('productSearch');
        if (!searchInput) return;

        let debounceTimer;
        searchInput.addEventListener('input', (e) => {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => this.filterProducts(e.target.value), 200);
        });

        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                searchInput.value = '';
                this.filterProducts('');
            }
        });
    },

    filterProducts(query) {
        const rows = document.querySelectorAll('#productsTableBody .product-row');
        const q = query.toLowerCase().trim();
        let visibleCount = 0;

        rows.forEach(row => {
            if (!q) {
                row.style.display = '';
                visibleCount++;
                return;
            }
            const name = (row.dataset.productName || '').toLowerCase();
            const barcode = (row.dataset.productBarcode || '').toLowerCase();
            const matches = name.includes(q) || barcode.includes(q);
            row.style.display = matches ? '' : 'none';
            if (matches) visibleCount++;
        });

        // Show empty state
        const emptyMsg = document.getElementById('productsEmptyMsg');
        if (emptyMsg) emptyMsg.style.display = visibleCount === 0 ? '' : 'none';
    },

    // ============================================================
    // CART MANAGEMENT
    // ============================================================
    addToCart(product, quantity = 1) {
        // SERIALIZED: show serial picker instead of direct add
        if (product.product_type === 'SERIALIZED') {
            this.showSerialPickerModal(product);
            return;
        }

        // Validate stock
        const stock = parseFloat(product.available_stock) || 0;
        const isService = product.product_type === 'SERVICE';
        const canNegative = POS.allowNegativeStock || product.allow_negative_stock || isService;

        // Use _uniqueKey for SERIALIZED items (never merge them)
        const existing = this.cart.find(item => 
            item._uniqueKey 
                ? item._uniqueKey === product._uniqueKey 
                : item.id === parseInt(product.id)
        );

        if (existing) {
            const newQty = existing.quantity + quantity;
            if (!canNegative && stock > 0 && newQty > stock) {
                this.showNotification(`⚠ الكمية المطلوبة (${newQty}) تتجاوز المخزون المتاح (${stock})`, 'warning');
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
            this.cart.push({
                id: parseInt(product.id),
                name: product.name,
                product_type: product.product_type || 'PRODUCT',
                barcode: product.barcode || '',
                price: parseFloat(product.sale_price) || 0,
                base_price: parseFloat(product.sale_price) || 0,
                quantity: parseInt(quantity) || 1,
                discount: 0,
                tax_rate: parseFloat(product.tax_rate) || 0,
                wht_rate: parseFloat(product.wht_rate) || 0,
                available_stock: stock,
                allow_negative_stock: product.allow_negative_stock || false,
                uoms: product.uoms || [{id:'base', name:'Unit', factor:1.0}],
                uom_id: 'base',
                base_cost: parseFloat(product.avg_cost) || parseFloat(product.sale_price) || 0,
                current_cost: parseFloat(product.avg_cost) || parseFloat(product.sale_price) || 0
            });
        }

        this.saveSessions();
        this.updateCartDisplay();
        this.updateSalePriceDisplay(product);
    },

    removeFromCart(productId) {
        this.cart = this.cart.filter(item => item.id !== parseInt(productId));
        this.saveSessions();
        this.updateCartDisplay();
        this.clearSalePriceDisplay();
    },

    updateQuantity(productId, newQty) {
        const item = this.cart.find(i => i.id === parseInt(productId));
        if (!item) return;

        const qty = parseInt(newQty);
        if (isNaN(qty) || qty <= 0) {
            if (confirm(`حذف "${item.name}" من السلة؟`)) {
                this.removeFromCart(productId);
            }
            return;
        }
        const canNegative = POS.allowNegativeStock || item.allow_negative_stock || item.product_type === 'SERVICE';
        if (!canNegative && qty > item.available_stock) {
            this.showNotification(`⚠ الحد الأقصى للمخزون: ${item.available_stock}`, 'warning');
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
        if (newQty <= 0) {
            this.removeFromCart(productId);
        } else {
            this.updateQuantity(productId, newQty);
        }
    },

    setDiscount(productId, discountPct) {
        const item = this.cart.find(i => i.id === parseInt(productId));
        if (item) {
            item.discount = Math.max(0, Math.min(100, parseFloat(discountPct) || 0));
            this.updateCartDisplay();
        }
    },

    updateUom(productId, uomId) {
        const item = this.cart.find(i => i.id === parseInt(productId));
        if (item) {
            item.uom_id = uomId;
            const selectedUom = item.uoms.find(u => u.id == uomId);
            const factor = selectedUom ? parseFloat(selectedUom.factor) || 1.0 : 1.0;
            item.price = item.base_price * factor;
            item.current_cost = (item.base_cost || 0) * factor;
            this.saveSessions();
            this.updateCartDisplay();
            // Update cost display in side panel if this product is selected
            const costEl = document.getElementById('displayCost');
            if (costEl) costEl.textContent = item.current_cost.toFixed(2);
        }
    },

    handlePriceInput(productId, inputEl) {
        const item = this.cart.find(i => i.id === parseInt(productId));
        if (!item) return;

        const val = parseFloat(inputEl.value);
        if (!isNaN(val) && val >= 0) {
            item.price = val;
        } else {
            item.price = 0;
        }

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
        set('modalTotal', this.formatCurrency(totals.total) + ' ج.م');
    },

    updateManualPrice(productId, newPrice) {
        const item = this.cart.find(i => i.id === parseInt(productId));
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
                const selectedUom = item.uoms && item.uoms.find(u => u.id == item.uom_id);
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
                return Math.min(100, (val / gross) * 100);
            }
            return 0;
        } else if (pctInput) {
            return Math.max(0, Math.min(100, parseFloat(pctInput.value) || 0));
        }
        return 0;
    },

    onDiscountValChange() {
        this.discountMode = 'val';
        const valInput = document.getElementById('globalDiscountVal');
        const pctInput = document.getElementById('globalDiscountPct');
        const val = Math.max(0, parseFloat(valInput ? valInput.value : 0) || 0);
        const gross = this.getCartGrossSubtotal();

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
        const pct = Math.max(0, Math.min(100, parseFloat(pctInput ? pctInput.value : 0) || 0));
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
            const pct = (val / gross) * 100;
            pctInput.value = pct > 0 ? (Math.round(pct * 100) / 100) : '0';
        } else if (pctInput && valInput) {
            const pct = Math.max(0, Math.min(100, parseFloat(pctInput.value) || 0));
            const val = gross * (pct / 100);
            valInput.value = val > 0 ? (Math.round(val * 100) / 100) : '0';
        }
    },

    // ============================================================
    // TOTALS CALCULATION
    // ============================================================
    calculateTotals() {
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
                const lineGross = item.price * item.quantity;
                const itemDiscount = lineGross * (item.discount / 100);
                let lineNet = lineGross - itemDiscount;
                const lineGlobalDiscount = lineNet * (globalDiscountPct / 100);
                lineNet = lineNet - lineGlobalDiscount;

                const effectiveTaxRate = applyTax ? item.tax_rate : 0;
                const effectiveWhtRate = applyWht ? item.wht_rate : 0;
                const lineTotal = lineNet + (lineNet * (effectiveTaxRate / 100)) - (lineNet * (effectiveWhtRate / 100));
                const selectedUom = item.uoms && item.uoms.find(u => u.id == item.uom_id);
                const uomFactor = selectedUom ? parseFloat(selectedUom.factor) || 1.0 : 1.0;
                const displayCost = ((item.base_cost || 0) * uomFactor).toFixed(2);
                const costLabel = item.product_type !== 'SERVICE' && item.base_cost > 0 
                    ? `<div style="font-size:0.75em; color:#f59e0b; margin-top:2px;">💰 تكلفة/وحدة: ${displayCost}</div>` 
                    : '';
                return `
                <div class="cart-item" data-cart-id="${item.id}" style="display:grid; grid-template-columns: 2fr 1fr 1.5fr 1fr 1fr; gap: 5px; align-items:center; border-bottom: 1px solid var(--border); padding-bottom: 10px; margin-bottom: 10px;">
                    <div>
                        <div class="cart-item-name">${item.name}</div>
                        <div class="cart-item-barcode" style="font-size:0.8em; color:var(--text-muted);">${item.barcode}</div>
                        ${costLabel}
                    </div>
                    <div class="cart-item-qty d-flex align-center gap-1">
                        <button class="qty-btn" onclick="POS.updateQuantity(${item.id}, ${item.quantity - 1})" style="padding:2px 6px;">−</button>
                        <span class="qty-value">${item.quantity}</span>
                        <button class="qty-btn" onclick="POS.updateQuantity(${item.id}, ${item.quantity + 1})" style="padding:2px 6px;">+</button>
                    </div>
                    <div>
                        <input type="number" 
                               value="${item.price}" 
                               style="width: 75px; padding: 4px; text-align: center; border: 1px solid ${POS.allowPriceEdit || item.product_type === 'SERVICE' ? '#0ea5e9' : '#ccc'}; border-radius: 4px; font-weight: bold; color: var(--text); background-color: ${POS.allowPriceEdit || item.product_type === 'SERVICE' ? '#fff' : '#f8f9fa'}; cursor: ${POS.allowPriceEdit || item.product_type === 'SERVICE' ? 'text' : 'not-allowed'}; box-shadow: ${POS.allowPriceEdit || item.product_type === 'SERVICE' ? '0 0 5px rgba(14,165,233,0.2)' : 'none'};"
                               onchange="POS.updateManualPrice(${item.id}, this.value)"
                               oninput="POS.handlePriceInput(${item.id}, this)"
                               ${POS.allowPriceEdit || item.product_type === 'SERVICE' ? '' : 'readonly'}
                               step="0.01" min="0" title="${POS.allowPriceEdit || item.product_type === 'SERVICE' ? 'تعديل السعر' : 'تعديل السعر مغلق من الإعدادات'}">
                        <div style="font-size: 0.8em; color: var(--text-muted);">Tax: ${effectiveTaxRate}% | WHT: ${effectiveWhtRate}%</div>
                    </div>
                    <div>
                        ${item.uoms && item.uoms.length > 1 ? `
                        <select onchange="POS.updateUom(${item.id}, this.value)" style="width:100%; padding:4px; font-size:0.9em; background:var(--surface); border:1px solid var(--border); border-radius:4px; color:var(--text);">
                            ${item.uoms.map(u => `<option value="${u.id}" ${item.uom_id == u.id ? 'selected' : ''}>${u.name}</option>`).join('')}
                        </select>
                        ` : `<span style="font-size:0.9em;">${item.uoms && item.uoms[0] ? item.uoms[0].name : 'Unit'}</span>`}
                    </div>
                    <div class="d-flex align-center gap-1" style="justify-content:space-between;">
                        <div class="cart-item-total" style="font-weight:bold;">${this.formatCurrency(lineTotal)}</div>
                        <button class="cart-item-remove" onclick="POS.removeFromCart(${item.id})" title="حذف" style="color:var(--danger); background:none; border:none; cursor:pointer; font-size:1.2em;">✕</button>
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
        set('modalTotal', this.formatCurrency(totals.total) + ' ج.م');

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

        if (priceEl) priceEl.textContent = parseFloat(product.sale_price).toFixed(2);
        if (qtyEl) {
            const cartItem = this.cart.find(i => i.id === parseInt(product.id));
            qtyEl.textContent = cartItem ? cartItem.quantity : 1;
            if (costEl && cartItem) {
                costEl.textContent = (cartItem.current_cost || cartItem.base_cost || 0).toFixed(2);
            } else if (costEl) {
                costEl.textContent = parseFloat(product.avg_cost || 0).toFixed(2);
            }
        }
        if (stockEl) stockEl.textContent = product.available_stock || 0;
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
                const canNegative = POS.allowNegativeStock || row.dataset.allowNegativeStock === 'true';
                if (canNegative) {
                    stockCell.textContent = remaining;
                    if (remaining < 0) {
                        stockCell.style.color = '#ff4d4f';
                        stockCell.style.fontWeight = 'bold';
                    } else {
                        stockCell.style.color = '';
                        stockCell.style.fontWeight = '';
                    }
                } else {
                    stockCell.textContent = Math.max(0, remaining);
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
            if (el) el.textContent = '-';
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
            this.addToCart(product, 1);
            this.highlightRow(row);
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
            const change = received - totals.total;
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
        const modal = document.getElementById('cashModal');
        if (modal) {
            const totals = this.calculateTotals();
            const totalEl = document.getElementById('modalTotal');
            if (totalEl) totalEl.textContent = `${this.formatCurrency(totals.total)} ج.م`;
            const cashInput = document.getElementById('cashReceived');
            if (cashInput) {
                cashInput.value = totals.total;
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

        if (received < totals.total) {
            this.showNotification(`⚠ المبلغ المدفوع (${this.formatCurrency(received)}) أقل من الإجمالي (${this.formatCurrency(totals.total)})`, 'error');
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

        const bankSelect = document.getElementById('posBank');
        const bankId = bankSelect ? bankSelect.value : null;

        if (!bankId) {
            this.showNotification('⚠️ الرجاء اختيار الحساب البنكي / ماكينة الفيزا من القائمة العلوية أولاً', 'error');
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
        
        const bankSelect = document.getElementById('posBank');
        const bankId = bankSelect ? bankSelect.value : null;

        if (!bankId) {
            this.showNotification('⚠️ الرجاء اختيار حساب البنك / إنستا من القائمة العلوية أولاً', 'error');
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
            if (cashInp) cashInp.value = totals.total;

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
        const remaining = total - totalPaid;

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

        const remaining = Math.max(0, total - otherTotal);
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

        if (Math.abs(sumPaid - total) > 0.05) {
            this.showNotification(`⚠️ إجمالي المبالغ المدخلة (${this.formatCurrency(sumPaid)}) لا يساوي إجمالي الفاتورة (${this.formatCurrency(total)})`, 'error');
            return;
        }

        const ewalletSelect = document.getElementById('posEwallet');
        const ewalletId = ewalletSelect ? ewalletSelect.value : null;

        const bankSelect = document.getElementById('posBank');
        const bankId = bankSelect ? bankSelect.value : null;

        if ((card > 0 || bank > 0) && !bankId) {
            this.showNotification('⚠️ الرجاء اختيار الحساب البنكي / ماكينة الفيزا من القائمة العلوية أولاً', 'error');
            return;
        }

        if (ewallet > 0 && !ewalletId) {
            this.showNotification('⚠️ الرجاء اختيار المحفظة الإلكترونية من القائمة العلوية أولاً', 'error');
            return;
        }

        const splitPayments = { cash, card, ewallet, bank, credit };

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
                // Update stock locally: subtract sold quantities from grid
                this.cart.forEach(item => {
                    const row = document.querySelector(`[data-product-id="${item.id}"]`);
                    if (row) {
                        const oldStock = parseFloat(row.dataset.availableStock) || 0;
                        row.dataset.availableStock = Math.max(0, oldStock - item.quantity);
                    }
                });
                this.showReceiptModal(data, totals, cashReceived);
                this.closeTab(null, this.activeSessionId);
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

        const change = cashReceived ? Math.max(0, cashReceived - (totals?.total || 0)) : 0;
        const itemsHtml = (data.items || this.cart).map(item => `
            <div class="receipt-item">
                <span>${item.name || item.product_name}</span>
                <span>${this.formatCurrency(item.total || (item.price * item.quantity))}</span>
            </div>`).join('');

        content.innerHTML = `
            <div class="receipt-header" style="text-align:center;margin-bottom:0.75rem;">
                <img src="/static/img/logo.jpg" alt="كاشيرك" style="height:45px;margin-bottom:4px;border-radius:6px;object-fit:contain;">
                <div style="font-size:1.1rem;font-weight:800;color:#1e293b;">كاشيرك Cashierak</div>
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

        modal.style.display = 'flex';
    },

    reprintLast() {
        if (this.lastInvoiceData) {
            this.showReceiptModal(this.lastInvoiceData);
            setTimeout(() => window.print(), 300);
        } else {
            this.showNotification('⚠️ لا توجد فاتورة سابقة لطباعتها في هذه الجلسة', 'warning');
        }
    },

    // ============================================================
    // CLEAR CART
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

        const session = this.sessions.find(s => s.id === this.activeSessionId);
        if (session) {
            session.discountVal = 0;
            session.discountPct = 0;
            session.discountMode = 'pct';
            this.saveSessions();
        }

        this.updateCartDisplay(false);
        this.clearSalePriceDisplay();
        // Clear product row selections
        document.querySelectorAll('.product-row.selected').forEach(r => r.classList.remove('selected'));
        this.selectedProductRow = null;
    },

    // ============================================================
    // TABS MANAGEMENT
    // ============================================================
    createTab() {
        const id = this.nextSessionId++;
        this.sessions.push({id: id, name: `فاتورة ${id}`, cart: [], discountVal: 0, discountPct: 0, discountMode: 'pct'});
        this.saveSessions();
        this.switchTab(id);
    },

    switchTab(id) {
        const session = this.sessions.find(s => s.id === id);
        if (session) {
            this.activeSessionId = id;
            this.discountMode = session.discountMode || 'pct';
            const valInput = document.getElementById('globalDiscountVal');
            if (valInput) valInput.value = session.discountVal !== undefined ? session.discountVal : '0';
            const pctInput = document.getElementById('globalDiscountPct');
            if (pctInput) pctInput.value = session.discountPct !== undefined ? session.discountPct : '0';

            this.saveSessions();
            this.renderTabs();
            this.updateCartDisplay(false);
            this.clearSalePriceDisplay();
            document.getElementById('txNumber').textContent = id.toString().padStart(3, '0');
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
        if (this.activeSessionId === id) {
            this.activeSessionId = this.sessions[this.sessions.length - 1].id;
        }
        this.saveSessions();
        this.renderTabs();
        this.updateCartDisplay();
        this.clearSalePriceDisplay();
        document.getElementById('txNumber').textContent = this.activeSessionId.toString().padStart(3, '0');
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
                        // Remove last cart item
                        if (this.cart.length > 0) {
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
    toggleReturnMode() {
        this.isReturnMode = !this.isReturnMode;
        const toggleBtn = document.getElementById('returnModeToggle');
        const navbar = document.querySelector('.pos-navbar');
        if (this.isReturnMode) {
            if (toggleBtn) toggleBtn.style.backgroundColor = 'var(--danger)';
            if (toggleBtn) toggleBtn.style.color = '#fff';
            if (navbar) navbar.style.borderBottom = '3px solid var(--danger)';
            this.showNotification('تم تفعيل وضع المرتجع', 'warning');
        } else {
            if (toggleBtn) toggleBtn.style.backgroundColor = 'transparent';
            if (toggleBtn) toggleBtn.style.color = 'var(--danger)';
            if (navbar) navbar.style.borderBottom = '';
            this.showNotification('تم إيقاف وضع المرتجع', 'info');
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
    reprintLast() {
        if (!this.lastInvoiceData || !this.lastInvoiceData.invoice_id) {
            this.showNotification('لا يوجد إيصال سابق للطباعة', 'warning');
            return;
        }
        this.printReceipt(this.lastInvoiceData.invoice_id);
    },

    printReceipt(invoiceId) {
        window.open(`/invoicing/receipt/${invoiceId}/`, 'receipt', 'width=350,height=600');
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
                }
            })
            .catch(() => {
                // Silently fail - Django template already rendered products
            });
    },

    renderProductsTable(products) {
        const tbody = document.getElementById('productsTableBody');
        if (!tbody) return;

        if (products.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2rem;">لا توجد منتجات</td></tr>';
            return;
        }

        tbody.innerHTML = products.map(p => `
            <tr class="product-row"
                data-product-id="${p.id}"
                data-product-name="${p.name}"
                data-product-barcode="${p.barcode || ''}"
                data-sale-price="${p.sale_price}"
                data-last-purchase="${p.last_purchase_price || 0}"
                data-last-sale="${p.last_sale_price || p.sale_price || 0}"
                data-available-stock="${p.available_stock || 0}"
                data-allow-negative-stock="${p.allow_negative_stock ? 'true' : 'false'}"
                data-tax-rate="${p.tax_rate || 0}"
                data-uoms='${JSON.stringify(p.uoms || [])}'>
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
                <td class="cost-col en">${parseFloat(p.last_purchase_price || 0).toFixed(2)} EGP</td>
                <td class="last-sale-col en">${parseFloat(p.last_sale_price || p.sale_price || 0).toFixed(2)} EGP</td>
                <td class="price-col en">${parseFloat(p.sale_price).toFixed(2)} EGP</td>
                <td class="stock-cell">${p.available_stock || 0}</td>
                <td>${p.unit_display || p.unit || '-'}</td>
                <td>
                    <div class="qty-control" style="justify-content:center;">
                        <button class="qty-btn" onclick="event.stopPropagation(); POS.updateQuantityForProduct(event, ${p.id}, -1)">−</button>
                        <span class="qty-value">1</span>
                        <button class="qty-btn" onclick="event.stopPropagation(); POS.updateQuantityForProduct(event, ${p.id}, 1)">+</button>
                    </div>
                </td>
            </tr>`).join('');

        // Reset flag so events re-attach after dynamic render
        const tbody2 = document.getElementById('productsTableBody');
        if (tbody2) tbody2._posEventsAttached = false;
        this.setupCartEvents();
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
document.addEventListener('DOMContentLoaded', () => {
    POS.init();
});

// Expose globally
window.POS = POS;
window.getCsrfToken = getCsrfToken;
