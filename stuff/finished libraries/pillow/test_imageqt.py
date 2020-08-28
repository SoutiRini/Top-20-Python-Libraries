import pytest
from PIL import ImageQt

from .helper import hopper

if ImageQt.qt_is_installed:
    from PIL.ImageQt import qRgba


@pytest.mark.skipif(not ImageQt.qt_is_installed, reason="Qt bindings are not installed")
class PillowQPixmapTestCase:
    @classmethod
    def setup_class(self):
        try:
            if ImageQt.qt_version == "5":
                from PyQt5.QtGui import QGuiApplication
            elif ImageQt.qt_version == "side2":
                from PySide2.QtGui import QGuiApplication
        except ImportError:
            pytest.skip("QGuiApplication not installed")
            return

        self.app = QGuiApplication([])

    @classmethod
    def teardown_class(self):
        self.app.quit()
        self.app = None


@pytest.mark.skipif(not ImageQt.qt_is_installed, reason="Qt bindings are not installed")
def test_rgb():
    # from https://doc.qt.io/archives/qt-4.8/qcolor.html
    # typedef QRgb
    # An ARGB quadruplet on the format #AARRGGBB,
    # equivalent to an unsigned int.
    if ImageQt.qt_version == "5":
        from PyQt5.QtGui import qRgb
    elif ImageQt.qt_version == "side2":
        from PySide2.QtGui import qRgb

    assert qRgb(0, 0, 0) == qRgba(0, 0, 0, 255)

    def checkrgb(r, g, b):
        val = ImageQt.rgb(r, g, b)
        val = val % 2 ** 24  # drop the alpha
        assert val >> 16 == r
        assert ((val >> 8) % 2 ** 8) == g
        assert val % 2 ** 8 == b

    checkrgb(0, 0, 0)
    checkrgb(255, 0, 0)
    checkrgb(0, 255, 0)
    checkrgb(0, 0, 255)


@pytest.mark.skipif(not ImageQt.qt_is_installed, reason="Qt bindings are not installed")
def test_image():
    for mode in ("1", "RGB", "RGBA", "L", "P"):
        ImageQt.ImageQt(hopper(mode))
