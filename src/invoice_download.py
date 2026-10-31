"""QR decoding, MIME extraction and bounded, validated invoice downloads."""
from __future__ import annotations

import hashlib
import html
import io
import json
import re
import threading
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MAX_BYTES = 40 * 1024 * 1024
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36'


class DownloadError(Exception):
    pass


def normalize_url(value):
    value = html.unescape(value.strip()).replace('\\/', '/')
    # QQ sometimes wraps the actual invoice address in a safety redirect.
    for _ in range(4):
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ('http', 'https') or not parsed.hostname:
            raise DownloadError('二维码内容不是下载链接。请使用“二维码交付”中的二维码，发票票面查验码不能下载文件。')
        if parsed.username or parsed.password:
            raise DownloadError('下载链接格式无效。')
        wrapped = parse_qs(parsed.query)
        if parsed.hostname.lower() in ('mail.qq.com', 'wx.mail.qq.com', 'url.cn', 'c.pc.qq.com'):
            target = next((wrapped[k][0] for k in ('url', 'target', 'pfurl', 'tourl') if wrapped.get(k)), None)
            if target and target.startswith(('http://', 'https://')):
                value = target
                continue
        return value
    raise DownloadError('下载链接跳转层数过多。')


def decode_qr(image):
    import zxingcpp
    from PIL import Image, ImageOps
    if isinstance(image, (str, Path)):
        with Image.open(image) as original:
            picture = ImageOps.exif_transpose(original).convert('RGB')
    else:
        picture = ImageOps.exif_transpose(image).convert('RGB')
    values = []
    for result in zxingcpp.read_barcodes(picture):
        if result.text and result.text not in values:
            values.append(result.text)
    if not values:
        raise DownloadError('未识别到二维码，请选择清晰、完整的二维码截图。')
    links = []
    for value in values:
        try:
            link = normalize_url(value)
        except DownloadError:
            continue
        if link not in links:
            links.append(link)
    if not links:
        raise DownloadError('已识别二维码，但内容不是下载网址。发票票面查验码不能直接下载。')
    return links


def pdf_valid(data):
    return bool(data and data[:1024].lstrip(b'\xef\xbb\xbf\x00\t\r\n ').startswith(b'%PDF-') and b'%%EOF' in data[-4096:])


def pdfs_in_payload(data, name='invoice.pdf'):
    if pdf_valid(data):
        return [(name if name.lower().endswith('.pdf') else 'invoice.pdf', data)]
    if data[:4] != b'PK\x03\x04':
        return []
    result = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        consumed = 0
        for item in archive.infolist():
            if not item.filename.lower().endswith('.pdf') or item.is_dir():
                continue
            consumed += item.file_size
            if consumed > MAX_BYTES or len(result) >= 30:
                raise DownloadError('发票压缩包过大，请手动解压后导入。')
            content = archive.read(item)
            if pdf_valid(content):
                result.append((Path(item.filename.replace('\\', '/')).name, content))
    return result


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        for key in ('href', 'src', 'data-url', 'data-href', 'data-pdf-url'):
            if attributes.get(key):
                self.links.append(attributes[key])
        if tag == 'meta' and attributes.get('http-equiv', '').lower() == 'refresh':
            match = re.search(r'url\s*=\s*[\'\"]?(.+?)[\'\"]?$', attributes.get('content', ''), re.I)
            if match:
                self.links.append(match.group(1))


def extract_links(text, base=''):
    text = html.unescape(text).replace('\\/', '/').replace('\\u0026', '&')
    parser = LinkParser()
    try:
        parser.feed(text)
    except Exception:
        pass
    candidates = list(parser.links)
    candidates.extend(re.findall(r'https?://[^\s<>\"\'\\]+', text))
    candidates.extend(re.findall(r'''["'](?:pdfUrl|pdf_url|downloadUrl|download_url|fileUrl|file_url|url|href)["']\s*:\s*["']([^"']+)["']''', text, re.I))
    candidates.extend(re.findall(r'''(?:window\.)?location(?:\.href)?\s*=\s*["']([^"']+)["']''', text))
    result = []
    for value in candidates:
        value = urljoin(base, value.strip())
        try:
            value = normalize_url(value)
        except (DownloadError, ValueError):
            continue
        if value not in result:
            result.append(value)
    return result


def invoice_link(url):
    # Signed download URLs may use octet-stream and have no .pdf suffix.
    return bool(re.search(r'pdf|invoice|einvoice|fapiao|dzfp|download|/fp/|suwell|ofd', url, re.I))


def response_filename(disposition, url):
    extended = re.search(r"filename\*\s*=\s*([^;\r\n]+)", disposition, re.I)
    if extended:
        value = extended.group(1).strip().strip('"')
        fields = value.split("'", 2)
        if len(fields) == 3:
            try:
                return unquote(fields[2], encoding=fields[0] or 'utf-8')
            except LookupError:
                return unquote(fields[2])
    ordinary = re.search(r'filename\s*=\s*"([^"\r\n]+)"|filename\s*=\s*([^;\r\n]+)', disposition, re.I)
    value = (ordinary.group(1) or ordinary.group(2)).strip() if ordinary else unquote(urlsplit(url).path.rsplit('/', 1)[-1])
    # Some tax platforms put UTF-8 bytes in a Latin-1 HTTP header.
    try:
        value = value.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return unquote(value) or 'invoice.pdf'


def message_links(msg):
    links = []
    for part in msg.walk():
        if part.get_content_type() not in ('text/plain', 'text/html'):
            continue
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes):
            continue
        try:
            text = data.decode(part.get_content_charset() or 'utf-8', errors='replace')
        except LookupError:
            text = data.decode('utf-8', errors='replace')
        for link in extract_links(text):
            if invoice_link(link) and link not in links:
                links.append(link)
    return links


def message_pdfs(msg):
    result = []
    for index, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes) or len(data) > MAX_BYTES:
            continue
        name = part.get_filename() or f'invoice_{index}.pdf'
        try:
            result.extend(pdfs_in_payload(data, name))
        except (zipfile.BadZipFile, RuntimeError):
            continue
    return result


def tax_pdf_url(url):
    """Official digital-invoice delivery QR -> the site's PDF export endpoint.

    Both /v/<cs> and /qrcode?cs=<cs> encode the invoice number, issue time
    and delivery verification suffix. Never send these fields to other hosts.
    """
    parsed = urlsplit(url)
    if not (parsed.hostname or '').lower().endswith('.chinatax.gov.cn'):
        return None
    token = parsed.path[3:] if parsed.path.startswith('/v/') else parse_qs(parsed.query).get('cs', [''])[0]
    match = re.fullmatch(r'2_(\d{20})_(\d{14})[A-Za-z0-9]{1,12}([A-Za-z0-9]{4})', token)
    if not match:
        return None
    number, issued, verification = match.groups()
    params = {'Wjgs': 'PDF', 'Jym': verification, 'Fphm': number, 'Kprq': issued, 'Czsj': str(int(time.time() * 1000))}
    return f'{parsed.scheme}://{parsed.netloc}/kpfw/fpjfzz/v1/exportDzfpwjEwm?' + urlencode(params)


class Downloader:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        retries = Retry(total=2, connect=2, read=2, backoff_factor=0.5,
                        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=('GET',))
        self.session.mount('https://', HTTPAdapter(max_retries=retries))
        self.session.mount('http://', HTTPAdapter(max_retries=retries))
        self.session.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/pdf,application/json,text/html,*/*'})

    def close(self):
        self.session.close()

    def download(self, url):
        url = normalize_url(url)
        direct = tax_pdf_url(url)
        queue = [(direct, url), (url, None)] if direct else [(url, None)]
        seen = set()
        error = None
        while queue and len(seen) < 12:
            address, referer = queue.pop(0)
            if address in seen:
                continue
            seen.add(address)
            try:
                headers = {'Referer': referer} if referer else {}
                with self.session.get(address, headers=headers, timeout=(12, 35), stream=True) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise DownloadError('文件超过 40 MB，已停止下载。')
                        chunks.append(chunk)
                    data = b''.join(chunks)
                    disposition = response.headers.get('Content-Disposition', '')
                    filename = response_filename(disposition, response.url)
                    found = pdfs_in_payload(data, filename or 'invoice.pdf')
                    if found:
                        return found
                    if response.encoding is None or response.encoding.lower() == 'iso-8859-1':
                        response.encoding = 'utf-8'
                    text = response.text
                    candidates = extract_links(text, response.url)
                    candidates.sort(key=lambda value: (not bool(re.search(r'\.pdf(?:[?#]|$)', value, re.I)), not invoice_link(value)))
                    for link in candidates:
                        if invoice_link(link) and link not in seen:
                            queue.append((link, response.url))
                    error = DownloadError('链接返回了网页，未找到可下载的 PDF；可能已过期或需要在浏览器中验证。')
            except requests.RequestException as exc:
                code = getattr(getattr(exc, 'response', None), 'status_code', None)
                error = DownloadError(f'下载服务返回 HTTP {code}，请重试或重新获取链接。' if code else '网络连接失败或超时，请稍后重试。')
            except (zipfile.BadZipFile, RuntimeError):
                error = DownloadError('下载的压缩包损坏或已加密。')
        raise error or DownloadError('未获取到有效 PDF 文件。')


_save_lock = threading.RLock()


def safe_filename(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:130]
    if not value:
        return 'invoice'
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        value = '_' + value
    return value


def save_pdf(directory, filename, data):
    """Never overwrite; exact duplicate is a successful, idempotent download."""
    if not pdf_valid(data):
        raise DownloadError('收到的内容不是完整 PDF，未保存。')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(Path(filename.replace('\\', '/')).stem)
    digest = hashlib.sha256(data).digest()
    with _save_lock:
        for number in range(10000):
            path = directory / (stem + (f' ({number})' if number else '') + '.pdf')
            if path.exists():
                if path.stat().st_size == len(data) and hashlib.sha256(path.read_bytes()).digest() == digest:
                    return path, True
                continue
            try:
                with path.open('xb') as stream:
                    stream.write(data)
            except FileExistsError:
                continue
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return path, False
    raise DownloadError('同名文件过多，请换一个保存目录。')
