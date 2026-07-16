'use strict';

/* ============================================================
   EasySupermarket Inventory Management JavaScript
   FIFO Lot Tracking, Filters, Tooltips, Transfer Modal
   ============================================================ */

const Inventory = {

    // ============================================================
    // INIT
    // ============================================================
    init() {
        this.setupFilters();
        this.colorCodeFifoBadges();
        this.setupFifoTooltips();
        this.setupTransferModal();
        this.setupStockActions();
        this.initCategoryChart();
        console.log('[Inventory] Initialized.');
    },

    // ============================================================
    // FILTERS
    // ============================================================
    setupFilters() {
        // Search by barcode/name
        const searchInput = document.getElementById('inventorySearch');
        if (searchInput) {
            let debounce;
            searchInput.addEventListener('input', (e) => {
                clearTimeout(debounce);
                debounce = setTimeout(() => this.applyFilters(), 250);
            });
        }

        // Warehouse filter
        const warehouseSelect = document.getElementById('warehouseFilter');
        if (warehouseSelect) {
            warehouseSelect.addEventListener('change', () => this.applyFilters());
        }

        // Category filter
        const categorySelect = document.getElementById('categoryFilter');
        if (categorySelect) {
            categorySelect.addEventListener('change', () => this.applyFilters());
        }

        // Stock status checkboxes
        const statusChecks = document.querySelectorAll('[data-stock-status]');
        statusChecks.forEach(cb => {
            cb.addEventListener('change', () => this.applyFilters());
        });

        // FIFO batches toggle
        const fifoToggle = document.getElementById('showFifoBatches');
        if (fifoToggle) {
            fifoToggle.addEventListener('change', () => {
                const batchRows = document.querySelectorAll('.fifo-batch-row');
                batchRows.forEach(row => {
                    row.style.display = fifoToggle.checked ? '' : 'none';
                });
            });
        }

        // Reset filters button
        const resetBtn = document.getElementById('resetFilters');
        if (resetBtn) {
            resetBtn.addEventListener('click', () => this.resetFilters());
        }
    },

    applyFilters() {
        const query = (document.getElementById('inventorySearch')?.value || '').toLowerCase().trim();
        const warehouse = document.getElementById('warehouseFilter')?.value || '';
        const category = document.getElementById('categoryFilter')?.value || '';

        // Get selected status filters
        const statusChecks = document.querySelectorAll('[data-stock-status]:checked');
        const selectedStatuses = new Set(Array.from(statusChecks).map(c => c.dataset.stockStatus));

        const rows = document.querySelectorAll('#inventoryTableBody .inventory-row');
        let visibleCount = 0;

        rows.forEach(row => {
            let show = true;

            // Text search
            if (query) {
                const name = (row.dataset.productName || '').toLowerCase();
                const barcode = (row.dataset.productBarcode || '').toLowerCase();
                const sku = (row.dataset.productSku || '').toLowerCase();
                if (!name.includes(query) && !barcode.includes(query) && !sku.includes(query)) {
                    show = false;
                }
            }

            // Warehouse filter
            if (warehouse && row.dataset.warehouse !== warehouse) show = false;

            // Category filter
            if (category && row.dataset.category !== category) show = false;

            // Status filter
            if (selectedStatuses.size > 0) {
                const rowStatus = row.dataset.stockStatus || '';
                if (!selectedStatuses.has(rowStatus)) show = false;
            }

            row.style.display = show ? '' : 'none';
            if (show) visibleCount++;
        });

        // Update result count
        const countEl = document.getElementById('visibleCount');
        if (countEl) countEl.textContent = visibleCount;
    },

    resetFilters() {
        const searchInput = document.getElementById('inventorySearch');
        if (searchInput) searchInput.value = '';

        const warehouseSelect = document.getElementById('warehouseFilter');
        if (warehouseSelect) warehouseSelect.value = '';

        const categorySelect = document.getElementById('categoryFilter');
        if (categorySelect) categorySelect.value = '';

        document.querySelectorAll('[data-stock-status]').forEach(cb => {
            cb.checked = true;
        });

        this.applyFilters();
    },

    // ============================================================
    // FIFO AGING COLOR CODING
    // ============================================================
    colorCodeFifoBadges() {
        document.querySelectorAll('[data-fifo-days]').forEach(el => {
            const days = parseInt(el.dataset.fifoDays);
            el.classList.remove('fifo-badge-critical', 'fifo-badge-warning', 'fifo-badge-moderate', 'fifo-badge-ok');

            if (isNaN(days) || days < 0) {
                el.classList.add('fifo-badge-ok');
                return;
            }

            // 0-7 days: CRITICAL (red)
            if (days <= 7) {
                el.classList.add('fifo-badge-critical');
                el.title = `⛔ حرج: ${days} يوم متبقي - يحتاج مراجعة فورية`;
            }
            // 8-60 days: WARNING (yellow)
            else if (days <= 60) {
                el.classList.add('fifo-badge-warning');
                el.title = `⚠ تحذير: ${days} يوم متبقي`;
            }
            // 61-180 days: MODERATE (orange)
            else if (days <= 180) {
                el.classList.add('fifo-badge-moderate');
                el.title = `🟠 معتدل: ${days} يوم متبقي`;
            }
            // 180+ days: OK (green)
            else {
                el.classList.add('fifo-badge-ok');
                el.title = `✅ جيد: ${days} يوم متبقي`;
            }
        });

        // Also color expiry date cells
        document.querySelectorAll('[data-expiry-date]').forEach(el => {
            const expiry = new Date(el.dataset.expiryDate);
            if (isNaN(expiry.getTime())) return;

            const now = new Date();
            const diffDays = Math.ceil((expiry - now) / (1000 * 60 * 60 * 24));

            if (diffDays <= 0) {
                el.style.color = '#ff4757';
                el.style.fontWeight = '700';
                el.innerHTML += ' <span style="font-size:0.65rem;background:rgba(255,71,87,0.2);padding:0.1rem 0.3rem;border-radius:3px;">منتهي</span>';
            } else if (diffDays <= 7) {
                el.style.color = '#ff4757';
            } else if (diffDays <= 30) {
                el.style.color = '#fbbf24';
            } else if (diffDays <= 90) {
                el.style.color = '#ff8c42';
            } else {
                el.style.color = '#2ed573';
            }
        });
    },

    // ============================================================
    // FIFO TOOLTIPS (batch details on hover)
    // ============================================================
    setupFifoTooltips() {
        // Create tooltip element
        let tooltip = document.getElementById('fifoTooltip');
        if (!tooltip) {
            tooltip = document.createElement('div');
            tooltip.id = 'fifoTooltip';
            tooltip.style.cssText = `
                display:none; position:fixed; z-index:9000;
                background:#111827; border:1px solid #2a3f5f;
                border-radius:8px; padding:0.75rem; min-width:200px;
                box-shadow:0 8px 24px rgba(0,0,0,0.5);
                font-family:'Inter',sans-serif; font-size:0.78rem;
                pointer-events:none; color:#f1f5f9;
            `;
            document.body.appendChild(tooltip);
        }

        // Attach to batch info cells
        document.querySelectorAll('[data-fifo-batches]').forEach(el => {
            el.style.cursor = 'help';

            el.addEventListener('mouseenter', (e) => {
                let batchData;
                try {
                    batchData = JSON.parse(el.dataset.fifoBatches);
                } catch {
                    batchData = [];
                }

                if (!batchData.length) return;

                const html = batchData.map(b => `
                    <div style="display:flex;justify-content:space-between;gap:1rem;padding:0.2rem 0;border-bottom:1px solid #1e2d40;">
                        <span style="color:#94a3b8;">دفعة #${b.lot_number || b.id}</span>
                        <span style="color:#00e5ff;">الكمية: ${b.quantity}</span>
                        <span style="color:${this.getExpiryColor(b.expiry_date)};">${b.expiry_date || 'N/A'}</span>
                    </div>`).join('');

                tooltip.innerHTML = `
                    <div style="font-weight:700;color:#00e5ff;margin-bottom:0.5rem;border-bottom:1px solid #2a3f5f;padding-bottom:0.4rem;">
                        📦 تفاصيل الدفعات FIFO
                    </div>
                    ${html}`;
                tooltip.style.display = 'block';
            });

            el.addEventListener('mousemove', (e) => {
                tooltip.style.left = `${e.clientX + 12}px`;
                tooltip.style.top = `${e.clientY - 10}px`;

                // Adjust if near right edge
                const rect = tooltip.getBoundingClientRect();
                if (rect.right > window.innerWidth - 10) {
                    tooltip.style.left = `${e.clientX - rect.width - 12}px`;
                }
            });

            el.addEventListener('mouseleave', () => {
                tooltip.style.display = 'none';
            });
        });
    },

    getExpiryColor(dateStr) {
        if (!dateStr) return '#64748b';
        const days = Math.ceil((new Date(dateStr) - new Date()) / (1000 * 60 * 60 * 24));
        if (days <= 0) return '#ff4757';
        if (days <= 7) return '#ff4757';
        if (days <= 30) return '#fbbf24';
        if (days <= 90) return '#ff8c42';
        return '#2ed573';
    },

    // ============================================================
    // TRANSFER MODAL
    // ============================================================
    setupTransferModal() {
        const transferBtn = document.getElementById('transferStockBtn');
        const transferModal = document.getElementById('transferModal');
        const closeBtn = document.getElementById('closeTransferModal');
        const transferForm = document.getElementById('transferForm');

        if (transferBtn && transferModal) {
            transferBtn.addEventListener('click', () => {
                transferModal.style.display = 'flex';
                // Pre-fill source warehouse from filter
                const warehouseFilter = document.getElementById('warehouseFilter');
                const sourceSelect = document.getElementById('sourceWarehouse');
                if (warehouseFilter?.value && sourceSelect) {
                    sourceSelect.value = warehouseFilter.value;
                }
            });
        }

        if (closeBtn && transferModal) {
            closeBtn.addEventListener('click', () => {
                transferModal.style.display = 'none';
            });
        }

        // Close on overlay click
        if (transferModal) {
            transferModal.addEventListener('click', (e) => {
                if (e.target === transferModal) transferModal.style.display = 'none';
            });
        }

        // Source/Dest validation
        const sourceWarehouse = document.getElementById('sourceWarehouse');
        const destWarehouse = document.getElementById('destWarehouse');
        if (sourceWarehouse && destWarehouse) {
            [sourceWarehouse, destWarehouse].forEach(sel => {
                sel.addEventListener('change', () => {
                    if (sourceWarehouse.value === destWarehouse.value && sourceWarehouse.value) {
                        this.showNotification('لا يمكن التحويل إلى نفس المخزن', 'error');
                        destWarehouse.value = '';
                    }
                });
            });
        }

        // Transfer form submit
        if (transferForm) {
            transferForm.addEventListener('submit', async (e) => {
                e.preventDefault();
                await this.submitTransfer(transferForm);
            });
        }
    },

    async submitTransfer(form) {
        const formData = new FormData(form);
        const data = Object.fromEntries(formData.entries());

        if (!data.source_warehouse || !data.dest_warehouse) {
            this.showNotification('يرجى اختيار المخازن المصدر والوجهة', 'error');
            return;
        }

        if (!data.quantity || parseFloat(data.quantity) <= 0) {
            this.showNotification('يرجى إدخال كمية صحيحة', 'error');
            return;
        }

        try {
            const resp = await fetch('/inventory/api/transfer/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfTokenInventory()
                },
                body: JSON.stringify(data)
            });
            const result = await resp.json();
            if (result.success) {
                this.showNotification('✓ تم تحويل المخزون بنجاح', 'success');
                document.getElementById('transferModal').style.display = 'none';
                // Refresh the page to show updated stock
                setTimeout(() => location.reload(), 1500);
            } else {
                this.showNotification('❌ خطأ: ' + result.error, 'error');
            }
        } catch (err) {
            this.showNotification('❌ خطأ في الاتصال', 'error');
        }
    },

    // ============================================================
    // STOCK ACTIONS (Adjust, Mark Expired)
    // ============================================================
    setupStockActions() {
        // Adjust stock buttons
        document.querySelectorAll('[data-action="adjust-stock"]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const productId = btn.dataset.productId;
                const currentQty = btn.dataset.currentQty;
                const newQty = prompt(`ضبط الكمية لـ "${btn.dataset.productName}":\nالكمية الحالية: ${currentQty}`, currentQty);
                if (newQty !== null && !isNaN(parseFloat(newQty))) {
                    this.adjustStock(productId, parseFloat(newQty));
                }
            });
        });

        // Mark as expired buttons
        document.querySelectorAll('[data-action="mark-expired"]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (confirm('تأكيد تعليم هذه الدفعة كمنتهية الصلاحية؟')) {
                    this.markExpired(btn.dataset.lotId);
                }
            });
        });

        // Row click — show product details
        document.querySelectorAll('#inventoryTableBody .inventory-row').forEach(row => {
            row.addEventListener('click', () => {
                const productId = row.dataset.productId;
                if (productId) this.showProductMovement(productId);
            });
        });
    },

    async adjustStock(productId, newQty) {
        try {
            const resp = await fetch('/inventory/api/adjust/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfTokenInventory()
                },
                body: JSON.stringify({ product_id: productId, quantity: newQty })
            });
            const data = await resp.json();
            if (data.success) {
                this.showNotification('✓ تم ضبط الكمية بنجاح', 'success');
                setTimeout(() => location.reload(), 1200);
            } else {
                this.showNotification('❌ ' + data.error, 'error');
            }
        } catch {
            this.showNotification('❌ خطأ في الاتصال', 'error');
        }
    },

    async markExpired(lotId) {
        try {
            const resp = await fetch('/inventory/api/lot/expire/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfTokenInventory()
                },
                body: JSON.stringify({ lot_id: lotId })
            });
            const data = await resp.json();
            if (data.success) {
                this.showNotification('✓ تم تعليم الدفعة كمنتهية الصلاحية', 'success');
                setTimeout(() => location.reload(), 1200);
            } else {
                this.showNotification('❌ ' + data.error, 'error');
            }
        } catch {
            this.showNotification('❌ خطأ في الاتصال', 'error');
        }
    },

    async showProductMovement(productId) {
        const panel = document.getElementById('movementLog');
        if (!panel) return;

        panel.innerHTML = '<div style="color:#475569;padding:1rem;text-align:center;">جارٍ التحميل...</div>';

        try {
            const resp = await fetch(`/inventory/api/movement/?product_id=${productId}`);
            const data = await resp.json();

            if (!data.movements || data.movements.length === 0) {
                panel.innerHTML = '<div style="color:#475569;padding:1rem;text-align:center;">لا توجد حركات مسجلة</div>';
                return;
            }

            panel.innerHTML = data.movements.map(m => `
                <div style="display:flex;justify-content:space-between;align-items:center;padding:0.5rem;border-bottom:1px solid #1e2d40;font-size:0.75rem;">
                    <div>
                        <div style="font-weight:600;color:#f1f5f9;">${m.movement_type}</div>
                        <div style="color:#475569;">${m.date}</div>
                    </div>
                    <div style="font-family:'Inter',monospace;font-weight:700;color:${m.quantity > 0 ? '#2ed573' : '#ff4757'};">
                        ${m.quantity > 0 ? '+' : ''}${m.quantity}
                    </div>
                </div>`).join('');
        } catch {
            panel.innerHTML = '<div style="color:#ff4757;padding:1rem;text-align:center;">خطأ في تحميل البيانات</div>';
        }
    },

    // ============================================================
    // CATEGORY DONUT CHART
    // ============================================================
    initCategoryChart() {
        const canvas = document.getElementById('stockCategoryChart');
        if (!canvas || typeof Chart === 'undefined') return;

        // Read data from data attributes if available
        let labels = [], values = [], colors = [];
        try {
            labels = JSON.parse(canvas.dataset.labels || '[]');
            values = JSON.parse(canvas.dataset.values || '[]');
        } catch {}

        if (!labels.length) {
            labels = ['بقالة', 'خضروات', 'ألبان', 'مجمد', 'أخرى'];
            values = [35, 20, 18, 15, 12];
        }

        colors = [
            'rgba(255,140,66,0.7)',
            'rgba(163,230,53,0.7)',
            'rgba(0,229,255,0.7)',
            'rgba(59,130,246,0.7)',
            'rgba(100,116,139,0.7)'
        ];

        new Chart(canvas, {
            type: 'doughnut',
            data: {
                labels: labels,
                datasets: [{
                    data: values,
                    backgroundColor: colors,
                    borderColor: colors.map(c => c.replace('0.7', '1')),
                    borderWidth: 1.5
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '65%',
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: { color: '#94a3b8', padding: 8, font: { size: 10, family: "'Tajawal'" } }
                    }
                }
            }
        });
    },

    // ============================================================
    // NOTIFICATIONS
    // ============================================================
    showNotification(msg, type = 'info') {
        const container = document.querySelector('.messages-container') || (() => {
            const c = document.createElement('div');
            c.className = 'messages-container';
            document.body.appendChild(c);
            return c;
        })();

        const alert = document.createElement('div');
        alert.className = `alert alert-${type === 'error' ? 'danger' : type}`;
        alert.innerHTML = `${msg} <button class="alert-close" onclick="this.parentElement.remove()">×</button>`;
        container.appendChild(alert);

        setTimeout(() => {
            alert.style.transition = 'all 0.3s ease';
            alert.style.opacity = '0';
            setTimeout(() => alert.remove(), 300);
        }, 4000);
    }
};

// CSRF helper for inventory
function getCsrfTokenInventory() {
    const input = document.querySelector('[name=csrfmiddlewaretoken]');
    if (input) return input.value;
    const cookie = document.cookie.split(';').find(c => c.trim().startsWith('csrftoken='));
    return cookie ? cookie.split('=')[1].trim() : '';
}

document.addEventListener('DOMContentLoaded', () => Inventory.init());
window.Inventory = Inventory;
