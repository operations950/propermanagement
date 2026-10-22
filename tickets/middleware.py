class TicketFilterMemoryMiddleware:
    """The ticket list remembers its filters so the up arrow on a ticket returns to the same filtered list (tickets.views._remember_list_filters).
    This forgets them the moment the person opens any page outside the tickets pages, so the next ticket opened from somewhere else goes back to the
    plain list. Only real page visits count: background requests (the weather box, a search-as-you-type call) do not clear anything."""
    KEEP_PREFIXES = ('/tickets/', '/static/', '/media/')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (request.method == 'GET' and not request.path.startswith(self.KEEP_PREFIXES)
                and 'text/html' in request.META.get('HTTP_ACCEPT', '')
                and not request.headers.get('X-Requested-With') and not request.headers.get('HX-Request')):
            session = getattr(request, 'session', None)
            if session is not None and 'ticket_list_qs' in session:
                del session['ticket_list_qs']
        return self.get_response(request)
