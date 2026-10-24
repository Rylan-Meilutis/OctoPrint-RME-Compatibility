"""Planned physical tool transitions indexed by exact streamed-file offsets."""
import bisect
import re

TOOL = re.compile(rb'^\s*(?:N\d+\s+)?T(\d+)(?=\s|[A-Z]|\*|$)', re.I)


def read_selections(path, capacity=8):
    selections, offset = [], 0
    with open(path, 'rb') as source:
        for raw in source:
            offset += len(raw)
            match = TOOL.match(raw.split(b';', 1)[0])
            if match:
                tool = int(match[1])
                if tool < capacity:
                    selections.append((offset, tool))
                    if len(selections) > 200000:
                        raise ValueError('Too many tool selections to index safely')
    return tuple(selections)


def remaining(selections, position, mapping=None):
    """Exclude the initial pickup, repeated selections and park/unpark cycles.

    These are planned transitions, not a prediction of manual interventions
    or filament fallback changes. Never derive them from percent completion.
    """
    transitions, previous = [], None
    mapping = mapping or {}
    for offset, logical in selections:
        physical = int(mapping.get(logical, mapping.get(str(logical), logical)))
        if not 0 <= physical < 8:
            return dict(total=None, completed=None, remaining=None)
        if previous is not None and physical != previous:
            transitions.append(offset)
        previous = physical
    completed = bisect.bisect_right(transitions, position)
    return dict(total=len(transitions), completed=completed,
                remaining=len(transitions) - completed)
