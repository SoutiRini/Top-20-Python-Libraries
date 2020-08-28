import os.path
import subprocess

import pytest
from PIL import Image

from .helper import IMCONVERT, assert_image_equal, hopper, imagemagick_available

_roundtrip = imagemagick_available()


def helper_save_as_palm(tmp_path, mode):
    # Arrange
    im = hopper(mode)
    outfile = str(tmp_path / ("temp_" + mode + ".palm"))

    # Act
    im.save(outfile)

    # Assert
    assert os.path.isfile(outfile)
    assert os.path.getsize(outfile) > 0


def open_with_imagemagick(tmp_path, f):
    if not imagemagick_available():
        raise OSError()

    outfile = str(tmp_path / "temp.png")
    rc = subprocess.call(
        [IMCONVERT, f, outfile], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
    )
    if rc:
        raise OSError
    return Image.open(outfile)


def roundtrip(tmp_path, mode):
    if not _roundtrip:
        return

    im = hopper(mode)
    outfile = str(tmp_path / "temp.palm")

    im.save(outfile)
    converted = open_with_imagemagick(tmp_path, outfile)
    assert_image_equal(converted, im)


def test_monochrome(tmp_path):
    # Arrange
    mode = "1"

    # Act / Assert
    helper_save_as_palm(tmp_path, mode)
    roundtrip(tmp_path, mode)


@pytest.mark.xfail(reason="Palm P image is wrong")
def test_p_mode(tmp_path):
    # Arrange
    mode = "P"

    # Act / Assert
    helper_save_as_palm(tmp_path, mode)
    roundtrip(tmp_path, mode)


def test_l_oserror(tmp_path):
    # Arrange
    mode = "L"

    # Act / Assert
    with pytest.raises(OSError):
        helper_save_as_palm(tmp_path, mode)


def test_rgb_oserror(tmp_path):
    # Arrange
    mode = "RGB"

    # Act / Assert
    with pytest.raises(OSError):
        helper_save_as_palm(tmp_path, mode)
