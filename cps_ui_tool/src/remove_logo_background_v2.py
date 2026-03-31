"""Remove background from logo with better preservation of logo elements."""

from PIL import Image
from project_paths import ASSETS_DIR

# Load the logo
source_path = ASSETS_DIR / "grl_updated_logo.jpg"
output_path = ASSETS_DIR / "grl_logo_transparent.png"

img = Image.open(source_path).convert("RGBA")
datas = img.getdata()

# Create new image data with transparency
newData = []
for item in datas:
    # Get RGB values
    r, g, b, a = item

    # More selective background removal - only remove very dark pixels
    # Keep anything that has decent brightness or orange/gray tones
    is_orange = r > 150 and g > 100 and b < 100  # Orange colors
    is_gray = abs(r - g) < 30 and abs(g - b) < 30 and r > 80  # Gray orbital ring
    is_white = r > 200 and g > 200 and b > 200  # White elements
    is_very_dark = r < 45 and g < 55 and b < 65  # Only very dark background

    if is_very_dark and not (is_orange or is_gray or is_white):
        # Make pure dark background transparent
        newData.append((r, g, b, 0))
    else:
        # Keep all logo elements
        newData.append(item)

img.putdata(newData)
img.save(output_path, "PNG")
print(f"{output_path.name} created successfully with improved transparency.")
print("Logo elements (orange bars, orbital ring, text) are now fully preserved.")
