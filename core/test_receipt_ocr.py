"""Free on-server OCR of InstaPay receipts (services.receipt_ocr)."""

import io
import os
import unittest
from datetime import timedelta
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile

from .models import PaymentReceipt
from .services import instapay, receipt_ocr
from .test_instapay import InstapayBase, cairo_now_text, receipt_fields

CONFIG = {
    'enabled': True,
    'number': '01012345678',
    'name': 'Dashawy Platform',
    'address': 'dashawy@instapay',
    'link': '',
    'auto_max': 5000.0,
}


def rows(*lines):
    """OCR rows from plain text lines, one word per token."""
    out = []
    for y, line in enumerate(lines):
        out.append(
            [
                {'text': word, 'conf': 95.0, 'box': (x * 100, y * 60, x * 100 + 90, y * 60 + 40)}
                for x, word in enumerate(line.split())
            ]
        )
    return out


class UnderstandTests(unittest.TestCase):
    def test_english_receipt(self):
        found = receipt_ocr.understand(
            rows(
                '14:52 4G 87%',
                'Transaction Successful',
                '300.00 EGP',
                'Reference Number 412563987412',
                'Date 17 Nov 2026 02:31 PM',
                'From Ahmed S***',
                'ahmed.s@instapay',
                'To Dashawy P***',
                'dashawy@instapay',
                'Fees 0.00 EGP',
            )
        )
        self.assertEqual(found['status'], 'successful')
        self.assertEqual(found['amount'], '300.00')
        self.assertEqual(found['reference'], '412563987412')
        self.assertEqual(found['transferred_at'], '2026-11-17 14:31')
        self.assertEqual(found['recipient_accounts'], ['dashawy@instapay'])
        self.assertEqual(found['sender_accounts'], ['ahmed.s@instapay'])
        self.assertTrue(found['receipt_like'])

    def test_arabic_receipt_with_arabic_digits(self):
        found = receipt_ocr.understand(
            rows(
                'تمت العملية بنجاح',
                '٣٠٠٫٠٠ ج.م',
                'الرقم المرجعي ٤١٢٥٦٣٩٨٧٤١٢',
                'التاريخ ١٧ نوفمبر ٢٠٢٦ ٠٢:٣١ م',
                'من احمد',
                'إلى داشاوي',
                '010*****678',
            )
        )
        self.assertEqual(found['status'], 'successful')
        self.assertEqual(found['amount'], '300.00')
        self.assertEqual(found['reference'], '412563987412')
        self.assertEqual(found['transferred_at'], '2026-11-17 14:31')
        self.assertEqual(found['recipient_accounts'], ['010*****678'])

    def test_failure_wins_over_success_words(self):
        found = receipt_ocr.understand(rows('Transaction unsuccessful', '300 EGP'))
        self.assertEqual(found['status'], 'failed')

    def test_fees_and_balance_are_not_the_amount(self):
        found = receipt_ocr.understand(
            rows('Successful', '300 EGP', 'Fees 5.00 EGP', 'Balance 12,000.00 EGP')
        )
        self.assertEqual(found['amount'], '300.00')

    def test_two_different_amounts_are_left_unread(self):
        found = receipt_ocr.understand(rows('300 EGP', 'Amount 350 EGP'))
        self.assertEqual(found['amount'], '')

    def test_address_in_a_note_is_not_the_recipient(self):
        found = receipt_ocr.understand(
            rows('To Someone', 'other@instapay', 'Reference 412563987412', 'Note dashawy@instapay')
        )
        self.assertEqual(found['recipient_accounts'], ['other@instapay'])

    def test_status_bar_clock_is_not_the_transfer_time(self):
        found = receipt_ocr.understand(rows('14:52 4G', 'Date 17 Nov 2026'))
        self.assertEqual(found['transferred_at'], '')

    def test_status_from_a_heading_read_without_spaces(self):
        found = receipt_ocr.understand(rows('300 EGP'), headings=['تمتالعمليةبنجاج'])
        self.assertEqual(found['status'], 'successful')

    def test_reversed_arabic_words_are_turned_around(self):
        fixed = receipt_ocr._orient_arabic(rows('يعجرملا مقرلا 412563987412'))
        self.assertEqual(
            receipt_ocr.understand(fixed)['reference'], '412563987412'
        )

    def test_foreign_recipient_is_reported_over_ours(self):
        picked = receipt_ocr._pick_account(['dashawy@instapay', 'thief@instapay'], CONFIG)
        self.assertEqual(picked, 'thief@instapay')


def ocr_fields(**overrides):
    fields = receipt_fields(reader='ocr', word_count=30)
    fields.update(overrides)
    return fields


class OcrEvaluationTests(InstapayBase):
    """An OCR reading refuses only what it cannot have misread."""

    def test_clean_ocr_reading_confirms_payment(self):
        res = self.upload(ocr_fields(transferred_at=cairo_now_text()))
        self.assertEqual(res.data['receipt']['status'], 'verified', res.data)

    def test_amount_mismatch_waits_for_an_admin(self):
        res = self.upload(ocr_fields(amount='800.00', transferred_at=cairo_now_text()))
        self.assertEqual(res.data['receipt']['status'], 'review')

    def test_foreign_recipient_waits_for_an_admin(self):
        res = self.upload(
            ocr_fields(recipient_account='thief@instapay', transferred_at=cairo_now_text())
        )
        self.assertEqual(res.data['receipt']['status'], 'review')

    def test_unknown_status_waits_for_an_admin(self):
        res = self.upload(
            ocr_fields(transaction_status='unknown', transferred_at=cairo_now_text())
        )
        self.assertEqual(res.data['receipt']['status'], 'review')

    def test_reused_reference_is_still_refused(self):
        self.upload(ocr_fields(transferred_at=cairo_now_text()))
        other = self.make_second_job()
        res = self.upload(ocr_fields(transferred_at=cairo_now_text()), job=other)
        self.assertEqual(res.data['receipt']['status'], 'rejected')

    def test_unreadable_image_waits_instead_of_being_refused(self):
        res = self.upload(ocr_fields(legibility='unreadable', is_transfer_receipt=False, word_count=2))
        self.assertEqual(res.data['receipt']['status'], 'review')

    def test_a_readable_non_receipt_is_refused(self):
        res = self.upload(ocr_fields(is_transfer_receipt=False, word_count=40))
        self.assertEqual(res.data['receipt']['status'], 'rejected')

    def make_second_job(self):
        from .models import Request
        from .models import utc_now

        Request.objects.filter(pk=self.job.pk).update(status='completed')
        return Request.objects.create(
            customer=self.customer,
            technician=self.technician,
            specialty='dish_install',
            description='طلب تاني',
            status='in_progress',
            service_price=300,
            commission_percent=20,
            created_at=utc_now() - timedelta(hours=1),
        )


FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
FONT_BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'


def render_receipt(lines, amount):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new('RGB', (1080, 1600), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    label = ImageFont.truetype(FONT, 36)
    value = ImageFont.truetype(FONT_BOLD, 38)
    draw.text((540, 300), 'Transaction Successful', font=ImageFont.truetype(FONT_BOLD, 52), fill=(30, 30, 40), anchor='mm')
    draw.text((540, 420), amount, font=ImageFont.truetype(FONT_BOLD, 84), fill=(30, 30, 40), anchor='mm')
    y = 560
    for name, text in lines:
        draw.text((60, y), name, font=label, fill=(120, 120, 130))
        draw.text((1020, y), text, font=value, fill=(30, 30, 40), anchor='ra')
        y += 110
    out = io.BytesIO()
    image.save(out, 'PNG')
    return out.getvalue()


@unittest.skipUnless(
    receipt_ocr.available() and os.path.exists(FONT_BOLD), 'Tesseract or test fonts missing'
)
class RealOcrTests(InstapayBase):
    """The whole path with a rendered screenshot and the real OCR."""

    def test_rendered_receipt_is_read_and_confirmed(self):
        when = cairo_now_text(timedelta(minutes=-5))
        image = render_receipt(
            [
                ('Reference Number', '412563987412'),
                ('Date', when.replace('-', '/')),
                ('From', 'ahmed.s@instapay'),
                ('To', 'dashawy@instapay'),
                ('Fees', '0.00 EGP'),
            ],
            '300.00 EGP',
        )
        # Dates print as 2026/11/17 14:31; the reader takes Y/M/D too.
        read = receipt_ocr.read_receipt(image, CONFIG)
        self.assertEqual(read['amount'], '300.00', read['ocr_text'])
        self.assertEqual(read['reference'], '412563987412')
        self.assertEqual(read['recipient_account'], 'dashawy@instapay')
        self.assertEqual(read['transaction_status'], 'successful')
        self.assertEqual(read['transferred_at'], when)

        self.as_customer()
        with patch.object(instapay, 'ai_configured', return_value=False):
            res = self.client.post(
                f'/api/customer/requests/{self.job.pk}/instapay-receipt',
                {
                    'receipt': SimpleUploadedFile('r.png', image, content_type='image/png'),
                    'amount': '300',
                },
                format='multipart',
            )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['receipt']['status'], 'verified', res.data)
        self.assertEqual(PaymentReceipt.objects.get().reference, '412563987412')

    def test_a_blank_image_waits_for_an_admin(self):
        from PIL import Image

        out = io.BytesIO()
        Image.new('RGB', (800, 1200), (255, 255, 255)).save(out, 'PNG')
        self.assertEqual(receipt_ocr.read_receipt(out.getvalue(), CONFIG)['legibility'], 'unreadable')
