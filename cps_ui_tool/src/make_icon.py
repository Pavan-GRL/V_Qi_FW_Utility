"""Run once to convert grl_logo.png to grl_logo.ico for PyInstaller."""

from PIL import Image

from project_paths import ASSETS_DIR

source_path = ASSETS_DIR / "grl_logo.png"
target_path = ASSETS_DIR / "grl_logo.ico"

img = Image.open(source_path).convert("RGBA")
img.save(
    target_path,
    format="ICO",
    sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print(f"{target_path.name} created successfully.")
