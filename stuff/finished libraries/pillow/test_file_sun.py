import os

import pytest
from PIL import Image, SunImagePlugin

from .helper import assert_image_equal, assert_image_similar, hopper

EXTRA_DIR = "Tests/images/sunraster"


def test_sanity():
    # Arrange
    # Created with ImageMagick: convert hopper.jpg hopper.ras
    test_file = "Tests/images/hopper.ras"

    # Act
    with Image.open(test_file) as im:

        # Assert
        assert im.size == (128, 128)

        assert_image_similar(im, hopper(), 5)  # visually verified

    invalid_file = "Tests/images/flower.jpg"
    with pytest.raises(SyntaxError):
        SunImagePlugin.SunImageFile(invalid_file)


def test_im1():
    with Image.open("Tests/images/sunraster.im1") as im:
        with Image.open("Tests/images/sunraster.im1.png") as target:
            assert_image_equal(im, target)


@pytest.mark.skipif(
    not os.path.exists(EXTRA_DIR), reason="Extra image files not installed"
)
def test_others():
    files = (
        os.path.join(EXTRA_DIR, f)
        for f in os.listdir(EXTRA_DIR)
        if os.path.splitext(f)[1] in (".sun", ".SUN", ".ras")
    )
    for path in files:
        with Image.open(path) as im:
            im.load()
            assert isinstance(im, SunImagePlugin.SunImageFile)
            target_path = "%s.png" % os.path.splitext(path)[0]
            # im.save(target_file)
            with Image.open(target_path) as target:
                assert_image_equal(im, target)
