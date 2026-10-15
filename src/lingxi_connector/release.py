"""Allowlisted portable distribution, excluding all local state and evidence."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

FILES=('安装.cmd','setup_wizard.py','run.py','write_config.ps1','Start.ps1','README_先看这里.md','requirements-portable.lock','PORTABLE-RUNTIME-SOURCE.json')
def sha(data):return hashlib.sha256(data).hexdigest()

def verify_manifest(root):
    root=Path(root).resolve()
    manifest=json.loads((root/'MANIFEST-SHA256.json').read_text(encoding='utf-8'))
    files=manifest.get('files')
    if not isinstance(files,dict) or not files or len(files)>10000:raise ValueError('Invalid manifest')
    for name,expected in files.items():
        parts=PurePosixPath(name)
        if parts.is_absolute() or '..' in parts.parts or '\\' in name or ':' in name:raise ValueError('Invalid manifest path')
        path=root.joinpath(*parts.parts)
        if not path.resolve().is_relative_to(root) or path.is_symlink():raise ValueError('Linked manifest target')
        if sha(path.read_bytes())!=expected:raise ValueError('Package hash mismatch')
    return True

def build(root,destination):
    root=Path(root).resolve();destination=Path(destination).resolve()
    paths=[root/name for name in FILES]
    paths.extend((root/'vendor').rglob('*'))
    paths.extend((root/'src').rglob('*.py'))
    paths.append(root/'skills/lingxi-cloud-memory/SKILL.md')
    entries={}
    for path in paths:
        if path.is_symlink():raise ValueError('Linked source refused')
        if not path.is_file():continue
        name=path.relative_to(root).as_posix()
        if '__pycache__' in path.parts or path.suffix in ('.pyc','.pyo','.log'):continue
        entries[name]=path
    if any(name not in entries for name in FILES):raise ValueError('Required product file missing')
    manifest={'version':'0.3.2','platform':'Windows x64','files':{name:sha(path.read_bytes()) for name,path in sorted(entries.items())}}
    destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('xb') as stream:
        with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for name,path in sorted(entries.items()):archive.write(path,name)
            archive.writestr('MANIFEST-SHA256.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    return {'path':str(destination),'sha256':sha(destination.read_bytes()),'bytes':destination.stat().st_size,'files':len(entries)+1,'runtime_included':False,'credentials_included':False}
