"""Rebuild the existing Python 3.12 PyInstaller entry while retaining its modules.

Run with tools/python312/python.exe. The original executable is backed up once.
Only the application entry is replaced; the original UI runs with maintained
extensions from src/invoice_extensions.py. No account configuration is bundled.
"""
import marshal
import pathlib
import shutil
import struct
import sys
import zipfile
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / 'invoice_manager_gui'
MAGIC = b'MEI\014\013\012\013\016'


def build():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit('Use Python 3.12 to compile the packaged application.')
    target = APP / 'invoice_manager_gui.exe'
    backup = APP / 'invoice_manager_gui.original.exe'
    blob = (backup if backup.exists() else target).read_bytes()
    cookie = blob.rfind(MAGIC)
    _, total, toc_offset, toc_size, version, lib = struct.unpack('!8sIIII64s', blob[cookie:cookie + 88])
    start = cookie + 88 - total
    entries = []
    p = start + toc_offset
    while p < start + toc_offset + toc_size:
        length, offset, size, unpacked, compressed, kind = struct.unpack('!IIIIBc', blob[p:p + 18])
        name = blob[p + 18:p + length].rstrip(b'\0')
        entries.append([name, kind, compressed, unpacked, blob[start + offset:start + offset + size]])
        p += length
    internal = APP / '_internal'
    legacy_entry = next(entry for entry in entries if entry[0] == b'invoice_manager_gui')
    original = zlib.decompress(legacy_entry[4]) if legacy_entry[2] else legacy_entry[4]
    if 'InvoiceManager' not in marshal.loads(original).co_names:
        # A previous distribution may have copied the patched launcher to the
        # backup filename. Never use that launcher as its own legacy module.
        for candidate in (ROOT / 'src' / 'invoice_legacy.marshal', internal / 'invoice_legacy.marshal', ROOT / 'tools' / 'recovered' / 'invoice_manager_gui.marshal'):
            if candidate.exists() and 'InvoiceManager' in marshal.loads(candidate.read_bytes()).co_names:
                original = candidate.read_bytes()
                break
        else:
            raise SystemExit('The original application bytecode is missing; the current application was not modified.')
    (ROOT / 'src' / 'invoice_legacy.marshal').write_bytes(original)
    legacy_entry[2:5] = [1, len(original), zlib.compress(original)]

    def executable(entry_list):
        data = bytearray()
        toc = bytearray()
        for name, kind, compressed, unpacked, payload in entry_list:
            name_field = name + b'\0'
            size = (18 + len(name_field) + 15) // 16 * 16
            toc.extend(struct.pack('!IIIIBc', size, len(data), len(payload), unpacked, compressed, kind))
            toc.extend(name_field.ljust(size - 18, b'\0'))
            data.extend(payload)
        return blob[:start] + data + toc + struct.pack('!8sIIII64s', MAGIC, len(data) + len(toc) + 88, len(data), len(toc), version, lib)

    # Keep a runnable, unpatched rollback executable even if the supplied backup
    # was itself already patched.
    baseline = executable(entries)
    if not backup.exists() or backup.read_bytes() != baseline:
        backup.write_bytes(baseline)
    for entry in entries:
        if entry[0] == b'invoice_manager_gui':
            (internal / 'invoice_legacy.marshal').write_bytes(original)
            source = '''import marshal, os, sys
sys.path.insert(0, os.path.join(sys._MEIPASS, 'python312.zip'))
namespace = {'__name__': 'invoice_legacy', '__file__': __file__}
with open(os.path.join(sys._MEIPASS, 'invoice_legacy.marshal'), 'rb') as f:
    exec(marshal.loads(f.read()), namespace)
from invoice_extensions import install, launch
install(namespace)
if '--validate' in sys.argv:
    import runpy
    runpy.run_path(sys.argv[sys.argv.index('--validate') + 1], init_globals={'legacy': namespace})
else:
    launch(namespace)
'''
            compiled = marshal.dumps(compile(source, 'invoice_manager_gui.py', 'exec'))
            entry[2:5] = [1, len(compiled), zlib.compress(compiled)]
    for name in ('invoice_extensions.py', 'invoice_download.py', 'invoice_dedup.py'):
        shutil.copy2(ROOT / 'src' / name, internal / name)
    shutil.copy2(ROOT / 'tools' / 'python312' / 'python312.zip', internal / 'python312.zip')
    license_dir = internal / 'licenses'
    license_dir.mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'tools' / 'python312' / 'LICENSE.txt', license_dir / 'Python-3.12-LICENSE.txt')
    wheel = next((ROOT / 'tools' / 'wheels').glob('zxing_cpp-*-cp312-*.whl'))
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.endswith('.pyd'):
                (internal / pathlib.PurePosixPath(name).name).write_bytes(archive.read(name))
            elif name.endswith('/licenses/LICENSE'):
                (license_dir / 'zxing-cpp-LICENSE.txt').write_bytes(archive.read(name))
    result = executable(entries)
    pending = target.with_suffix('.new.exe')
    pending.write_bytes(result)
    pending.replace(target)
    print('Updated executable; original retained as', backup.name)


if __name__ == '__main__':
    build()
