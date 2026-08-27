from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tickets', '0037_ticket_created_by'),
    ]

    operations = [
        migrations.AddField(
            model_name='ticketattachment',
            name='visible_to_vendor',
            # Existing rows all backfill to True — this matches the current
            # is_document-based proxy filter's behavior for every attachment
            # that already exists (an old image uploaded via the Documents
            # card, if any, has no record of that and stays visible, same as
            # it was before this field existed). preserve_default=False
            # means the model field itself has NO default going forward, so
            # any future TicketAttachment.objects.create() that forgets to
            # pass visible_to_vendor fails loudly instead of silently
            # leaking a new upload path.
            field=models.BooleanField(
                default=True,
                help_text="Whether this can appear on the public, unauthenticated vendor completion link. "
                          "False for internal-only uploads like the Documents card, regardless of file type — "
                          "deliberately has no model-level default, so every creation site must decide explicitly "
                          "rather than leaking a new upload path by accident.",
            ),
            preserve_default=False,
        ),
    ]
