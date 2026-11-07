# Art

| Path | What |
|------|------|
| `original/` | The logo and icon as drawn (SVG and PNG) |
| `final/` | Everything made from them: favicons, icon sizes, logo variants, the loading GIF |
| `make_images.py` | Builds the logo variants (text as outlines) and the favicon into `out/`. Needs fontTools and two fonts in `fonts/`: [Rajdhani](https://fonts.google.com/specimen/Rajdhani) Bold and [Quicksand](https://fonts.google.com/specimen/Quicksand) (variable); both are SIL Open Font License, so they aren't kept here |
| `make_loading.py` | Renders the loading animation's frames (`make_loading.py <out dir> [w h fps seconds]`); join them with ffmpeg into `final/batowatch_loading.gif` |
| `make_tv_layers.py` | Makes the TV app's loading-screen layers (`make_tv_layers.py ../tv 512`) |

All of them render SVG with `rsvg-convert`.
