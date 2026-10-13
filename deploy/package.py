"""Create source-only archives for deploying STRYKE.BOT."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "deploy" / "stryke-bot-gcp.zip"
FILES = (
    "main.py",
    "requirements.txt",
    ".env.example",
    "README.md",
    "deploy/stryke-bot.service",
    "deploy/install-gcp.sh",
)
TERMUX_OUTPUT = ROOT / "deploy" / "stryke-bot-termux.zip"
WINDOWS_OUTPUT = ROOT / "deploy" / "stryke-bot-windows.zip"
TERMUX_FILES = (
    "main.py",
    "requirements.txt",
    ".env.example",
    "README.md",
    "deploy/install-termux.sh",
    "deploy/start-termux.sh",
    "deploy/boot-termux.sh",
)
WINDOWS_FILES = (
    "main.py",
    "requirements.txt",
    ".env.example",
    "README.md",
    "deploy/start-windows.bat",
)

with ZipFile(OUTPUT, "w", ZIP_DEFLATED) as archive:
    for relative in FILES:
        archive.write(ROOT / relative, arcname=f"stryke-bot/{relative}")

print(OUTPUT)

with ZipFile(TERMUX_OUTPUT, "w", ZIP_DEFLATED) as archive:
    for relative in TERMUX_FILES:
        archive.write(ROOT / relative, arcname=f"stryke-bot/{relative}")

print(TERMUX_OUTPUT)

with ZipFile(WINDOWS_OUTPUT, "w", ZIP_DEFLATED) as archive:
    for relative in WINDOWS_FILES:
        archive.write(ROOT / relative, arcname=f"stryke-bot/{relative}")

print(WINDOWS_OUTPUT)
