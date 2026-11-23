import tkinter as tk
from tkinter import messagebox
import configparser
import requests
import threading
import time
from urllib.parse import urlparse
from crypto_utils import serialize_invoice, sign_document

# قراءة الإعدادات
config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')

try:
    BASE_URL = config['Settings']['BASE_URL'].strip().rstrip('/')
    DLL_PATH = config['Settings']['DLL_PATH'].strip()
    API_TOKEN = config['Settings'].get('API_TOKEN', '').strip()
    # Optional: path to a CA bundle (PEM) for self-hosted servers with a private CA.
    CA_BUNDLE = config['Settings'].get('CA_BUNDLE', '').strip()
except KeyError:
    messagebox.showerror("خطأ", "ملف الإعدادات config.ini غير موجود أو غير صالح.")
    exit()

if not API_TOKEN:
    messagebox.showerror("خطأ", "يجب إضافة API_TOKEN في ملف config.ini (من صفحة إعدادات الفاتورة الإلكترونية بالنظام).")
    exit()

_parsed = urlparse(BASE_URL)
if _parsed.scheme == 'http' and _parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
    messagebox.showwarning(
        "تحذير أمني",
        "رابط السيرفر يستخدم http:// غير المشفر. سيتم إرسال مفتاح الربط والفواتير بدون تشفير. "
        "يُنصح بشدة باستخدام https://",
    )

# TLS certificates are always verified (never verify=False).
VERIFY = CA_BUNDLE if CA_BUNDLE else True
HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}
TIMEOUT = (10, 60)

is_running = False

def start_signing_loop(pin):
    global is_running
    while is_running:
        try:
            # 1. سحب الفواتير من السيرفر
            res = requests.get(f"{BASE_URL}/einvoicing/api/pending/", headers=HEADERS, verify=VERIFY, timeout=TIMEOUT)
            if res.status_code == 401:
                status_label.config(text="مفتاح الربط API_TOKEN غير صحيح. راجع config.ini")
            elif res.status_code == 200:
                invoices = res.json()
                for inv in invoices:
                    status_label.config(text=f"جاري توقيع فاتورة رقم {inv['id']}...")
                    
                    try:
                        # التكويد المتسلسل
                        serialized = serialize_invoice(inv['data'])
                        
                        # التوقيع بالتوكن وتغليف CAdES-BES
                        final_cades = sign_document(serialized, DLL_PATH, pin)
                        
                        # دمج التوقيع
                        signed_json = inv['data']
                        signed_json['signatures'] = [{"signatureType": "I", "value": final_cades}]
                        
                        # إرسال للسيرفر
                        post_res = requests.post(f"{BASE_URL}/einvoicing/api/signed/{inv['id']}/", json=signed_json, headers=HEADERS, verify=VERIFY, timeout=TIMEOUT)
                        
                        if post_res.status_code == 200:
                            status_label.config(text=f"تم إرسال فاتورة {inv['id']} بنجاح!")
                        else:
                            status_label.config(text=f"خطأ أثناء رفع الفاتورة {inv['id']} للسيرفر")
                            
                    except Exception as e:
                        status_label.config(text=f"خطأ توقيع: {str(e)}")
                        
        except Exception as e:
            status_label.config(text=f"خطأ في الاتصال بالسيرفر: {str(e)}")
            
        time.sleep(10)

def toggle_connection():
    global is_running
    if not is_running:
        pin = pin_entry.get()
        if not pin:
            messagebox.showwarning("تنبيه", "برجاء إدخال الرقم السري للفلاشة")
            return
        is_running = True
        btn_connect.config(text="إيقاف الاتصال", bg="#4A2545", fg="white") # Deep Plum
        status_label.config(text="متصل.. في انتظار الفواتير")
        # Run in background
        threading.Thread(target=start_signing_loop, args=(pin,), daemon=True).start()
    else:
        is_running = False
        btn_connect.config(text="بدء الاتصال", bg="#EC228D", fg="white") # Brand Pink
        status_label.config(text="تم الإيقاف")

# --- تصميم الواجهة الرسومية ---
root = tk.Tk()
root.title("EasyMbStore E-Signer")
root.geometry("400x260")
root.configure(bg="#F9E0E8") # Soft Blush

tk.Label(root, text="برنامج التوقيع الإلكتروني للضرائب", font=("Arial", 14, "bold"), bg="#F9E0E8", fg="#4A2545").pack(pady=15)

tk.Label(root, text="الرقم السري للتوكن (PIN):", font=("Arial", 11), bg="#F9E0E8").pack()
pin_entry = tk.Entry(root, show="*", font=("Arial", 14), justify="center")
pin_entry.pack(pady=5)

btn_connect = tk.Button(root, text="بدء الاتصال", font=("Arial", 12, "bold"), bg="#EC228D", fg="white", command=toggle_connection)
btn_connect.pack(pady=15, ipadx=20)

status_label = tk.Label(root, text="جاهز للعمل", font=("Arial", 10), bg="#F9E0E8", fg="#4A2545")
status_label.pack()

root.mainloop()
