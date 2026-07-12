'use strict';

/* ============================================================
   EasySupermarket POS System
   Complete Point of Sale JavaScript
   ============================================================ */
const POS = {
    sessions: [{id: 1, name: 'فاتورة 1', cart: []}],
    activeSessionId: 1,
    nextSessionId: 2,
    
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
            }
        } catch(e) { /* ignore */ }
    },
    
    currentWarehouseId: null,
    currentBranchId: null,
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
        
        console.log('[POS] Initialized. Warehouse:', this.currentWarehouseId, 'Branch:', this.currentBranchId);
    },

    // ============================================================
    // DATE/TIME (POS navbar display)
    // ============================================================
    updateDateTime() {
        const now = new Date();
        const el = document.getElementById('posDateTime');
        const cartEl = document.getElementById('cartDate');
        if (!el && !cartEl) return;

        const timeStr = now.toLocaleTimeString('en-US', { hour12: false });
        const dateStr = now.toLocaleDateString('en-GB', { day: '2-digit', month: '2-digit', year: 'numeric' });

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
            available_stock: parseFloat(cleanNumber(row.dataset.availableStock)) || 0,
            tax_rate: parseFloat(cleanNumber(row.dataset.taxRate)) || 0,
            wht_rate: parseFloat(cleanNumber(row.dataset.whtRate)) || 0,
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
        // Validate stock
        const stock = parseInt(product.available_stock) || 0;
        const isService = product.product_type === 'SERVICE';
        const existing = this.cart.find(item => item.id === parseInt(product.id));

        if (existing) {
            const newQty = existing.quantity + quantity;
            if (!isService && stock > 0 && newQty > stock) {
                this.showNotification(`⚠ الكمية المطلوبة (${newQty}) تتجاوز المخزون المتاح (${stock})`, 'warning');
                return;
            }
            existing.quantity = newQty;
        } else {
            if (!isService && stock === 0) {
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
                uoms: product.uoms || [{id:'base', name:'Unit'}],
                uom_id: 'base'
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
        if (item.product_type !== 'SERVICE' && qty > item.available_stock && item.available_stock > 0) {
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
            this.saveSessions();
            this.updateCartDisplay();
        }
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
                if (item.product_type !== 'SERVICE' && POS.priceMarginPercent > 0) {
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
        
        const globalDiscountInput = document.getElementById('globalDiscountPct');
        const globalDiscountPct = globalDiscountInput ? Math.max(0, Math.min(100, parseFloat(globalDiscountInput.value) || 0)) : 0;

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
    updateCartDisplay() {
        const cartItems = document.getElementById('cartItems');
        if (!cartItems) return;

        const applyTaxCheckbox = document.getElementById('posApplyTax');
        const applyTax = applyTaxCheckbox ? applyTaxCheckbox.checked : false;

        const applyWhtCheckbox = document.getElementById('posApplyWht');
        const applyWht = applyWhtCheckbox ? applyWhtCheckbox.checked : false;

        const globalDiscountInput = document.getElementById('globalDiscountPct');
        const globalDiscountPct = globalDiscountInput ? Math.max(0, Math.min(100, parseFloat(globalDiscountInput.value) || 0)) : 0;

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
                return `
                <div class="cart-item" data-cart-id="${item.id}" style="display:grid; grid-template-columns: 2fr 1fr 1.5fr 1fr 1fr; gap: 5px; align-items:center; border-bottom: 1px solid var(--border); padding-bottom: 10px; margin-bottom: 10px;">
                    <div>
                        <div class="cart-item-name">${item.name}</div>
                        <div class="cart-item-barcode" style="font-size:0.8em; color:var(--text-muted);">${item.barcode}</div>
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

        if (priceEl) priceEl.textContent = parseFloat(product.sale_price).toFixed(2);
        if (qtyEl) {
            const cartItem = this.cart.find(i => i.id === parseInt(product.id));
            qtyEl.textContent = cartItem ? cartItem.quantity : 1;
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
                stockCell.textContent = Math.max(0, originalStock - reserved);
                // Show active session's cart qty in the grid row
                if (qtyValue) {
                    const activeCartItem = this.cart.find(i => i.id === productId);
                    qtyValue.textContent = activeCartItem ? activeCartItem.quantity : 1;
                }
            }
        });
    },

    clearSalePriceDisplay() {
        ['displayPrice', 'displayQty', 'displayStock'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.textContent = '-';
        });
    },

    // ============================================================
    // CART EVENTS (row click delegation)
    // ============================================================
    setupCartEvents() {
        const tbody = document.getElementById('productsTableBody');
        if (!tbody) return;

        // Prevent duplicate listeners
        if (tbody._posEventsAttached) return;
        tbody._posEventsAttached = true;

        tbody.addEventListener('click', (e) => {
            // Don't trigger on quantity buttons
            if (e.target.closest('.qty-btn')) return;
            if (e.target.closest('.qty-control')) return;

            const row = e.target.closest('.product-row');
            if (!row) return;

            const product = this.extractProductFromRow(row);
            this.addToCart(product, 1);
            this.highlightRow(row);
        });
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
        if (confirm('تأكيد الدفع بالبطاقة؟')) {
            await this.completeSale('CARD');
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
            this.showNotification('⚠️ الرجاء اختيار الحساب البنكي من القائمة العلوية أولاً', 'error');
            return;
        }

        if (confirm('تأكيد التحويل البنكي؟')) {
            await this.completeSale('BANK_TRANSFER', null, null, bankId);
        }
    },

    // ============================================================
    // COMPLETE SALE
    // ============================================================
    async completeSale(paymentType, cashReceived = null, ewalletId = null, bankAccountId = null) {
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

        const globalDiscountInput = document.getElementById('globalDiscountPct');
        const globalDiscountPct = globalDiscountInput ? Math.max(0, Math.min(100, parseFloat(globalDiscountInput.value) || 0)) : 0;

        const payload = {
            cart: this.cart.map(item => ({
                product_id: item.id,
                quantity: item.quantity,
                unit_price: item.price,
                discount_percent: item.discount,
                tax_rate: applyTax ? item.tax_rate : 0,
                wht_rate: applyWht ? item.wht_rate : 0,
                uom_id: item.uom_id
            })),
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
            cash_received: cashReceived
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
            } else {
                throw new Error(data.error || 'فشل في إتمام البيع');
            }
        } catch (err) {
            this.showNotification(`❌ خطأ: ${err.message}`, 'error');
        }
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
                <div style="font-size:1rem;font-weight:800;">ميزا MIRA MARKET</div>
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

    // ============================================================
    // CLEAR CART
    // ============================================================
    clearCart() {
        this.cart = [];
        this.updateCartDisplay();
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
        this.sessions.push({id: id, name: `فاتورة ${id}`, cart: []});
        this.saveSessions();
        this.switchTab(id);
    },

    switchTab(id) {
        if (this.sessions.find(s => s.id === id)) {
            this.activeSessionId = id;
            this.saveSessions();
            this.renderTabs();
            this.updateCartDisplay();
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
                    this.processBankPayment();
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
        if (!tbody || products.length === 0) return;

        tbody.innerHTML = products.map(p => `
            <tr class="product-row"
                data-product-id="${p.id}"
                data-product-name="${p.name}"
                data-product-barcode="${p.barcode || ''}"
                data-sale-price="${p.sale_price}"
                data-available-stock="${p.available_stock || 0}"
                data-tax-rate="${p.tax_rate || 0}"
                data-uoms='${JSON.stringify(p.uoms || [])}'>
                <td>
                    ${p.image ? `<img src="${p.image}" class="product-thumb" alt="${p.name}">` :
                      `<div class="product-thumb-placeholder">📦</div>`}
                </td>
                <td>${p.name}</td>
                <td class="en">${p.barcode || '-'}</td>
                <td>
                    <div class="qty-control">
                        <button class="qty-btn" onclick="event.stopPropagation(); POS.updateQuantityForProduct(event, ${p.id}, -1)">−</button>
                        <span class="qty-value">1</span>
                        <button class="qty-btn" onclick="event.stopPropagation(); POS.updateQuantityForProduct(event, ${p.id}, 1)">+</button>
                    </div>
                </td>
                <td class="stock-cell">${p.available_stock || 0}</td>
                <td class="price-col en">${parseFloat(p.sale_price).toFixed(2)} EGP</td>
                <td>-</td>
                <td class="en">${parseFloat(p.sale_price).toFixed(2)} EGP</td>
                <td>${p.unit_display || p.unit || '-'}</td>
            </tr>`).join('');

        // Reset flag so events re-attach after dynamic render
        const tbody2 = document.getElementById('productsTableBody');
        if (tbody2) tbody2._posEventsAttached = false;
        this.setupCartEvents();
        this.updateGridStockDisplay();
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
