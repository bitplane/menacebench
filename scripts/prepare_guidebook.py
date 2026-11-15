"""Copy the upstream plain-text guidebook from the pinned game source."""
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    build = ROOT/'build'
    commit = json.loads((build/'build.json').read_text())['source_commit']
    shutil.copyfile(build/f'NetHack-{commit}'/'doc/Guidebook.txt', build/'Guidebook.txt')
