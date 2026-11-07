# test wrapper: this venv's pygame has no PNG support, so load PNGs through Pillow
import runpy, sys, pygame
from PIL import Image
_load = pygame.image.load
def load(p, *a):
    if str(p).endswith(".png"):
        im = Image.open(p).convert("RGBA")
        return pygame.image.frombuffer(im.tobytes(), im.size, "RGBA").copy()
    return _load(p, *a)
pygame.image.load = load
sys.argv = [sys.argv[1]]
runpy.run_path(sys.argv[0], run_name="__main__")
