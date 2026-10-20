"""Minimal CLI; live upload is unavailable until its contracts are approved."""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path
from typing import Sequence

from .input_parser import parse_declaration_folder
from .live_browser import LoginCredentials, run_login_check


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sana-declaration-uploader")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("source_dir", type=Path)
    login_parser = subparsers.add_parser(
        "login-check", help="ورود آزمایشی بدون ثبت یا تغییر اطلاعات"
    )
    login_parser.add_argument("--headless", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect":
        parse_declaration_folder(args.source_dir)
        return 0
    if args.command == "login-check":
        credentials = LoginCredentials(
            national_code=getpass.getpass("کد ملی: "),
            password=getpass.getpass("رمز شخصی: "),
            legal_identity=getpass.getpass("شناسه حقوقی: "),
            otp=getpass.getpass("رمز موقت: "),
        )
        run_login_check(credentials, headless=args.headless)
        print("ورود خودکاربری با موفقیت تأیید شد؛ هیچ اطلاعاتی ثبت نشد.")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
