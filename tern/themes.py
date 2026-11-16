"""Color schemes for the screen. `t` on the screen steps to the next one, `T` back, in this order.

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
    # Christmas: snow white, berry red and holly green, with gold only on the key hints
    "christmas": {
        "base": {1: 196, 2: 40, 3: 231, 4: 28, 5: 160, 6: 71, 7: 255},
        "grey": 188, "key": 220, "head": 255,
        "border": {"spot": 160, "contract": 34, "ladder": 255, "book": 28, "equity": 196, "log": 255},
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
    # Valentine's Day: roses, blush pinks, and a little chocolate
    "valentine": {
        "base": {1: 160, 2: 211, 3: 223, 4: 132, 5: 199, 6: 217, 7: 225},
        "grey": 181, "key": 197, "head": 211,
        "border": {"spot": 197, "contract": 211, "ladder": 161, "book": 218, "equity": 168, "log": 125},
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
        "border": {"spot": 34, "contract": 220, "ladder": 40, "book": 178, "equity": 28, "log": 76},
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
        "base": {1: 160, 2: 215, 3: 223, 4: 124, 5: 168, 6: 203, 7: 224},
        "grey": 131, "key": 196, "head": 203,
        "border": {"spot": 196, "contract": 160, "ladder": 124, "book": 167, "equity": 88, "log": 131},
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
        "border": {"spot": 214, "contract": 208, "ladder": 178, "book": 172, "equity": 136, "log": 130},
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
        "base": {1: 160, 2: 106, 3: 214, 4: 66, 5: 131, 6: 179, 7: 230},
        "grey": 138, "key": 208, "head": 179,
        "border": {"spot": 166, "contract": 178, "ladder": 130, "book": 124, "equity": 100, "log": 94},
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
        "border": {"spot": 196, "contract": 255, "ladder": 27, "book": 160, "equity": 33, "log": 252},
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
        "border": {"spot": 48, "contract": 135, "ladder": 43, "book": 171, "equity": 37, "log": 99},
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
        "border": {"spot": 196, "contract": 208, "ladder": 226, "book": 46, "equity": 33, "log": 129},
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
        "border": {"spot": 51, "contract": 45, "ladder": 75, "book": 105, "equity": 135, "log": 201},
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
        "base": {1: 160, 2: 227, 3: 229, 4: 130, 5: 166, 6: 214, 7: 230},
        "grey": 180, "key": 231, "head": 220,
        "border": {"spot": 124, "contract": 160, "ladder": 202, "book": 208, "equity": 214, "log": 220},
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
        "border": {"spot": 45, "contract": 39, "ladder": 105, "book": 168, "equity": 202, "log": 196},
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
        "border": {"spot": 63, "contract": 99, "ladder": 61, "book": 135, "equity": 69, "log": 171},
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
        "border": {"spot": 121, "contract": 137, "ladder": 115, "book": 94, "equity": 79, "log": 130},
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
        "border": {"spot": 137, "contract": 173, "ladder": 130, "book": 180, "equity": 94, "log": 136},
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
        "border": {"spot": 213, "contract": 117, "ladder": 219, "book": 159, "equity": 207, "log": 153},
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
        "border": {"spot": 218, "contract": 151, "ladder": 225, "book": 175, "equity": 150, "log": 182},
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
        "border": {"spot": 173, "contract": 80, "ladder": 180, "book": 108, "equity": 137, "log": 73},
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
        "border": {"spot": 44, "contract": 214, "ladder": 211, "book": 41, "equity": 38, "log": 209},
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
        "border": {"spot": 61, "contract": 103, "ladder": 67, "book": 146, "equity": 60, "log": 110},
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
        "border": {"spot": 129, "contract": 165, "ladder": 98, "book": 135, "equity": 92, "log": 171},
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
        "border": {"spot": 215, "contract": 211, "ladder": 191, "book": 216, "equity": 204, "log": 155},
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
        "border": {"spot": 255, "contract": 252, "ladder": 249, "book": 246, "equity": 243, "log": 240},
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
        "border": {"spot": 29, "contract": 64, "ladder": 71, "book": 101, "equity": 22, "log": 65},
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
}


NAMES = list(THEMES)
DEFAULT = "classic"


def get(name: str | None) -> dict:
    return THEMES.get(name or DEFAULT, THEMES[DEFAULT])


def next_name(name: str | None, step: int = 1) -> str:
    """The theme after `name` (step 1, the t key) or before it (step -1, T)."""
    i = NAMES.index(name) if name in THEMES else (-1 if step > 0 else 0)
    return NAMES[(i + step) % len(NAMES)]
