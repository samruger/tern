"""Color schemes for the screen. `t` on the screen steps to the next one, `T` back, in this order; the themes box
lists them.

Every color is a 256-color index (see https://www.ditig.com/256-colors-cheat-sheet). A theme only
applies on a 256-color terminal; on 8 colors the screen keeps its basic fallbacks.

Roles:
  base      what the 8 basic names draw as: red = losses / NO side / errors, green = gains / YES,
            yellow = warnings and the selected strike, white = plain text, and so on
  grey      quiet text: column values, hints, ages
  key       the tabs' numbers and the pressable letter of each hint on the borders
  border    each box's frame, and its tab while it is on screen
  tab, tab_text
            the background of the tabs not on screen, and their names
  fill      the dark background of a list's highlighted row, and what is left of a meter
  head      column headers in the ladder and book
  sym, event, unit
            the status line: the coin, the hourly contract, small labels like "1h"
  word_a, word_b, word_c
            connective words: "closes", "rule", "next … slot in" on the status line
  ticker, strike, slot, verb, variant, phrase, sep
            log tokens: a contract, its strike, "15m slot", "settled", a shadow variant
            ("band90"), "favorites in band", the · between parts
  value     the book's money values
  rows      the btop-style gradient down the lists, and (reversed) the ladder's spread/volume tint
  fade_txt, fade_ts
            the log's age fade, newest first, for text and timestamps
  grads     the charts: spot price, YES price (cheap → dear), P&L up, P&L down

A box fills the screen, so a gradient covers a lot of rows: `smooth` puts the colors between its steps in, and
`readable` is what a color draws as on a fill it would be hard to read on.
"""

from functools import lru_cache
from math import dist

THEMES: dict[str, dict] = {
    # the original look: basic colors, light grey
    "classic": {
        "base": {},
        "grey": 248, "key": 205, "head": 248,
        "border": {"spot": 6, "contract": 5, "ladder": 208, "book": 3, "variants": 2, "log": 4,
                   "gate": 50, "reports": 218, "settings": 214, "themes": 205, "help": 44},
        "fill": 237, "tab": 25, "tab_text": 255,
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
        "border": {"spot": 117, "contract": 146, "ladder": 110, "book": 189, "variants": 74, "log": 146,
                   "gate": 179, "reports": 189, "settings": 215, "themes": 74, "help": 153},
        "fill": 17, "tab": 61, "tab_text": 231,
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
        "border": {"spot": 81, "contract": 177, "ladder": 69, "book": 205, "variants": 75, "log": 213,
                   "gate": 87, "reports": 141, "settings": 219, "themes": 141, "help": 218},
        "fill": 54, "tab": 91, "tab_text": 189,
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
        "border": {"spot": 208, "contract": 211, "ladder": 141, "book": 214, "variants": 170, "log": 135,
                   "gate": 203, "reports": 222, "settings": 203, "themes": 183, "help": 209},
        "fill": 53, "tab": 125, "tab_text": 224,
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
        "border": {"spot": 80, "contract": 33, "ladder": 44, "book": 116, "variants": 36, "log": 152,
                   "gate": 49, "reports": 214, "settings": 51, "themes": 33, "help": 43},
        "fill": 17, "tab": 31, "tab_text": 231,
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
        "border": {"spot": 214, "contract": 175, "ladder": 108, "book": 178, "variants": 142, "log": 109,
                   "gate": 221, "reports": 220, "settings": 180, "themes": 178, "help": 223},
        "fill": 52, "tab": 130, "tab_text": 231,
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
        "border": {"spot": 46, "contract": 40, "ladder": 82, "book": 120, "variants": 114, "log": 157,
                   "gate": 154, "reports": 157, "settings": 220, "themes": 119, "help": 108},
        "fill": 22, "tab": 28, "tab_text": 231,
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
    # Christmas: snow white, berry red and holly green, with gold only on the key hints
    "christmas": {
        "base": {1: 196, 2: 40, 3: 231, 4: 28, 5: 196, 6: 71, 7: 255},
        "grey": 188, "key": 220, "head": 255,
        "border": {"spot": 203, "contract": 40, "ladder": 255, "book": 114, "variants": 203, "log": 157,
                   "gate": 217, "reports": 220, "settings": 151, "themes": 210, "help": 114},
        "fill": 22, "tab": 28, "tab_text": 231,
        "sym": 196, "event": 77, "unit": 188,
        "word_a": 194, "word_b": 217, "word_c": 255,
        "ticker": 77, "strike": 255, "slot": 203, "verb": 217, "variant": 157, "phrase": 194, "sep": 196,
        "value": 255,
        "rows": [255, 231, 224, 217, 210, 203, 196, 160, 71, 77, 114, 157],
        "fade_txt": [255, 255, 231, 231, 224, 224, 188, 188, 151, 151],
        "fade_ts": [255, 224, 217, 210, 203, 197, 160, 77, 71, 71],
        "grads": {
            "spot": [22, 28, 34, 40, 77, 114, 157, 255],
            "yes": [28, 34, 71, 151, 255, 217, 203, 160],
            "eq_up": [22, 28, 34, 40, 77, 114, 157, 255],
            "eq_dn": [52, 88, 124, 160, 196, 203, 210, 224],
        },
    },
    # Dracula: purple, pink, cyan on near-black
    "dracula": {
        "base": {1: 210, 2: 84, 3: 228, 4: 141, 5: 212, 6: 117, 7: 255},
        "grey": 103, "key": 212, "head": 141,
        "border": {"spot": 117, "contract": 212, "ladder": 135, "book": 215, "variants": 84, "log": 171,
                   "gate": 225, "reports": 135, "settings": 146, "themes": 228, "help": 218},
        "fill": 54, "tab": 97, "tab_text": 231,
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
        "border": {"spot": 226, "contract": 201, "ladder": 51, "book": 199, "variants": 45, "log": 213,
                   "gate": 219, "reports": 220, "settings": 213, "themes": 228, "help": 213},
        "fill": 54, "tab": 91, "tab_text": 220,
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
        "base": {1: 196, 2: 118, 3: 214, 4: 92, 5: 134, 6: 208, 7: 223},
        "grey": 138, "key": 118, "head": 208,
        "border": {"spot": 208, "contract": 177, "ladder": 202, "book": 141, "variants": 76, "log": 215,
                   "gate": 150, "reports": 214, "settings": 223, "themes": 202, "help": 144},
        "fill": 53, "tab": 91, "tab_text": 222,
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
        "border": {"spot": 159, "contract": 218, "ladder": 153, "book": 225, "variants": 157, "log": 219,
                   "gate": 116, "reports": 146, "settings": 224, "themes": 219, "help": 194},
        "fill": 54, "tab": 97, "tab_text": 231,
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
        "border": {"spot": 153, "contract": 182, "ladder": 116, "book": 109, "variants": 150, "log": 189,
                   "gate": 173, "reports": 152, "settings": 222, "themes": 189, "help": 152},
        "fill": 17, "tab": 60, "tab_text": 255,
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
    # Valentine's Day: roses, blush pinks, and a little chocolate
    "valentine": {
        "base": {1: 196, 2: 211, 3: 223, 4: 132, 5: 199, 6: 217, 7: 225},
        "grey": 181, "key": 197, "head": 211,
        "border": {"spot": 211, "contract": 218, "ladder": 212, "book": 218, "variants": 212, "log": 224,
                   "gate": 212, "reports": 138, "settings": 212, "themes": 217, "help": 211},
        "fill": 52, "tab": 125, "tab_text": 225,
        "sym": 204, "event": 211, "unit": 181,
        "word_a": 224, "word_b": 218, "word_c": 181,
        "ticker": 211, "strike": 225, "slot": 197, "verb": 218, "variant": 217, "phrase": 224, "sep": 197,
        "value": 231,
        "rows": [231, 225, 224, 218, 217, 211, 204, 168, 162, 125],
        "fade_txt": [231, 225, 225, 224, 218, 217, 181, 175, 138, 132],
        "fade_ts": [218, 212, 211, 205, 204, 198, 168, 168, 162, 162],
        "grads": {
            "spot": [52, 89, 125, 161, 197, 204, 211, 218],
            "yes": [96, 132, 168, 204, 211, 218, 197, 160],
            "eq_up": [89, 125, 162, 168, 204, 211, 218, 225],
            "eq_dn": [52, 52, 88, 88, 124, 124, 160, 196],
        },
    },
    # St. Patrick's Day: shamrock greens, pot-of-gold, and a rainbow on the log's timestamps
    "st patrick": {
        "base": {1: 167, 2: 46, 3: 220, 4: 35, 5: 178, 6: 48, 7: 230},
        "grey": 150, "key": 220, "head": 220,
        "border": {"spot": 76, "contract": 220, "ladder": 40, "book": 178, "variants": 149, "log": 76,
                   "gate": 222, "reports": 120, "settings": 214, "themes": 151, "help": 184},
        "fill": 22, "tab": 28, "tab_text": 231,
        "sym": 220, "event": 77, "unit": 150,
        "word_a": 193, "word_b": 222, "word_c": 151,
        "ticker": 77, "strike": 220, "slot": 214, "verb": 186, "variant": 120, "phrase": 222, "sep": 220,
        "value": 222,
        "rows": [230, 229, 228, 227, 221, 185, 149, 113, 77, 71],
        "fade_txt": [230, 230, 194, 193, 157, 151, 150, 114, 108, 72],
        "fade_ts": [196, 208, 220, 226, 118, 46, 51, 39, 99, 201],
        "grads": {
            "spot": [22, 28, 34, 40, 76, 112, 184, 220],
            "yes": [22, 28, 34, 70, 106, 142, 178, 220],
            "eq_up": [22, 28, 34, 40, 46, 82, 190, 226],
            "eq_dn": [52, 88, 124, 130, 166, 167, 173, 216],
        },
    },
    # red light: darkroom / night-vision, every shade of red; gains show amber, losses deep red
    "red light": {
        "base": {1: 196, 2: 215, 3: 223, 4: 124, 5: 168, 6: 203, 7: 224},
        "grey": 131, "key": 196, "head": 203,
        "border": {"spot": 208, "contract": 172, "ladder": 210, "book": 174, "variants": 208, "log": 174,
                   "gate": 214, "reports": 224, "settings": 209, "themes": 223, "help": 210},
        "fill": 52, "tab": 124, "tab_text": 224,
        "sym": 203, "event": 167, "unit": 131,
        "word_a": 174, "word_b": 210, "word_c": 138,
        "ticker": 167, "strike": 217, "slot": 203, "verb": 174, "variant": 210, "phrase": 181, "sep": 160,
        "value": 217,
        "rows": [224, 217, 210, 203, 203, 197, 196, 167, 167, 131],
        "fade_txt": [224, 224, 217, 217, 210, 210, 174, 174, 167, 167],
        "fade_ts": [210, 203, 203, 197, 196, 196, 167, 167, 160, 160],
        "grads": {
            "spot": [52, 52, 88, 88, 124, 160, 196, 203],
            "yes": [52, 88, 124, 160, 196, 202, 209, 216],
            "eq_up": [88, 130, 166, 172, 208, 214, 215, 223],
            "eq_dn": [52, 52, 88, 88, 124, 124, 160, 160],
        },
    },
    # amber CRT: an old monochrome terminal glowing orange
    "amber": {
        "base": {1: 166, 2: 220, 3: 229, 4: 136, 5: 172, 6: 214, 7: 223},
        "grey": 137, "key": 229, "head": 214,
        "border": {"spot": 214, "contract": 178, "ladder": 220, "book": 221, "variants": 222, "log": 180,
                   "gate": 186, "reports": 220, "settings": 180, "themes": 221, "help": 144},
        "fill": 52, "tab": 130, "tab_text": 231,
        "sym": 220, "event": 214, "unit": 137,
        "word_a": 180, "word_b": 215, "word_c": 144,
        "ticker": 214, "strike": 229, "slot": 220, "verb": 179, "variant": 221, "phrase": 186, "sep": 172,
        "value": 229,
        "rows": [230, 229, 223, 222, 221, 215, 214, 178, 172, 136],
        "fade_txt": [230, 229, 223, 223, 222, 186, 180, 144, 137, 94],
        "fade_ts": [220, 214, 214, 208, 178, 172, 136, 130, 94, 58],
        "grads": {
            "spot": [52, 94, 130, 136, 172, 178, 214, 220],
            "yes": [58, 94, 136, 172, 178, 214, 220, 229],
            "eq_up": [94, 136, 172, 178, 214, 220, 221, 229],
            "eq_dn": [52, 88, 124, 130, 160, 166, 167, 173],
        },
    },
    # autumn: maple red, pumpkin, harvest gold, bark brown
    "autumn": {
        "base": {1: 196, 2: 106, 3: 214, 4: 66, 5: 131, 6: 179, 7: 230},
        "grey": 138, "key": 208, "head": 179,
        "border": {"spot": 214, "contract": 178, "ladder": 216, "book": 167, "variants": 142, "log": 174,
                   "gate": 186, "reports": 174, "settings": 214, "themes": 144, "help": 214},
        "fill": 52, "tab": 130, "tab_text": 231,
        "sym": 208, "event": 179, "unit": 138,
        "word_a": 180, "word_b": 216, "word_c": 144,
        "ticker": 179, "strike": 220, "slot": 208, "verb": 174, "variant": 186, "phrase": 180, "sep": 166,
        "value": 223,
        "rows": [230, 223, 222, 221, 215, 209, 173, 167, 131, 95],
        "fade_txt": [230, 230, 223, 223, 187, 180, 174, 138, 137, 95],
        "fade_ts": [214, 208, 202, 166, 167, 173, 131, 137, 130, 130],
        "grads": {
            "spot": [52, 88, 94, 130, 166, 172, 208, 214],
            "yes": [64, 100, 142, 178, 214, 208, 202, 160],
            "eq_up": [22, 58, 64, 100, 106, 142, 148, 185],
            "eq_dn": [52, 88, 124, 160, 166, 167, 173, 216],
        },
    },
    # Fourth of July: red, white and blue with firework sparkle
    "fourth of july": {
        "base": {1: 196, 2: 81, 3: 229, 4: 27, 5: 203, 6: 75, 7: 255},
        "grey": 146, "key": 196, "head": 75,
        "border": {"spot": 203, "contract": 255, "ladder": 153, "book": 203, "variants": 117, "log": 252,
                   "gate": 189, "reports": 217, "settings": 117, "themes": 210, "help": 153},
        "fill": 17, "tab": 25, "tab_text": 255,
        "sym": 255, "event": 75, "unit": 146,
        "word_a": 189, "word_b": 217, "word_c": 153,
        "ticker": 75, "strike": 210, "slot": 196, "verb": 189, "variant": 117, "phrase": 153, "sep": 196,
        "value": 231,
        "rows": [210, 203, 196, 160, 255, 231, 189, 111, 69, 27],
        "fade_txt": [255, 255, 231, 189, 189, 153, 147, 146, 104, 61],
        "fade_ts": [196, 203, 210, 224, 255, 189, 153, 111, 69, 27],
        "grads": {
            "spot": [17, 18, 19, 20, 21, 27, 33, 39],
            "yes": [19, 21, 27, 33, 255, 210, 203, 196],
            "eq_up": [17, 18, 19, 26, 33, 39, 75, 117],
            "eq_dn": [52, 88, 124, 160, 196, 203, 210, 217],
        },
    },
    # aurora borealis: green curtains shading into teal, violet and pink
    "aurora": {
        "base": {1: 204, 2: 48, 3: 228, 4: 63, 5: 171, 6: 50, 7: 195},
        "grey": 109, "key": 48, "head": 86,
        "border": {"spot": 48, "contract": 135, "ladder": 43, "book": 171, "variants": 123, "log": 135,
                   "gate": 35, "reports": 183, "settings": 123, "themes": 170, "help": 86},
        "fill": 17, "tab": 30, "tab_text": 231,
        "sym": 48, "event": 86, "unit": 109,
        "word_a": 158, "word_b": 183, "word_c": 152,
        "ticker": 86, "strike": 219, "slot": 171, "verb": 183, "variant": 122, "phrase": 158, "sep": 135,
        "value": 195,
        "rows": [195, 158, 122, 86, 50, 44, 38, 62, 98, 134, 170],
        "fade_txt": [195, 195, 158, 152, 152, 146, 140, 139, 103, 97],
        "fade_ts": [48, 49, 43, 37, 31, 61, 98, 134, 170, 162],
        "grads": {
            "spot": [17, 23, 29, 35, 41, 47, 49, 123],
            "yes": [23, 29, 36, 43, 62, 98, 134, 170],
            "eq_up": [22, 23, 29, 35, 41, 48, 85, 122],
            "eq_dn": [53, 89, 125, 161, 162, 168, 204, 211],
        },
    },
    # rainbow: every box a different color of the spectrum, rainbow lists and timestamps
    "rainbow": {
        "base": {1: 203, 2: 119, 3: 227, 4: 75, 5: 213, 6: 87, 7: 255},
        "grey": 189, "key": 226, "head": 87,
        "border": {"spot": 203, "contract": 208, "ladder": 226, "book": 46, "variants": 75, "log": 201,
                   "gate": 155, "reports": 213, "settings": 203, "themes": 221, "help": 86},
        "fill": 17, "tab": 62, "tab_text": 231,
        "sym": 208, "event": 87, "unit": 189,
        "word_a": 215, "word_b": 141, "word_c": 157,
        "ticker": 75, "strike": 227, "slot": 213, "verb": 141, "variant": 87, "phrase": 215, "sep": 201,
        "value": 227,
        "rows": [203, 209, 215, 221, 227, 191, 155, 119, 86, 87, 75, 105, 141, 177, 213],
        "fade_txt": [255, 255, 231, 225, 219, 189, 195, 194, 230, 224],
        "fade_ts": [196, 208, 220, 226, 118, 46, 51, 39, 99, 201],
        "grads": {
            "spot": [21, 27, 33, 51, 46, 226, 208, 196],
            "yes": [129, 33, 51, 46, 226, 214, 208, 196],
            "eq_up": [129, 63, 33, 39, 51, 49, 46, 118],
            "eq_dn": [53, 89, 125, 161, 197, 203, 209, 215],
        },
    },
    # prism: one big gradient, cyan at the top left sweeping to magenta at the bottom right
    "prism": {
        "base": {1: 204, 2: 86, 3: 228, 4: 69, 5: 207, 6: 51, 7: 231},
        "grey": 152, "key": 231, "head": 123,
        "border": {"spot": 51, "contract": 45, "ladder": 75, "book": 105, "variants": 177, "log": 200,
                   "gate": 37, "reports": 213, "settings": 189, "themes": 207, "help": 183},
        "fill": 54, "tab": 92, "tab_text": 123,
        "sym": 51, "event": 81, "unit": 152,
        "word_a": 159, "word_b": 183, "word_c": 189,
        "ticker": 81, "strike": 219, "slot": 207, "verb": 183, "variant": 123, "phrase": 189, "sep": 171,
        "value": 225,
        "rows": [123, 117, 111, 105, 141, 177, 213, 207, 201],
        "fade_txt": [231, 195, 195, 189, 189, 183, 183, 177, 176, 170],
        "fade_ts": [51, 45, 39, 69, 99, 135, 171, 207, 201, 200],
        "grads": {
            "spot": [23, 30, 37, 44, 51, 87, 123, 159],
            "yes": [51, 45, 39, 63, 99, 135, 171, 201],
            "eq_up": [24, 31, 38, 45, 51, 87, 123, 195],
            "eq_dn": [53, 90, 127, 164, 201, 206, 212, 218],
        },
    },
    # lava: one big gradient from molten red to white-hot yellow; gains glow yellow, losses dark red
    "lava": {
        "base": {1: 196, 2: 227, 3: 229, 4: 130, 5: 166, 6: 214, 7: 230},
        "grey": 180, "key": 231, "head": 220,
        "border": {"spot": 208, "contract": 214, "ladder": 209, "book": 208, "variants": 186, "log": 220,
                   "gate": 216, "reports": 221, "settings": 209, "themes": 227, "help": 216},
        "fill": 52, "tab": 124, "tab_text": 223,
        "sym": 214, "event": 208, "unit": 180,
        "word_a": 223, "word_b": 216, "word_c": 186,
        "ticker": 208, "strike": 229, "slot": 202, "verb": 216, "variant": 221, "phrase": 223, "sep": 196,
        "value": 230,
        "rows": [230, 229, 228, 227, 221, 214, 208, 209, 203, 167],
        "fade_txt": [230, 230, 229, 223, 223, 222, 216, 215, 209, 173],
        "fade_ts": [228, 221, 220, 214, 208, 202, 203, 167, 167, 131],
        "grads": {
            "spot": [52, 88, 124, 160, 196, 202, 208, 220],
            "yes": [52, 88, 124, 160, 202, 208, 214, 226],
            "eq_up": [94, 130, 166, 172, 208, 214, 220, 228],
            "eq_dn": [52, 52, 88, 88, 124, 124, 160, 160],
        },
    },
    # ice and fire: the screen runs from icy blue at the top to flames at the bottom
    "ice and fire": {
        "base": {1: 203, 2: 86, 3: 222, 4: 33, 5: 209, 6: 117, 7: 255},
        "grey": 146, "key": 214, "head": 153,
        "border": {"spot": 45, "contract": 117, "ladder": 189, "book": 168, "variants": 202, "log": 203,
                   "gate": 215, "reports": 189, "settings": 203, "themes": 51, "help": 217},
        "fill": 17, "tab": 25, "tab_text": 255,
        "sym": 117, "event": 75, "unit": 146,
        "word_a": 153, "word_b": 217, "word_c": 223,
        "ticker": 75, "strike": 216, "slot": 208, "verb": 189, "variant": 117, "phrase": 223, "sep": 203,
        "value": 230,
        "rows": [195, 159, 117, 75, 69, 104, 168, 203, 209, 215],
        "fade_txt": [255, 255, 231, 195, 189, 225, 224, 223, 217, 216],
        "fade_ts": [51, 45, 39, 69, 105, 168, 203, 202, 208, 214],
        "grads": {
            "spot": [17, 19, 27, 33, 39, 45, 51, 159],
            "yes": [33, 39, 45, 159, 223, 214, 208, 196],
            "eq_up": [17, 19, 25, 31, 37, 44, 87, 159],
            "eq_dn": [52, 88, 124, 160, 196, 202, 208, 214],
        },
    },
    # galaxy: deep space indigo through nebula violet to starlight
    "galaxy": {
        "base": {1: 204, 2: 122, 3: 229, 4: 62, 5: 177, 6: 111, 7: 231},
        "grey": 146, "key": 229, "head": 147,
        "border": {"spot": 171, "contract": 219, "ladder": 111, "book": 171, "variants": 111, "log": 171,
                   "gate": 189, "reports": 213, "settings": 229, "themes": 183, "help": 153},
        "fill": 54, "tab": 62, "tab_text": 231,
        "sym": 147, "event": 111, "unit": 146,
        "word_a": 183, "word_b": 219, "word_c": 189,
        "ticker": 111, "strike": 229, "slot": 177, "verb": 183, "variant": 153, "phrase": 189, "sep": 99,
        "value": 231,
        "rows": [231, 225, 219, 183, 177, 141, 105, 99, 63, 69],
        "fade_txt": [231, 231, 225, 189, 189, 183, 147, 146, 140, 104],
        "fade_ts": [229, 225, 219, 213, 177, 141, 105, 99, 63, 62],
        "grads": {
            "spot": [17, 18, 54, 55, 56, 93, 135, 183],
            "yes": [18, 19, 55, 92, 129, 171, 213, 225],
            "eq_up": [17, 18, 24, 31, 37, 44, 86, 158],
            "eq_dn": [53, 89, 125, 161, 168, 204, 211, 218],
        },
    },
    # mint chip: mint green and cool cream with chocolate brown
    "mint chip": {
        "base": {1: 167, 2: 121, 3: 229, 4: 79, 5: 138, 6: 122, 7: 230},
        "grey": 144, "key": 121, "head": 158,
        "border": {"spot": 121, "contract": 137, "ladder": 151, "book": 136, "variants": 158, "log": 173,
                   "gate": 157, "reports": 180, "settings": 158, "themes": 144, "help": 230},
        "fill": 22, "tab": 29, "tab_text": 231,
        "sym": 121, "event": 115, "unit": 144,
        "word_a": 187, "word_b": 180, "word_c": 151,
        "ticker": 115, "strike": 230, "slot": 173, "verb": 180, "variant": 158, "phrase": 187, "sep": 137,
        "value": 230,
        "rows": [230, 194, 158, 157, 121, 115, 108, 144, 180, 137],
        "fade_txt": [230, 230, 194, 194, 188, 187, 151, 144, 138, 137],
        "fade_ts": [158, 157, 121, 115, 79, 72, 137, 136, 130, 130],
        "grads": {
            "spot": [52, 94, 130, 137, 79, 115, 121, 158],
            "yes": [94, 130, 137, 180, 158, 121, 115, 79],
            "eq_up": [22, 29, 36, 72, 79, 115, 121, 158],
            "eq_dn": [52, 88, 94, 130, 131, 167, 173, 180],
        },
    },
    # coffee: espresso, caramel, latte foam
    "coffee": {
        "base": {1: 167, 2: 150, 3: 222, 4: 137, 5: 174, 6: 180, 7: 230},
        "grey": 138, "key": 215, "head": 180,
        "border": {"spot": 180, "contract": 216, "ladder": 215, "book": 180, "variants": 186, "log": 174,
                   "gate": 186, "reports": 215, "settings": 224, "themes": 223, "help": 174},
        "fill": 52, "tab": 94, "tab_text": 230,
        "sym": 215, "event": 180, "unit": 138,
        "word_a": 187, "word_b": 181, "word_c": 144,
        "ticker": 180, "strike": 223, "slot": 215, "verb": 181, "variant": 186, "phrase": 187, "sep": 137,
        "value": 230,
        "rows": [230, 224, 223, 187, 181, 180, 174, 173, 137, 131],
        "fade_txt": [230, 230, 224, 223, 187, 181, 180, 144, 138, 137],
        "fade_ts": [215, 179, 180, 173, 137, 137, 131, 131, 95, 95],
        "grads": {
            "spot": [52, 58, 94, 130, 136, 172, 179, 223],
            "yes": [58, 94, 136, 172, 179, 216, 223, 230],
            "eq_up": [58, 64, 100, 106, 143, 149, 150, 187],
            "eq_dn": [52, 88, 94, 130, 131, 167, 174, 181],
        },
    },
    # bubblegum: bright candy pinks and sky blue
    "bubblegum": {
        "base": {1: 197, 2: 121, 3: 228, 4: 117, 5: 213, 6: 159, 7: 231},
        "grey": 182, "key": 159, "head": 219,
        "border": {"spot": 213, "contract": 117, "ladder": 219, "book": 159, "variants": 207, "log": 153,
                   "gate": 218, "reports": 147, "settings": 183, "themes": 225, "help": 213},
        "fill": 53, "tab": 126, "tab_text": 255,
        "sym": 213, "event": 117, "unit": 182,
        "word_a": 225, "word_b": 159, "word_c": 218,
        "ticker": 117, "strike": 228, "slot": 213, "verb": 225, "variant": 159, "phrase": 218, "sep": 207,
        "value": 225,
        "rows": [231, 225, 219, 213, 212, 218, 189, 153, 117, 159],
        "fade_txt": [231, 231, 225, 225, 219, 218, 189, 182, 176, 175],
        "fade_ts": [159, 153, 117, 111, 147, 183, 219, 213, 207, 206],
        "grads": {
            "spot": [90, 126, 162, 198, 205, 212, 219, 225],
            "yes": [117, 153, 189, 225, 219, 213, 207, 201],
            "eq_up": [24, 31, 38, 81, 117, 123, 159, 195],
            "eq_dn": [89, 125, 161, 197, 204, 211, 218, 225],
        },
    },
    # sakura: cherry blossom pinks on soft spring greens
    "sakura": {
        "base": {1: 168, 2: 150, 3: 223, 4: 146, 5: 218, 6: 151, 7: 231},
        "grey": 181, "key": 211, "head": 218,
        "border": {"spot": 225, "contract": 151, "ladder": 225, "book": 219, "variants": 150, "log": 224,
                   "gate": 108, "reports": 219, "settings": 224, "themes": 231, "help": 194},
        "fill": 53, "tab": 132, "tab_text": 231,
        "sym": 218, "event": 151, "unit": 181,
        "word_a": 225, "word_b": 194, "word_c": 224,
        "ticker": 151, "strike": 225, "slot": 211, "verb": 218, "variant": 194, "phrase": 224, "sep": 211,
        "value": 231,
        "rows": [231, 225, 224, 218, 217, 181, 175, 151, 150, 108],
        "fade_txt": [231, 231, 225, 225, 224, 218, 182, 181, 175, 139],
        "fade_ts": [225, 219, 218, 212, 211, 175, 151, 150, 114, 108],
        "grads": {
            "spot": [95, 132, 168, 175, 211, 218, 224, 231],
            "yes": [108, 150, 151, 194, 225, 218, 211, 168],
            "eq_up": [65, 71, 107, 114, 150, 151, 157, 194],
            "eq_dn": [89, 125, 131, 168, 174, 175, 181, 224],
        },
    },
    # desert: sand, terracotta, sage and a turquoise sky
    "desert": {
        "base": {1: 167, 2: 108, 3: 222, 4: 73, 5: 173, 6: 80, 7: 230},
        "grey": 144, "key": 80, "head": 180,
        "border": {"spot": 223, "contract": 80, "ladder": 144, "book": 108, "variants": 187, "log": 73,
                   "gate": 222, "reports": 187, "settings": 223, "themes": 151, "help": 223},
        "fill": 52, "tab": 131, "tab_text": 231,
        "sym": 209, "event": 80, "unit": 144,
        "word_a": 187, "word_b": 180, "word_c": 151,
        "ticker": 73, "strike": 223, "slot": 209, "verb": 180, "variant": 116, "phrase": 187, "sep": 173,
        "value": 230,
        "rows": [230, 223, 222, 216, 180, 173, 137, 108, 109, 80],
        "fade_txt": [230, 230, 224, 223, 187, 187, 181, 144, 138, 137],
        "fade_ts": [80, 116, 109, 108, 144, 180, 173, 137, 131, 131],
        "grads": {
            "spot": [52, 94, 130, 137, 173, 180, 216, 223],
            "yes": [30, 37, 73, 108, 144, 180, 173, 167],
            "eq_up": [22, 58, 65, 72, 108, 114, 151, 187],
            "eq_dn": [52, 88, 94, 131, 167, 173, 174, 181],
        },
    },
    # tropical: lagoon teal, mango, flamingo pink, palm green
    "tropical": {
        "base": {1: 204, 2: 49, 3: 221, 4: 38, 5: 211, 6: 44, 7: 231},
        "grey": 152, "key": 214, "head": 51,
        "border": {"spot": 51, "contract": 214, "ladder": 211, "book": 41, "variants": 159, "log": 209,
                   "gate": 50, "reports": 223, "settings": 51, "themes": 221, "help": 217},
        "fill": 23, "tab": 30, "tab_text": 231,
        "sym": 214, "event": 51, "unit": 152,
        "word_a": 158, "word_b": 217, "word_c": 223,
        "ticker": 44, "strike": 221, "slot": 211, "verb": 217, "variant": 86, "phrase": 223, "sep": 209,
        "value": 230,
        "rows": [230, 229, 221, 215, 209, 211, 218, 158, 86, 44],
        "fade_txt": [231, 231, 230, 224, 224, 194, 158, 152, 116, 109],
        "fade_ts": [221, 215, 209, 211, 212, 86, 50, 44, 38, 37],
        "grads": {
            "spot": [23, 30, 37, 44, 50, 86, 122, 159],
            "yes": [30, 37, 44, 49, 221, 215, 209, 204],
            "eq_up": [22, 29, 35, 41, 48, 49, 85, 122],
            "eq_dn": [89, 125, 161, 168, 204, 210, 211, 217],
        },
    },
    # midnight: navy and silver under moonlight
    "midnight": {
        "base": {1: 174, 2: 115, 3: 230, 4: 61, 5: 146, 6: 110, 7: 255},
        "grey": 103, "key": 230, "head": 153,
        "border": {"spot": 146, "contract": 110, "ladder": 153, "book": 189, "variants": 117, "log": 110,
                   "gate": 153, "reports": 110, "settings": 146, "themes": 189, "help": 117},
        "fill": 17, "tab": 61, "tab_text": 231,
        "sym": 153, "event": 110, "unit": 103,
        "word_a": 188, "word_b": 152, "word_c": 146,
        "ticker": 110, "strike": 230, "slot": 153, "verb": 188, "variant": 152, "phrase": 188, "sep": 67,
        "value": 255,
        "rows": [255, 254, 253, 252, 189, 153, 147, 110, 104, 67],
        "fade_txt": [255, 255, 254, 253, 252, 251, 250, 249, 247, 245],
        "fade_ts": [230, 189, 153, 147, 111, 110, 104, 67, 61, 61],
        "grads": {
            "spot": [19, 20, 25, 26, 61, 67, 104, 146],
            "yes": [17, 18, 61, 67, 110, 153, 189, 230],
            "eq_up": [17, 23, 24, 30, 37, 73, 115, 151],
            "eq_dn": [52, 53, 89, 95, 131, 132, 174, 181],
        },
    },
    # grape soda: purples, fizzy violet and lime
    "grape": {
        "base": {1: 204, 2: 154, 3: 228, 4: 98, 5: 165, 6: 141, 7: 231},
        "grey": 140, "key": 154, "head": 177,
        "border": {"spot": 177, "contract": 141, "ladder": 177, "book": 141, "variants": 139, "log": 177,
                   "gate": 155, "reports": 219, "settings": 154, "themes": 189, "help": 177},
        "fill": 54, "tab": 92, "tab_text": 225,
        "sym": 177, "event": 141, "unit": 140,
        "word_a": 183, "word_b": 219, "word_c": 189,
        "ticker": 141, "strike": 191, "slot": 165, "verb": 183, "variant": 177, "phrase": 189, "sep": 129,
        "value": 225,
        "rows": [225, 219, 183, 177, 171, 141, 135, 129, 98, 92],
        "fade_txt": [231, 231, 225, 189, 189, 183, 183, 146, 140, 139],
        "fade_ts": [191, 155, 177, 171, 165, 135, 129, 92, 91, 54],
        "grads": {
            "spot": [53, 54, 55, 92, 93, 129, 165, 177],
            "yes": [54, 91, 128, 165, 171, 177, 190, 154],
            "eq_up": [22, 28, 34, 70, 112, 148, 154, 191],
            "eq_dn": [53, 89, 125, 161, 168, 204, 211, 218],
        },
    },
    # sherbet: orange, raspberry and lime scoops
    "sherbet": {
        "base": {1: 204, 2: 155, 3: 228, 4: 215, 5: 211, 6: 191, 7: 231},
        "grey": 181, "key": 155, "head": 215,
        "border": {"spot": 215, "contract": 218, "ladder": 191, "book": 223, "variants": 212, "log": 155,
                   "gate": 172, "reports": 193, "settings": 172, "themes": 218, "help": 215},
        "fill": 53, "tab": 131, "tab_text": 231,
        "sym": 215, "event": 191, "unit": 181,
        "word_a": 223, "word_b": 218, "word_c": 193,
        "ticker": 191, "strike": 223, "slot": 211, "verb": 218, "variant": 193, "phrase": 223, "sep": 211,
        "value": 230,
        "rows": [230, 223, 216, 215, 209, 211, 218, 193, 192, 191],
        "fade_txt": [231, 231, 230, 224, 223, 223, 217, 181, 180, 174],
        "fade_ts": [215, 209, 210, 211, 212, 218, 192, 191, 155, 149],
        "grads": {
            "spot": [130, 166, 172, 209, 215, 216, 223, 230],
            "yes": [148, 155, 191, 229, 223, 216, 211, 204],
            "eq_up": [64, 70, 106, 112, 148, 155, 191, 193],
            "eq_dn": [89, 125, 161, 168, 204, 211, 217, 224],
        },
    },
    # monochrome: one smooth grey gradient; only gains, losses and the key keep a color
    "monochrome": {
        "base": {1: 210, 2: 151, 3: 255, 4: 247, 5: 250, 6: 252, 7: 254},
        "grey": 245, "key": 255, "head": 252,
        "border": {"spot": 255, "contract": 252, "ladder": 249, "book": 250, "variants": 251, "log": 254,
                   "gate": 250, "reports": 253, "settings": 251, "themes": 252, "help": 249},
        "fill": 237, "tab": 242, "tab_text": 231,
        "sym": 255, "event": 252, "unit": 245,
        "word_a": 248, "word_b": 250, "word_c": 247,
        "ticker": 250, "strike": 255, "slot": 255, "verb": 249, "variant": 253, "phrase": 248, "sep": 243,
        "value": 255,
        "rows": [255, 254, 253, 252, 251, 250, 249, 248, 247, 246],
        "fade_txt": [255, 255, 254, 253, 252, 251, 250, 249, 248, 247],
        "fade_ts": [255, 253, 251, 249, 247, 245, 244, 243, 242, 241],
        "grads": {
            "spot": [236, 238, 240, 243, 246, 249, 252, 255],
            "yes": [238, 240, 242, 244, 246, 248, 251, 255],
            "eq_up": [236, 238, 240, 243, 246, 249, 252, 255],
            "eq_dn": [52, 88, 124, 131, 167, 174, 181, 224],
        },
    },
    # deep forest: pine, moss and fern with mushroom and berry accents
    "forest": {
        "base": {1: 167, 2: 113, 3: 186, 4: 29, 5: 132, 6: 72, 7: 194},
        "grey": 108, "key": 186, "head": 150,
        "border": {"spot": 113, "contract": 149, "ladder": 157, "book": 187, "variants": 113, "log": 194,
                   "gate": 174, "reports": 157, "settings": 187, "themes": 113, "help": 186},
        "fill": 22, "tab": 65, "tab_text": 231,
        "sym": 113, "event": 72, "unit": 108,
        "word_a": 151, "word_b": 144, "word_c": 187,
        "ticker": 72, "strike": 186, "slot": 174, "verb": 144, "variant": 150, "phrase": 187, "sep": 101,
        "value": 194,
        "rows": [194, 193, 157, 150, 114, 113, 107, 71, 65, 64],
        "fade_txt": [194, 194, 194, 193, 151, 151, 150, 108, 108, 102],
        "fade_ts": [186, 150, 149, 113, 107, 71, 71, 65, 65, 64],
        "grads": {
            "spot": [22, 22, 28, 29, 64, 71, 107, 150],
            "yes": [22, 28, 64, 101, 137, 143, 174, 167],
            "eq_up": [22, 28, 34, 70, 76, 113, 150, 193],
            "eq_dn": [52, 88, 94, 131, 132, 167, 174, 181],
        },
    },
    # gruvbox: warm retro browns with earthy red, olive and gold
    "gruvbox": {
        "base": {1: 203, 2: 142, 3: 214, 4: 108, 5: 174, 6: 108, 7: 187},
        "grey": 138, "key": 208, "head": 214,
        "border": {"spot": 108, "contract": 174, "ladder": 208, "book": 214, "variants": 142, "log": 247,
                   "gate": 209, "reports": 107, "settings": 221, "themes": 138, "help": 230},
        "fill": 237, "tab": 94, "tab_text": 230,
        "sym": 214, "event": 208, "unit": 138,
        "word_a": 187, "word_b": 174, "word_c": 144,
        "ticker": 108, "strike": 214, "slot": 208, "verb": 174, "variant": 108, "phrase": 144, "sep": 244,
        "value": 230,
        "rows": [230, 221, 214, 208, 209, 174, 138, 247, 108, 108],
        "fade_txt": [230, 223, 223, 187, 187, 181, 144, 144, 144, 138],
        "fade_ts": [214, 214, 208, 208, 208, 166, 166, 167, 137, 138],
        "grads": {
            "spot": [237, 239, 59, 66, 66, 108, 143, 214],
            "yes": [71, 107, 142, 178, 214, 208, 202, 203],
            "eq_up": [58, 58, 100, 100, 142, 142, 185, 185],
            "eq_dn": [52, 88, 124, 124, 160, 203, 203, 210],
        },
    },
    # catppuccin mocha: soft pastels on a dark base
    "catppuccin": {
        "base": {1: 211, 2: 151, 3: 223, 4: 111, 5: 183, 6: 116, 7: 189},
        "grey": 247, "key": 216, "head": 147,
        "border": {"spot": 116, "contract": 146, "ladder": 74, "book": 218, "variants": 151, "log": 117,
                   "gate": 216, "reports": 74, "settings": 217, "themes": 146, "help": 224},
        "fill": 54, "tab": 97, "tab_text": 231,
        "sym": 116, "event": 147, "unit": 247,
        "word_a": 146, "word_b": 218, "word_c": 146,
        "ticker": 111, "strike": 223, "slot": 216, "verb": 183, "variant": 116, "phrase": 224, "sep": 103,
        "value": 224,
        "rows": [224, 224, 183, 183, 147, 147, 111, 117, 116, 116],
        "fade_txt": [224, 254, 254, 189, 189, 152, 146, 146, 145, 247],
        "fade_ts": [216, 217, 218, 183, 183, 147, 147, 111, 104, 103],
        "grads": {
            "spot": [239, 240, 60, 74, 117, 116, 117, 147],
            "yes": [116, 151, 151, 187, 223, 216, 216, 211],
            "eq_up": [238, 65, 71, 71, 114, 150, 151, 194],
            "eq_dn": [236, 89, 125, 161, 167, 210, 217, 224],
        },
    },
    # rosé pine: muted rose, iris and foam, like dusk
    "rose pine": {
        "base": {1: 168, 2: 151, 3: 216, 4: 152, 5: 182, 6: 152, 7: 189},
        "grey": 103, "key": 216, "head": 182,
        "border": {"spot": 152, "contract": 189, "ladder": 181, "book": 216, "variants": 73, "log": 217,
                   "gate": 110, "reports": 224, "settings": 217, "themes": 255, "help": 217},
        "fill": 54, "tab": 96, "tab_text": 231,
        "sym": 181, "event": 182, "unit": 103,
        "word_a": 189, "word_b": 181, "word_c": 103,
        "ticker": 152, "strike": 216, "slot": 168, "verb": 182, "variant": 181, "phrase": 189, "sep": 60,
        "value": 189,
        "rows": [255, 224, 217, 181, 174, 175, 176, 146, 146, 152],
        "fade_txt": [189, 189, 189, 189, 253, 252, 146, 146, 248, 103],
        "fade_ts": [216, 216, 217, 182, 182, 146, 146, 116, 103, 60],
        "grads": {
            "spot": [236, 237, 239, 60, 67, 110, 152, 189],
            "yes": [66, 73, 152, 186, 216, 217, 175, 168],
            "eq_up": [237, 239, 59, 66, 73, 115, 151, 194],
            "eq_dn": [235, 237, 95, 131, 168, 168, 211, 218],
        },
    },
    # solarized dark: the classic blue-cyan base with saturated accents
    "solarized": {
        "base": {1: 166, 2: 100, 3: 136, 4: 32, 5: 168, 6: 36, 7: 254},
        "grey": 247, "key": 166, "head": 32,
        "border": {"spot": 247, "contract": 168, "ladder": 36, "book": 136, "variants": 179, "log": 247,
                   "gate": 136, "reports": 73, "settings": 179, "themes": 37, "help": 230},
        "fill": 17, "tab": 24, "tab_text": 254,
        "sym": 36, "event": 32, "unit": 246,
        "word_a": 247, "word_b": 62, "word_c": 254,
        "ticker": 32, "strike": 136, "slot": 166, "verb": 62, "variant": 36, "phrase": 247, "sep": 242,
        "value": 230,
        "rows": [230, 254, 179, 136, 166, 168, 97, 68, 32, 36],
        "fade_txt": [230, 255, 254, 254, 252, 249, 247, 247, 246, 246],
        "fade_ts": [36, 31, 32, 68, 68, 97, 132, 168, 96, 242],
        "grads": {
            "spot": [236, 23, 23, 31, 31, 37, 73, 247],
            "yes": [32, 31, 72, 100, 136, 166, 166, 166],
            "eq_up": [234, 58, 58, 64, 100, 100, 142, 185],
            "eq_dn": [52, 52, 88, 124, 124, 160, 167, 210],
        },
    },
    # everforest: calm sage and moss, gentle on the eyes
    "everforest": {
        "base": {1: 174, 2: 144, 3: 180, 4: 109, 5: 175, 6: 108, 7: 187},
        "grey": 245, "key": 174, "head": 144,
        "border": {"spot": 253, "contract": 175, "ladder": 187, "book": 180, "variants": 187, "log": 174,
                   "gate": 255, "reports": 187, "settings": 253, "themes": 255, "help": 253},
        "fill": 237, "tab": 65, "tab_text": 231,
        "sym": 144, "event": 109, "unit": 245,
        "word_a": 187, "word_b": 175, "word_c": 248,
        "ticker": 109, "strike": 180, "slot": 174, "verb": 175, "variant": 108, "phrase": 187, "sep": 244,
        "value": 255,
        "rows": [255, 187, 180, 174, 175, 145, 109, 109, 108, 144],
        "fade_txt": [255, 253, 187, 187, 249, 145, 248, 247, 246, 245],
        "fade_ts": [180, 180, 144, 108, 108, 108, 109, 109, 246, 244],
        "grads": {
            "spot": [237, 238, 240, 242, 66, 108, 145, 187],
            "yes": [109, 109, 108, 144, 180, 180, 174, 174],
            "eq_up": [236, 238, 240, 65, 107, 144, 150, 193],
            "eq_dn": [235, 237, 239, 131, 131, 174, 210, 217],
        },
    },
    # kanagawa: ink-wash blues with autumn red and old gold
    "kanagawa": {
        "base": {1: 168, 2: 107, 3: 180, 4: 110, 5: 103, 6: 109, 7: 187},
        "grey": 242, "key": 215, "head": 110,
        "border": {"spot": 109, "contract": 249, "ladder": 216, "book": 180, "variants": 109, "log": 174,
                   "gate": 186, "reports": 168, "settings": 187, "themes": 249, "help": 215},
        "fill": 17, "tab": 60, "tab_text": 255,
        "sym": 110, "event": 110, "unit": 242,
        "word_a": 180, "word_b": 174, "word_c": 146,
        "ticker": 110, "strike": 180, "slot": 215, "verb": 103, "variant": 109, "phrase": 180, "sep": 59,
        "value": 187,
        "rows": [187, 186, 216, 215, 174, 139, 103, 104, 110, 110],
        "fade_txt": [187, 187, 187, 187, 187, 187, 180, 144, 144, 246],
        "fade_ts": [215, 215, 180, 249, 109, 110, 110, 104, 67, 59],
        "grads": {
            "spot": [234, 235, 237, 238, 60, 68, 110, 110],
            "yes": [239, 66, 108, 180, 216, 215, 209, 168],
            "eq_up": [236, 238, 240, 65, 107, 107, 150, 187],
            "eq_dn": [236, 237, 238, 131, 167, 167, 174, 217],
        },
    },
    # vaporwave: hot pink, aqua and mint, all neon
    "vaporwave": {
        "base": {1: 204, 2: 49, 3: 228, 4: 45, 5: 206, 6: 45, 7: 255},
        "grey": 146, "key": 228, "head": 45,
        "border": {"spot": 45, "contract": 206, "ladder": 105, "book": 228, "variants": 49, "log": 105,
                   "gate": 218, "reports": 39, "settings": 206, "themes": 44, "help": 206},
        "fill": 54, "tab": 91, "tab_text": 49,
        "sym": 45, "event": 135, "unit": 146,
        "word_a": 218, "word_b": 135, "word_c": 159,
        "ticker": 45, "strike": 228, "slot": 206, "verb": 135, "variant": 49, "phrase": 218, "sep": 97,
        "value": 231,
        "rows": [231, 218, 206, 171, 105, 39, 44, 49, 121, 228],
        "fade_txt": [231, 225, 225, 225, 225, 183, 183, 183, 182, 146],
        "fade_ts": [49, 44, 45, 75, 105, 171, 170, 206, 170, 133],
        "grads": {
            "spot": [53, 54, 91, 134, 105, 39, 81, 159],
            "yes": [45, 69, 135, 170, 211, 223, 216, 204],
            "eq_up": [235, 23, 29, 36, 42, 49, 85, 158],
            "eq_dn": [53, 89, 125, 161, 167, 204, 211, 218],
        },
    },
    # volcano: obsidian greys under rivers of magma
    "volcano": {
        "base": {1: 202, 2: 149, 3: 221, 4: 209, 5: 168, 6: 215, 7: 254},
        "grey": 247, "key": 221, "head": 209,
        "border": {"spot": 214, "contract": 220, "ladder": 214, "book": 144, "variants": 223, "log": 181,
                   "gate": 168, "reports": 181, "settings": 221, "themes": 181, "help": 214},
        "fill": 52, "tab": 130, "tab_text": 231,
        "sym": 209, "event": 221, "unit": 247,
        "word_a": 223, "word_b": 209, "word_c": 181,
        "ticker": 209, "strike": 221, "slot": 202, "verb": 168, "variant": 215, "phrase": 223, "sep": 244,
        "value": 230,
        "rows": [229, 221, 220, 214, 208, 208, 202, 202, 202, 160],
        "fade_txt": [230, 255, 254, 254, 253, 187, 181, 181, 248, 247],
        "fade_ts": [221, 220, 214, 208, 202, 202, 166, 160, 131, 95],
        "grads": {
            "spot": [234, 235, 236, 88, 130, 202, 208, 221],
            "yes": [244, 173, 214, 208, 208, 202, 202, 202],
            "eq_up": [235, 237, 58, 64, 71, 113, 150, 193],
            "eq_dn": [52, 52, 88, 124, 160, 202, 203, 216],
        },
    },
    # arctic: snow white, glacier blue and polar night
    "arctic": {
        "base": {1: 210, 2: 115, 3: 223, 4: 75, 5: 147, 6: 117, 7: 231},
        "grey": 146, "key": 231, "head": 153,
        "border": {"spot": 195, "contract": 189, "ladder": 147, "book": 116, "variants": 104, "log": 147,
                   "gate": 104, "reports": 254, "settings": 104, "themes": 189, "help": 147},
        "fill": 17, "tab": 31, "tab_text": 231,
        "sym": 195, "event": 153, "unit": 146,
        "word_a": 189, "word_b": 153, "word_c": 195,
        "ticker": 75, "strike": 223, "slot": 231, "verb": 147, "variant": 117, "phrase": 189, "sep": 67,
        "value": 231,
        "rows": [231, 195, 195, 153, 153, 117, 117, 111, 110, 104],
        "fade_txt": [231, 231, 231, 231, 255, 254, 189, 152, 152, 146],
        "fade_ts": [231, 195, 189, 153, 117, 117, 111, 74, 68, 67],
        "grads": {
            "spot": [236, 238, 24, 61, 68, 117, 189, 231],
            "yes": [75, 117, 117, 195, 254, 223, 216, 210],
            "eq_up": [237, 23, 29, 71, 72, 115, 158, 195],
            "eq_dn": [235, 237, 89, 131, 167, 168, 210, 224],
        },
    },
    # toxic: acid green slime with hazard yellow and poison purple
    "toxic": {
        "base": {1: 203, 2: 118, 3: 190, 4: 135, 5: 171, 6: 85, 7: 194},
        "grey": 144, "key": 190, "head": 118,
        "border": {"spot": 118, "contract": 135, "ladder": 190, "book": 85, "variants": 76, "log": 171,
                   "gate": 192, "reports": 104, "settings": 151, "themes": 141, "help": 192},
        "fill": 22, "tab": 64, "tab_text": 231,
        "sym": 118, "event": 190, "unit": 144,
        "word_a": 192, "word_b": 141, "word_c": 151,
        "ticker": 85, "strike": 190, "slot": 118, "verb": 141, "variant": 171, "phrase": 192, "sep": 65,
        "value": 230,
        "rows": [230, 227, 190, 155, 154, 83, 85, 104, 135, 171],
        "fade_txt": [230, 230, 194, 194, 193, 187, 151, 150, 150, 144],
        "fade_ts": [190, 154, 118, 83, 84, 79, 104, 99, 97, 60],
        "grads": {
            "spot": [234, 236, 58, 64, 76, 118, 154, 190],
            "yes": [135, 74, 84, 119, 154, 190, 214, 203],
            "eq_up": [234, 22, 22, 64, 70, 118, 155, 193],
            "eq_dn": [53, 53, 89, 125, 161, 203, 210, 217],
        },
    },
    # royal: regal purple and navy trimmed in gold
    "royal": {
        "base": {1: 167, 2: 78, 3: 221, 4: 69, 5: 135, 6: 75, 7: 255},
        "grey": 140, "key": 221, "head": 221,
        "border": {"spot": 221, "contract": 175, "ladder": 146, "book": 179, "variants": 75, "log": 137,
                   "gate": 75, "reports": 175, "settings": 229, "themes": 189, "help": 222},
        "fill": 54, "tab": 62, "tab_text": 231,
        "sym": 221, "event": 135, "unit": 140,
        "word_a": 189, "word_b": 222, "word_c": 146,
        "ticker": 69, "strike": 221, "slot": 179, "verb": 135, "variant": 75, "phrase": 189, "sep": 60,
        "value": 229,
        "rows": [229, 221, 221, 179, 175, 141, 135, 98, 98, 69],
        "fade_txt": [255, 255, 255, 255, 189, 189, 183, 146, 146, 140],
        "fade_ts": [222, 221, 221, 180, 140, 141, 105, 69, 61, 60],
        "grads": {
            "spot": [235, 17, 54, 55, 62, 135, 175, 221],
            "yes": [69, 105, 140, 180, 215, 215, 209, 167],
            "eq_up": [235, 237, 23, 65, 71, 78, 115, 194],
            "eq_dn": [52, 52, 89, 125, 131, 167, 174, 217],
        },
    },
    # copper and verdigris: burnished metal gone green with age
    "copper": {
        "base": {1: 167, 2: 79, 3: 179, 4: 73, 5: 174, 6: 79, 7: 224},
        "grey": 138, "key": 79, "head": 179,
        "border": {"spot": 216, "contract": 73, "ladder": 174, "book": 79, "variants": 180, "log": 109,
                   "gate": 216, "reports": 224, "settings": 116, "themes": 216, "help": 187},
        "fill": 52, "tab": 130, "tab_text": 231,
        "sym": 179, "event": 79, "unit": 138,
        "word_a": 187, "word_b": 116, "word_c": 180,
        "ticker": 73, "strike": 179, "slot": 173, "verb": 174, "variant": 79, "phrase": 187, "sep": 95,
        "value": 224,
        "rows": [224, 223, 216, 179, 173, 173, 131, 66, 73, 79],
        "fade_txt": [224, 224, 224, 224, 187, 187, 181, 144, 144, 138],
        "fade_ts": [179, 173, 173, 138, 109, 79, 73, 72, 66, 241],
        "grads": {
            "spot": [234, 235, 237, 130, 173, 173, 216, 223],
            "yes": [73, 73, 115, 180, 179, 173, 167, 167],
            "eq_up": [236, 23, 23, 30, 72, 79, 116, 158],
            "eq_dn": [52, 52, 88, 131, 131, 167, 174, 217],
        },
    },
    # day trader: a bright morning desk, sky blue and sunshine with money green
    "day trader": {
        "base": {1: 203, 2: 77, 3: 221, 4: 75, 5: 141, 6: 81, 7: 231},
        "grey": 152, "key": 221, "head": 81,
        "border": {"spot": 74, "contract": 221, "ladder": 77, "book": 209, "variants": 81, "log": 153,
                   "gate": 141, "reports": 151, "settings": 215, "themes": 152, "help": 186},
        "fill": 17, "tab": 25, "tab_text": 255,
        "sym": 221, "event": 75, "unit": 152,
        "word_a": 195, "word_b": 222, "word_c": 151,
        "ticker": 75, "strike": 221, "slot": 209, "verb": 141, "variant": 81, "phrase": 195, "sep": 103,
        "value": 231,
        "rows": [231, 230, 222, 221, 215, 186, 81, 75, 74, 77],
        "fade_txt": [231, 231, 231, 231, 255, 255, 189, 253, 152, 152],
        "fade_ts": [221, 215, 215, 180, 116, 81, 75, 75, 68, 67],
        "grads": {
            "spot": [24, 25, 32, 75, 75, 81, 152, 229],
            "yes": [75, 81, 116, 186, 221, 215, 209, 203],
            "eq_up": [22, 22, 29, 35, 41, 77, 84, 158],
            "eq_dn": [52, 88, 124, 160, 167, 203, 210, 217],
        },
    },
    # night trader: after-hours navy, moonlight and glowing green tickers
    "night trader": {
        "base": {1: 203, 2: 84, 3: 215, 4: 69, 5: 141, 6: 81, 7: 189},
        "grey": 103, "key": 84, "head": 81,
        "border": {"spot": 146, "contract": 81, "ladder": 78, "book": 146, "variants": 84, "log": 110,
                   "gate": 80, "reports": 189, "settings": 117, "themes": 146, "help": 159},
        "fill": 17, "tab": 61, "tab_text": 231,
        "sym": 84, "event": 81, "unit": 103,
        "word_a": 146, "word_b": 147, "word_c": 110,
        "ticker": 81, "strike": 215, "slot": 84, "verb": 141, "variant": 69, "phrase": 146, "sep": 60,
        "value": 189,
        "rows": [189, 189, 147, 111, 111, 69, 75, 81, 80, 84],
        "fade_txt": [189, 189, 189, 189, 153, 146, 146, 110, 103, 103],
        "fade_ts": [84, 85, 81, 75, 69, 69, 62, 61, 60, 60],
        "grads": {
            "spot": [234, 235, 237, 24, 61, 81, 117, 159],
            "yes": [69, 75, 81, 85, 78, 179, 209, 203],
            "eq_up": [235, 22, 23, 29, 35, 84, 85, 158],
            "eq_dn": [52, 52, 88, 125, 167, 203, 210, 217],
        },
    },
    # candy cane: red and white stripes with a peppermint-green stem
    "candy cane": {
        "base": {1: 197, 2: 85, 3: 229, 4: 204, 5: 211, 6: 158, 7: 231},
        "grey": 181, "key": 85, "head": 231,
        "border": {"spot": 204, "contract": 231, "ladder": 210, "book": 217, "variants": 85, "log": 210,
                   "gate": 35, "reports": 204, "settings": 35, "themes": 210, "help": 122},
        "fill": 52, "tab": 124, "tab_text": 224,
        "sym": 197, "event": 231, "unit": 181,
        "word_a": 231, "word_b": 204, "word_c": 158,
        "ticker": 204, "strike": 231, "slot": 197, "verb": 217, "variant": 85, "phrase": 224, "sep": 138,
        "value": 231,
        "rows": [231, 196, 231, 196, 231, 196, 231, 196, 231, 196],
        "fade_txt": [231, 231, 231, 255, 255, 224, 224, 224, 181, 181],
        "fade_ts": [197, 210, 255, 217, 203, 203, 217, 255, 217, 204],
        "grads": {
            "spot": [196, 231, 196, 231, 196, 231, 196, 231],
            "yes": [158, 121, 121, 194, 224, 210, 203, 197],
            "eq_up": [236, 23, 29, 35, 78, 85, 122, 195],
            "eq_dn": [52, 88, 124, 160, 160, 197, 203, 217],
        },
    },
    # green tea: matcha, cream and a roasted-hojicha brown
    "green tea": {
        "base": {1: 167, 2: 149, 3: 186, 4: 108, 5: 248, 6: 151, 7: 255},
        "grey": 144, "key": 179, "head": 149,
        "border": {"spot": 149, "contract": 187, "ladder": 230, "book": 149, "variants": 187, "log": 180,
                   "gate": 193, "reports": 186, "settings": 230, "themes": 179, "help": 187},
        "fill": 22, "tab": 65, "tab_text": 231,
        "sym": 149, "event": 179, "unit": 144,
        "word_a": 187, "word_b": 180, "word_c": 151,
        "ticker": 108, "strike": 186, "slot": 179, "verb": 144, "variant": 149, "phrase": 187, "sep": 101,
        "value": 230,
        "rows": [230, 230, 193, 187, 150, 149, 107, 143, 137, 137],
        "fade_txt": [230, 230, 230, 254, 253, 187, 187, 151, 249, 144],
        "fade_ts": [179, 179, 149, 149, 108, 108, 65, 65, 65, 241],
        "grads": {
            "spot": [234, 236, 238, 65, 107, 149, 186, 230],
            "yes": [65, 108, 108, 149, 149, 179, 173, 167],
            "eq_up": [235, 22, 58, 64, 107, 149, 186, 193],
            "eq_dn": [52, 236, 94, 95, 131, 167, 174, 217],
        },
    },
    # psychedelic: every hue at once, spinning
    "psychedelic": {
        "base": {1: 203, 2: 83, 3: 227, 4: 69, 5: 207, 6: 87, 7: 231},
        "grey": 183, "key": 227, "head": 207,
        "border": {"spot": 203, "contract": 215, "ladder": 227, "book": 83, "variants": 75, "log": 69,
                   "gate": 85, "reports": 219, "settings": 87, "themes": 69, "help": 205},
        "fill": 54, "tab": 91, "tab_text": 253,
        "sym": 207, "event": 87, "unit": 183,
        "word_a": 219, "word_b": 157, "word_c": 153,
        "ticker": 87, "strike": 227, "slot": 209, "verb": 207, "variant": 83, "phrase": 219, "sep": 134,
        "value": 231,
        "rows": [203, 215, 227, 119, 84, 80, 69, 135, 207, 203],
        "fade_txt": [231, 225, 225, 225, 195, 195, 230, 230, 253, 183],
        "fade_ts": [227, 155, 83, 85, 81, 75, 99, 171, 205, 203],
        "grads": {
            "spot": [207, 99, 63, 81, 85, 119, 227, 209],
            "yes": [87, 84, 83, 191, 221, 209, 204, 207],
            "eq_up": [23, 29, 35, 41, 83, 119, 191, 227],
            "eq_dn": [53, 89, 125, 161, 198, 206, 207, 219],
        },
    },
    # alien: UFO-beam green, flying-saucer silver and nebula violet
    "alien": {
        "base": {1: 204, 2: 119, 3: 191, 4: 69, 5: 135, 6: 85, 7: 194},
        "grey": 109, "key": 119, "head": 85,
        "border": {"spot": 85, "contract": 135, "ladder": 119, "book": 250, "variants": 77, "log": 69,
                   "gate": 122, "reports": 68, "settings": 152, "themes": 183, "help": 122},
        "fill": 22, "tab": 29, "tab_text": 231,
        "sym": 119, "event": 85, "unit": 109,
        "word_a": 194, "word_b": 183, "word_c": 252,
        "ticker": 85, "strike": 191, "slot": 119, "verb": 135, "variant": 250, "phrase": 194, "sep": 65,
        "value": 255,
        "rows": [255, 253, 152, 157, 119, 83, 85, 68, 98, 135],
        "fade_txt": [255, 255, 255, 194, 194, 253, 152, 151, 151, 109],
        "fade_ts": [191, 155, 119, 83, 85, 80, 68, 98, 61, 60],
        "grads": {
            "spot": [234, 235, 237, 23, 36, 85, 122, 194],
            "yes": [135, 99, 69, 80, 84, 83, 155, 191],
            "eq_up": [235, 22, 22, 64, 70, 83, 155, 193],
            "eq_dn": [235, 53, 89, 125, 168, 204, 211, 218],
        },
    },
}


NAMES = list(THEMES)
DEFAULT = "classic"


def get(name: str | None) -> dict:
    return THEMES.get(name or DEFAULT, THEMES[DEFAULT])


def next_name(name: str | None, step: int = 1) -> str:
    """The theme after `name` (step 1, the t key) or before it (step -1, T)."""
    i = NAMES.index(name) if name in THEMES else (-1 if step > 0 else 0)
    return NAMES[(i + step) % len(NAMES)]


# ---------------------------------------------------------------- the 256 colors

BACK = 234                                        # about the background these are for
CUBE = (0, 95, 135, 175, 215, 255)


def rgb(i: int) -> tuple[int, int, int]:
    """A 256-color index, 16 and up (the first 16 are whatever the terminal says they are)."""
    if i >= 232:
        return (8 + 10 * (i - 232),) * 3
    n = i - 16
    return CUBE[n // 36], CUBE[n // 6 % 6], CUBE[n % 6]


def _linear(c: int) -> float:
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _light(c: tuple) -> float:
    r, g, b = map(_linear, c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: int, b: int) -> float:
    """How far apart two colors are in lightness, 1 (the same) to 21 (black and white)."""
    x, y = sorted((_light(rgb(a)), _light(rgb(b))))
    return (y + 0.05) / (x + 0.05)


def _lab(c: tuple) -> tuple[float, float, float]:
    r, g, b = map(_linear, c)
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    x, y, z = f((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.9505), f(0.2126 * r + 0.7152 * g + 0.0722 * b), \
        f((0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.089)
    return 116 * y - 16, 500 * (x - y), 200 * (y - z)


LAB = {i: _lab(rgb(i)) for i in range(16, 256)}


def distance(a: int, b: int) -> float:
    """How different two colors look: 2 is barely, 10 is plainly."""
    return dist(LAB[a], LAB[b])


def _nearest(c: tuple) -> int:
    at = _lab(c)
    return min(LAB, key=lambda i: dist(LAB[i], at))


@lru_cache(maxsize=None)
def readable(fg: int, bg: int, least: float = 4.0) -> int:
    """fg, or the color nearest to it that can be read on bg."""
    if fg < 16 or bg < 16 or contrast(fg, bg) >= least:
        return fg
    return min((i for i in range(16, 256) if contrast(i, bg) >= least), key=lambda i: distance(i, fg))


@lru_cache(maxsize=None)
def visible(ramp: tuple) -> tuple:
    """A chart's gradient without the steps too dark to see on a dark background."""
    return tuple(c for c in ramp if c < 16 or contrast(c, BACK) >= 1.5) or ramp


@lru_cache(maxsize=None)
def _between(a: int, b: int, depth: int) -> tuple:
    """The colors to show between a and b: the one nearest to halfway, where there is one that lies between them."""
    if not depth or a < 16 or b < 16 or a == b:
        return ()
    d = distance(a, b)
    c = _nearest(tuple((p + q) / 2 for p, q in zip(rgb(a), rgb(b))))
    if c in (a, b) or max(distance(a, c), distance(c, b)) > 0.85 * d or distance(a, c) + distance(c, b) > 1.35 * d:
        return ()
    return _between(a, c, depth - 1) + (c,) + _between(c, b, depth - 1)


@lru_cache(maxsize=None)
def smooth(ramp: tuple) -> tuple:
    """A gradient with the 256-color terminal's own in-between colors put in."""
    out = [ramp[0]]
    for a, b in zip(ramp, ramp[1:]):
        out += [*_between(a, b, 2), b]
    return tuple(out)
