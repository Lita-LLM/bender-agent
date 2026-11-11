"""Передача разговора другому агенту стаи (тул hand_over).

Записка «о чём шла речь» живёт в памяти процесса и уходит новому агенту вместе с первой
репликой хозяина в том же канале: она часть самой реплики, история потом не меняется.
"""
import time

TTL = 3600  # записка старше часа уже не про этот разговор


def leave(host, agent_id, channel, giver, note):
    notes = host.__dict__.setdefault('_handoffs', {})
    notes[(agent_id, channel)] = {'from': giver, 'note': str(note or '').strip()[:1000], 'at': time.time()}


def take(host, agent_id, channel):
    """Префикс к первой реплике после передачи; пусто, если передачи не было."""
    item = host.__dict__.get('_handoffs', {}).pop((agent_id, channel), None)
    if not item or time.time() - item['at'] > TTL:
        return ''
    return '[Разговор передал тебе ' + item['from'] + (': ' + item['note'] if item['note'] else '') + '] '
