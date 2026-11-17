"""No stored file name can be longer than a FileField's database column holds (100 characters): the storage shortens the name of the file, never its folder or its extension."""
from django.test import SimpleTestCase

from accounting.models import AssociationDocument, Bill
from core.storage import MAX_STORED_NAME, clamp_stored_name


class StoredNameTests(SimpleTestCase):
    def test_a_short_name_is_left_alone(self):
        self.assertEqual(clamp_stored_name('association_documents/2026/11/Bylaws.pdf'), 'association_documents/2026/11/Bylaws.pdf')

    def test_a_long_name_is_shortened_keeping_folder_and_extension(self):
        name = 'association_documents/2026/11/' + 'Castlewood Town Villas Rules and Regulations (revised 2-9-13) ' * 3 + '.pdf'
        out = clamp_stored_name(name)
        self.assertLessEqual(len(out), MAX_STORED_NAME)
        self.assertTrue(out.startswith('association_documents/2026/11/Castlewood Town Villas'))
        self.assertTrue(out.endswith('.pdf'))

    def test_a_name_with_no_extension_is_shortened_too(self):
        out = clamp_stored_name('bills/2026/11/' + 'x' * 300)
        self.assertEqual(len(out), MAX_STORED_NAME)

    def test_room_is_left_for_the_storage_to_add_its_own_suffix(self):
        self.assertLessEqual(MAX_STORED_NAME + 12, 100)

    def test_every_upload_field_names_its_file_through_the_clamp(self):
        long_name = 'A very long file name ' * 12 + '.pdf'
        for model, field in ((AssociationDocument, 'file'), (Bill, 'file')):
            f = model._meta.get_field(field)
            name = f.generate_filename(model(), long_name)
            self.assertLessEqual(len(name), 100, (model, name))
