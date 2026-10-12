"""Minimal CLI; live upload is unavailable until its contracts are approved."""

from __future__ import annotations

import argparse
import getpass
from pathlib import Path
from typing import Sequence

from .input_parser import parse_declaration_folder
from .catalog_capture import promote_captured_catalogs, run_interactive_catalog_capture
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
    capture_parser = subparsers.add_parser(
        "capture-lookups",
        help="استخراج خواندنی کدهای مکان و واحد قضایی از صفحهٔ باز ثنا",
    )
    capture_parser.add_argument(
        "--output-dir", type=Path, default=Path("lookups") / "capture"
    )
    promote_parser = subparsers.add_parser(
        "promote-lookups",
        help="جایگزینی اتمیک lookupهای اصلی با بخش معتبر برداشت ثنا",
    )
    promote_parser.add_argument(
        "--capture-dir", type=Path, default=Path("lookups") / "capture"
    )
    promote_parser.add_argument(
        "--lookup-dir", type=Path, default=Path("lookups")
    )
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
    if args.command == "capture-lookups":
        print(
            "در پنجرهٔ Chrome وارد ثنا شوید و فقط تا صفحهٔ دارای کامبوهای "
            "استان، شهرستان و شهر پیش بروید. استخراج خودکار از همان‌جا آغاز می‌شود."
        )
        counts = run_interactive_catalog_capture(args.output_dir)
        print(
            f"استخراج تمام شد: {counts['locations']} مکان و "
            f"{counts['court_units']} واحد قضایی؛ "
            f"{counts['unresolved_branches']} شاخهٔ بدون شهر در {args.output_dir}"
        )
        return 0
    if args.command == "promote-lookups":
        counts = promote_captured_catalogs(args.capture_dir, args.lookup_dir)
        print(
            f"ارتقای مکان‌ها انجام شد: {counts['locations']} مکان؛ "
            f"{counts['court_units']} واحد قضایی قبلی حفظ شد و "
            f"{counts['excluded_branches']} شاخهٔ مسئله‌دار کنار گذاشته شد."
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
