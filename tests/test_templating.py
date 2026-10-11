from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sana_declaration_uploader.models import DeclarationRequest
from sana_declaration_uploader.templating import (
    DescriptionRenderError,
    render_request,
)


class TemplatingTests(unittest.TestCase):
    def test_notice_fields_can_be_resolved_before_render(self) -> None:
        with TemporaryDirectory() as directory:
            request = DeclarationRequest(
                source_dir=Path(directory),
                recipient_id="r1",
                fields={"notice_number": "N-1"},
                description_template=(
                    "{{recipient.full_name}} / {{notice.number}}"
                ),
                parameters={},
            )

            draft = render_request(
                request,
                {
                    "recipient_first_name": "نام",
                    "recipient_last_name": "نمونه",
                },
            )

            self.assertEqual(
                draft.fields["declaration_text"], "نام نمونه / N-1"
            )

    def test_missing_parameter_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            request = DeclarationRequest(
                source_dir=Path(directory),
                recipient_id="r1",
                fields={},
                description_template="مبلغ {{param.amount}}",
                parameters={},
            )

            with self.assertRaisesRegex(
                DescriptionRenderError, "placeholder_unresolved:param.amount"
            ):
                render_request(request)


if __name__ == "__main__":
    unittest.main()
