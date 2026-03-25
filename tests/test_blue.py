import os
import pathlib
import subprocess

# blue must be imported before black.  See GH#72.
import blue
import black
import pytest

from shutil import copytree
from tempfile import TemporaryDirectory

tests_dir = pathlib.Path(__file__).parent.absolute()


def get_custom_environment() -> dict[str, str]:
    custom_env = os.environ.copy()
    custom_env['COVERAGE_PROCESS_START'] = tests_dir.joinpath(
        '..', 'pyproject.toml'
    ).resolve()
    return custom_env


@pytest.mark.parametrize(
    'test_dir',
    [
        'config_setup',
        'config_tox',
        'config_blue',
        'config_pyproject',
        'good_cases',
    ],
)
def test_good_dirs(test_dir):
    src_dir = tests_dir / test_dir
    with TemporaryDirectory() as dst_dir:
        copytree(src_dir, dst_dir, dirs_exist_ok=True)
        result = subprocess.run(
            ['blue', '--check', '--diff', '.'],
            cwd=src_dir,
            env=get_custom_environment(),
        )
        assert result.returncode == 0


@pytest.mark.parametrize(
    'test_dir',
    ['bad_cases'],
)
def test_bad_dirs(test_dir):
    src_dir = tests_dir / test_dir
    with TemporaryDirectory() as dst_dir:
        copytree(src_dir, dst_dir, dirs_exist_ok=True)
        result = subprocess.run(
            ['blue', '--check', '--diff', '.'],
            cwd=src_dir,
            env=get_custom_environment(),
        )
        assert result.returncode == 1


def test_main():
    result = subprocess.run(
        ['blue', '--help'],
        env=get_custom_environment(),
    )
    assert result.returncode == 0


def test_version():
    result = subprocess.run(
        ['blue', '--version'],
        env=get_custom_environment(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    version = (
        f'blue, version {blue.version}, based on black {black.__version__}\n'
    )
    assert result.stdout.endswith(version)
    assert result.stderr == ''
