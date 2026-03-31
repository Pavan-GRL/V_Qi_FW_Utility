"""Remove background from logo to make it transparent."""

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

    # Check if pixel is dark (background)
    # The logo background is dark (#1E2A2F / #23303A range)
    # We'll make dark pixels transparent
    if r < 60 and g < 70 and b < 80:  # Dark background threshold
        # Make it transparent
        newData.append((r, g, b, 0))
    else:
        # Keep the pixel
        newData.append(item)

img.putdata(newData)
img.save(output_path, "PNG")
print(f"{output_path.name} created successfully with transparent background.")
