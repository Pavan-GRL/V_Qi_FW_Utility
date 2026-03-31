"""Create icon from the updated GRL logo."""

from PIL import Image
from project_paths import ASSETS_DIR

source_path = ASSETS_DIR / "grl_updated_logo.jpg"
target_path = ASSETS_DIR / "grl_updated_logo.ico"

img = Image.open(source_path).convert("RGBA")
img.save(
    target_path,
    format="ICO",
    sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print(f"{target_path.name} created successfully.")
