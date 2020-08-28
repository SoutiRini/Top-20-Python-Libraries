import pytest
from PIL import Image, TarIO, features

from .helper import is_pypy

# Sample tar archive
TEST_TAR_FILE = "Tests/images/hopper.tar"


def test_sanity():
    for codec, test_path, format in [
        ["zlib", "hopper.png", "PNG"],
        ["jpg", "hopper.jpg", "JPEG"],
    ]:
        if features.check(codec):
            with TarIO.TarIO(TEST_TAR_FILE, test_path) as tar:
                with Image.open(tar) as im:
                    im.load()
                    assert im.mode == "RGB"
                    assert im.size == (128, 128)
                    assert im.format == format


@pytest.mark.skipif(is_pypy(), reason="Requires CPython")
def test_unclosed_file():
    def open():
        TarIO.TarIO(TEST_TAR_FILE, "hopper.jpg")

    pytest.warns(ResourceWarning, open)


def test_close():
    def open():
        tar = TarIO.TarIO(TEST_TAR_FILE, "hopper.jpg")
        tar.close()

    pytest.warns(None, open)


def test_contextmanager():
    def open():
        with TarIO.TarIO(TEST_TAR_FILE, "hopper.jpg"):
            pass

    pytest.warns(None, open)
