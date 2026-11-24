"""Build directly from the same source that runs in development."""
import subprocess
import sys
from pathlib import Path
root = Path(__file__).resolve().parent
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                str(root / 'TikTokVideoCollector.spec')], cwd=root, check=True)
