import importlib.util
import json
from pathlib import Path
import pytest
from nethack_run import images


@pytest.mark.parametrize('prefix', ['', 'sha256:'])
def test_record_image_accepts_engine_id_formats(tmp_path, monkeypatch, prefix):
    script = Path(__file__).resolve().parents[1]/'scripts/record_image.py'
    spec = importlib.util.spec_from_file_location('record_image', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, '__file__', str(tmp_path/'scripts/record_image.py'))
    monkeypatch.delenv('NETHACK_CONTAINER_ENGINE', raising=False)
    calls = []
    def inspect(command, **kwargs):
        calls.append(command)
        return prefix + 'a'*64 + '\n'
    monkeypatch.setattr(module.subprocess, 'check_output', inspect)
    module.record('localhost/nethack-run:local')
    assert calls[0][:3] == ['podman','image','inspect']
    assert (tmp_path/'build/image-id').read_text() == 'sha256:'+'a'*64+'\n'


@pytest.mark.parametrize('engine', [None, 'docker'])
def test_replay_dispatch_uses_selected_engine(tmp_path, monkeypatch, engine):
    image = 'sha256:'+'b'*64
    (tmp_path/'config').mkdir()
    (tmp_path/'config/official-images.json').write_text(json.dumps([image]))
    monkeypatch.setattr(images, 'ROOT', tmp_path)
    monkeypatch.setattr(images, 'load_package', lambda path: ({'image_digest': image}, []))
    monkeypatch.delenv('NETHACK_IMAGE_DIGEST', raising=False)
    monkeypatch.delenv('NETHACK_CONTAINER_ENGINE', raising=False)
    if engine: monkeypatch.setenv('NETHACK_CONTAINER_ENGINE', engine)
    calls = []
    monkeypatch.setattr(images.subprocess, 'run', lambda command, **kwargs: calls.append((command, kwargs)))
    assert images.dispatch(tmp_path/'package')
    command, kwargs = calls[0]
    assert command[:3] == [engine or 'podman', 'run', '--rm']
    assert command[-3:] == [image, 'replay', '/package']
    assert '--read-only' in command
    assert command[command.index('--network')+1] == 'none'
    assert kwargs == {'check': True, 'timeout': 600}
