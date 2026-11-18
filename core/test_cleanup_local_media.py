"""cleanup_local_media: free the files volume without losing a file."""
import os
import tempfile
from io import StringIO
from unittest import mock

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from core.management.commands import boot_tasks
from core.models import Property, PropertyDocument


class CleanupTests(TestCase):
    def setUp(self):
        self.local = tempfile.TemporaryDirectory()
        self.cloud = tempfile.TemporaryDirectory()
        self.addCleanup(self.local.cleanup)
        self.addCleanup(self.cloud.cleanup)
        storages = {'documents': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': self.cloud.name}},
                    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage', 'OPTIONS': {'location': self.cloud.name}},
                    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}
        ov = override_settings(STORAGES=storages, MEDIA_ROOT=self.local.name, CLOUDINARY_CLOUD_NAME='c', CLOUDINARY_API_KEY='k', CLOUDINARY_API_SECRET='s')
        ov.enable()
        self.addCleanup(ov.disable)
        self.prop = Property.objects.create(name='P', property_type='association')

    def put(self, root, name, data=b'x' * 1000):
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def doc(self, name):
        d = PropertyDocument.objects.create(property=self.prop, name=name, file='placeholder')
        PropertyDocument.objects.filter(pk=d.pk).update(file=f'property_documents/2026/10/{name}')
        return d

    def run_cmd(self, *args):
        out = StringIO()
        call_command('cleanup_local_media', *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_a_report_changes_nothing(self):
        self.doc('a.pdf')
        local = self.put(self.local.name, 'property_documents/2026/10/a.pdf')
        self.put(self.cloud.name, 'property_documents/2026/10/a.pdf')
        text = self.run_cmd()
        self.assertIn('Safe to delete (Cloudinary holds the same file): 1 files', text)
        self.assertIn('Report only', text)
        self.assertTrue(os.path.exists(local))

    def test_a_file_cloudinary_holds_is_deleted_from_the_volume(self):
        self.doc('a.pdf')
        local = self.put(self.local.name, 'property_documents/2026/10/a.pdf')
        cloud = self.put(self.cloud.name, 'property_documents/2026/10/a.pdf')
        text = self.run_cmd('--apply')
        self.assertFalse(os.path.exists(local))
        self.assertTrue(os.path.exists(cloud))
        self.assertIn('1 deleted', text)

    def test_a_file_cloudinary_does_not_hold_is_copied_up_before_it_is_deleted(self):
        self.doc('b.pdf')
        local = self.put(self.local.name, 'property_documents/2026/10/b.pdf', b'important' * 100)
        self.run_cmd('--apply')
        self.assertFalse(os.path.exists(local))
        with open(os.path.join(self.cloud.name, 'property_documents/2026/10/b.pdf'), 'rb') as f:
            self.assertEqual(f.read(), b'important' * 100)

    def test_a_file_cloudinary_holds_at_a_different_size_is_not_trusted(self):
        self.doc('c.pdf')
        self.put(self.local.name, 'property_documents/2026/10/c.pdf', b'the full file' * 50)
        self.put(self.cloud.name, 'property_documents/2026/10/c.pdf', b'cut off')
        text = self.run_cmd()
        self.assertIn('Safe to delete (Cloudinary holds the same file): 0 files', text)
        self.assertIn('1 are there with a different size', text)

    def test_a_file_no_row_names_is_kept_unless_orphans_are_asked_for(self):
        stray = self.put(self.local.name, 'old/stray.txt')
        self.run_cmd('--apply')
        self.assertTrue(os.path.exists(stray))
        text = self.run_cmd('--apply', '--orphans')
        self.assertFalse(os.path.exists(stray))
        self.assertIn('1 orphans deleted', text)

    def test_it_refuses_when_cloudinary_is_not_set_up(self):
        with override_settings(CLOUDINARY_CLOUD_NAME=''):
            with self.assertRaises(CommandError):
                self.run_cmd('--apply')

    def test_a_missing_media_folder_is_not_an_error(self):
        with override_settings(MEDIA_ROOT=os.path.join(self.local.name, 'nope')):
            self.assertIn('nothing to do', self.run_cmd())


class BootSwitchTests(TestCase):
    def test_nothing_runs_without_the_variable(self):
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(boot_tasks, 'call_command') as call:
            os.environ.pop('MEDIA_CLEANUP', None)
            boot_tasks.Command().media_cleanup()
        call.assert_not_called()

    def test_report_apply_and_apply_orphans(self):
        for mode, kwargs in (('report', {'apply': False, 'orphans': False}), ('apply', {'apply': True, 'orphans': False}), ('apply_orphans', {'apply': True, 'orphans': True})):
            with mock.patch.dict(os.environ, {'MEDIA_CLEANUP': mode}), mock.patch.object(boot_tasks, 'call_command') as call:
                boot_tasks.Command().media_cleanup()
            call.assert_called_once_with('cleanup_local_media', **kwargs)

    def test_a_failing_clean_up_never_stops_the_site_starting(self):
        with mock.patch.dict(os.environ, {'MEDIA_CLEANUP': 'apply'}), mock.patch.object(boot_tasks, 'call_command', side_effect=RuntimeError('boom')):
            boot_tasks.Command().media_cleanup()            # does not raise
