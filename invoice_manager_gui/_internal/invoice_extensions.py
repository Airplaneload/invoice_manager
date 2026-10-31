"""Maintained extensions to the distributed invoice manager (legacy UI retained)."""
from __future__ import annotations

import email
import imaplib
import io
import os
import queue
import tempfile
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import ImageGrab

from invoice_download import (DownloadError, Downloader, decode_qr, message_links,
                              message_pdfs, normalize_url, pdf_valid, safe_filename, save_pdf)
from invoice_dedup import deduplicate, extract_identity, find_duplicate

LEGACY = {}


def invoice_lock(self):
    if not hasattr(self, '_invoice_lock'):
        self._invoice_lock = threading.RLock()
    return self._invoice_lock


def clean_duplicate_invoices(self):
    if active_download(self):
        self.log('下载完成后将自动清理重复发票。')
        return 0
    with invoice_lock(self):
        count = deduplicate(self.invoices, self.log)
        if count:
            self.save_invoice_metadata()
        return count


def cleanup_duplicates_clicked(self):
    count = clean_duplicate_invoices(self)
    self.refresh_invoice_list()
    self.log(f'重复检查完成：清理 {count} 份；按发票号和价税合计匹配，信息不完整的发票保留。')


def set_download_progress(self, text):
    self.download_progress.configure(text=text)


def active_download(self):
    """Use the worker itself to distinguish an active task from a stale flag."""
    worker = getattr(self, '_active_download_thread', None)
    busy = worker is not None and worker.is_alive()
    self._invoice_busy = busy
    return busy


def start_download_worker(self, target, *args):
    def run():
        try:
            target(*args)
        finally:
            if getattr(self, '_active_download_thread', None) is threading.current_thread():
                self._invoice_busy = False

    worker = threading.Thread(target=run, daemon=True)
    self._active_download_thread = worker
    self._invoice_busy = True
    try:
        worker.start()
    except Exception:
        self._active_download_thread = None
        self._invoice_busy = False
        raise
    return worker


def save_attachment(self, download_dir, email_item, filename, content, product_name=''):
    invoice = LEGACY['InvoiceInfo']()
    invoice.email_id = email_item.email_id
    invoice.download_date = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    invoice.product_name = product_name
    Path(download_dir).mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        if not pdf_valid(content):
            raise DownloadError('收到的内容不是完整 PDF，未保存。')
        with tempfile.NamedTemporaryFile(dir=download_dir, prefix='.invoice_', suffix='.pdf', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        invoice.filepath = str(temporary)
        invoice.filename = temporary.name
        try:
            self.recognize_invoice(invoice)
        except Exception:
            self.log('发票已获取，自动识别暂未完成，可稍后重新识别。')
        product = next((item.get('name', '') for item in invoice.items if item.get('name')), '') or invoice.product_name or product_name
        date = (email_item.date or datetime.now().strftime('%Y-%m-%d'))[:10]
        stem = product or Path(filename.replace('\\', '/')).stem or '发票'
        with invoice_lock(self):
            existing = find_duplicate(self.invoices, invoice)
            if existing:
                self.log('已跳过重复发票（发票号和价税合计相同）：' + existing.filename)
                return existing.filepath
            path, duplicate = save_pdf(download_dir, f'{safe_filename(date)}_{safe_filename(stem)}.pdf', content)
            invoice.filepath, invoice.filename = str(path), path.name
            invoice.product_name = product
            if not any(os.path.normcase(os.path.abspath(item.filepath)) == os.path.normcase(str(path.resolve())) for item in self.invoices):
                self.invoices.append(invoice)
        self.log(('发票已存在，下载成功: ' if duplicate else '已保存发票: ') + path.name)
        return str(path)
    except Exception as exc:
        self.log('保存失败: ' + (str(exc) if isinstance(exc, DownloadError) else type(exc).__name__))
        return None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def download_url(self, url):
    downloader = Downloader()
    try:
        return downloader.download(url)[0][1]
    except DownloadError as exc:
        self.log(str(exc))
        return None
    finally:
        downloader.close()


def download_selected(self):
    if active_download(self):
        messagebox.showinfo('下载中', '请等待当前下载完成。', parent=self.root)
        return
    if getattr(self, 'is_running', False):
        messagebox.showinfo('正在扫描', '请等待邮件扫描完成，或先取消扫描。', parent=self.root)
        return
    selected = [item for item in self._all_emails if item.selected]
    if not selected:
        messagebox.showwarning('提示', '请先勾选要下载的邮件。', parent=self.root)
        return
    account, auth = self.email_account.get().strip(), self.auth_code.get().strip()
    folder = self.download_dir.get()
    if not account or not auth:
        messagebox.showwarning('提示', '请先填写邮箱和授权码。', parent=self.root)
        return
    events = queue.Queue()
    set_download_progress(self, f'准备下载 {len(selected)} 封邮件…')

    def worker():
        mail, downloader = None, None
        success, failed = [], []
        try:
            downloader = Downloader()
            # A dedicated read-only connection avoids interference with preview/scan.
            mail = imaplib.IMAP4_SSL('imap.qq.com', 993, timeout=30)
            mail.login(account, auth)
            status, _ = mail.select('INBOX', readonly=True)
            if status != 'OK':
                raise DownloadError('无法打开收件箱。')
            for index, item in enumerate(selected, 1):
                events.put(('progress', f'正在下载 {index}/{len(selected)}'))
                try:
                    raw = None
                    for attempt in range(2):
                        try:
                            status, data = mail.fetch(item.email_id, '(BODY.PEEK[])')
                            if status != 'OK':
                                raise DownloadError('无法读取邮件，请重新扫描后重试。')
                            raw = next((part[1] for part in data if isinstance(part, tuple) and isinstance(part[1], bytes)), None)
                            if not raw:
                                raise DownloadError('邮件内容为空，请重新扫描后重试。')
                            break
                        except (imaplib.IMAP4.abort, OSError):
                            if attempt:
                                raise
                            try:
                                mail.logout()
                            except Exception:
                                pass
                            mail = imaplib.IMAP4_SSL('imap.qq.com', 993, timeout=30)
                            mail.login(account, auth)
                            mail.select('INBOX', readonly=True)
                    msg = email.message_from_bytes(raw)
                    files = message_pdfs(msg)
                    errors = []
                    if not files:
                        for link in message_links(msg):
                            try:
                                files.extend(downloader.download(link))
                                if files:
                                    break
                            except DownloadError as exc:
                                errors.append(str(exc))
                    if not files:
                        raise DownloadError(errors[-1] if errors else '未找到 PDF 附件或有效下载链接，可使用“扫码下载”。')
                    count = 0
                    product = self._extract_product_name_from_subject(item.subject)
                    for name, content in files:
                        name = self.decode_header_str(name)
                        if self._save_attachment(folder, item, name, content, product):
                            count += 1
                    if count != len(files):
                        raise DownloadError('部分发票保存失败，请查看日志后重试。')
                    success.append(item)
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, DownloadError) else ('邮箱连接中断，请重试。' if isinstance(exc, (OSError, imaplib.IMAP4.error)) else type(exc).__name__)
                    failed.append((item, reason))
                    events.put(('log', '下载失败: ' + reason))
        except Exception as exc:
            failed = [(item, '邮箱登录或连接失败，请检查配置后重试。') for item in selected if item not in success]
            events.put(('log', '邮箱连接失败: ' + type(exc).__name__))
        finally:
            try:
                if downloader:
                    downloader.close()
                if mail:
                    try:
                        mail.logout()
                    except Exception:
                        pass
            finally:
                events.put(('done', (success, failed)))

    def poll():
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == 'progress':
                    set_download_progress(self, value)
                elif kind == 'log':
                    self.log(value)
                elif kind == 'done':
                    if active_download(self):
                        events.put((kind, value))
                        self.root.after(100, poll)
                        return
                    successes, failures = value
                    active_download(self)
                    for item in successes:
                        item.selected = False
                    self.refresh_email_list()
                    self.refresh_invoice_list()
                    self.save_invoice_metadata()
                    set_download_progress(self, f'完成：成功 {len(successes)} 封，失败 {len(failures)} 封')
                    if failures:
                        messagebox.showwarning('下载结果', f'成功 {len(successes)} 封，失败 {len(failures)} 封。\n失败邮件保持勾选，可直接重试。\n\n' + '\n'.join(reason for _, reason in failures[:4]), parent=self.root)
                    return
        except queue.Empty:
            pass
        self.root.after(100, poll)

    self.download_thread = start_download_worker(self, worker)
    self.root.after(100, poll)


def open_qr_download(self):
    current = getattr(self, '_qr_window', None)
    if current is not None and current.winfo_exists():
        current.lift()
        return
    win = tk.Toplevel(self.root)
    win.withdraw()
    self._qr_window = win
    win.title('扫码下载发票')
    win.geometry('680x390')
    win.minsize(620, 370)
    win.transient(self.root)
    frame = ttk.Frame(win, padding=18)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='识别二维码图片，直接下载发票', font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='支持“二维码交付”截图；复制截图后可按 Ctrl+V 粘贴。').pack(anchor='w', pady=(8, 12))
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x')
    link_var, status_var = tk.StringVar(), tk.StringVar(value='请选择二维码图片，或粘贴截图 / 下载链接。')
    folder_var = tk.StringVar(value=self.download_dir.get())
    state = {'busy': False}
    events = queue.Queue()
    selected_link = {'value': ''}

    def recognized(image):
        links = decode_qr(image)
        if len(links) > 1:
            raise DownloadError('图片包含多个下载二维码，请裁剪为单个二维码后重试。')
        link_var.set(links[0])
        selected_link['value'] = links[0]
        status_var.set('二维码已识别，点击“下载发票”保存。')

    def browse():
        name = filedialog.askopenfilename(parent=win, title='选择二维码图片', filetypes=[('图片', '*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff'), ('所有文件', '*.*')])
        if name:
            try:
                recognized(name)
            except Exception as exc:
                status_var.set(str(exc) if isinstance(exc, DownloadError) else '无法读取图片，请选择其他文件。')

    def paste(event=None):
        if state['busy']:
            return 'break'
        try:
            value = ImageGrab.grabclipboard()
            if isinstance(value, list) and value:
                recognized(value[0])
            elif value is not None:
                recognized(value)
            else:
                link_var.set(normalize_url(win.clipboard_get()))
                status_var.set('下载链接已粘贴。')
        except Exception as exc:
            status_var.set(str(exc) if isinstance(exc, DownloadError) else '剪贴板中没有可用截图或下载链接。')
        return 'break'

    ttk.Button(buttons, text='选择二维码图片', command=browse).pack(side='left')
    ttk.Button(buttons, text='粘贴截图 / 链接', command=paste).pack(side='left', padx=8)
    ttk.Label(frame, text='下载链接（也可以直接粘贴）：').pack(anchor='w', pady=(16, 5))
    entry = ttk.Entry(frame, textvariable=link_var)
    entry.pack(fill='x')
    row = ttk.Frame(frame)
    row.pack(fill='x', pady=(14, 0))
    ttk.Label(row, text='保存到：').pack(side='left')
    ttk.Entry(row, textvariable=folder_var).pack(side='left', fill='x', expand=True, padx=5)

    def choose_folder():
        result = filedialog.askdirectory(parent=win, initialdir=folder_var.get())
        if result:
            folder_var.set(result)

    folder_button = ttk.Button(row, text='浏览', command=choose_folder)
    folder_button.pack(side='left')
    ttk.Label(frame, textvariable=status_var, wraplength=620).pack(anchor='w', fill='x', pady=(16, 8))
    action_row = ttk.Frame(frame)
    action_row.pack(fill='x', side='bottom')

    def worker(link, folder):
        downloader = None
        try:
            downloader = Downloader()
            files = downloader.download(link)
            item = LEGACY['EmailItem']()
            item.email_id = ''
            item.date = datetime.now().strftime('%Y-%m-%d')
            paths = []
            for name, data in files:
                saved = self._save_attachment(folder, item, name, data)
                if not saved:
                    raise DownloadError('文件保存失败，请检查保存目录后重试。')
                paths.append(saved)
            events.put(('done', paths))
        except Exception as exc:
            events.put(('error', str(exc) if isinstance(exc, DownloadError) else '下载失败，请稍后重试。'))
        finally:
            if downloader:
                downloader.close()

    def start():
        if active_download(self):
            status_var.set('当前已有下载任务，请稍后重试。')
            return
        try:
            link = normalize_url(link_var.get())
        except (DownloadError, ValueError) as exc:
            status_var.set(str(exc))
            return
        folder = folder_var.get().strip()
        if not folder:
            status_var.set('请选择保存目录。')
            return
        state['busy'] = True
        selected_link['value'] = link
        download_button.configure(state='disabled')
        status_var.set('正在下载发票，请稍候…')
        try:
            start_download_worker(self, worker, link, folder)
        except Exception:
            state['busy'] = False
            download_button.configure(state='normal')
            status_var.set('无法启动下载，请重试。')
            return
        win.after(100, poll)

    def poll():
        if not win.winfo_exists():
            return
        try:
            kind, value = events.get_nowait()
        except queue.Empty:
            win.after(100, poll)
            return
        if active_download(self):
            events.put((kind, value))
            win.after(100, poll)
            return
        state['busy'] = False
        active_download(self)
        download_button.configure(state='normal')
        if kind == 'done':
            self.refresh_invoice_list()
            self.save_invoice_metadata()
            status_var.set(f'下载成功：已保存 {len(value)} 份发票。\n' + '\n'.join(Path(path).name for path in value))
        else:
            status_var.set(value + '\n可点击“浏览器打开”完成平台验证，或更换链接重试。')

    def browser_open():
        try:
            webbrowser.open(normalize_url(link_var.get()))
        except (DownloadError, ValueError) as exc:
            status_var.set(str(exc))

    download_button = ttk.Button(action_row, text='下载发票', command=start)
    download_button.pack(side='left')
    ttk.Button(action_row, text='浏览器打开', command=browser_open).pack(side='left', padx=8)
    ttk.Button(action_row, text='打开保存目录', command=lambda: os.startfile(str(Path(folder_var.get()).resolve()))).pack(side='left')
    for key in ('<Control-v>', '<Control-V>'):
        win.bind(key, paste)

    def close():
        if state['busy']:
            status_var.set('正在下载，请等待完成后关闭。')
            return
        win.destroy()

    win.protocol('WM_DELETE_WINDOW', close)
    self.center_toplevel(win, 680, 390)
    win.deiconify()


def install(namespace):
    LEGACY.update(namespace)
    cls = namespace['InvoiceManager']
    cls.VERSION = '1.5.7'
    old_create = cls.create_ui
    old_log = cls.log
    old_extract = cls._extract_invoice_info
    old_refresh = cls.refresh_invoice_list

    def extract_invoice_info(self, text, invoice):
        old_extract(self, text, invoice)
        extract_identity(text, invoice)

    def refresh_invoice_list(self):
        clean_duplicate_invoices(self)
        old_refresh(self)

    def log(self, message):
        if threading.current_thread() is threading.main_thread():
            old_log(self, message)
        else:
            self._extension_events.put(str(message))

    def create_ui(self):
        self._extension_events = queue.Queue()
        self._invoice_lock = threading.RLock()
        old_create(self)
        toolbar = self.root.winfo_children()[0]
        ttk.Button(toolbar, text='扫码下载', command=lambda: open_qr_download(self)).pack(side='left', padx=(8, 0), before=self.status_label)
        widgets = list(self.root.winfo_children())
        while widgets:
            widget = widgets.pop()
            if isinstance(widget, ttk.Button) and widget.cget('text') == '重新识别':
                ttk.Button(widget.master, text='清理重复', width=8,
                           command=lambda: cleanup_duplicates_clicked(self)).pack(side='left', padx=2, after=widget)
                break
            widgets.extend(widget.winfo_children())

        def drain():
            try:
                while True:
                    old_log(self, self._extension_events.get_nowait())
            except queue.Empty:
                pass
            self.root.after(100, drain)

        self.root.after(100, drain)

    cls.create_ui = create_ui
    cls.log = log
    cls._extract_invoice_info = extract_invoice_info
    cls.refresh_invoice_list = refresh_invoice_list
    cls.clean_duplicate_invoices = clean_duplicate_invoices
    cls._save_attachment = save_attachment
    cls._download_from_url = download_url
    cls.download_selected = download_selected
    cls.open_qr_download = open_qr_download


def launch(namespace):
    root = namespace['tk'].Tk()
    namespace['InvoiceManager'](root)
    root.mainloop()
