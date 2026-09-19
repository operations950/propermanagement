"""Encrypts QuickBooks OAuth tokens at rest (AES, see core/fields.py) —
required by Intuit's QuickBooks security requirements. realm_id also moves
from a 50-character CharField to a text column, since ciphertext is longer
than the value it wraps.

The RunPython at the end re-saves every existing QuickBooksToken row so a
connection made BEFORE this deploy is encrypted too, rather than sitting
in plain text until someone happens to reconnect. It's a no-op when there
are no rows (nothing connected yet). Reads are already safe either way:
EncryptedTextField returns an un-prefixed legacy value as-is, so an
existing connection keeps working through the moment of the migration.
Not reversible in a meaningful sense (there's no going back to plaintext
on purpose) — reverse is a no-op."""
import core.fields
from django.db import migrations


def encrypt_existing_rows(apps, schema_editor):
    QuickBooksToken = apps.get_model('core', 'QuickBooksToken')
    count = 0
    for token in QuickBooksToken.objects.all():
        # Loaded values are plaintext (legacy) or already-decrypted; saving
        # runs each field's get_prep_value, which encrypts.
        token.save(update_fields=['realm_id', 'access_token', 'refresh_token'])
        count += 1
    print(f'Encrypted {count} stored QuickBooks token row(s).')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0042_restore_quadplex_turnover_prices'),
    ]

    operations = [
        migrations.AlterField(
            model_name='quickbookstoken',
            name='access_token',
            field=core.fields.EncryptedTextField(blank=True),
        ),
        migrations.AlterField(
            model_name='quickbookstoken',
            name='realm_id',
            field=core.fields.EncryptedTextField(help_text='The QuickBooks company ID this token authorizes access to.'),
        ),
        migrations.AlterField(
            model_name='quickbookstoken',
            name='refresh_token',
            field=core.fields.EncryptedTextField(),
        ),
        migrations.RunPython(encrypt_existing_rows, migrations.RunPython.noop),
    ]
