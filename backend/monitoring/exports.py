"""Bounded, all-or-nothing CSV with text formula protection and original numbers."""
import csv
import io
from .contracts import CsvExport
from .errors import MonitoringError

MAX_ROWS = 10000
MAX_BYTES = 10 * 1024 * 1024


def cell(value):
    if value is None: return ''
    if isinstance(value, str) and value:
        # Leading whitespace can be ignored by spreadsheet formula parsers.
        stripped = value.lstrip()
        if (stripped and stripped[0] in '=+-@') or ord(value[0]) < 32:
            return "'" + value
    return value


def make_csv(filename, columns, rows):
    output = bytearray(b'\xef\xbb\xbf')
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    for index, values in enumerate([columns] if rows is None else _values(columns, rows)):
        if index > MAX_ROWS:
            raise MonitoringError('monitoring.export_limit', '导出超过 10,000 行，请缩小范围')
        writer.writerow([cell(value) for value in values])
        encoded = stream.getvalue().encode('utf-8')
        if len(output) + len(encoded) > MAX_BYTES:
            raise MonitoringError('monitoring.export_limit', '导出超过 10 MiB，请缩小范围')
        output.extend(encoded)
        stream.seek(0)
        stream.truncate(0)
    return CsvExport(filename, bytes(output))


def _values(columns, rows):
    yield columns
    for row in rows:
        yield [row.get(column) for column in columns]
