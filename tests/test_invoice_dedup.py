import json
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
from invoice_dedup import (BACKUP_DIRECTORY, deduplicate, extract_identity,
                           find_duplicate, invoice_key, normalize_amount)
import invoice_extensions as extension

PDF = b'%PDF-1.7\nfirst document\n%%EOF\n'
NUMBER = '26132000003448484866'
TEMP_ROOT = pathlib.Path(__file__).resolve().parents[1] / 'validation_downloads'
TEMP_ROOT.mkdir(exist_ok=True)


class Invoice(SimpleNamespace):
    def __init__(self, **kwargs):
        super().__init__(filename='', filepath='', invoice_number='', total_amount='',
                         payment_screenshot='', items=[], product_name='', selected=False)
        self.__dict__.update(kwargs)


def invoice_file(folder, name, **kwargs):
    path = pathlib.Path(folder) / name
    path.write_bytes(PDF)
    fields = dict(invoice_number=NUMBER, total_amount='108.00')
    fields.update(kwargs)
    return Invoice(filename=name, filepath=str(path), **fields)


class IdentityTests(unittest.TestCase):
    def test_number_and_decimal_amount_normalization(self):
        a = Invoice(invoice_number=NUMBER, total_amount='￥1,108.00元')
        b = Invoice(invoice_number='２６１３２０００００３４４８４８４８６６', total_amount='1108')
        self.assertEqual(invoice_key(a), invoice_key(b))
        self.assertIsNotNone(invoice_key(a))

    def test_incomplete_or_invalid_identity_never_matches(self):
        for fields in ({}, {'invoice_number': NUMBER}, {'total_amount': '108.00'},
                       {'invoice_number': '123456789'},
                       {'invoice_number': 26132000003448484866},
                       {'total_amount': 'NaN'}, {'total_amount': 'Infinity'},
                       {'total_amount': '108.001'}, {'total_amount': '1,08.00'}):
            defaults = dict(invoice_number=NUMBER, total_amount='108.00')
            if fields:
                defaults.update(fields)
            else:
                defaults = {}
            if fields == {'invoice_number': NUMBER}:
                defaults.pop('total_amount')
            if fields == {'total_amount': '108.00'}:
                defaults.pop('invoice_number')
            with self.subTest(fields=fields):
                self.assertIsNone(invoice_key(Invoice(**defaults)))

    def test_zero_and_negative_amounts_are_valid(self):
        self.assertIsNotNone(invoice_key(Invoice(invoice_number=NUMBER, total_amount='0.00')))
        self.assertEqual(normalize_amount('-108.00'), -108)

    def test_traditional_invoice_requires_matching_code(self):
        a = Invoice(invoice_number='00123456', total_amount='108')
        self.assertIsNone(invoice_key(a))
        a.invoice_code = '012345678901'
        b = Invoice(invoice_number='00123456', invoice_code='012345678902', total_amount='108')
        self.assertIsNotNone(invoice_key(a))
        self.assertNotEqual(invoice_key(a), invoice_key(b))

    def test_extract_printed_identity_and_grand_total(self):
        item = Invoice(total_amount='107.99')
        extract_identity('发 票 号 码：２６１３２０００００３４４８４８４８６６\n'
                         '价税合计（大写） 壹佰零捌圆整 （小写）¥108.00', item)
        self.assertEqual(item.invoice_number, NUMBER)
        self.assertEqual(item.total_amount, '108.00')

    def test_extract_traditional_number_preserves_zeroes(self):
        item = Invoice()
        extract_identity('发票代码：012345678901\n发票号码：00123456\n价税合计:￥108.00', item)
        self.assertEqual(invoice_key(item), ('00123456', normalize_amount('108'), '012345678901'))


class CleanupTests(unittest.TestCase):
    def test_different_filenames_and_pdf_bytes_are_archived(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder:
            first = invoice_file(folder, '原件.pdf')
            other = invoice_file(folder, '再次下载.pdf', total_amount='108')
            pathlib.Path(other.filepath).write_bytes(PDF.replace(b'first', b'regenerated'))
            other.selected = True
            invoices = [first, other]
            self.assertEqual(deduplicate(invoices), 1)
            self.assertEqual(invoices, [first])
            self.assertTrue(first.selected)
            self.assertEqual(pathlib.Path(first.filepath).read_bytes(), PDF)
            self.assertFalse(pathlib.Path(other.filepath).exists())
            backup = list((pathlib.Path(folder) / BACKUP_DIRECTORY).glob('*/*.pdf'))
            self.assertEqual(len(backup), 1)
            self.assertIn(b'regenerated', backup[0].read_bytes())
            manifest = json.loads((backup[0].parent / '恢复信息.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['invoice']['invoice_number'], NUMBER)
            self.assertEqual(manifest['original_file'], other.filepath)
            self.assertEqual(deduplicate(invoices), 0)

    def test_payment_screenshot_record_is_retained(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder:
            screenshot = pathlib.Path(folder) / '付款.png'
            screenshot.write_bytes(b'payment proof')
            first = invoice_file(folder, 'first.pdf')
            other = invoice_file(folder, 'other.pdf', payment_screenshot=str(screenshot))
            invoices = [first, other]
            self.assertEqual(deduplicate(invoices), 1)
            self.assertEqual(invoices, [other])
            self.assertTrue(screenshot.exists())

    def test_same_amount_different_number_and_same_number_different_amount_stay(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder:
            invoices = [invoice_file(folder, 'a.pdf'),
                        invoice_file(folder, 'b.pdf', invoice_number='26132000003448484867'),
                        invoice_file(folder, 'c.pdf', total_amount='109'),
                        invoice_file(folder, 'd.pdf', invoice_number='')]
            self.assertEqual(deduplicate(invoices), 0)
            self.assertEqual(len(list(pathlib.Path(folder).glob('*.pdf'))), 4)

    def test_archive_failure_keeps_both_files_and_records(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder:
            invoices = [invoice_file(folder, 'a.pdf'), invoice_file(folder, 'b.pdf')]
            with patch('invoice_dedup.Path.rename', side_effect=PermissionError):
                self.assertEqual(deduplicate(invoices), 0)
            self.assertEqual(len(invoices), 2)
            self.assertTrue(all(pathlib.Path(item.filepath).exists() for item in invoices))

    def test_same_path_records_are_collapsed_without_moving_file(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder:
            first = invoice_file(folder, 'a.pdf')
            other = Invoice(**vars(first))
            invoices = [first, other]
            self.assertEqual(deduplicate(invoices), 1)
            self.assertTrue(pathlib.Path(first.filepath).exists())
            self.assertFalse((pathlib.Path(folder) / BACKUP_DIRECTORY).exists())

    def test_missing_file_does_not_prevent_new_download(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder:
            first = invoice_file(folder, 'a.pdf')
            pathlib.Path(first.filepath).unlink()
            self.assertIsNone(find_duplicate([first], first))


class DownloadIntegrationTests(unittest.TestCase):
    def test_download_reuses_identity_across_names_dates_and_contents(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder:
            def recognize(item):
                item.invoice_number, item.total_amount = NUMBER, '108.00'
            app = SimpleNamespace(invoices=[], recognize_invoice=recognize, log=lambda msg: None)
            email = SimpleNamespace(email_id='test', date='2026-10-20')
            with patch.dict(extension.LEGACY, InvoiceInfo=Invoice):
                first = extension.save_attachment(app, folder, email, 'one.pdf', PDF, '商品甲')
                email.date = '2026-10-31'
                again = extension.save_attachment(app, folder, email, 'two.pdf', PDF.replace(b'first', b'new'), '商品乙')
            self.assertTrue(first)
            self.assertEqual(first, again)
            self.assertEqual(len(app.invoices), 1)
            self.assertEqual(len(list(pathlib.Path(folder).iterdir())), 1)

    def test_same_amount_different_invoice_number_downloads_both(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder:
            numbers = iter([NUMBER, '26132000003448484867'])
            def recognize(item):
                item.invoice_number, item.total_amount = next(numbers), '108.00'
            app = SimpleNamespace(invoices=[], recognize_invoice=recognize, log=lambda msg: None)
            email = SimpleNamespace(email_id='test', date='2026-10-20')
            with patch.dict(extension.LEGACY, InvoiceInfo=Invoice):
                first = extension.save_attachment(app, folder, email, 'one.pdf', PDF, '商品')
                other = extension.save_attachment(app, folder, email, 'one.pdf', PDF.replace(b'first', b'new'), '商品')
            self.assertNotEqual(first, other)
            self.assertEqual(len(app.invoices), 2)


if __name__ == '__main__':
    unittest.main()
