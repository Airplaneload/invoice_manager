"""Read-only mailbox check through the packaged application's download action."""
import email
import imaplib
import json
import pathlib
import sys
import time
from email.header import decode_header, make_header

import invoice_extensions as extension

root_dir = pathlib.Path(sys.executable).resolve().parents[1]
out = root_dir / 'validation_downloads'
config = json.loads((root_dir / 'invoice_manager_gui' / 'config.json').read_text(encoding='utf8'))
report = {}
mail = None
root = None
try:
    cls = legacy['InvoiceManager']
    def load_config(self):
        self.email_account.set(config['email'])
        self.auth_code.set(config['auth_code'])
        self.download_dir.set(str(out / 'live_packaged'))
    cls.load_config = load_config
    cls.create_tray_icon = lambda self: None
    cls.update_auto_startup_state = lambda self: None
    cls.check_for_updates = lambda self, silent=True: None
    cls.save_invoice_metadata = lambda self: None
    notices = []
    extension.messagebox.showinfo = lambda title, *args, **kw: notices.append(title)
    extension.messagebox.showwarning = lambda title, *args, **kw: notices.append(title)
    root = legacy['tk'].Tk()
    root.withdraw()
    app = cls(root)
    mail = imaplib.IMAP4_SSL('imap.qq.com', 993, timeout=30)
    mail.login(config['email'], config['auth_code'])
    mail.select('INBOX', readonly=True)
    _, data = mail.search(None, 'ALL')
    items = []
    for message_id in reversed(data[0].split()[-30:]):
        _, fetched = mail.fetch(message_id, '(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE)])')
        raw = next((part[1] for part in fetched if isinstance(part, tuple)), None)
        if not raw:
            continue
        header = email.message_from_bytes(raw)
        subject = str(make_header(decode_header(header.get('Subject', ''))))
        if '发票' not in subject:
            continue
        item = legacy['EmailItem']()
        item.email_id = message_id
        item.subject = subject
        item.date_obj = email.utils.parsedate_to_datetime(header['Date']).replace(tzinfo=None)
        item.date = item.date_obj.strftime('%Y-%m-%d')
        item.selected = True
        items.append(item)
        if len(items) == 2:
            break
    mail.logout()
    mail = None
    if not items:
        raise RuntimeError('No invoice samples found')
    app._all_emails = app.emails = items
    app._invoice_busy = True
    app.download_selected()
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        root.update()
        progress = app.download_progress.cget('text')
        if progress.startswith('完成：'):
            report = {'samples': len(items), 'successes': sum(not item.selected for item in items), 'no_stuck_busy': not extension.active_download(app), 'unexpected_busy_popup': '下载中' in notices}
            report['success'] = report['successes'] == len(items) and report['no_stuck_busy'] and not report['unexpected_busy_popup']
            break
        time.sleep(0.03)
    else:
        report = {'success': False, 'reason': 'Download did not finish within the validation deadline'}
except Exception as exc:
    report = {'success': False, 'error_type': type(exc).__name__}
finally:
    if mail:
        try:
            mail.logout()
        except Exception:
            pass
    if root:
        root.destroy()
    (out / 'live_packaged_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
