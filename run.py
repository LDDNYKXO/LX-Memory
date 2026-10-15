"""Portable entry point; dependencies and source are private to this product."""
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
ROOT = Path(__file__).resolve().parent
temporary = ROOT / 'runtime' / 'tmp'
temporary.mkdir(parents=True, exist_ok=True)
os.environ['TEMP'] = str(temporary)
os.environ['TMP'] = str(temporary)
dependency_root = ROOT / 'vendor' / 'deps' if Path(sys.executable).resolve() == (ROOT / 'vendor' / 'python' / 'python.exe').resolve() else ROOT / 'runtime' / 'deps'
sys.path[:0] = [str(ROOT / 'src'), str(dependency_root)]
import site
site.addsitedir(str(dependency_root))

if __name__ == '__main__':
    from lingxi_connector.cli import main
    main(ROOT)
