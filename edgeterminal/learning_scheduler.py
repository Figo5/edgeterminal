"""Independent five-minute macOS launcher for offline learning jobs."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
from pathlib import Path

from .config import ROOT, settings
from .research_scheduler import _domain, _launchctl

LABEL = 'com.edgeterminal.learning'
PYTHON = ROOT / '.venv/bin/python'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
LOG_DIR = Path.home() / 'Library/Logs/EdgeTerminal'


def specification():
    return {'Label': LABEL, 'ProgramArguments': [str(PYTHON), '-m', 'edgeterminal.learning_worker', 'run'],
            'WorkingDirectory': str(ROOT), 'StartInterval': 300, 'RunAtLoad': True,
            'ProcessType': 'Background', 'Nice': 10,
            'EnvironmentVariables': {'EDGETERMINAL_OFFLINE_LEARNER': '1', 'PYTHONDONTWRITEBYTECODE': '1',
                'DATABASE_URL': settings.database_url, 'NFLVERSE_CACHE_DIR': str(settings.nflverse_cache_dir),
                'TIMEZONE': settings.timezone,
                'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
                'NUMEXPR_NUM_THREADS': '1', 'VECLIB_MAXIMUM_THREADS': '1'},
            'StandardOutPath': str(LOG_DIR / 'learning.out.log'),
            'StandardErrorPath': str(LOG_DIR / 'learning.err.log')}


def install():
    if os.uname().sysname != 'Darwin':
        raise RuntimeError('Learning LaunchAgent requires macOS')
    if not PYTHON.is_file():
        raise FileNotFoundError('Learning virtualenv Python is missing')
    PLIST.parent.mkdir(parents=True, exist_ok=True); LOG_DIR.mkdir(parents=True, exist_ok=True)
    raw = plistlib.dumps(specification(), fmt=plistlib.FMT_XML)
    if PLIST.exists() and PLIST.read_bytes() != raw:
        raise ValueError('Installed learning LaunchAgent differs; review before replacing it')
    if not PLIST.exists():
        with PLIST.open('xb') as stream:
            stream.write(raw)
    result = _launchctl('bootstrap', _domain(), str(PLIST))
    if result.returncode and _launchctl('print', _domain() + '/' + LABEL).returncode:
        raise RuntimeError('Learning LaunchAgent could not be loaded')
    return status()


def status():
    supported = os.uname().sysname == 'Darwin'
    return {'supported': supported, 'installed': PLIST.is_file(),
            'loaded': supported and _launchctl('print', _domain() + '/' + LABEL).returncode == 0,
            'label': LABEL, 'interval_seconds': 300}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'status'))
    args = parser.parse_args()
    print(json.dumps(install() if args.action == 'install' else status(), sort_keys=True))


if __name__ == '__main__':
    main()
