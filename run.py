"""Portable entry point; dependencies and source are private to this product."""
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
ROOT = Path(__file__).resolve().parent
if __name__ == '__main__':
    # Stable launcher gate: block new stdio/imports while a multi-file transaction
    # is incomplete. The updater cannot replace this launcher in its first scope.
    active=ROOT/'runtime/session/maintenance/active.json'
    import json
    def check_gate():
      if active.exists():
        try:
            if active.is_symlink() or active.stat().st_size>4096:raise ValueError()
            phase=json.loads(active.read_text(encoding='utf-8')).get('phase')
            if phase!='ready':raise ValueError()
        except (OSError,ValueError,TypeError,AttributeError):
            sys.stderr.write('MAINTENANCE_IN_PROGRESS: run explicit recovery before starting.\n')
            raise SystemExit(4)
    check_gate()
    # Coordinate imports with the updater, then recheck to close the check/import race.
    lock_path=ROOT/'runtime/session/maintenance/startup.lock'
    if ROOT.resolve() not in lock_path.resolve().parents:
        raise SystemExit(4)
    for part in (lock_path,*lock_path.parents):
        if part==ROOT:break
        if part.is_symlink() or getattr(part,'is_junction',lambda:False)():raise SystemExit(4)
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    _startup_stream=lock_path.open('a+b')
    try:
        if os.name=='nt':
            import msvcrt
            if lock_path.stat().st_size==0:_startup_stream.write(b'0');_startup_stream.flush()
            _startup_stream.seek(0);msvcrt.locking(_startup_stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(_startup_stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        _startup_stream.close();sys.stderr.write('MAINTENANCE_STARTUP_BUSY\n');raise SystemExit(4)
    def release_startup():
        if not _startup_stream.closed:
            if os.name=='nt':
                _startup_stream.seek(0);msvcrt.locking(_startup_stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(_startup_stream.fileno(),fcntl.LOCK_UN)
            _startup_stream.close()
    sys._lx_release_startup=release_startup
    check_gate()
temporary = ROOT / 'runtime' / 'tmp'
temporary.mkdir(parents=True, exist_ok=True)
os.environ['TEMP'] = str(temporary)
os.environ['TMP'] = str(temporary)
dependency_root = ROOT / 'vendor' / 'deps' if Path(sys.executable).resolve() == (ROOT / 'vendor' / 'python' / 'python.exe').resolve() else ROOT / 'runtime' / 'deps'
sys.path[:0] = [str(ROOT / 'src'), str(dependency_root)]
import site
site.addsitedir(str(dependency_root))

def use_utf8_streams():
    """Agent-facing JSON must be UTF-8 even when stdout/stderr are pipes.

    A console is already UTF-8 on Windows (PEP 528); a redirected stream is not, and would
    otherwise carry Chinese in the machine's ANSI code page, which a UTF-8 reader cannot decode.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError, OSError):
            pass


if __name__ == '__main__':
    use_utf8_streams()
    from lingxi_connector.cli import main
    try:main(ROOT)
    finally:getattr(sys,'_lx_release_startup',lambda:None)()
