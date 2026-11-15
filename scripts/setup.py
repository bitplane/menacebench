#!/usr/bin/env python3
"""Fetch hash-checked upstream archives and build the tracked patch series."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build'
COMMIT = '16ff59115315917b93185d026aeefea06db9b0f4'
SOURCES = {
    'nethack.tar.gz': (f'https://codeload.github.com/NetHack/NetHack/tar.gz/{COMMIT}', '7ac4e7d63e8cbbec70c2a3bd11bf58bfd35952cb380aee10c17b8ce024a8a9d0'),
    'lua.tar.gz': ('https://www.lua.org/ftp/lua-5.4.8.tar.gz', '4f18ddae154e793e46eeab727c59ef1c0c0c2b744e7b94219710d76f530629ae'),
}


def run(*args, cwd=ROOT, **kwargs):
    subprocess.run(args, cwd=cwd, check=True, **kwargs)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    downloads = BUILD / 'downloads'
    downloads.mkdir(parents=True, exist_ok=True)
    for name, (url, sha) in SOURCES.items():
        target = downloads / name
        if not target.exists():
            run('curl', '-fL', '--retry', '3', url, '-o', str(target))
        if digest(target) != sha:
            raise SystemExit(f'Archive checksum mismatch: {target}')
    src = BUILD / f'NetHack-{COMMIT}'
    if src.exists():
        shutil.rmtree(src)
    with tarfile.open(downloads / 'nethack.tar.gz') as archive:
        archive.extractall(BUILD, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
    # Isolate git apply from any enclosing checkout: otherwise git can silently
    # skip paths when invoked from a generated subdirectory of the repository.
    run('git', 'init', '-q', cwd=src)
    for patch in sorted((ROOT / 'patches').glob('*.patch')):
        run('git', 'apply', '--no-index', '--recount', str(patch), cwd=src)
    shutil.rmtree(src / '.git')
    assert 'harness_consumed(i)' in (src/'win/tty/wintty.c').read_text()
    assert 'NETHACK_SEED' in (src/'sys/unix/unixmain.c').read_text()
    (src / 'lib').mkdir(exist_ok=True)
    with tarfile.open(downloads / 'lua.tar.gz') as archive:
        archive.extractall(src / 'lib', **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
    hints = (src / 'sys/unix/hints/linux-minimal').read_text()
    hints = hints.replace('PREFIX=$(wildcard ~)/nethack-minimal', 'PREFIX=' + str(BUILD / 'install'))
    flags = '-O2 -I../include -DNOMAIL -DNOSHELL -DNOSUSPEND'
    # Optional locally extracted system headers; containers use installed headers.
    headers = BUILD / 'deps/usr/include'
    if (headers / 'ncurses.h').exists():
        flags += ' -I' + str(headers)
    hints = hints.replace('CFLAGS=-g -I../include', 'CC=gcc\nCFLAGS=' + flags)
    hints = hints.replace('WINTTYLIB=-lncurses -ltinfo', 'WINTTYLIB=-l:libncurses.so.6 -l:libtinfo.so.6')
    (src / 'sys/unix/hints/harness').write_text(hints)
    env = dict(os.environ, SOURCE_DATE_EPOCH='1777723200')
    run('sh', 'sys/unix/setup.sh', 'sys/unix/hints/harness', cwd=src, env=env)
    # Upstream recursive targets can concurrently rebuild the same Lua archive.
    # Serialize the build to avoid truncated objects on fresh builds.
    run('make', '-j1', cwd=src, env=env)
    run('make', 'install', cwd=src, env=env)
    game = BUILD / 'install/games/lib/nethackdir'
    shutil.copyfile(ROOT / 'config/sysconf', game / 'sysconf')
    manifest = {
        'source_commit': COMMIT,
        'archives': {name: sha for name, (_, sha) in SOURCES.items()},
        'patches': {p.name: digest(p) for p in sorted((ROOT / 'patches').glob('*.patch'))},
        'options_digest': digest(ROOT / 'config/nethackrc'),
        'sysconf_digest': digest(ROOT / 'config/sysconf'),
        'patch_set_digest': hashlib.sha256(b''.join(p.name.encode()+b'\0'+p.read_bytes() for p in sorted((ROOT/'patches').glob('*.patch')))).hexdigest(),
        'compiler': subprocess.check_output(['gcc', '--version'], text=True).splitlines()[0],
        'executable_digest': digest(game / 'nethack'),
        'profile': hints,
    }
    (BUILD / 'build.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Built', game / 'nethack')

if __name__ == '__main__':
    main()
