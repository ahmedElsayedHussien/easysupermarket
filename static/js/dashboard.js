'use strict';

/* ============================================================
   EasySupermarket — Dashboard JavaScript
   Handles: Server time, Charts, Branch selector, Messages
   ============================================================ */

// ============================================================
// SERVER TIME CLOCK (real-time)
// ============================================================
function updateServerTime() {
    const now = new Date();
    const timeEl = document.getElementById('serverTime');
    const navTimeEl = document.getElementById('navServerTime');

    const timeStr = now.toLocaleTimeString('en-US', {
        hour12: false,
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });

    const dateStr = now.toLocaleDateString('en-US', {
        day: '2-digit',
        month: 'short',
        year: 'numeric',
        weekday: 'short'
    });

    const dayAr = ['الأحد', 'الاثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت'][now.getDay()];

    if (timeEl) {
        timeEl.textContent = timeStr;
    }
    if (navTimeEl) {
        navTimeEl.innerHTML = `<span class="font-english">${dateStr}</span> &nbsp;|&nbsp; <span class="font-english">${timeStr}</span> &nbsp;|&nbsp; <span>${dayAr}</span>`;
    }
}

// Start clock
setInterval(updateServerTime, 1000);
updateServerTime();

// ============================================================
// CHART.JS GLOBAL DEFAULTS (Dark Theme)
// ============================================================
function setChartDefaults() {
    if (typeof Chart === 'undefined') return;

    Chart.defaults.color = '#94a3b8';
    Chart.defaults.borderColor = '#1e2d40';
    Chart.defaults.font.family = "'Inter', sans-serif";
    Chart.defaults.font.size = 11;

    Chart.defaults.plugins.legend.labels.color = '#94a3b8';
    Chart.defaults.plugins.legend.labels.padding = 16;
    Chart.defaults.plugins.legend.labels.usePointStyle = true;

    Chart.defaults.plugins.tooltip.backgroundColor = '#111827';
    Chart.defaults.plugins.tooltip.titleColor = '#f1f5f9';
    Chart.defaults.plugins.tooltip.bodyColor = '#94a3b8';
    Chart.defaults.plugins.tooltip.borderColor = '#2a3f5f';
    Chart.defaults.plugins.tooltip.borderWidth = 1;
    Chart.defaults.plugins.tooltip.padding = 10;
    Chart.defaults.plugins.tooltip.cornerRadius = 8;
    Chart.defaults.plugins.tooltip.displayColors = true;
}

// ============================================================
// CHART INSTANCES (stored globally for updates)
// ============================================================
const DashCharts = {};

// ============================================================
// INITIALIZE DASHBOARD CHARTS
// ============================================================
function initDashboardCharts(salesData, salesLabels, profitData, branchStatusData) {
    if (typeof Chart === 'undefined') {
        console.warn('Chart.js not loaded yet');
        return;
    }

    setChartDefaults();

    // --- SALES LINE CHART ---
    const salesCtx = document.getElementById('salesChart');
    if (salesCtx) {
        if (DashCharts.sales) DashCharts.sales.destroy();
        DashCharts.sales = new Chart(salesCtx, {
            type: 'line',
            data: {
                labels: salesLabels || ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul'],
                datasets: [{
                    label: 'إجمالي المبيعات',
                    data: salesData || [120000, 145000, 138000, 162000, 155000, 180000, 195000],
                    borderColor: '#2ed573',
                    backgroundColor: 'rgba(46,213,115,0.08)',
                    borderWidth: 2,
                    pointBackgroundColor: '#2ed573',
                    pointBorderColor: '#111827',
                    pointRadius: 4,
                    pointHoverRadius: 6,
                    tension: 0.4,
                    fill: true
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => `${ctx.parsed.y.toLocaleString()} ج.م`
                        }
                    }
                },
                scales: {
                    x: {
                        grid: { color: '#1e2d40' },
                        ticks: { color: '#475569' }
                    },
                    y: {
                        grid: { color: '#1e2d40' },
                        ticks: {
                            color: '#475569',
                            callback: (val) => val.toLocaleString() + ' ج.م'
                        }
                    }
                }
            }
        });
    }

    // --- PROFIT MARGIN LINE CHART ---
    const profitCtx = document.getElementById('profitChart');
    if (profitCtx) {
        if (DashCharts.profit) DashCharts.profit.destroy();
        DashCharts.profit = new Chart(profitCtx, {
            type: 'line',
            data: {
                labels: salesLabels || ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul'],
                datasets: [{
                    label: 'هامش الربح %',
                    data: profitData || [22, 25, 23, 28, 26, 31, 29],
                    borderColor: '#fbbf24',
                    backgroundColor: 'rgba(251,191,36,0.08)',
                    borderWidth: 2,
                    pointBackgroundColor: '#fbbf24',
                    pointBorderColor: '#111827',
                    pointRadius: 4,
                    pointHoverRadius: 6,
                    tension: 0.4,
                    fill: true
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => `${ctx.parsed.y}%`
                        }
                    }
                },
                scales: {
                    x: { grid: { color: '#1e2d40' }, ticks: { color: '#475569' } },
                    y: {
                        grid: { color: '#1e2d40' },
                        ticks: { color: '#475569', callback: (val) => val + '%' },
                        min: 0, max: 50
                    }
                }
            }
        });
    }

    // --- BRANCH STATUS DONUT CHART ---
    const branchCtx = document.getElementById('branchStatusChart');
    if (branchCtx) {
        if (DashCharts.branch) DashCharts.branch.destroy();
        const bData = branchStatusData || { active: 12, inactive: 3 };
        DashCharts.branch = new Chart(branchCtx, {
            type: 'doughnut',
            data: {
                labels: ['نشطة', 'غير نشطة'],
                datasets: [{
                    data: [bData.active, bData.inactive],
                    backgroundColor: ['rgba(46,213,115,0.7)', 'rgba(255,71,87,0.5)'],
                    borderColor: ['#2ed573', '#ff4757'],
                    borderWidth: 2,
                    hoverOffset: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '70%',
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: { color: '#94a3b8', padding: 10, font: { size: 11 } }
                    }
                }
            }
        });
    }

    // --- REVENUE DONUT CHART (Right Panel) ---
    const revenueCtx = document.getElementById('revenueChart');
    if (revenueCtx) {
        if (DashCharts.revenue) DashCharts.revenue.destroy();
        DashCharts.revenue = new Chart(revenueCtx, {
            type: 'doughnut',
            data: {
                labels: ['نقدي', 'بطاقة', 'محفظة'],
                datasets: [{
                    data: [65, 25, 10],
                    backgroundColor: [
                        'rgba(46,213,115,0.6)',
                        'rgba(59,130,246,0.6)',
                        'rgba(168,85,247,0.6)'
                    ],
                    borderColor: ['#2ed573', '#3b82f6', '#a855f7'],
                    borderWidth: 2,
                    hoverOffset: 4
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '72%',
                plugins: {
                    legend: { display: false }
                }
            }
        });
    }

    // --- STOCK CATEGORY DONUT ---
    const stockCtx = document.getElementById('stockCategoryChart');
    if (stockCtx) {
        if (DashCharts.stock) DashCharts.stock.destroy();
        DashCharts.stock = new Chart(stockCtx, {
            type: 'doughnut',
            data: {
                labels: ['بقالة', 'خضروات', 'ألبان', 'مجمد', 'أخرى'],
                datasets: [{
                    data: [35, 20, 18, 15, 12],
                    backgroundColor: [
                        'rgba(255,140,66,0.6)',
                        'rgba(163,230,53,0.6)',
                        'rgba(0,229,255,0.6)',
                        'rgba(59,130,246,0.6)',
                        'rgba(100,116,139,0.6)'
                    ],
                    borderColor: ['#ff8c42', '#a3e635', '#00e5ff', '#3b82f6', '#64748b'],
                    borderWidth: 2
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: '65%',
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: { color: '#94a3b8', padding: 8, font: { size: 10 } }
                    }
                }
            }
        });
    }
}

// ============================================================
// BRANCH SELECTOR
// ============================================================
document.addEventListener('DOMContentLoaded', function() {
    // Branch selector in navbar
    const branchSelect = document.getElementById('branchSelector');
    if (branchSelect) {
        branchSelect.addEventListener('change', function() {
            const branchId = this.value;
            if (!branchId) return;
            // Update URL with branch parameter
            const url = new URL(window.location.href);
            url.searchParams.set('branch', branchId);
            window.location.href = url.toString();
        });
    }

    // Context panel selectors
    const contextBranch = document.getElementById('contextBranch');
    const contextWarehouse = document.getElementById('contextWarehouse');

    if (contextBranch) {
        contextBranch.addEventListener('change', function() {
            // Reload warehouses for selected branch
            const branchId = this.value;
            if (contextWarehouse && branchId) {
                fetch(`/core/api/warehouses/?branch_id=${branchId}`)
                    .then(r => r.json())
                    .then(data => {
                        contextWarehouse.innerHTML = data.warehouses.map(w =>
                            `<option value="${w.id}">${w.name}</option>`
                        ).join('');
                    }).catch(() => {});
            }
        });
    }
});

// ============================================================
// AUTO-DISMISS MESSAGES
// ============================================================
document.addEventListener('DOMContentLoaded', function() {
    const alerts = document.querySelectorAll('.alert');
    alerts.forEach(function(alert) {
        // Auto dismiss after 4 seconds
        setTimeout(() => {
            alert.style.transition = 'all 0.4s ease';
            alert.style.opacity = '0';
            alert.style.transform = 'translateX(30px)';
            setTimeout(() => alert.remove(), 400);
        }, 4000);

        // Manual close button
        const closeBtn = alert.querySelector('.alert-close');
        if (closeBtn) {
            closeBtn.addEventListener('click', () => {
                alert.style.transition = 'all 0.3s ease';
                alert.style.opacity = '0';
                setTimeout(() => alert.remove(), 300);
            });
        }
    });
});

// ============================================================
// SIDEBAR TOGGLE
// ============================================================
function toggleSidebar() {
    const sidebar = document.querySelector('.sidebar');
    const mainContent = document.querySelector('.main-content');
    if (!sidebar) return;

    if (window.innerWidth <= 768) {
        sidebar.classList.toggle('mobile-open');
    } else {
        sidebar.classList.toggle('collapsed');
        if (mainContent) {
            mainContent.classList.toggle('sidebar-collapsed');
        }
    }
}

// Expose globally
window.toggleSidebar = toggleSidebar;

// ============================================================
// HUB SEARCH (Command Hub page filter)
// ============================================================
document.addEventListener('DOMContentLoaded', function() {
    const hubSearch = document.getElementById('hubSearch');
    if (!hubSearch) return;

    hubSearch.addEventListener('input', function() {
        const query = this.value.toLowerCase().trim();
        const cards = document.querySelectorAll('.module-card');

        cards.forEach(card => {
            if (!query) {
                card.style.display = '';
                card.style.opacity = '1';
                return;
            }
            const title = (card.querySelector('.card-title')?.textContent || '').toLowerCase();
            const subtitle = (card.querySelector('.card-subtitle')?.textContent || '').toLowerCase();
            const matches = title.includes(query) || subtitle.includes(query);
            card.style.display = matches ? '' : 'none';
            card.style.opacity = matches ? '1' : '0';
        });
    });

    hubSearch.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            this.value = '';
            document.querySelectorAll('.module-card').forEach(c => {
                c.style.display = '';
                c.style.opacity = '1';
            });
        }
    });
});

// ============================================================
// ACTIVE NAV LINK HIGHLIGHTING
// ============================================================
document.addEventListener('DOMContentLoaded', function() {
    const currentPath = window.location.pathname;
    document.querySelectorAll('.nav-link').forEach(link => {
        const href = link.getAttribute('href');
        if (href && currentPath.startsWith(href) && href !== '/') {
            link.classList.add('active');
        }
    });
});

// ============================================================
// ADMIN DASHBOARD: RECENT TRANSACTIONS FEED
// ============================================================
function loadRecentTransactions() {
    const container = document.getElementById('recentTransactions');
    if (!container) return;

    fetch('/invoicing/api/recent-transactions/')
        .then(r => r.json())
        .then(data => {
            if (!data.transactions) return;
            container.innerHTML = data.transactions.map(tx => `
                <div class="transaction-item d-flex justify-between align-center p-2" style="border-bottom:1px solid var(--border-default)">
                    <div>
                        <div style="font-size:0.78rem;font-weight:600;">${tx.invoice_number}</div>
                        <div style="font-size:0.68rem;color:var(--text-muted);">${tx.branch_name} • ${tx.created_at}</div>
                    </div>
                    <div style="font-family:var(--font-english);font-weight:700;color:var(--neon-green);">
                        ${parseFloat(tx.total_amount).toLocaleString()} ج.م
                    </div>
                </div>
            `).join('');
        })
        .catch(() => {});
}

// ============================================================
// ADMIN DASHBOARD: BRANCH STATUS UPDATE
// ============================================================
function updateBranchStatus() {
    const counter = document.getElementById('activeBranchCount');
    if (!counter) return;

    fetch('/core/api/branch-status/')
        .then(r => r.json())
        .then(data => {
            if (data.active !== undefined) {
                counter.textContent = data.active;
            }
            if (DashCharts.branch && data.active !== undefined) {
                DashCharts.branch.data.datasets[0].data = [data.active, data.total - data.active];
                DashCharts.branch.update('none');
            }
        })
        .catch(() => {});
}

// ============================================================
// TABLE SORT
// ============================================================
function initTableSort(tableId) {
    const table = document.getElementById(tableId);
    if (!table) return;

    const headers = table.querySelectorAll('th[data-sort]');
    headers.forEach(th => {
        th.style.cursor = 'pointer';
        th.addEventListener('click', function() {
            const col = this.dataset.sort;
            const asc = this.dataset.dir !== 'asc';
            this.dataset.dir = asc ? 'asc' : 'desc';

            // Update all header indicators
            headers.forEach(h => h.querySelector('.sort-icon')?.remove());
            const icon = document.createElement('span');
            icon.className = 'sort-icon';
            icon.textContent = asc ? ' ▲' : ' ▼';
            icon.style.fontSize = '0.65rem';
            this.appendChild(icon);

            // Sort rows
            const tbody = table.querySelector('tbody');
            const rows = Array.from(tbody.querySelectorAll('tr'));
            rows.sort((a, b) => {
                const aVal = a.querySelector(`[data-col="${col}"]`)?.textContent.trim() || '';
                const bVal = b.querySelector(`[data-col="${col}"]`)?.textContent.trim() || '';
                const aNum = parseFloat(aVal.replace(/[^\d.-]/g, ''));
                const bNum = parseFloat(bVal.replace(/[^\d.-]/g, ''));
                if (!isNaN(aNum) && !isNaN(bNum)) {
                    return asc ? aNum - bNum : bNum - aNum;
                }
                return asc ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
            });
            rows.forEach(row => tbody.appendChild(row));
        });
    });
}

// ============================================================
// EXPORT TO CSV
// ============================================================
function exportTableToCSV(tableId, filename) {
    const table = document.getElementById(tableId);
    if (!table) return;

    const rows = Array.from(table.querySelectorAll('tr'));
    const csv = rows.map(row => {
        const cells = Array.from(row.querySelectorAll('th, td'));
        return cells.map(cell => `"${cell.textContent.trim().replace(/"/g, '""')}"`).join(',');
    }).join('\n');

    const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename || 'export.csv';
    a.click();
    URL.revokeObjectURL(url);
}

window.exportTableToCSV = exportTableToCSV;

// ============================================================
// INITIALIZE ON DOM READY
// ============================================================
document.addEventListener('DOMContentLoaded', function() {
    // Init charts if on dashboard page
    const hasDashboard = document.getElementById('salesChart') ||
                         document.getElementById('revenueChart');
    if (hasDashboard && typeof Chart !== 'undefined') {
        // Charts will be initialized by the page with real data
        setChartDefaults();
    }

    // Sort any tables with data-sort headers
    document.querySelectorAll('table[id]').forEach(t => initTableSort(t.id));

    // Refresh recent transactions every 30s on admin dashboard
    if (document.getElementById('recentTransactions')) {
        loadRecentTransactions();
        setInterval(loadRecentTransactions, 30000);
    }
});

// ============================================================
// EXPOSE GLOBALS
// ============================================================
window.initDashboardCharts = initDashboardCharts;
window.setChartDefaults = setChartDefaults;
window.DashCharts = DashCharts;
