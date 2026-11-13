from unittest import mock

from django.test import TestCase


class HealthzTests(TestCase):
    def test_ok_without_login(self):
        response = self.client.get('/healthz/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    def test_503_when_database_is_down(self):
        with mock.patch('cms.views.connection.cursor', side_effect=RuntimeError('down')):
            response = self.client.get('/healthz/')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'error')
