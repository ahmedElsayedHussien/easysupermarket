from django.contrib import admin
from django.urls import path
from django.http import HttpResponse

def public_home(request):
    from apps.public.tenants.models import Tenant

    tenants = Tenant.objects.exclude(schema_name='public').order_by('schema_name')

    tenant_cards = ''
    icons = ['🛒', '🛍️', '🏬', '🏪', '✨']
    colors = [
        ('rgba(59,130,246,0.15)', '#3b82f6'),
        ('rgba(168,85,247,0.15)', '#a855f7'),
        ('rgba(16,185,129,0.15)', '#10b981'),
        ('rgba(245,158,11,0.15)', '#f59e0b'),
        ('rgba(239,68,68,0.15)',  '#ef4444'),
    ]

    for idx, tenant in enumerate(tenants):
        bg_color, accent = colors[idx % len(colors)]
        icon = icons[idx % len(icons)]
        # Assuming domain routing: http://domain:8080/
        domain = tenant.domains.first()
        domain_str = domain.domain if domain else 'localhost'
        tenant_url = f'http://{domain_str}:8080/'

        tenant_cards += f'''
        <div class="shop-card" style="--accent:{accent};--bg:{bg_color}">
            <div class="shop-icon">{icon}</div>
            <div class="shop-info">
                <h3>{tenant.name}</h3>
                <span class="schema-badge">{tenant.schema_name}</span>
            </div>
            <div class="shop-actions">
                <a href="{tenant_url}" class="btn-shop" target="_blank">
                    <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"
                         viewBox="0 0 24 24"><path d="M15 3h6v6M10 14L21 3M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/></svg>
                    دخول السوبر ماركت
                </a>
            </div>
        </div>'''

    html = f'''<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EasySupermarket — لوحة التحكم المركزية</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cairo:wght@300;400;600;700;900&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --bg-primary: #080c14;
    --bg-secondary: #0f1626;
    --bg-card: rgba(22,33,54,0.65);
    --border: rgba(255,255,255,0.07);
    --text-primary: #f3f4f6;
    --text-secondary: #9ca3af;
    --blue: #3b82f6;
    --purple: #a855f7;
  }}

  body {{
    background: var(--bg-primary);
    color: var(--text-primary);
    font-family: 'Cairo', sans-serif;
    min-height: 100vh;
    overflow-x: hidden;
  }}

  /* Animated Background */
  body::before {{
    content: '';
    position: fixed;
    inset: 0;
    background:
      radial-gradient(ellipse 60% 40% at 20% 20%, rgba(59,130,246,0.07) 0%, transparent 60%),
      radial-gradient(ellipse 50% 35% at 80% 80%, rgba(168,85,247,0.07) 0%, transparent 60%);
    pointer-events: none;
    z-index: 0;
  }}

  header {{
    position: relative;
    z-index: 1;
    text-align: center;
    padding: 60px 24px 40px;
  }}

  .logo-badge {{
    display: inline-flex;
    align-items: center;
    gap: 10px;
    background: rgba(59,130,246,0.12);
    border: 1px solid rgba(59,130,246,0.25);
    border-radius: 50px;
    padding: 6px 18px;
    font-size: 13px;
    color: var(--blue);
    margin-bottom: 24px;
    letter-spacing: 0.5px;
  }}

  .logo-dot {{
    width: 7px; height: 7px;
    background: var(--blue);
    border-radius: 50%;
    animation: pulse 2s infinite;
  }}

  @keyframes pulse {{
    0%,100% {{ opacity:1; transform:scale(1); }}
    50%      {{ opacity:.5; transform:scale(1.4); }}
  }}

  h1 {{
    font-size: clamp(2rem, 5vw, 3.2rem);
    font-weight: 900;
    line-height: 1.2;
    background: linear-gradient(135deg, #f3f4f6 0%, var(--blue) 50%, var(--purple) 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 14px;
  }}

  .subtitle {{
    color: var(--text-secondary);
    font-size: 16px;
    max-width: 480px;
    margin: 0 auto 36px;
    line-height: 1.7;
  }}

  .header-actions {{
    display: flex;
    gap: 12px;
    justify-content: center;
    flex-wrap: wrap;
  }}

  .btn-primary {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: linear-gradient(135deg, var(--blue), var(--purple));
    color: #fff;
    text-decoration: none;
    padding: 12px 28px;
    border-radius: 10px;
    font-size: 15px;
    font-weight: 700;
    transition: opacity .2s, transform .2s;
    box-shadow: 0 4px 20px rgba(59,130,246,0.3);
  }}
  .btn-primary:hover {{ opacity:.9; transform:translateY(-2px); }}

  .btn-outline {{
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: transparent;
    color: var(--text-secondary);
    text-decoration: none;
    padding: 12px 28px;
    border-radius: 10px;
    font-size: 15px;
    font-weight: 600;
    border: 1px solid var(--border);
    transition: border-color .2s, color .2s;
  }}
  .btn-outline:hover {{ border-color:var(--blue); color:var(--blue); }}

  .stats-bar {{
    position: relative;
    z-index: 1;
    display: flex;
    justify-content: center;
    gap: 0;
    max-width: 700px;
    margin: 0 auto 50px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 16px;
    backdrop-filter: blur(12px);
    overflow: hidden;
  }}

  .stat {{
    flex: 1;
    text-align: center;
    padding: 20px 16px;
    border-left: 1px solid var(--border);
  }}
  .stat:last-child {{ border-left: none; }}

  .stat-value {{
    font-size: 28px;
    font-weight: 900;
    background: linear-gradient(135deg, var(--blue), var(--purple));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }}
  .stat-label {{
    font-size: 12px;
    color: var(--text-secondary);
    margin-top: 2px;
  }}

  .section {{
    position: relative;
    z-index: 1;
    max-width: 900px;
    margin: 0 auto;
    padding: 0 24px 80px;
  }}

  .section-title {{
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 18px;
    font-weight: 700;
    color: var(--text-secondary);
    margin-bottom: 20px;
    text-transform: uppercase;
    letter-spacing: 1px;
  }}
  .section-title::after {{
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
  }}

  .shops-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 16px;
  }}

  .shop-card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 22px;
    backdrop-filter: blur(12px);
    transition: transform .2s, border-color .2s, box-shadow .2s;
    display: flex;
    flex-direction: column;
    gap: 14px;
    position: relative;
    overflow: hidden;
  }}

  .shop-card::before {{
    content: '';
    position: absolute;
    top: 0; right: 0;
    width: 80px; height: 80px;
    background: radial-gradient(circle, var(--bg) 0%, transparent 70%);
    border-radius: 0 16px 0 80px;
  }}

  .shop-card:hover {{
    transform: translateY(-4px);
    border-color: var(--accent);
    box-shadow: 0 8px 30px color-mix(in srgb, var(--accent) 20%, transparent);
  }}

  .shop-icon {{
    width: 48px; height: 48px;
    background: var(--bg);
    border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 22px;
  }}

  .shop-info h3 {{
    font-size: 17px;
    font-weight: 700;
    color: var(--text-primary);
    margin-bottom: 4px;
  }}

  .schema-badge {{
    display: inline-block;
    background: rgba(255,255,255,0.06);
    color: var(--text-secondary);
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 4px;
    font-family: monospace;
    border: 1px solid var(--border);
  }}

  .shop-actions {{
    margin-top: auto;
  }}

  .btn-shop {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: color-mix(in srgb, var(--accent) 15%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
    color: var(--accent);
    text-decoration: none;
    padding: 9px 16px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    transition: background .2s, transform .2s;
    width: 100%;
    justify-content: center;
  }}
  .btn-shop:hover {{
    background: color-mix(in srgb, var(--accent) 25%, transparent);
    transform: translateY(-1px);
  }}

  footer {{
    position: relative;
    z-index: 1;
    text-align: center;
    padding: 24px;
    border-top: 1px solid var(--border);
    color: var(--text-secondary);
    font-size: 13px;
  }}

  footer a {{ color: var(--blue); text-decoration: none; }}
  footer a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>

<header>
  <div class="logo-badge">
    <span class="logo-dot"></span>
    EasySupermarket — نظام إدارة المتاجر والسوبر ماركت
  </div>
  <h1>لوحة التحكم المركزية</h1>
  <p class="subtitle">
    منصة سحابية متكاملة (SaaS) لإدارة الفروع المتعددة والمحلات المنفصلة.
  </p>
  <div class="header-actions">
    <a href="/admin/" class="btn-primary">
      <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"
           viewBox="0 0 24 24"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
      لوحة الإدارة (إنشاء عملاء)
    </a>
  </div>
</header>

<div class="stats-bar">
  <div class="stat">
    <div class="stat-value">{tenants.count()}</div>
    <div class="stat-label">سوبر ماركت نشط</div>
  </div>
  <div class="stat">
    <div class="stat-value">ERP</div>
    <div class="stat-label">نظام متكامل</div>
  </div>
  <div class="stat">
    <div class="stat-value">POS</div>
    <div class="stat-label">نقطة بيع ذكية</div>
  </div>
  <div class="stat">
    <div class="stat-value">SaaS</div>
    <div class="stat-label">تعدد المستأجرين</div>
  </div>
</div>

<div class="section">
  <div class="section-title">مساحات العمل الحالية (العملاء)</div>
  <div class="shops-grid">
    {tenant_cards}
  </div>
</div>

<footer>
  EasySupermarket &copy; 2026 &nbsp;·&nbsp;
  <a href="/admin/">لوحة الإدارة</a> &nbsp;·&nbsp;
  <a href="/admin/tenants/tenant/add/">إضافة سوبر ماركت جديد</a>
</footer>

</body>
</html>'''

    return HttpResponse(html)


urlpatterns = [
    path('', public_home, name='public_home'),
    path('admin/', admin.site.urls),
]
