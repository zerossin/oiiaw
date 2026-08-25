from oiiaw import ui_assets
from oiiaw.ui_assets import apply_window_icon, face_asset_path, face_ico_asset_path, tray_icon


def test_face_asset_is_packaged_with_the_application():
    assert face_asset_path().is_file()
    assert face_ico_asset_path().is_file()


def test_tray_icon_combines_face_with_state_badge():
    idle = tray_icon((70, 130, 180))
    error = tray_icon((210, 60, 60))

    assert idle.mode == "RGBA"
    assert idle.size == (64, 64)
    assert idle.getpixel((51, 51)) == (70, 130, 180, 255)
    assert error.getpixel((51, 51)) == (210, 60, 60, 255)
    assert idle.getpixel((32, 32)) != idle.getpixel((51, 51))


def test_window_icon_is_applied_and_kept_alive(monkeypatch):
    icon = object()
    monkeypatch.setattr(ui_assets.tk, "PhotoImage", lambda **kwargs: icon)
    monkeypatch.setattr(ui_assets.sys, "platform", "win32")

    class Window:
        def iconphoto(self, default, image):
            self.applied = (default, image)

        def iconbitmap(self, path):
            self.bitmap = path

    window = Window()
    apply_window_icon(window)

    assert window.applied == (True, icon)
    assert window._oiiaw_icon is icon
    assert window.bitmap == str(face_ico_asset_path())
