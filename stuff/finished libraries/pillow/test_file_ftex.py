from PIL import Image

from .helper import assert_image_equal, assert_image_similar


def test_load_raw():
    with Image.open("Tests/images/ftex_uncompressed.ftu") as im:
        with Image.open("Tests/images/ftex_uncompressed.png") as target:
            assert_image_equal(im, target)


def test_load_dxt1():
    with Image.open("Tests/images/ftex_dxt1.ftc") as im:
        with Image.open("Tests/images/ftex_dxt1.png") as target:
            assert_image_similar(im, target.convert("RGBA"), 15)
