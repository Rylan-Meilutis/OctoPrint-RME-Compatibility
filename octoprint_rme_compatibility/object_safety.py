"""Read-only object boundaries and a feature-detected Cancel Object adapter.

Never guess a shared purge/tool-change region from geometry. Only labeled,
closed object spans are cancellable. Byte offsets refer to the actual file
OctoPrint streams, not its original preprocessed upload.
"""
import functools
import os
import re

START = re.compile(r"^(?:@object\s+|;\s*printing object\s+)(.+)$", re.I)
STOP = re.compile(r"^(?:@objectstop|;\s*stop printing object)(?:\s+(.*))?$", re.I)
SHARED = frozenset(('STARTGCODE', 'ENDGCODE', 'NONMESH', 'WIPE_TOWER', 'PRIME_TOWER'))
COMMAND = re.compile(r"^\s*(?:N\d+\s+)?([GMT]\d+)(?=\s|[A-Z]|\*|$)", re.I)
E_VALUE = re.compile(r"\bE\s*(-?(?:\d+(?:\.\d*)?|\.\d+))", re.I)


def motion_command(cmd):
    match = COMMAND.match(str(cmd))
    return bool(match and match.group(1).upper() in ('G0', 'G1', 'G2', 'G3'))


def fingerprint(path):
    stat = os.stat(path)
    return (os.path.realpath(path), stat.st_size, stat.st_mtime_ns, stat.st_ino)


def read_index(path):
    identity = fingerprint(path)
    result = _read_index(*identity)
    if fingerprint(path) != identity:
        raise ValueError('The selected G-code changed while inspecting object labels')
    return result


@functools.lru_cache(maxsize=4)
def _read_index(path, size, modified, inode):
    objects = {}
    current = None
    relative_e = False
    relative_xyz = False
    extrusion = 0.0
    offset = 0

    def close(end, stopped_name=None):
        if current is not None:
            row = objects[current]
            row['closed'] = True
            if stopped_name and stopped_name != current:
                row['reason'] = 'Object start/stop names do not match'
            row['last_end'] = end

    with open(path, 'rb') as source:
        for raw in source:
            offset += len(raw)
            line = raw.decode('utf-8', 'replace').strip()
            stop = STOP.fullmatch(line)
            if stop:
                close(offset, (stop.group(1) or '').strip())
                current = None
                continue
            start = START.fullmatch(line)
            if start:
                close(offset - len(raw))
                name = start.group(1).strip()
                current = None if name.upper() in SHARED else name
                if current is not None:
                    if current not in objects and len(objects) >= 1024:
                        raise ValueError('Too many labeled objects')
                    row = objects.setdefault(current, dict(name=current, last_print_end=None,
                        last_end=None, closed=False, reason=None))
                    row['closed'] = False
                continue
            code = line.split(';', 1)[0].strip()
            match = COMMAND.match(code)
            command = match.group(1).upper() if match else ''
            if command == 'M83': relative_e = True
            elif command == 'M82': relative_e = False
            elif command == 'G90': relative_xyz = False
            elif command == 'G91': relative_xyz = True
            value = E_VALUE.search(code)
            e = float(value.group(1)) if value else None
            if command == 'G92' and e is not None:
                extrusion = e
            if current is not None:
                row = objects[current]
                if command.startswith('T') or command in ('G12', 'G28', 'G29', 'G427', 'M600', 'M701', 'M702', 'M1600') or re.search(r'(?:wipe|prime)[ _-]*tower|CP\s+TOOLCHANGE', line, re.I):
                    row['reason'] = 'Tool changes, priming and prime-tower blocks must be outside object labels'
                if motion_command(code) and relative_xyz:
                    row['reason'] = 'Object motion must use absolute XYZ coordinates'
                if command in ('G0', 'G1', 'G2', 'G3') and e is not None and (e > 0 if relative_e else e > extrusion):
                    if command in ('G2', 'G3') or re.search(r'\b[XY]\s*-?[\d.]', code, re.I):
                        row['last_print_end'] = offset
            if command in ('G0', 'G1', 'G2', 'G3') and e is not None:
                extrusion = extrusion + e if relative_e else e
    for row in objects.values():
        if not row['closed']:
            row['reason'] = 'Missing final object-stop label; label shared/end G-code explicitly'
        elif row['last_print_end'] is None:
            row['reason'] = row['reason'] or 'No labeled extrusion moves were found'
    return objects


def status(row, position, active):
    completed = bool(row and row['last_print_end'] is not None and position is not None and position >= row['last_print_end'])
    reason = (row or {}).get('reason')
    if not active:
        reason = 'No matching local print is active'
    elif row is None:
        reason = 'No unambiguous object labels were found'
    elif completed:
        reason = 'This object has finished being queued for printing'
    elif position is None:
        reason = 'Current print position is unavailable'
    return dict(completed=completed, cancellable=not bool(reason), reason=reason,
                last_print_end=(row or {}).get('last_print_end'))


def install(implementation, enabled, validate):
    """Guard API/helper cancellation and preserve non-motion printer state.

    The upstream registered helper retains a bound cancel_object method, but
    it dynamically calls _get_entry_byid. Guard that common lookup as well.
    Neither adapter performs file I/O on the G-code queue thread.
    """
    if getattr(implementation, '_rme_object_safety', False):
        return True
    lookup = getattr(implementation, '_get_entry_byid', None)
    allow = getattr(implementation, '_skip_allow', None)
    if not callable(lookup) or not callable(allow) or not callable(getattr(implementation, 'cancel_object', None)):
        return False
    if not all(hasattr(implementation, field) for field in
               ('allowed', 'object_list', 'reptag', 'skipping', 'startskip', 'endskip')):
        return False

    def checked_lookup(object_id):
        entry = lookup(object_id)
        if enabled() and entry is not None:
            error = validate(entry['object'])
            if error:
                from werkzeug.exceptions import Conflict
                raise Conflict(description=error)
        return entry

    def safe_allow(cmd):
        if enabled() and not motion_command(cmd):
            return cmd
        return allow(cmd)

    implementation._get_entry_byid = checked_lookup
    implementation._skip_allow = safe_allow
    implementation._rme_object_safety = True
    return True


def prepare_queue(implementation):
    # The upstream filter only calls _skip_allow when this list is nonempty.
    # This is a process-local sentinel, not a user setting or a permissive regex.
    if not implementation.allowed:
        implementation.allowed = ['RME non-motion safety']


def stop_span(implementation):
    # Upstream's optional stop-tags setting defaults off. Honor explicit stop
    # boundaries for RME unconditionally, including absolute-E restoration.
    if implementation.skipping:
        implementation.skipping = False
        implementation.endskip = True
    implementation.startskip = False
    implementation.active_object = 'None'
