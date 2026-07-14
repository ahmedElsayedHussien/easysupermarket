import tkinter as tk
from tkinter import messagebox
import configparser
import requests
import threading
import time
from crypto_utils import serialize_invoice, hash_and_sign, build_cades_bes

# قراءة الإعدادات
config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')

try:
    BASE_URL = config['Settings']['BASE_URL']
    DLL_PATH = config['Settings']['DLL_PATH']
except KeyError:
    messagebox.showerror("خطأ", "ملف الإعدادات config.ini غير موجود أو غير صالح.")
    exit()

is_running = False

def start_signing_loop(pin):
    global is_running
    while is_running:
        try:
            # 1. سحب الفواتير من السيرفر
            res = requests.get(f"{BASE_URL}/einvoicing/api/pending/")
            if res.status_code == 200:
                invoices = res.json()
                for inv in invoices:
                    status_label.config(text=f"جاري توقيع فاتورة رقم {inv['id']}...")
                    
                    try:
                        # التكويد المتسلسل
                        serialized = serialize_invoice(inv['data'])
                        
                        # التوقيع بالتوكن
                        raw_sig, cert_der, hash_bytes = hash_and_sign(serialized, DLL_PATH, pin)
                        
                        # التغليف CAdES-BES
                        final_cades = build_cades_bes(raw_sig, cert_der, hash_bytes)
                        
                        # دمج التوقيع
                        signed_json = inv['data']
                        signed_json['signatures'] = [{"signatureType": "I", "value": final_cades}]
                        
                        # إرسال للسيرفر
                        post_res = requests.post(f"{BASE_URL}/einvoicing/api/signed/{inv['id']}/", json=signed_json)
                        
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
