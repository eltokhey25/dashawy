"""Admin endpoints ported from the Node backend, and their section permissions."""

import json
from unittest.mock import patch

from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import (
    Customer,
    DataSubjectRequest,
    Request,
    RequestBroadcast,
    Review,
    ServicePricing,
    Setting,
    Specialty,
    Technician,
    TechnicianWallet,
    User,
    WalletTransaction,
    WithdrawalRequest,
)
from .services.payments import add_wallet_transaction

PASSWORD = 'TesterPass123'


class AdminBase(APITestCase):
    def setUp(self):
        cache.clear()
        telegram = patch('core.services.notifications.send_telegram', return_value=True)
        telegram.start()
        self.addCleanup(telegram.stop)
        Specialty.objects.create(key='dish_install', label='تركيب دش', sort_order=1)
        self.super_admin = self.user('super', 'admin', is_super_admin=True)
        self.limited_admin = self.user('limited', 'admin', permissions=json.dumps(['reviews']))
        self.customer_user = self.user('cust', 'customer', phone='+201000000701')
        self.customer = Customer.objects.create(
            user=self.customer_user, name='عميل الأدمن', phone='+201000000701'
        )
        self.tech_user = self.user('tech', 'technician', phone='+201000000702')
        self.technician = Technician.objects.create(
            user=self.tech_user, name='فني الأدمن', phone='+201000000702',
            specialty='dish_install', availability='available', approval_status='approved',
        )

    def user(self, name, role, **extra):
        return User.objects.create(
            email=f'admin-test-{name}@example.com',
            password_hash=hash_password(PASSWORD),
            role=role,
            **extra,
        )

    def as_super(self):
        self.client.force_authenticate(user=self.super_admin)

    def completed_job(self, **fields):
        return Request.objects.create(
            customer=self.customer, technician=self.technician,
            specialty='dish_install', description='x', status='completed',
            commission_percent=20, **fields,
        )


class AdminPermissionTests(AdminBase):
    def test_section_permissions_are_enforced(self):
        self.client.force_authenticate(user=self.limited_admin)
        self.assertEqual(self.client.get('/api/admin/reviews').status_code, 200)
        self.assertEqual(self.client.get('/api/admin/wallets').status_code, 403)
        self.assertEqual(self.client.get('/api/admin/settings').status_code, 403)
        self.assertEqual(self.client.get('/api/admin/admins').status_code, 403)
        # The older views too: an admin with only "reviews" must not reach
        # customer data, technician documents, requests or password resets.
        self.assertEqual(self.client.get('/api/admin/customers').status_code, 403)
        self.assertEqual(self.client.get('/api/admin/technicians').status_code, 403)
        self.assertEqual(
            self.client.get(f'/api/admin/technicians/{self.technician.pk}/document/national_id').status_code, 403
        )
        self.assertEqual(self.client.get('/api/admin/requests').status_code, 403)
        self.assertEqual(self.client.get('/api/admin/stats').status_code, 403)
        self.assertEqual(
            self.client.post(f'/api/admin/users/{self.customer_user.pk}/reset-password').status_code, 403
        )
        self.assertEqual(
            self.client.put('/api/admin/settings', {'backup_telegram_chat_id': '1'}, format='json').status_code, 403
        )

    def test_non_admins_are_refused(self):
        self.client.force_authenticate(user=self.tech_user)
        self.assertEqual(self.client.get('/api/admin/wallets').status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get('/api/admin/wallets').status_code, 401)


class AdminFinanceTests(AdminBase):
    def test_payout_reduces_debt_and_lifts_the_block(self):
        add_wallet_transaction(self.technician.pk, type='debt', amount=300)
        self.as_super()
        wallets = self.client.get('/api/admin/wallets').json()
        row = next(w for w in wallets['wallets'] if w['technician_id'] == self.technician.pk)
        self.assertEqual(row['blocked'], 1)

        paid = self.client.post(
            f'/api/admin/wallets/{self.technician.pk}/payout', {'amount': 120}, format='json'
        )
        self.assertEqual(paid.status_code, 200)
        self.assertEqual(paid.json()['total_debt'], 180)
        txs = self.client.get(f'/api/admin/wallets/{self.technician.pk}/transactions').json()
        self.assertEqual([t['type'] for t in txs['transactions']], ['payout', 'debt'])

    def test_withdrawal_lifecycle(self):
        add_wallet_transaction(self.technician.pk, type='payment', amount=200)
        row = WithdrawalRequest.objects.create(technician=self.technician, amount=150)
        self.as_super()
        listed = self.client.get('/api/admin/withdrawals?status=pending').json()
        self.assertEqual(listed['awaiting_count'], 1)
        self.assertEqual(listed['withdrawals'][0]['technician_name'], 'فني الأدمن')

        self.assertEqual(self.client.post(f'/api/admin/withdrawals/{row.pk}/approve').status_code, 200)
        paid = self.client.post(f'/api/admin/withdrawals/{row.pk}/paid', {'note': 'فودافون كاش'}, format='json')
        self.assertEqual(paid.status_code, 200)
        self.assertEqual(TechnicianWallet.objects.get(pk=self.technician.pk).balance, 50)
        again = self.client.post(f'/api/admin/withdrawals/{row.pk}/paid')
        self.assertEqual(again.status_code, 409)

        rejected = WithdrawalRequest.objects.create(technician=self.technician, amount=20)
        self.assertEqual(self.client.post(f'/api/admin/withdrawals/{rejected.pk}/reject').status_code, 200)
        self.assertEqual(self.client.post(f'/api/admin/withdrawals/{rejected.pk}/paid').status_code, 400)

    def test_resolving_a_dispute_books_the_commission_once(self):
        job = self.completed_job(
            amount_status='disputed', technician_reported_amount=200, customer_reported_amount=300,
        )
        self.as_super()
        disputes = self.client.get('/api/admin/payments/disputed').json()
        self.assertEqual([d['id'] for d in disputes['disputes']], [job.pk])
        self.assertIn(job.pk, [p['id'] for p in self.client.get('/api/admin/payments/pending').json()['payments']])

        resolved = self.client.post(
            f'/api/admin/payments/{job.pk}/resolve-dispute',
            {'final_amount': 250, 'note': 'اتفقنا على 250'},
            format='json',
        )
        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()['status'], 'resolved')
        self.assertEqual(resolved.json()['commission'], 50)
        job.refresh_from_db()
        self.assertEqual(job.dispute_resolved_by_id, self.super_admin.pk)
        self.assertEqual(job.dispute_note, 'اتفقنا على 250')
        self.assertEqual(
            WalletTransaction.objects.filter(request_id=job.pk, type='debt').count(), 1
        )
        twice = self.client.post(
            f'/api/admin/payments/{job.pk}/resolve-dispute', {'final_amount': 250}, format='json'
        )
        self.assertEqual(twice.status_code, 400)

    def test_confirming_a_legacy_payment(self):
        job = self.completed_job(commission_amount=30)
        self.as_super()
        self.assertEqual(self.client.post(f'/api/admin/payments/{job.pk}/confirm').status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.payment_status, 'confirmed')
        self.assertEqual(TechnicianWallet.objects.get(pk=self.technician.pk).total_debt, 30)
        again = self.client.post(f'/api/admin/payments/{job.pk}/confirm').json()
        self.assertTrue(again['already_confirmed'])
        self.assertEqual(self.client.post(f'/api/admin/payments/{job.pk}/refund').status_code, 200)


class AdminConfigTests(AdminBase):
    def test_settings_only_save_known_keys_and_feed_the_site(self):
        self.as_super()
        saved = self.client.put(
            '/api/admin/settings',
            {'settings': {'wallet_debt_limit': '500', 'site_name': 'دشاوي برو', 'evil': 'x'}},
            format='json',
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(Setting.objects.get(key='wallet_debt_limit').value, '500')
        self.assertFalse(Setting.objects.filter(key='evil').exists())
        config = self.client.get('/api/public/config').json()
        self.assertEqual(config['site_name'], 'دشاوي برو')

    def test_pricing_and_specialties(self):
        self.as_super()
        self.assertEqual(
            self.client.put('/api/admin/pricing/dish_install', {'price': 175}, format='json').status_code,
            200,
        )
        self.assertEqual(ServicePricing.objects.get(service_key='dish_install').price, 175)
        pricing = self.client.get('/api/admin/pricing').json()['pricing']
        self.assertEqual(pricing[0]['price'], 175)
        self.assertEqual(
            self.client.put('/api/admin/pricing/dish_install', {'price': -5}, format='json').status_code,
            400,
        )

        made = self.client.post(
            '/api/admin/specialties', {'key': 'lnb_fix', 'label': 'تغيير LNB'}, format='json'
        )
        self.assertEqual(made.status_code, 201)
        self.assertTrue(ServicePricing.objects.filter(service_key='lnb_fix').exists())
        spec_id = made.json()['specialty']['id']
        self.assertEqual(
            self.client.put(f'/api/admin/specialties/{spec_id}', {'enabled': False}, format='json').status_code,
            200,
        )
        self.assertFalse(Specialty.objects.get(pk=spec_id).enabled)
        self.assertEqual(self.client.delete(f'/api/admin/specialties/{spec_id}').status_code, 200)

        used = Specialty.objects.get(key='dish_install')
        self.assertEqual(self.client.delete(f'/api/admin/specialties/{used.pk}').status_code, 400)
        bad = self.client.post('/api/admin/specialties', {'key': 'Bad Key', 'label': 'x'}, format='json')
        self.assertEqual(bad.status_code, 400)


class AdminManageTests(AdminBase):
    def test_admin_accounts(self):
        self.as_super()
        made = self.client.post(
            '/api/admin/admins',
            {'email': 'new-admin@example.com', 'password': 'AdminPass123', 'permissions': ['stats', 'bogus']},
            format='json',
        )
        self.assertEqual(made.status_code, 201)
        self.assertEqual(made.json()['admin']['permissions'], ['stats'])
        admin_id = made.json()['admin']['id']
        self.assertEqual(
            self.client.put(f'/api/admin/admins/{admin_id}', {'permissions': ['reviews']}, format='json').json()['permissions'],
            ['reviews'],
        )
        self.assertTrue(self.client.post(f'/api/admin/admins/{admin_id}/toggle-active').json()['blocked'])
        self.assertEqual(
            self.client.post(f'/api/admin/admins/{admin_id}/reset-password', {'new_password': 'Another123'}, format='json').status_code,
            200,
        )
        self.assertEqual(self.client.delete(f'/api/admin/admins/{self.super_admin.pk}').status_code, 400)
        self.assertEqual(self.client.delete(f'/api/admin/admins/{admin_id}').status_code, 200)

    def test_technician_create_update_delete_and_pending(self):
        self.as_super()
        made = self.client.post(
            '/api/admin/technicians',
            {'email': 'made-tech@example.com', 'password': 'TechPass123', 'name': 'فني جديد',
             'phone': '01000000799', 'area_key': 'smouha'},
            format='json',
        )
        self.assertEqual(made.status_code, 201)
        tech_id = made.json()['technician']['id']
        self.assertEqual(made.json()['technician']['approval_status'], 'approved')
        updated = self.client.put(
            f'/api/admin/technicians/{tech_id}', {'availability': 'offline'}, format='json'
        )
        self.assertEqual(updated.json()['technician']['availability'], 'offline')

        Technician.objects.filter(pk=tech_id).update(approval_status='pending')
        pending = self.client.get('/api/admin/technicians/pending').json()['technicians']
        self.assertEqual([t['id'] for t in pending], [tech_id])
        self.assertEqual(self.client.delete(f'/api/admin/technicians/{tech_id}').status_code, 200)
        self.assertFalse(User.objects.filter(email='made-tech@example.com').exists())

    def test_customer_detail_block_and_delete(self):
        self.completed_job()
        self.as_super()
        detail = self.client.get(f'/api/admin/customers/{self.customer.pk}').json()
        self.assertEqual(detail['stats']['completed'], 1)
        self.client.patch(f'/api/admin/customers/{self.customer.pk}/block', {'blocked': True}, format='json')
        self.assertTrue(User.objects.get(pk=self.customer_user.pk).blocked)
        self.assertEqual(self.client.delete(f'/api/admin/customers/{self.customer.pk}').status_code, 200)
        self.assertFalse(Request.objects.filter(customer_id=self.customer.pk).exists())

    def test_request_assign_unassign_rebroadcast_delete(self):
        job = Request.objects.create(
            customer=self.customer, specialty='dish_install', description='x', status='pending'
        )
        self.as_super()
        self.assertEqual(self.client.post(f'/api/admin/requests/{job.pk}/rebroadcast').json()['sent_to'], 1)
        self.assertEqual(len(self.client.get(f'/api/admin/requests/{job.pk}/broadcasts').json()['broadcasts']), 1)
        self.assertEqual([r['id'] for r in self.client.get('/api/admin/requests/queued').json()['requests']], [job.pk])

        assigned = self.client.post(
            f'/api/admin/requests/{job.pk}/assign', {'technician_id': self.technician.pk}, format='json'
        )
        self.assertEqual(assigned.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, 'assigned')
        self.assertRegex(job.start_pin, r'^\d{4}$')
        self.assertEqual(RequestBroadcast.objects.get(request=job).status, 'expired')

        self.assertEqual(self.client.post(f'/api/admin/requests/{job.pk}/unassign').status_code, 200)
        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'available')
        self.assertEqual(self.client.delete(f'/api/admin/requests/{job.pk}').status_code, 200)
        self.assertFalse(Request.objects.filter(pk=job.pk).exists())

    def test_reviews_moderation(self):
        review = Review.objects.create(
            request=self.completed_job(), customer=self.customer,
            technician=self.technician, rating=2, comment='اتأخر',
        )
        self.client.force_authenticate(user=self.limited_admin)
        listed = self.client.get('/api/admin/reviews?search=اتأخر').json()
        self.assertEqual(listed['total'], 1)
        self.assertEqual(listed['reviews'][0]['technician_name'], 'فني الأدمن')
        self.client.post(f'/api/admin/reviews/{review.pk}/hide', {'reason': 'ألفاظ'}, format='json')
        stats = self.client.get('/api/admin/reviews/stats').json()
        self.assertEqual(stats['hidden'], 1)
        self.client.post(f'/api/admin/reviews/{review.pk}/unhide')
        self.assertFalse(Review.objects.get(pk=review.pk).hidden)
        self.assertEqual(self.client.delete(f'/api/admin/reviews/{review.pk}').status_code, 200)

    def test_data_subject_requests_and_erase(self):
        item = DataSubjectRequest.objects.create(user=self.customer_user, request_type='erasure')
        self.as_super()
        listed = self.client.get('/api/admin/data-subject-requests').json()['requests']
        self.assertEqual(listed[0]['user_email'], self.customer_user.email)
        self.client.patch(f'/api/admin/data-subject-requests/{item.pk}', {'status': 'completed'}, format='json')
        self.assertEqual(DataSubjectRequest.objects.get(pk=item.pk).status, 'completed')
        self.assertEqual(self.client.delete(f'/api/admin/users/{self.customer_user.pk}/erase').status_code, 200)
        self.assertFalse(User.objects.filter(pk=self.customer_user.pk).exists())
        self.assertEqual(self.client.delete(f'/api/admin/users/{self.super_admin.pk}/erase').status_code, 403)
