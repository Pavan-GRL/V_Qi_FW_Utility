"""Run once to convert grl_logo.png → grl_logo.ico for PyInstaller --icon."""
from PIL import Image

img = Image.open("grl_logo.png").convert("RGBA")
img.save(
    "grl_logo.ico",
    format="ICO",
    sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("grl_logo.ico created successfully.")
