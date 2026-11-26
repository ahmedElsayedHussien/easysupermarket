"""
Local Signer: pulls the pending e-invoices from the server, signs them with the USB token (CAdES-BES) and
sends them back.

Token safety: an Egyptian signing token locks itself for good after a few wrong PINs. So ANY signing
error (wrong PIN, token unplugged, no certificate…) stops the loop at once — it is never retried with the
same PIN. Network / server errors don't touch the token: those are retried on the next round.

Tkinter is not thread-safe: the worker thread only talks to the window through root.after().
"""
import configparser
import logging
import threading
import tkinter as tk
from tkinter import messagebox
from urllib.parse import urlparse

import requests

from crypto_utils import serialize_invoice, sign_document

POLL_SECONDS = 10
TIMEOUT = (10, 60)

logging.basicConfig(filename='signer.log', level=logging.INFO, encoding='utf-8',
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('signer')

# the window exists first (hidden) so a start-up error never shows an empty white window behind it
root = tk.Tk()
root.withdraw()


def fatal(message):
    log.error(message)
    messagebox.showerror("خطأ", message)
    raise SystemExit(1)


config = configparser.ConfigParser()
config.read('config.ini', encoding='utf-8')
try:
    BASE_URL = config['Settings']['BASE_URL'].strip().rstrip('/')
    DLL_PATH = config['Settings']['DLL_PATH'].strip()
    API_TOKEN = config['Settings'].get('API_TOKEN', '').strip()
    # Optional: path to a CA bundle (PEM) for self-hosted servers with a private CA.
    CA_BUNDLE = config['Settings'].get('CA_BUNDLE', '').strip()
except KeyError:
    fatal("ملف الإعدادات config.ini غير موجود أو غير صالح.")

if not API_TOKEN:
    fatal("يجب إضافة API_TOKEN في ملف config.ini (من صفحة إعدادات الفاتورة الإلكترونية بالنظام).")

_parsed = urlparse(BASE_URL)
if _parsed.scheme == 'http' and _parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
    messagebox.showwarning(
        "تحذير أمني",
        "رابط السيرفر يستخدم http:// غير المشفر. سيتم إرسال مفتاح الربط والفواتير بدون تشفير. "
        "يُنصح بشدة باستخدام https://",
    )

# one HTTPS connection reused; TLS certificates are always verified (never verify=False)
session = requests.Session()
session.headers.update({"Authorization": f"Bearer {API_TOKEN}"})
session.verify = CA_BUNDLE if CA_BUNDLE else True

stop_event = threading.Event()
worker = None


def set_status(text):
    root.after(0, lambda: status_label.config(text=text))


def signing_loop(pin):
    while not stop_event.is_set():
        try:
            res = session.get(f"{BASE_URL}/einvoicing/api/pending/", timeout=TIMEOUT)
        except requests.RequestException as e:
            log.warning('server unreachable: %s', e)
            set_status(f"خطأ في الاتصال بالسيرفر — هحاول تاني بعد {POLL_SECONDS} ثواني")
            stop_event.wait(POLL_SECONDS)
            continue

        if res.status_code == 401:
            log.error('pending: 401 — wrong API_TOKEN')
            root.after(0, stop_on_error, "مفتاح الربط API_TOKEN غير صحيح. راجع config.ini")
            return
        if res.status_code != 200:
            log.warning('pending: HTTP %s %s', res.status_code, res.text[:300])
            set_status(f"السيرفر رد بخطأ (HTTP {res.status_code}) — هحاول تاني")
            stop_event.wait(POLL_SECONDS)
            continue

        invoices = res.json()
        for inv in invoices:
            if stop_event.is_set():
                return
            set_status(f"جاري توقيع فاتورة رقم {inv['id']}...")
            try:
                serialized = serialize_invoice(inv['data'])
                final_cades = sign_document(serialized, DLL_PATH, pin)
            except Exception as e:  # noqa: BLE001 — any token error: stop, never retry the PIN
                log.error('signing invoice %s failed: %s', inv['id'], e)
                root.after(0, stop_on_error, f"توقف التوقيع لحماية الفلاشة من القفل: {e}")
                return
            signed_json = inv['data']
            signed_json['signatures'] = [{"signatureType": "I", "value": final_cades}]
            try:
                post_res = session.post(f"{BASE_URL}/einvoicing/api/signed/{inv['id']}/", json=signed_json,
                                        timeout=TIMEOUT)
            except requests.RequestException as e:
                log.warning('upload of invoice %s failed: %s', inv['id'], e)
                set_status(f"تعذر رفع الفاتورة {inv['id']} — هتتبعت في المرة الجاية")
                continue
            if post_res.status_code == 200:
                log.info('invoice %s signed and sent', inv['id'])
                set_status(f"تم إرسال فاتورة {inv['id']} بنجاح!")
            else:
                log.warning('upload of invoice %s: HTTP %s %s', inv['id'], post_res.status_code, post_res.text[:500])
                set_status(f"خطأ أثناء رفع الفاتورة {inv['id']} للسيرفر (HTTP {post_res.status_code}) — التفاصيل في signer.log")
        if not invoices:
            set_status("متصل.. في انتظار الفواتير")
        stop_event.wait(POLL_SECONDS)       # wakes at once when stopped


def set_idle(text):
    btn_connect.config(text="بدء الاتصال", bg="#EC228D", fg="white", state="normal")  # Brand Pink
    pin_entry.config(state="normal")
    status_label.config(text=text)


def stop_on_error(message):
    stop_event.set()
    set_idle(message)
    messagebox.showerror("توقف", message)


def toggle_connection():
    global worker
    if worker is not None and worker.is_alive():
        stop_event.set()
        btn_connect.config(state="disabled")
        status_label.config(text="جاري الإيقاف...")
        root.after(200, wait_for_stop)
        return
    pin = pin_entry.get()
    if not pin:
        messagebox.showwarning("تنبيه", "برجاء إدخال الرقم السري للفلاشة")
        return
    stop_event.clear()
    btn_connect.config(text="إيقاف الاتصال", bg="#4A2545", fg="white")  # Deep Plum
    pin_entry.config(state="disabled")
    status_label.config(text="متصل.. في انتظار الفواتير")
    log.info('connection started')
    worker = threading.Thread(target=signing_loop, args=(pin,), daemon=True)
    worker.start()


def wait_for_stop():
    """The old worker must be gone before a new one can start (one token session at a time)."""
    if worker is not None and worker.is_alive():
        root.after(200, wait_for_stop)
        return
    log.info('connection stopped')
    set_idle("تم الإيقاف")


# --- الواجهة ---
root.title("EasyMbStore E-Signer")
root.geometry("400x260")
root.configure(bg="#F9E0E8")  # Soft Blush

tk.Label(root, text="برنامج التوقيع الإلكتروني للضرائب", font=("Arial", 14, "bold"), bg="#F9E0E8", fg="#4A2545").pack(pady=15)

tk.Label(root, text="الرقم السري للتوكن (PIN):", font=("Arial", 11), bg="#F9E0E8").pack()
pin_entry = tk.Entry(root, show="*", font=("Arial", 14), justify="center")
pin_entry.pack(pady=5)

btn_connect = tk.Button(root, text="بدء الاتصال", font=("Arial", 12, "bold"), bg="#EC228D", fg="white", command=toggle_connection)
btn_connect.pack(pady=15, ipadx=20)

status_label = tk.Label(root, text="جاهز للعمل", font=("Arial", 10), bg="#F9E0E8", fg="#4A2545", wraplength=380)
status_label.pack()

root.deiconify()
root.mainloop()
