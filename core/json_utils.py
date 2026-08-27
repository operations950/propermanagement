"""Safe JSON-for-<script>-tag serialization — see dumps_for_script."""
import json


def dumps_for_script(value):
    """json.dumps(value), with '<', '>', and '&' escaped to their \\uXXXX
    forms — the exact same escaping Django's own json_script template
    filter applies (django.utils.html.json_script), reimplemented here
    directly rather than importing that filter's private
    _json_script_escapes mapping.

    Every call site that builds a *_json context value later rendered as
    <script type="application/json">{{ value|safe }}</script> MUST go
    through this, not bare json.dumps(). A bare json.dumps() result can
    contain a literal "</script>" substring whenever the underlying data
    includes one (a Contact name, a Unit label — both free text a staff
    member can set to anything) — the browser's HTML tokenizer closes the
    <script> tag at that literal substring regardless of it being inside
    a JSON string, letting whatever text follows execute as real script.
    Escaping '<'/'>' makes that substring impossible to produce; escaping
    '&' additionally guards against '&lt;' up-front decoding to '<' before
    the tokenizer sees it, matching json_script's own choice to escape all
    three rather than just the two strictly needed for the </script> case."""
    return (
        json.dumps(value)
        .replace('<', '\\u003C')
        .replace('>', '\\u003E')
        .replace('&', '\\u0026')
    )
