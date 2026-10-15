import importlib.util
from pathlib import Path

from powermonitor._vendor import pymonsoon
from powermonitor.models import SDK_REVISION


def test_vendored_sdk_revision_and_namespace():
    assert pymonsoon.SDK_REVISION == SDK_REVISION
    assert importlib.util.find_spec("Monsoon") is None


def test_vendored_license_is_packaged_with_sdk_sources():
    package_dir = Path(pymonsoon.__file__).parent
    assert (package_dir / "LICENSE.txt").read_text().startswith("Copyright 2017 Monsoon")
