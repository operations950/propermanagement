"""Switch the new layout on or off for people, by email or username. Safe to repeat.

  python manage.py set_new_face justin@example.com                 switch on
  python manage.py set_new_face anna@example.com --role manager    switch on and set the menu
  python manage.py set_new_face anna@example.com --off             back to the original site
The same thing is on the Admin > People and menus screen.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from core.models import StaffProfile


class Command(BaseCommand):
    help = 'Switch the new layout on or off for people.'

    def add_arguments(self, parser):
        parser.add_argument('who', nargs='+', help='email addresses or usernames')
        parser.add_argument('--off', action='store_true')
        parser.add_argument('--role', choices=StaffProfile.MenuRole.values)

    def handle(self, *args, **opts):
        User = get_user_model()
        for who in opts['who']:
            sp = StaffProfile.objects.filter(Q(user__email__iexact=who) | Q(user__username__iexact=who)).first()
            if not sp:
                raise CommandError(f'No staff account for {who}.')
            sp.new_face = not opts['off']
            if opts['role']:
                sp.menu_role = opts['role']
            sp.save(update_fields=['new_face', 'menu_role'])
            self.stdout.write(f'{sp}: new layout {"on" if sp.new_face else "off"}, menu {sp.menu_role or "default"}')
