"""Finishing a job: completion, two-sided cash confirmation and reviews."""

from unittest.mock import patch

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import (
    Customer,
    Request,
    Review,
    Specialty,
    Technician,
    TechnicianWallet,
    User,
    WalletTransaction,
)

PASSWORD = 'TesterPass123'


class CompletionFlowTests(APITestCase):
    def setUp(self):
        cache.clear()
        telegram = patch('core.services.notifications.send_telegram', return_value=True)
        telegram.start()
        self.addCleanup(telegram.stop)
        Specialty.objects.create(key='dish_install', label='تركيب دش')
        self.customer_user = User.objects.create(
            email='done-customer@example.com',
            phone='+201000000501',
            password_hash=hash_password(PASSWORD),
            role='customer',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user, name='عميل الإنهاء', phone='+201000000501'
        )
        self.tech_user = User.objects.create(
            email='done-tech@example.com',
            phone='+201000000502',
            password_hash=hash_password(PASSWORD),
            role='technician',
        )
        self.technician = Technician.objects.create(
            user=self.tech_user,
            name='فني الإنهاء',
            phone='+201000000502',
            specialty='dish_install',
            availability='available',
            approval_status='approved',
        )

    def as_customer(self):
        self.client.force_authenticate(user=self.customer_user)

    def as_technician(self):
        self.client.force_authenticate(user=self.tech_user)

    def started_job(self):
        """Order -> accepted -> started with the customer's code."""
        self.as_customer()
        request_id = self.client.post(
            '/api/customer/requests',
            {'specialty': 'dish_install', 'description': 'الدش مش شغال', 'area_key': 'smouha'},
            format='json',
        ).json()['request']['id']
        self.as_technician()
        offer = self.client.get('/api/technician/incoming').json()['requests'][0]
        self.client.post(f"/api/technician/broadcasts/{offer['broadcast_id']}/accept")
        pin = Request.objects.get(pk=request_id).start_pin
        started = self.client.post(f'/api/technician/requests/{request_id}/start', {'pin': pin})
        self.assertEqual(started.status_code, status.HTTP_200_OK)
        return request_id

    def complete(self, request_id, notes='ركبت الدش وظبطت الإشارة'):
        self.as_technician()
        return self.client.post(
            f'/api/technician/requests/{request_id}/complete',
            {'work_notes': notes},
            format='json',
        )

    def report(self, who, request_id, amount):
        (self.as_technician if who == 'technician' else self.as_customer)()
        prefix = 'technician' if who == 'technician' else 'customer'
        return self.client.post(
            f'/api/{prefix}/requests/{request_id}/report-payment',
            {'amount': amount},
            format='json',
        )

    def test_complete_requires_notes_and_frees_the_technician(self):
        request_id = self.started_job()
        self.assertEqual(self.complete(request_id, notes='').status_code, 400)

        done = self.complete(request_id)
        self.assertEqual(done.status_code, status.HTTP_200_OK)
        job = Request.objects.get(pk=request_id)
        self.assertEqual(job.status, 'completed')
        self.assertEqual(job.work_notes, 'ركبت الدش وظبطت الإشارة')
        self.assertIsNotNone(job.completed_at)
        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'available')

        # Still on the active list until the cash amount is reported.
        active = self.client.get('/api/technician/requests').json()['requests']
        self.assertEqual([r['id'] for r in active], [request_id])

    def test_only_an_in_progress_job_can_be_completed(self):
        self.as_customer()
        job = Request.objects.create(
            customer=self.customer, technician=self.technician,
            specialty='dish_install', description='x', status='assigned',
        )
        self.assertEqual(self.complete(job.pk).status_code, 400)

    def test_matching_reports_book_the_commission_once(self):
        request_id = self.started_job()
        self.complete(request_id)

        before = self.report('customer', request_id, 300)
        self.assertEqual(before.json()['status'], 'pending')
        self.assertEqual(before.json()['waiting_on'], 'technician')

        matched = self.report('technician', request_id, 300)
        self.assertEqual(matched.status_code, status.HTTP_200_OK)
        body = matched.json()
        self.assertEqual(body['status'], 'matched')
        self.assertEqual(body['final_amount'], 300)
        self.assertEqual(body['commission'], 60)  # default 20 %
        self.assertEqual(body['total_debt'], 60)

        job = Request.objects.get(pk=request_id)
        self.assertEqual(job.amount_status, 'matched')
        self.assertEqual(job.payment_status, 'confirmed')
        wallet = TechnicianWallet.objects.get(technician=self.technician)
        self.assertEqual(wallet.total_debt, 60)
        self.assertEqual(
            WalletTransaction.objects.filter(request_id=request_id, type='debt').count(), 1
        )

        # A second report on a settled request is refused.
        again = self.report('customer', request_id, 300)
        self.assertEqual(again.status_code, status.HTTP_409_CONFLICT)
        # The job leaves the technician's active list once it is settled.
        self.as_technician()
        self.assertEqual(self.client.get('/api/technician/requests').json()['requests'], [])

    def test_different_amounts_are_disputed_without_commission(self):
        request_id = self.started_job()
        self.complete(request_id)
        self.report('technician', request_id, 200)
        disputed = self.report('customer', request_id, 300)
        self.assertEqual(disputed.json()['status'], 'disputed')
        self.assertEqual(Request.objects.get(pk=request_id).amount_status, 'disputed')
        self.assertFalse(WalletTransaction.objects.filter(request_id=request_id).exists())

    def test_report_before_completion_and_bad_amounts_are_rejected(self):
        request_id = self.started_job()
        self.assertEqual(self.report('technician', request_id, 100).status_code, 400)
        self.complete(request_id)
        self.assertEqual(self.report('technician', request_id, 0).status_code, 400)
        self.assertEqual(self.report('technician', request_id, 'abc').status_code, 400)
        self.assertEqual(self.report('technician', request_id, 100001).status_code, 400)

    def test_review_flow_and_pending_actions(self):
        request_id = self.started_job()
        self.as_customer()
        early = self.client.post(
            f'/api/customer/requests/{request_id}/review', {'rating': 5}, format='json'
        )
        self.assertEqual(early.status_code, 400)

        self.complete(request_id)
        self.as_customer()
        pending = self.client.get('/api/customer/requests/pending-actions').json()
        self.assertEqual(pending['count'], 1)
        self.assertEqual(pending['requests'][0]['specialty_label'], 'تركيب دش')
        self.assertEqual(pending['requests'][0]['technician_name'], 'فني الإنهاء')
        self.assertEqual(self.client.get('/api/customer/pending-reviews').json()['count'], 1)

        bad = self.client.post(
            f'/api/customer/requests/{request_id}/review', {'rating': 6}, format='json'
        )
        self.assertEqual(bad.status_code, 400)
        made = self.client.post(
            f'/api/customer/requests/{request_id}/review',
            {'rating': 5, 'comment': 'فني ممتاز'},
            format='json',
        )
        self.assertEqual(made.status_code, status.HTTP_201_CREATED)
        self.assertEqual(made.json()['review']['request_id'], request_id)
        twice = self.client.post(
            f'/api/customer/requests/{request_id}/review', {'rating': 4}, format='json'
        )
        self.assertEqual(twice.status_code, 400)

        self.assertEqual(self.client.get('/api/customer/requests/pending-actions').json()['count'], 0)
        mine = self.client.get('/api/customer/my-reviews').json()['reviews']
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]['technician_name'], 'فني الإنهاء')

        # The technician replies once.
        review_id = Review.objects.get(request_id=request_id).pk
        self.as_technician()
        reply = self.client.post(
            f'/api/technician/reviews/{review_id}/reply', {'reply': 'شكراً ليك'}, format='json'
        )
        self.assertEqual(reply.status_code, status.HTTP_200_OK)
        self.assertEqual(reply.json()['review']['technician_reply'], 'شكراً ليك')
        second = self.client.post(
            f'/api/technician/reviews/{review_id}/reply', {'reply': 'تاني'}, format='json'
        )
        self.assertEqual(second.status_code, 400)

    def test_cash_declaration(self):
        request_id = self.started_job()
        self.as_customer()
        early = self.client.post(
            f'/api/customer/requests/{request_id}/pay', {'method': 'cash'}, format='json'
        )
        self.assertEqual(early.status_code, 400)
        self.complete(request_id)
        self.as_customer()
        wrong = self.client.post(
            f'/api/customer/requests/{request_id}/pay', {'method': 'instapay'}, format='json'
        )
        self.assertEqual(wrong.status_code, 400)
        paid = self.client.post(
            f'/api/customer/requests/{request_id}/pay', {'method': 'cash'}, format='json'
        )
        self.assertEqual(paid.status_code, status.HTTP_200_OK)
        self.assertEqual(Request.objects.get(pk=request_id).payment_status, 'pending_verification')

    def test_other_users_cannot_touch_the_job(self):
        request_id = self.started_job()
        other_tech = User.objects.create(
            email='done-other-tech@example.com', phone='+201000000503',
            password_hash='x', role='technician',
        )
        Technician.objects.create(
            user=other_tech, name='فني تاني', phone='+201000000503',
            specialty='dish_install', availability='available', approval_status='approved',
        )
        self.client.force_authenticate(user=other_tech)
        response = self.client.post(
            f'/api/technician/requests/{request_id}/complete',
            {'work_notes': 'محاولة من فني تاني'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        other_customer = User.objects.create(
            email='done-other@example.com', phone='+201000000504',
            password_hash='x', role='customer',
        )
        Customer.objects.create(user=other_customer, name='Other', phone='+201000000504')
        self.client.force_authenticate(user=other_customer)
        response = self.client.post(
            f'/api/customer/requests/{request_id}/report-payment', {'amount': 10}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
