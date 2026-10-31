import io
import pathlib
import sys
import tempfile
import unittest
import zipfile
from email.message import EmailMessage

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))
from invoice_download import (DownloadError, Downloader, extract_links, message_links,
                              message_pdfs, normalize_url, pdf_valid, pdfs_in_payload, save_pdf, tax_pdf_url, response_filename)

PDF = b'%PDF-1.7\nexample test document\n%%EOF\n'


class Response:
    encoding = 'utf-8'
    def __init__(self, url, content, headers=None):
        self.url, self.content = url, content
        self.headers = headers or {}
    @property
    def text(self):
        return self.content.decode('utf-8', errors='replace')
    def iter_content(self, size):
        yield self.content
    def raise_for_status(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


class Session:
    def __init__(self, routes):
        self.routes, self.headers, self.calls = routes, {}, []
    def mount(self, *args):
        pass
    def get(self, url, **kwargs):
        self.calls.append(url)
        return Response(url, self.routes[url])
    def close(self):
        pass


class DownloadTests(unittest.TestCase):
    def test_chinese_http_filenames(self):
        self.assertEqual(response_filename('attachment; filename="' + '发票.pdf'.encode('utf8').decode('latin1') + '"', 'https://example.test/file'), '发票.pdf')
        self.assertEqual(response_filename("attachment; filename*=UTF-8''%E5%8F%91%E7%A5%A8.pdf", 'https://example.test/file'), '发票.pdf')

    def test_official_tax_delivery_qr(self):
        from urllib.parse import urlsplit, parse_qs
        link = tax_pdf_url('https://dppt.example.chinatax.gov.cn/v/2_12345678901234567890_2026103015570850Q6X4345')
        parsed = urlsplit(link)
        self.assertEqual(parsed.hostname, 'dppt.example.chinatax.gov.cn')
        params = parse_qs(parsed.query)
        self.assertEqual(params['Jym'], ['4345'])
        self.assertEqual(params['Kprq'], ['20261030155708'])
        self.assertEqual(params['Fphm'], ['12345678901234567890'])
        self.assertEqual(params['Wjgs'], ['PDF'])

    def test_tax_adapter_does_not_rewrite_other_hosts(self):
        self.assertIsNone(tax_pdf_url('https://chinatax.gov.cn.example.test/v/2_12345678901234567890_2026103015570850Q6X4345'))

    def test_exact_duplicate_and_distinct_same_name(self):
        with tempfile.TemporaryDirectory() as directory:
            first, duplicate = save_pdf(directory, '日期_连接器.pdf', PDF)
            again, duplicate = save_pdf(directory, '日期_连接器.pdf', PDF)
            self.assertTrue(duplicate)
            self.assertEqual(first, again)
            other, duplicate = save_pdf(directory, '日期_连接器.pdf', PDF.replace(b'example', b'another'))
            self.assertFalse(duplicate)
            self.assertNotEqual(first, other)
            self.assertEqual(first.read_bytes(), PDF)

    def test_html_is_not_saved_as_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DownloadError):
                save_pdf(directory, 'error.pdf', b'<html>expired</html>')
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])

    def test_truncated_pdf_rejected(self):
        self.assertFalse(pdf_valid(b'%PDF-1.7\ntruncated'))

    def test_path_components_cannot_escape_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _ = save_pdf(directory, '../../CON.pdf', PDF)
            self.assertEqual(path.parent, pathlib.Path(directory))
            self.assertEqual(path.name, '_CON.pdf')

    def test_inline_unnamed_pdf(self):
        msg = EmailMessage()
        msg.set_content('发票')
        msg.add_attachment(PDF, maintype='application', subtype='pdf', disposition='inline')
        self.assertEqual(message_pdfs(msg)[0][1], PDF)

    def test_pdf_from_zip_with_unsafe_member_path(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('../../invoice.pdf', PDF)
            archive.writestr('error.pdf', '<html>not a pdf</html>')
        self.assertEqual(pdfs_in_payload(buffer.getvalue()), [('invoice.pdf', PDF)])

    def test_qq_redirect_preserves_encoded_signature(self):
        url = normalize_url('https://mail.qq.com/cgi-bin/readtemplate?url=https%3A%2F%2Fexample.test%2Finvoice%3Ftoken%3Da%252Fb%252Bc')
        self.assertEqual(url, 'https://example.test/invoice?token=a%2Fb%2Bc')

    def test_mime_html_entities(self):
        msg = EmailMessage()
        msg.set_content('<a href="https://example.test/download?x=1&amp;y=2">发票</a>', subtype='html')
        self.assertEqual(message_links(msg), ['https://example.test/download?x=1&y=2'])

    def test_tax_verification_code_is_not_a_download_url(self):
        with self.assertRaises(DownloadError):
            normalize_url('01,10,123456,12345678,47.50,20261030')

    def test_octet_stream_pdf_and_relative_json_link(self):
        session = Session({'https://example.test/invoice': b'{"downloadUrl":"/file.pdf?token=a%2Fb"}', 'https://example.test/file.pdf?token=a%2Fb': PDF})
        files = Downloader(session).download('https://example.test/invoice')
        self.assertEqual(files[0][1], PDF)
        self.assertEqual(len(session.calls), 2)

    def test_circular_html_links_are_bounded(self):
        session = Session({'https://example.test/invoice': b'<a href="/invoice">again</a>'})
        with self.assertRaises(DownloadError):
            Downloader(session).download('https://example.test/invoice')
        self.assertEqual(len(session.calls), 1)

    def test_html_login_page_not_success(self):
        session = Session({'https://example.test/invoice': b'<html>please login</html>'})
        with self.assertRaises(DownloadError):
            Downloader(session).download('https://example.test/invoice')


if __name__ == '__main__':
    unittest.main()
