"""Create a distributable archive without local account or invoice data."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'validation_downloads' / 'release'


def package():
    version_source = (ROOT / 'src' / 'invoice_extensions.py').read_text(encoding='utf8')
    import re
    version = re.search(r"cls.VERSION = '([^']+)'", version_source).group(1)
    for source in (ROOT / 'src').glob('invoice_*.py'):
        deployed = ROOT / 'invoice_manager_gui' / '_internal' / source.name
        if not deployed.exists() or source.read_bytes() != deployed.read_bytes():
            raise RuntimeError(f'Rebuild the application before packaging: {source.name}')
    files = [ROOT / name for name in ('README.md', '启动发票管理器.bat', 'invoice_manager_gui/invoice_manager_gui.exe', 'invoice_manager_gui/config.example.json')]
    for path in (ROOT / 'invoice_manager_gui' / '_internal').rglob('*'):
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        if path.name in ('config.json', 'invoice_metadata.json') or path.suffix in ('.log', '.pyc'):
            continue
        files.append(path)
    config_path = ROOT / 'invoice_manager_gui' / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf8')) if config_path.exists() else {}
    private_values = [str(config[key]).encode('utf8') for key in ('email', 'auth_code') if config.get(key)]
    for path in files:
        content = path.read_bytes()
        if any(value in content for value in private_values):
            raise RuntimeError(f'Private account configuration detected in {path.relative_to(ROOT)}')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    archive = OUTPUT / f'invoice_manager_v{version}_windows_x64.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(files):
            bundle.write(path, path.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None:
            raise RuntimeError('Release archive failed integrity verification')
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    print(json.dumps({'archive': str(archive), 'files': len(files), 'bytes': archive.stat().st_size, 'sha256': checksum}, ensure_ascii=False))


if __name__ == '__main__':
    package()
