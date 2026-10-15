"""Double-click entrypoint bootstrap."""
import runpy
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent
runpy.run_path(str(ROOT/'run.py'),run_name='connector_bootstrap')
from lingxi_connector.wizard import main
from lingxi_connector.setup import InstallerError

if __name__=='__main__':
    try:main(ROOT)
    except InstallerError as error:
        print('安装未完成：'+str(error));sys.exit(2)
    except KeyboardInterrupt:
        print('已取消；已完成的步骤保留，下次可继续。');sys.exit(2)
    except Exception as error:
        print('安装检查未完成（'+type(error).__name__+'）；已完成步骤和私有恢复记录保留。');sys.exit(2)
