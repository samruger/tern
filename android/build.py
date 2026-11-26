#!/usr/bin/env python3
"""Builds the Android app: python3 android/build.py [--home ADDRESS] [--outside ADDRESS] [--blank] [--out FILE]

Needs a JDK (17 or newer) and, in $ANDROID_HOME (~/.local/share/android-sdk without it), a platform and build-tools from
Google. No Gradle. The apk gets this desk's addresses and its certificate's fingerprint, so the phone has nothing to ask:
keep that apk to yourself, or build one with --blank. The addresses given are kept in ~/.config/tern/android.json, and the
signing key is ~/.config/tern/android.jks, made the first time. Android takes an update only with the same key.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
CONF = os.path.expanduser("~/.config/tern")
KEPT, KEY, KEY_PASS = (os.path.join(CONF, n) for n in ("android.json", "android.jks", "android.pass"))
MIN, TARGET, VERSION = 29, 36, "1.1"


def run(*cmd: str, **kw) -> str:
    p = subprocess.run(cmd, text=True, capture_output=True, **kw)
    if p.returncode:
        sys.exit(f"{os.path.basename(cmd[0])} failed:\n{p.stdout}{p.stderr}")
    return p.stdout


def newest(pattern: str) -> str:
    found = sorted(glob.glob(pattern), key=lambda p: [int(n) for n in re.findall(r"\d+", os.path.basename(p))])
    if not found:
        sys.exit(f"nothing at {pattern}: install the Android SDK's platform and build-tools, or set ANDROID_HOME")
    return found[-1]


def private(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        fh.write(text)


def preset(home: str | None, outside: str | None) -> dict:
    """The addresses: those given, else those given before."""
    from tern import webauth
    try:
        with open(KEPT) as fh:
            kept = json.load(fh)
    except (OSError, ValueError):
        kept = {}
    for k, v in (("home", home), ("outside", outside)):
        if v is not None:
            kept[k] = v
    if home is not None or outside is not None:
        private(KEPT, json.dumps(kept) + "\n")
    out = {"home": kept.get("home", ""), "outside": kept.get("outside", "")}
    if os.path.exists(webauth.CERT_FILE):
        out["pin"] = webauth.fingerprint()
    return out


def signing_key() -> list[str]:
    if not os.path.exists(KEY):
        private(KEY_PASS, secrets.token_urlsafe(24) + "\n")
        run("keytool", "-genkeypair", "-keystore", KEY, "-storetype", "PKCS12", "-storepass:file", KEY_PASS, "-alias", "tern", "-keyalg", "RSA",
            "-keysize", "4096", "-validity", "18250", "-dname", "CN=tern")
        os.chmod(KEY, 0o600)
        print(f"made a signing key: {KEY} and {KEY_PASS} (keep copies: an update needs the same key)")
    return ["--ks", KEY, "--ks-pass", f"file:{KEY_PASS}", "--ks-key-alias", "tern"]


def build(out: str, values: dict) -> None:
    sdk = os.environ.get("ANDROID_HOME") or os.path.expanduser("~/.local/share/android-sdk")
    tools, jar = newest(sdk + "/build-tools/*"), os.path.join(newest(sdk + "/platforms/android-*"), "android.jar")
    tool = lambda n: os.path.join(tools, n)
    with tempfile.TemporaryDirectory() as t:
        p = lambda *n: os.path.join(t, *n)
        for d in ("assets", "gen", "classes", "dex"):
            os.mkdir(p(d))
        if any(values.values()):
            with open(p("assets", "preset.properties"), "w") as fh:
                fh.writelines(f"{k}={v}\n".replace("\\", "") for k, v in values.items())
        run(tool("aapt2"), "compile", "--dir", os.path.join(HERE, "res"), "-o", p("res.zip"))
        run(tool("aapt2"), "link", "-o", p("base.apk"), "-I", jar, "--manifest", os.path.join(HERE, "AndroidManifest.xml"), "--java", p("gen"), "-A", p("assets"),
            "--min-sdk-version", str(MIN), "--target-sdk-version", str(TARGET), "--version-name", VERSION,
            "--version-code", str(int((time.time() - 1767225600) // 60)), p("res.zip"))       # minutes since 2026: each build is newer than the last
        java = sorted(glob.glob(os.path.join(HERE, "src", "tern", "desk", "*.java"))) + glob.glob(p("gen", "**", "*.java"), recursive=True)
        run("javac", "--release", "17", "-XDstringConcat=inline", "-Xlint:all,-options,-classfile,-serial,-this-escape", "-Werror", "-encoding", "UTF-8", "-cp", jar, "-d", p("classes"), *java)
        run(tool("d8"), "--release", "--min-api", str(MIN), "--lib", jar, "--output", p("dex"), *glob.glob(p("classes", "**", "*.class"), recursive=True))
        with zipfile.ZipFile(p("base.apk"), "a", zipfile.ZIP_DEFLATED) as z:
            z.write(p("dex", "classes.dex"), "classes.dex")
        run(tool("zipalign"), "-f", "4", p("base.apk"), p("aligned.apk"))
        run(tool("apksigner"), "sign", *signing_key(), "--out", p("tern.apk"), p("aligned.apk"))
        run(tool("apksigner"), "verify", "--min-sdk-version", str(MIN), p("tern.apk"))
        shutil.copyfile(p("tern.apk"), out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Builds the Android app.")
    ap.add_argument("--home", help="the desk's address at home, HOST or HOST:PORT")
    ap.add_argument("--outside", help="its address from outside, '' for none")
    ap.add_argument("--blank", action="store_true", help="no addresses in it: the phone asks")
    ap.add_argument("--out", default=os.path.expanduser("~/tern.apk"))
    a = ap.parse_args()
    values = {} if a.blank else preset(a.home, a.outside)
    build(a.out, values)
    print(f"wrote {a.out} ({os.path.getsize(a.out) // 1024} KB)" + "".join(f"\n  {k}: {v}" for k, v in values.items() if v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
