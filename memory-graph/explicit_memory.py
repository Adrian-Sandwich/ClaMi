"""Version explicit, human-authored statements; never infer durable preferences."""
import re

LINE = re.compile(r'^\s*(?:[-*]\s*)?(objetivo|restricción|restriccion|pendiente)'
                  r'(?:\s*\[([^\]]{1,80})\])?\s*:\s*(.+)$', re.IGNORECASE)


def extract(messages):
    history = []
    current = {}
    for message in sorted(messages, key=lambda m: m['id']):
        if message.get('author') != 'adrian':
            continue
        # Ignore code fences and quoted text, which may merely show examples.
        fenced = False
        for line in message.get('body', '').splitlines():
            if line.strip().startswith(('```', '~~~')):
                fenced = not fenced
                continue
            match = None if fenced else LINE.match(line)
            if not match:
                continue
            kind, key, text = match.groups()
            kind = {'restricción': 'restriccion'}.get(kind.lower(), kind.lower())
            key = (key or 'general').strip().casefold()
            identity = (kind, key)
            previous = current.get(identity)
            entry = {'kind': kind, 'key': key, 'text': text.strip(),
                     'message_id': message['id'], 'created_at': message.get('created_at'),
                     'version': previous['version'] + 1 if previous else 1,
                     'active': text.strip().casefold() not in ('cancelado', 'cancelada', 'resuelto', 'resuelta')}
            history.append(entry)
            current[identity] = entry
    return {'current': list(current.values()), 'history': history}
