"""Test runner that makes `manage.py test` work from a clean clone.

Two things production settings do that tests must not:

* Static files use WhiteNoise's manifest storage, which raises "Missing
  staticfiles manifest entry" for every rendered page until `collectstatic`
  has been run. Tests use plain storage instead.
* Uploads go to MEDIA_ROOT (`private_media/`). Tests get a throwaway
  directory, so running the suite on the Pi never touches real uploads.
"""
import shutil
import tempfile

from django.conf import settings
from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class JohnCadTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._media_root = tempfile.mkdtemp(prefix='johncad-test-media-')
        storages = {
            **settings.STORAGES,
            'staticfiles': {
                'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage',
            },
        }
        self._overrides = override_settings(
            MEDIA_ROOT=self._media_root, STORAGES=storages,
        )
        self._overrides.enable()

    def teardown_test_environment(self, **kwargs):
        self._overrides.disable()
        shutil.rmtree(self._media_root, ignore_errors=True)
        super().teardown_test_environment(**kwargs)
