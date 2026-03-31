"""Create icon from the transparent logo for better appearance."""

from PIL import Image
from project_paths import ASSETS_DIR

source_path = ASSETS_DIR / "grl_logo_transparent.png"
target_path = ASSETS_DIR / "grl_updated_logo.ico"

# Load the transparent logo
img = Image.open(source_path).convert("RGBA")

# Create icon with multiple sizes
img.save(
    target_path,
    format="ICO",
    sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print(f"{target_path.name} created successfully from transparent logo.")
print("Icon now has transparent background matching the UI theme.")
