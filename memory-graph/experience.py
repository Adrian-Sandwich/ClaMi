"""Keep reported usefulness separate from observable execution events."""
EVENTS = {'EJECUCIÓN FALLIDA': 'execution_failed', 'MERGE OK': 'merged',
          'MERGE DETENIDO': 'merge_blocked', 'MERGE PENDIENTE': 'merge_blocked'}


def build(row):
    reports = sorted(row.get('outcome_reports') or [], key=lambda r: r['id'])
    events = []
    for message in row.get('system_messages') or []:
        if message.get('author') != 'magi':
            continue
        for prefix, kind in EVENTS.items():
            if (message.get('body') or '').startswith(prefix + ' —'):
                events.append({'kind': kind, 'message_id': message['id'],
                               'detail': message['body'], 'created_at': message.get('created_at')})
                break
    execution = (row.get('minority_report') or {}).get('execution') or {}
    return {'reports': reports, 'latest_report': reports[-1] if reports else None,
            'revised': len({r['status'] for r in reports}) > 1,
            'events': events,
            'execution': {key: execution[key] for key in ('review_id','reviewed_sha','merge_sha','base_sha') if key in execution}}
