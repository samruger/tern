"""Color schemes for the screen. `t` on the screen cycles through them, in this order.

Every color is a 256-color index (see https://www.ditig.com/256-colors-cheat-sheet). A theme only
applies on a 256-color terminal; on 8 colors the screen keeps its basic fallbacks.

Roles:
  base      what the 8 basic names draw as: red = losses / NO side / errors, green = gains / YES,
            yellow = warnings and the selected strike, white = plain text, and so on
  grey      quiet text: column values, hints, ages
  key       the box numbers and the pressable letter of each hint on the borders
  border    each box's frame
  head      column headers in the ladder and book
  sym, event, unit
            the status line: the coin, the hourly contract, small labels like "1h"
  word_a, word_b, word_c
            connective words: "closes", "rule", "next … slot in" on the status line
  ticker, strike, slot, verb, variant, phrase, sep
            log tokens: a contract, its strike, "15m slot", "settled", a shadow variant
            ("band90"), "favorites in band", the · between parts
  value     the book's money values
  rows      the btop-style gradient down the book, and (reversed) the ladder's spread/volume tint
  fade_txt, fade_ts
            the log's age fade, newest first, for text and timestamps
  grads     the charts: spot price, YES price (cheap → dear), equity up, equity down
"""

THEMES: dict[str, dict] = {
    # the original look: basic colors, light grey
    "classic": {
        "base": {},
        "grey": 248, "key": 205, "head": 248,
        "border": {"spot": 6, "contract": 5, "ladder": 208, "book": 3, "equity": 2, "log": 4},
        "sym": 6, "event": 75, "unit": 110,
        "word_a": 183, "word_b": 218, "word_c": 180,
        "ticker": 73, "strike": 222, "slot": 214, "verb": 183, "variant": 117, "phrase": 180, "sep": 205,
        "value": 7,
        "rows": [248],
        "fade_txt": [253], "fade_ts": [110],
        "grads": {
            "spot": [24, 25, 26, 32, 38, 44, 50, 87],
            "yes": [34, 70, 106, 142, 178, 214, 208, 203],
            "eq_up": [22, 28, 34, 35, 41, 47, 48, 49],
            "eq_dn": [52, 88, 124, 160, 196, 197, 198, 199],
        },
    },
    # the desktop's Tokyo Night: soft blues and purples, green / orange / coral for meaning
    "tokyo night": {
        "base": {1: 210, 2: 149, 3: 179, 4: 111, 5: 141, 6: 117, 7: 189},
        "grey": 103, "key": 215, "head": 111,
        "border": {"spot": 117, "contract": 141, "ladder": 111, "book": 140, "equity": 67, "log": 104},
        "sym": 117, "event": 111, "unit": 103,
        "word_a": 146, "word_b": 141, "word_c": 104,
        "ticker": 111, "strike": 215, "slot": 141, "verb": 140, "variant": 117, "phrase": 179, "sep": 60,
        "value": 189,
        "rows": [189, 153, 147, 117, 111, 110, 104, 103],
        "fade_txt": [189, 189, 153, 146, 146, 110, 109, 103, 103, 60],
        "fade_ts": [117, 111, 110, 110, 104, 104, 67, 61, 60, 60],
        "grads": {
            "spot": [60, 61, 67, 68, 74, 110, 111, 117],
            "yes": [67, 74, 110, 149, 179, 215, 210, 204],
            "eq_up": [22, 28, 64, 70, 106, 107, 149, 150],
            "eq_dn": [52, 88, 131, 167, 168, 203, 210, 211],
        },
    },
    # the wallpaper: neon pinks and electric blues
    "synthwave": {
        "base": {1: 204, 2: 122, 3: 221, 4: 69, 5: 170, 6: 81, 7: 189},
        "grey": 146, "key": 205, "head": 117,
        "border": {"spot": 81, "contract": 171, "ladder": 69, "book": 205, "equity": 75, "log": 170},
        "sym": 81, "event": 75, "unit": 110,
        "word_a": 183, "word_b": 218, "word_c": 147,
        "ticker": 111, "strike": 219, "slot": 213, "verb": 183, "variant": 117, "phrase": 147, "sep": 205,
        "value": 218,
        "rows": [219, 213, 177, 171, 141, 135, 105, 99, 69, 75, 111, 117],
        "fade_txt": [225, 219, 218, 183, 182, 177, 176, 141, 140, 104],
        "fade_ts": [159, 123, 117, 111, 75, 69, 68, 63, 62, 61],
        "grads": {
            "spot": [55, 56, 57, 63, 69, 75, 81, 87],
            "yes": [33, 63, 99, 135, 171, 207, 205, 204],
            "eq_up": [23, 30, 37, 44, 50, 86, 122, 158],
            "eq_dn": [53, 89, 125, 161, 162, 198, 204, 211],
        },
    },
    # outrun sunset: orange into hot pink into purple
    "sunset": {
        "base": {1: 197, 2: 150, 3: 220, 4: 99, 5: 170, 6: 209, 7: 230},
        "grey": 181, "key": 220, "head": 215,
        "border": {"spot": 208, "contract": 205, "ladder": 141, "book": 214, "equity": 170, "log": 99},
        "sym": 214, "event": 209, "unit": 181,
        "word_a": 217, "word_b": 211, "word_c": 183,
        "ticker": 209, "strike": 221, "slot": 205, "verb": 217, "variant": 215, "phrase": 183, "sep": 205,
        "value": 222,
        "rows": [223, 222, 221, 215, 209, 204, 205, 170, 134, 98],
        "fade_txt": [230, 224, 223, 217, 216, 210, 174, 168, 132, 96],
        "fade_ts": [221, 215, 214, 208, 209, 203, 204, 168, 132, 96],
        "grads": {
            "spot": [53, 89, 125, 161, 197, 203, 209, 215],
            "yes": [99, 135, 171, 207, 205, 209, 215, 221],
            "eq_up": [58, 64, 100, 142, 148, 184, 191, 192],
            "eq_dn": [53, 89, 125, 161, 197, 198, 199, 205],
        },
    },
    # deep sea: teals and ice blues with a warm key
    "ocean": {
        "base": {1: 210, 2: 85, 3: 229, 4: 33, 5: 147, 6: 51, 7: 195},
        "grey": 109, "key": 214, "head": 87,
        "border": {"spot": 45, "contract": 33, "ladder": 37, "book": 81, "equity": 30, "log": 39},
        "sym": 51, "event": 45, "unit": 109,
        "word_a": 116, "word_b": 80, "word_c": 152,
        "ticker": 45, "strike": 229, "slot": 87, "verb": 116, "variant": 123, "phrase": 152, "sep": 37,
        "value": 195,
        "rows": [195, 159, 123, 87, 81, 45, 39, 38, 32, 31],
        "fade_txt": [195, 195, 159, 152, 152, 116, 109, 109, 66, 66],
        "fade_ts": [123, 87, 81, 45, 44, 38, 37, 31, 30, 24],
        "grads": {
            "spot": [17, 18, 24, 25, 31, 37, 43, 49],
            "yes": [23, 29, 36, 43, 50, 87, 159, 229],
            "eq_up": [22, 29, 36, 42, 48, 85, 121, 157],
            "eq_dn": [52, 88, 124, 167, 174, 210, 217, 224],
        },
    },
    # 70s retro: mustard, burnt orange, olive, teal on warm cream (gruvbox-ish)
    "retro": {
        "base": {1: 167, 2: 142, 3: 214, 4: 109, 5: 175, 6: 108, 7: 223},
        "grey": 144, "key": 208, "head": 214,
        "border": {"spot": 214, "contract": 175, "ladder": 108, "book": 208, "equity": 142, "log": 109},
        "sym": 214, "event": 108, "unit": 144,
        "word_a": 180, "word_b": 173, "word_c": 137,
        "ticker": 109, "strike": 214, "slot": 208, "verb": 175, "variant": 108, "phrase": 180, "sep": 137,
        "value": 223,
        "rows": [223, 222, 221, 215, 214, 179, 173, 137, 108, 109],
        "fade_txt": [230, 223, 223, 187, 187, 180, 180, 144, 138, 102],
        "fade_ts": [214, 214, 178, 172, 172, 136, 137, 101, 95, 95],
        "grads": {
            "spot": [94, 130, 136, 172, 178, 214, 220, 222],
            "yes": [66, 108, 142, 178, 214, 208, 202, 167],
            "eq_up": [22, 58, 64, 100, 106, 142, 148, 186],
            "eq_dn": [52, 88, 94, 130, 166, 167, 173, 174],
        },
    },
    # phosphor terminal: greens with an amber key
    "matrix": {
        "base": {1: 203, 2: 83, 3: 220, 4: 35, 5: 157, 6: 48, 7: 194},
        "grey": 108, "key": 220, "head": 120,
        "border": {"spot": 46, "contract": 34, "ladder": 40, "book": 77, "equity": 28, "log": 71},
        "sym": 46, "event": 84, "unit": 108,
        "word_a": 151, "word_b": 114, "word_c": 108,
        "ticker": 84, "strike": 228, "slot": 156, "verb": 151, "variant": 120, "phrase": 150, "sep": 34,
        "value": 194,
        "rows": [194, 157, 156, 120, 119, 83, 77, 71, 65, 59],
        "fade_txt": [194, 194, 157, 151, 151, 114, 108, 108, 65, 65],
        "fade_ts": [120, 84, 83, 77, 71, 70, 65, 64, 28, 22],
        "grads": {
            "spot": [22, 28, 34, 40, 46, 82, 118, 154],
            "yes": [22, 28, 64, 100, 136, 172, 208, 203],
            "eq_up": [22, 28, 34, 40, 46, 83, 120, 157],
            "eq_dn": [52, 88, 124, 160, 166, 203, 209, 216],
        },
    },
    # Christmas: holly green, berry red, gold tinsel, snow white
    "christmas": {
        "base": {1: 160, 2: 40, 3: 220, 4: 30, 5: 161, 6: 72, 7: 255},
        "grey": 151, "key": 220, "head": 222,
        "border": {"spot": 160, "contract": 34, "ladder": 178, "book": 124, "equity": 28, "log": 196},
        "sym": 196, "event": 71, "unit": 151,
        "word_a": 194, "word_b": 217, "word_c": 223,
        "ticker": 71, "strike": 220, "slot": 203, "verb": 217, "variant": 156, "phrase": 223, "sep": 220,
        "value": 230,
        "rows": [255, 224, 217, 210, 203, 167, 71, 77, 113, 150],
        "fade_txt": [255, 255, 231, 224, 224, 194, 188, 151, 145, 108],
        "fade_ts": [220, 214, 178, 172, 167, 161, 124, 71, 65, 22],
        "grads": {
            "spot": [22, 28, 34, 40, 76, 112, 148, 220],
            "yes": [22, 28, 34, 142, 178, 214, 196, 160],
            "eq_up": [22, 28, 34, 40, 41, 77, 114, 157],
            "eq_dn": [52, 88, 124, 160, 196, 203, 210, 224],
        },
    },
    # Dracula: purple, pink, cyan on near-black
    "dracula": {
        "base": {1: 210, 2: 84, 3: 228, 4: 141, 5: 212, 6: 117, 7: 255},
        "grey": 103, "key": 212, "head": 141,
        "border": {"spot": 117, "contract": 212, "ladder": 141, "book": 215, "equity": 84, "log": 98},
        "sym": 117, "event": 141, "unit": 103,
        "word_a": 183, "word_b": 218, "word_c": 146,
        "ticker": 141, "strike": 215, "slot": 212, "verb": 183, "variant": 117, "phrase": 228, "sep": 212,
        "value": 231,
        "rows": [231, 225, 218, 212, 177, 141, 135, 105, 111, 117],
        "fade_txt": [255, 255, 225, 189, 189, 183, 146, 140, 103, 60],
        "fade_ts": [212, 206, 177, 171, 141, 135, 99, 98, 61, 60],
        "grads": {
            "spot": [54, 55, 92, 98, 135, 141, 177, 212],
            "yes": [61, 98, 141, 117, 84, 228, 215, 210],
            "eq_up": [22, 29, 35, 41, 48, 84, 120, 157],
            "eq_dn": [53, 89, 125, 161, 168, 204, 210, 217],
        },
    },
    # cyberpunk: acid yellow, neon cyan, hot magenta
    "cyberpunk": {
        "base": {1: 197, 2: 50, 3: 226, 4: 33, 5: 201, 6: 51, 7: 230},
        "grey": 145, "key": 226, "head": 51,
        "border": {"spot": 226, "contract": 201, "ladder": 51, "book": 199, "equity": 45, "log": 165},
        "sym": 226, "event": 51, "unit": 145,
        "word_a": 159, "word_b": 219, "word_c": 229,
        "ticker": 51, "strike": 226, "slot": 201, "verb": 213, "variant": 87, "phrase": 229, "sep": 201,
        "value": 228,
        "rows": [229, 228, 227, 226, 220, 207, 201, 165, 81, 51],
        "fade_txt": [230, 230, 229, 223, 225, 219, 183, 177, 141, 99],
        "fade_ts": [51, 45, 39, 33, 63, 99, 129, 128, 91, 54],
        "grads": {
            "spot": [53, 90, 127, 164, 201, 207, 213, 226],
            "yes": [27, 33, 39, 51, 87, 201, 199, 197],
            "eq_up": [23, 30, 37, 44, 51, 87, 123, 159],
            "eq_dn": [53, 89, 125, 161, 197, 199, 205, 211],
        },
    },
    # Halloween: pumpkin orange, witch purple, slime green
    "halloween": {
        "base": {1: 160, 2: 118, 3: 214, 4: 92, 5: 134, 6: 208, 7: 223},
        "grey": 138, "key": 118, "head": 208,
        "border": {"spot": 208, "contract": 92, "ladder": 202, "book": 134, "equity": 76, "log": 130},
        "sym": 208, "event": 141, "unit": 138,
        "word_a": 180, "word_b": 177, "word_c": 144,
        "ticker": 141, "strike": 214, "slot": 118, "verb": 177, "variant": 215, "phrase": 150, "sep": 208,
        "value": 222,
        "rows": [223, 222, 215, 214, 208, 202, 166, 134, 98, 92],
        "fade_txt": [230, 223, 223, 216, 216, 180, 174, 138, 132, 96],
        "fade_ts": [214, 208, 208, 202, 166, 130, 134, 98, 92, 54],
        "grads": {
            "spot": [52, 88, 94, 130, 166, 202, 208, 214],
            "yes": [54, 92, 98, 134, 166, 202, 208, 214],
            "eq_up": [22, 28, 34, 70, 76, 112, 118, 155],
            "eq_dn": [52, 88, 124, 160, 166, 167, 173, 216],
        },
    },
    # cotton candy: soft pastel pinks, mint, baby blue
    "pastel": {
        "base": {1: 211, 2: 121, 3: 229, 4: 153, 5: 219, 6: 159, 7: 255},
        "grey": 146, "key": 218, "head": 153,
        "border": {"spot": 159, "contract": 218, "ladder": 153, "book": 225, "equity": 157, "log": 183},
        "sym": 159, "event": 153, "unit": 146,
        "word_a": 189, "word_b": 225, "word_c": 194,
        "ticker": 153, "strike": 229, "slot": 218, "verb": 225, "variant": 159, "phrase": 194, "sep": 218,
        "value": 230,
        "rows": [231, 225, 224, 218, 219, 183, 189, 153, 159, 158],
        "fade_txt": [255, 255, 231, 225, 225, 189, 188, 182, 146, 145],
        "fade_ts": [159, 153, 153, 147, 183, 182, 146, 146, 103, 103],
        "grads": {
            "spot": [104, 110, 116, 152, 153, 189, 225, 218],
            "yes": [116, 152, 158, 194, 230, 224, 218, 211],
            "eq_up": [65, 72, 79, 115, 121, 157, 158, 194],
            "eq_dn": [95, 132, 168, 174, 175, 211, 217, 224],
        },
    },
    # Nord: arctic frost blues and aurora accents
    "nord": {
        "base": {1: 174, 2: 150, 3: 222, 4: 110, 5: 139, 6: 116, 7: 255},
        "grey": 103, "key": 222, "head": 116,
        "border": {"spot": 110, "contract": 139, "ladder": 116, "book": 109, "equity": 150, "log": 67},
        "sym": 116, "event": 110, "unit": 103,
        "word_a": 152, "word_b": 182, "word_c": 146,
        "ticker": 110, "strike": 222, "slot": 173, "verb": 182, "variant": 116, "phrase": 152, "sep": 67,
        "value": 254,
        "rows": [255, 254, 189, 153, 152, 116, 110, 109, 67, 66],
        "fade_txt": [255, 255, 254, 253, 252, 188, 152, 146, 103, 60],
        "fade_ts": [116, 116, 110, 110, 109, 67, 67, 66, 60, 60],
        "grads": {
            "spot": [23, 24, 60, 67, 68, 110, 116, 153],
            "yes": [67, 109, 116, 150, 186, 222, 173, 174],
            "eq_up": [22, 65, 71, 107, 108, 150, 151, 194],
            "eq_dn": [52, 95, 131, 132, 168, 174, 181, 224],
        },
    },
}


NAMES = list(THEMES)
DEFAULT = "classic"


def get(name: str | None) -> dict:
    return THEMES.get(name or DEFAULT, THEMES[DEFAULT])


def next_name(name: str | None) -> str:
    i = NAMES.index(name) if name in THEMES else -1
    return NAMES[(i + 1) % len(NAMES)]
