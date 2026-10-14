"""Build with stdlib pathlib even when Anaconda has the obsolete backport installed."""
import importlib.metadata
import sys
from pathlib import Path

original_distribution = importlib.metadata.distribution


def distribution(name):
    if name in ('enum34', 'typing', 'pathlib'):
        raise importlib.metadata.PackageNotFoundError(name)
    return original_distribution(name)


if __name__ == '__main__':
    importlib.metadata.distribution = distribution
    from PyInstaller.__main__ import run
    root = Path(__file__).resolve().parent
    run(['--noconfirm', '--distpath', str(root/'release'), str(root/'一站式题库助手.spec')])
