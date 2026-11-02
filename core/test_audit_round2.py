"""Second round of audit fixes: suspended technicians, file erasure, order
budgets and lock notifications."""

from unittest.mock import patch

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from .auth_backend import hash_password
from .models import Customer, Request, Specialty, StoredFile, Technician, TechnicianPortfolio, User
from .services.assignment import release_technician
from .tests import PNG_BYTES

PASSWORD = 'TesterPass123'
DATABASE_STORAGES = {
    'default': {'BACKEND': 'core.storage.DatabaseStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


class Round2Base(APITestCase):
    def setUp(self):
        cache.clear()
        for target in ('core.services.notifications.send_telegram',):
            stub = patch(target, return_value=True)
            stub.start()
            self.addCleanup(stub.stop)
        Specialty.objects.create(key='dish_install', label='تركيب دش')
        self.customer_user = User.objects.create(
            email='r2-customer@example.com', phone='+201000000601',
            password_hash=hash_password(PASSWORD), role='customer',
        )
        self.customer = Customer.objects.create(
            user=self.customer_user, name='عميل', phone='+201000000601'
        )
        self.tech_user = User.objects.create(
            email='r2-tech@example.com', phone='+201000000602',
            password_hash=hash_password(PASSWORD), role='technician',
        )
        self.technician = Technician.objects.create(
            user=self.tech_user, name='فني', phone='+201000000602',
            specialty='dish_install', availability='available', approval_status='approved',
        )
        self.admin = User.objects.create(
            email='r2-admin@example.com', password_hash=hash_password(PASSWORD),
            role='admin', is_super_admin=True,
        )

    def order(self):
        self.client.force_authenticate(user=self.customer_user)
        return self.client.post(
            '/api/customer/requests',
            {'specialty': 'dish_install', 'description': 'الدش مش شغال', 'area_key': 'smouha'},
            format='json',
        )

    def accepted_job(self):
        request_id = self.order().json()['request']['id']
        self.client.force_authenticate(user=self.tech_user)
        offer = self.client.get('/api/technician/incoming').json()['requests'][0]
        self.client.post(f"/api/technician/broadcasts/{offer['broadcast_id']}/accept")
        return request_id


class SuspendedTechnicianTests(Round2Base):
    def test_suspended_technician_is_refused_everywhere_and_stays_suspended(self):
        request_id = self.accepted_job()
        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'busy')

        # The admin suspends them mid-job.
        Technician.objects.filter(pk=self.technician.pk).update(availability='offline')
        self.client.force_authenticate(user=self.tech_user)
        self.assertEqual(self.client.get('/api/technician/requests').status_code, 403)
        self.assertEqual(self.client.get('/api/technician/incoming').status_code, 403)
        chat = self.client.post(
            f'/api/messages/{request_id}', {'body': 'أهلاً'}, format='json'
        )
        self.assertEqual(chat.status_code, 403)
        self.assertEqual(self.client.get(f'/api/messages/{request_id}').status_code, 403)

        # Whatever ends the job, a suspended technician does not return to the pool.
        release_technician(self.technician.pk)
        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'offline')
        self.client.force_authenticate(user=self.customer_user)
        self.client.post(f'/api/customer/requests/{request_id}/cancel')
        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'offline')

        # A busy technician is released as before.
        Technician.objects.filter(pk=self.technician.pk).update(availability='busy')
        release_technician(self.technician.pk)
        self.technician.refresh_from_db()
        self.assertEqual(self.technician.availability, 'available')


@override_settings(STORAGES=DATABASE_STORAGES)
class ErasureDeletesFilesTests(Round2Base):
    def stored(self, name):
        storages['default'].save(name, ContentFile(PNG_BYTES))
        return '/uploads/' + name

    def test_erase_and_delete_remove_the_files(self):
        Technician.objects.filter(pk=self.technician.pk).update(
            criminal_record_path=self.stored(f'technicians/user-{self.tech_user.pk}/record.png'),
            national_id_path=self.stored(f'technicians/user-{self.tech_user.pk}/id.png'),
        )
        TechnicianPortfolio.objects.create(
            technician_id=self.technician.pk,
            image_path=self.stored(f'technicians/portfolio/tech-{self.technician.pk}/work.png'),
        )
        request_id = self.order().json()['request']['id']
        Request.objects.filter(pk=request_id).update(
            order_photo_path=self.stored(f'orders/{request_id}.png')
        )
        unrelated = self.stored('orders/unrelated.png')
        self.assertEqual(StoredFile.objects.count(), 5)

        self.client.force_authenticate(user=self.admin)
        # The files go once the deleting transaction commits.
        with self.captureOnCommitCallbacks(execute=True):
            erased = self.client.delete(f'/api/admin/users/{self.tech_user.pk}/erase')
        self.assertEqual(erased.status_code, 200)
        self.assertFalse(StoredFile.objects.filter(name__startswith='technicians/').exists())
        self.assertFalse(User.objects.filter(pk=self.tech_user.pk).exists())

        with self.captureOnCommitCallbacks(execute=True):
            deleted = self.client.delete(f'/api/admin/customers/{self.customer.pk}')
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(
            list(StoredFile.objects.values_list('name', flat=True)), ['orders/unrelated.png']
        )
        self.assertTrue(storages['default'].exists(unrelated.replace('/uploads/', '')))


class OrderBudgetTests(Round2Base):
    def test_open_orders_are_capped(self):
        for _ in range(3):
            self.assertEqual(self.order().status_code, status.HTTP_201_CREATED)
        fourth = self.order()
        self.assertEqual(fourth.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('طلبات شغالة', fourth.json()['error'])
        # Cancelling one frees a slot.
        first = Request.objects.filter(customer_id=self.customer.pk).order_by('id').first()
        self.client.post(f'/api/customer/requests/{first.pk}/cancel')
        self.assertEqual(self.order().status_code, status.HTTP_201_CREATED)

    @patch('core.views.MAX_ORDERS_PER_DAY', 2)
    def test_daily_order_budget_is_per_account(self):
        first = self.order().json()['request']['id']
        self.client.post(f'/api/customer/requests/{first}/cancel')
        second = self.order().json()['request']['id']
        self.client.post(f'/api/customer/requests/{second}/cancel')
        self.assertEqual(self.order().status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        # Nothing was broadcast for the refused order.
        self.assertEqual(Request.objects.filter(customer_id=self.customer.pk).count(), 2)


class LockNotificationTests(Round2Base):
    @patch('core.views.notify_account_locked')
    def test_owner_is_told_when_the_account_locks(self, notify):
        for _ in range(5):
            self.client.post(
                '/api/auth/login',
                {'identifier': self.customer_user.email, 'password': 'wrong-pass'},
                format='json',
            )
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0].pk, self.customer_user.pk)
