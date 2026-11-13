from django.db import migrations, models

# Where each expense account sat in the budget until now (accounting/budgeting.py decided it from these lists); 6903 Insurance Financing joins the insurance accounts in Administrative.
UTILITIES = {'6410', '6420', '6425', '6429', '6430', '6431', '6435', '6440', '6450'}
ADMIN = {'6320', '6350', '6460', '6510', '6610', '6611', '6800', '6820', '6830', '6880', '6881', '6882', '6883', '6884', '6885', '6886', '6891', '6894', '6895', '6899', '6902', '6903', '6904', '6916', '6917', '6920'}


def section(code):
    return 'Utilities' if code in UTILITIES else 'Administrative' if code in ADMIN else 'Operating'


def seed(apps, schema_editor):
    """Every expense account gets its section written down, so nothing moves in any budget (except 6903, which goes to Administrative)."""
    # 6903 was added by hand on the live chart; a fresh database gets it here, and a database that already has it is left as it is
    apps.get_model('accounting', 'MasterAccount').objects.get_or_create(code='6903', defaults={'name': 'Insurance Financing', 'kind': 'expense'})
    for name in ('MasterAccount', 'Account'):
        model = apps.get_model('accounting', name)
        for row in model.objects.filter(kind='expense'):
            row.budget_section = section(row.code)
            row.save(update_fields=['budget_section'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0012_ledger_detail'),
    ]

    operations = [
        migrations.AddField(
            model_name='account',
            name='budget_section',
            field=models.CharField(blank=True, default='', help_text='Where an expense account sits in the budget. Blank: the program decides from the number.', max_length=14),
        ),
        migrations.AddField(
            model_name='masteraccount',
            name='budget_section',
            field=models.CharField(blank=True, default='', help_text='Where an expense account sits in every association\'s budget: Administrative, Utilities or Operating.', max_length=14),
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
