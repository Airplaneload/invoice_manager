"""Validate duplicate cleanup through the real packaged UI using private copies."""
import json
import pathlib
import shutil
import sys
import tempfile
import tkinter as tk

from invoice_dedup import BACKUP_DIRECTORY, invoice_key

root_dir = pathlib.Path(sys.executable).resolve().parents[1]
out = root_dir / 'validation_downloads'
report = {}
root = None
try:
    cls = legacy['InvoiceManager']
    with tempfile.TemporaryDirectory(dir=out) as folder:
        folder = pathlib.Path(folder)
        samples = list((out / 'dedup_samples').glob('*.pdf'))
        assert samples, 'Missing local PDF samples'
        for sample in samples:
            shutil.copy2(sample, folder / sample.name)
        metadata = folder / 'invoice_metadata.json'
        source_meta = root_dir / 'invoice_manager_gui' / 'invoice_metadata.json'
        shutil.copy2(source_meta, metadata)
        legacy['METADATA_FILE'] = str(metadata)
        cls.load_config = lambda self: self.download_dir.set(str(folder))
        cls.create_tray_icon = lambda self: None
        cls.update_auto_startup_state = lambda self: None
        cls.check_for_updates = lambda self, silent=True: None
        root = tk.Tk()
        root.withdraw()
        app = cls(root)
        root.update()
        report['version'] = app.VERSION
        report['loaded'] = len(samples)
        report['remaining'] = len(app.invoices)
        archived = list((folder / BACKUP_DIRECTORY).glob('*/*.pdf'))
        report['archived'] = len(archived)
        keys = [invoice_key(invoice) for invoice in app.invoices]
        report['all_pdfs_recognized'] = all(key is not None for key in keys)
        report['no_duplicate_identity'] = len(keys) == len(set(keys))
        report['all_files_preserved'] = len(app.invoices) + len(archived) == len(samples)
        report['expected_real_duplicates'] = len(archived) == 5
        saved_meta = json.loads(metadata.read_text(encoding='utf-8'))
        report['metadata_matches_list'] = set(saved_meta) == {item.filename for item in app.invoices}
        app.load_invoices_from_disk()
        report['reload_does_not_load_backups'] = len(app.invoices) == report['remaining']

        widgets = list(root.winfo_children())
        cleanup = None
        while widgets:
            widget = widgets.pop()
            if widget.winfo_class() == 'TButton' and widget.cget('text') == '清理重复':
                cleanup = widget
                break
            widgets.extend(widget.winfo_children())
        assert cleanup is not None
        cleanup.invoke()
        report['manual_cleanup_idempotent'] = len(app.invoices) == report['remaining']
        app.notebook.select(1)
        root.deiconify()
        root.update()
        report['cleanup_button_visible'] = bool(cleanup.winfo_ismapped() and cleanup.winfo_width() > 40)
        report['toolbar_fits'] = all(
            child.winfo_x() + child.winfo_width() <= cleanup.master.winfo_width()
            for child in cleanup.master.winfo_children())

        # Download a regenerated PDF with a new name and email date.
        original = app.invoices[0]
        item = legacy['EmailItem']()
        item.date, item.email_id = '2026-10-31', 'dedup-test'
        content = pathlib.Path(original.filepath).read_bytes().replace(b'%%EOF', b'\n%%EOF')
        before = len(app.invoices)
        path = app._save_attachment(str(folder), item, 'new_filename.pdf', content, '新名称')
        report['download_reuses_existing_identity'] = path == original.filepath and len(app.invoices) == before
        report['no_temporary_files_left'] = not list(folder.glob('.invoice_*'))

        # Reload and recognition callbacks use the same automatic cleanup path.
        duplicate = folder / 'duplicate_after_recognition.pdf'
        shutil.copy2(original.filepath, duplicate)
        extra = legacy['InvoiceInfo']()
        extra.filepath, extra.filename = str(duplicate), duplicate.name
        app.invoices.append(extra)
        app.recognize_invoice(extra)
        app.refresh_invoice_list()
        report['recognition_refresh_cleans_duplicates'] = len(app.invoices) == before and not duplicate.exists()
        report['success'] = all(value for value in report.values() if isinstance(value, bool))
        root.destroy()
        root = None
except Exception:
    import traceback
    report['success'] = False
    report['error'] = traceback.format_exc()
finally:
    if root:
        root.destroy()
    (out / 'packaged_dedup_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
