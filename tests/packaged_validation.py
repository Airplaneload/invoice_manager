"""Run with invoice_manager_gui.exe --validate <absolute path to this file>."""
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import tkinter as tk
from email.message import EmailMessage
from contextlib import contextmanager


@contextmanager
def patched(obj, name, value):
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)

root_dir = pathlib.Path(sys.executable).resolve().parents[1]
out = root_dir / 'validation_downloads'
out.mkdir(exist_ok=True)
report = {}
try:
    import zxingcpp
    from invoice_download import decode_qr, pdf_valid
    sample = os.environ.get('INVOICE_TEST_QR')
    if sample:
        links = decode_qr(sample)
        report['qr_decode'] = len(links) == 1
    cls = legacy['InvoiceManager']
    report['startup_update_check_preserved'] = 'check_for_updates' in cls.__init__.__code__.co_names
    report['github_update_check_preserved'] = cls.check_for_updates.__module__ == 'invoice_legacy' and cls.GITHUB_REPO == 'Airplaneload/invoice_manager'
    cls.load_config = lambda self: self.download_dir.set(str(out / 'packaged'))
    cls.create_tray_icon = lambda self: None
    cls.update_auto_startup_state = lambda self: None
    cls.check_for_updates = lambda self, silent=True: None
    root = tk.Tk()
    root.withdraw()
    app = cls(root)
    root.update()
    toolbar = root.winfo_children()[0]
    button = next(widget for widget in toolbar.winfo_children() if widget.winfo_class() == 'TButton' and widget.cget('text') == '扫码下载')
    button.invoke()
    root.update()
    report['qr_window'] = app._qr_window.winfo_exists() == 1
    expected_position = f'+{root.winfo_screenwidth() // 2 - 680 // 2}+{root.winfo_screenheight() // 2 - 390 // 2}'
    report['qr_window_centered'] = app._qr_window.geometry().endswith(expected_position)
    report['version'] = app.VERSION
    qr_files = list((out / 'qr').glob('*.pdf'))
    if qr_files:
        invoice = legacy['InvoiceInfo']()
        invoice.filepath = str(qr_files[0])
        invoice.filename = qr_files[0].name
        app.recognize_invoice(invoice)
        report['qr_pdf_recognized'] = bool(invoice.invoice_number and invoice.total_amount)
        report['qr_total_amount'] = str(invoice.total_amount)
    # Exercise the actual replacement for the original failing save method.
    candidates = list((out / 'mail').glob('*.pdf'))
    if candidates:
        data = candidates[0].read_bytes()
        item = legacy['EmailItem']()
        item.date = '2026-10-31'
        item.email_id = 'test'
        with tempfile.TemporaryDirectory(dir=out) as directory:
            app.recognize_invoice = lambda invoice: setattr(invoice, 'product_name', '同名测试')
            first = app._save_attachment(directory, item, 'invoice.pdf', data)
            again = app._save_attachment(directory, item, 'invoice.pdf', data)
            app.recognize_invoice = lambda invoice: setattr(invoice, 'product_name', '同名测试')
            # A different valid PDF with same product must retain both files.
            second_data = candidates[1].read_bytes() if len(candidates) > 1 else data
            second = app._save_attachment(directory, item, 'invoice.pdf', second_data)
            report['duplicate_save'] = bool(first and first == again)
            report['collision_save'] = bool(second and (second != first if second_data != data else second == first))
            report['saved_pdf_valid'] = all(pdf_valid(path.read_bytes()) for path in pathlib.Path(directory).glob('*.pdf'))
        # Exercise the actual Download button flow against the legacy ttk.Label.
        # This catches UI/worker integration failures that backend-only tests miss.
        import invoice_extensions as extension
        notices = []
        msg = EmailMessage()
        msg.set_content('invoice')
        msg.add_attachment(data, maintype='application', subtype='pdf', filename='invoice.pdf')
        raw = msg.as_bytes()

        class FakeMail:
            def __init__(self, *args, **kwargs):
                pass
            def login(self, *args):
                return 'OK', []
            def select(self, *args, **kwargs):
                return 'OK', []
            def fetch(self, msg_id, query):
                assert query == '(BODY.PEEK[])'
                return 'OK', [(b'1 FETCH', raw)]
            def logout(self):
                return 'BYE', []

        def wait_for_download():
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                root.update()
                if str(app.download_progress.cget('text')).startswith('完成：'):
                    if app.download_thread:
                        app.download_thread.join(timeout=1)
                    return
                time.sleep(0.01)
            raise AssertionError('The Download button did not finish')

        app.email_account.set('local-test@example.test')
        app.auth_code.set('test-only')
        item.selected = True
        item.subject = '测试发票'
        app._all_emails = [item]
        app.emails = [item]
        app.is_running = False
        app._invoice_busy = True  # Recover the exact stale-state symptom.
        with patched(extension.imaplib, 'IMAP4_SSL', FakeMail), \
             patched(extension.messagebox, 'showinfo', lambda title, *args, **kw: notices.append(title)), \
             patched(extension.messagebox, 'showwarning', lambda title, *args, **kw: notices.append(title)), \
             patched(app, 'save_invoice_metadata', lambda: None):
            app.download_selected()
            wait_for_download()
            report['download_button_after_scan'] = '成功 1 封' in app.download_progress.cget('text') and not item.selected
            report['stale_download_state_recovered'] = '下载中' not in notices and not extension.active_download(app)
            item.selected = True
            def fail_downloader():
                raise RuntimeError('simulated initialization failure')
            with patched(extension, 'Downloader', fail_downloader):
                app.download_selected()
                wait_for_download()
            report['download_initialization_failure_released'] = item.selected and not extension.active_download(app)
            app.download_selected()
            wait_for_download()
            report['download_retry_after_failure'] = '成功 1 封' in app.download_progress.cget('text') and not item.selected
    app._qr_window.destroy()
    root.destroy()
    report['success'] = all(value for key, value in report.items() if isinstance(value, bool))
except Exception:
    import traceback
    report['success'] = False
    report['error'] = traceback.format_exc()
finally:
    (out / 'packaged_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
