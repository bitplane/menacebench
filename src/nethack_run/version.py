"""Read the package version from source metadata or an installed distribution."""
from importlib.metadata import version
from pathlib import Path


def package_version():
    project = Path(__file__).resolve().parents[2]/'pyproject.toml'
    if project.is_file():
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10
            import tomli as tomllib
        with project.open('rb') as file:
            return tomllib.load(file)['project']['version']
    return version('nethack-run')


VERSION = package_version()
