"""Conservative invoice identity matching and recoverable duplicate cleanup."""
from __future__ import annotations

import json
import os
import re
import unicodedata
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

BACKUP_DIRECTORY = '重复发票备份'


def normalize_number(value):
    if not isinstance(value, str):
        return ''  # Numeric values may already have lost leading zeroes.
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value))


def normalize_amount(value):
    if value is None or isinstance(value, bool):
        return None
    text = unicodedata.normalize('NFKC', str(value)).strip()
    text = re.sub(r'^(?:人民币|RMB|CNY|[¥￥])\s*', '', text, flags=re.I)
    text = re.sub(r'\s*元$', '', text).strip()
    if not re.fullmatch(r'-?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?', text):
        return None
    try:
        amount = Decimal(text.replace(',', ''))
        # Do not round distinct amounts into the same identity.
        if amount != amount.quantize(Decimal('0.01')):
            return None
        return amount
    except InvalidOperation:
        return None


def invoice_key(invoice):
    number = normalize_number(getattr(invoice, 'invoice_number', ''))
    amount = normalize_amount(getattr(invoice, 'total_amount', None))
    if amount is None or not re.fullmatch(r'(?:[0-9]{8}|[0-9]{20})', number):
        return None
    code = ''
    if len(number) == 8:
        # Traditional invoice numbers are only unique together with their code.
        code = normalize_number(getattr(invoice, 'invoice_code', ''))
        if not re.fullmatch(r'(?:[0-9]{10}|[0-9]{12})', code):
            return None
    return number, amount, code


def extract_identity(text, invoice):
    """Read explicit printed labels, including 8-digit traditional invoices."""
    compact = re.sub(r'[^\S\n]+', '', unicodedata.normalize('NFKC', text))
    for label, field, sizes in (
        (r'(?:发票号码|发票号|票号)', 'invoice_number', (8, 20)),
        (r'发票代码', 'invoice_code', (10, 12)),
    ):
        match = re.search(label + r'[:：]?\s*([0-9]+)(?![0-9])', compact)
        if match and len(match[1]) in sizes:
            setattr(invoice, field, match[1])
    # The printed grand total is preferable to a sum of rounded item values.
    for label in (r'\(小写\)', r'价税合计'):
        match = re.search(label + r'[:：]?\s*[¥￥]?\s*(-?[0-9][0-9,]*(?:\.[0-9]+)?)', compact)
        if match:
            amount = normalize_amount(match[1])
            if amount is not None:
                invoice.total_amount = format(amount, '.2f')
                break


def existing_file(invoice):
    path = getattr(invoice, 'filepath', '')
    return bool(path) and Path(path).is_file()


def same_file(first, second):
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def find_duplicate(invoices, incoming):
    key = invoice_key(incoming)
    if key is None:
        return None
    return next((item for item in invoices if invoice_key(item) == key and existing_file(item)), None)


def archive_duplicate(duplicate, retained):
    """Write recovery information before moving the redundant original file."""
    source = Path(duplicate.filepath).absolute()
    # Never remove two separate filenames that point at the same physical file.
    if same_file(source, retained.filepath):
        return None
    folder = source.parent / BACKUP_DIRECTORY / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    destination = folder / source.name
    manifest = {
        'original_file': str(source),
        'retained_file': str(Path(retained.filepath).absolute()),
        'backup_file': str(destination),
        'invoice': vars(duplicate).copy(),
    }
    (folder / '恢复信息.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    source.rename(destination)
    return destination


def deduplicate(invoices, log=lambda message: None):
    groups = {}
    for invoice in invoices:
        key = invoice_key(invoice)
        if key is not None and existing_file(invoice):
            groups.setdefault(key, []).append(invoice)
    removed = set()
    for group in groups.values():
        # Preserve payment evidence and richer recognized details where possible.
        retained = max(group, key=lambda item: (
            bool(getattr(item, 'payment_screenshot', '') and Path(item.payment_screenshot).is_file()),
            bool(getattr(item, 'items', [])),
        ))
        for duplicate in group:
            if duplicate is retained:
                continue
            try:
                backup = archive_duplicate(duplicate, retained)
            except OSError:
                log('重复发票备份失败，已保留原文件：' + duplicate.filename)
                continue
            retained.selected = bool(getattr(retained, 'selected', False) or getattr(duplicate, 'selected', False))
            removed.add(id(duplicate))
            log('已清理重复发票：' + duplicate.filename + '；保留：' + retained.filename +
                ('；备份：' + str(backup) if backup else ''))
    invoices[:] = [item for item in invoices if id(item) not in removed]
    return len(removed)
